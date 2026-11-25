"""Canvas through an API token: the route for pilots that can't install LTI yet.

The token belongs to an account that is a teacher in each linked course. With it, Aletheia reads the class
list, writes its iframe into an assignment's description, sends sign-in links to students' Canvas inboxes,
and hands uploads in on the student's behalf (grading rights; never a grade). Every course it touches must
be on the connection's allow-list.
"""

import base64
import hashlib
import json
import logging
import re
import urllib.error
import urllib.parse
import urllib.request

from cryptography.fernet import Fernet, InvalidToken
from django.conf import settings
from django.db import transaction
from django.utils import timezone

from .models import Assignment, CanvasConnection, CanvasCourse, CanvasMember, Platform

log = logging.getLogger(__name__)

TIMEOUT = 20
DEPLOYMENT_ID = "canvas-token"


class CanvasError(Exception):
    def __init__(self, message, status=0):
        super().__init__(message)
        self.status = status


# ---------------------------------------------------------------- the token

def _fernet():
    key = getattr(settings, "ALETHEIA_ENCRYPTION_KEY", "")
    if not key:  # derived from SECRET_KEY: changing SECRET_KEY means re-entering every token
        key = base64.urlsafe_b64encode(hashlib.sha256(b"aletheia-canvas-token:" + settings.SECRET_KEY.encode()).digest())
    return Fernet(key)


def set_token(connection, token):
    token = token.strip()
    connection.token_encrypted = _fernet().encrypt(token.encode()).decode()
    connection.token_hint = token[-4:]


def _token(connection):
    if not connection.token_encrypted:
        raise CanvasError("This connection has no token (it may have been removed when the pilot ended).")
    try:
        return _fernet().decrypt(connection.token_encrypted.encode()).decode()
    except InvalidToken:
        raise CanvasError("The stored token can't be decrypted. Enter it again.")


def ensure_platform(connection):
    """Each connection has a Platform record, so its assignments fit the same model as LTI ones."""
    if connection.platform_id:
        return connection.platform
    platform, _ = Platform.objects.get_or_create(
        issuer=f"canvas-token:{connection.origin}",
        client_id="token",
        defaults={
            "name": f"{connection.name} (Canvas API token)",
            "deployment_ids": DEPLOYMENT_ID,
            "auth_login_url": connection.origin,
            "jwks_url": connection.origin,
        },
    )
    connection.platform = platform
    return platform


# ---------------------------------------------------------------- HTTP

def _course_id_in(path):
    m = re.match(r"^/courses/(\d+)", path)
    return m.group(1) if m else None


def request(connection, method, path, data=None, params=None, absolute_url=None, auth=True):
    """One Canvas API call. Refuses any course that isn't on the connection's allow-list."""
    if absolute_url is None:
        course_id = _course_id_in(path)
        if course_id is not None and not connection.course_allowed(course_id):
            raise CanvasError(f"Course {course_id} isn't on this connection's list of allowed courses.")
        url = f"{connection.origin}/api/v1{path}"
        if params:
            url += "?" + urllib.parse.urlencode(params, doseq=True)
    else:
        url = absolute_url
    body = urllib.parse.urlencode(data, doseq=True).encode() if data is not None else None
    headers = {"Accept": "application/json", "User-Agent": "Aletheia"}
    if auth:
        headers["Authorization"] = f"Bearer {_token(connection)}"
    req = urllib.request.Request(url, data=body, method=method, headers=headers)
    if method != "GET":
        log.info("Canvas %s %s", method, path or "(upload)")
    try:
        with urllib.request.urlopen(req, timeout=TIMEOUT) as resp:
            text = resp.read().decode()
            return (json.loads(text) if text.strip() else None), resp.headers
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode(errors="replace")[:300]
        raise CanvasError(_error_message(exc.code, detail), status=exc.code)
    except urllib.error.URLError as exc:
        raise CanvasError(f"Couldn't reach Canvas: {exc.reason}")


def _error_message(status, detail):
    if status == 401:
        return "Canvas didn't accept the token (it may have expired or been deleted)."
    if status == 403:
        return "The token's account isn't allowed to do this. Is it a teacher in the course?"
    if status == 404:
        return "Canvas couldn't find that (check the course or assignment ID)."
    try:
        errors = json.loads(detail).get("errors")
        if isinstance(errors, list) and errors:
            return f"Canvas said: {errors[0].get('message', errors[0])}"
    except (ValueError, AttributeError):
        pass
    return f"Canvas returned an error ({status})."


def _next_url(headers):
    for part in (headers.get("Link") or "").split(","):
        if 'rel="next"' in part:
            return part[part.find("<") + 1:part.find(">")]
    return None


