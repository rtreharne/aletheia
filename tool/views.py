import json
import logging
from datetime import timedelta
import secrets
import time
from functools import wraps
from urllib.parse import urlencode

from django.conf import settings
from django.core import signing
from django.core.cache import cache
from django.core.files import File
from django.db import transaction
from django.db.models import Max, Prefetch
from django.http import FileResponse, Http404, HttpResponseBadRequest, JsonResponse
from django.templatetags.static import static
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.views.decorators.csrf import csrf_exempt
from django.utils import timezone
from django.utils.crypto import constant_time_compare
from django.views.decorators.http import require_POST

from . import ags, booking, demos, lti, nrps, registration, room, services
from .forms import ContactForm, SettingsForm, UploadForm
from .models import Assignment, Attempt, Enquiry, IntegrityEvent, Invitation, Platform
from .utils import ExtractionError, extension, extract_text

log = logging.getLogger(__name__)


FILE_KINDS = {".pdf": "PDF", ".docx": "Word", ".txt": "Text", ".zip": "ZIP"}


def home(request):
    examples = []
    for demo in demos.DEMOS:
        path = demos.file_path(demo)
        examples.append({
            **demo,
            "ready": demos.prepared(demo) is not None,
            "chips": demos.settings_summary(demo),
            "file_kind": FILE_KINDS.get(path.suffix, path.suffix.lstrip(".").upper()),
            "file_size": path.stat().st_size if path.exists() else 0,
        })
    url = request.build_absolute_uri
    return render(request, "tool/home.html", {
        "examples": examples,
        "canvas_url": url(reverse("lti_register_canvas")),
        "moodle_url": url(reverse("lti_register_moodle")),
        "lti_urls": [
            ("Login (OIDC initiation)", url(reverse("lti_login"))),
            ("Launch / redirect", url(reverse("lti_launch"))),
            ("Public keyset (JWKS)", url(reverse("lti_jwks"))),
            ("Deep linking", url(reverse("lti_launch"))),
        ],
        "contact_email": settings.CONTACT_EMAIL,
        "meeting_minutes": settings.MEETING_MINUTES,
    })


def error(request, message, status=400):
    return render(request, "tool/error.html", {"message": message}, status=status)


# ---------------------------------------------------------------- LTI endpoints

@csrf_exempt
def lti_login(request):
    params = request.POST if request.method == "POST" else request.GET
    try:
        url, query = lti.begin_login(request.session, params, request.build_absolute_uri(reverse("lti_launch")))
    except lti.LaunchError as exc:
        return error(request, str(exc))
    return redirect(f"{url}?{urlencode(query)}")


@csrf_exempt
@require_POST
def lti_launch(request):
    try:
        platform, claims = lti.validate_launch(request.session, request.POST.get("id_token"), request.POST.get("state"))
    except lti.LaunchError as exc:
        return error(request, str(exc))

    message_type = claims.get(lti.CLAIM + "message_type")
    deployment_id = claims.get(lti.CLAIM + "deployment_id")
    instructor = lti.is_instructor(claims)
    if not instructor and not lti.is_student(claims):
        # Observers (e.g. parents) and anyone else without a teaching or learning role get nothing.
        return error(request, "Aletheia is only available to the students and teaching staff on this course.", status=403)

    if message_type == "LtiDeepLinkingRequest":
        if not instructor:
            return error(request, "Only instructors can add Aletheia to a course.", status=403)
        settings_claim = claims.get(lti.DL_CLAIM + "deep_linking_settings") or {}
        if not settings_claim.get("deep_link_return_url"):
            return error(request, "The deep linking request had no return URL.")
        request.session["lti_deep_link"] = {
            "platform": platform.pk,
            "deployment_id": deployment_id,
            "return_url": settings_claim["deep_link_return_url"],
            "data": settings_claim.get("data"),
        }
        return redirect("deep_link")

    if message_type != "LtiResourceLinkRequest":
        return error(request, "Unsupported LTI message type.")

    link = claims.get(lti.CLAIM + "resource_link") or {}
    context = claims.get(lti.CLAIM + "context") or {}
    if not link.get("id"):
        return error(request, "The launch had no resource link.")

    assignment, created = Assignment.objects.get_or_create(
        platform=platform,
        deployment_id=deployment_id,
        resource_link_id=link["id"],
        defaults={
            "title": (link.get("title") or "")[:255],
            "context_id": context.get("id", ""),
            "context_title": (context.get("title") or "")[:255],
        },
    )
    changed = []
    if not created and context.get("title") and context["title"] != assignment.context_title:
        assignment.context_title = context["title"][:255]
        changed.append("context_title")
    # The gradebook column (present when the LMS placed Aletheia as a graded assignment).
    grades = claims.get(lti.AGS_CLAIM) or {}
    lineitem, scopes = str(grades.get("lineitem") or "")[:500], list(grades.get("scope") or [])
    if lineitem and (lineitem, scopes) != (assignment.lineitem_url, assignment.ags_scopes):
        assignment.lineitem_url, assignment.ags_scopes = lineitem, scopes
        changed += ["lineitem_url", "ags_scopes"]
    # The course's class list service (so the roster can show students who haven't started).
    memberships = str((claims.get(lti.NRPS_CLAIM) or {}).get("context_memberships_url") or "")[:500]
    if memberships and memberships != assignment.memberships_url:
        assignment.memberships_url = memberships
        changed.append("memberships_url")
    if changed:
        assignment.save(update_fields=changed)

    launches = request.session.get("lti_launches", {})
    launches[str(assignment.pk)] = {
        "user_id": claims["sub"],
        "name": lti.display_name(claims)[:255],
        "instructor": instructor,
    }
    request.session["lti_launches"] = launches
    request.session.cycle_key()
    return redirect("assignment", assignment.pk)


def jwks(request):
    return JsonResponse(lti.public_jwks())


ICON_PATH = "tool/icons/aletheia-128.png"


def icon(request):
    """A stable icon URL for LMS tool listings (and /favicon.ico for browsers that ask for it)."""
    name = "tool/icons/favicon.ico" if request.path.endswith(".ico") else ICON_PATH
    return redirect(static(name), permanent=False)


