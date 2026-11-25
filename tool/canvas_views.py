"""Pages Canvas shows in Aletheia's iframe (the API-token route): signing in through the Canvas inbox.

1. The iframe in the Canvas assignment: the person finds their name, and a one-time link goes to their
   Canvas inbox (sent from the token's account).
2. The link (opened from the inbox, top-level) creates a temporary Canvas page whose iframe carries a
   one-time code, and sends them there, so they are back inside Canvas.
3. That iframe exchanges the code for a 12-hour sign-in (the same session entry an LTI launch makes),
   deletes the temporary page, and offers a button back to the assignment, where they are now signed in.
"""

import hashlib
import secrets
import time

from django.core.cache import cache
from django.db import transaction
from django.http import JsonResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.utils import timezone
from django.views.decorators.clickjacking import xframe_options_exempt
from django.views.decorators.csrf import ensure_csrf_cookie
from django.views.decorators.http import require_POST

from . import canvas
from .models import Assignment, CanvasMember, CanvasSignIn

SEARCHES_PER_MINUTE = 30
SEND_GAP_SECONDS = 60
SENDS_PER_DAY = 10


def _hash(value):
    return hashlib.sha256(value.encode()).hexdigest()


def _assignment(assignment_id):
    return get_object_or_404(
        Assignment.objects.select_related("canvas_course__connection"), pk=assignment_id, canvas_course__isnull=False
    )


def _framed(response, assignment):
    """Only the linked Canvas may show these pages in a frame."""
    response["Content-Security-Policy"] = f"frame-ancestors 'self' {assignment.canvas_course.connection.origin}"
    response["Cache-Control"] = "no-store"
    return response


def _canvas_assignment_url(assignment):
    return f"{assignment.canvas_course.url}/assignments/{assignment.canvas_assignment_id}"


def signed_in(request, assignment):
    """The person's sign-in for this assignment, if any and not expired."""
    launch = request.session.get("lti_launches", {}).get(str(assignment.pk))
    if launch and launch.get("canvas_until", 0) > time.time():
        return launch
    return None


def _render(request, assignment, template, context=None, status=200):
    page = render(request, template, {"assignment": assignment, "canvas_assignment_url": _canvas_assignment_url(assignment),
                                      **(context or {})}, status=status)
    return _framed(page, assignment)


@xframe_options_exempt
@ensure_csrf_cookie
def frame(request, assignment_id):
    """The iframe in the Canvas assignment (and, with ?code=, on the temporary sign-in page)."""
    assignment = _assignment(assignment_id)
    if request.GET.get("code"):
        return _exchange(request, assignment, request.GET["code"])
    if signed_in(request, assignment):
        return redirect("assignment", assignment.pk)
    if not assignment.canvas_course.connection.token_encrypted:
        return _render(request, assignment, "tool/canvas_sign_in.html", {"disconnected": True})
    return _render(request, assignment, "tool/canvas_sign_in.html")


@xframe_options_exempt
@require_POST
def search(request, assignment_id):
    """Names in the class list matching what was typed (at least two characters; at most ten names)."""
    assignment = _assignment(assignment_id)
    ip_key = f"aletheia:canvas-search:{request.META.get('REMOTE_ADDR', '')}"
    if cache.get(ip_key, 0) >= SEARCHES_PER_MINUTE:
        return JsonResponse({"error": "Too many searches. Wait a minute and try again."}, status=429)
    cache.set(ip_key, cache.get(ip_key, 0) + 1, 60)
    words = request.POST.get("q", "").lower().split()
    if sum(len(w) for w in words) < 2:
        return JsonResponse({"results": []})
    members = assignment.canvas_course.members.filter(active=True)
    for word in words:
        members = members.filter(name__icontains=word)
    return JsonResponse({"results": [{"id": m.pk, "name": m.name} for m in members.order_by("name")[:10]]})


