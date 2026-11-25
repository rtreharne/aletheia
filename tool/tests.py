import json
import random
import re
import shutil
import tempfile
import time
from datetime import timedelta
from pathlib import Path
from unittest import mock
from urllib.parse import parse_qs, urlencode, urlparse

import jwt
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import TestCase, override_settings
from django.urls import reverse
from django.utils import timezone

from . import ai, lti, services
from .models import Assignment, Attempt, IntegrityEvent, Invitation, Platform, Question
from . import ags, nrps, registration
from django.core.cache import cache

TMP = Path(tempfile.mkdtemp(prefix="aletheia-tests-"))
PLATFORM_KEY = rsa.generate_private_key(public_exponent=65537, key_size=2048)
ISSUER = "https://lms.example.edu"
CLIENT_ID = "client-123"
DEPLOYMENT = "dep-1"
LEARNER = "http://purl.imsglobal.org/vocab/lis/v2/membership#Learner"
INSTRUCTOR = "http://purl.imsglobal.org/vocab/lis/v2/membership#Instructor"

SUBMISSION = ("Photosynthesis converts light energy into chemical energy. " * 20).encode()

OBJECTIVES = [{"text": f"Objective {n}"} for n in range(1, 11)]


def fake_question(title, brief, submission_text, objective, kind, seconds, avoid_prompts=(), code=False, on_usage=None):
    """Deterministic stand-in for Claude: the correct answer is always option 0 (and 1 for MAQ)."""
    n = len(avoid_prompts) + 1
    snippet = "def total(items):\n    return sum(items)" if code else ""
    if kind == "mcq":
        return {"prompt": f"{objective} Q{n}?", "code": snippet, "options": ["right", "w1", "w2", "w3"], "correct": [0],
                "explanation": f"Explanation {n}."}
    return {"prompt": f"{objective} Q{n}?", "code": snippet, "options": ["right", "also right", "w1", "w2"],
            "correct": [0, 1], "explanation": f"Explanation {n}."}


def tearDownModule():
    shutil.rmtree(TMP, ignore_errors=True)


@override_settings(
    LTI_PRIVATE_KEY_PATH=TMP / "keys" / "private.pem",
    LTI_PUBLIC_KEY_PATH=TMP / "keys" / "public.pem",
    MEDIA_ROOT=TMP / "media",
    SECURE_SSL_REDIRECT=False,
)
class Base(TestCase):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        lti.generate_keys(force=True)

    def setUp(self):
        self.platform = Platform.objects.create(
            name="Test LMS",
            issuer=ISSUER,
            client_id=CLIENT_ID,
            deployment_ids=DEPLOYMENT,
            auth_login_url="https://lms.example.edu/auth",
            jwks_url="https://lms.example.edu/jwks",
        )
        patcher = mock.patch.object(lti, "_platform_signing_key", return_value=PLATFORM_KEY.public_key())
        patcher.start()
        self.addCleanup(patcher.stop)

    def id_token(self, nonce, roles, sub="user-1", message_type="LtiResourceLinkRequest", **extra):
        now = int(time.time())
        claims = {
            "iss": ISSUER,
            "aud": CLIENT_ID,
            "sub": sub,
            "iat": now,
            "exp": now + 300,
            "nonce": nonce,
            "name": f"Person {sub}",
            lti.CLAIM + "version": "1.3.0",
            lti.CLAIM + "deployment_id": DEPLOYMENT,
            lti.CLAIM + "message_type": message_type,
            lti.CLAIM + "roles": roles,
            lti.CLAIM + "resource_link": {"id": "link-1", "title": "Essay check"},
            lti.CLAIM + "context": {"id": "course-1", "title": "Biology 101"},
        }
        claims.update(extra)
        return jwt.encode(claims, PLATFORM_KEY, algorithm="RS256")

    def launch(self, roles, sub="user-1", client=None, **extra):
        client = client or self.client
        resp = client.get(reverse("lti_login"), {"iss": ISSUER, "login_hint": "hint", "client_id": CLIENT_ID})
        self.assertEqual(resp.status_code, 302)
        query = parse_qs(urlparse(resp["Location"]).query)
        token = self.id_token(query["nonce"][0], roles, sub=sub, **extra)
        return client.post(reverse("lti_launch"), {"id_token": token, "state": query["state"][0]})

    def hand_in(self, assignment, upload, client=None):
        """Upload a file, then tick both boxes and confirm, as a student would."""
        client = client or self.client
        resp = client.post(reverse("submit", args=[assignment.pk]), {"file": upload})
        draft = Attempt.objects.filter(assignment=assignment, status=Attempt.DRAFT).order_by("-pk").first()
        if draft is None:
            return resp
        return client.post(reverse("confirm", args=[assignment.pk, draft.pk]), {"correct_file": "1", "ai_policy": "1"})

    def configured_assignment(self, **kwargs):
        self.launch([INSTRUCTOR], sub="teacher")
        assignment = Assignment.objects.get()
        for key, value in {"configured": True, "objective_count": 3, "title": "Essay check", **kwargs}.items():
            setattr(assignment, key, value)
        assignment.save()
        return assignment


class LaunchTests(Base):
    def test_instructor_first_launch_goes_to_settings(self):
        resp = self.launch([INSTRUCTOR], sub="teacher")
        assignment = Assignment.objects.get()
        self.assertRedirects(resp, reverse("assignment", args=[assignment.pk]), fetch_redirect_response=False)
        resp = self.client.get(reverse("assignment", args=[assignment.pk]))
        self.assertRedirects(resp, reverse("settings", args=[assignment.pk]))
        self.assertEqual(assignment.context_title, "Biology 101")

    def test_settings_save_configures(self):
        self.launch([INSTRUCTOR], sub="teacher")
        assignment = Assignment.objects.get()
        resp = self.client.post(
            reverse("settings", args=[assignment.pk]),
            {"title": "Check", "brief": "Focus on method", "objective_count": 6, "questions_per_objective": 1,
             "retries_per_objective": 1, "seconds_per_question": 45, "max_attempts": 2},
        )
        self.assertRedirects(resp, reverse("assignment", args=[assignment.pk]), fetch_redirect_response=False)
        assignment.refresh_from_db()
        self.assertTrue(assignment.configured)
        self.assertEqual(assignment.objective_count, 6)

    def test_replayed_state_is_rejected(self):
        resp = self.client.get(reverse("lti_login"), {"iss": ISSUER, "login_hint": "x"})
        query = parse_qs(urlparse(resp["Location"]).query)
        data = {"id_token": self.id_token(query["nonce"][0], [LEARNER]), "state": query["state"][0]}
        self.assertEqual(self.client.post(reverse("lti_launch"), data).status_code, 302)
        self.assertEqual(self.client.post(reverse("lti_launch"), data).status_code, 400)

    def test_wrong_nonce_audience_and_deployment_rejected(self):
        for extra in ({"aud": "someone-else"}, {lti.CLAIM + "deployment_id": "other"}):
            resp = self.launch([LEARNER], **extra)
            self.assertEqual(resp.status_code, 400, extra)
        resp = self.client.get(reverse("lti_login"), {"iss": ISSUER, "login_hint": "x"})
        query = parse_qs(urlparse(resp["Location"]).query)
        token = self.id_token("wrong-nonce", [LEARNER])
        resp = self.client.post(reverse("lti_launch"), {"id_token": token, "state": query["state"][0]})
        self.assertEqual(resp.status_code, 400)

    def test_unknown_platform(self):
        resp = self.client.get(reverse("lti_login"), {"iss": "https://elsewhere", "login_hint": "x"})
        self.assertEqual(resp.status_code, 400)

    def test_student_cannot_open_settings_or_review(self):
        assignment = self.configured_assignment()
        self.client.logout()
        self.launch([LEARNER])
        self.assertEqual(self.client.get(reverse("settings", args=[assignment.pk])).status_code, 403)

    def test_without_launch_access_denied(self):
        assignment = Assignment.objects.create(platform=self.platform, deployment_id=DEPLOYMENT, resource_link_id="x")
        self.assertEqual(self.client.get(reverse("assignment", args=[assignment.pk])).status_code, 403)

    def test_jwks(self):
        keys = self.client.get(reverse("lti_jwks")).json()["keys"]
        self.assertEqual(keys[0]["alg"], "RS256")
        self.assertIn("kid", keys[0])

    def test_deep_link_response_is_signed_by_tool(self):
        dl_settings = {lti.DL_CLAIM + "deep_linking_settings": {"deep_link_return_url": "https://lms.example.edu/return", "data": "abc"}}
        resp = self.launch([INSTRUCTOR], message_type="LtiDeepLinkingRequest", **dl_settings)
        self.assertRedirects(resp, reverse("deep_link"), fetch_redirect_response=False)
        resp = self.client.post(reverse("deep_link"), {"title": "Week 3 check"})
        token = resp.context["jwt"]
        public = serialization.load_pem_public_key((TMP / "keys" / "public.pem").read_bytes())
        claims = jwt.decode(token, public, algorithms=["RS256"], audience=ISSUER)
        self.assertEqual(claims["iss"], CLIENT_ID)
        self.assertEqual(claims[lti.DL_CLAIM + "data"], "abc")
        self.assertEqual(claims[lti.DL_CLAIM + "content_items"][0]["title"], "Week 3 check")


class ValidationTests(TestCase):
    def test_objectives_trimmed_and_counted(self):
        data = {"objectives": [{"text": " A "}, {"text": ""}, {"text": "B"}, {"text": "C"}]}
        self.assertEqual(ai.validate_objectives(data, 2), [{"text": "A"}, {"text": "B"}])
        with self.assertRaises(ai.GenerationError):
            ai.validate_objectives(data, 4)

    def test_question_rules(self):
        good_mcq = {"prompt": "Q?", "options": ["a", "b", "c", "d"], "correct": [2]}
        self.assertEqual(ai.validate_question(good_mcq, "mcq")["correct"], [2])
        bad = [
            ({**good_mcq, "correct": [0, 1]}, "mcq"),  # single answer with two correct
            ({**good_mcq, "correct": [0]}, "maq"),  # multiple answer with one correct
            ({**good_mcq, "correct": [0, 1, 2, 3]}, "maq"),  # every option correct
            ({**good_mcq, "correct": [7]}, "mcq"),
            ({**good_mcq, "prompt": " "}, "mcq"),
        ]
        for data, kind in bad:
            with self.assertRaises(ai.GenerationError, msg=data):
                ai.validate_question(data, kind)

    def test_question_text_tidied(self):
        data = {"prompt": "Which of these  apply? (Select all that apply)",
                "options": ["shade.", "Transpiration", "wind", "soil"], "correct": [0, 1]}
        q = ai.validate_question({**data, "explanation": " Shade  cools. "}, "maq")
        self.assertEqual(q["prompt"], "Which of these apply?")
        self.assertEqual(q["explanation"], "Shade cools.")
        self.assertEqual(q["options"], ["Shade", "Transpiration", "Wind", "Soil"])
        with self.assertRaises(ai.GenerationError):
            ai.validate_question({**data, "options": ["A", "a", "b", "c"]}, "maq")

    def test_shuffle_keeps_correct_answer(self):
        q = {"options": ["right", "w1", "w2", "w3"], "correct": [0], "kind": "mcq"}
        for seed in range(10):
            s = ai.shuffle_options(q, random.Random(seed))
            self.assertEqual([s["options"][i] for i in s["correct"]], ["right"])

    def test_kinds_alternate(self):
        self.assertEqual([services.kind_for(0, p) for p in range(3)], ["mcq", "maq", "mcq"])
        self.assertEqual(services.kind_for(1, 0), "maq")


