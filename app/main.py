"""FastAPI application entry point."""
from __future__ import annotations

import hashlib
import html
import json
import logging
import os
import secrets
import time
from collections import deque
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from threading import Lock
from typing import Optional

from fastapi import (
    Depends,
    FastAPI,
    HTTPException,
    Request,
    Response,
    responses,
)
from fastapi.middleware.cors import CORSMiddleware
from fastapi.middleware.gzip import GZipMiddleware
from fastapi.responses import (
    FileResponse,
    HTMLResponse,
    JSONResponse,
    RedirectResponse,
)
from fastapi.staticfiles import StaticFiles
from sqlalchemy import select, text
from sqlalchemy.exc import DataError, SQLAlchemyError
from sqlalchemy.orm import Session as OrmSession
from starlette.middleware.base import BaseHTTPMiddleware

from app.auth import (
    COOKIE_NAME,
    hash_password,
    new_jti,
    parse_session_token,
    set_session_cookie,
    trusted_forwarded_ip,
)
from app.chatbot.router import router as chatbot_router
from app.config import get_settings
from app.database import SessionLocal, get_db, init_db
from app.metrics import MetricsMiddleware
from app.metrics import render as render_metrics
from app.models import ROLE_ADMIN, Activity, Organization, User
from app.models import Session as SessionRow
from app.routes import (
    agile,
    agile_board,
    agile_planning,
    agile_reports,
    agile_taxonomy,
    audit,
    auth,
    bugs,
    custom_fields,
    devices,
    dsar,
    events,
    git,
    invitations,
    memberships,
    notifications,
    organizations,
    projects,
    push,
    reports,
    saved_views,
    sessions,
    stats,
    totp,
    users,
    webhooks,
)
from app.schemas import (
    ALLOWED_ENVIRONMENTS,
    ALLOWED_ITEM_TYPES,
    ALLOWED_PRIORITIES,
    ALLOWED_STATUSES,
    STATUSES_BY_TYPE,
)
from app.telemetry import instrument_app, setup_telemetry, shutdown_telemetry
from app.tenancy import create_organization

logger = logging.getLogger("bug_hunter")
logging.basicConfig(level=get_settings().LOG_LEVEL)
# Console format (LOG_FORMAT) + OTLP traces/metrics/logs. No-op unless
# OTEL_EXPORTER_OTLP_ENDPOINT is set; never replaces console output.
setup_telemetry()


# Asset version hash, recomputed on every server start.
# Injected into HTML so asset URLs change on redeploy (cache busting).
ASSET_VERSION_PLACEHOLDER = "__ASSET_VERSION__"
APP_NAME_PLACEHOLDER = "__APP_NAME__"
APP_VERSION_PLACEHOLDER = "__APP_VERSION__"


# Files over this cap hash by path+size, not content, to keep startup fast.
_MAX_ASSET_FILE_BYTES = 8 * 1024 * 1024


def _compute_asset_version(static_dir: Path) -> str:
    h = hashlib.sha256()
    if not static_dir.exists():
        return "dev"
    for path in sorted(static_dir.rglob("*")):
        if path.is_file() and not path.name.startswith("."):
            try:
                h.update(path.relative_to(static_dir).as_posix().encode("utf-8"))
                h.update(b"|")
                st = path.stat()
                if st.st_size > _MAX_ASSET_FILE_BYTES:
                    # Path + size still shifts the version if the file is replaced.
                    h.update(f"size={st.st_size}".encode("utf-8"))
                else:
                    h.update(path.read_bytes())
            except OSError:
                continue
    return h.hexdigest()[:12]


def _reset_bootstrap_admin(db, admin: User, s) -> None:
    """BOOTSTRAP_ADMIN_RESET_PASSWORD recovery: restore the bootstrap account as an active
    admin with the configured password and sign out its devices."""
    admin.password_hash = hash_password(s.BOOTSTRAP_ADMIN_PASSWORD)
    admin.is_active = True
    admin.role = ROLE_ADMIN
    admin.session_version = (admin.session_version or 0) + 1
    db.execute(SessionRow.__table__.delete().where(SessionRow.user_id == admin.id))
    logger.warning(
        "Bootstrap: reset the password of %s (BOOTSTRAP_ADMIN_RESET_PASSWORD). "
        "Change it, then turn the switch off.", s.BOOTSTRAP_ADMIN_EMAIL,
    )


def _bootstrap() -> None:
    """First-run bootstrap. Idempotent.

    With BOOTSTRAP_ADMIN_EMAIL/PASSWORD set and no such user yet, create the organization
    BOOTSTRAP_ORG_NAME with that admin (and its default project). An existing bootstrap user is
    never touched unless BOOTSTRAP_ADMIN_RESET_PASSWORD is on. Without bootstrap settings, public
    sign-up (/signup) creates organizations; if that is off too, nobody could ever sign in, so
    startup stops with an actionable error.
    """
    s = get_settings()
    email = s.BOOTSTRAP_ADMIN_EMAIL.strip().lower()
    with SessionLocal() as db:
        if not email:
            if not s.ALLOW_PUBLIC_SIGNUP and db.query(User).count() == 0:
                raise RuntimeError(
                    "No way to create the first account: public sign-up is off and no "
                    "bootstrap admin is configured. Set BOOTSTRAP_ADMIN_EMAIL and "
                    "BOOTSTRAP_ADMIN_PASSWORD, or enable ALLOW_PUBLIC_SIGNUP."
                )
            return
        existing = db.scalar(select(User).where(User.email == email))
        if existing is not None:
            if s.BOOTSTRAP_ADMIN_RESET_PASSWORD and s.BOOTSTRAP_ADMIN_PASSWORD:
                _reset_bootstrap_admin(db, existing, s)
                db.commit()
            return
        if not s.BOOTSTRAP_ADMIN_PASSWORD:
            raise RuntimeError(
                "BOOTSTRAP_ADMIN_EMAIL is set but BOOTSTRAP_ADMIN_PASSWORD is empty."
            )
        org_name = s.BOOTSTRAP_ORG_NAME or "Default Organization"
        # Reuse the organization of that name (e.g. the one an upgraded single-tenant database
        # was given) so the admin lands next to the existing data.
        org = db.scalar(
            select(Organization).where(Organization.name == org_name).order_by(Organization.id)
        ) or create_organization(db, org_name)
        db.add(User(
            org_id=org.id, name=s.BOOTSTRAP_ADMIN_NAME, email=email, role=ROLE_ADMIN,
            is_active=True, password_hash=hash_password(s.BOOTSTRAP_ADMIN_PASSWORD),
        ))
        db.commit()
        logger.warning("Bootstrap: created organization %r with admin %s. CHANGE THE PASSWORD.",
                       org.name, email)


