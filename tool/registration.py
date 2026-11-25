"""LTI Dynamic Registration (as Canvas implements it).

An LMS admin enters Aletheia's registration URL. The LMS opens it with `openid_configuration`
and `registration_token`; Aletheia reads the platform's configuration, posts its own tool
configuration, receives a client ID, saves the platform and tells the LMS it has finished.
"""

import json
from urllib.parse import urlparse

from django.conf import settings

from . import ags, lti, nrps
from .models import Platform

TOOL_CONFIG = "https://purl.imsglobal.org/spec/lti-tool-configuration"
PLATFORM_CONFIG = "https://purl.imsglobal.org/spec/lti-platform-configuration"
CANVAS_PRIVACY = "https://canvas.instructure.com/lti/privacy_level"
CANVAS_ACCOUNT_NAME = "https://canvas.instructure.com/lti/account_name"


class RegistrationError(Exception):
    pass


def _secure(url):
    scheme = urlparse(url or "").scheme
    return scheme == "https" or (settings.DEBUG and scheme == "http")


def fetch_platform_config(config_url, token):
    if not _secure(config_url):
        raise RegistrationError("The LMS configuration address must use HTTPS.")
    headers = {"Accept": "application/json"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    try:
        config = lti.http_json("GET", config_url, headers=headers)
    except lti.ServiceError as exc:
        raise RegistrationError(f"Couldn't read the LMS configuration ({exc}).")
    required = ["issuer", "authorization_endpoint", "token_endpoint", "jwks_uri", "registration_endpoint"]
    missing = [key for key in required if not config.get(key)]
    if missing:
        raise RegistrationError(f"The LMS configuration is missing {', '.join(missing)}.")
    if not all(_secure(config[key]) for key in required[1:]):
        raise RegistrationError("The LMS endpoints must use HTTPS.")
    return config


def tool_configuration(urls, host):
    """What Aletheia asks the LMS for. `urls` holds absolute login, launch and jwks URLs."""
    return {
        "application_type": "web",
        "response_types": ["id_token"],
        "grant_types": ["implicit", "client_credentials"],
        "initiate_login_uri": urls["login"],
        "redirect_uris": [urls["launch"]],
        "client_name": "Aletheia",
        "logo_uri": urls["icon"],
        "jwks_uri": urls["jwks"],
        "token_endpoint_auth_method": "private_key_jwt",
        "scope": " ".join([ags.SCORE_SCOPE, nrps.MEMBERSHIP_SCOPE]),
        TOOL_CONFIG: {
            "domain": host,
            "target_link_uri": urls["launch"],
            "description": "Checks that students understand the work they submit, then sends the work to the gradebook for marking.",
            "claims": ["iss", "sub", "name", "given_name", "family_name", "email"],
            "messages": [
                {
                    "type": "LtiDeepLinkingRequest",
                    "target_link_uri": urls["launch"],
                    "label": "Aletheia",
                    "icon_uri": urls["icon"],
                    "placements": ["assignment_selection"],
                }
            ],
            CANVAS_PRIVACY: "public",
        },
    }


def register(config_url, token, urls, host):
    """Run the registration and return the new Platform."""
    config = fetch_platform_config(config_url, token)
    headers = {"Content-Type": "application/json", "Accept": "application/json"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    body = json.dumps(tool_configuration(urls, host)).encode()
    try:
        response = lti.http_json("POST", config["registration_endpoint"], body, headers)
    except lti.ServiceError as exc:
        raise RegistrationError(f"The LMS refused the registration ({exc}).")
    client_id = str(response.get("client_id") or "")
    if not client_id:
        raise RegistrationError("The LMS did not return a client ID.")
    if Platform.objects.filter(issuer=config["issuer"], client_id=client_id).exists():
        # Never let a registration overwrite an existing platform's keys or endpoints.
        raise RegistrationError("This LMS registration already exists in Aletheia.")
    platform_config = config.get(PLATFORM_CONFIG) or {}
    product = (platform_config.get("product_family_code") or "LMS").capitalize()
    account = platform_config.get(CANVAS_ACCOUNT_NAME) or urlparse(config_url).hostname
    return Platform.objects.create(
        name=f"{product}: {account}"[:120],
        issuer=config["issuer"],
        client_id=client_id,
        deployment_ids="",  # accept any deployment of this registration
        auth_login_url=config["authorization_endpoint"],
        jwks_url=config["jwks_uri"],
        access_token_url=config["token_endpoint"],
    )