@mock.patch.object(services, "run_in_background", side_effect=lambda fn, *args: fn(*args))
@mock.patch.object(ai, "generate_question", side_effect=fake_question)
@mock.patch.object(ai, "generate_objectives", side_effect=lambda title, brief, submission_text, count, code=False, on_usage=None: OBJECTIVES[:count])
class StudentFlowTests(Base):
    def upload(self, assignment):
        return self.hand_in(assignment, SimpleUploadedFile("essay.txt", SUBMISSION, content_type="text/plain"))

    def start(self, assignment):
        self.client.logout()
        self.launch([LEARNER])
        self.upload(assignment)
        attempt = Attempt.objects.get(lti_user_id="user-1", status=Attempt.READY)
        self.client.post(reverse("begin", args=[assignment.pk, attempt.pk]))
        return attempt

    def show(self, assignment, attempt):
        return self.client.get(reverse("attempt", args=[assignment.pk, attempt.pk]))

    def answer(self, assignment, attempt, right=True, expired=False):
        question = self.show(assignment, attempt).context["question"]
        options = question.correct if right else [i for i in range(len(question.options)) if i not in question.correct][:1]
        data = {"question": question.pk, "option": options, "expired": "1" if expired else "0"}
        self.client.post(reverse("answer", args=[assignment.pk, attempt.pk]), data)
        return Question.objects.get(pk=question.pk)

    def test_objectives_then_first_question_prepared(self, *_):
        assignment = self.configured_assignment()
        self.client.logout()
        self.launch([LEARNER])
        self.upload(assignment)
        attempt = Attempt.objects.get()
        self.assertEqual(attempt.status, Attempt.READY)
        self.assertEqual(attempt.objectives, OBJECTIVES[:3])
        self.assertEqual(list(attempt.questions.values_list("objective_index", "position", "status")), [(0, 0, "ready")])
        resp = self.show(assignment, attempt)
        self.assertContains(resp, "Objective 2")  # listed in the overlay

    def test_wrong_answer_retries_objective_then_moves_on(self, *_):
        assignment = self.configured_assignment()
        attempt = self.start(assignment)

        q = self.answer(assignment, attempt, right=True)
        self.assertEqual((q.objective_index, q.position), (0, 0))
        q = self.answer(assignment, attempt, right=False)
        self.assertEqual((q.objective_index, q.position), (1, 0))
        q = self.answer(assignment, attempt, right=True)  # retry on objective 2
        self.assertEqual((q.objective_index, q.position), (1, 1))
        q = self.answer(assignment, attempt, right=False)
        self.assertEqual((q.objective_index, q.position), (2, 0))
        q = self.answer(assignment, attempt, right=False)  # retry on objective 3, also wrong
        self.assertEqual((q.objective_index, q.position), (2, 1))

        attempt.refresh_from_db()
        self.assertEqual(attempt.status, Attempt.COMPLETED)
        self.assertEqual([o["alignment"] for o in attempt.objective_alignment], [100, 50, 0])
        self.assertEqual(attempt.overall_alignment, 50.0)
        resp = self.show(assignment, attempt)
        self.assertContains(resp, "Alignment")
        self.assertContains(resp, "50%")
        self.assertNotContains(resp, "Q1?")  # questions are never shown back to students

    def test_more_questions_per_objective(self, *_):
        assignment = self.configured_assignment(objective_count=1, questions_per_objective=2, retries_per_objective=1)
        attempt = self.start(assignment)
        self.answer(assignment, attempt, right=True)
        self.answer(assignment, attempt, right=False)
        self.answer(assignment, attempt, right=True)  # needs a second correct: gets one retry
        attempt.refresh_from_db()
        self.assertEqual(attempt.status, Attempt.COMPLETED)
        self.assertEqual(attempt.objective_alignment[0]["correct"], 2)
        self.assertEqual(attempt.objective_alignment[0]["total"], 3)

    def test_no_retries_setting(self, *_):
        assignment = self.configured_assignment(objective_count=2, retries_per_objective=0)
        attempt = self.start(assignment)
        self.answer(assignment, attempt, right=False)
        q = self.answer(assignment, attempt, right=False)
        self.assertEqual(q.objective_index, 1)
        attempt.refresh_from_db()
        self.assertEqual(attempt.status, Attempt.COMPLETED)

    def test_retries_written_only_after_a_wrong_answer(self, *_):
        assignment = self.configured_assignment(objective_count=3)
        attempt = self.start(assignment)
        self.answer(assignment, attempt, right=True)
        self.assertFalse(attempt.questions.filter(position=1).exists())  # no retry prepared in advance
        self.answer(assignment, attempt, right=False)
        self.show(assignment, attempt)  # the next page asks for the retry
        self.assertTrue(attempt.questions.filter(objective_index=1, position=1).exists())

    def test_unused_prefetched_questions_not_shown_to_instructor(self, *_):
        assignment = self.configured_assignment(objective_count=3)
        attempt = self.start(assignment)
        self.answer(assignment, attempt, right=True)
        self.answer(assignment, attempt, right=True)
        attempt.questions.filter(objective_index=2).update(served_at=None, order=None)  # prepared, not yet shown
        self.assertEqual(attempt.questions.count(), 3)
        instructor = self.client_class()
        self.launch([INSTRUCTOR], sub="teacher", client=instructor)
        resp = instructor.get(reverse("attempt_detail", args=[assignment.pk, attempt.pk]))
        self.assertEqual(len(resp.context["questions"]), 2)

    def test_waiting_while_question_generates(self, *_):
        assignment = self.configured_assignment()
        attempt = self.start(assignment)
        self.answer(assignment, attempt, right=True)
        # The next question was requested but its generation hasn't finished.
        Question.objects.filter(attempt=attempt, objective_index=1, position=0).update(
            status=Question.PENDING, prompt="", requested_at=timezone.now()
        )
        resp = self.show(assignment, attempt)
        self.assertTemplateUsed(resp, "tool/waiting.html")
        status = self.client.get(reverse("attempt_status", args=[assignment.pk, attempt.pk])).json()
        self.assertTrue(status["waiting"])
        self.assertFalse(Question.objects.get(attempt=attempt, objective_index=1, position=0).served_at)

    def test_failed_question_is_retried_then_fails_attempt(self, generate_objectives, generate_question, background):
        generate_question.side_effect = ai.GenerationError("nope")
        assignment = self.configured_assignment()
        self.client.logout()
        self.launch([LEARNER])
        self.upload(assignment)
        attempt = Attempt.objects.get()
        self.client.post(reverse("begin", args=[assignment.pk, attempt.pk]))
        for _ in range(services.MAX_QUESTION_TRIES):
            self.show(assignment, attempt)
        attempt.refresh_from_db()
        self.assertEqual(attempt.status, Attempt.FAILED)
        self.assertEqual(generate_question.call_count, services.MAX_QUESTION_TRIES)

    def test_answer_times_recorded(self, *_):
        assignment = self.configured_assignment(objective_count=3)
        attempt = self.start(assignment)
        for seconds, right in ((4, True), (10, True), (None, False)):
            question = self.show(assignment, attempt).context["question"]
            if seconds is None:  # let this one run out
                Question.objects.filter(pk=question.pk).update(served_at=timezone.now() - timedelta(minutes=5))
                self.show(assignment, attempt)
                continue
            Question.objects.filter(pk=question.pk).update(served_at=timezone.now() - timedelta(seconds=seconds))
            self.client.post(
                reverse("answer", args=[assignment.pk, attempt.pk]),
                {"question": question.pk, "option": question.correct if right else []},
            )
        self.answer(assignment, attempt, right=True)  # follow-up on objective 3
        attempt.refresh_from_db()
        self.assertEqual(attempt.status, Attempt.COMPLETED)
        self.assertEqual(attempt.questions.get(order=0).seconds_allowed, 45)
        self.assertAlmostEqual(attempt.questions.get(order=0).answer_seconds, 4, delta=1)
        self.assertIsNone(attempt.questions.get(order=2).answer_seconds)  # timed out: excluded
        self.assertAlmostEqual(attempt.median_answer_seconds, 4, delta=1)  # median of ~4, ~10, ~0

        instructor = self.client_class()
        self.launch([INSTRUCTOR], sub="teacher", client=instructor)
        self.assertContains(instructor.get(reverse("assignment", args=[assignment.pk])), "Median time")
        resp = self.show(assignment, attempt)
        self.assertNotContains(resp, "Median")  # students never see speed

    def test_late_answer_scores_nothing(self, *_):
        assignment = self.configured_assignment()
        attempt = self.start(assignment)
        question = self.show(assignment, attempt).context["question"]
        Question.objects.filter(pk=question.pk).update(served_at=timezone.now() - timedelta(seconds=70))
        self.client.post(
            reverse("answer", args=[assignment.pk, attempt.pk]),
            {"question": question.pk, "option": question.correct},
        )
        question.refresh_from_db()
        self.assertFalse(question.is_correct)
        self.assertTrue(question.timed_out)

    def test_abandoned_question_expires_on_return(self, *_):
        assignment = self.configured_assignment()
        attempt = self.start(assignment)
        question = self.show(assignment, attempt).context["question"]
        Question.objects.filter(pk=question.pk).update(served_at=timezone.now() - timedelta(minutes=5))
        resp = self.show(assignment, attempt)
        question.refresh_from_db()
        self.assertTrue(question.timed_out)
        # The miss earns a follow-up on the same objective.
        self.assertEqual((resp.context["question"].objective_index, resp.context["question"].position), (0, 1))

    def test_reload_does_not_reset_timer_or_skip(self, *_):
        assignment = self.configured_assignment()
        attempt = self.start(assignment)
        first = self.show(assignment, attempt).context["question"]
        again = self.show(assignment, attempt).context["question"]
        self.assertEqual(first.pk, again.pk)
        self.assertEqual(first.served_at, again.served_at)

    def test_cannot_answer_unserved_question(self, *_):
        assignment = self.configured_assignment()
        attempt = self.start(assignment)
        self.show(assignment, attempt)
        later = Question.objects.get(attempt=attempt, objective_index=1, position=0)
        self.client.post(reverse("answer", args=[assignment.pk, attempt.pk]), {"question": later.pk, "option": later.correct})
        later.refresh_from_db()
        self.assertIsNone(later.answered_at)

    def test_attempt_limit(self, *_):
        assignment = self.configured_assignment(objective_count=1, max_attempts=1)
        attempt = self.start(assignment)
        self.answer(assignment, attempt, right=True)
        self.upload(assignment)
        self.assertEqual(Attempt.objects.filter(lti_user_id="user-1").count(), 1)

    def test_other_student_cannot_see_attempt(self, *_):
        assignment = self.configured_assignment()
        attempt = self.start(assignment)
        self.client.logout()
        self.launch([LEARNER], sub="user-2")
        self.assertEqual(self.show(assignment, attempt).status_code, 404)

    def test_integrity_events_logged_and_shown(self, *_):
        assignment = self.configured_assignment()
        attempt = self.start(assignment)
        url = reverse("log_event", args=[assignment.pk, attempt.pk])
        for kind in ("hidden", "paste", "bogus"):
            self.client.post(url, json.dumps({"kind": kind}), content_type="application/json")
        self.assertEqual(attempt.events.count(), 2)
        self.assertEqual(attempt.integrity_flag_count(), 2)

        instructor = self.client_class()
        self.launch([INSTRUCTOR], sub="teacher", client=instructor)
        resp = instructor.get(reverse("assignment", args=[assignment.pk]))
        self.assertContains(resp, "user-1, Person")
        resp = instructor.get(reverse("attempt_detail", args=[assignment.pk, attempt.pk]))
        self.assertContains(resp, "Paste")

    def test_unreadable_upload_rejected(self, *_):
        assignment = self.configured_assignment()
        self.client.logout()
        self.launch([LEARNER])
        resp = self.client.post(
            reverse("submit", args=[assignment.pk]),
            {"file": SimpleUploadedFile("tiny.txt", b"too short", content_type="text/plain")},
        )
        self.assertEqual(resp.status_code, 400)
        self.assertFalse(Attempt.objects.exists())

    def test_objective_failure_does_not_use_attempt(self, generate_objectives, generate_question, background):
        generate_objectives.side_effect = ai.GenerationError("nope")
        assignment = self.configured_assignment(max_attempts=1)
        self.client.logout()
        self.launch([LEARNER])
        self.upload(assignment)
        self.assertEqual(Attempt.objects.get().status, Attempt.FAILED)
        resp = self.client.get(reverse("assignment", args=[assignment.pk]))
        self.assertContains(resp, "couldn't prepare questions")
        self.assertTrue(resp.context["can_start"])

    def test_quiet_note_after_leaving_the_page(self, *_):
        assignment = self.configured_assignment(objective_count=3, retries_per_objective=0)
        self.client.logout()
        self.launch([LEARNER])
        self.upload(assignment)
        attempt = Attempt.objects.get(lti_user_id="user-1", status=Attempt.READY)
        self.assertContains(self.show(assignment, attempt), "recorded and shown to your instructor")  # told up front
        self.client.post(reverse("begin", args=[assignment.pk, attempt.pk]))
        self.answer(assignment, attempt, right=True)
        self.assertNotContains(self.show(assignment, attempt), "You left the page")
        page = self.show(assignment, attempt)
        second = page.context["question"]
        IntegrityEvent.objects.create(attempt=attempt, question=second, kind="hidden")
        IntegrityEvent.objects.create(attempt=attempt, question=second, kind="visible")
        self.assertNotContains(self.show(assignment, attempt), "You left the page")  # not mid-question
        self.answer(assignment, attempt, right=True)
        self.assertContains(self.show(assignment, attempt), "You left the page during the last question")

    def test_feedback_shows_answers_explanations_and_times_away(self, *_):
        assignment = self.configured_assignment(objective_count=2, retries_per_objective=0)
        attempt = self.start(assignment)
        feedback_url = reverse("feedback", args=[assignment.pk, attempt.pk])
        self.assertRedirects(self.client.get(feedback_url), reverse("attempt", args=[assignment.pk, attempt.pk]),
                             fetch_redirect_response=False)  # not until the attempt is complete

        right = self.answer(assignment, attempt, right=True)
        IntegrityEvent.objects.create(attempt=attempt, question=right, kind="hidden")
        IntegrityEvent.objects.create(attempt=attempt, question=right, kind="visible")
        IntegrityEvent.objects.create(attempt=attempt, question=right, kind="blur")
        IntegrityEvent.objects.create(attempt=attempt, question=right, kind="copy")
        wrong = self.answer(assignment, attempt, right=False)
        Question.objects.filter(pk=right.pk).update(explanation="Shown only to instructors.")
        Question.objects.filter(pk=wrong.pk).update(explanation="Why the right answer is right.")
        attempt.refresh_from_db()
        self.assertEqual(attempt.status, Attempt.COMPLETED)

        resp = self.show(assignment, attempt)
        self.assertContains(resp, feedback_url)
        resp = self.client.get(feedback_url)
        self.assertContains(resp, right.prompt)
        self.assertContains(resp, wrong.prompt)
        self.assertContains(resp, "Why the right answer is right.")
        self.assertNotContains(resp, "Shown only to instructors.")  # no explanation for a correct answer
        self.assertContains(resp, "You left the page 2 times while answering.")
        self.assertNotContains(resp, "follow-up")
        self.assertNotRegex(resp.content.decode(), r"\d\.\ds")  # no answer times for students

        other = self.client_class()
        self.launch([LEARNER], sub="user-2", client=other)
        self.assertEqual(other.get(feedback_url).status_code, 404)

        instructor = self.client_class()
        self.launch([INSTRUCTOR], sub="teacher", client=instructor)
        resp = instructor.get(reverse("attempt_detail", args=[assignment.pk, attempt.pk]))
        self.assertContains(resp, "Shown only to instructors.")
        self.assertContains(resp, "Why the right answer is right.")

    def test_dashboard_flags_open_integrity_log(self, *_):
        assignment = self.configured_assignment(objective_count=1, retries_per_objective=0)
        attempt = self.start(assignment)
        question = self.answer(assignment, attempt, right=True)
        start = timezone.now()
        IntegrityEvent.objects.create(attempt=attempt, question=question, kind="blur", created_at=start)
        IntegrityEvent.objects.create(attempt=attempt, question=question, kind="focus", created_at=start + timedelta(seconds=12))
        IntegrityEvent.objects.create(attempt=attempt, question=question, kind="paste", created_at=start + timedelta(seconds=20))
        instructor = self.client_class()
        self.launch([INSTRUCTOR], sub="teacher", client=instructor)
        resp = instructor.get(reverse("assignment", args=[assignment.pk]))
        self.assertContains(resp, 'class="log-open flag" data-log="log-1"')
        self.assertContains(resp, ">2</button>")  # blur + paste are flags; focus isn't
        self.assertContains(resp, 'id="log-1"')
        self.assertContains(resp, "Window regained focus <span class=\"muted\">after 12s</span>")
        self.assertContains(resp, reverse("attempt_detail", args=[assignment.pk, attempt.pk]))

    def test_whole_quiz_time_limit(self, *_):
        assignment = self.configured_assignment(objective_count=3, time_each_question=False, time_limit_minutes=10)
        attempt = self.start(assignment)
        attempt.refresh_from_db()
        self.assertFalse(attempt.time_each_question)
        page = self.show(assignment, attempt)
        question = page.context["question"]
        self.assertAlmostEqual(question.seconds_allowed, 600, delta=2)  # the question has the quiz's time left
        self.assertEqual(page.context["timer_total"], 600)
        self.assertContains(page, 'id="clock"')
        self.answer(assignment, attempt, right=True)  # objective 1 cleared

        # Nine and a half minutes later: the next question only gets what's left of the quiz.
        Attempt.objects.filter(pk=attempt.pk).update(started_at=timezone.now() - timedelta(minutes=9, seconds=30))
        attempt.refresh_from_db()
        question = self.show(assignment, attempt).context["question"]
        self.assertLessEqual(question.seconds_allowed, 31)

        # Time is up: the attempt ends and unreached objectives count as 0.
        Attempt.objects.filter(pk=attempt.pk).update(started_at=timezone.now() - timedelta(minutes=11))
        Question.objects.filter(pk=question.pk).update(served_at=timezone.now() - timedelta(minutes=2))
        resp = self.show(assignment, attempt)
        attempt.refresh_from_db()
        self.assertEqual(attempt.status, Attempt.COMPLETED)
        self.assertTrue(attempt.ran_out_of_time)
        self.assertEqual([o["alignment"] for o in attempt.objective_alignment], [100, 0, 0])
        self.assertEqual([o["not_reached"] for o in attempt.objective_alignment], [False, False, True])
        self.assertAlmostEqual(attempt.overall_alignment, 33.3)
        self.assertContains(resp, "Not reached")
        self.assertContains(resp, "Time ran out")

    def test_feedback_when_never_away(self, *_):
        assignment = self.configured_assignment(objective_count=1, retries_per_objective=0)
        attempt = self.start(assignment)
        self.answer(assignment, attempt, right=True)
        resp = self.client.get(reverse("feedback", args=[assignment.pk, attempt.pk]))
        self.assertContains(resp, "You stayed on the page throughout.")