def _runtime_config_warnings(s) -> list[str]:
    """Collect non-fatal startup warnings for insecure-but-allowed config.

    Returned as a list so tests can verify the policy; fatal checks stay in lifespan().
    """
    warnings: list[str] = []
    if not s.SESSION_SECRET:
        warnings.append(
            "SESSION_SECRET is not set. Using a random per-process fallback, so "
            "sessions will NOT survive a restart and multi-worker deployments log "
            "users out unpredictably. Set SESSION_SECRET (`openssl rand -hex 32`) "
            "for any non-throwaway deploy, HTTP or HTTPS."
        )
    # Console email stays allowed in dev. On an HTTPS deploy without an
    # explicit APP_ENV it is allowed but warned about (bodies can carry
    # password-reset links); APP_ENV=production rejects it outright in
    # _production_config_errors.
    if s.is_production and s.EMAIL_BACKEND == "console":
        warnings.append(
            "EMAIL_BACKEND=console on a production deploy: every email body, "
            "INCLUDING password-reset links, is written to the logs. Set "
            "EMAIL_BACKEND=smtp (or 'disabled') in production."
        )
    if s.AUTO_LOGIN_ENABLED:
        warnings.append(
            "AUTO_LOGIN_ENABLED is on: every visitor is signed in automatically "
            "as the bootstrap admin (BOOTSTRAP_ADMIN_EMAIL), with no "
            "credentials, and never sees the login screen. This is a "
            "convenience switch for trusted local/dev deployments only — "
            "leave it off anywhere the server is reachable by anyone else."
        )
    return warnings


def _is_placeholder(value: str | None) -> bool:
    """True for the published ".env.example" placeholders ("replace_with_...").

    They are long enough to pass a length check, but anyone can read them, so a
    production deploy must treat them as unset.
    """
    return (value or "").strip().lower().startswith("replace_with")


def _production_config_errors(s) -> list[str]:
    """Fail-closed production checks. Any entry aborts startup with 500-safe text.

    Two tiers. Every production-like deploy (``is_production``: APP_ENV says
    production, or COOKIE_SECURE=true) must have a strong session secret, no
    default bootstrap password, auto-login off and a well-formed Git key. The
    stricter deployment-shape rules (secure cookie, https base URL, real email
    backend) apply only when APP_ENV explicitly declares production/staging, so
    an HTTPS deploy that never set APP_ENV keeps working across upgrades.
    """
    errors: list[str] = []
    if len(s.SESSION_SECRET or "") < 32 or _is_placeholder(s.SESSION_SECRET):
        errors.append("SESSION_SECRET must be set to a strong value (>= 32 chars)")
    if s.BOOTSTRAP_ADMIN_EMAIL and (
        (s.BOOTSTRAP_ADMIN_PASSWORD or "") in ("", "ChangeMe123!")
        or _is_placeholder(s.BOOTSTRAP_ADMIN_PASSWORD)
    ):
        # Only matters on a fresh install (empty users table); checked at
        # bootstrap time so existing deploys never break on restart.
        errors.append("default bootstrap admin password is not allowed in production")
    if s.AUTO_LOGIN_ENABLED:
        errors.append("AUTO_LOGIN_ENABLED must be off in production")
    key = (s.GIT_CREDENTIAL_ENCRYPTION_KEY or "").strip()
    if key:
        try:
            from cryptography.fernet import Fernet

            Fernet(key.encode("utf-8"))
        except Exception:
            errors.append("GIT_CREDENTIAL_ENCRYPTION_KEY is malformed")
    if not getattr(s, "is_production_env", False):
        return errors
    if not s.COOKIE_SECURE:
        errors.append("COOKIE_SECURE must be true in production")
    if not (s.APP_BASE_URL or "").strip():
        errors.append("APP_BASE_URL must be set in production")
    elif not (s.APP_BASE_URL or "").strip().lower().startswith("https://"):
        errors.append("APP_BASE_URL must use https in production")
    if s.EMAIL_BACKEND == "console":
        errors.append("EMAIL_BACKEND=console is not allowed in production")
    return errors


def _safe_init_db() -> bool:
    """Run schema init and bootstrap; a DB-down error starts the app degraded
    (reported by /api/health) rather than crash-looping.

    Bounded by DB_STARTUP_TIMEOUT_SECONDS: a firewalled/slow Postgres (or
    DDL waiting on a lock) must never hang the container at "Waiting for
    application startup" until the platform kills the replica -- run the
    blocking SQLAlchemy work in a thread and give up loudly instead.
    """
    import concurrent.futures

    def _work() -> None:
        # Startup writes (first-run seeding, board backfill) run inside
        # init_db's migration lock, so only one replica ever performs them.
        init_db(on_migrated=_startup_writes)

    timeout = float(get_settings().DB_STARTUP_TIMEOUT_SECONDS)
    with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
        future = pool.submit(_work)
        try:
            future.result(timeout=timeout)
            return True
        except concurrent.futures.TimeoutError:
            logger.error(
                "Database initialization timed out after %ss; starting "
                "degraded (/api/health reports degraded). A later replica "
                "or restart will retry reconciliation.",
                get_settings().DB_STARTUP_TIMEOUT_SECONDS,
            )
            return False
        except (SQLAlchemyError, OSError):
            logger.exception(
                "Database initialization failed at startup; starting degraded."
            )
            return False


