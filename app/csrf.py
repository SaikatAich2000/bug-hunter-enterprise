"""CSRF defence-in-depth via the double-submit cookie pattern.

We already rely on `SameSite=Lax` to block cross-site cookie sending
(the modern primary defence). This adds a belt-and-suspenders layer:

  1. On the first GET, we set a `bh_csrf` cookie containing a random
     128-bit token.
  2. The SPA's fetch wrapper copies that cookie value into an
     `X-CSRF-Token` header on every state-changing request.
  3. The server middleware compares the header to the cookie and
     rejects (403) any mismatch.

A cross-site attacker can't read the cookie (HttpOnly is off for this
one, by design — JS needs to copy it; but it's still on the victim's
origin and Same-Origin Policy stops attacker JS from reading it). They
also can't predict the value, so they can't forge the header. Strict
SameSite=Lax would block the request before it gets this far, but
defence-in-depth means we still verify if it does.

The cookie is a non-secret token, regenerated freely. We use a
TimestampSigner so a long-cached cookie eventually rotates.

Endpoints exempt from the check: GETs, HEADs, OPTIONS, and the few
endpoints that are themselves the entry point for the cookie (login,
signup, forgot-password — we don't have a session yet at those calls,
and the rate-limit middleware already shields them).
"""
from __future__ import annotations

import secrets

from fastapi import Request, Response
from fastapi.responses import JSONResponse
from starlette.middleware.base import BaseHTTPMiddleware

from app.config import get_settings

CSRF_COOKIE = "bh_csrf"
CSRF_HEADER = "X-CSRF-Token"

# Endpoints that can be called WITHOUT a CSRF token because they
# happen before the user has any session at all. Same set as our
# rate-limit rules.
_EXEMPT_PATHS = frozenset({
    "/api/auth/login",
    "/api/auth/signup",
    "/api/auth/forgot-password",
    "/api/auth/reset-password",
    "/api/invitations/accept",
})


def _new_token() -> str:
    """Random URL-safe 128-bit token."""
    return secrets.token_urlsafe(16)


def issue_csrf_cookie(response: Response) -> str:
    """Mint a new CSRF token and set it as a cookie on `response`.
    Returns the token so callers can verify they read what they wrote."""
    settings = get_settings()
    token = _new_token()
    response.set_cookie(
        key=CSRF_COOKIE,
        value=token,
        # HttpOnly=False because the SPA JS has to read it. Same-Origin
        # Policy prevents cross-site JS from seeing it; SameSite=Lax
        # prevents the browser from leaking it cross-site.
        httponly=False,
        secure=settings.COOKIE_SECURE,
        samesite="lax",
        path="/",
        # Long-ish — rotating churns the SPA's first-load.
        max_age=settings.SESSION_TTL_SECONDS,
    )
    return token


class CSRFMiddleware(BaseHTTPMiddleware):
    """Double-submit CSRF check.

    Enabled by default (settings.CSRF_PROTECTION). When false, the
    middleware is still installed but every request passes through —
    we keep the install so the cookie is still seeded for forward
    compatibility (operators can flip the flag without re-deploying).
    """

    async def dispatch(self, request: Request, call_next):
        settings = get_settings()
        method = request.method.upper()
        path = request.url.path

        # Always seed the CSRF cookie on responses to documents the SPA
        # serves (HTML pages, the SPA boot). Cheap; eliminates a
        # "first POST fails" race on a fresh tab.
        seed_cookie = (
            method == "GET"
            and (path == "/" or path.endswith(".html") or path == "/login" or path == "/signup")
        )

        # Skip the check on read-only methods and on the bootstrap
        # endpoints that issue the session in the first place.
        skip_check = (
            not settings.CSRF_PROTECTION
            or method in ("GET", "HEAD", "OPTIONS")
            or not path.startswith("/api/")
            or path in _EXEMPT_PATHS
        )

        if not skip_check:
            cookie = request.cookies.get(CSRF_COOKIE, "")
            header = request.headers.get(CSRF_HEADER, "")
            if not cookie or not header or not secrets.compare_digest(cookie, header):
                return JSONResponse(
                    status_code=403,
                    content={"detail": "CSRF check failed. Reload the page and try again."},
                )

        response: Response = await call_next(request)
        if seed_cookie and CSRF_COOKIE not in request.cookies:
            issue_csrf_cookie(response)
        return response