FRAME = {"HTTP_SEC_FETCH_DEST": "iframe"}


@mock.patch.object(services, "run_in_background", side_effect=lambda fn, *args: fn(*args))
@mock.patch.object(ai, "generate_question", side_effect=fake_question)
@mock.patch.object(ai, "generate_objectives", side_effect=lambda title, brief, submission_text, count, code=False, on_usage=None: OBJECTIVES[:count])
class NewTabTests(Base):
    def ready(self, **settings_):
        assignment = self.configured_assignment(objective_count=1, retries_per_objective=0, **settings_)
        self.client.logout()
        self.launch([LEARNER])
        self.hand_in(assignment, SimpleUploadedFile("essay.txt", SUBMISSION, content_type="text/plain"))
        return assignment, Attempt.objects.get()

    def handoff_url(self, assignment, attempt):
        page = self.client.get(reverse("attempt", args=[assignment.pk, attempt.pk]), **FRAME)
        self.assertContains(page, 'target="_blank" id="begin-form"')
        token = page.context["handoff"]
        return reverse("start_in_tab", args=[assignment.pk, attempt.pk]) + "?" + urlencode({"t": token})

    def test_begin_opens_questions_in_a_new_tab(self, *_):
        assignment, attempt = self.ready()
        url = self.handoff_url(assignment, attempt)
        self.assertEqual(self.handoff_url(assignment, attempt), url)  # reloading the frame keeps the same link

        tab = self.client_class()  # a new tab with none of the frame's cookies
        resp = tab.get(url)
        self.assertRedirects(resp, reverse("attempt", args=[assignment.pk, attempt.pk]), fetch_redirect_response=False)
        attempt.refresh_from_db()
        self.assertEqual(attempt.status, Attempt.IN_PROGRESS)
        self.assertEqual(tab.get(url).status_code, 403)  # one use only

        # The LMS frame now just points at the tab.
        frame = self.client.get(reverse("attempt", args=[assignment.pk, attempt.pk]), **FRAME)
        self.assertContains(frame, "open in another tab")

        # Answer in the tab; finishing there tells the student to go back to their course.
        question = tab.get(reverse("attempt", args=[assignment.pk, attempt.pk])).context["question"]
        tab.post(reverse("answer", args=[assignment.pk, attempt.pk]),
                 {"question": question.pk, "option": question.correct, "expired": "0"})
        done = tab.get(reverse("attempt", args=[assignment.pk, attempt.pk]))
        self.assertContains(done, "close this tab")
        self.assertNotContains(done, "http-equiv")
        # ...while the LMS frame shows the result.
        result = self.client.get(reverse("attempt", args=[assignment.pk, attempt.pk]), **FRAME)
        self.assertContains(result, "Review your answers")

    def test_room_word_checked_in_the_new_tab(self, *_):
        assignment, attempt = self.ready(room_word_required=True)
        url = self.handoff_url(assignment, attempt)
        tab = self.client_class()
        resp = tab.get(url + "&room_word=wrongword")
        self.assertContains(resp, "isn&#x27;t the word on screen", status_code=400)
        attempt.refresh_from_db()
        self.assertEqual(attempt.status, Attempt.READY)
        self.assertTrue(attempt.events.filter(kind="room_start_wrong").exists())

    def test_forged_or_expired_link_refused(self, *_):
        assignment, attempt = self.ready()
        url = reverse("start_in_tab", args=[assignment.pk, attempt.pk])
        self.assertEqual(self.client_class().get(url + "?t=forged").status_code, 403)

    def test_outside_the_lms_frame_nothing_changes(self, *_):
        assignment, attempt = self.ready()
        page = self.client.get(reverse("attempt", args=[assignment.pk, attempt.pk]))
        self.assertNotContains(page, reverse("start_in_tab", args=[assignment.pk, attempt.pk]))
        self.assertContains(page, reverse("begin", args=[assignment.pk, attempt.pk]))


from django.core import mail
from . import booking, demos
from .models import Enquiry


@override_settings(MEDIA_ROOT=TMP / "media")
class ReviewTests(Base):
    """The instructor's review of one attempt: headline flags, and inviting the student to try again."""

    def setUp(self):
        super().setUp()
        self.assignment = self.configured_assignment(max_attempts=1)  # self.client is now the teacher
        self.attempt = Attempt.objects.create(
            assignment=self.assignment, lti_user_id="user-1", user_name="Ada Lovelace", submission_text="x",
            status=Attempt.COMPLETED, overall_alignment=60, original_filename="essay.txt",
        )
        self.student = self.client_class()
        self.launch([LEARNER], sub="user-1", client=self.student)

    def review(self):
        return self.client.get(reverse("attempt_detail", args=[self.assignment.pk, self.attempt.pk]))

    def test_flags_are_headline_figures(self):
        for kind in ["hidden", "visible", "blur", "focus", "paste", "room_failed", "room_passed"]:
            IntegrityEvent.objects.create(attempt=self.attempt, kind=kind)
        resp = self.review()
        self.assertEqual(resp.context["flags"], 4)
        self.assertEqual(resp.context["flag_parts"],
                         [(2, "left the page"), (1, "copy, cut or paste"), (1, "room word missed")])
        self.assertContains(resp, '<div class="figure flagged">', html=False)

    def test_no_flags(self):
        resp = self.review()
        self.assertEqual(resp.context["flags"], 0)
        self.assertContains(resp, "None recorded")

    def test_invite_gives_one_more_attempt_until_used(self):
        home = reverse("assignment", args=[self.assignment.pk])
        self.assertFalse(self.student.get(home).context["can_start"])

        invite = reverse("invite", args=[self.assignment.pk, self.attempt.pk])
        self.client.post(invite)
        self.client.post(invite)  # a second click while pending changes nothing
        self.assertEqual(Invitation.objects.count(), 1)
        self.assertContains(self.review(), "waiting for a new attempt")
        resp = self.student.get(home)
        self.assertTrue(resp.context["can_start"])
        self.assertContains(resp, "invited you to try again")

        Attempt.objects.create(assignment=self.assignment, lti_user_id="user-1", submission_text="x",
                               number=2, status=Attempt.COMPLETED)
        resp = self.student.get(home)
        self.assertFalse(resp.context["can_start"])
        self.assertNotContains(resp, "invited you to try again")
        self.assertContains(self.review(), "Invite to try again")

    def test_withdraw_pending_invitation(self):
        self.client.post(reverse("invite", args=[self.assignment.pk, self.attempt.pk]))
        self.client.post(reverse("withdraw_invitation", args=[self.assignment.pk, self.attempt.pk]))
        self.assertFalse(Invitation.objects.exists())
        self.assertFalse(self.student.get(reverse("assignment", args=[self.assignment.pk])).context["can_start"])

    def test_students_cannot_invite_themselves(self):
        resp = self.student.post(reverse("invite", args=[self.assignment.pk, self.attempt.pk]))
        self.assertEqual(resp.status_code, 403)
        self.assertFalse(Invitation.objects.exists())
        self.assertEqual(self.client.get(reverse("invite", args=[self.assignment.pk, self.attempt.pk])).status_code, 405)


class FakeCanvas:
    """Stands in for canvas.request: records calls and answers from a small in-memory Canvas."""

    def __init__(self):
        self.calls = []
        self.assignments = {}
        self.messages = []
        self.pages = {}
        self.enrollments = [
            {"type": "StudentEnrollment", "user": {"id": 1, "name": "Ada Lovelace", "sortable_name": "Lovelace, Ada"}},
            {"type": "TeacherEnrollment", "user": {"id": 2, "name": "Tess Teacher", "sortable_name": "Teacher, Tess"}},
            {"type": "StudentEnrollment", "user": {"id": 2, "name": "Tess Teacher", "sortable_name": "Teacher, Tess"}},
            {"type": "TaEnrollment", "user": {"id": 3, "name": "Tom Assistant", "sortable_name": "Assistant, Tom"}},
            {"type": "ObserverEnrollment", "user": {"id": 4, "name": "A Parent"}},
            {"type": "StudentViewEnrollment", "user": {"id": 5, "name": "Test student"}},
        ]

    def __call__(self, connection, method, path, data=None, params=None, absolute_url=None, auth=True):
        from tool import canvas as c
        course_id = c._course_id_in(path or "")
        if course_id and not connection.course_allowed(course_id):
            raise c.CanvasError("not allowed")
        self.calls.append((method, path, data))
        if path == "/users/self":
            return {"name": "Aletheia Bot"}, {}
        if path == "/conversations":
            self.messages.append(data)
            return [{"id": 1}], {}
        if path.endswith("/pages") and method == "POST":
            slug = f"aletheia-sign-in-{len(self.pages)}"
            self.pages[slug] = data["wiki_page[body]"]
            return {"url": slug, "html_url": f"https://canvas.example.edu/courses/101/pages/{slug}"}, {}
        if "/pages/" in path and method == "DELETE":
            self.pages.pop(path.rsplit("/", 1)[1], None)
            return {}, {}
        if path.endswith("/permissions"):
            return {"read_roster": True, "send_messages": True, "manage_grades": True}, {}
        if path.endswith("/enrollments"):
            if params and params.get("user_id") == "self":
                return [{"type": "TeacherEnrollment"}], {}
            types = set(params.get("type[]", []))
            return [e for e in self.enrollments if e["type"] in types], {}
        if path.endswith("/assignments") and method == "POST":
            new_id = 900 + len(self.assignments)
            self.assignments[new_id] = {"id": new_id, "name": data["assignment[name]"], "description": "",
                                        "submission_types": ["online_upload"], "published": False}
            return self.assignments[new_id], {}
        if path.endswith("/assignments"):
            return list(self.assignments.values()), {}
        if "/assignments/" in path:
            a = self.assignments[int(path.rsplit("/", 1)[1])]
            if method == "PUT":
                if "assignment[description]" in data:
                    a["description"] = data["assignment[description]"]
                if "assignment[submission_types][]" in data:
                    a["submission_types"] = data["assignment[submission_types][]"]
            return a, {}
        return {"name": "Biology 101"}, {}


