"""LTI Names and Role Provisioning Services: the course's class list, for the instructor roster."""

import logging
import re

from django.core.cache import cache

from . import ags, lti

log = logging.getLogger(__name__)

MEMBERSHIP_SCOPE = "https://purl.imsglobal.org/spec/lti-nrps/scope/contextmembership.readonly"
MEMBERSHIP_MEDIA_TYPE = "application/vnd.ims.lti-nrps.v2.membershipcontainer+json"
CACHE_SECONDS = 5 * 60
FAILURE_CACHE_SECONDS = 60
MAX_PAGES = 50


def class_list(assignment):
    """Active learners on the course as [{"user_id", "name"}], plus a problem message if it couldn't be read.

    Returns (None, None) when the LMS offers no class list for this placement.
    """
    if not assignment.memberships_url:
        return None, None
    key = f"aletheia:class-list:{assignment.pk}"
    cached = cache.get(key)
    if cached is not None:
        return cached
    try:
        result = (_fetch(assignment), None)
    except lti.ServiceError as exc:
        log.warning("Class list unavailable (assignment %s): %s", assignment.pk, exc)
        result = (None, str(exc))
        cache.set(key, result, FAILURE_CACHE_SECONDS)
        return result
    cache.set(key, result, CACHE_SECONDS)
    return result


def _fetch(assignment):
    token = ags.access_token(assignment.platform, (MEMBERSHIP_SCOPE,))
    headers = {"Authorization": f"Bearer {token}", "Accept": MEMBERSHIP_MEDIA_TYPE}
    url, students = assignment.memberships_url, {}
    for _ in range(MAX_PAGES):
        data, response_headers = lti.http_json("GET", url, headers=headers, with_headers=True)
        for member in data.get("members") or []:
            user_id = str(member.get("user_id") or "")
            if not user_id or (member.get("status") or "Active") != "Active":
                continue
            claims = {lti.CLAIM + "roles": member.get("roles") or []}
            if lti.is_instructor(claims) or not lti.is_student(claims):
                continue  # staff, observers and anyone else aren't on the roster
            students[user_id] = _name(member)
        url = next_link(response_headers)
        if not url:
            break
    return [{"user_id": user_id, "name": name} for user_id, name in students.items()]


def _name(member):
    name = lti.surname_first(member.get("name"), member.get("given_name"), member.get("family_name"))
    return name or member.get("email") or "Student"


def next_link(headers):
    """The rel="next" URL from a Link header, if any."""
    link = next((v for k, v in (headers or {}).items() if k.lower() == "link"), "")
    for part in link.split(","):
        match = re.search(r'<([^>]+)>\s*;.*\brel="?next"?', part)
        if match:
            return match.group(1)
    return None
