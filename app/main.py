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

from app.auth import COOKIE_NAME, hash_password, parse_session_token
from app.config import get_settings
from app.csrf import CSRFMiddleware
from app.database import SessionLocal, init_db
from app.models import (
    Activity, Organization, ROLE_ADMIN, Session as SessionRow, User,
)
from app.observability import (
    ObservabilityMiddleware, configure_logging, render_prometheus,
)
from app.chatbot.router import router as chatbot_router
from app.routes import (
    audit, auth, bugs, events, invitations, memberships, organizations,
    projects, sessions, stats, users,
    webhooks as webhooks_route,
    saved_views as saved_views_route,
    branding as branding_route,
    custom_fields as custom_fields_route,
    dsar as dsar_route,
    totp as totp_route,
)
from app.schemas import (
    ALLOWED_ENVIRONMENTS,
    ALLOWED_PRIORITIES,
    ALLOWED_STATUSES,
)

# Configure logging EARLY so subsequent imports use the right format.
configure_logging(
    json_logging=get_settings().JSON_LOGGING,
    level=get_settings().LOG_LEVEL,
)
logger = logging.getLogger("bug_hunter")


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


async def _audit_retention_loop(retention_days: int):
    """Background sweep that deletes activity_log rows older than the
    configured retention window. Runs on startup, then every 24h.

    We swallow exceptions so a transient DB issue doesn't kill the
    sweep loop — operators will see retries via the access log.
    """
    import asyncio
    from datetime import timedelta

    while True:
        try:
            cutoff = datetime.now(timezone.utc) - timedelta(days=retention_days)
            db = SessionLocal()
            try:
                deleted = db.execute(
                    Activity.__table__.delete().where(Activity.created_at < cutoff)
                ).rowcount
                db.commit()
                if deleted:
                    logger.info(
                        "audit retention: purged %d rows older than %d days",
                        deleted, retention_days,
                    )
            finally:
                db.close()
        except Exception:
            logger.exception("audit retention sweep failed")
        await asyncio.sleep(24 * 3600)


_SLUG_BAD = __import__("re").compile(r"[^a-z0-9]+")


def _bootstrap_admin() -> None:
    """First-run bootstrap of an organization + admin user.

    Three outcomes depending on the database state:

      1. No user with `BOOTSTRAP_ADMIN_EMAIL` exists → create the org
         (reusing one with the bootstrap name if present) AND create
         the admin user with the configured password.
      2. The user exists AND `BOOTSTRAP_ADMIN_RESET_PASSWORD=true` →
         RESET that user's password to `BOOTSTRAP_ADMIN_PASSWORD` and
         re-activate them, and bump session_version (which invalidates
         existing sessions). The user is logged in via the env-var
         password on the next request. Logs a loud warning so this
         doesn't go unnoticed.
      3. The user exists AND `BOOTSTRAP_ADMIN_RESET_PASSWORD=false`
         (the safe default) → leave the user untouched. Log a clear
         info-level line so operators can see why the bootstrap is a
         no-op (e.g. "I changed BOOTSTRAP_ADMIN_PASSWORD but can't
         log in" → this log tells them to set the reset flag).

    Outcome 2 is the escape hatch for the common deployment situation
    where the prod DB already had a user with this email (from an
    earlier deployment, a manual signup, or a previous bootstrap with
    a different password) and the operator is locked out. After
    logging in with the new password, the operator should:

      a. Change the password via the Account panel.
      b. Flip `BOOTSTRAP_ADMIN_RESET_PASSWORD` back to false (or remove
         it) so the next redeploy doesn't keep stomping the password.

    No-op behaviour is guaranteed in both safe paths:
      - Returns immediately if email or password is empty.
      - Never modifies an existing user when the reset flag is off.
      - Never creates a duplicate user / org.
    """
    s = get_settings()
    if not s.BOOTSTRAP_ADMIN_EMAIL or not s.BOOTSTRAP_ADMIN_PASSWORD:
        return

    email = s.BOOTSTRAP_ADMIN_EMAIL.strip().lower()
    if not email:
        return

    db = SessionLocal()
    try:
        from sqlalchemy import select as _sel
        existing = db.scalar(_sel(User).where(User.email == email))
        if existing is not None:
            if s.BOOTSTRAP_ADMIN_RESET_PASSWORD:
                existing.password_hash = hash_password(s.BOOTSTRAP_ADMIN_PASSWORD)
                # Reactivate in case the account was disabled. Promote
                # to admin if downgraded — the env-var bootstrap is
                # meant for an admin so we should restore that role.
                existing.is_active = True
                existing.role = ROLE_ADMIN
                # Bump session_version so any cached cookies fail
                # validation. The operator gets a fresh session via
                # the env-var password.
                existing.session_version = (existing.session_version or 0) + 1
                db.commit()
                logger.warning(
                    "Bootstrap: RESET password for existing admin %s "
                    "(BOOTSTRAP_ADMIN_RESET_PASSWORD=true). "
                    "Log in with the env-var password, change it, "
                    "then unset the reset flag.",
                    email,
                )
            else:
                logger.info(
                    "Bootstrap: user %s already exists; leaving untouched. "
                    "Set BOOTSTRAP_ADMIN_RESET_PASSWORD=true and redeploy "
                    "if you need to reset the password.",
                    email,
                )
            return

        # Reuse the bootstrap org if it already exists by name, otherwise
        # create a fresh one. Reusing makes re-bootstrapping after a
        # manual user-row deletion work without piling up empty orgs.
        org = db.scalar(
            _sel(Organization).where(Organization.name == s.BOOTSTRAP_ORG_NAME)
        )
        if org is None:
            base_slug = _SLUG_BAD.sub("-", s.BOOTSTRAP_ORG_NAME.lower()).strip("-") or "default"
            base_slug = base_slug[:60]
            slug = base_slug
            # Race-safe slug collision handler — extremely unlikely to
            # collide on a fresh DB but keeps us robust.
            import secrets as _secrets
            while db.scalar(_sel(Organization.id).where(Organization.slug == slug)):
                slug = f"{base_slug}-{_secrets.token_hex(3)}"
            org = Organization(
                name=s.BOOTSTRAP_ORG_NAME,
                slug=slug,
                description="Created automatically on first boot.",
            )
            db.add(org)
            db.flush()  # need org.id

        admin = User(
            org_id=org.id,
            name=s.BOOTSTRAP_ADMIN_NAME or "Admin",
            email=email,
            role=ROLE_ADMIN,
            is_active=True,
            password_hash=hash_password(s.BOOTSTRAP_ADMIN_PASSWORD),
        )
        db.add(admin)
        db.commit()
        logger.warning(
            "Bootstrap: created default admin %s (org: %s). CHANGE THE PASSWORD.",
            email, org.name,
        )
    except Exception:
        logger.exception("Bootstrap admin creation failed")
        try:
            db.rollback()
        except Exception:
            pass
    finally:
        db.close()