class CanvasTokenRouteTests(TestCase):
    def setUp(self):
        from tool import canvas
        self.canvas = canvas
        self.fake = FakeCanvas()
        patcher = mock.patch.object(canvas, "request", side_effect=self.fake)
        patcher.start()
        self.addCleanup(patcher.stop)
        from tool.models import CanvasConnection, CanvasCourse
        self.conn = CanvasConnection(name="Uni", base_url="https://canvas.example.edu", allowed_course_ids="101, 102")
        canvas.set_token(self.conn, "secret-token-abcd")
        canvas.ensure_platform(self.conn)
        self.conn.save()
        self.course = CanvasCourse.objects.create(connection=self.conn, canvas_course_id=101)

    def frame_url(self, assignment):
        return f"https://aletheia.test/canvas/a/{assignment.pk}/"

    def test_token_is_encrypted_and_only_the_hint_is_readable(self):
        self.assertNotIn("secret-token", self.conn.token_encrypted)
        self.assertEqual(self.conn.token_hint, "abcd")
        self.assertEqual(self.canvas._token(self.conn), "secret-token-abcd")

    def test_courses_off_the_allow_list_are_refused(self):
        with self.assertRaises(self.canvas.CanvasError):
            self.fake(self.conn, "GET", "/courses/999/enrollments")
        self.assertTrue(self.conn.course_allowed(102))
        self.assertFalse(self.conn.course_allowed(10))

    def test_check_reports_account_and_course_readiness(self):
        result = self.canvas.check(self.conn)
        self.assertTrue(result["ok"])
        self.assertEqual(result["account"], "Aletheia Bot")
        self.assertEqual(set(result["courses"]), {"101", "102"})

    def test_sync_maps_roles_and_leaves_out_observers_and_test_student(self):
        from tool.models import CanvasMember
        self.assertEqual(self.canvas.sync_course(self.course), 3)
        roles = dict(self.course.members.values_list("canvas_user_id", "role"))
        self.assertEqual(roles, {1: CanvasMember.STUDENT, 2: CanvasMember.TEACHER, 3: CanvasMember.TA})
        self.assertEqual(self.course.name, "Biology 101")
        self.fake.enrollments = self.fake.enrollments[1:]  # Ada leaves the course
        self.canvas.sync_course(self.course)
        self.assertFalse(self.course.members.get(canvas_user_id=1).active)

    def test_link_creates_writes_one_iframe_and_unlink_removes_it(self):
        assignment = self.canvas.link_assignment(self.course, self.frame_url, new_name="Essay check")
        remote = self.fake.assignments[assignment.canvas_assignment_id]
        self.assertEqual(remote["description"].count('data-aletheia="frame"'), 1)
        self.assertIn(self.frame_url(assignment), remote["description"])
        self.assertEqual(assignment.platform, self.conn.platform)
        self.assertEqual(assignment.title, "Essay check")
        self.assertFalse(assignment.configured)  # a teacher sets it up on first sign-in
        again = self.canvas.link_assignment(self.course, self.frame_url, canvas_assignment_id=assignment.canvas_assignment_id)
        self.assertEqual(again.pk, assignment.pk)
        self.assertEqual(remote["description"].count('data-aletheia="frame"'), 1)
        self.canvas.unlink_assignment(assignment)
        self.assertNotIn("data-aletheia", remote["description"])

    def test_link_keeps_the_teachers_description_and_adds_uploads(self):
        self.fake.assignments[500] = {"id": 500, "name": "Lab report", "description": "<p>Write it up.</p>",
                                      "submission_types": ["online_text_entry"], "published": True}
        self.canvas.link_assignment(self.course, self.frame_url, canvas_assignment_id=500)
        remote = self.fake.assignments[500]
        self.assertTrue(remote["description"].startswith("<p>Write it up.</p>"))
        self.assertEqual(remote["submission_types"], ["online_text_entry", "online_upload"])

    def test_end_pilot_unlinks_everything_then_deletes_the_token(self):
        assignment = self.canvas.link_assignment(self.course, self.frame_url, new_name="Essay check")
        self.assertEqual(self.canvas.offboard(self.conn), [])
        self.assertNotIn("data-aletheia", self.fake.assignments[assignment.canvas_assignment_id]["description"])
        self.conn.refresh_from_db()
        self.assertEqual(self.conn.token_encrypted, "")
        with self.assertRaises(self.canvas.CanvasError):
            self.canvas._token(self.conn)

    def test_admin_pages_render_and_end_pilot_asks_first(self):
        from django.contrib.auth.models import User
        User.objects.create_superuser("admin", "a@example.org", "pw")
        self.client.login(username="admin", password="pw")
        self.canvas.link_assignment(self.course, self.frame_url, new_name="Essay check")
        self.assertContains(self.client.get(reverse("admin:tool_canvasconnection_change", args=[self.conn.pk])), "abcd")
        self.assertNotContains(self.client.get(reverse("admin:tool_canvasconnection_change", args=[self.conn.pk])), "secret-token")
        page = self.client.get(reverse("admin:tool_canvascourse_assignments", args=[self.course.pk]))
        self.assertContains(page, "Essay check")
        self.assertContains(page, "Unlink")
        confirm = self.client.post(reverse("admin:tool_canvasconnection_changelist"),
                                   {"action": "end_pilot", "_selected_action": [self.conn.pk]})
        self.assertContains(confirm, "End pilot?")
        self.conn.refresh_from_db()
        self.assertTrue(self.conn.token_encrypted)  # nothing happens until confirmed

    def test_frame_page_only_frameable_from_that_canvas(self):
        assignment = self.canvas.link_assignment(self.course, self.frame_url, new_name="Essay check")
        resp = self.client.get(reverse("canvas_frame", args=[assignment.pk]))
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp["Content-Security-Policy"], "frame-ancestors 'self' https://canvas.example.edu")
        self.assertNotIn("X-Frame-Options", resp)


class CanvasSignInTests(TestCase):
    """Signing in through the Canvas inbox, then using Aletheia as staff or student."""

    def setUp(self):
        cache.clear()
        from tool import canvas
        self.canvas = canvas
        self.fake = FakeCanvas()
        patcher = mock.patch.object(canvas, "request", side_effect=self.fake)
        patcher.start()
        self.addCleanup(patcher.stop)
        from tool.models import CanvasConnection, CanvasCourse
        conn = CanvasConnection(name="Uni", base_url="https://canvas.example.edu", allowed_course_ids="101")
        canvas.set_token(conn, "secret-token-abcd")
        canvas.ensure_platform(conn)
        conn.save()
        self.course = CanvasCourse.objects.create(connection=conn, canvas_course_id=101)
        canvas.sync_course(self.course)
        self.assignment = canvas.link_assignment(self.course, lambda a: f"https://aletheia.test/canvas/a/{a.pk}/",
                                                 new_name="Essay check")

    def sign_in(self, name, client=None):
        """Search, send, open the inbox link, and exchange the code on the temporary page."""
        client = client or self.client
        frame = reverse("canvas_frame", args=[self.assignment.pk])
        self.assertContains(client.get(frame), "find your name")
        found = client.post(reverse("canvas_search", args=[self.assignment.pk]), {"q": name.split()[0][:3]}).json()
        member_id = next(r["id"] for r in found["results"] if r["name"] == name)
        sent = client.post(reverse("canvas_send", args=[self.assignment.pk]), {"member": member_id})
        self.assertContains(sent, "Check your Canvas inbox")
        link = re.search(r"https?://\S+/canvas/in/\S+/", self.fake.messages[-1]["body"]).group(0)
        landed = self.client_class().get(urlparse(link).path)  # opened from the inbox: a separate, top-level visit
        self.assertEqual(landed.status_code, 303)
        slug = landed["Location"].rsplit("/", 1)[1]
        code = re.search(r"code=([^\"&]+)", self.fake.pages[slug]).group(1)
        done = client.get(frame, {"code": code})
        self.assertContains(done, "signed in")
        self.assertNotIn(slug, self.fake.pages)  # the temporary page is deleted straight away
        return done

    def test_teacher_signs_in_through_the_inbox_and_sets_up_the_check(self):
        self.sign_in("Tess Teacher")
        self.assertEqual(self.fake.messages[-1]["recipients[]"], [2])
        resp = self.client.get(reverse("assignment", args=[self.assignment.pk]))
        self.assertRedirects(resp, reverse("settings", args=[self.assignment.pk]), fetch_redirect_response=False)

    def test_student_lands_on_the_upload_page(self):
        self.assignment.configured = True
        self.assignment.save()
        self.sign_in("Ada Lovelace")
        resp = self.client.get(reverse("assignment", args=[self.assignment.pk]))
        self.assertTemplateUsed(resp, "tool/student_home.html")
        self.assertContains(resp, "Hand in here, not with Canvas's Submit button.", html=False)

    def test_search_lists_only_current_members_and_never_observers(self):
        url = reverse("canvas_search", args=[self.assignment.pk])
        self.assertEqual(self.client.post(url, {"q": "a"}).json()["results"], [])  # at least two characters
        names = [r["name"] for r in self.client.post(url, {"q": "pa"}).json()["results"]]
        self.assertNotIn("A Parent", names)
        self.assertEqual([r["name"] for r in self.client.post(url, {"q": "tom ass"}).json()["results"]], ["Tom Assistant"])

    def test_links_work_once_and_newer_links_replace_older_ones(self):
        from tool.models import CanvasSignIn
        member = self.course.members.get(canvas_user_id=1)
        self.client.post(reverse("canvas_send", args=[self.assignment.pk]), {"member": member.pk})
        first = re.search(r"/canvas/in/\S+/", self.fake.messages[-1]["body"]).group(0)
        self.assertContains(self.client.post(reverse("canvas_send", args=[self.assignment.pk]), {"member": member.pk}),
                            "less than a minute ago", status_code=429)
        cache.clear()
        self.client.post(reverse("canvas_send", args=[self.assignment.pk]), {"member": member.pk})
        self.assertContains(self.client.get(first), "newer sign-in link", status_code=410)
        second = re.search(r"/canvas/in/\S+/", self.fake.messages[-1]["body"]).group(0)
        self.assertEqual(self.client.get(second).status_code, 303)
        self.assertContains(self.client.get(second), "already been used", status_code=410)
        CanvasSignIn.objects.update(created_at=timezone.now() - timedelta(hours=2), link_used_at=None)
        self.assertContains(self.client.get(second), "expired", status_code=410)

    def test_sign_in_expires_after_twelve_hours(self):
        self.sign_in("Tess Teacher")
        session = self.client.session
        session["lti_launches"][str(self.assignment.pk)]["canvas_until"] = time.time() - 1
        session.save()
        resp = self.client.get(reverse("assignment", args=[self.assignment.pk]))
        self.assertRedirects(resp, reverse("canvas_frame", args=[self.assignment.pk]), fetch_redirect_response=False)

    def test_preview_as_student_hands_nothing_in_and_stays_off_the_dashboard(self):
        self.assignment.configured = True
        self.assignment.save()
        self.sign_in("Tess Teacher")
        self.client.post(reverse("preview_start", args=[self.assignment.pk]))
        home = self.client.get(reverse("assignment", args=[self.assignment.pk]))
        self.assertTemplateUsed(home, "tool/student_home.html")
        self.assertContains(home, "Previewing as a student")
        Attempt.objects.create(assignment=self.assignment, lti_user_id="preview:canvas:2", user_name="Tess Teacher",
                               submission_text="x", status=Attempt.COMPLETED)
        self.client.post(reverse("preview_stop", args=[self.assignment.pk]))
        self.assertFalse(Attempt.objects.filter(lti_user_id__startswith="preview:").exists())
        dashboard = self.client.get(reverse("assignment", args=[self.assignment.pk]))
        self.assertTemplateUsed(dashboard, "tool/dashboard.html")
        self.assertContains(dashboard, "Preview as student")

    def test_students_cannot_preview(self):
        self.sign_in("Ada Lovelace")
        self.assertEqual(self.client.post(reverse("preview_start", args=[self.assignment.pk])).status_code, 403)

    def test_partitioned_cookies(self):
        from django.http import HttpResponse
        from tool.middleware import PartitionedCookiesMiddleware
        def view(request):
            response = HttpResponse()
            response.set_cookie("a", "1", samesite="None", secure=True)
            response.set_cookie("b", "1", samesite="Lax")
            return response
        response = PartitionedCookiesMiddleware(view)(None)
        self.assertIn("Partitioned", response.cookies["a"].OutputString())
        self.assertNotIn("Partitioned", response.cookies["b"].OutputString())


