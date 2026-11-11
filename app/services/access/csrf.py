"""Dashboard CSRF tokens (v1.6.1 plan D24, D25, feature seven).

- Signed-in requests: ``token = HMAC-SHA256(csrf_secret, session_id)``. ``csrf_secret`` is a per-session random value
  stored on ``dashboard_sessions`` (P1), so a token is bound to one session: another session's token, or the token
  of a session that was signed out (revoked) and replaced, never verifies.
- The sign-in form has no session yet. It uses a short-lived signed cookie ``rh_login_csrf`` (``nonce.issued.sig``)
  plus a form token derived from the same nonce (signed double submit). The signing key is
  ``DASHBOARD_CSRF_SECRET`` or, when that is empty, a random per-process key (a sign-in form rendered before an API
  restart, or by another replica, then fails once and is shown again with a fresh token).

Every comparison uses ``hmac.compare_digest``. Nothing here touches the database or the HTTP layer.
"""

from __future__ import annotations

import hashlib
import hmac
import secrets
import time

FIELD_NAME = "_csrf"
HEADER_NAME = "X-CSRF-Token"
LOGIN_COOKIE_NAME = "rh_login_csrf"
LOGIN_COOKIE_PATH = "/dashboard/login"
LOGIN_TOKEN_TTL_SECONDS = 900

_PROCESS_KEY = secrets.token_bytes(32)


def session_token(csrf_secret: str | None, session_id: str | None) -> str:
    """The token every form of a signed-in session carries (empty when the session has no secret)."""
    if not csrf_secret or not session_id:
        return ""
    return hmac.new(csrf_secret.encode("utf-8"), session_id.encode("utf-8"), hashlib.sha256).hexdigest()


def verify_session_token(csrf_secret: str | None, session_id: str | None, supplied: str | None) -> bool:
    expected = session_token(csrf_secret, session_id)
    if not expected or not supplied:
        return False
    return hmac.compare_digest(expected.encode("utf-8"), str(supplied).encode("utf-8"))


def _login_key(secret: str | None) -> bytes:
    return secret.encode("utf-8") if secret else _PROCESS_KEY


def _sign(key: bytes, purpose: str, *parts: str) -> str:
    message = "|".join((purpose, *parts)).encode("utf-8")
    return hmac.new(key, message, hashlib.sha256).hexdigest()


def new_login_token(secret: str | None = None, *, now: float | None = None) -> tuple[str, str]:
    """``(cookie_value, form_token)`` for one rendering of the sign-in form."""
    key = _login_key(secret)
    nonce = secrets.token_urlsafe(24)
    issued = str(int(now if now is not None else time.time()))
    cookie = f"{nonce}.{issued}.{_sign(key, 'login-cookie', nonce, issued)}"
    return cookie, _sign(key, "login-form", nonce)


def verify_login_token(
    cookie_value: str | None, supplied: str | None, secret: str | None = None, *, now: float | None = None
) -> bool:
    """The cookie must be ours (signature), younger than ``LOGIN_TOKEN_TTL_SECONDS`` and match the form token."""
    if not cookie_value or not supplied:
        return False
    parts = cookie_value.split(".")
    if len(parts) != 3:
        return False
    nonce, issued, signature = parts
    key = _login_key(secret)
    if not hmac.compare_digest(_sign(key, "login-cookie", nonce, issued).encode("utf-8"), signature.encode("utf-8")):
        return False
    try:
        age = (now if now is not None else time.time()) - int(issued)
    except ValueError:
        return False
    if age < 0 or age > LOGIN_TOKEN_TTL_SECONDS:
        return False
    return hmac.compare_digest(_sign(key, "login-form", nonce).encode("utf-8"), str(supplied).encode("utf-8"))