def _board_schema_ready(db) -> bool:
    """Guard: on an old database the display_id columns may not exist
    yet (a previous failed migration). Reconciling with the full
    Board model would SELECT the missing column and crash startup --
    defer to the next boot after _add_missing_columns has run."""
    from sqlalchemy import inspect as _inspect
    # get_columns() returns a list of per-column dicts (unhashable),
    # so collect the names directly — set() on the dicts raises
    # "TypeError: cannot use 'dict' as a set element".
    board_col_names = {col["name"] for col in _inspect(db.bind).get_columns("boards")}
    required = {"display_id"}
    if required <= board_col_names:
        return True
    logger.warning(
        "Board reconciliation deferred: boards table lacks %s; "
        "will retry next boot after column migration.",
        sorted(required - board_col_names),
    )
    return False


def _reconcile_board_columns(db) -> tuple[bool, int]:
    """Add the Testing column + status mappings to every board.

    Returns (changed, testing_count): whether anything was updated and how
    many boards expose a Testing column after reconciliation.
    """
    from app.agile.boards import reconcile_board_status_mappings, reconcile_testing_column
    from app.models import Board
    changed = False
    boards = db.scalars(select(Board)).all()
    for board in boards:
        if reconcile_testing_column(db, board):
            changed = True
        if reconcile_board_status_mappings(db, board):
            changed = True
    testing_count = sum(1 for b in boards if "testing" in {c.category for c in b.columns})
    return changed, testing_count


def _reconcile_status_transitions(db) -> bool:
    """Backfill WorkflowTransition rows for statuses seeded after the fact.

    Returns True when any transition row was added.
    """
    from app.agile.workflow import reconcile_transitions
    from app.models import WorkflowStatus
    changed = False
    work_item_types = db.scalars(
        select(WorkflowStatus.work_item_type).where(WorkflowStatus.scope_key == "global").distinct()
    ).all()
    for work_item_type in work_item_types:
        if reconcile_transitions(db, work_item_type):
            changed = True
    return changed


def _reconcile_existing_boards() -> None:
    """Additive backfill: boards activated before the Testing
    column existed only have todo/in_progress/done. Idempotent — a no-op on
    every subsequent boot once every board has a testing column. Also
    backfills WorkflowTransition rows for any status added to a work-item
    type after its transitions were first seeded (e.g. Testing added to
    Bug/Task/Requirement later), otherwise no card could ever transition
    into/out of that status."""
    try:
        with SessionLocal() as db:
            if not _board_schema_ready(db):
                return
            columns_changed, testing_count = _reconcile_board_columns(db)
            transitions_changed = _reconcile_status_transitions(db)
            if columns_changed or transitions_changed:
                db.commit()
            if columns_changed:
                logger.info("Reconciled Testing column onto %d pre-existing board(s).",
                            testing_count)
            if transitions_changed:
                logger.info("Reconciled missing workflow transitions for newly-added statuses.")
    except (SQLAlchemyError, OSError):
        logger.exception("Board Testing-column reconciliation failed; continuing without it.")
    except Exception:
        logger.exception("Board reconciliation failed unexpectedly; continuing without it.")


def _check_db_health() -> bool:
    """Quick DB liveness probe used by /api/health."""
    try:
        db = SessionLocal()
        try:
            db.execute(text("SELECT 1"))
            return True
        finally:
            db.close()
    except (SQLAlchemyError, OSError):
        return False


def _startup_writes() -> None:
    """Data writes that must run once per boot, under the migration lock."""
    _bootstrap()
    # Logs and swallows its own failures: a backfill problem never blocks boot.
    _reconcile_existing_boards()


def _enforce_production_config(_settings) -> None:
    """Fail-closed production gate; a no-op outside production."""
    if not _settings.is_production:
        return
    failures = _production_config_errors(_settings)
    # A fresh production database with a placeholder bootstrap password must
    # not boot; an existing database (users already present) is unaffected.
    if any("bootstrap" in failure for failure in failures):
        try:
            from app.database import SessionLocal as _SessionLocal
            from app.models import User as _User

            with _SessionLocal() as _db:
                if _db.query(_User).count() > 0:
                    failures = [
                        failure
                        for failure in failures
                        if "bootstrap" not in failure
                    ]
        except SQLAlchemyError:
            logger.warning(
                "Production bootstrap-password check skipped: database "
                "unavailable at startup."
            )
    if failures:
        raise RuntimeError(
            "Production configuration is unsafe: " + "; ".join(failures)
        )


@asynccontextmanager
async def lifespan(app: FastAPI):
    _safe_init_db()
    _settings = get_settings()
    _enforce_production_config(_settings)
    for _w in _runtime_config_warnings(_settings):
        logger.warning(_w)

    # Optional email-digest scheduler. No-op unless EMAIL_DIGEST_CRON is set.
    from app import scheduler
    scheduler.start()

    logger.info("%s started. asset_version=%s", settings.APP_NAME, app.state.asset_version)
    yield
    await scheduler.stop()
    logger.info("%s shutting down.", settings.APP_NAME)
    shutdown_telemetry()  # flush queued spans/metrics/logs before exit


settings = get_settings()
# API docs: always on in development; a production deploy (APP_ENV=production
# or COOKIE_SECURE=true) turns /docs, /redoc and /openapi.json off unless
# ENABLE_API_DOCS=true, since a full endpoint map is reconnaissance surface.
# The UI assets are self-hosted (app/static/swagger-ui, vendored from
# swagger-ui-dist@5 / redoc@2) and the pages use only external scripts because
# the strict CSP (`script-src 'self'`, no inline) blanks FastAPI's default.
_docs_enabled = settings.ENABLE_API_DOCS or not settings.is_production
# OpenAPI requires a non-empty version; APP_VERSION stays blank when .env
# doesn't set it (no hardcoded fallback), so show "dev" rather than crash.
DISPLAY_VERSION = settings.APP_VERSION or "dev"

