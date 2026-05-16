"""FastAPI entry point — Bug Hunter v4 (multi-tenant)."""
from __future__ import annotations

import hashlib
import logging
import time
from collections import deque
from contextlib import asynccontextmanager
from pathlib import Path
from threading import Lock

from datetime import datetime, timezone

from fastapi import FastAPI, HTTPException, Request, Response
from fastapi.middleware.cors import CORSMiddleware
from fastapi.middleware.gzip import GZipMiddleware
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from sqlalchemy import select
from starlette.middleware.base import BaseHTTPMiddleware

from app.auth import COOKIE_NAME, parse_session_token
from app.config import get_settings
from app.database import SessionLocal, init_db
from app.models import Session as SessionRow
from app.chatbot.router import router as chatbot_router
from app.routes import (
    audit, auth, bugs, invitations, memberships, organizations,
    projects, sessions, stats, users,
)
from app.schemas import (
    ALLOWED_ENVIRONMENTS,
    ALLOWED_PRIORITIES,
    ALLOWED_STATUSES,
)

logger = logging.getLogger("bug_hunter")
logging.basicConfig(level=get_settings().LOG_LEVEL)


# ---------------------------------------------------------------------------
# Asset version — recomputed on every server start.
# ---------------------------------------------------------------------------
ASSET_VERSION_PLACEHOLDER = "__ASSET_VERSION__"


def _compute_asset_version(static_dir: Path) -> str:
    h = hashlib.sha256()
    if not static_dir.exists():
        return "dev"
    for path in sorted(static_dir.rglob("*")):
        if path.is_file() and not path.name.startswith("."):
            try:
                h.update(path.relative_to(static_dir).as_posix().encode("utf-8"))
                h.update(b"|")
                h.update(path.read_bytes())
            except OSError:
                continue
    return h.hexdigest()[:12]


@asynccontextmanager
async def lifespan(app: FastAPI):
    init_db()

    if not get_settings().SESSION_SECRET:
        logger.warning(
            "SESSION_SECRET is not set. Using a random per-process fallback. "
            "Set SESSION_SECRET in your environment for stable sessions across "
            "restarts and multi-worker deployments."
        )

    logger.info("Bug Hunter v4 started. asset_version=%s", app.state.asset_version)
    yield
    logger.info("Bug Hunter shutting down.")


settings = get_settings()
app = FastAPI(
    title=settings.APP_NAME,
    version=settings.APP_VERSION,
    lifespan=lifespan,
)
app.state.asset_version = _compute_asset_version(settings.STATIC_DIR)


# ---------------------------------------------------------------------------
# CORS
# ---------------------------------------------------------------------------
_origins = settings.CORS_ORIGINS or ["*"]
_allow_credentials = True
if _origins == ["*"]:
    _allow_credentials = False
    logger.warning(
        "CORS_ORIGINS='*' is incompatible with credentials. Set CORS_ORIGINS to "
        "your concrete origin(s) for cross-origin browser sessions."
    )

app.add_middleware(
    CORSMiddleware,
    allow_origins=_origins,
    allow_credentials=_allow_credentials,
    allow_methods=["*"],
    allow_headers=["*"],
)

app.add_middleware(GZipMiddleware, minimum_size=1024, compresslevel=5)


# ---------------------------------------------------------------------------
# Cache-Control + security headers middleware
# ---------------------------------------------------------------------------
class CacheControlMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next):
        response: Response = await call_next(request)
        path = request.url.path

        # Security headers on every response. CSP is strict — same-origin
        # scripts/styles only, no inline. Login.js etc. are external files
        # served from /static which satisfies 'self'.
        if "Content-Security-Policy" not in response.headers:
            response.headers["Content-Security-Policy"] = (
                "default-src 'self'; "
                "script-src 'self'; "
                "style-src 'self' 'unsafe-inline'; "
                "img-src 'self' data: blob:; "
                "font-src 'self' data:; "
                "connect-src 'self'; "
                "frame-ancestors 'none'; "
                "base-uri 'self'; "
                "form-action 'self'"
            )
        response.headers.setdefault("X-Content-Type-Options", "nosniff")
        response.headers.setdefault("X-Frame-Options", "DENY")
        response.headers.setdefault("Referrer-Policy", "strict-origin-when-cross-origin")
        response.headers.setdefault(
            "Permissions-Policy",
            "geolocation=(), microphone=(), camera=()",
        )
        if settings.COOKIE_SECURE:
            response.headers.setdefault(
                "Strict-Transport-Security",
                "max-age=31536000; includeSubDomains",
            )

        # Don't override Cache-Control if the route already set one
        # (attachment downloads etc.).
        if response.headers.get("Cache-Control"):
            return response

        if path.startswith("/static/"):
            response.headers["Cache-Control"] = "public, max-age=31536000, immutable"
        elif path.startswith("/api/"):
            response.headers["Cache-Control"] = "no-store"
        else:
            response.headers["Cache-Control"] = "no-store, must-revalidate"
        return response


app.add_middleware(CacheControlMiddleware)


# ---------------------------------------------------------------------------
# Rate limiting
# ---------------------------------------------------------------------------
_RATE_RULES: dict[str, tuple[int, int]] = {
    "/api/auth/login": (8, 60),
    "/api/auth/signup": (5, 600),               # 5 signups / 10 min per IP
    "/api/auth/forgot-password": (3, 60),
    "/api/auth/email-change/request": (10, 600), # 10 / 10 min per IP
    "/api/auth/email-change/confirm": (20, 60),
    "/api/invitations/accept": (10, 600),
}
_rate_buckets: dict[tuple[str, str], deque] = {}
_rate_lock = Lock()
_RATE_BUCKETS_MAX = 10_000