def paginate(connection, path, params=None):
    data, headers = request(connection, "GET", path, params={"per_page": 100, **(params or {})})
    items = list(data or [])
    url = _next_url(headers)
    while url:
        if not url.startswith(connection.origin + "/api/v1/"):
            break
        data, headers = request(connection, "GET", None, absolute_url=url)
        items.extend(data or [])
        url = _next_url(headers)
    return items


# ---------------------------------------------------------------- checking the connection

NEEDED = {
    "read_roster": "read the class list",
    "send_messages": "send sign-in links to students' inboxes",
    "manage_grades": "hand uploads in on the student's behalf",
}


def allowed_course_ids(connection):
    return sorted({c.strip() for c in connection.allowed_course_ids.replace(",", "\n").split() if c.strip()})


def check(connection):
    """Whose token this is, and whether it can do what Aletheia needs in each allowed course."""
    result = {"ok": True, "courses": {}}
    try:
        me, _ = request(connection, "GET", "/users/self")
        result["account"] = me.get("name", "")
        connection.sender_name = me.get("name", "")
    except CanvasError as exc:
        result.update(ok=False, error=str(exc))
    else:
        for course_id in allowed_course_ids(connection):
            course = {"ok": True, "missing": []}
            try:
                info, _ = request(connection, "GET", f"/courses/{course_id}")
                course["name"] = info.get("name", "")
                perms, _ = request(connection, "GET", f"/courses/{course_id}/permissions",
                                   params={"permissions[]": list(NEEDED)})
                course["missing"] = [NEEDED[p] for p in NEEDED if not perms.get(p)]
                mine, _ = request(connection, "GET", f"/courses/{course_id}/enrollments",
                                  params={"user_id": "self", "type[]": ["TeacherEnrollment"]})
                course["teacher"] = bool(mine)
                if course["missing"] or not course["teacher"]:
                    course["ok"] = False
            except CanvasError as exc:
                course.update(ok=False, error=str(exc))
            result["courses"][course_id] = course
            result["ok"] = result["ok"] and course["ok"]
    connection.check_result = result
    connection.checked_at = timezone.now()
    connection.save(update_fields=["check_result", "checked_at", "sender_name"])
    return result


# ---------------------------------------------------------------- the class list

ROLES = {
    "StudentEnrollment": CanvasMember.STUDENT,
    "TeacherEnrollment": CanvasMember.TEACHER,
    "TaEnrollment": CanvasMember.TA,
    "DesignerEnrollment": CanvasMember.DESIGNER,
}  # Observers and Student View's test student are left out.
RANK = {CanvasMember.TEACHER: 4, CanvasMember.TA: 3, CanvasMember.DESIGNER: 2, CanvasMember.STUDENT: 1}


def sync_course(course):
    """Refresh the course's name and class list from Canvas. People who have left are kept, marked inactive."""
    connection = course.connection
    try:
        info, _ = request(connection, "GET", f"/courses/{course.canvas_course_id}")
        enrollments = paginate(connection, f"/courses/{course.canvas_course_id}/enrollments",
                               {"state[]": ["active"], "type[]": list(ROLES)})
    except CanvasError as exc:
        course.sync_error = str(exc)
        course.save(update_fields=["sync_error"])
        raise
    people = {}
    for e in enrollments:
        role = ROLES.get(e.get("type"))
        user = e.get("user") or {}
        if not role or not user.get("id"):
            continue
        current = people.get(user["id"])
        if current is None or RANK[role] > RANK[current["role"]]:  # staff role wins over a student one
            people[user["id"]] = {
                "role": role,
                "name": user.get("name") or user.get("short_name") or f"Canvas user {user['id']}",
                "sortable_name": user.get("sortable_name", ""),
            }
    with transaction.atomic():
        for user_id, p in people.items():
            CanvasMember.objects.update_or_create(
                course=course, canvas_user_id=user_id, defaults={**p, "active": True}
            )
        course.members.exclude(canvas_user_id__in=people).update(active=False)
        course.name = info.get("name", course.name)
        course.synced_at = timezone.now()
        course.sync_error = ""
        course.save(update_fields=["name", "synced_at", "sync_error"])
    return len(people)


# ---------------------------------------------------------------- assignments

FRAME = re.compile(r"\s*<iframe[^>]*\bdata-aletheia=[\"']frame[\"'][^>]*>\s*</iframe>", re.IGNORECASE)


def frame_html(src):
    return (f'<iframe title="Aletheia" src="{src}" width="100%" height="900" '
            f'allow="clipboard-write" data-aletheia="frame"></iframe>')


def list_assignments(course):
    items = paginate(course.connection, f"/courses/{course.canvas_course_id}/assignments", {"order_by": "position"})
    return [
        {"id": a["id"], "name": a.get("name", ""), "published": a.get("published", False),
         "submission_types": a.get("submission_types", []),
         "has_frame": bool(FRAME.search(a.get("description") or ""))}
        for a in items
    ]