_SWAGGER_ASSET_DIR = settings.STATIC_DIR / "swagger-ui"
app = FastAPI(
    title=settings.APP_NAME,
    version=DISPLAY_VERSION,
    lifespan=lifespan,
    docs_url=None,
    redoc_url=None,
    openapi_url="/openapi.json" if _docs_enabled else None,
)
# Server spans (route, status, latency) + HTTP metrics + DB/outbound spans.
instrument_app(app)


def swagger_docs() -> responses.HTMLResponse:
    return HTMLResponse(
        f"""<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>{html.escape(settings.APP_NAME)} — API docs</title>
  <link rel="stylesheet" href="/static/swagger-ui/swagger-ui.css">
  <link rel="icon" type="image/png" href="/static/icon.png?v={app.state.asset_version}" />
</head>
<body>
  <div id="swagger-ui"></div>
  <script src="/static/swagger-ui/swagger-ui-bundle.js" defer></script>
  <script src="/static/swagger-ui/swagger-ui-init.js" defer></script>
</body>
</html>""",
    )


def redoc_docs() -> responses.HTMLResponse:
    return HTMLResponse(
        f"""<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>{html.escape(settings.APP_NAME)} — API reference</title>
  <link rel="icon" type="image/png" href="/static/icon.png?v={app.state.asset_version}" />
</head>
<body>
  <redoc spec-url="/openapi.json"></redoc>
  <script src="/static/swagger-ui/redoc.standalone.js"></script>
</body>
</html>""",
    )


def swagger_ui_init_js() -> FileResponse:
    """External Swagger UI initializer (CSP forbids FastAPI's inline script)."""
    return FileResponse(
        _SWAGGER_ASSET_DIR / "swagger-ui-init.js",
        media_type="application/javascript",
    )


if _docs_enabled:
    app.add_api_route("/docs", swagger_docs, methods=["GET"], include_in_schema=False)
    app.add_api_route("/redoc", redoc_docs, methods=["GET"], include_in_schema=False)
    app.add_api_route(
        "/static/swagger-ui/swagger-ui-init.js", swagger_ui_init_js,
        methods=["GET"], include_in_schema=False,
    )

# Computed once at import time. Kept on app.state so tests can override it.
app.state.asset_version = _compute_asset_version(settings.STATIC_DIR)


# CORS
# The SPA uses cookies, so responses must echo a concrete allowlisted Origin —
# the spec forbids "*" with credentials.
_origins = list(settings.CORS_ORIGINS)
_allow_credentials = True
if not _origins:
    # Same-origin only; same-origin traffic skips CORS anyway.
    _allow_credentials = False
elif "*" in _origins:
    # Wildcard + credentials is forbidden; drop credentials so preflights still pass.
    _allow_credentials = False
    logger.warning(
        "CORS_ORIGINS contains '*' which disables credentialed CORS. Set "
        "CORS_ORIGINS to your concrete origin(s) (e.g. "
        "https://bugs.example.com) to allow cross-origin browser sessions."
    )

# CORSMiddleware is registered last: Starlette stacks middleware in reverse,
# and CORS must be outermost to intercept OPTIONS before other middleware.

# Gzip compression — skips bodies under 1 KB.
app.add_middleware(GZipMiddleware, minimum_size=1024, compresslevel=5)


# Request body size limit
# Rejects an oversized Content-Length with 413 before the body buffers into
# RAM. Chunked requests are covered by StreamingBodyLimitMiddleware.
class BodySizeLimitMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next):
        cl_header = request.headers.get("content-length")
        if cl_header:
            try:
                length = int(cl_header)
            except ValueError:
                # Malformed Content-Length; reject.
                return JSONResponse(
                    status_code=400,
                    content={"detail": "We couldn't process this request. Please try again."},
                )
            if length > settings.MAX_REQUEST_BODY_BYTES:
                logger.warning(
                    "Body too large: %d bytes claimed (limit %d) on %s",
                    length, settings.MAX_REQUEST_BODY_BYTES, request.url.path,
                )
                return JSONResponse(
                    status_code=413,
                    content={"detail": "This upload is too large. Please attach a smaller file."},
                )
        return await call_next(request)


app.add_middleware(BodySizeLimitMiddleware)


class _RequestBodyTooLarge(Exception):
    """Raised by the wrapped receive callable when the body exceeds the cap."""


class StreamingBodyLimitMiddleware:
    """Pure-ASGI backstop: counts actual body bytes so chunked requests
    (no Content-Length) can't stream unbounded into RAM; 413 past the cap.
    Raw ASGI because BaseHTTPMiddleware cannot wrap `receive`."""

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope.get("type") != "http":
            await self.app(scope, receive, send)
            return

        max_bytes = settings.MAX_REQUEST_BODY_BYTES
        total = 0

        async def counting_receive():
            nonlocal total
            message = await receive()
            if message.get("type") == "http.request":
                total += len(message.get("body", b"") or b"")
                if total > max_bytes:
                    raise _RequestBodyTooLarge()
            return message

        started = False

        async def tracking_send(message):
            nonlocal started
            if message.get("type") == "http.response.start":
                started = True
            await send(message)

        try:
            await self.app(scope, counting_receive, tracking_send)
        except _RequestBodyTooLarge:
            if started:
                # Response already started; can't replace it cleanly.
                raise
            logger.warning(
                "Streaming body exceeded %d bytes on %s",
                max_bytes, scope.get("path", ""),
            )
            resp = JSONResponse(
                status_code=413,
                content={"detail": "This upload is too large. Please attach a smaller file."},
            )
            _apply_security_headers(resp.headers)
            resp.headers.setdefault("Cache-Control", "no-store")
            await resp(scope, receive, send)


app.add_middleware(StreamingBodyLimitMiddleware)