class LegalPageTests(TestCase):
    def test_terms_and_privacy_show_placeholders_until_configured(self):
        for name in ("terms", "privacy"):
            resp = self.client.get(reverse(name))
            self.assertEqual(resp.status_code, 200)
            self.assertContains(resp, '<span class="placeholder">[Company name]</span>', html=False)
        self.assertContains(self.client.get(reverse("terms")), "£5 per <strong>counted student</strong>", html=False)
        self.assertContains(self.client.get(reverse("privacy")), "Anthropic, PBC")

    def test_configured_details_replace_placeholders(self):
        with self.settings(LEGAL={"company_name": "Example Ltd"}):
            resp = self.client.get(reverse("terms"))
        self.assertContains(resp, "Example Ltd")
        self.assertNotContains(resp, "[Company name]")

    def test_landing_links_legal_documents_and_loads_nothing_from_google(self):
        resp = self.client.get(reverse("home"))
        self.assertContains(resp, reverse("terms"))
        self.assertContains(resp, reverse("privacy"))
        self.assertContains(resp, "aletheia-dpa-template")
        self.assertNotContains(resp, "googleapis")


class DemoTests(TestCase):
    def setUp(self):
        cache.clear()

    def start(self, slug="history"):
        return self.client.post(reverse("demo_start", args=[slug]))

    def test_landing_lists_six_examples_with_settings_and_files(self):
        resp = self.client.get(reverse("home"))
        self.assertEqual(len(resp.context["examples"]), 6)
        self.assertContains(resp, "LTI 1.3")
        self.assertContains(resp, "/lti/register/canvas")
        self.assertContains(resp, "5 min for the quiz")
        self.assertContains(resp, "code viva")
        for demo in demos.DEMOS:
            self.assertTrue(demos.file_path(demo).exists(), demo["slug"])
            self.assertEqual(self.client.get(reverse("demo_file", args=[demo["slug"]])).status_code, 200)

    def test_demo_runs_on_the_real_engine_without_generating(self):
        with mock.patch.object(ai, "generate_objectives") as gen_o, mock.patch.object(ai, "generate_question") as gen_q:
            resp = self.start()
            attempt = Attempt.objects.get()
            self.assertRedirects(resp, reverse("attempt", args=[attempt.assignment_id, attempt.pk]), fetch_redirect_response=False)
            page = self.client.get(reverse("attempt", args=[attempt.assignment_id, attempt.pk]))
            self.assertNotContains(page, "Check your file")  # demos go straight to Begin
            self.assertContains(page, "Begin")
            attempt.refresh_from_db()
            self.assertEqual(attempt.status, Attempt.READY)
            self.assertEqual(len(attempt.objectives), 5)
            self.assertEqual(attempt.questions.filter(status=Question.READY).count(), 10)
            self.client.post(reverse("begin", args=[attempt.assignment_id, attempt.pk]))
            while True:
                page = self.client.get(reverse("attempt", args=[attempt.assignment_id, attempt.pk]))
                if "question" not in page.context:
                    break
                q = page.context["question"]
                self.client.post(reverse("answer", args=[attempt.assignment_id, attempt.pk]),
                                 {"question": q.pk, "option": q.correct, "expired": "0"})
            gen_o.assert_not_called()
            gen_q.assert_not_called()
        attempt.refresh_from_db()
        self.assertEqual(attempt.status, Attempt.COMPLETED)
        self.assertEqual(attempt.overall_alignment, 100)
        self.assertContains(page, "See what an instructor sees")
        review = self.client.get(reverse("demo_review", args=[attempt.pk]))
        self.assertContains(review, "This is what an instructor sees")
        self.assertIn("flags", review.context)
        self.assertContains(review, reverse("demo_start", args=["history"]))  # "Invite to try again" starts afresh
        self.assertEqual(self.client_class().get(reverse("demo_review", args=[attempt.pk])).status_code, 404)

    def test_demo_never_takes_uploads(self):
        self.start()
        attempt = Attempt.objects.get()
        resp = self.client.get(reverse("assignment", args=[attempt.assignment_id]))
        self.assertRedirects(resp, reverse("attempt", args=[attempt.assignment_id, attempt.pk]), fetch_redirect_response=False)
        attempt.delete()
        resp = self.client.get(reverse("assignment", args=[attempt.assignment_id]))
        self.assertRedirects(resp, reverse("home") + "#try", fetch_redirect_response=False)
        resp = self.client.post(reverse("submit", args=[attempt.assignment_id]),
                                {"file": SimpleUploadedFile("mine.txt", SUBMISSION, content_type="text/plain")})
        self.assertEqual(resp.status_code, 404)

    def test_demo_starts_limited_per_visitor_not_per_tunnel(self):
        url = reverse("demo_start", args=["history"])
        for _ in range(30):
            self.client.post(url, HTTP_CF_CONNECTING_IP="203.0.113.5")
        self.assertEqual(self.client.post(url, HTTP_CF_CONNECTING_IP="203.0.113.5").status_code, 429)
        self.assertEqual(self.client_class().post(url, HTTP_CF_CONNECTING_IP="203.0.113.6").status_code, 302)

    def test_restarting_a_demo_replaces_the_visitors_attempt(self):
        self.start(); self.start()
        self.assertEqual(Attempt.objects.count(), 1)

    def test_demo_settings_match_registry(self):
        self.start("economics")
        attempt = Attempt.objects.get()
        self.assertFalse(attempt.time_each_question)
        self.assertEqual(attempt.time_limit_minutes, 5)
        self.assertEqual(attempt.retries_per_objective, 0)


@override_settings(
    EMAIL_BACKEND="django.core.mail.backends.locmem.EmailBackend",
    CONTACT_EMAIL="hello@example.org", DEFAULT_FROM_EMAIL="Aletheia <hello@example.org>",
    MEETING_URL="https://meet.example.org/aletheia",
)
class ContactTests(TestCase):
    def setUp(self):
        cache.clear()
        self.slot = booking.available_slots()[3]

    def send(self, **data):
        fields = {"name": "Ada Lovelace", "email": "ada@uni.example", "institution": "Uni", "message": "Hello", **data}
        return self.client.post(reverse("contact"), fields)

    def iso(self, dt):
        return dt.strftime("%Y-%m-%dT%H:%M:%SZ")

    def test_message_only(self):
        resp = self.send()
        self.assertTrue(resp.json()["ok"])
        self.assertEqual(Enquiry.objects.get().status, Enquiry.RECEIVED)
        (note,) = mail.outbox
        self.assertEqual(note.to, ["hello@example.org"])
        self.assertEqual(note.reply_to, ["ada@uni.example"])

    def test_needs_message_or_call_and_valid_email(self):
        resp = self.send(message="", email="not-an-email")
        self.assertEqual(resp.status_code, 400)
        self.assertIn("email", resp.json()["errors"])
        self.assertIn("__all__", resp.json()["errors"])
        self.assertFalse(Enquiry.objects.exists())

    def test_bots_quietly_ignored(self):
        resp = self.send(website="http://spam.example")
        self.assertTrue(resp.json()["ok"])
        self.assertFalse(Enquiry.objects.exists())
        self.assertFalse(mail.outbox)

    def test_slots_are_weekdays_in_uk_working_hours(self):
        slots = booking.all_slots()
        local = [s.astimezone(booking.zone()) for s in slots]
        self.assertTrue(all(d.weekday() < 5 for d in local))
        self.assertTrue(all((10, 0) <= (d.hour, d.minute) <= (15, 45) for d in local))
        self.assertTrue(all(d.date() > timezone.now().astimezone(booking.zone()).date() for d in local))
        data = self.client.get(reverse("meet_slots")).json()
        self.assertEqual(len(data["slots"]), len(slots))

    def test_booking_needs_email_confirmation(self):
        resp = self.send(meeting=self.iso(self.slot))
        self.assertTrue(resp.json()["ok"])
        self.assertIn("confirm", resp.json()["message"])
        enquiry = Enquiry.objects.get()
        self.assertEqual(enquiry.status, Enquiry.PENDING)
        self.assertNotIn(self.iso(self.slot), self.client.get(reverse("meet_slots")).json()["slots"])  # held
        notice, ask = mail.outbox
        self.assertEqual(ask.to, ["ada@uni.example"])
        confirm_url = reverse("meet_confirm", args=[enquiry.token])
        self.assertIn(confirm_url, ask.body)

        # Opening the link (as an email scanner might) doesn't confirm; the button does.
        self.assertContains(self.client.get(confirm_url), "Confirm your call")
        enquiry.refresh_from_db()
        self.assertEqual(enquiry.status, Enquiry.PENDING)
        resp = self.client.post(confirm_url)
        self.assertContains(resp, "Your call is booked")
        enquiry.refresh_from_db()
        self.assertEqual(enquiry.status, Enquiry.CONFIRMED)
        booked, owner = mail.outbox[2:]
        self.assertEqual(booked.to, ["ada@uni.example"])
        self.assertIn("https://meet.example.org/aletheia", booked.body)
        name, ics, mimetype = booked.attachments[0]
        self.assertIn("DTSTART:" + self.slot.strftime("%Y%m%dT%H%M%SZ"), ics)
        self.assertIn("METHOD:REQUEST", ics)
        self.assertEqual(owner.to, ["hello@example.org"])

        # Cancelling frees the slot and tells both sides.
        cancel_url = reverse("meet_cancel", args=[enquiry.token])
        self.assertIn(cancel_url, booked.body)
        self.assertContains(self.client.post(cancel_url), "Your call is cancelled")
        self.assertIn(self.iso(self.slot), self.client.get(reverse("meet_slots")).json()["slots"])
        self.assertIn("METHOD:CANCEL", mail.outbox[-1].attachments[0][1])

    def test_slot_cannot_be_double_booked(self):
        self.send(meeting=self.iso(self.slot))
        resp = self.send(meeting=self.iso(self.slot), email="other@uni.example")
        self.assertEqual(resp.status_code, 409)
        self.assertIn("meeting", resp.json()["errors"])

    def test_unconfirmed_hold_expires(self):
        self.send(meeting=self.iso(self.slot))
        Enquiry.objects.update(created_at=timezone.now() - booking.HOLD - timedelta(minutes=1))
        self.assertTrue(self.send(meeting=self.iso(self.slot), email="other@uni.example").json()["ok"])
        first = Enquiry.objects.order_by("pk").first()
        self.assertContains(self.client.post(reverse("meet_confirm", args=[first.token])), "no longer available")

    def test_times_outside_availability_refused(self):
        saturday = next(s for s in [timezone.now() + timedelta(days=d) for d in range(1, 9)]
                        if s.astimezone(booking.zone()).weekday() == 5)
        at = saturday.astimezone(booking.zone()).replace(hour=11, minute=0, second=0, microsecond=0)
        self.assertEqual(self.send(meeting=self.iso(at.astimezone(booking.ZoneInfo("UTC")))).status_code, 409)


class SettingsFormTests(Base):
    def test_limits_enforced(self):
        self.launch([INSTRUCTOR], sub="teacher")
        assignment = Assignment.objects.get()
        base = {"title": "T", "brief": "", "objective_count": 5, "questions_per_objective": 1,
                "retries_per_objective": 1, "seconds_per_question": 45, "max_attempts": 1}
        for field, value in [("objective_count", 11), ("retries_per_objective", 4), ("seconds_per_question", 2)]:
            resp = self.client.post(reverse("settings", args=[assignment.pk]), {**base, field: value})
            self.assertEqual(resp.status_code, 200, field)
            self.assertIn(field, resp.context["form"].errors)
        resp = self.client.post(reverse("settings", args=[assignment.pk]), base)
        self.assertEqual(resp.status_code, 302)


LOCAL = {"HTTP_HOST": "localhost"}
DEV_ON = override_settings(DEV_LOGIN=True, ALLOWED_HOSTS=["localhost", "testserver", "aletheia.example.org"])