@xframe_options_exempt
@require_POST
def send(request, assignment_id):
    assignment = _assignment(assignment_id)
    member = get_object_or_404(CanvasMember, pk=request.POST.get("member"), course=assignment.canvas_course, active=True)
    gap_key, day_key = f"aletheia:canvas-send-gap:{member.pk}", f"aletheia:canvas-send-day:{member.pk}"
    if cache.get(gap_key):
        return _render(request, assignment, "tool/canvas_sign_in.html",
                       {"problem": "A link was sent less than a minute ago. Check your Canvas inbox, or wait a minute and try again."}, 429)
    if cache.get(day_key, 0) >= SENDS_PER_DAY:
        return _render(request, assignment, "tool/canvas_sign_in.html",
                       {"problem": "Too many sign-in links today. Use the newest one in your Canvas inbox, or try again tomorrow."}, 429)
    token = secrets.token_urlsafe(32)
    with transaction.atomic():
        CanvasSignIn.objects.filter(member=member, assignment=assignment, link_used_at__isnull=True,
                                    superseded_at__isnull=True).update(superseded_at=timezone.now())
        CanvasSignIn.objects.create(member=member, assignment=assignment, link_hash=_hash(token))
    try:
        canvas.send_sign_in_message(member, assignment, request.build_absolute_uri(reverse("canvas_sign_in", args=[token])))
    except canvas.CanvasError as exc:
        return _render(request, assignment, "tool/canvas_sign_in.html",
                       {"problem": f"Couldn't send the link: {exc}"}, 502)
    cache.set(gap_key, True, SEND_GAP_SECONDS)
    cache.set(day_key, cache.get(day_key, 0) + 1, 24 * 3600)
    _tidy_pages(assignment)
    return _render(request, assignment, "tool/canvas_sent.html",
                   {"member": member, "inbox_url": f"{assignment.canvas_course.connection.origin}/conversations"})


def sign_in_link(request, token):
    """Opened from the Canvas inbox: make the temporary Canvas page with a one-time code, and go there."""
    sign_in = CanvasSignIn.objects.select_related("assignment__canvas_course__connection", "member").filter(
        link_hash=_hash(token)).first()
    if sign_in is None:
        return render(request, "tool/canvas_link_status.html", {"problem": "This sign-in link isn't valid."}, status=404)
    assignment = sign_in.assignment
    back = _canvas_assignment_url(assignment)
    if not sign_in.link_usable():
        if sign_in.superseded_at:
            problem = "A newer sign-in link has been sent. Use the newest message in your Canvas inbox."
        elif sign_in.link_used_at:
            problem = "This sign-in link has already been used."
        else:
            problem = "This sign-in link has expired (links last an hour)."
        return render(request, "tool/canvas_link_status.html", {"problem": problem, "back": back}, status=410)
    code = secrets.token_urlsafe(24)
    frame_src = request.build_absolute_uri(reverse("canvas_frame", args=[assignment.pk])) + f"?code={code}"
    try:
        slug, page_url = canvas.create_sign_in_page(assignment.canvas_course, frame_src)
    except canvas.CanvasError as exc:
        return render(request, "tool/canvas_link_status.html", {"problem": f"Couldn't open Canvas: {exc}", "back": back}, status=502)
    sign_in.link_used_at = timezone.now()
    sign_in.code_hash = _hash(code)
    sign_in.page_slug = slug
    sign_in.save(update_fields=["link_used_at", "code_hash", "page_slug"])
    response = redirect(page_url)
    response.status_code = 303
    response["Cache-Control"] = "no-store"
    return response


def _exchange(request, assignment, code):
    with transaction.atomic():
        sign_in = CanvasSignIn.objects.select_for_update().select_related("member").filter(
            code_hash=_hash(code), assignment=assignment).first()
        if sign_in is None or not sign_in.code_usable():
            return _render(request, assignment, "tool/canvas_sign_in.html",
                           {"problem": "That sign-in has expired or was already used. Send yourself a new link."}, 410)
        sign_in.code_used_at = timezone.now()
        sign_in.save(update_fields=["code_used_at"])
    member = sign_in.member
    launches = request.session.get("lti_launches", {})
    launches[str(assignment.pk)] = {
        "user_id": member.lti_user_id,
        "name": member.name,
        "instructor": member.is_staff,
        "canvas_until": time.time() + CanvasSignIn.SESSION_LIFETIME.total_seconds(),
    }
    request.session["lti_launches"] = launches
    request.session.cycle_key()
    try:
        canvas.delete_sign_in_page(assignment.canvas_course, sign_in.page_slug)
        CanvasSignIn.objects.filter(pk=sign_in.pk).update(page_slug="")
    except canvas.CanvasError:
        pass  # tidied up later
    return _render(request, assignment, "tool/canvas_signed_in.html", {"member": member})


def _tidy_pages(assignment):
    """Delete temporary sign-in pages left behind (code never used, or deletion failed)."""
    stale = CanvasSignIn.objects.filter(
        assignment__canvas_course=assignment.canvas_course, link_used_at__lt=timezone.now() - CanvasSignIn.CODE_LIFETIME,
    ).exclude(page_slug="")[:5]
    for sign_in in stale:
        try:
            canvas.delete_sign_in_page(assignment.canvas_course, sign_in.page_slug)
            CanvasSignIn.objects.filter(pk=sign_in.pk).update(page_slug="")
        except canvas.CanvasError:
            break