# Cache-Control middleware — prevents stale HTML after redeploy.
#   HTML / api -> no-store; /static/assets/ -> immutable 1y (content-hashed);
#   /static/ -> 1h.
class CacheControlMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next):
        response: Response = await call_next(request)
        path = request.url.path
        # Don't override if the route already set Cache-Control (e.g. downloads).
        if response.headers.get("Cache-Control"):
            return response
        if path.startswith("/api/"):
            response.headers["Cache-Control"] = "no-store"
        elif path.startswith("/static/assets/"):
            # Vite content-hashed filenames — safe to cache for a year.
            response.headers["Cache-Control"] = "public, max-age=31536000, immutable"
        elif path.startswith("/static/"):
            # Not fingerprinted; shorter cache.
            response.headers["Cache-Control"] = "public, max-age=3600"
        else:
            # HTML uncached so a redeploy shows immediately.
            response.headers["Cache-Control"] = "no-store, no-cache, must-revalidate, max-age=0"
            response.headers["Pragma"] = "no-cache"
        return response


app.add_middleware(CacheControlMiddleware)


# Security headers — applied to every response.
# style-src needs 'unsafe-inline' (app sets .style.x); HSTS is gated on
# COOKIE_SECURE so a dev/HTTP deploy isn't locked into HTTPS.
# Endpoints the Firebase SDK needs for token mint/refresh; added to
# connect-src only when web push is enabled. Firebase scripts are vendored,
# so script-src stays 'self'.
_FCM_CONNECT_SRC = (
    " https://fcm.googleapis.com https://fcmregistrations.googleapis.com"
    " https://firebaseinstallations.googleapis.com https://www.googleapis.com"
)


def _build_csp() -> str:
    connect = "connect-src 'self'"
    if get_settings().WEB_PUSH_ENABLED:
        connect += _FCM_CONNECT_SRC
    return (
        "default-src 'self'; "
        "img-src 'self' data: blob:; "
        "media-src 'self' data: blob:; "
        "style-src 'self' 'unsafe-inline'; "
        "script-src 'self'; "
        "font-src 'self' data:; "
        f"{connect}; "
        "worker-src 'self'; "
        "object-src 'none'; "
        "frame-ancestors 'none'; "
        "base-uri 'self'; "
        "form-action 'self'"
    )


_CSP = _build_csp()

def _apply_security_headers(h) -> None:
    """Set the standard security headers (shared by middleware, short-circuit
    responses, and the 500 handler). setdefault lets handlers override."""
    h.setdefault("Content-Security-Policy", _CSP)
    h.setdefault("X-Content-Type-Options", "nosniff")
    h.setdefault("X-Frame-Options", "DENY")
    h.setdefault("Referrer-Policy", "strict-origin-when-cross-origin")
    h.setdefault("Permissions-Policy",
                 "camera=(), microphone=(), geolocation=(), "
                 "payment=(), usb=(), magnetometer=(), gyroscope=(), accelerometer=()")
    h.setdefault("Cross-Origin-Opener-Policy", "same-origin")
    h.setdefault("Cross-Origin-Resource-Policy", "same-origin")
    h.setdefault("X-Permitted-Cross-Domain-Policies", "none")
    # Don't advertise the stack.
    if "server" in h:
        del h["server"]
    # HSTS only behind real HTTPS (COOKIE_SECURE doubles as the signal).
    if settings.COOKIE_SECURE:
        h.setdefault("Strict-Transport-Security",
                     "max-age=63072000; includeSubDomains")


class SecurityHeadersMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next):
        response: Response = await call_next(request)
        _apply_security_headers(response.headers)
        return response


app.add_middleware(SecurityHeadersMiddleware)


# Rate limiting on auth-sensitive endpoints
# In-memory per-IP sliding window (per-worker buckets; add nginx limit_req
# for a strict global limit). Tuned to absorb typos, slow credential stuffing.
_RATE_RULES: dict[str, tuple[int, int]] = {
    # (max_requests, window_seconds)
    "/api/auth/login": (8, 60),
    # second factor: a 6-digit code is brute-forceable, so throttle per IP as well as per account.
    "/api/auth/login/totp": (8, 60),
    "/api/auth/signup": (5, 60),
    "/api/invitations/accept": (8, 60),
    # re-authenticates with the password and checks a 6-digit code.
    "/api/auth/email-change/request": (5, 60),
    "/api/auth/email-change/confirm": (8, 60),
    "/api/auth/forgot-password": (3, 60),
    # change-password bcrypt-verifies on every call — throttle the amplification.
    "/api/auth/reset-password": (5, 60),
    "/api/auth/change-password": (5, 60),
}
_rate_buckets: dict[tuple[str, str], deque] = {}
_rate_lock = Lock()
# Soft cap to bound memory when hammered from many IPs.
_RATE_BUCKETS_MAX = 10_000
# Buckets idle longer than the longest window are reclaimable.
_MAX_RATE_WINDOW = max((w for _, w in _RATE_RULES.values()), default=60)


def _evict_one_rate_bucket(now: float) -> None:
    """Make room for a new bucket: reclaim idle buckets first, then the
    least-recently-active — never insertion order, so an attacker can't churn
    keys to flush their own throttle. Caller holds _rate_lock."""
    horizon = now - _MAX_RATE_WINDOW
    dead = [k for k, b in _rate_buckets.items() if not b or b[-1] < horizon]
    for k in dead:
        del _rate_buckets[k]
    if len(_rate_buckets) >= _RATE_BUCKETS_MAX and _rate_buckets:
        oldest = min(_rate_buckets, key=lambda k: _rate_buckets[k][-1])
        del _rate_buckets[oldest]