class DevLaunchTests(Base):
    @override_settings(DEV_LOGIN=False, ALLOWED_HOSTS=["localhost"])
    def test_dev_launch_disabled_when_off(self):
        self.assertEqual(self.client.get(reverse("dev_launch", args=["student"]), **LOCAL).status_code, 404)

    @DEV_ON
    def test_dev_launch_on_localhost(self):
        resp = self.client.get(reverse("dev_launch", args=["instructor"]), **LOCAL)
        self.assertEqual(resp.status_code, 302)

    @DEV_ON
    def test_dev_launch_never_through_a_tunnel_or_public_host(self):
        url = reverse("dev_launch", args=["instructor"])
        self.assertEqual(self.client.get(url, HTTP_HOST="aletheia.example.org").status_code, 404)
        self.assertEqual(self.client.get(url, HTTP_CF_RAY="abc123", **LOCAL).status_code, 404)
        self.assertEqual(self.client.get(url, HTTP_X_FORWARDED_FOR="203.0.113.9", **LOCAL).status_code, 404)

    @DEV_ON
    def test_dev_reset_clears_dev_student(self):
        self.client.get(reverse("dev_launch", args=["student"]), **LOCAL)
        assignment = Assignment.objects.get(resource_link_id="dev")
        Attempt.objects.create(assignment=assignment, lti_user_id="dev-student", submission_text="x", status=Attempt.COMPLETED)
        other = Assignment.objects.create(platform=self.platform, deployment_id=DEPLOYMENT, resource_link_id="other")
        Attempt.objects.create(assignment=other, lti_user_id="dev-student", submission_text="x")
        resp = self.client.post(reverse("dev_reset"), **LOCAL)
        self.assertRedirects(resp, reverse("dev_launch", args=["student"]), fetch_redirect_response=False)
        self.assertEqual(Attempt.objects.count(), 1)  # only the dev assignment's attempts go

    @DEV_ON
    def test_dev_reset_refused_off_localhost(self):
        self.assertEqual(self.client.post(reverse("dev_reset"), HTTP_HOST="aletheia.example.org").status_code, 404)


MENTOR = "http://purl.imsglobal.org/vocab/lis/v2/membership#Mentor"
TA = "http://purl.imsglobal.org/vocab/lis/v2/membership/Instructor#TeachingAssistant"
INST_INSTRUCTOR = "http://purl.imsglobal.org/vocab/lis/v2/institution/person#Instructor"
INST_ADMIN = "http://purl.imsglobal.org/vocab/lis/v2/institution/person#Administrator"
LINEITEM = "https://lms.example.edu/api/lti/courses/1/line_items/7?include=x"
GRADES = {lti.AGS_CLAIM: {"lineitem": LINEITEM, "scope": [ags.SCORE_SCOPE]}}


def claims(*roles):
    return {lti.CLAIM + "roles": list(roles)}


class RoleTests(TestCase):
    def test_roles(self):
        self.assertTrue(lti.is_instructor(claims(INSTRUCTOR)))
        self.assertTrue(lti.is_instructor(claims(TA)))
        self.assertTrue(lti.is_instructor(claims(INST_ADMIN, LEARNER)))
        self.assertFalse(lti.is_instructor(claims(MENTOR)))
        # Teaching in another course doesn't make you staff in one you study in.
        self.assertFalse(lti.is_instructor(claims(INST_INSTRUCTOR, LEARNER)))
        self.assertTrue(lti.is_student(claims(INST_INSTRUCTOR, LEARNER)))
        self.assertFalse(lti.is_student(claims(MENTOR)))
        self.assertFalse(lti.is_student(claims(MENTOR, LEARNER)))
        self.assertFalse(lti.is_student(claims()))

    def test_requests_name_aletheia_as_user_agent(self):
        response = mock.MagicMock()
        response.__enter__.return_value.read.return_value = b'{"ok": true}'
        with mock.patch("urllib.request.urlopen", return_value=response) as urlopen:
            self.assertEqual(lti.http_json("GET", "https://lms.example.edu/x"), {"ok": True})
        self.assertEqual(urlopen.call_args.args[0].get_header("User-agent"), lti.USER_AGENT)

    def test_platform_keys_fetched_with_user_agent(self):
        platform = Platform(jwks_url="https://lms.example.edu/jwks")
        with mock.patch.object(jwt, "PyJWKClient") as client:
            lti._platform_signing_key(platform, "token")
        self.assertEqual(client.call_args.kwargs["headers"], {"User-Agent": lti.USER_AGENT})

    def test_surname_first(self):
        self.assertEqual(lti.surname_first("Ada Lovelace"), "Lovelace, Ada")
        self.assertEqual(lti.surname_first("Mary Ann  Smith"), "Smith, Mary Ann")
        self.assertEqual(lti.surname_first("Ludwig van Beethoven", "Ludwig", "van Beethoven"), "van Beethoven, Ludwig")
        self.assertEqual(lti.surname_first("Lovelace, Ada"), "Lovelace, Ada")
        self.assertEqual(lti.surname_first("Cher"), "Cher")
        self.assertEqual(lti.display_name({"given_name": "Test", "family_name": "Student", "name": "Test Student"}), "Student, Test")
        self.assertEqual(lti.display_name({}), "Student")

    def test_scores_url_keeps_query(self):
        self.assertEqual(ags.scores_url(LINEITEM), "https://lms.example.edu/api/lti/courses/1/line_items/7/scores?include=x")
        self.assertEqual(ags.scores_url("https://x/li/7/"), "https://x/li/7/scores")


@mock.patch.object(services, "run_in_background", side_effect=lambda fn, *args: fn(*args))
@mock.patch.object(ai, "generate_question", side_effect=fake_question)
@mock.patch.object(ai, "generate_objectives", side_effect=lambda title, brief, submission_text, count, code=False, on_usage=None: OBJECTIVES[:count])
class GradebookTests(Base):
    def setUp(self):
        super().setUp()
        cache.clear()
        self.platform.access_token_url = "https://lms.example.edu/login/oauth2/token"
        self.platform.save()
        self.calls = []
        patcher = mock.patch.object(lti, "http_json", side_effect=self.fake_http)
        patcher.start()
        self.addCleanup(patcher.stop)

    def fake_http(self, method, url, data=None, headers=None):
        self.calls.append({"method": method, "url": url, "data": data, "headers": headers or {}})
        if url.endswith("/token"):
            return {"access_token": "tok", "expires_in": 3600}
        return {}

    def scores(self):
        return [json.loads(c["data"]) for c in self.calls if "/scores" in c["url"]]

    def test_observer_gets_nothing(self, *_):
        self.configured_assignment()
        self.client.logout()
        resp = self.launch([MENTOR], sub="parent")
        self.assertEqual(resp.status_code, 403)
        self.assertNotIn("lti_launches", self.client.session)

    def test_launch_records_gradebook_column(self, *_):
        self.launch([INSTRUCTOR], sub="teacher", **GRADES)
        assignment = Assignment.objects.get()
        self.assertEqual(assignment.lineitem_url, LINEITEM)
        self.assertTrue(ags.can_submit(assignment))

    def test_upload_is_handed_in_and_nothing_more(self, *_):
        assignment = self.configured_assignment(objective_count=1, retries_per_objective=0)
        self.client.logout()
        self.launch([LEARNER], **GRADES)
        resp = self.client.get(reverse("assignment", args=[assignment.pk]))
        self.assertContains(resp, "handed in to your course")
        self.hand_in(assignment, SimpleUploadedFile("essay.txt", SUBMISSION, content_type="text/plain"))
        attempt = Attempt.objects.get()
        self.assertEqual(attempt.lms_status, Attempt.LMS_SENT)

        token_call, score_call = self.calls[0], next(c for c in self.calls if "/scores" in c["url"])
        self.assertIn(b"client_assertion", token_call["data"])
        self.assertEqual(score_call["url"], ags.scores_url(LINEITEM))
        self.assertEqual(score_call["headers"]["Authorization"], "Bearer tok")
        (submission,) = self.scores()
        self.assertEqual(submission["userId"], "user-1")
        self.assertEqual(submission["gradingProgress"], "PendingManual")
        self.assertNotIn("scoreGiven", submission)
        canvas = submission[ags.CANVAS_SUBMISSION]
        self.assertTrue(canvas["new_submission"] and canvas["preserve_score"])
        (item,) = canvas["content_items"]
        self.assertEqual((item["title"], item["media_type"]), ("essay.txt", "text/plain"))
        file_resp = self.client_class().get(item["url"].replace("http://testserver", ""))
        self.assertEqual(b"".join(file_resp.streaming_content), SUBMISSION)

        # Finishing the quiz posts nothing more to the gradebook.
        self.client.post(reverse("begin", args=[assignment.pk, attempt.pk]))
        question = self.client.get(reverse("attempt", args=[assignment.pk, attempt.pk])).context["question"]
        self.client.post(reverse("answer", args=[assignment.pk, attempt.pk]),
                         {"question": question.pk, "option": question.correct, "expired": "0"})
        attempt.refresh_from_db()
        self.assertEqual(attempt.status, Attempt.COMPLETED)
        self.assertEqual(len(self.scores()), 1)

    def test_failed_hand_in_is_shown_to_instructor(self, *_):
        assignment = self.configured_assignment()
        self.client.logout()
        self.launch([LEARNER], **GRADES)

        def refuse(method, url, data=None, headers=None):
            raise lti.ServiceError("POST scores returned 422: user not found")

        with mock.patch.object(lti, "http_json", side_effect=refuse):
            self.hand_in(assignment, SimpleUploadedFile("essay.txt", SUBMISSION, content_type="text/plain"))
        attempt = Attempt.objects.get()
        self.assertEqual(attempt.lms_status, Attempt.LMS_FAILED)
        instructor = self.client_class()
        self.launch([INSTRUCTOR], sub="teacher", client=instructor, **GRADES)
        resp = instructor.get(reverse("attempt_detail", args=[assignment.pk, attempt.pk]))
        self.assertContains(resp, "not handed in")
        self.assertContains(resp, "user not found")

    def test_no_gradebook_column_means_no_hand_in(self, *_):
        assignment = self.configured_assignment()
        self.client.logout()
        self.launch([LEARNER])
        self.hand_in(assignment, SimpleUploadedFile("essay.txt", SUBMISSION, content_type="text/plain"))
        self.assertEqual(Attempt.objects.get().lms_status, "")
        self.assertFalse(self.scores())

    def test_file_link_must_be_signed(self, *_):
        self.assertEqual(self.client.get(reverse("lms_file", args=["1:forged"])).status_code, 404)


@mock.patch.object(services, "run_in_background", side_effect=lambda fn, *args: fn(*args))
@mock.patch.object(ai, "generate_question", side_effect=fake_question)
@mock.patch.object(ai, "generate_objectives", side_effect=lambda title, brief, submission_text, count, code=False, on_usage=None: OBJECTIVES[:count])
class ConfirmTests(Base):
    def setUp(self):
        super().setUp()
        cache.clear()
        self.platform.access_token_url = "https://lms.example.edu/login/oauth2/token"
        self.platform.save()
        self.posts = []
        patcher = mock.patch.object(lti, "http_json", side_effect=lambda *a, **k: self.posts.append(a) or {"access_token": "t"})
        patcher.start()
        self.addCleanup(patcher.stop)

    def upload(self, **assignment_settings):
        assignment = self.configured_assignment(max_attempts=1, **assignment_settings)
        self.client.logout()
        self.launch([LEARNER], **GRADES)
        resp = self.client.post(reverse("submit", args=[assignment.pk]),
                                {"file": SimpleUploadedFile("essay.txt", SUBMISSION, content_type="text/plain")})
        return assignment, Attempt.objects.get(), resp

    def test_upload_waits_for_confirmation(self, generate_objectives, *_):
        assignment, draft, resp = self.upload()
        self.assertRedirects(resp, reverse("attempt", args=[assignment.pk, draft.pk]), fetch_redirect_response=False)
        self.assertEqual(draft.status, Attempt.DRAFT)
        generate_objectives.assert_not_called()
        self.assertFalse(self.posts)  # nothing handed in yet
        page = self.client.get(reverse("assignment", args=[assignment.pk]), follow=True)
        self.assertContains(page, "Check your file")
        self.assertContains(page, SUBMISSION.decode()[:40])  # the text Aletheia read
        self.assertContains(page, "Generative AI policy")
        self.assertContains(page, "do not use AI tools")  # the default policy
        self.assertContains(page, "Hand in and start")
        self.assertTrue(_can_start_for(assignment, "user-1"))  # a draft doesn't use an attempt

    def test_both_boxes_required(self, generate_objectives, *_):
        assignment, draft, _ = self.upload()
        url = reverse("confirm", args=[assignment.pk, draft.pk])
        resp = self.client.post(url, {"correct_file": "1"})
        self.assertEqual(resp.status_code, 400)
        self.assertContains(resp, "read and agree to the policy", status_code=400)
        resp = self.client.post(url, {"ai_policy": "1"})
        self.assertContains(resp, "right file", status_code=400)
        draft.refresh_from_db()
        self.assertEqual(draft.status, Attempt.DRAFT)
        generate_objectives.assert_not_called()

    def test_confirm_hands_in_starts_and_records(self, generate_objectives, *_):
        assignment, draft, _ = self.upload()
        self.client.post(reverse("confirm", args=[assignment.pk, draft.pk]), {"correct_file": "1", "ai_policy": "1"})
        draft.refresh_from_db()
        self.assertEqual(draft.status, Attempt.READY)
        self.assertEqual(draft.lms_status, Attempt.LMS_SENT)
        self.assertIsNotNone(draft.file_confirmed_at)
        self.assertIsNotNone(draft.ai_policy_agreed_at)
        self.assertIn("do not use AI tools", draft.ai_policy_text)
        self.assertFalse(_can_start_for(assignment, "user-1"))  # now it counts
        instructor = self.client_class()
        self.launch([INSTRUCTOR], sub="teacher", client=instructor, **GRADES)
        resp = instructor.get(reverse("attempt_detail", args=[assignment.pk, draft.pk]))
        self.assertContains(resp, "Confirmed the file")
        self.assertContains(resp, "generative AI policy")

    def test_no_policy_means_one_box(self, *_):
        assignment, draft, _ = self.upload(ai_policy="  ")
        page = self.client.get(reverse("attempt", args=[assignment.pk, draft.pk]))
        self.assertNotContains(page, "Generative AI policy")
        self.client.post(reverse("confirm", args=[assignment.pk, draft.pk]), {"correct_file": "1"})
        draft.refresh_from_db()
        self.assertEqual(draft.status, Attempt.READY)
        self.assertIsNone(draft.ai_policy_agreed_at)

    def test_choose_a_different_file(self, *_):
        assignment, draft, _ = self.upload()
        path = draft.file.path
        resp = self.client.post(reverse("discard", args=[assignment.pk, draft.pk]))
        self.assertRedirects(resp, reverse("assignment", args=[assignment.pk]), fetch_redirect_response=False)
        self.assertFalse(Attempt.objects.exists())
        self.assertFalse(Path(path).exists())
        self.assertContains(self.client.get(reverse("assignment", args=[assignment.pk])), "dropzone")

    def test_new_upload_replaces_draft(self, *_):
        assignment, first, _ = self.upload()
        self.client.post(reverse("discard", args=[assignment.pk, first.pk]))
        self.client.post(reverse("submit", args=[assignment.pk]),
                         {"file": SimpleUploadedFile("essay2.txt", SUBMISSION, content_type="text/plain")})
        self.client.post(reverse("submit", args=[assignment.pk]),
                         {"file": SimpleUploadedFile("essay3.txt", SUBMISSION, content_type="text/plain")})
        self.assertEqual(list(Attempt.objects.values_list("original_filename", flat=True)), ["essay3.txt"])

    def test_drafts_hidden_from_instructors(self, *_):
        assignment, draft, _ = self.upload()
        instructor = self.client_class()
        self.launch([INSTRUCTOR], sub="teacher", client=instructor, **GRADES)
        self.assertNotContains(instructor.get(reverse("assignment", args=[assignment.pk])), "user-1, Person")

    def test_pdf_preview_only_for_own_pdf(self, *_):
        assignment, draft, _ = self.upload()
        self.assertEqual(self.client.get(reverse("upload_preview", args=[assignment.pk, draft.pk])).status_code, 404)


