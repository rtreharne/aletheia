"""Attempt lifecycle: objectives, question-by-question generation, timing, scoring.

Each objective is tested in turn. An objective is cleared once the student has
answered `questions_per_objective` questions on it correctly, or once they have
used up `retries_per_objective` extra questions on it; then the next objective
starts. Questions are generated just ahead of need: when one is shown, the next
objective's first question is requested (and any further question this objective
needs whatever the answer). A retry after a wrong answer is written on demand.
"""

import logging
import math
import random
import statistics
import threading
import time
from datetime import datetime, timedelta

from django.db import close_old_connections, transaction
from django.db.models import F
from django.utils import timezone

from . import ags, ai, lti, room
from .models import Attempt, IntegrityEvent, Question

log = logging.getLogger(__name__)

# Allowance for network latency when the timer auto-submits.
ANSWER_GRACE_SECONDS = 3
STALE_OBJECTIVES = timedelta(minutes=10)
STALE_QUESTION = timedelta(minutes=3)
MAX_QUESTION_TRIES = 3

QUESTION = "question"
ROOM_CHECK = "room_check"
WAITING = "waiting"
DONE = "done"
FAILED = "failed"


def run_in_background(fn, *args):
    """Run fn after the current transaction commits, in a thread with its own DB connection."""

    def run():
        close_old_connections()
        try:
            fn(*args)
        except Exception:
            log.exception("Background task %s failed", getattr(fn, "__name__", fn))
        finally:
            close_old_connections()

    transaction.on_commit(lambda: threading.Thread(target=run, daemon=True).start())


# ---------------------------------------------------------------- cost

def meter(attempt_id):
    """A callback that adds one Claude response's tokens and cost to the attempt's running totals."""

    def record(usage):
        Attempt.objects.filter(pk=attempt_id).update(
            ai_requests=F("ai_requests") + 1,
            ai_input_tokens=F("ai_input_tokens") + usage["input"],
            ai_output_tokens=F("ai_output_tokens") + usage["output"],
            ai_cache_write_tokens=F("ai_cache_write_tokens") + usage["cache_write"],
            ai_cache_read_tokens=F("ai_cache_read_tokens") + usage["cache_read"],
            ai_cost_usd=F("ai_cost_usd") + usage["cost_usd"],
        )

    return record


# ---------------------------------------------------------------- objectives

def generate_objectives(attempt_id):
    attempt = Attempt.objects.select_related("assignment").get(pk=attempt_id)
    assignment = attempt.assignment
    try:
        objectives = ai.generate_objectives(
            title=assignment.title,
            brief=assignment.brief,
            submission_text=attempt.submission_text,
            count=attempt.objective_count,
            code=attempt.code_viva,
            on_usage=meter(attempt.pk),
        )
    except Exception as exc:
        log.exception("Objective generation failed for attempt %s", attempt_id)
        attempt.status = Attempt.FAILED
        attempt.error = str(exc)[:2000]
        attempt.save(update_fields=["status", "error"])
        return
    attempt.objectives = objectives
    attempt.room_checks = plan_room_checks(attempt, len(objectives))
    attempt.status = Attempt.READY
    attempt.save(update_fields=["objectives", "room_checks", "status"])
    # Have the first question ready by the time the student presses Begin.
    request_question(attempt, 0, 0)


def load_demo(attempt, prepared):
    """Give a demo attempt the objectives and questions written in advance (instead of generating them)."""
    attempt.objectives = prepared["objectives"][: attempt.objective_count]
    attempt.room_checks = []
    attempt.status = Attempt.READY
    attempt.save(update_fields=["objectives", "room_checks", "status"])
    Question.objects.bulk_create(
        Question(
            attempt=attempt, objective_index=q["objective_index"], position=q["position"], kind=q["kind"],
            prompt=q["prompt"], code=q.get("code", ""), options=q["options"], correct=q["correct"],
            explanation=q.get("explanation", ""), status=Question.READY,
        )
        for q in prepared["questions"]
        if q["objective_index"] < len(attempt.objectives)
    )


def start_attempt_generation(attempt_id):
    run_in_background(generate_objectives, attempt_id)


def expire_stale_generation(attempt):
    if attempt.status == Attempt.GENERATING and timezone.now() - attempt.created_at > STALE_OBJECTIVES:
        attempt.status = Attempt.FAILED
        attempt.error = "Generation timed out."
        attempt.save(update_fields=["status", "error"])