def register_canvas(request):
    """LTI Dynamic Registration: a Canvas admin enters this URL and Canvas opens it in a frame."""
    config_url = request.GET.get("openid_configuration")
    if not config_url:
        return render(request, "tool/register.html", {"url": request.build_absolute_uri()})
    urls = {
        name: request.build_absolute_uri(reverse(route))
        for name, route in (("login", "lti_login"), ("launch", "lti_launch"), ("jwks", "lti_jwks"))
    }
    urls["icon"] = request.build_absolute_uri(static(ICON_PATH))
    try:
        platform = registration.register(
            config_url, request.GET.get("registration_token", ""), urls, request.get_host().split(":")[0]
        )
    except registration.RegistrationError as exc:
        return render(request, "tool/register.html", {"problem": str(exc)}, status=400)
    return render(request, "tool/register.html", {"platform": platform})


def lms_file(request, token):
    """The uploaded file, fetched by the LMS when it builds the gradebook submission.

    The link is signed and expires, so only the LMS that was given it can fetch the file.
    """
    try:
        attempt_id = signing.loads(token, salt=LMS_FILE_SALT, max_age=LMS_FILE_MAX_AGE)
    except signing.BadSignature:
        raise Http404
    attempt = get_object_or_404(Attempt, pk=attempt_id)
    if not attempt.file:
        raise Http404
    return FileResponse(attempt.file.open("rb"), as_attachment=True, filename=attempt.original_filename)


LMS_FILE_SALT = "aletheia.lms-file"
LMS_FILE_MAX_AGE = 7 * 24 * 60 * 60


def lms_file_url(request, attempt):
    return request.build_absolute_uri(reverse("lms_file", args=[signing.dumps(attempt.pk, salt=LMS_FILE_SALT)]))


def deep_link(request):
    data = request.session.get("lti_deep_link")
    if not data:
        return error(request, "Please start from your course's 'add content' menu.")
    if request.method == "POST":
        platform = get_object_or_404(Platform, pk=data["platform"])
        title = (request.POST.get("title") or "").strip()[:255] or "Aletheia"
        token = lti.deep_link_response(platform, data, request.build_absolute_uri(reverse("lti_launch")), title)
        del request.session["lti_deep_link"]
        return render(request, "tool/deep_link_return.html", {"return_url": data["return_url"], "jwt": token})
    return render(request, "tool/deep_link.html")


# ---------------------------------------------------------------- access control

def launched(instructor=None):
    """Require an LTI launch into this assignment (optionally as instructor or student)."""

    def decorator(view):
        @wraps(view)
        def wrapper(request, assignment_id, *args, **kwargs):
            launch = request.session.get("lti_launches", {}).get(str(assignment_id))
            if launch and launch.get("canvas_until") is not None and launch["canvas_until"] < time.time():
                launch = None  # a Canvas inbox sign-in lasts 12 hours
            if not launch:
                if Assignment.objects.filter(pk=assignment_id, canvas_course__isnull=False).exists():
                    return redirect("canvas_frame", assignment_id)  # sign in through the Canvas inbox
                return error(request, "Please open this assignment from your course.", status=403)
            if instructor is not None and launch["instructor"] != instructor:
                return error(request, "You don't have access to this page.", status=403)
            request.launch = launch
            assignment = get_object_or_404(Assignment, pk=assignment_id)
            return view(request, assignment, *args, **kwargs)

        return wrapper

    return decorator


def _student_attempt(request, assignment, attempt_id):
    return get_object_or_404(
        Attempt, pk=attempt_id, assignment=assignment, lti_user_id=request.launch["user_id"]
    )


NOT_COUNTED = (Attempt.DRAFT, Attempt.FAILED)


def _attempts_used(assignment, user_id):
    return assignment.attempts.filter(lti_user_id=user_id).exclude(status__in=NOT_COUNTED).count()


def _can_start(assignment, user_id):
    """Within the attempt limit, plus one more for each time an instructor invited them to try again."""
    if assignment.max_attempts == 0:
        return True
    invited = assignment.invitations.filter(lti_user_id=user_id).count()
    return _attempts_used(assignment, user_id) < assignment.max_attempts + invited


def _pending_invitation(assignment, user_id):
    """The student's latest invitation to try again, if they haven't handed in a new attempt since."""
    invitation = assignment.invitations.filter(lti_user_id=user_id).last()
    if invitation is None:
        return None
    handed_in_since = (
        assignment.attempts.filter(lti_user_id=user_id, created_at__gt=invitation.created_at)
        .exclude(status__in=NOT_COUNTED).exists()
    )
    return None if handed_in_since else invitation


# ---------------------------------------------------------------- assignment entry

@launched()
def assignment_view(request, assignment):
    if request.launch["instructor"]:
        if not assignment.configured:
            return redirect("settings", assignment.pk)
        return dashboard(request, assignment)
    return student_home(request, assignment)


def student_home(request, assignment):
    if demos.is_demo(assignment):
        # Demos never take uploads: back to the examples (or on to the visitor's attempt).
        active = assignment.attempts.filter(
            lti_user_id=request.launch["user_id"], status__in=[Attempt.DRAFT, Attempt.READY, Attempt.IN_PROGRESS]
        ).first()
        return redirect("attempt", assignment.pk, active.pk) if active else redirect(reverse("home") + "#try")
    if not assignment.configured:
        return render(request, "tool/not_ready.html", {"assignment": assignment})
    user_id = request.launch["user_id"]
    attempts = list(assignment.attempts.filter(lti_user_id=user_id))
    active = next(
        (a for a in attempts if a.status in (Attempt.DRAFT, Attempt.GENERATING, Attempt.READY, Attempt.IN_PROGRESS)), None
    )
    if active:
        return redirect("attempt", assignment.pk, active.pk)
    completed = [a for a in attempts if a.status == Attempt.COMPLETED]
    attempts = [a for a in attempts if a.status != Attempt.DRAFT]
    return render(
        request,
        "tool/student_home.html",
        {
            "assignment": assignment,
            "latest": completed[0] if completed else None,
            "failed": attempts[0] if attempts and attempts[0].status == Attempt.FAILED else None,
            "can_start": _can_start(assignment, user_id),
            "invited": _pending_invitation(assignment, user_id),
            "form": UploadForm(code=assignment.code_viva),
            "gradebook": ags.can_submit(assignment),
            "dev_reset": dev_allowed(request) and user_id == "dev-student",
        },
    )