def _client_ip(request: Request) -> str:
    if settings.TRUST_PROXY_FORWARDED_FOR:
        xff = request.headers.get("x-forwarded-for")
        if xff:
            return xff.split(",")[0].strip()
    return request.client.host if request.client else "unknown"


class RateLimitMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next):
        path = request.url.path
        rule = _RATE_RULES.get(path)
        if rule is None or request.method.upper() != "POST":
            return await call_next(request)

        max_req, window = rule
        ip = _client_ip(request)
        now = time.monotonic()
        cutoff = now - window

        with _rate_lock:
            bucket = _rate_buckets.get((path, ip))
            if bucket is None:
                if len(_rate_buckets) >= _RATE_BUCKETS_MAX:
                    _rate_buckets.pop(next(iter(_rate_buckets)), None)
                bucket = deque()
                _rate_buckets[(path, ip)] = bucket
            while bucket and bucket[0] < cutoff:
                bucket.popleft()
            if len(bucket) >= max_req:
                retry_after = max(1, int(window - (now - bucket[0])))
                logger.warning(
                    "Rate limit hit: %s from %s (%d/%d in %ss)",
                    path, ip, len(bucket), max_req, window,
                )
                return JSONResponse(
                    status_code=429,
                    content={"detail": "Too many attempts. Please try again later."},
                    headers={"Retry-After": str(retry_after)},
                )
            bucket.append(now)
        return await call_next(request)


app.add_middleware(RateLimitMiddleware)


app.mount("/static", StaticFiles(directory=settings.STATIC_DIR), name="static")


def _serve_html(filename: str) -> HTMLResponse:
    body = (settings.STATIC_DIR / filename).read_text(encoding="utf-8")
    body = body.replace(ASSET_VERSION_PLACEHOLDER, app.state.asset_version)
    return HTMLResponse(body)


def _has_valid_session(request: Request) -> bool:
    """Used by HTML routes to decide whether to redirect to /login. The
    SPA's API calls do their own check via _user_from_request."""
    token = request.cookies.get(COOKIE_NAME, "")
    parsed = parse_session_token(token)
    if parsed is None:
        return False
    user_id, _session_version, jti = parsed
    if jti is None:
        return True
    db = SessionLocal()
    try:
        sess = db.scalar(select(SessionRow).where(SessionRow.jti == jti))
        if sess is None or sess.user_id != user_id:
            return False
        expires = sess.expires_at
        if expires.tzinfo is None:
            expires = expires.replace(tzinfo=timezone.utc)
        return expires >= datetime.now(timezone.utc)
    finally:
        db.close()


# ---------------------------------------------------------------------------
# HTML page routes
# ---------------------------------------------------------------------------
@app.get("/", response_class=HTMLResponse, include_in_schema=False)
def home(request: Request):
    if not _has_valid_session(request):
        return RedirectResponse(url="/login.html", status_code=302)
    return _serve_html("index.html")


@app.get("/login.html", response_class=HTMLResponse, include_in_schema=False)
@app.get("/login", response_class=HTMLResponse, include_in_schema=False)
def login_page(request: Request):
    if _has_valid_session(request):
        return RedirectResponse(url="/", status_code=302)
    return _serve_html("login.html")


@app.get("/signup.html", response_class=HTMLResponse, include_in_schema=False)
@app.get("/signup", response_class=HTMLResponse, include_in_schema=False)
def signup_page(request: Request):
    if _has_valid_session(request):
        return RedirectResponse(url="/", status_code=302)
    return _serve_html("signup.html")


@app.get("/accept-invite.html", response_class=HTMLResponse, include_in_schema=False)
@app.get("/accept-invite", response_class=HTMLResponse, include_in_schema=False)
def accept_invite_page() -> HTMLResponse:
    # Reachable even when logged in — if a user is already authenticated
    # and clicks an invite link, the page will show a helpful "log out
    # first to accept" message.
    return _serve_html("accept-invite.html")


@app.get("/reset.html", response_class=HTMLResponse, include_in_schema=False)
@app.get("/reset", response_class=HTMLResponse, include_in_schema=False)
def reset_page() -> HTMLResponse:
    return _serve_html("reset.html")


# ---------------------------------------------------------------------------
# Meta endpoints
# ---------------------------------------------------------------------------
@app.get("/api/health", tags=["meta"])
def health() -> dict[str, str]:
    return {
        "status": "ok",
        "version": settings.APP_VERSION,
        "asset_version": app.state.asset_version,
    }


@app.get("/api/meta", tags=["meta"])
def meta() -> dict[str, object]:
    return {
        "statuses": ALLOWED_STATUSES,
        "priorities": ALLOWED_PRIORITIES,
        "environments": ALLOWED_ENVIRONMENTS,
        "allow_public_signup": settings.ALLOW_PUBLIC_SIGNUP,
    }


# Routers
app.include_router(auth.router)
app.include_router(organizations.router)
app.include_router(invitations.router)
app.include_router(users.router)
app.include_router(projects.router)
app.include_router(memberships.router)
app.include_router(bugs.router)
app.include_router(stats.router)
app.include_router(audit.router)
app.include_router(sessions.router)
app.include_router(chatbot_router)


@app.exception_handler(HTTPException)
async def http_exc_handler(request: Request, exc: HTTPException) -> JSONResponse:
    return JSONResponse(status_code=exc.status_code, content={"detail": exc.detail})


if __name__ == "__main__":
    import uvicorn
    uvicorn.run("app.main:app", host="0.0.0.0", port=8000, reload=False)