# ---------------------------------------------------------------- room word

def plan_room_checks(attempt, objective_count, rng=random):
    """Pick 1–2 objectives (never the first) to be preceded by a room word spot check."""
    if not attempt.room_word_required or objective_count < 2:
        return []
    count = 1 if objective_count <= 3 else 2
    before = sorted(rng.sample(range(1, objective_count), count))
    return [{"before": i, "status": "pending", "started_at": None} for i in before]


NOT_STARTED = (Attempt.DRAFT, Attempt.GENERATING, Attempt.READY)
SETTINGS_UNTIL_BEGIN = ("room_word_required", "seconds_per_question", "time_each_question", "time_limit_minutes")


def apply_current_settings(attempt, assignment):
    """Until the student presses Begin, their attempt follows the assignment's current room word and
    timing settings, so an instructor's change applies to anyone who hasn't started yet."""
    if attempt.status not in NOT_STARTED:
        return
    changed = [name for name in SETTINGS_UNTIL_BEGIN if getattr(attempt, name) != getattr(assignment, name)]
    for name in changed:
        setattr(attempt, name, getattr(assignment, name))
    if "room_word_required" in changed and attempt.objectives:
        attempt.room_checks = plan_room_checks(attempt, len(attempt.objectives))
        changed.append("room_checks")
    if changed:
        attempt.save(update_fields=changed)


def _due_check(attempt, objective_index):
    return next(
        (c for c in attempt.room_checks if c["before"] == objective_index and c["status"] == "pending"), None
    )


def _check_overdue(check, now):
    started = datetime.fromisoformat(check["started_at"])
    return now > started + timedelta(seconds=room.CHECK_SECONDS + room.CHECK_GRACE_SECONDS)


def _resolve_check(attempt, check, passed, now):
    check["status"] = "passed" if passed else "failed"
    attempt.save(update_fields=["room_checks"])
    IntegrityEvent.objects.create(attempt=attempt, kind="room_passed" if passed else "room_failed", created_at=now)


def open_room_check(attempt, now):
    """The spot check in progress, or None. Starts its clock the first time it is shown."""
    check = next((c for c in attempt.room_checks if c["status"] == "pending" and c["started_at"]), None)
    if check and _check_overdue(check, now):
        _resolve_check(attempt, check, False, now)
        return None
    return check


def room_check_seconds_remaining(check, now=None):
    started = datetime.fromisoformat(check["started_at"])
    elapsed = ((now or timezone.now()) - started).total_seconds()
    return max(0.0, room.CHECK_SECONDS - elapsed)


def submit_room_check(attempt, typed, now=None):
    now = now or timezone.now()
    check = open_room_check(attempt, now)
    if check:
        _resolve_check(attempt, check, room.check(attempt.assignment.room_secret, typed), now)


# ---------------------------------------------------------------- questions

SNIPPET_SECONDS_PER_LINE = 2
SNIPPET_MAX_EXTRA_SECONDS = 30


def quiz_deadline(attempt):
    """When the whole quiz must end, if it has one overall time limit (questions not timed individually)."""
    if attempt.time_each_question or not attempt.started_at:
        return None
    return attempt.started_at + timedelta(minutes=attempt.time_limit_minutes)


def quiz_seconds_left(attempt, now=None):
    deadline = quiz_deadline(attempt)
    if deadline is None:
        return None
    return max(0.0, (deadline - (now or timezone.now())).total_seconds())


def seconds_for(attempt, question, now=None):
    """Base time, plus reading time for any code snippet. Untimed questions get whatever is left of the quiz."""
    left = quiz_seconds_left(attempt, now)
    if left is not None:
        return max(1, math.ceil(left))
    extra = 0
    if question.code:
        lines = question.code.count("\n") + 1
        extra = min(SNIPPET_MAX_EXTRA_SECONDS, SNIPPET_SECONDS_PER_LINE * lines)
    return attempt.seconds_per_question + extra


def kind_for(objective_index, position):
    """Alternate single- and multiple-answer questions."""
    return (Question.MCQ, Question.MAQ)[(objective_index + position) % 2]