def _can_start_for(assignment, user_id):
    from .views import _can_start

    return _can_start(assignment, user_id)


MEMBERSHIPS = "https://lms.example.edu/api/lti/courses/1/names_and_roles"
CLASS_LIST = {lti.NRPS_CLAIM: {"context_memberships_url": MEMBERSHIPS, "service_versions": ["2.0"]}}


class ClassListTests(Base):
    def setUp(self):
        super().setUp()
        cache.clear()
        self.platform.access_token_url = "https://lms.example.edu/login/oauth2/token"
        self.platform.save()
        self.pages = {
            MEMBERSHIPS: ({"members": [
                {"user_id": "user-1", "name": "Ada Started", "roles": [LEARNER], "status": "Active"},
                {"user_id": "user-2", "name": "Zed Waiting", "roles": [LEARNER]},
                {"user_id": "teacher", "name": "Prof", "roles": [INSTRUCTOR]},
                {"user_id": "parent", "name": "Parent", "roles": [MENTOR]},
            ]}, {"Link": f'<{MEMBERSHIPS}?page=2>; rel="next", <{MEMBERSHIPS}>; rel="first"'}),
            MEMBERSHIPS + "?page=2": ({"members": [
                {"user_id": "user-3", "given_name": "Bea", "family_name": "Waiting", "roles": [LEARNER]},
                {"user_id": "user-4", "name": "Gone Student", "roles": [LEARNER], "status": "Inactive"},
                {"user_id": "ta", "name": "A TA", "roles": [TA]},
            ]}, {}),
        }
        self.calls = []
        patcher = mock.patch.object(lti, "http_json", side_effect=self.fake_http)
        patcher.start()
        self.addCleanup(patcher.stop)

    def fake_http(self, method, url, data=None, headers=None, with_headers=False):
        self.calls.append((method, url, headers or {}))
        if url.endswith("/token"):
            self.token_scope = parse_qs(data.decode())["scope"][0]
            return {"access_token": "tok"}
        return self.pages[url] if with_headers else self.pages[url][0]

    def dashboard(self):
        self.launch([INSTRUCTOR], sub="teacher", **CLASS_LIST)
        assignment = Assignment.objects.get()
        assignment.configured = True
        assignment.save()
        Attempt.objects.create(assignment=assignment, lti_user_id="user-1", user_name="Ada Started",
                               submission_text="x", status=Attempt.COMPLETED, overall_alignment=80)
        return assignment, self.client.get(reverse("assignment", args=[assignment.pk]))

    def test_roster_lists_every_enrolled_student(self):
        assignment, resp = self.dashboard()
        self.assertEqual(assignment.memberships_url, MEMBERSHIPS)
        self.assertEqual(self.token_scope, nrps.MEMBERSHIP_SCOPE)
        names = [r["name"] for r in resp.context["rows"]]
        self.assertEqual(names, ["Started, Ada", "Waiting, Bea", "Waiting, Zed"])  # staff, observers, inactive left out
        self.assertContains(resp, "Not started", count=2)
        self.assertContains(resp, "1 of 3 students has handed in.")
        self.assertContains(resp, 'class="sort" data-col="1" data-type="number" data-first="desc">Alignment')
        self.assertContains(resp, 'data-sort="80.000000"')  # Ada's alignment, for sorting
        # Cached: a second visit doesn't ask the LMS again.
        calls = len(self.calls)
        self.client.get(reverse("assignment", args=[assignment.pk]))
        self.assertEqual(len(self.calls), calls)

    def test_class_list_failure_falls_back(self):
        def refuse(method, url, data=None, headers=None, with_headers=False):
            if url.endswith("/token"):
                raise lti.ServiceError('POST token returned 400: {"error":"invalid_scope"}')
            return {}

        with mock.patch.object(lti, "http_json", side_effect=refuse):
            _, resp = self.dashboard()
        self.assertEqual([r["name"] for r in resp.context["rows"]], ["Started, Ada"])
        self.assertContains(resp, "read the class list from your course")
        self.assertContains(resp, "invalid_scope")

    def test_no_class_list_service_means_no_change(self):
        self.launch([INSTRUCTOR], sub="teacher")
        assignment = Assignment.objects.get()
        self.assertEqual(nrps.class_list(assignment), (None, None))
        self.assertFalse(self.calls)

    def test_next_link(self):
        self.assertEqual(nrps.next_link({"link": '<https://x/a?p=2>; rel="next"'}), "https://x/a?p=2")
        self.assertIsNone(nrps.next_link({"Link": '<https://x/a>; rel="first"'}))
        self.assertIsNone(nrps.next_link({}))


CONFIG_URL = "https://canvas.test.instructure.com/api/lti/security/openid-configuration"
PLATFORM_CONFIG = {
    "issuer": "https://canvas.test.instructure.com",
    "authorization_endpoint": "https://sso.test.canvaslms.com/api/lti/authorize_redirect",
    "token_endpoint": "https://sso.test.canvaslms.com/login/oauth2/token",
    "jwks_uri": "https://sso.test.canvaslms.com/api/lti/security/jwks",
    "registration_endpoint": "https://canvas.test.instructure.com/api/lti/registrations",
    registration.PLATFORM_CONFIG: {"product_family_code": "canvas", registration.CANVAS_ACCOUNT_NAME: "Uni of Example"},
}


class RegistrationTests(TestCase):
    def setUp(self):
        self.calls = []
        patcher = mock.patch.object(lti, "http_json", side_effect=self.fake_http)
        patcher.start()
        self.addCleanup(patcher.stop)

    def fake_http(self, method, url, data=None, headers=None):
        self.calls.append({"method": method, "url": url, "data": data, "headers": headers or {}})
        return PLATFORM_CONFIG if method == "GET" else {"client_id": "10000000000077"}

    def register(self, path=None, config_url=CONFIG_URL):
        return self.client.get(path or reverse("lti_register_canvas"),
                               {"openid_configuration": config_url, "registration_token": "reg-tok"})

    def test_registration_creates_platform(self):
        resp = self.register()
        self.assertContains(resp, "org.imsglobal.lti.close")
        platform = Platform.objects.get()
        self.assertEqual(platform.client_id, "10000000000077")
        self.assertEqual(platform.issuer, "https://canvas.test.instructure.com")
        self.assertEqual(platform.access_token_url, PLATFORM_CONFIG["token_endpoint"])
        self.assertEqual(platform.name, "Canvas: Uni of Example")
        get, post = self.calls
        self.assertEqual(get["headers"]["Authorization"], "Bearer reg-tok")
        self.assertEqual(post["url"], PLATFORM_CONFIG["registration_endpoint"])
        self.assertEqual(post["headers"]["Authorization"], "Bearer reg-tok")
        body = json.loads(post["data"])
        self.assertEqual(body["redirect_uris"], ["http://testserver/lti/launch/"])
        self.assertEqual(body["initiate_login_uri"], "http://testserver/lti/login/")
        self.assertEqual(body["jwks_uri"], "http://testserver/lti/jwks/")
        self.assertEqual(body["scope"].split(), [ags.SCORE_SCOPE, nrps.MEMBERSHIP_SCOPE])
        tool = body[registration.TOOL_CONFIG]
        self.assertEqual(tool[registration.CANVAS_PRIVACY], "public")
        self.assertEqual(tool["messages"][0]["placements"], ["assignment_selection"])
        self.assertTrue(body["logo_uri"].startswith("http://testserver/static/tool/icons/aletheia-128"))
        self.assertEqual(tool["messages"][0]["icon_uri"], body["logo_uri"])

    def test_without_parameters_shows_instructions(self):
        self.assertContains(self.client.get("/lti/register/canvas"), "+ LTI Registration")
        self.assertContains(self.client.get("/lti/register/canvas/"), "+ LTI Registration")
        self.assertFalse(self.calls)

    def test_any_canvas_host_accepted_over_https_only(self):
        self.assertEqual(self.register(config_url="https://canvas.myuni.ac.uk/api/lti/security/openid-configuration").status_code, 200)
        Platform.objects.all().delete()
        self.calls.clear()
        resp = self.register(config_url="http://canvas.myuni.ac.uk/api/lti/security/openid-configuration")
        self.assertEqual(resp.status_code, 400)
        self.assertFalse(self.calls)

    def test_existing_platform_never_overwritten(self):
        Platform.objects.create(name="Original", issuer=PLATFORM_CONFIG["issuer"], client_id="10000000000077",
                                auth_login_url="https://a.example/auth", jwks_url="https://a.example/jwks")
        self.assertEqual(self.register().status_code, 400)
        self.assertEqual(Platform.objects.get().jwks_url, "https://a.example/jwks")


class RoomWordTests(TestCase):
    def test_current_and_previous_word_accepted(self):
        secret, now = "s3cret", 1_000_000.0
        from . import room

        current = room.word_for_window(secret, room._window(now))
        previous = room.word_for_window(secret, room._window(now) - 1)
        older = room.word_for_window(secret, room._window(now) - 2)
        self.assertTrue(room.check(secret, current, now))
        self.assertTrue(room.check(secret, f"  {previous.upper()} ", now))
        if older not in (current, previous):
            self.assertFalse(room.check(secret, older, now))
        self.assertFalse(room.check(secret, "", now))

    def test_one_typo_allowed_on_longer_words(self):
        from . import room

        self.assertTrue(room._close_enough("lanturn", "lantern"))  # substitution
        self.assertTrue(room._close_enough("lantrn", "lantern"))  # deletion
        self.assertFalse(room._close_enough("lntrn", "lantern"))  # two letters off
        self.assertFalse(room._close_enough("fax", "fox"))  # short words must be exact

    def test_spot_checks_never_before_first_objective(self):
        attempt = Attempt(room_word_required=True)
        for seed in range(20):
            checks = services.plan_room_checks(attempt, 5, random.Random(seed))
            self.assertEqual(len(checks), 2)
            self.assertNotIn(0, [c["before"] for c in checks])
        self.assertEqual(services.plan_room_checks(Attempt(room_word_required=False), 5), [])


@mock.patch.object(services, "plan_room_checks", side_effect=lambda attempt, n, rng=None: (
    [{"before": 1, "status": "pending", "started_at": None}] if attempt.room_word_required else []))
