"""LTI 1.3 core: OIDC login, launch validation, deep linking and tool keys."""

import json
import secrets
import time
import urllib.error
import urllib.request
from functools import lru_cache

import jwt
from django.conf import settings
from jwcrypto import jwk

from .models import Platform

CLAIM = "https://purl.imsglobal.org/spec/lti/claim/"
DL_CLAIM = "https://purl.imsglobal.org/spec/lti-dl/claim/"

AGS_CLAIM = "https://purl.imsglobal.org/spec/lti-ags/claim/endpoint"
NRPS_CLAIM = "https://purl.imsglobal.org/spec/lti-nrps/claim/namesroleservice"
INSTRUCTOR_ROLE_MARKERS = ("Instructor", "TeachingAssistant", "ContentDeveloper", "Administrator")
# Observers (in Canvas, parents and other "observer" enrolments) are sent as Mentor.
# They get nothing from Aletheia.
OBSERVER_ROLE_MARKERS = ("Mentor",)
HTTP_TIMEOUT = 20
# Python's default "Python-urllib/x.y" is refused by some firewalls (e.g. Cloudflare's
# Browser Integrity Check, error 1010), so name ourselves.
USER_AGENT = "Aletheia-LTI/1.0"

MAX_PENDING_LOGINS = 20


class LaunchError(Exception):
    pass


class ServiceError(Exception):
    """A call from Aletheia to the LMS failed."""


def http_json(method, url, data=None, headers=None, with_headers=False):
    """Make a request and return the decoded JSON body (or {} if empty).

    With `with_headers`, return (body, response headers) instead, e.g. to follow Link paging.
    """
    request = urllib.request.Request(url, data=data, method=method, headers={"User-Agent": USER_AGENT, **(headers or {})})
    try:
        with urllib.request.urlopen(request, timeout=HTTP_TIMEOUT) as response:
            body = response.read()
            response_headers = dict(response.headers.items())
    except urllib.error.HTTPError as exc:
        detail = exc.read()[:500].decode("utf-8", "replace")
        raise ServiceError(f"{method} {url} returned {exc.code}: {detail}")
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        raise ServiceError(f"{method} {url} failed: {exc}")
    try:
        data = json.loads(body) if body.strip() else {}
    except json.JSONDecodeError:
        raise ServiceError(f"{method} {url} did not return JSON.")
    return (data, response_headers) if with_headers else data


# ---------------------------------------------------------------- tool keys

def generate_keys(force=False):
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric import rsa

    priv_path, pub_path = settings.LTI_PRIVATE_KEY_PATH, settings.LTI_PUBLIC_KEY_PATH
    if priv_path.exists() and pub_path.exists() and not force:
        return False
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    priv_path.parent.mkdir(parents=True, exist_ok=True)
    pub_path.parent.mkdir(parents=True, exist_ok=True)
    priv_path.write_bytes(
        key.private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.PKCS8,
            serialization.NoEncryption(),
        )
    )
    pub_path.write_bytes(
        key.public_key().public_bytes(
            serialization.Encoding.PEM,
            serialization.PublicFormat.SubjectPublicKeyInfo,
        )
    )
    _public_jwk.cache_clear()
    return True


@lru_cache(maxsize=1)
def _public_jwk():
    key = jwk.JWK.from_pem(settings.LTI_PUBLIC_KEY_PATH.read_bytes())
    data = json.loads(key.export_public())
    data["kid"] = key.thumbprint()
    data.update({"alg": "RS256", "use": "sig"})
    return data


def public_jwks():
    return {"keys": [_public_jwk()]}


def sign(payload):
    return jwt.encode(
        payload,
        settings.LTI_PRIVATE_KEY_PATH.read_bytes(),
        algorithm="RS256",
        headers={"kid": _public_jwk()["kid"]},
    )


# ---------------------------------------------------------------- login

def find_platform(issuer, client_id=None):
    platforms = Platform.objects.filter(issuer=issuer)
    if client_id:
        platforms = platforms.filter(client_id=client_id)
    platforms = list(platforms[:2])
    if len(platforms) != 1:
        raise LaunchError(
            "This LMS is not registered with Aletheia." if not platforms
            else "Several registrations match this LMS; it must send client_id."
        )
    return platforms[0]


def begin_login(session, params, redirect_uri):
    """Handle OIDC login initiation; return the platform authorisation URL params."""
    issuer = params.get("iss")
    login_hint = params.get("login_hint")
    if not issuer or not login_hint:
        raise LaunchError("Missing login parameters.")
    platform = find_platform(issuer, params.get("client_id"))

    state = secrets.token_urlsafe(32)
    nonce = secrets.token_urlsafe(32)
    pending = session.get("lti_pending", {})
    pending[state] = {"nonce": nonce, "platform": platform.pk, "at": int(time.time())}
    # Keep the most recent few logins only.
    session["lti_pending"] = dict(sorted(pending.items(), key=lambda kv: kv[1]["at"])[-MAX_PENDING_LOGINS:])

    query = {
        "scope": "openid",
        "response_type": "id_token",
        "response_mode": "form_post",
        "prompt": "none",
        "client_id": platform.client_id,
        "redirect_uri": redirect_uri,
        "login_hint": login_hint,
        "state": state,
        "nonce": nonce,
    }
    if params.get("lti_message_hint"):
        query["lti_message_hint"] = params["lti_message_hint"]
    return platform.auth_login_url, query