def generate_question(question_id):
    question = Question.objects.select_related("attempt__assignment").get(pk=question_id)
    attempt = question.attempt
    assignment = attempt.assignment
    others = attempt.questions.filter(status=Question.READY).exclude(pk=question.pk)
    avoid = [
        f"{q.prompt} (options: {'; '.join(q.options)})" for q in others.order_by("objective_index", "position")
    ]
    try:
        generated = ai.generate_question(
            title=assignment.title,
            brief=assignment.brief,
            submission_text=attempt.submission_text,
            objective=attempt.objectives[question.objective_index]["text"],
            kind=question.kind,
            seconds=attempt.seconds_per_question,
            avoid_prompts=avoid,
            code=attempt.code_viva,
            on_usage=meter(attempt.pk),
        )
    except Exception as exc:
        log.exception("Question generation failed (question %s)", question_id)
        question.status = Question.FAILED
        question.error = str(exc)[:2000]
        question.save(update_fields=["status", "error"])
        return
    question.prompt = generated["prompt"]
    question.code = generated.get("code", "")
    question.options = generated["options"]
    question.correct = generated["correct"]
    question.explanation = generated.get("explanation", "")
    question.status = Question.READY
    question.error = ""
    question.save(update_fields=["prompt", "code", "options", "correct", "explanation", "status", "error"])


def request_question(attempt, objective_index, position):
    """Make sure a question exists for this slot, generating it if needed."""
    question, created = Question.objects.get_or_create(
        attempt=attempt,
        objective_index=objective_index,
        position=position,
        defaults={"kind": kind_for(objective_index, position), "tries": 1},
    )
    if created:
        run_in_background(generate_question, question.pk)
        question.refresh_from_db()  # generation may already have finished (it runs inline when not in a request)
        return question

    stuck = question.status == Question.PENDING and timezone.now() - question.requested_at > STALE_QUESTION
    if (question.status == Question.FAILED or stuck) and question.tries < MAX_QUESTION_TRIES:
        question.status = Question.PENDING
        question.tries += 1
        question.requested_at = timezone.now()
        question.save(update_fields=["status", "tries", "requested_at"])
        run_in_background(generate_question, question.pk)
    return question


def _objective_cleared(attempt, asked):
    correct = sum(1 for q in asked if q.is_correct)
    return correct >= attempt.questions_per_objective or len(asked) >= (
        attempt.questions_per_objective + attempt.retries_per_objective
    )


def next_slot(attempt):
    """The (objective_index, position) of the next question to ask, or None when finished."""
    answered = [q for q in attempt.questions.all() if q.answered_at is not None]
    for index in range(len(attempt.objectives)):
        asked = [q for q in answered if q.objective_index == index]
        if not _objective_cleared(attempt, asked):
            return index, len(asked)
    return None


def prefetch(attempt, objective_index, position):
    """Request the questions that will certainly be needed after this one.

    A further question on this objective is written in advance only if it will be asked whatever the
    answer (the objective needs more correct answers). A retry, needed only after a wrong answer, is
    written when that happens: most are never needed, so the student waits a few seconds instead.
    """
    if position + 1 < attempt.questions_per_objective:
        request_question(attempt, objective_index, position + 1)
    if objective_index + 1 < len(attempt.objectives):
        request_question(attempt, objective_index + 1, 0)


def _served_unanswered(attempt):
    return attempt.questions.filter(served_at__isnull=False, answered_at__isnull=True).first()


def _record(question, selected, now, timed_out):
    question.selected = sorted(set(selected))
    question.answered_at = now
    question.timed_out = timed_out
    question.is_correct = question.selected == sorted(question.correct)
    question.save(update_fields=["selected", "answered_at", "timed_out", "is_correct"])