@launched(instructor=False)
@require_POST
def submit(request, assignment):
    if demos.is_demo(assignment):
        raise Http404
    user_id = request.launch["user_id"]
    if not assignment.configured or not _can_start(assignment, user_id):
        return redirect("assignment", assignment.pk)
    form = UploadForm(request.POST, request.FILES, code=assignment.code_viva)
    if not form.is_valid():
        return _upload_error(request, assignment, form)

    upload = form.cleaned_data["file"]
    with transaction.atomic():
        _discard_drafts(assignment, user_id)
        number = (assignment.attempts.filter(lti_user_id=user_id).aggregate(n=Max("number"))["n"] or 0) + 1
        attempt = Attempt.objects.create(
            assignment=assignment,
            lti_user_id=user_id,
            user_name=request.launch["name"],
            number=number,
            seconds_per_question=assignment.seconds_per_question,
            time_each_question=assignment.time_each_question,
            time_limit_minutes=assignment.time_limit_minutes,
            room_word_required=assignment.room_word_required,
            code_viva=assignment.code_viva,
            objective_count=assignment.objective_count,
            questions_per_objective=assignment.questions_per_objective,
            retries_per_objective=assignment.retries_per_objective,
            file=upload,
            original_filename=upload.name[:255],
            submission_text="",
            status=Attempt.DRAFT,
        )
        try:
            text = extract_text(attempt.file.path, code=assignment.code_viva, name=upload.name)
        except ExtractionError as exc:
            text, problem = "", str(exc)
        else:
            problem = None
            if len(text) < (80 if assignment.code_viva else 200):
                problem = "We couldn't find enough text in that file. If it's a scanned PDF, upload a DOCX or text version."
            elif len(text) > settings.MAX_SUBMISSION_CHARS:
                problem = "That file is too long to assess. Please upload a shorter version."
        if problem:
            attempt.file.delete(save=False)
            transaction.set_rollback(True)
            form.add_error("file", problem)
            return _upload_error(request, assignment, form)
        attempt.submission_text = text
        attempt.save(update_fields=["submission_text"])
    # Nothing is handed in or generated until the student has checked the file and confirmed.
    return redirect("attempt", assignment.pk, attempt.pk)


def _discard_drafts(assignment, user_id):
    for draft in assignment.attempts.filter(lti_user_id=user_id, status=Attempt.DRAFT):
        draft.file.delete(save=False)
        draft.delete()


PREVIEW_CHARS = 30000


def _confirm_page(request, assignment, attempt, errors=(), status=200):
    text = attempt.submission_text
    return render(
        request,
        "tool/confirm.html",
        {
            "assignment": assignment,
            "attempt": attempt,
            "is_pdf": extension(attempt.original_filename) == ".pdf",
            "file_size": attempt.file.size if attempt.file else 0,
            "preview_text": text[:PREVIEW_CHARS],
            "truncated": len(text) > PREVIEW_CHARS,
            "policy": assignment.ai_policy.strip(),
            "gradebook": ags.can_submit(assignment),
            "errors": errors,
        },
        status=status,
    )


@launched(instructor=False)
@require_POST
def confirm(request, assignment, attempt_id):
    """The student has checked their file (and agreed to the AI policy): hand in and start."""
    user_id = request.launch["user_id"]
    with transaction.atomic():
        attempt = _locked_attempt(request, assignment, attempt_id)
        if attempt.status != Attempt.DRAFT:
            return redirect("attempt", assignment.pk, attempt.pk)
        if not _can_start(assignment, user_id):
            return redirect("assignment", assignment.pk)
        policy = assignment.ai_policy.strip()
        errors = []
        if not request.POST.get("correct_file"):
            errors.append("correct_file")
        if policy and not request.POST.get("ai_policy"):
            errors.append("ai_policy")
        if errors:
            return _confirm_page(request, assignment, attempt, errors, status=400)

        now = timezone.now()
        if demos.is_demo(assignment):
            attempt.file_confirmed_at = now
            if policy:
                attempt.ai_policy_text, attempt.ai_policy_agreed_at = policy, now
            attempt.created_at = now
            attempt.save(update_fields=["file_confirmed_at", "ai_policy_text", "ai_policy_agreed_at", "created_at"])
            services.load_demo(attempt, demos.prepared(demos.get(demos.slug_for(assignment))))
            return redirect("attempt", assignment.pk, attempt.pk)
        attempt.status = Attempt.GENERATING
        attempt.created_at = now  # handed in now, not when the file was first uploaded
        attempt.file_confirmed_at = now
        if policy:
            attempt.ai_policy_text, attempt.ai_policy_agreed_at = policy, now
        # The upload also becomes a normal submission in the gradebook (never for a staff preview).
        hand_in = ags.can_submit(assignment) and not request.launch.get("preview")
        if hand_in:
            attempt.lms_status = Attempt.LMS_PENDING
        attempt.save(update_fields=[
            "status", "created_at", "file_confirmed_at", "ai_policy_text", "ai_policy_agreed_at", "lms_status"
        ])
        if hand_in:
            services.run_in_background(services.send_submission, attempt.pk, lms_file_url(request, attempt))
        services.start_attempt_generation(attempt.pk)
    return redirect("attempt", assignment.pk, attempt.pk)


@launched(instructor=False)
@require_POST
def discard(request, assignment, attempt_id):
    """Throw away an unconfirmed upload so the student can choose a different file."""
    with transaction.atomic():
        attempt = _locked_attempt(request, assignment, attempt_id)
        if attempt.status == Attempt.DRAFT:
            attempt.file.delete(save=False)
            attempt.delete()
    if demos.is_demo(assignment):
        return redirect(reverse("home") + "#try")
    return redirect("assignment", assignment.pk)


