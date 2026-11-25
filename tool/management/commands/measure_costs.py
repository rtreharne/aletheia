"""Run each landing-page demo through the real engine, as a simulated student, and report what it cost.

Calls Claude for real (objectives and every question), so it spends money: roughly $0.10–0.40 per demo.

    python manage.py measure_costs                 # all six demos
    python manage.py measure_costs history --json out.json --keep
"""

import json
import random
import time
from unittest import mock

from django.core.management.base import BaseCommand
from django.utils import timezone

from tool import demos, services
from tool.models import Assignment, Attempt, Platform
from tool.utils import extract_text

ISSUER = "https://aletheia.cost-measurement"
SETTINGS = ("seconds_per_question", "time_each_question", "time_limit_minutes", "room_word_required", "code_viva",
            "objective_count", "questions_per_objective", "retries_per_objective")


class Command(BaseCommand):
    help = "Measure the Claude cost of one attempt at each demo, with a simulated student."

    def add_arguments(self, parser):
        parser.add_argument("slugs", nargs="*", help="Demos to run (default: all).")
        parser.add_argument("--correct", type=float, default=0.7, help="Chance the student answers correctly.")
        parser.add_argument("--seed", type=int, default=1)
        parser.add_argument("--json", help="Write the results (and the questions written) to this file.")
        parser.add_argument("--keep", action="store_true", help="Keep the attempts in the database.")

    def handle(self, *slugs, **options):
        rng = random.Random(options["seed"])
        chosen = [d for d in demos.DEMOS if not slugs or d["slug"] in slugs]
        platform, _ = Platform.objects.get_or_create(
            issuer=ISSUER, client_id="measure",
            defaults={"name": "Cost measurement", "auth_login_url": ISSUER + "/", "jwks_url": ISSUER + "/"},
        )
        results = []
        # Run generation inline, in order, instead of in background threads.
        with mock.patch.object(services, "run_in_background", side_effect=lambda fn, *args: fn(*args)):
            for demo in chosen:
                results.append(self.run_demo(platform, demo, rng, options["correct"]))
        if not options["keep"]:
            platform.delete()

        self.stdout.write("")
        self.stdout.write(f"{'Demo':<12}{'Chars':>8}{'Calls':>7}{'Qs made':>9}{'Asked':>7}{'In':>8}{'Write':>8}"
                          f"{'Read':>8}{'Out':>8}{'Cost $':>9}")
        for r in results:
            self.stdout.write(f"{r['slug']:<12}{r['chars']:>8}{r['requests']:>7}{r['questions_made']:>9}"
                              f"{r['questions_asked']:>7}{r['input']:>8}{r['cache_write']:>8}{r['cache_read']:>8}"
                              f"{r['output']:>8}{r['cost_usd']:>9.4f}")
        if options["json"]:
            with open(options["json"], "w") as f:
                json.dump(results, f, indent=1)

    def run_demo(self, platform, demo, rng, p_correct):
        code = demo["settings"].get("code_viva", False)
        text = extract_text(str(demos.file_path(demo)), code=code, name=demo["file"])
        fields = {
            "time_limit_minutes": 10, "time_each_question": True, "retries_per_objective": 1,
            "questions_per_objective": 1, "room_word_required": False, "code_viva": False, **demo["settings"],
        }
        assignment, _ = Assignment.objects.update_or_create(
            platform=platform, deployment_id="measure", resource_link_id=f"measure-{demo['slug']}",
            defaults={"title": demo["title"], "brief": demo["brief"], "configured": True, **fields},
        )
        attempt = Attempt.objects.create(
            assignment=assignment, lti_user_id="cost-measurement", user_name="Cost measurement",
            original_filename=demo["file"], submission_text=text, status=Attempt.GENERATING,
            **{k: getattr(assignment, k) for k in SETTINGS},
        )
        started = time.monotonic()
        self.stdout.write(f"{demo['slug']}: objectives…")
        services.generate_objectives(attempt.pk)
        attempt.refresh_from_db()
        if attempt.status != Attempt.READY:
            raise SystemExit(f"{demo['slug']}: objectives failed: {attempt.error}")
        attempt.status, attempt.started_at = Attempt.IN_PROGRESS, timezone.now()
        attempt.save(update_fields=["status", "started_at"])

        asked = wrong = 0
        while True:
            state, question = services.advance(attempt)
            if state == services.WAITING:
                question.refresh_from_db()
                if question.status == question.FAILED:
                    services.request_question(attempt, question.objective_index, question.position)
                continue
            if state != services.QUESTION:
                break
            right = rng.random() < p_correct
            selected = question.correct if right else [i for i in range(len(question.options)) if i not in question.correct][:1]
            services.submit_answer(attempt, question, selected, expired=False)
            asked += 1
            wrong += not right
            self.stdout.write(f"  objective {question.objective_index + 1} q{question.position + 1}: "
                              f"{'right' if right else 'wrong'}")
        attempt.refresh_from_db()
        return {
            "slug": demo["slug"], "chars": len(text), "status": attempt.status,
            "requests": attempt.ai_requests, "questions_made": attempt.questions.count(),
            "questions_asked": asked, "wrong": wrong,
            "input": attempt.ai_input_tokens, "output": attempt.ai_output_tokens,
            "cache_write": attempt.ai_cache_write_tokens, "cache_read": attempt.ai_cache_read_tokens,
            "cost_usd": round(attempt.ai_cost_usd, 5), "seconds": round(time.monotonic() - started, 1),
            "objectives": attempt.objectives,
            "questions": [
                {"objective": q.objective_index + 1, "position": q.position + 1, "kind": q.kind, "prompt": q.prompt,
                 "code": q.code, "options": q.options, "correct": q.correct, "explanation": q.explanation}
                for q in attempt.questions.order_by("objective_index", "position")
            ],
        }