# ---------------------------------------------------------------- launch

def _platform_signing_key(platform, id_token):
    client = jwt.PyJWKClient(platform.jwks_url, cache_keys=True, timeout=10, headers={"User-Agent": USER_AGENT})
    return client.get_signing_key_from_jwt(id_token).key


def validate_launch(session, id_token, state):
    """Validate an id_token from the platform and return (platform, claims)."""
    pending = session.get("lti_pending", {})
    login = pending.pop(state or "", None)
    session["lti_pending"] = pending
    if not id_token or not login:
        raise LaunchError("This launch has expired or its cookies were blocked. Please relaunch from your course.")

    platform = Platform.objects.filter(pk=login["platform"]).first()
    if not platform:
        raise LaunchError("This LMS is not registered with Aletheia.")

    try:
        claims = jwt.decode(
            id_token,
            _platform_signing_key(platform, id_token),
            algorithms=["RS256"],
            audience=platform.client_id,
            issuer=platform.issuer,
            leeway=60,
            options={"require": ["exp", "iat", "iss", "aud", "sub"]},
        )
    except jwt.PyJWTError as exc:
        raise LaunchError(f"The launch could not be verified ({exc}).")

    if claims.get("nonce") != login["nonce"]:
        raise LaunchError("The launch nonce did not match.")
    aud = claims.get("aud")
    if isinstance(aud, list) and len(aud) > 1 and claims.get("azp") != platform.client_id:
        raise LaunchError("The launch was not addressed to Aletheia.")
    if claims.get(CLAIM + "version") != "1.3.0":
        raise LaunchError("Only LTI 1.3 launches are supported.")
    if not platform.accepts_deployment(claims.get(CLAIM + "deployment_id", "")):
        raise LaunchError("This deployment is not registered with Aletheia.")
    return platform, claims


def _roles(claims):
    return [str(role) for role in (claims.get(CLAIM + "roles") or [])]


def _course_roles(claims):
    """Roles in this course only. Platforms (Canvas among them) also send institution roles, so
    someone who teaches elsewhere would otherwise look like staff in a course they study in."""
    return [
        role.rsplit("#", 1)[-1]
        for role in _roles(claims)
        if "/membership" in role or ("/" not in role and "#" not in role)
    ]


def is_instructor(claims):
    if any(marker in role for role in _course_roles(claims) for marker in INSTRUCTOR_ROLE_MARKERS):
        return True
    # Institution and system administrators may open any course.
    return any(
        role.endswith(("institution/person#Administrator", "system/person#Administrator", "system/person#SysAdmin"))
        for role in _roles(claims)
    )


def is_student(claims):
    """A learner in this course who is not also an observer. Anyone else who isn't staff is turned away."""
    roles = _course_roles(claims)
    if any(marker in role for role in roles for marker in OBSERVER_ROLE_MARKERS):
        return False
    return "Learner" in roles


def surname_first(full_name="", given="", family=""):
    """'Lovelace, Ada' for listing and sorting by surname.

    Uses separate given and family names when the LMS sends them; otherwise treats the last
    word of the full name as the surname. Names already written 'Surname, Given' are kept.
    """
    given, family, full_name = (given or "").strip(), (family or "").strip(), " ".join((full_name or "").split())
    if family and given:
        return f"{family}, {given}"
    if "," in full_name:
        return full_name
    parts = full_name.split(" ")
    if len(parts) > 1:
        return f"{parts[-1]}, {' '.join(parts[:-1])}"
    return full_name or family or given


def display_name(claims):
    """The student's name as shown to instructors: 'Surname, Given'."""
    name = surname_first(claims.get("name"), claims.get("given_name"), claims.get("family_name"))
    return name or claims.get("email") or "Student"


# ---------------------------------------------------------------- deep linking

def deep_link_response(platform, deep_link, launch_url, title):
    now = int(time.time())
    payload = {
        "iss": platform.client_id,
        "aud": platform.issuer,
        "iat": now,
        "exp": now + 300,
        "nonce": secrets.token_urlsafe(16),
        CLAIM + "message_type": "LtiDeepLinkingResponse",
        CLAIM + "version": "1.3.0",
        CLAIM + "deployment_id": deep_link["deployment_id"],
        DL_CLAIM + "content_items": [
            {"type": "ltiResourceLink", "title": title, "url": launch_url}
        ],
    }
    if deep_link.get("data"):
        payload[DL_CLAIM + "data"] = deep_link["data"]
    return sign(payload)