@launched(instructor=False)
def upload_preview(request, assignment, attempt_id):
    """The student's own PDF, shown inline on the confirmation page."""
    attempt = _student_attempt(request, assignment, attempt_id)
    if not attempt.file or extension(attempt.original_filename) != ".pdf":
        raise Http404
    response = FileResponse(attempt.file.open("rb"), content_type="application/pdf", filename=attempt.original_filename)
    response["Content-Disposition"] = "inline"
    return response


def _upload_error(request, assignment, form):
    return render(
        request,
        "tool/student_home.html",
        {"assignment": assignment, "can_start": True, "form": form, "gradebook": ags.can_submit(assignment)},
        status=400,
    )


# ---------------------------------------------------------------- the quiz

def _locked_attempt(request, assignment, attempt_id):
    return get_object_or_404(
        Attempt.objects.select_for_update(),
        pk=attempt_id,
        assignment=assignment,
        lti_user_id=request.launch["user_id"],
    )


@launched(instructor=False)
def attempt_view(request, assignment, attempt_id):
    with transaction.atomic():
        attempt = _locked_attempt(request, assignment, attempt_id)
        services.expire_stale_generation(attempt)
        services.apply_current_settings(attempt, assignment)
        context = {"assignment": assignment, "attempt": attempt, "objectives": attempt.objectives}

        if attempt.status == Attempt.DRAFT:
            return _confirm_page(request, assignment, attempt)
        if attempt.status == Attempt.GENERATING:
            return render(request, "tool/generating.html", context)
        if attempt.status == Attempt.READY:
            return _ready_page(request, assignment, attempt)
        if attempt.status == Attempt.IN_PROGRESS and in_frame(request):
            # The questions are in their own tab; the LMS frame just waits for them.
            context["handoff"] = _handoff_token(request, assignment, attempt)
            return render(request, "tool/in_tab.html", context)
        if attempt.status == Attempt.IN_PROGRESS:
            state, question = services.advance(attempt)
            if state == services.QUESTION:
                context.update(
                    question=question,
                    options=list(enumerate(question.options)),
                    current_objective=question.objective_index,
                    objective_number=question.objective_index + 1,
                    objective_total=len(attempt.objectives),
                    is_follow_up=question.position > 0,
                    remaining=round(question.seconds_remaining(), 2),
                    # Untimed questions show the time left for the whole quiz instead.
                    timer_total=question.seconds_allowed if attempt.time_each_question else attempt.time_limit_minutes * 60,
                    left_last=_left_during_last_question(attempt),
                )
                return render(request, "tool/question.html", context)
            if state == services.ROOM_CHECK:
                context.update(
                    remaining=round(services.room_check_seconds_remaining(question), 2),
                    total=room.CHECK_SECONDS,
                    current_objective=question["before"],
                )
                return render(request, "tool/room_check.html", context)
            if state == services.WAITING:
                context["current_objective"] = question.objective_index
                return render(request, "tool/waiting.html", context)
            attempt.refresh_from_db()
        if attempt.status == Attempt.FAILED:
            return redirect("assignment", assignment.pk)

    if attempt.status == Attempt.COMPLETED and request.launch.get("tab") and not in_frame(request):
        # Finished in the questions' own tab: the result is shown back in the course.
        return render(request, "tool/finished_tab.html", {"assignment": assignment})
    context["can_start"] = _can_start(assignment, request.launch["user_id"])
    context["demo"] = demos.is_demo(assignment)
    context["dev_reset"] = dev_allowed(request) and request.launch["user_id"] == "dev-student"
    return render(request, "tool/result.html", context)


@launched(instructor=False)
def attempt_status(request, assignment, attempt_id):
    """Polled while waiting: says whether there is something new to show."""
    with transaction.atomic():
        attempt = _locked_attempt(request, assignment, attempt_id)
        services.expire_stale_generation(attempt)
        if attempt.status == Attempt.GENERATING:
            return JsonResponse({"waiting": True, "status": attempt.status})
        return JsonResponse({"waiting": services.peek(attempt) == services.WAITING, "status": attempt.status})


@launched(instructor=False)
@require_POST
def begin(request, assignment, attempt_id):
    attempt = _student_attempt(request, assignment, attempt_id)
    problem = _begin(assignment, attempt, request.POST.get("room_word"))
    if problem:
        return _ready_page(request, assignment, attempt, room_error=problem, status=400)
    return redirect("attempt", assignment.pk, attempt.pk)


def _begin(assignment, attempt, room_word):
    """Start the quiz (checking the room word if needed). Returns a problem message, or None."""
    if attempt.status != Attempt.READY:
        return None
    services.apply_current_settings(attempt, assignment)
    if attempt.room_word_required and not room.check(assignment.room_secret, room_word):
        IntegrityEvent.objects.create(attempt=attempt, kind="room_start_wrong")
        return "That isn't the word on screen. Check and try again."
    attempt.status = Attempt.IN_PROGRESS
    attempt.started_at = timezone.now()
    attempt.save(update_fields=["status", "started_at"])
    return None


# ---------------------------------------------------------------- questions in their own tab
#
# Inside the LMS, Aletheia runs in a frame. The questions open in a tab of their own, through a
# signed, one-time link: it starts a session in that tab (which works even where the browser
# keeps the frame's cookies separate). When the quiz ends, the tab tells the student to return to the LMS.

HANDOFF_SALT = "aletheia.handoff"
HANDOFF_MAX_AGE = 30 * 60


def in_frame(request):
    """Is this page being loaded inside a frame (the LMS), rather than as a tab of its own?"""
    return request.META.get("HTTP_SEC_FETCH_DEST") == "iframe"


