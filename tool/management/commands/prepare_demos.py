"""Write the landing-page demos' objectives and questions with Claude, once, and save them.

    python manage.py prepare_demos                 # any demo without questions.json
    python manage.py prepare_demos --force history # rewrite one
"""

import json
from concurrent.futures import ThreadPoolExecutor

from django.core.management.base import BaseCommand
from django.utils import timezone
from django.conf import settings

from tool import ai, demos, services
from tool.utils import extract_text


def prepare(demo):
    s = demo["settings"]
    code = s.get("code_viva", False)
    text = extract_text(str(demos.file_path(demo)), code=code, name=demo["file"])
    objectives = ai.generate_objectives(demo["title"], demo["brief"], text, s["objective_count"], code=code)
    slots = s.get("questions_per_objective", 1) + s.get("retries_per_objective", 1)
    seconds = s["seconds_per_question"] if s.get("time_each_question", True) else 45
    questions = []
    for index, objective in enumerate(objectives):
        for position in range(slots):
            kind = services.kind_for(index, position)
            avoid = [f"{q['prompt']} (options: {'; '.join(q['options'])})" for q in questions]
            q = ai.generate_question(demo["title"], demo["brief"], text, objective["text"], kind, seconds,
                                     avoid_prompts=avoid, code=code)
            questions.append({"objective_index": index, "position": position, "kind": kind, **q})
    data = {
        "slug": demo["slug"],
        "model": settings.ALETHEIA_AI_MODEL,
        "generated_at": timezone.now().isoformat(timespec="seconds"),
        "objectives": objectives,
        "questions": questions,
    }
    demos.questions_path(demo).write_text(json.dumps(data, indent=2, ensure_ascii=False) + "\n")
    return demo["slug"], len(questions)


class Command(BaseCommand):
    help = "Generate the landing-page demos' objectives and questions (calls Claude)."

    def add_arguments(self, parser):
        parser.add_argument("slugs", nargs="*", help="Only these demos.")
        parser.add_argument("--force", action="store_true", help="Rewrite demos that already have questions.")

    def handle(self, *args, **options):
        todo = [d for d in demos.DEMOS if not options["slugs"] or d["slug"] in options["slugs"]]
        todo = [d for d in todo if options["force"] or not demos.prepared(d)]
        if not todo:
            self.stdout.write("All demos are prepared.")
            return
        with ThreadPoolExecutor(max_workers=6) as pool:
            for slug, count in pool.map(prepare, todo):
                self.stdout.write(f"{slug}: {count} questions")
