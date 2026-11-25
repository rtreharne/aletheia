"""Cookies that keep working when Aletheia is inside an LMS's iframe.

Browsers block ordinary third-party cookies in iframes, but allow "partitioned" ones (CHIPS): a separate
cookie jar for each site the iframe is embedded in. Every SameSite=None cookie (the session and CSRF
cookies, in production) is marked Partitioned. Python's cookie module only learns that attribute in 3.14.
"""

from http import cookies

cookies.Morsel._reserved.setdefault("partitioned", "Partitioned")
cookies.Morsel._flags.add("partitioned")


class PartitionedCookiesMiddleware:
    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        response = self.get_response(request)
        for morsel in response.cookies.values():
            if str(morsel.get("samesite", "")).lower() == "none" and morsel.get("secure"):
                morsel["partitioned"] = True
        return response