def _handoff_token(request, assignment, attempt):
    if not attempt.handoff_nonce:  # reused until a tab uses it, so reloading the frame can't cancel a tab opening
        attempt.handoff_nonce = secrets.token_urlsafe(16)
        attempt.save(update_fields=["handoff_nonce"])
    launch = request.launch
    return signing.dumps(
        {"a": assignment.pk, "t": attempt.pk, "n": attempt.handoff_nonce, "u": launch["user_id"],
         "name": launch.get("name", "")},
        salt=HANDOFF_SALT,
        compress=True,
    )


def _ready_page(request, assignment, attempt, room_error=None, status=200):
    framed = in_frame(request)
    return render(
        request,
        "tool/ready.html",
        {
            "assignment": assignment,
            "attempt": attempt,
            "objectives": attempt.objectives,
            "room_error": room_error,
            "new_tab": framed,
            "handoff": _handoff_token(request, assignment, attempt) if framed else "",
        },
        status=status,
    )


def start_in_tab(request, assignment_id, attempt_id):
    """Opened in a new tab from the LMS frame: sign the student in here, begin, and show the questions."""
    try:
        data = signing.loads(request.GET.get("t", ""), salt=HANDOFF_SALT, max_age=HANDOFF_MAX_AGE)
    except signing.BadSignature:
        return error(request, "This link has expired. Go back to your course and open the assignment again.", status=403)
    if (data.get("a"), data.get("t")) != (assignment_id, attempt_id):
        return error(request, "This link isn't valid.", status=403)
    with transaction.atomic():
        attempt = get_object_or_404(
            Attempt.objects.select_for_update(), pk=attempt_id, assignment_id=assignment_id, lti_user_id=data.get("u")
        )
        if not attempt.handoff_nonce or not constant_time_compare(attempt.handoff_nonce, data.get("n", "")):
            return error(request, "This link has already been used. Go back to your course and open the assignment again.", status=403)
        attempt.handoff_nonce = ""
        attempt.save(update_fields=["handoff_nonce"])
        launches = request.session.get("lti_launches", {})
        launches[str(assignment_id)] = {
            "user_id": attempt.lti_user_id, "name": data.get("name", ""), "instructor": False, "tab": True,
        }
        request.session["lti_launches"] = launches
        request.launch = launches[str(assignment_id)]
        problem = _begin(attempt.assignment, attempt, request.GET.get("room_word"))
        if problem:
            return _ready_page(request, attempt.assignment, attempt, room_error=problem, status=400)
    return redirect("attempt", assignment_id, attempt_id)


@launched(instructor=False)
@require_POST
def answer(request, assignment, attempt_id):
    with transaction.atomic():
        attempt = _locked_attempt(request, assignment, attempt_id)
        if attempt.status == Attempt.IN_PROGRESS:
            question = attempt.questions.filter(served_at__isnull=False, answered_at__isnull=True).first()
            if question and str(question.pk) == request.POST.get("question"):
                try:
                    selected = [int(v) for v in request.POST.getlist("option")]
                except ValueError:
                    selected = []
                services.submit_answer(attempt, question, selected, expired=request.POST.get("expired") == "1")
                if services.next_slot(attempt) is None:
                    services.finish(attempt)
    return redirect("attempt", assignment.pk, attempt_id)


@launched(instructor=False)
@require_POST
def room_check(request, assignment, attempt_id):
    with transaction.atomic():
        attempt = _locked_attempt(request, assignment, attempt_id)
        if attempt.status == Attempt.IN_PROGRESS:
            services.submit_room_check(attempt, request.POST.get("room_word"))
    return redirect("attempt", assignment.pk, attempt_id)


def _left_during_last_question(attempt):
    """True if the student left the page while on the question they answered last."""
    last = attempt.questions.filter(answered_at__isnull=False).order_by("-answered_at").first()
    return bool(last) and attempt.events.filter(question=last, kind__in=IntegrityEvent.LEFT_PAGE_KINDS).exists()


@launched(instructor=False)
@require_POST
def log_event(request, assignment, attempt_id):
    attempt = _student_attempt(request, assignment, attempt_id)
    if attempt.status != Attempt.IN_PROGRESS:
        return JsonResponse({"ok": False})
    try:
        payload = json.loads(request.body or "{}")
    except json.JSONDecodeError:
        return HttpResponseBadRequest()
    kind = payload.get("kind")
    if kind not in dict(IntegrityEvent.KINDS):
        return HttpResponseBadRequest()
    question = attempt.questions.filter(pk=payload.get("question")).first()
    if attempt.events.count() < 2000:
        IntegrityEvent.objects.create(attempt=attempt, question=question, kind=kind)
    return JsonResponse({"ok": True})


# ---------------------------------------------------------------- instructor

@launched(instructor=True)
@require_POST
def preview_start(request, assignment):
    """Try the student side as yourself: nothing is handed in, and it never appears on the dashboard."""
    launches = request.session.get("lti_launches", {})
    staff = dict(launches[str(assignment.pk)])
    launches[str(assignment.pk)] = {
        "user_id": PREVIEW_PREFIX + staff["user_id"],
        "name": staff.get("name", ""),
        "instructor": False,
        "preview": True,
        "preview_of": staff,
        **({"canvas_until": staff["canvas_until"]} if "canvas_until" in staff else {}),
    }
    request.session["lti_launches"] = launches
    return redirect("assignment", assignment.pk)


@launched()
@require_POST
def preview_stop(request, assignment):
    """Back to the teacher's view; the preview's attempts are deleted."""
    launch = request.launch
    if launch.get("preview_of"):
        for attempt in assignment.attempts.filter(lti_user_id=launch["user_id"]):
            if attempt.file:
                attempt.file.delete(save=False)
            attempt.delete()
        launches = request.session.get("lti_launches", {})
        launches[str(assignment.pk)] = launch["preview_of"]
        request.session["lti_launches"] = launches
    return redirect("assignment", assignment.pk)


@launched(instructor=True)
def settings_view(request, assignment):
    form = SettingsForm(request.POST or None, instance=assignment)
    if request.method == "POST" and form.is_valid():
        assignment = form.save(commit=False)
        assignment.configured = True
        assignment.save()
        return redirect("assignment", assignment.pk)
    return render(request, "tool/settings.html", {"assignment": assignment, "form": form})