def link_assignment(course, frame_url_for, canvas_assignment_id=None, new_name=""):
    """Put Aletheia in a Canvas assignment (an existing one, or a new unpublished one).

    `frame_url_for(assignment)` gives the absolute URL of the iframe for an Aletheia assignment.
    Returns the Aletheia Assignment, which the instructor configures on first sign-in, as with LTI.
    """
    connection = course.connection
    base = f"/courses/{course.canvas_course_id}/assignments"
    if canvas_assignment_id is None:
        created, _ = request(connection, "POST", base, data={
            "assignment[name]": new_name or "Aletheia check",
            "assignment[submission_types][]": "online_upload",
            "assignment[published]": "false",
            "assignment[notify_of_update]": "false",
        })
        canvas_assignment_id = created["id"]
    current, _ = request(connection, "GET", f"{base}/{canvas_assignment_id}")
    assignment, _ = Assignment.objects.update_or_create(
        platform=ensure_platform(connection),
        deployment_id=DEPLOYMENT_ID,
        resource_link_id=f"canvas-assignment-{canvas_assignment_id}",
        defaults={
            "canvas_course": course,
            "canvas_assignment_id": canvas_assignment_id,
            "context_id": str(course.canvas_course_id),
            "context_title": course.name,
            "title": current.get("name", ""),
        },
    )
    description = FRAME.sub("", current.get("description") or "")
    types = [t for t in current.get("submission_types", []) if t not in ("none", "on_paper", "external_tool")]
    if "online_upload" not in types:
        types.append("online_upload")
    request(connection, "PUT", f"{base}/{canvas_assignment_id}", data={
        "assignment[description]": description + frame_html(frame_url_for(assignment)),
        "assignment[submission_types][]": types,
        "assignment[notify_of_update]": "false",
    })
    return assignment


def unlink_assignment(assignment):
    """Take Aletheia's iframe out of the Canvas assignment. The assignment and its submissions stay."""
    course = assignment.canvas_course
    path = f"/courses/{course.canvas_course_id}/assignments/{assignment.canvas_assignment_id}"
    try:
        current, _ = request(course.connection, "GET", path)
    except CanvasError as exc:
        if exc.status == 404:  # deleted in Canvas already
            return
        raise
    description = current.get("description") or ""
    if FRAME.search(description):
        request(course.connection, "PUT", path, data={
            "assignment[description]": FRAME.sub("", description), "assignment[notify_of_update]": "false",
        })


def offboard(connection):
    """End a pilot: remove Aletheia from every linked assignment, then delete the stored token."""
    problems = []
    for assignment in Assignment.objects.filter(canvas_course__connection=connection):
        try:
            unlink_assignment(assignment)
        except CanvasError as exc:
            problems.append(f"{assignment.title or assignment.canvas_assignment_id}: {exc}")
    if not problems:
        connection.token_encrypted = ""
        connection.token_hint = ""
        connection.save(update_fields=["token_encrypted", "token_hint"])
    return problems


# ---------------------------------------------------------------- signing in through the inbox

def send_sign_in_message(member, assignment, link_url):
    """A one-time sign-in link, to the person's Canvas inbox (from the token's account)."""
    course = member.course
    title = assignment.title or "your assignment"
    body = (
        f"Here is your link to sign in to Aletheia for “{title}” in {course.name}:\n\n"
        f"{link_url}\n\n"
        "It works once, within the next hour, and takes you back into Canvas.\n\n"
        "If you didn't ask for this, you can ignore it."
    )
    request(course.connection, "POST", "/conversations", data={
        "recipients[]": [member.canvas_user_id],
        "subject": f"Sign in to Aletheia: {title}"[:255],
        "body": body,
        "force_new": 1,
        "context_code": f"course_{course.canvas_course_id}",
    })


def create_sign_in_page(course, frame_src):
    """A short-lived Canvas page holding Aletheia's iframe with a one-time code. Returns (slug, html_url)."""
    page, _ = request(course.connection, "POST", f"/courses/{course.canvas_course_id}/pages", data={
        "wiki_page[title]": f"Aletheia sign-in {timezone.now():%H%M%S%f}",
        "wiki_page[body]": (f'<iframe title="Aletheia sign-in" src="{frame_src}" width="100%" height="500" '
                            f'data-aletheia="sign-in"></iframe>'),
        "wiki_page[published]": 1,
        "wiki_page[editing_roles]": "teachers",
        "wiki_page[notify_of_update]": 0,
    })
    slug = page.get("url", "")
    return slug, page.get("html_url") or f"{course.url}/pages/{slug}"


def delete_sign_in_page(course, slug):
    if not slug.startswith("aletheia-sign-in"):
        return  # only ever delete pages Aletheia made
    try:
        request(course.connection, "DELETE", f"/courses/{course.canvas_course_id}/pages/{urllib.parse.quote(slug)}")
    except CanvasError as exc:
        if exc.status != 404:
            raise