@mock.patch.object(services, "run_in_background", side_effect=lambda fn, *args: fn(*args))
@mock.patch.object(ai, "generate_question", side_effect=fake_question)
@mock.patch.object(ai, "generate_objectives", side_effect=lambda title, brief, submission_text, count, code=False, on_usage=None: OBJECTIVES[:count])
class RoomFlowTests(Base):
    def ready_attempt(self):
        assignment = self.configured_assignment(room_word_required=True)
        self.client.logout()
        self.launch([LEARNER])
        self.hand_in(assignment, SimpleUploadedFile("essay.txt", SUBMISSION, content_type="text/plain"))
        return assignment, Attempt.objects.get()

    def word(self, assignment):
        from . import room

        return room.current_word(assignment.room_secret)[0]

    def test_students_told_to_attend_the_room(self, *_):
        assignment = self.configured_assignment(room_word_required=True)
        self.client.logout()
        self.launch([LEARNER])
        home = self.client.get(reverse("assignment", args=[assignment.pk]))
        self.assertContains(home, "Answer your questions in the room.")
        self.client.post(reverse("submit", args=[assignment.pk]),
                         {"file": SimpleUploadedFile("essay.txt", SUBMISSION, content_type="text/plain")})
        draft = Attempt.objects.get()
        confirm = self.client.get(reverse("attempt", args=[assignment.pk, draft.pk]))
        self.assertContains(confirm, "answer the questions in the room")
        self.assertContains(confirm, ">Continue</button>")
        self.assertNotContains(confirm, "and start")
        self.client.post(reverse("confirm", args=[assignment.pk, draft.pk]), {"correct_file": "1", "ai_policy": "1"})
        ready = self.client.get(reverse("attempt", args=[assignment.pk, draft.pk]))
        self.assertContains(ready, "Begin only when you're in the room.")
        self.assertNotContains(ready, "viva")

    def test_no_room_message_without_room_word(self, *_):
        assignment = self.configured_assignment()
        self.client.logout()
        self.launch([LEARNER])
        self.assertNotContains(self.client.get(reverse("assignment", args=[assignment.pk])), "in the room")

    def test_turning_room_word_off_applies_to_students_who_have_not_begun(self, *_):
        assignment, attempt = self.ready_attempt()
        self.assertTrue(attempt.room_word_required)
        self.assertTrue(attempt.room_checks)
        Assignment.objects.filter(pk=assignment.pk).update(room_word_required=False)
        page = self.client.get(reverse("attempt", args=[assignment.pk, attempt.pk]))
        self.assertNotContains(page, 'name="room_word"')
        self.client.post(reverse("begin", args=[assignment.pk, attempt.pk]))  # no word needed now
        attempt.refresh_from_db()
        self.assertEqual(attempt.status, Attempt.IN_PROGRESS)
        self.assertFalse(attempt.room_word_required)
        self.assertEqual(attempt.room_checks, [])  # no spot checks either

        # Once begun, a later change doesn't alter the quiz in progress.
        Assignment.objects.filter(pk=assignment.pk).update(room_word_required=True, seconds_per_question=20)
        self.client.get(reverse("attempt", args=[assignment.pk, attempt.pk]))
        attempt.refresh_from_db()
        self.assertFalse(attempt.room_word_required)
        self.assertEqual(attempt.seconds_per_question, 45)

    def test_begin_needs_room_word(self, *_):
        assignment, attempt = self.ready_attempt()
        resp = self.client.post(reverse("begin", args=[assignment.pk, attempt.pk]), {"room_word": "nonsense"})
        self.assertEqual(resp.status_code, 400)
        self.assertEqual(attempt.events.get().kind, "room_start_wrong")
        self.client.post(reverse("begin", args=[assignment.pk, attempt.pk]), {"room_word": self.word(assignment)})
        attempt.refresh_from_db()
        self.assertEqual(attempt.status, Attempt.IN_PROGRESS)

    def _begin_and_clear_first_objective(self, assignment, attempt):
        self.client.post(reverse("begin", args=[assignment.pk, attempt.pk]), {"room_word": self.word(assignment)})
        question = self.client.get(reverse("attempt", args=[assignment.pk, attempt.pk])).context["question"]
        self.client.post(reverse("answer", args=[assignment.pk, attempt.pk]),
                         {"question": question.pk, "option": question.correct})

    def test_spot_check_before_objective_passes(self, *_):
        assignment, attempt = self.ready_attempt()
        self._begin_and_clear_first_objective(assignment, attempt)
        resp = self.client.get(reverse("attempt", args=[assignment.pk, attempt.pk]))
        self.assertTemplateUsed(resp, "tool/room_check.html")
        self.client.post(reverse("room_check", args=[assignment.pk, attempt.pk]), {"room_word": self.word(assignment)})
        resp = self.client.get(reverse("attempt", args=[assignment.pk, attempt.pk]))
        self.assertEqual(resp.context["question"].objective_index, 1)
        self.assertEqual(attempt.events.filter(kind="room_passed").count(), 1)
        self.assertEqual(attempt.integrity_flag_count(), 0)

    def test_missed_spot_check_is_flagged_and_quiz_continues(self, *_):
        assignment, attempt = self.ready_attempt()
        self._begin_and_clear_first_objective(assignment, attempt)
        self.client.get(reverse("attempt", args=[assignment.pk, attempt.pk]))  # check starts
        attempt.refresh_from_db()
        attempt.room_checks[0]["started_at"] = (timezone.now() - timedelta(seconds=30)).isoformat()
        attempt.save()
        resp = self.client.get(reverse("attempt", args=[assignment.pk, attempt.pk]))
        self.assertEqual(resp.context["question"].objective_index, 1)
        self.assertEqual(attempt.integrity_flag_count(), 1)

    def test_wrong_spot_check_is_flagged(self, *_):
        assignment, attempt = self.ready_attempt()
        self._begin_and_clear_first_objective(assignment, attempt)
        self.client.get(reverse("attempt", args=[assignment.pk, attempt.pk]))
        self.client.post(reverse("room_check", args=[assignment.pk, attempt.pk]), {"room_word": "nonsense"})
        self.assertEqual(attempt.events.filter(kind="room_failed").count(), 1)

    def test_projector_is_instructor_only(self, *_):
        assignment, attempt = self.ready_attempt()
        self.assertEqual(self.client.get(reverse("room_screen", args=[assignment.pk])).status_code, 403)
        self.assertEqual(self.client.get(reverse("room_word", args=[assignment.pk])).status_code, 403)
        self.assertEqual(self.client.get(reverse("room_word", args=[assignment.pk]), {"t": "forged"}).status_code, 403)

    def test_projector_keeps_working_after_student_launch_in_same_browser(self, *_):
        assignment, attempt = self.ready_attempt()
        teacher = self.client_class()
        self.launch([INSTRUCTOR], sub="teacher", client=teacher)
        word_url = teacher.get(reverse("room_screen", args=[assignment.pk])).context["word_url"]
        self.launch([LEARNER], sub="test-student", client=teacher)  # e.g. the LMS's student view
        data = teacher.get(word_url).json()
        self.assertEqual(data["word"], self.word(assignment))

    def test_projector_pass_is_tied_to_its_assignment(self, *_):
        assignment, attempt = self.ready_attempt()
        teacher = self.client_class()
        self.launch([INSTRUCTOR], sub="teacher", client=teacher)
        word_url = teacher.get(reverse("room_screen", args=[assignment.pk])).context["word_url"]
        other = Assignment.objects.create(platform=self.platform, deployment_id=DEPLOYMENT, resource_link_id="other")
        forged = word_url.replace(f"/a/{assignment.pk}/", f"/a/{other.pk}/")
        self.assertEqual(teacher.get(forged).status_code, 403)


class ScriptsFilterTests(TestCase):
    def render(self, value):
        from .templatetags.aletheia import scripts

        return str(scripts(value))

    def test_sup_and_sub_allowed(self):
        self.assertEqual(self.render("CO<sub>2</sub> and x<SUP>2</SUP>"), "CO<sub>2</sub> and x<sup>2</sup>")

    def test_everything_else_escaped(self):
        html = self.render('<script>alert(1)</script><sup onclick="x">2</sup><b>bold</b>')
        self.assertNotIn("<script", html)
        self.assertNotIn("<b>", html)
        self.assertIn("&lt;script&gt;", html)
        self.assertNotIn("<sup", html)  # tags with attributes are not allowed through

    def test_unbalanced_tags_repaired(self):
        self.assertEqual(self.render("m s<sup>−1"), "m s<sup>−1</sup>")
        self.assertEqual(self.render("x</sup>2"), "x2")
        self.assertEqual(self.render("a<sub>b<sup>c</sub>d"), "a<sub>b<sup>c</sup></sub>d")

    def test_scientific_notation_kept_together(self):
        self.assertEqual(self.render("2.1 × 10<sup>−4</sup>"), "2.1\u00a0×\u00a010<sup>−4</sup>")

    def test_unicode_scripts_converted(self):
        self.assertEqual(self.render("H₂O at 10⁻³ m²"), "H<sub>2</sub>O at 10<sup>−3</sup> m<sup>2</sup>")


class CodeVivaTests(TestCase):
    def setUp(self):
        self.dir = Path(tempfile.mkdtemp(dir=TMP))

    def zip_project(self, files):
        import zipfile

        path = self.dir / "project.zip"
        with zipfile.ZipFile(path, "w") as z:
            for name, body in files.items():
                z.writestr(name, body)
        return str(path)

    def test_zip_extraction_keeps_indentation_and_skips_dependencies(self):
        from .utils import extract_text

        path = self.zip_project({
            "src/app.py": "def main():\n\tif True:\n\t\treturn 1\n",
            "node_modules/lib/index.js": "junk()",
            ".git/config": "x",
            "src/__pycache__/app.pyc": "x",
            "logo.png": "binary",
            "README.md": "# My app",
        })
        text = extract_text(path, code=True)
        self.assertIn("=== src/app.py ===\ndef main():\n    if True:\n        return 1", text)
        self.assertIn("=== README.md ===", text)
        self.assertNotIn("junk", text)
        self.assertNotIn("logo.png", text)

    def test_notebook_extraction(self):
        from .utils import extract_text

        path = self.dir / "analysis.ipynb"
        path.write_text(json.dumps({"cells": [
            {"cell_type": "markdown", "source": ["Load data"]},
            {"cell_type": "code", "source": ["import pandas as pd\n", "df = pd.read_csv('x.csv')"]},
        ]}))
        text = extract_text(str(path), code=True, name="analysis.ipynb")
        self.assertIn("# Load data", text)
        self.assertIn("df = pd.read_csv('x.csv')", text)

    def test_source_files_only_accepted_in_code_mode(self):
        from .forms import UploadForm

        upload = {"file": SimpleUploadedFile("app.py", b"print(1)")}
        self.assertFalse(UploadForm({}, upload).is_valid())
        self.assertTrue(UploadForm({}, {"file": SimpleUploadedFile("app.py", b"print(1)")}, code=True).is_valid())

    def test_snippet_tidied_and_limited(self):
        data = {"prompt": "What does `total` return?", "code": "```python\n    def total(x):\n        return sum(x)\n```",
                "options": ["`6`", "`None`", "an error", "`[1, 2, 3]`"], "correct": [0]}
        q = ai.validate_question(data, "mcq", code=True)
        self.assertEqual(q["code"], "def total(x):\n    return sum(x)")
        self.assertEqual(q["options"], ["`6`", "`None`", "An error", "`[1, 2, 3]`"])  # code left as written
        with self.assertRaises(ai.GenerationError):
            ai.validate_question({**data, "code": "\n".join(["x = 1"] * 25)}, "mcq", code=True)

    def test_snippet_adds_reading_time(self):
        attempt = Attempt(seconds_per_question=45)
        self.assertEqual(services.seconds_for(attempt, Question(code="")), 45)
        self.assertEqual(services.seconds_for(attempt, Question(code="a\nb\nc")), 51)
        self.assertEqual(services.seconds_for(attempt, Question(code="\n".join("x" * 20))), 75)

    def test_inline_code_rendered_and_escaped(self):
        from .templatetags.aletheia import scripts

        html = str(scripts("Why does `items[0] < 2` fail when x<sup>2</sup>?"))
        self.assertEqual(html, "Why does <code>items[0] &lt; 2</code> fail when x<sup>2</sup>?")
        self.assertNotIn("<script", str(scripts("`<script>alert(1)</script>`")))


@mock.patch.object(services, "run_in_background", side_effect=lambda fn, *args: fn(*args))
@mock.patch.object(ai, "generate_question", side_effect=fake_question)
@mock.patch.object(ai, "generate_objectives", side_effect=lambda title, brief, submission_text, count, code=False, on_usage=None: OBJECTIVES[:count])
class CodeVivaFlowTests(Base):
    def test_code_viva_end_to_end(self, generate_objectives, generate_question, _):
        assignment = self.configured_assignment(code_viva=True, objective_count=1)
        self.client.logout()
        self.launch([LEARNER])
        resp = self.client.get(reverse("assignment", args=[assignment.pk]))
        self.assertContains(resp, ".zip")
        self.hand_in(assignment, SimpleUploadedFile("solver.py", b"def solve(grid):\n    for row in grid:\n        print(row)\n" * 5))
        attempt = Attempt.objects.get()
        self.assertTrue(attempt.code_viva)
        self.assertIn("=== solver.py ===\ndef solve(grid):\n    for row", attempt.submission_text)
        self.assertTrue(generate_objectives.call_args.kwargs["code"])
        self.client.post(reverse("begin", args=[assignment.pk, attempt.pk]))
        resp = self.client.get(reverse("attempt", args=[assignment.pk, attempt.pk]))
        self.assertContains(resp, '<pre class="code"><code>def total(items):\n    return sum(items)</code></pre>', html=False)
        self.assertEqual(resp.context["question"].seconds_allowed, 45 + 4)
        self.assertTrue(generate_question.call_args.kwargs["code"])