PREVIEW_PREFIX = "preview:"


def dashboard(request, assignment):
    attempts = (
        assignment.attempts.exclude(status__in=NOT_COUNTED).exclude(lti_user_id__startswith=PREVIEW_PREFIX)
        .order_by("lti_user_id", "number")
        .prefetch_related(Prefetch("events", queryset=IntegrityEvent.objects.select_related("question")))
    )
    students = {}
    for attempt in attempts:
        row = students.setdefault(
            attempt.lti_user_id,
            {"name": lti.surname_first(attempt.user_name), "attempts": [], "latest": None, "flags": 0, "logged": []},
        )
        row["name"] = lti.surname_first(attempt.user_name) or row["name"]
        row["attempts"].append(attempt)
        if attempt.status == Attempt.COMPLETED:
            row["latest"] = attempt
        attempt.log = _integrity_log(attempt.events.all())
        row["flags"] += sum(1 for e in attempt.log if e.flagged)
        if attempt.log:
            row["logged"].append(attempt)
    enrolled, class_list_problem = nrps.class_list(assignment)
    for member in enrolled or []:
        row = students.get(member["user_id"])
        if row is None:
            students[member["user_id"]] = {
                "name": member["name"], "attempts": [], "latest": None, "flags": 0, "logged": [], "not_started": True,
            }
        else:
            row["name"] = member["name"]  # the course's current, properly split name
    # Students who have handed in first, then those who haven't, each alphabetically.
    rows = sorted(students.values(), key=lambda r: (r.get("not_started", False), (r["name"] or "").lower()))
    return render(
        request,
        "tool/dashboard.html",
        {
            "assignment": assignment,
            "rows": rows,
            "gradebook": ags.can_submit(assignment),
            "class_list": enrolled is not None,
            "class_list_problem": class_list_problem,
            "started": sum(1 for r in rows if not r.get("not_started")),
        },
    )


LEAVE_KINDS = {"hidden": "visible", "blur": "focus"}


def _integrity_log(events):
    """Events in order, each marked if it counts as a flag, and returns marked with time away."""
    log, left_at = [], {}
    for event in events:
        event.flagged = event.kind in IntegrityEvent.FLAG_KINDS
        event.away_seconds = None
        if event.kind in LEAVE_KINDS:
            left_at[LEAVE_KINDS[event.kind]] = event.created_at
        elif event.kind in left_at:
            event.away_seconds = (event.created_at - left_at.pop(event.kind)).total_seconds()
        log.append(event)
    return log


ROOM_SCREEN_SALT = "aletheia.room-screen"
ROOM_SCREEN_MAX_AGE = 12 * 60 * 60


@launched(instructor=True)
def room_screen(request, assignment):
    # The projector page carries its own signed pass, so it keeps working even if
    # this browser later launches as someone else (e.g. the LMS's student view).
    token = signing.dumps({"assignment": assignment.pk}, salt=ROOM_SCREEN_SALT)
    word_url = reverse("room_word", args=[assignment.pk]) + "?" + urlencode({"t": token})
    return render(
        request, "tool/room.html", {"assignment": assignment, "rotation": room.ROTATION_SECONDS, "word_url": word_url}
    )


def room_word(request, assignment_id):
    try:
        data = signing.loads(request.GET.get("t", ""), salt=ROOM_SCREEN_SALT, max_age=ROOM_SCREEN_MAX_AGE)
    except signing.BadSignature:
        return JsonResponse({"error": "expired"}, status=403)
    if data.get("assignment") != assignment_id:
        return JsonResponse({"error": "expired"}, status=403)
    assignment = get_object_or_404(Assignment, pk=assignment_id)
    word, seconds_left = room.current_word(assignment.room_secret)
    response = JsonResponse({"word": word, "seconds_left": round(seconds_left, 2)})
    response["Cache-Control"] = "no-store"
    return response


def _served_questions(attempt):
    """Questions the student saw, in order, with each option marked correct and/or chosen."""
    questions = list(attempt.questions.filter(served_at__isnull=False).order_by("order"))
    for q in questions:
        q.rows = [
            {"text": text, "correct": i in q.correct, "chosen": i in (q.selected or [])}
            for i, text in enumerate(q.options)
        ]
        q.objective = attempt.objectives[q.objective_index]["text"] if q.objective_index < len(attempt.objectives) else ""
    return questions


@launched(instructor=False)
def feedback(request, assignment, attempt_id):
    attempt = get_object_or_404(
        Attempt, pk=attempt_id, assignment=assignment, lti_user_id=request.launch["user_id"]
    )
    if attempt.status != Attempt.COMPLETED:
        return redirect("attempt", assignment.pk, attempt.pk)
    return render(
        request,
        "tool/feedback.html",
        {
            "assignment": assignment,
            "attempt": attempt,
            "questions": _served_questions(attempt),
            "times_left": attempt.events.filter(kind__in=IntegrityEvent.LEFT_PAGE_KINDS).count(),
        },
    )


@launched(instructor=True)
def attempt_detail(request, assignment, attempt_id):
    attempt = get_object_or_404(Attempt, pk=attempt_id, assignment=assignment)
    return render(request, "tool/attempt_detail.html", _detail_context(assignment, attempt))


@require_POST
@launched(instructor=True)
def invite(request, assignment, attempt_id):
    """Invite the student to try again: one more attempt, shown to them next time they open the assignment."""
    attempt = get_object_or_404(Attempt, pk=attempt_id, assignment=assignment)
    if not _pending_invitation(assignment, attempt.lti_user_id):
        Invitation.objects.create(assignment=assignment, lti_user_id=attempt.lti_user_id, attempt=attempt)
    return redirect("attempt_detail", assignment.pk, attempt.pk)


@require_POST
@launched(instructor=True)
def withdraw_invitation(request, assignment, attempt_id):
    attempt = get_object_or_404(Attempt, pk=attempt_id, assignment=assignment)
    invitation = _pending_invitation(assignment, attempt.lti_user_id)
    if invitation:
        invitation.delete()
    return redirect("attempt_detail", assignment.pk, attempt.pk)