@asynccontextmanager
async def lifespan(app: FastAPI):
    import asyncio
    init_db()
    _bootstrap_admin()

    settings = get_settings()
    if not settings.SESSION_SECRET:
        logger.warning(
            "SESSION_SECRET is not set. Using a random per-process fallback. "
            "Set SESSION_SECRET in your environment for stable sessions across "
            "restarts and multi-worker deployments."
        )

    # Spawn the audit-retention sweep if enabled.
    retention_task = None
    if settings.AUDIT_RETENTION_DAYS > 0:
        retention_task = asyncio.create_task(
            _audit_retention_loop(settings.AUDIT_RETENTION_DAYS)
        )

    logger.info(
        "Bug Hunter v%s started. asset_version=%s",
        settings.APP_VERSION, app.state.asset_version,
    )
    try:
        yield
    finally:
        if retention_task is not None:
            retention_task.cancel()
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
# CSRF defence-in-depth (double-submit cookie). Installed after rate-limit
# so unauthenticated abuse is throttled FIRST and our 403 doesn't even
# get evaluated on a flooded path.
app.add_middleware(CSRFMiddleware)
# Observability is the OUTERMOST middleware so it sees the request from
# the moment it arrives until the moment the response leaves; that way
# the access log + /metrics histogram includes time spent in every other
# middleware below it (rate limit, CSRF, CORS, ...).
app.add_middleware(
    ObservabilityMiddleware,
    json_logging=settings.JSON_LOGGING,
)


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


# Serve the PWA service worker from the root path so its scope can
# cover the entire origin. (Service workers are restricted to the
# scope at-or-below the URL they're served from; /static/sw.js would
# only control /static/* without an explicit Service-Worker-Allowed
# header, which static-file mounts don't easily emit.)
@app.get("/sw.js", include_in_schema=False)
def service_worker() -> Response:
    body = (settings.STATIC_DIR / "sw.js").read_text(encoding="utf-8")
    return Response(
        content=body,
        media_type="application/javascript",
        headers={"Service-Worker-Allowed": "/",
                 "Cache-Control": "no-cache"},
    )


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
app.include_router(totp_route.router)
app.include_router(organizations.router)
app.include_router(branding_route.router)
app.include_router(invitations.router)
app.include_router(users.router)
app.include_router(projects.router)
app.include_router(custom_fields_route.router)
app.include_router(memberships.router)
app.include_router(bugs.router)
app.include_router(events.router)
app.include_router(stats.router)
app.include_router(audit.router)
app.include_router(sessions.router)
app.include_router(webhooks_route.router)
app.include_router(saved_views_route.router)
app.include_router(dsar_route.router)
app.include_router(chatbot_router)


# ---------------------------------------------------------------------------
# /api/metrics — Prometheus text exposition. Off by default so anonymous
# scrapers can't fingerprint deployments; enable via METRICS_ENABLED.
# Optionally guarded by a shared bearer token (METRICS_TOKEN).
# ---------------------------------------------------------------------------
@app.get("/api/metrics", include_in_schema=False)
def metrics_endpoint(request: Request) -> Response:
    s = get_settings()
    if not s.METRICS_ENABLED:
        raise HTTPException(status_code=404, detail="Not found")
    if s.METRICS_TOKEN:
        auth = request.headers.get("authorization", "")
        if not auth.startswith("Bearer "):
            raise HTTPException(status_code=401, detail="Missing bearer token")
        if auth[7:].strip() != s.METRICS_TOKEN:
            raise HTTPException(status_code=403, detail="Invalid metrics token")
    text = render_prometheus()
    return Response(content=text, media_type="text/plain; version=0.0.4")


@app.exception_handler(HTTPException)
async def http_exc_handler(request: Request, exc: HTTPException) -> JSONResponse:
    return JSONResponse(status_code=exc.status_code, content={"detail": exc.detail})


if __name__ == "__main__":
    import uvicorn
    uvicorn.run("app.main:app", host="0.0.0.0", port=8000, reload=False)
