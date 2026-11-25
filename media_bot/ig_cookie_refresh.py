"""Check one existing Instagram login and return verified server cookie changes."""
import copy
import http.client
import json
import logging
import re
from http.cookiejar import CookieJar
from urllib.parse import unquote
from urllib.request import Request

HOST = "www.instagram.com"
PATH = "/api/v1/accounts/current_user/?edit=true"
MAX_RESPONSE = 512 * 1024
DOMAINS = {"instagram.com", "www.instagram.com"}


def _valid_cookie(cookie):
    return (cookie.domain.lstrip(".").lower() in DOMAINS
            and re.fullmatch(r"[A-Za-z0-9_!#$%&'*+.^`|~-]{1,128}", cookie.name)
            and isinstance(cookie.value, str) and len(cookie.value) <= 8192
            and all("!" <= ch <= "~" and ch != ";" for ch in cookie.value)
            and cookie.path.startswith("/")
            and all("!" <= ch <= "~" for ch in cookie.path))


def expected_identity(jar):
    """Use the imported session's numeric identity, including legacy sessionid-only imports."""
    identities = set()
    sessions = set()
    for cookie in jar:
        if (cookie.domain.lstrip(".").lower() not in DOMAINS
                or cookie.path != "/" or cookie.is_expired()):
            continue
        if cookie.name == "ds_user_id":
            identities.add(cookie.value)
        if cookie.name == "sessionid" and cookie.value:
            sessions.add(cookie.value)
            prefix = re.match(r"^([0-9]+):", unquote(cookie.value))
            if prefix:
                identities.add(prefix[1])
    if len(sessions) == 1 and len(identities) == 1:
        identity = identities.pop()
        if isinstance(identity, str) and re.fullmatch(r"[0-9]{1,30}", identity):
            return identity
    return ""


def _state(jar):
    return {(c.domain, c.path, c.name): (c.value, c.expires, c.secure, c.discard,
                                        c.has_nonstandard_attr("HttpOnly"))
            for c in jar}


def _failure(reason, status=0):
    # Supply only fixed reason codes and numeric HTTP status, never response text or headers.
    logging.getLogger(__name__).warning("IG session check reason=%s http=%d", reason, status)
    return "error", None


def check(cookies, expected_user_id):
    """One fixed-origin GET. Never follow redirects, log credentials, or modify the input."""
    if not expected_user_id or expected_identity(cookies) != expected_user_id:
        return _failure("input_identity")
    jar = CookieJar()
    for cookie in cookies:
        if not _valid_cookie(cookie):
            return _failure("invalid_input_cookie")
        jar.set_cookie(copy.deepcopy(cookie))
    request = Request("https://" + HOST + PATH, headers={
        "User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/130.0.0.0 Safari/537.36",
        "Accept": "application/json",
        "X-IG-App-ID": "936619743392459",
        "Referer": "https://www.instagram.com/",
    })
    jar.add_cookie_header(request)
    before = _state(jar)
    connection = http.client.HTTPSConnection(HOST, timeout=20)
    try:
        connection.request("GET", PATH, headers=dict(request.header_items()))
        with connection.getresponse() as response:
            if response.status == 429:
                return "rate_limited", None
            if 300 <= response.status < 400:
                location = response.getheader("Location", "")
                if re.match(r"^(?:https://www\.instagram\.com)?/(?:accounts/login|challenge|checkpoint)(?:/|\?|$)", location):
                    return "login_required", None
                return _failure("unexpected_redirect", response.status)
            if response.status not in (200, 400, 401, 403):
                return _failure("http_status", response.status)
            body = response.read(MAX_RESPONSE + 1)
            if len(body) > MAX_RESPONSE:
                return _failure("body_too_large", response.status)
            try:
                data = json.loads(body)
            except (ValueError, RecursionError):
                return _failure("invalid_json", response.status)
            if not isinstance(data, dict):
                return _failure("unexpected_json", response.status)
            message = str(data.get("message", "")).lower()
            if ("please wait a few minutes" in message or "rate_limit" in message
                    or "too many requests" in message or "rate limit" in message
                    or "feedback_required" in message or data.get("error_type") == "rate_limit_error"):
                return "rate_limited", None
            if (response.status == 401 or any(data.get(k) for k in ("login_required", "checkpoint_required", "challenge", "checkpoint_url"))
                    or message in ("login_required", "checkpoint_required", "challenge_required")):
                return "login_required", None
            user = data.get("user")
            if (response.status != 200 or data.get("status") != "ok" or not isinstance(user, dict)
                    or str(user.get("pk")) != expected_user_id):
                return _failure("account_not_confirmed", response.status)
            jar.extract_cookies(response, request)
        if (expected_identity(jar) != expected_user_id or len(jar) > 128
                or not all(_valid_cookie(c) for c in jar)):
            return _failure("invalid_result_cookie")
        if before == _state(jar):
            return "unchanged", None
        return "updated", jar
    except (OSError, http.client.HTTPException, ValueError, RecursionError):
        return _failure("transport_or_decode")
    finally:
        connection.close()