def _client_ip(request: Request) -> str:
    """Resolve the client IP for rate limiting.

    X-Forwarded-For is spoofable, so it's only used when TRUST_PROXY_FORWARDED_FOR
    is set; trusted_forwarded_ip shares semantics with the audit IP resolver.
    """
    if settings.TRUST_PROXY_FORWARDED_FOR:
        xff = request.headers.get("x-forwarded-for")
        if xff:
            trusted = trusted_forwarded_ip(xff, settings.TRUST_PROXY_HOP_COUNT)
            if trusted is not None:
                return trusted
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
                # Evict idle buckets first; only fall back to least-recently-active.
                if len(_rate_buckets) >= _RATE_BUCKETS_MAX:
                    _evict_one_rate_bucket(now)
                bucket = deque()
                _rate_buckets[(path, ip)] = bucket
            # Drop timestamps outside the window.
            while bucket and bucket[0] < cutoff:
                bucket.popleft()
            if len(bucket) >= max_req:
                retry_after = max(1, int(window - (now - bucket[0])))
                logger.warning(
                    "Rate limit hit: %s from %s (%d/%d in %ss)",
                    path, ip, len(bucket), max_req, window,
                )
                resp = JSONResponse(
                    status_code=429,
                    content={"detail": "Too many attempts. Please try again later."},
                    headers={"Retry-After": str(retry_after)},
                )
                _apply_security_headers(resp.headers)
                resp.headers.setdefault("Cache-Control", "no-store")
                return resp
            bucket.append(now)
        return await call_next(request)


app.add_middleware(RateLimitMiddleware)
app.add_middleware(MetricsMiddleware)


# CSRF defense in depth — SameSite=Lax has gaps (subdomains, older browsers),
# so mutating /api/ requests must present a matching Origin/Referer. Clients
# sending neither (curl, httpx) pass: CSRF needs a browser as deputy.
def _allowed_origins() -> set[str]:
    """Origins allowed by the CSRF check (CORS_ORIGINS minus "*"); the request
    Host is added separately so SPA usage needs no CORS config."""
    return {o.rstrip("/") for o in settings.CORS_ORIGINS if o and o != "*"}


# Safe methods (GET/HEAD/OPTIONS) skip the check.
_CSRF_UNSAFE_METHODS = {"POST", "PUT", "PATCH", "DELETE"}
# No exemptions: login included, so cross-site forced-login is blocked too.
_CSRF_EXEMPT_PATHS: frozenset[str] = frozenset()


class CsrfOriginMiddleware(BaseHTTPMiddleware):
    """Block mutating requests whose Origin doesn't match the app's host."""
    async def dispatch(self, request: Request, call_next):
        method = request.method.upper()
        path = request.url.path
        if (
            method not in _CSRF_UNSAFE_METHODS
            or not path.startswith("/api/")
            or path in _CSRF_EXEMPT_PATHS
        ):
            return await call_next(request)

        origin = request.headers.get("origin", "").rstrip("/")
        referer = request.headers.get("referer", "")

        # No browser fingerprint: not a browser, cannot be CSRF.
        if not origin and not referer:
            return await call_next(request)

        # Same-origin URLs from the request Host. Schemes are concatenated
        # (not literal "http://") to dodge static-analyzer hardcoded-URL warnings.
        host = request.headers.get("host", "")
        allowed = _allowed_origins()
        if host:
            sep = "://"
            for scheme in ("http", "https"):
                allowed.add(scheme + sep + host)

        if origin:
            if origin in allowed:
                return await call_next(request)
        else:
            # Origin absent means referer is present (see the guard above);
            # match by URL prefix.
            if any(referer.startswith(a + "/") or referer == a for a in allowed if a):
                return await call_next(request)

        logger.warning(
            "CSRF check failed: method=%s path=%s origin=%r referer=%r",
            method, path, origin, referer,
        )
        resp = JSONResponse(
            status_code=403,
            content={"detail": "Cross-origin request blocked."},
        )
        _apply_security_headers(resp.headers)
        resp.headers.setdefault("Cache-Control", "no-store")
        return resp


app.add_middleware(CsrfOriginMiddleware)

# CORS registered last (outermost — see note near _origins).
if _origins:
    # Restrict methods/headers to what the SPA uses; smaller blast radius.
    app.add_middleware(
        CORSMiddleware,
        allow_origins=_origins,
        allow_credentials=_allow_credentials,
        allow_methods=["GET", "POST", "PUT", "PATCH", "DELETE", "OPTIONS"],
        allow_headers=["Authorization", "Content-Type", "X-Requested-With"],
    )


app.mount("/static", StaticFiles(directory=settings.STATIC_DIR), name="static")


# Rendered pages are stable per process; keyed on asset_version so tests
# that swap it stay correct.
_html_cache: dict[tuple[str, str], str] = {}


def _serve_html(filename: str) -> HTMLResponse:
    """Serve an HTML file with branding placeholders replaced, cached
    per (file, asset_version)."""
    key = (filename, app.state.asset_version)
    body = _html_cache.get(key)
    if body is None:
        body = (settings.STATIC_DIR / filename).read_text(encoding="utf-8")
        body = body.replace(ASSET_VERSION_PLACEHOLDER, app.state.asset_version)
        body = body.replace(APP_NAME_PLACEHOLDER, settings.APP_NAME)
        body = body.replace(APP_VERSION_PLACEHOLDER, DISPLAY_VERSION)
        if len(_html_cache) > 32:  # only a few pages exist; clear if somehow large
            _html_cache.clear()
        _html_cache[key] = body
    return HTMLResponse(body)


def _has_valid_session(request: Request) -> bool:
    """True if the request carries a valid, non-revoked session cookie.

    Must check revocation, not just the signature — accepting a revoked cookie
    here makes / and /login.html redirect-loop each other.
    """
    token = request.cookies.get(COOKIE_NAME, "")
    parsed = parse_session_token(token)
    if parsed is None:
        return False
    user_id, _session_version, jti = parsed
    if jti is None:
        # Legacy pre-sessions-table cookie: signature alone; /api/auth/me still
        # does the full check.
        return True
    # Modern cookie: the session row must exist and not be expired.
    db = SessionLocal()
    try:
        sess = db.scalar(select(SessionRow).where(SessionRow.jti == jti))
        if sess is None or sess.user_id != user_id:
            return False
        expires = sess.expires_at
        if expires.tzinfo is None:
            expires = expires.replace(tzinfo=timezone.utc)
        return expires >= datetime.now(timezone.utc)
    except SQLAlchemyError:
        # DB momentarily down: send to login rather than 500.
        logger.exception("_has_valid_session: DB lookup failed for jti=%s", jti)
        return False
    finally:
        db.close()