def advance(attempt, now=None):
    """Work out what the student should see now; serve the next question if it is ready.

    Returns (state, question) where state is QUESTION, WAITING, DONE or FAILED.
    A question whose time has fully run out (e.g. the student left) is recorded
    as unanswered first.
    """
    now = now or timezone.now()
    current = _served_unanswered(attempt)
    if current:
        if not current.overdue(ANSWER_GRACE_SECONDS, now):
            return QUESTION, current
        _record(current, [], now, timed_out=True)

    deadline = quiz_deadline(attempt)
    if deadline and now >= deadline:
        # The whole-quiz time limit is up: stop here; objectives not reached score 0.
        attempt.ran_out_of_time = True
        attempt.save(update_fields=["ran_out_of_time"])
        finish(attempt)
        return DONE, None

    check = open_room_check(attempt, now)
    if check:
        return ROOM_CHECK, check

    slot = next_slot(attempt)
    if slot is None:
        finish(attempt)
        return DONE, None

    question = request_question(attempt, *slot)
    check = _due_check(attempt, slot[0]) if slot[1] == 0 else None
    if check:
        check["started_at"] = now.isoformat()
        attempt.save(update_fields=["room_checks"])
        return ROOM_CHECK, check
    if question.status == Question.READY:
        question.order = attempt.questions.filter(served_at__isnull=False).count()
        question.seconds_allowed = seconds_for(attempt, question, now)
        question.served_at = now
        question.save(update_fields=["order", "seconds_allowed", "served_at"])
        prefetch(attempt, *slot)
        return QUESTION, question
    if question.status == Question.FAILED and question.tries >= MAX_QUESTION_TRIES:
        attempt.status = Attempt.FAILED
        attempt.error = f"Could not generate a question: {question.error}"[:2000]
        attempt.save(update_fields=["status", "error"])
        return FAILED, None
    return WAITING, question


def peek(attempt):
    """Like advance, but never serves a question or starts a timer (for polling)."""
    if attempt.status != Attempt.IN_PROGRESS:
        return attempt.status
    if _served_unanswered(attempt) or open_room_check(attempt, timezone.now()):
        return QUESTION
    slot = next_slot(attempt)
    if slot is None:
        return DONE
    if slot[1] == 0 and _due_check(attempt, slot[0]):
        return ROOM_CHECK
    question = request_question(attempt, *slot)
    if question.status == Question.READY:
        return QUESTION
    if question.status == Question.FAILED and question.tries >= MAX_QUESTION_TRIES:
        return FAILED
    return WAITING


def submit_answer(attempt, question, selected, expired, now=None):
    now = now or timezone.now()
    late = question.overdue(ANSWER_GRACE_SECONDS, now)
    valid = [i for i in selected if 0 <= i < len(question.options)]
    if question.kind == Question.MCQ:
        valid = valid[:1]
    # Late submissions score nothing; an on-time expiry submits what was selected.
    _record(question, [] if late else valid, now, timed_out=late or expired)


# ---------------------------------------------------------------- scoring

def score(attempt):
    """Per objective: share of its questions answered correctly. Overall: mean across all objectives.

    An objective with no answered questions (only possible when the quiz time limit ran out
    before it was reached) is marked not reached and counts as 0.
    """
    answered = [q for q in attempt.questions.all() if q.answered_at is not None]
    per_objective = []
    for index, objective in enumerate(attempt.objectives):
        asked = [q for q in answered if q.objective_index == index]
        correct = sum(1 for q in asked if q.is_correct)
        per_objective.append(
            {
                "text": objective["text"],
                "correct": correct,
                "total": len(asked),
                "alignment": round(100 * correct / len(asked)) if asked else 0,
                "not_reached": not asked,
            }
        )
    values = [o["alignment"] for o in per_objective]
    overall = round(sum(values) / len(values), 1) if values else 0.0
    return per_objective, overall


def finish(attempt):
    if attempt.status == Attempt.COMPLETED:
        return
    attempt.objective_alignment, attempt.overall_alignment = score(attempt)
    times = [q.answer_seconds for q in attempt.questions.all() if q.answer_seconds is not None]
    attempt.median_answer_seconds = round(statistics.median(times), 1) if times else None
    attempt.status = Attempt.COMPLETED
    attempt.completed_at = timezone.now()
    attempt.save(
        update_fields=["objective_alignment", "overall_alignment", "median_answer_seconds", "status", "completed_at"]
    )


# ---------------------------------------------------------------- LMS gradebook

def send_submission(attempt_id, file_url):
    """Put the uploaded file in the LMS gradebook as a new submission, for teachers to mark."""
    attempt = Attempt.objects.select_related("assignment__platform").get(pk=attempt_id)
    assignment = attempt.assignment
    try:
        ags.post_score(assignment.platform, assignment.lineitem_url, ags.submission_payload(attempt, file_url))
    except lti.ServiceError as exc:
        log.warning("Gradebook submission failed (attempt %s): %s", attempt_id, exc)
        Attempt.objects.filter(pk=attempt_id).update(lms_status=Attempt.LMS_FAILED, lms_error=str(exc)[:2000])
        return
    Attempt.objects.filter(pk=attempt_id).update(lms_status=Attempt.LMS_SENT, lms_error="")