FLAG_GROUPS = [
    ("left the page", IntegrityEvent.LEFT_PAGE_KINDS),
    ("copy, cut or paste", ["copy", "cut", "paste"]),
    ("room word missed", ["room_failed"]),
]


def _flag_summary(log):
    """Total flags and a short breakdown, e.g. [(3, "left the page"), (1, "copy, cut or paste")]."""
    parts = []
    for label, kinds in FLAG_GROUPS:
        count = sum(1 for e in log if e.kind in kinds)
        if count:
            parts.append((count, label))
    return sum(n for n, _ in parts), parts


def _detail_context(assignment, attempt, demo=False):
    others = assignment.attempts.filter(lti_user_id=attempt.lti_user_id).exclude(status__in=NOT_COUNTED).order_by("number")
    log = _integrity_log(attempt.events.select_related("question"))
    flags, flag_parts = _flag_summary(log)
    return {
        "assignment": assignment,
        "attempt": attempt,
        "questions": _served_questions(attempt),
        "events": log,
        "flags": flags,
        "flag_parts": flag_parts,
        "others": [] if demo else others,
        "demo": demo,
        "demo_slug": demos.slug_for(assignment) if demo else None,
        "invitation": None if demo else _pending_invitation(assignment, attempt.lti_user_id),
        "can_start": None if demo else _can_start(assignment, attempt.lti_user_id),
    }


@launched(instructor=True)
def attempt_file(request, assignment, attempt_id):
    attempt = get_object_or_404(Attempt, pk=attempt_id, assignment=assignment)
    if not attempt.file:
        raise Http404
    return FileResponse(attempt.file.open("rb"), as_attachment=True, filename=attempt.original_filename)


# ---------------------------------------------------------------- landing-page demos

def client_ip(request):
    """The visitor's address. The app is reachable only through the Cloudflare tunnel (or from this
    machine), so Cloudflare's header can be trusted; otherwise every visitor shares the tunnel's address."""
    forwarded = request.META.get("HTTP_CF_CONNECTING_IP") or request.META.get("HTTP_X_FORWARDED_FOR", "").split(",")[0]
    return forwarded.strip() or request.META.get("REMOTE_ADDR", "")


DEMO_KEEP = timedelta(days=2)
DEMO_STARTS_PER_HOUR = 30


def demo_file(request, slug):
    """The demo's coursework file: PDFs open in the browser, other files download."""
    demo = demos.get(slug)
    if not demo:
        raise Http404
    path = demos.file_path(demo)
    inline = path.suffix == ".pdf" or path.suffix == ".txt"
    return FileResponse(path.open("rb"), as_attachment=not inline, filename=demo["file"])


@require_POST
def demo_start(request, slug):
    """Try a demo: a fresh attempt for this visitor, ready to begin."""
    demo = demos.get(slug)
    prepared = demo and demos.prepared(demo)
    if not prepared:
        raise Http404
    ip_key = f"aletheia:demo-starts:{client_ip(request)}"
    starts = cache.get(ip_key, 0)
    if starts >= DEMO_STARTS_PER_HOUR:
        return error(request, "You've started a lot of demos in the last hour. Please try again later.", status=429)
    cache.set(ip_key, starts + 1, 3600)

    for old in Attempt.objects.filter(
        assignment__platform__issuer=demos.DEMO_ISSUER, created_at__lt=timezone.now() - DEMO_KEEP
    ):
        old.file.delete(save=False)
        old.delete()

    assignment = demos.assignment(demo)
    visitor = request.session.get("demo_user") or demos.DEMO_USER_PREFIX + secrets.token_urlsafe(12)
    request.session["demo_user"] = visitor
    launches = request.session.get("lti_launches", {})
    launches[str(assignment.pk)] = {"user_id": visitor, "name": "Visitor, Demo", "instructor": False}
    request.session["lti_launches"] = launches

    with transaction.atomic():
        for previous in assignment.attempts.filter(lti_user_id=visitor):
            previous.file.delete(save=False)
            previous.delete()
        s = demo["settings"]
        attempt = Attempt(
            assignment=assignment, lti_user_id=visitor, user_name="Visitor, Demo", status=Attempt.READY,
            seconds_per_question=assignment.seconds_per_question, time_each_question=assignment.time_each_question,
            time_limit_minutes=assignment.time_limit_minutes, code_viva=assignment.code_viva,
            objective_count=assignment.objective_count, questions_per_objective=assignment.questions_per_objective,
            retries_per_objective=assignment.retries_per_objective, original_filename=demo["file"],
            submission_text=extract_text(str(demos.file_path(demo)), code=s.get("code_viva", False), name=demo["file"]),
        )
        with demos.file_path(demo).open("rb") as f:
            attempt.file.save(demo["file"], File(f), save=False)
        attempt.file_confirmed_at = timezone.now()  # demos skip the "check your file" step
        attempt.save()
        services.load_demo(attempt, prepared)
    return redirect("attempt", assignment.pk, attempt.pk)


def demo_review(request, attempt_id):
    """The instructor's view of the visitor's own demo attempt."""
    visitor = request.session.get("demo_user")
    attempt = get_object_or_404(
        Attempt, pk=attempt_id, lti_user_id=visitor or "-", assignment__platform__issuer=demos.DEMO_ISSUER
    )
    return render(request, "tool/attempt_detail.html", _detail_context(attempt.assignment, attempt, demo=True))


# ---------------------------------------------------------------- legal pages

def _legal(request, page):
    return render(request, f"tool/{page}.html",
                  {"page": page, "legal": settings.LEGAL, "contact_email": settings.CONTACT_EMAIL})


def terms(request):
    return _legal(request, "terms")


def privacy(request):
    return _legal(request, "privacy")


# ---------------------------------------------------------------- contact and calls

CONTACTS_PER_HOUR = 5