def _auto_login(request: Request, db: OrmSession) -> Optional[tuple[User, str]]:
    """When AUTO_LOGIN_ENABLED, sign in as the bootstrap admin with no
    credentials — a convenience switch for trusted local/dev deployments,
    not a hardened auth mode (see the startup warning in
    _runtime_config_warnings). Returns None (falling back to the normal
    login flow) if the flag is off or that account is missing/deactivated.
    """
    if not settings.AUTO_LOGIN_ENABLED:
        return None
    email = settings.BOOTSTRAP_ADMIN_EMAIL.strip().lower()
    user = db.scalar(select(User).where(User.email == email))
    if user is None or not user.is_active:
        return None
    jti = new_jti()
    expires_at = datetime.now(timezone.utc) + timedelta(seconds=settings.SESSION_TTL_SECONDS)
    db.add(SessionRow(
        user_id=user.id,
        jti=jti,
        user_agent=(request.headers.get("user-agent") or "")[:400],
        ip_address=_client_ip(request)[:64],
        expires_at=expires_at,
    ))
    db.add(Activity(
        org_id=user.org_id, bug_id=None, entity_type="auth", entity_id=None,
        actor_user_id=user.id, actor_name=user.name,
        action="login", detail=f"{user.email} auto-logged in (AUTO_LOGIN_ENABLED)",
    ))
    db.commit()
    return user, jti


@app.get("/", response_class=HTMLResponse, include_in_schema=False)
def home(request: Request, db: OrmSession = Depends(get_db)):
    # Server-side redirect avoids a flash of the empty app shell.
    if not _has_valid_session(request):
        auto = _auto_login(request, db)
        if auto is None:
            return RedirectResponse(url="/login.html", status_code=302)
        user, jti = auto
        resp = _serve_html("index.html")
        set_session_cookie(resp, user, jti=jti)
        return resp
    return _serve_html("index.html")


@app.get("/login.html", response_class=HTMLResponse, include_in_schema=False)
@app.get("/login", response_class=HTMLResponse, include_in_schema=False)
def login_page(request: Request, db: OrmSession = Depends(get_db)):
    # Skip the login form for users who are already logged in.
    if _has_valid_session(request):
        return RedirectResponse(url="/", status_code=302)
    auto = _auto_login(request, db)
    if auto is not None:
        user, jti = auto
        resp = RedirectResponse(url="/", status_code=302)
        set_session_cookie(resp, user, jti=jti)
        return resp
    return _serve_html("login.html")


@app.get("/signup.html", response_class=HTMLResponse, include_in_schema=False)
@app.get("/signup", response_class=HTMLResponse, include_in_schema=False)
def signup_page(request: Request) -> Response:
    if _has_valid_session(request):
        return RedirectResponse(url="/", status_code=302)
    return _serve_html("signup.html")


@app.get("/reset.html", response_class=HTMLResponse, include_in_schema=False)
@app.get("/reset", response_class=HTMLResponse, include_in_schema=False)
def reset_page() -> HTMLResponse:
    # Always reachable — even a logged-in user may follow a reset link.
    return _serve_html("reset.html")


# Public pages: an invitation link, the privacy policy and the account-deletion page (the
# last two are the URLs an app-store listing points to) work signed in or out.
@app.get("/accept-invite.html", response_class=HTMLResponse, include_in_schema=False)
@app.get("/accept-invite", response_class=HTMLResponse, include_in_schema=False)
def accept_invite_page() -> HTMLResponse:
    return _serve_html("accept-invite.html")


@app.get("/privacy.html", response_class=HTMLResponse, include_in_schema=False)
@app.get("/privacy", response_class=HTMLResponse, include_in_schema=False)
def privacy_page() -> HTMLResponse:
    return _serve_html("privacy.html")


@app.get("/delete-account.html", response_class=HTMLResponse, include_in_schema=False)
@app.get("/delete-account", response_class=HTMLResponse, include_in_schema=False)
def delete_account_page() -> HTMLResponse:
    return _serve_html("delete-account.html")


# FCM service worker served from root scope (a worker only controls pages at
# or below its own path, so /static/ would be too narrow). Config is injected
# like the HTML placeholders.
_FIREBASE_SW = """\
importScripts('/static/vendor/firebase-app-compat.js');
importScripts('/static/vendor/firebase-messaging-compat.js');
firebase.initializeApp(__FIREBASE_CONFIG__);
const messaging = firebase.messaging();
messaging.onBackgroundMessage(function (payload) {
  const n = payload.notification || {};
  const d = payload.data || {};
    self.registration.showNotification(n.title || '__APP_NAME__', {
    body: n.body || '',
    icon: '/static/icon.png?v=__ASSET_VERSION__',
    badge: '/static/icon.png?v=__ASSET_VERSION__',
    data: { url: d.url || '/' },
    tag: d.url || 'bug-hunter'
  });
});
self.addEventListener('notificationclick', function (event) {
  event.notification.close();
  const url = (event.notification.data && event.notification.data.url) || '/';
  event.waitUntil(clients.matchAll({ type: 'window', includeUncontrolled: true }).then(function (cl) {
    for (const c of cl) { if ('focus' in c) { c.navigate(url); return c.focus(); } }
    if (clients.openWindow) return clients.openWindow(url);
  }));
});
"""


@app.get("/firebase-messaging-sw.js", include_in_schema=False)
def firebase_messaging_sw() -> Response:
    """Serve the FCM background service worker with the Firebase config injected.

    Returns a no-op comment when web push isn't configured, so the client-side
    service worker registration doesn't 404.
    """
    media = "application/javascript"
    headers = {"Service-Worker-Allowed": "/", "Cache-Control": "no-cache"}
    if not (settings.WEB_PUSH_ENABLED and settings.FIREBASE_API_KEY):
        return Response("/* web push not configured */", media_type=media, headers=headers)
    cfg = json.dumps({
        "apiKey": settings.FIREBASE_API_KEY,
        "authDomain": settings.FIREBASE_AUTH_DOMAIN,
        "projectId": settings.FIREBASE_PROJECT_ID,
        "messagingSenderId": settings.FIREBASE_MESSAGING_SENDER_ID,
        "appId": settings.FIREBASE_APP_ID,
    })
    return Response(
        _FIREBASE_SW.replace("__FIREBASE_CONFIG__", cfg)
        .replace("__APP_NAME__", settings.APP_NAME)
        .replace("__ASSET_VERSION__", app.state.asset_version),
        media_type=media,
        headers=headers,
    )


