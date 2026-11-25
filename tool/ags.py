"""LTI Assignment and Grade Services: put each upload in the LMS gradebook as a normal submission.

On Canvas the score carries Canvas's submission extension, so the uploaded file becomes a
real submission attempt that teachers mark in SpeedGrader. Aletheia never sets a grade, and
the request preserves any grade a teacher has already given. Nothing else is posted.
"""

import json
import mimetypes
import secrets
import time
from urllib.parse import urlencode

from django.core.cache import cache
from django.utils import timezone

from . import lti

SCORE_SCOPE = "https://purl.imsglobal.org/spec/lti-ags/scope/score"
CANVAS_SUBMISSION = "https://canvas.instructure.com/lti/submission"
SCORE_MEDIA_TYPE = "application/vnd.ims.lis.v1.score+json"


def can_submit(assignment):
    """True if this assignment's launches gave a gradebook column we may post scores to."""
    return bool(assignment.lineitem_url) and SCORE_SCOPE in (assignment.ags_scopes or [])


def scores_url(lineitem_url):
    base, sep, query = lineitem_url.partition("?")
    return base.rstrip("/") + "/scores" + sep + query


def _timestamp(moment=None):
    return (moment or timezone.now()).isoformat(timespec="microseconds")


def access_token(platform, scopes=(SCORE_SCOPE,)):
    """OAuth2 client-credentials token, signed with Aletheia's key, cached until shortly before expiry."""
    if not platform.access_token_url:
        raise lti.ServiceError(f"{platform} has no token URL.")
    scope = " ".join(sorted(scopes))
    key = f"aletheia:lms-token:{platform.pk}:{scope}"
    token = cache.get(key)
    if token:
        return token
    now = int(time.time())
    assertion = lti.sign({
        "iss": platform.client_id,
        "sub": platform.client_id,
        "aud": platform.access_token_url,
        "iat": now,
        "exp": now + 300,
        "jti": secrets.token_urlsafe(16),
    })
    body = urlencode({
        "grant_type": "client_credentials",
        "client_assertion_type": "urn:ietf:params:oauth:client-assertion-type:jwt-bearer",
        "client_assertion": assertion,
        "scope": scope,
    }).encode()
    data = lti.http_json(
        "POST",
        platform.access_token_url,
        body,
        {"Content-Type": "application/x-www-form-urlencoded", "Accept": "application/json"},
    )
    token = data.get("access_token")
    if not token:
        raise lti.ServiceError("The LMS did not return an access token.")
    cache.set(key, token, max(60, int(data.get("expires_in") or 3600) - 120))
    return token


def post_score(platform, lineitem_url, payload):
    token = access_token(platform)
    return lti.http_json(
        "POST",
        scores_url(lineitem_url),
        _json(payload),
        {"Authorization": f"Bearer {token}", "Content-Type": SCORE_MEDIA_TYPE, "Accept": "application/json"},
    )


def _json(payload):
    return json.dumps(payload).encode()


def submission_payload(attempt, file_url):
    """A new submission attempt holding the uploaded file, waiting for a teacher to mark it."""
    submitted = _timestamp(attempt.created_at)
    media_type = mimetypes.guess_type(attempt.original_filename)[0] or "application/octet-stream"
    return {
        "userId": attempt.lti_user_id,
        "activityProgress": "Submitted",
        "gradingProgress": "PendingManual",
        "timestamp": _timestamp(),
        "submission": {"submittedAt": submitted},
        CANVAS_SUBMISSION: {
            "new_submission": True,
            "preserve_score": True,
            "submission_type": "online_upload",
            "submitted_at": submitted,
            "content_items": [
                {"type": "file", "url": file_url, "title": attempt.original_filename, "media_type": media_type}
            ],
        },
    }