@require_POST
def contact(request):
    """The landing page's contact form (sent with fetch). A requested call must be confirmed by email."""
    form = ContactForm(request.POST)
    if not form.is_valid():
        errors = {field: [str(e) for e in errs] for field, errs in form.errors.items()}
        return JsonResponse({"ok": False, "errors": errors}, status=400)
    data = form.cleaned_data
    if data["website"]:
        return JsonResponse({"ok": True, "message": "Thank you."})  # quietly ignore bots
    ip_key = f"aletheia:contacts:{client_ip(request)}"
    if cache.get(ip_key, 0) >= CONTACTS_PER_HOUR:
        return JsonResponse({"ok": False, "errors": {"__all__": ["Too many messages. Please try again later."]}}, status=429)
    cache.set(ip_key, cache.get(ip_key, 0) + 1, 3600)

    meeting = data.get("meeting")
    if meeting and not booking.is_available(meeting):
        return JsonResponse({"ok": False, "errors": {"meeting": ["That time has just been taken. Please choose another."]}}, status=409)
    enquiry = Enquiry.objects.create(
        name=data["name"], email=data["email"], institution=data["institution"], message=data["message"],
        meeting_start=meeting, status=Enquiry.PENDING if meeting else Enquiry.RECEIVED, token=booking.new_token(),
    )
    try:
        booking.notify_new(enquiry)
        if meeting:
            booking.ask_to_confirm(enquiry, request.build_absolute_uri(reverse("meet_confirm", args=[enquiry.token])))
    except Exception:
        log.exception("Couldn't send contact emails (enquiry %s)", enquiry.pk)
        return JsonResponse({"ok": False, "errors": {"__all__": [
            f"We couldn't send email just now. Please write to {settings.CONTACT_EMAIL}."]}}, status=502)
    if meeting:
        return JsonResponse({"ok": True, "message": (
            f"Thank you. We've emailed {enquiry.email} a link to confirm your call on "
            f"{booking.describe(meeting)}. The time is held for 2 hours.")})
    return JsonResponse({"ok": True, "message": "Thank you. We'll be in touch soon."})


def meet_slots(request):
    response = JsonResponse({
        "time_zone": settings.MEETING_TIME_ZONE,
        "minutes": settings.MEETING_MINUTES,
        "slots": [slot.strftime("%Y-%m-%dT%H:%M:%SZ") for slot in booking.available_slots()],
    })
    response["Cache-Control"] = "no-store"
    return response


def meet_confirm(request, token):
    """Confirm a call. A button (POST) does it, so email scanners that open links can't."""
    enquiry = get_object_or_404(Enquiry, token=token, meeting_start__isnull=False)
    state = enquiry.status
    if request.method == "POST" and enquiry.status == Enquiry.PENDING:
        if timezone.now() - enquiry.created_at > booking.CONFIRM_WITHIN or not booking.is_available(
            enquiry.meeting_start, ignore=enquiry
        ):
            state = "taken"
        else:
            enquiry.status, enquiry.confirmed_at = Enquiry.CONFIRMED, timezone.now()
            enquiry.save(update_fields=["status", "confirmed_at"])
            booking.send_confirmed(enquiry, request.build_absolute_uri(reverse("meet_cancel", args=[enquiry.token])))
            state = Enquiry.CONFIRMED
    elif enquiry.status == Enquiry.PENDING and timezone.now() - enquiry.created_at > booking.CONFIRM_WITHIN:
        state = "taken"
    return render(request, "tool/meet.html", {
        "enquiry": enquiry, "state": state, "when": booking.describe(enquiry.meeting_start), "action": "confirm",
        "meeting_url": settings.MEETING_URL,
    })


def meet_cancel(request, token):
    enquiry = get_object_or_404(Enquiry, token=token, meeting_start__isnull=False)
    if request.method == "POST" and enquiry.status in (Enquiry.PENDING, Enquiry.CONFIRMED):
        was_confirmed = enquiry.status == Enquiry.CONFIRMED
        enquiry.status = Enquiry.CANCELLED
        enquiry.save(update_fields=["status"])
        if was_confirmed:
            booking.send_cancelled(enquiry)
    return render(request, "tool/meet.html", {
        "enquiry": enquiry, "state": enquiry.status, "when": booking.describe(enquiry.meeting_start), "action": "cancel",
    })


# ---------------------------------------------------------------- local development

LOCAL_HOSTS = {"localhost", "127.0.0.1"}
PROXY_HEADERS = ("HTTP_CF_RAY", "HTTP_CF_CONNECTING_IP", "HTTP_X_FORWARDED_FOR", "HTTP_X_FORWARDED_HOST")


def dev_allowed(request):
    """Dev sign-in is for this machine only: never through a tunnel or proxy."""
    return (
        settings.DEV_LOGIN
        and request.get_host().split(":")[0] in LOCAL_HOSTS
        and not any(request.META.get(h) for h in PROXY_HEADERS)
    )


def dev_launch(request, role):
    """Simulate an LTI launch (local development only) so the app can be tried without an LMS."""
    if not dev_allowed(request) or role not in ("instructor", "student"):
        raise Http404
    platform, _ = Platform.objects.get_or_create(
        issuer="http://localhost", client_id="dev",
        defaults={"name": "Local development", "auth_login_url": "http://localhost/", "jwks_url": "http://localhost/"},
    )
    assignment, _ = Assignment.objects.get_or_create(
        platform=platform, deployment_id="dev", resource_link_id="dev",
        defaults={"title": "Sample assignment", "context_title": "Local course"},
    )
    instructor = role == "instructor"
    launches = request.session.get("lti_launches", {})
    launches[str(assignment.pk)] = {
        "user_id": "dev-instructor" if instructor else "dev-student",
        "name": "Dev Instructor" if instructor else "Dev Student",
        "instructor": instructor,
    }
    request.session["lti_launches"] = launches
    return redirect("assignment", assignment.pk)


@require_POST
def dev_reset(request):
    """Delete the dev student's attempts (local development only)."""
    if not dev_allowed(request):
        raise Http404
    for attempt in Attempt.objects.filter(lti_user_id="dev-student", assignment__resource_link_id="dev"):
        attempt.file.delete(save=False)
        attempt.delete()
    return redirect("dev_launch", "student")