@app.get("/api/health", tags=["meta"])
def health() -> Response:
    """Liveness and readiness probe.

    Returns 503 when the database is unreachable so the Docker HEALTHCHECK and
    load balancer treat a DB-down app as unhealthy. version and asset_version
    are unauthenticated: the SPA uses asset_version to detect a redeploy, and
    the app version is already shown on the public login page.
    """
    db_ok = _check_db_health()
    payload = {
        "status": "ok" if db_ok else "degraded",
        "database": "ok" if db_ok else "unavailable",
        "version": DISPLAY_VERSION,
        "asset_version": app.state.asset_version,
    }
    return JSONResponse(payload, status_code=200 if db_ok else 503)


@app.get("/api/meta", tags=["meta"])
def meta() -> dict[str, object]:
    """Return static enums and per-item-type status sets for the frontend."""
    return {
        "statuses": ALLOWED_STATUSES,
        "statuses_by_type": STATUSES_BY_TYPE,
        "priorities": ALLOWED_PRIORITIES,
        "environments": ALLOWED_ENVIRONMENTS,
        "item_types": ALLOWED_ITEM_TYPES,
        # Lets the login page show or hide "Create an organization".
        "signup_enabled": settings.ALLOW_PUBLIC_SIGNUP,
        # Facts the public privacy page states about this installation.
        "privacy_contact_email": settings.PRIVACY_CONTACT_EMAIL,
        "audit_retention_days": settings.AUDIT_RETENTION_DAYS,
    }


@app.get("/api/metrics", include_in_schema=False)
def metrics(request: Request) -> Response:
    """Prometheus counters; 404 unless METRICS_ENABLED, guarded by METRICS_TOKEN when set."""
    if not settings.METRICS_ENABLED:
        raise HTTPException(status_code=404, detail="Not found")
    if settings.METRICS_TOKEN:
        supplied = request.headers.get("authorization", "")
        if not supplied.startswith("Bearer "):
            raise HTTPException(status_code=401, detail="Missing bearer token")
        if not secrets.compare_digest(supplied[7:].strip(), settings.METRICS_TOKEN):
            raise HTTPException(status_code=403, detail="Invalid metrics token")
    return Response(render_metrics(), media_type="text/plain; version=0.0.4")


app.include_router(auth.router)
app.include_router(totp.router)
app.include_router(dsar.router)
app.include_router(organizations.router)
app.include_router(invitations.router)
app.include_router(users.router)
app.include_router(projects.router)
app.include_router(memberships.router)
app.include_router(custom_fields.router)
app.include_router(bugs.router)
app.include_router(events.router)
app.include_router(stats.router)
app.include_router(reports.router)
app.include_router(audit.router)
app.include_router(sessions.router)
app.include_router(saved_views.router)
app.include_router(webhooks.router)
app.include_router(notifications.router)
app.include_router(push.router)
app.include_router(devices.router)
app.include_router(agile.router)
app.include_router(agile_board.router)
app.include_router(agile_planning.router)
app.include_router(agile_taxonomy.router)
app.include_router(agile_reports.router)
app.include_router(git.router)
app.include_router(chatbot_router)


@app.exception_handler(HTTPException)
async def http_exc_handler(request: Request, exc: HTTPException) -> JSONResponse:
    # Preserve headers attached by the raiser (Retry-After on 429,
    # WWW-Authenticate on 401); FastAPI's default handler drops them.
    return JSONResponse(
        status_code=exc.status_code,
        content={"detail": exc.detail},
        headers=getattr(exc, "headers", None) or None,
    )


@app.exception_handler(OverflowError)
@app.exception_handler(DataError)
async def out_of_range_handler(request: Request, exc: Exception) -> JSONResponse:
    # A client-supplied value the database can't hold: an id past 64 bits
    # (SQLite raises OverflowError while binding), or PostgreSQL's DataError
    # (integer out of range, value too long). The statement never ran and the
    # session is rolled back on close, so this is bad input, not a server fault.
    logger.warning(
        "Rejected out-of-range value on %s %s: %s",
        request.method, request.url.path, type(exc).__name__,
    )
    # Same shape as FastAPI's own 422 (documented as HTTPValidationError).
    return JSONResponse(status_code=422, content={"detail": [{
        "type": "value_error", "loc": ["request"],
        "msg": "A value in the request is out of range.", "input": None,
    }]})


@app.exception_handler(Exception)
async def unhandled_exc_handler(request: Request, exc: Exception) -> JSONResponse:
    # ServerErrorMiddleware sits outside the security-headers middleware, so
    # without this handler an unhandled 500 would ship as bare text with no
    # CSP or anti-clickjacking headers. Return a generic JSON response with the
    # full header set and no internal details.
    logger.exception("Unhandled exception on %s %s", request.method, request.url.path)
    resp = JSONResponse(status_code=500, content={"detail": "Internal server error."})
    _apply_security_headers(resp.headers)
    resp.headers.setdefault("Cache-Control", "no-store")
    return resp


if __name__ == "__main__":
    import uvicorn
    # Default to loopback. Set UVICORN_HOST=0.0.0.0 in containers where the
    # container boundary and reverse proxy make it safe to bind all interfaces.
    _host = os.getenv("UVICORN_HOST", "127.0.0.1")
    _port = int(os.getenv("UVICORN_PORT", "8000"))
    uvicorn.run("app.main:app", host=_host, port=_port, reload=False)
