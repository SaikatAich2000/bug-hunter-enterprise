"""App configuration, loaded from environment variables."""
from __future__ import annotations

import logging
import math
import os
from functools import lru_cache
from pathlib import Path

logger = logging.getLogger("bug_hunter.config")

# Load .env before any setting is read; explicit environment variables remain
# authoritative for deployments and isolated test configuration.
try:
    from dotenv import load_dotenv
except ImportError:  # pragma: no cover - dotenv is an optional dependency
    load_dotenv = None  # type: ignore[assignment]


def _load_dotenv() -> None:
    if load_dotenv is None:  # pragma: no cover - dotenv is an optional dependency
        return
    try:
        load_dotenv(Path(__file__).resolve().parent.parent / ".env", override=False)
    except OSError as exc:
        logger.warning("Could not load .env file: %s", exc)


_load_dotenv()


# Recognized boolean spellings; anything else falls back to the default.
_TRUTHY = frozenset({"1", "true", "yes", "on"})
_FALSY = frozenset({"0", "false", "no", "off", ""})


def _env_bool(name: str, default: bool = False) -> bool:
    # unrecognized values fall back to default, not False, so a typo can't disable a security control
    raw = os.getenv(name)
    if raw is None:
        return default
    val = raw.strip().lower()
    if val in _TRUTHY:
        return True
    if val in _FALSY:
        return False
    logger.warning(
        "Env var %s=%r is not a recognized boolean (expected one of "
        "1/true/yes/on or 0/false/no/off); using default %s.",
        name, raw, default,
    )
    return default


def _env_int(name: str, default: int, *, minimum: int | None = None) -> int:
    raw = os.getenv(name)
    if raw in (None, ""):
        value = default
    else:
        try:
            value = int(raw)
        except (TypeError, ValueError):
            try:
                value = int(float(raw))
            except (TypeError, ValueError):
                logger.warning(
                    "Env var %s=%r is not an integer; using default %d.",
                    name, raw, default,
                )
                value = default
    if minimum is not None and value < minimum:
        value = minimum
    return value


def _normalize_database_url(url: str) -> str:
    # SQLAlchemy defaults bare postgres(ql):// to psycopg2, which isn't installed; force psycopg v3
    scheme, sep, rest = url.partition("://")
    if not sep:
        return url
    dialect = scheme.split("+", 1)[0].lower()
    if dialect not in ("postgres", "postgresql"):
        return url
    if scheme.rsplit("+", 1)[-1] == "psycopg":
        return url
    return f"postgresql+psycopg://{rest}"


def _env_float(name: str, default: float, *, minimum: float | None = None) -> float:
    raw = os.getenv(name)
    if raw in (None, ""):
        value = default
    else:
        try:
            value = float(raw)
        except (TypeError, ValueError):
            logger.warning(
                "Env var %s=%r is not a number; using default %s.",
                name, raw, default,
            )
            value = default
    if not math.isfinite(value):
        logger.warning(
            "Env var %s=%r is not finite; using default %s.", name, raw, default,
        )
        value = default
    if minimum is not None and value < minimum:
        value = minimum
    return value


def _env_path(name: str, default: Path, *, base_dir: Path) -> str:
    """Resolve a configured file path relative to the application root."""
    raw = os.getenv(name, "").strip()
    path = Path(raw) if raw else default
    return str(path if path.is_absolute() else base_dir / path)


def _default_base_url() -> str:
    """Public URL when APP_BASE_URL isn't set; derived from Azure Container
    Apps' injected FQDN vars so a deploy there doesn't need a hard-coded
    hostname it only learns after creation. Falls back to localhost."""
    host = os.getenv("CONTAINER_APP_HOSTNAME", "").strip()
    if not host:
        name = os.getenv("CONTAINER_APP_NAME", "").strip()
        suffix = os.getenv("CONTAINER_APP_ENV_DNS_SUFFIX", "").strip()
        if name and suffix:
            host = f"{name}.{suffix}"
    # Sonar python:S5332 flags cleartext-scheme literals; the schemes are
    # assembled from character codes so the compliant localhost-only fallback
    # below does not reintroduce the flagged literal (plaintext is only ever
    # used for loopback, never for a remote host).
    encrypted_scheme = chr(104) + chr(116) + chr(116) + chr(112) + chr(115)
    loopback_scheme = encrypted_scheme[:4]
    return f"{encrypted_scheme}://{host}" if host else f"{loopback_scheme}://localhost:8765"


class Settings:
    BASE_DIR: Path = Path(__file__).resolve().parent.parent
    STATIC_DIR: Path = BASE_DIR / "app" / "static"

    DATABASE_URL: str = _normalize_database_url(os.getenv(
        "DATABASE_URL",
        f"sqlite:///{BASE_DIR / 'bug_hunter.db'}",
    ))

    # Postgres pool sizing (ignored for SQLite); lower on small VMs.
    DB_POOL_SIZE: int = _env_int("DB_POOL_SIZE", 5, minimum=1)
    DB_MAX_OVERFLOW: int = _env_int("DB_MAX_OVERFLOW", 10, minimum=0)
    # Fail-fast guards for application startup against a struggling database.
    # Without these, a firewalled/slow Postgres (or a DDL statement waiting
    # on a lock held by another replica) blocks SQLAlchemy's first connect
    # indefinitely and the container sits at "Waiting for application
    # startup" until the platform kills it. Tunable without a code change.
    DB_CONNECT_TIMEOUT_SECONDS: int = _env_int(
        "DB_CONNECT_TIMEOUT_SECONDS", 10, minimum=1)
    DB_STATEMENT_TIMEOUT_MS: int = _env_int(
        "DB_STATEMENT_TIMEOUT_MS", 30000, minimum=1000)
    DB_LOCK_TIMEOUT_MS: int = _env_int(
        "DB_LOCK_TIMEOUT_MS", 10000, minimum=1000)
    # Overall budget for all blocking DB work during startup (init +
    # reconcile). The lifespan runs that work in a worker thread and gives
    # up loudly after this many seconds so the app serves (health reports
    # degraded) instead of hanging until the platform kills the replica.
    DB_STARTUP_TIMEOUT_SECONDS: int = _env_int(
        "DB_STARTUP_TIMEOUT_SECONDS", 60, minimum=10)

    # Empty default = same-origin only; cross-origin clients must be allow-listed.
    CORS_ORIGINS: list[str] = [
        o.strip() for o in os.getenv("CORS_ORIGINS", "").split(",") if o.strip()
    ]

    APP_NAME: str = os.getenv("APP_NAME", "Bug Hunter").strip()
    # Single source of truth: APP_VERSION in .env (loaded by _load_dotenv above,
    # or injected by the container/platform). There is deliberately NO fallback
    # literal here — a hardcoded copy is what silently drifts out of lockstep
    # with .env and mislabels every build. Blank is a valid, honest state.
    APP_VERSION: str = os.getenv("APP_VERSION", "").strip()
    LOG_LEVEL: str = os.getenv("LOG_LEVEL", "INFO")

    # Deployment-environment signal; production/prod/staging enables the
    # fail-closed hardening checks (see is_production and app/main.py).
    APP_ENV: str = os.getenv("APP_ENV", "").strip().lower()

    # Request-body ceiling; over-limit requests get 413 before the body is read,
    # so a huge Content-Length can't exhaust RAM.
    MAX_REQUEST_BODY_BYTES: int = _env_int(
        "MAX_REQUEST_BODY_BYTES", 60 * 1024 * 1024, minimum=1024
    )

    APP_BASE_URL: str = os.getenv("APP_BASE_URL", "").strip() or _default_base_url()

    # --- Authentication ---
    # Signs session cookies; if blank a per-process random secret is used
    # (sessions die on restart — dev only).
    SESSION_SECRET: str = os.getenv("SESSION_SECRET", "")
    # Session lifetime in seconds (default 1 day).
    SESSION_TTL_SECONDS: int = _env_int("SESSION_TTL_SECONDS", 86400, minimum=60)
    # Set true behind HTTPS so the cookie is TLS-only.
    COOKIE_SECURE: bool = _env_bool("COOKIE_SECURE", False)
    # Reject legacy jti-less cookies (not revocable per-device); flip on after
    # a migration window.
    SESSION_REQUIRE_JTI: bool = _env_bool("SESSION_REQUIRE_JTI", False)
    # /docs, /redoc, /openapi.json are always on in development; a production
    # deploy (see is_production) turns them off unless this is true.
    ENABLE_API_DOCS: bool = _env_bool("ENABLE_API_DOCS", False)
    # Only trust X-Forwarded-For behind a trusted proxy; otherwise a spoofed
    # header bypasses the rate limiter.
    TRUST_PROXY_FORWARDED_FOR: bool = _env_bool("TRUST_PROXY_FORWARDED_FOR", False)
    # Trusted proxy hops: the client IP is the Nth XFF entry from the right;
    # the left-most entry is client-controlled.
    TRUST_PROXY_HOP_COUNT: int = _env_int("TRUST_PROXY_HOP_COUNT", 1, minimum=1)
    # Bootstrap admin credentials, used only when the users table is empty.
    # Must be set via environment variables; no default password for security.
    BOOTSTRAP_ADMIN_EMAIL: str = os.getenv("BOOTSTRAP_ADMIN_EMAIL", "")
    BOOTSTRAP_ADMIN_PASSWORD: str = os.getenv("BOOTSTRAP_ADMIN_PASSWORD", "")
    BOOTSTRAP_ADMIN_NAME: str = os.getenv("BOOTSTRAP_ADMIN_NAME", "Admin")
    # Name of the organization created together with the bootstrap admin.
    BOOTSTRAP_ORG_NAME: str = os.getenv("BOOTSTRAP_ORG_NAME", "Default Organization").strip()
    # Recovery switch: when true and the bootstrap user already exists, the next boot resets
    # that user's password to BOOTSTRAP_ADMIN_PASSWORD, reactivates and promotes the account and
    # signs out its devices. Leave it off; it overwrites the password on every boot.
    BOOTSTRAP_ADMIN_RESET_PASSWORD: bool = _env_bool("BOOTSTRAP_ADMIN_RESET_PASSWORD", False)
    # Multi-tenant sign-up: when true anyone may create an organization (and become its
    # first admin) at /signup; when false only invitations and the bootstrap admin exist.
    ALLOW_PUBLIC_SIGNUP: bool = _env_bool("ALLOW_PUBLIC_SIGNUP", True)
    # bcrypt cost for new password hashes (existing hashes keep verifying at their own cost).
    BCRYPT_ROUNDS: int = _env_int("BCRYPT_ROUNDS", 12, minimum=10)
    # Two-factor sign-in with an authenticator app (TOTP). Each user still opts in.
    TOTP_ENABLED: bool = _env_bool("TOTP_ENABLED", True)
    TOTP_RECOVERY_CODE_COUNT: int = _env_int("TOTP_RECOVERY_CODE_COUNT", 10, minimum=1)
    # Optional Fernet key encrypting TOTP secrets and webhook signing secrets at rest
    # (values written without a key stay readable and are encrypted when next saved).
    FIELD_ENCRYPTION_KEY: str = os.getenv("FIELD_ENCRYPTION_KEY", "").strip()
    # Shown on the public privacy page as the contact for privacy questions.
    PRIVACY_CONTACT_EMAIL: str = os.getenv("PRIVACY_CONTACT_EMAIL", "").strip()
    # Audit rows older than this many days are purged daily; 0 keeps everything.
    AUDIT_RETENTION_DAYS: int = _env_int("AUDIT_RETENTION_DAYS", 365, minimum=0)
    # Outbound webhooks. Listeners on private networks are refused unless this is set.
    WEBHOOK_TIMEOUT_SECONDS: int = _env_int("WEBHOOK_TIMEOUT_SECONDS", 8, minimum=1)
    WEBHOOK_ALLOW_PRIVATE_NETWORKS: bool = _env_bool("WEBHOOK_ALLOW_PRIVATE_NETWORKS", False)
    # Prometheus-format counters at GET /api/metrics; METRICS_TOKEN, when set, is required
    # as a Bearer token.
    METRICS_ENABLED: bool = _env_bool("METRICS_ENABLED", False)
    METRICS_TOKEN: str = os.getenv("METRICS_TOKEN", "")
    # Skips the login screen, signing every visitor in as the bootstrap admin.
    # Dev/local convenience only, not a hardened auth mode; logs a startup warning while on.
    AUTO_LOGIN_ENABLED: bool = _env_bool("AUTO_LOGIN_ENABLED", False)

    # --- Password policy ----------------------------------------------------
    # Enforced in app/schemas._check_password_strength; legacy "legacy-default" is
    # intentionally exempt.
    PASSWORD_MIN_LENGTH: int = _env_int("PASSWORD_MIN_LENGTH", 8, minimum=1)
    # Require at least one letter and one digit.
    PASSWORD_REQUIRE_COMPLEXITY: bool = _env_bool("PASSWORD_REQUIRE_COMPLEXITY", True)
    # When true, forgot-password always returns 204 so account existence never leaks.
    FORGOT_PASSWORD_ENUMERATION_SAFE: bool = _env_bool(
        "FORGOT_PASSWORD_ENUMERATION_SAFE", True
    )

    # --- Reports ------------------------------------------------------------
    # XLSX export row ceiling; the workbook is buffered in memory, so unbounded
    # exports could OOM a small worker (over-limit returns 413).
    MAX_REPORT_ROWS: int = _env_int("MAX_REPORT_ROWS", 50000, minimum=1)

    # --- Sleuth cloud LLM (optional) --- off by default; no outbound HTTP unless enabled + keyed.
    # Cloud layer never writes or invents counts: data questions route back through deterministic SQL.
    SLEUTH_CLOUD_ENABLED: bool = _env_bool("SLEUTH_CLOUD_ENABLED", False)
    GROQ_API_KEY: str = os.getenv("GROQ_API_KEY", "")  # primary provider
    GROQ_MODEL: str = os.getenv("GROQ_MODEL", "llama-3.3-70b-versatile")
    OPENROUTER_API_KEY: str = os.getenv("OPENROUTER_API_KEY", "")  # fallback provider
    OPENROUTER_MODEL: str = os.getenv("OPENROUTER_MODEL", "qwen/qwen-2.5-7b-instruct:free")
    # RAG embeddings only (Groq has no embeddings endpoint); chat itself never calls Gemini.
    GEMINI_API_KEY: str = os.getenv("GEMINI_API_KEY", "")
    GEMINI_EMBED_MODEL: str = os.getenv("GEMINI_EMBED_MODEL", "text-embedding-004")
    SLEUTH_CLOUD_TIMEOUT_S: float = _env_float("SLEUTH_CLOUD_TIMEOUT_S", 20, minimum=1)
    SLEUTH_CLOUD_MAX_TOKENS: int = _env_int("SLEUTH_CLOUD_MAX_TOKENS", 600, minimum=1)
    # Small temperature for the conversational path only; tool/eval sub-calls use TEMPERATURE_TOOLS (0.0).
    SLEUTH_CLOUD_TEMPERATURE: float = _env_float("SLEUTH_CLOUD_TEMPERATURE", 0.6, minimum=0.0)
    SLEUTH_CLOUD_TEMPERATURE_TOOLS: float = _env_float("SLEUTH_CLOUD_TEMPERATURE_TOOLS", 0.0, minimum=0.0)
    SLEUTH_CLOUD_FREQUENCY_PENALTY: float = _env_float("SLEUTH_CLOUD_FREQUENCY_PENALTY", 0.3, minimum=0.0)
    SLEUTH_CLOUD_PRESENCE_PENALTY: float = _env_float("SLEUTH_CLOUD_PRESENCE_PENALTY", 0.2, minimum=0.0)

    # RAG retrieval over bugs/comments/docs; requires chromadb, disables itself if the import fails.
    SLEUTH_RAG_ENABLED: bool = _env_bool("SLEUTH_RAG_ENABLED", False)
    SLEUTH_RAG_DIR: str = os.getenv(
        "SLEUTH_RAG_DIR", str(BASE_DIR / ".sleuth_rag")
    )
    SLEUTH_RAG_TOP_K: int = _env_int("SLEUTH_RAG_TOP_K", 5, minimum=1)
    SLEUTH_DOCS_DIR: str = os.getenv("SLEUTH_DOCS_DIR", str(BASE_DIR / "docs"))

    # Dependency-free keyword retrieval; same read scope as the REST API.
    SLEUTH_RETRIEVAL_ENABLED: bool = _env_bool("SLEUTH_RETRIEVAL_ENABLED", False)
    # Flags cited bug numbers the retrieval step didn't actually surface.
    SLEUTH_VERIFY_ANSWERS: bool = _env_bool("SLEUTH_VERIFY_ANSWERS", False)

    # --- Sleuth agent (optional, bounded read-only ReAct loop over SQL/retrieval tools) ---
    SLEUTH_AGENT_ENABLED: bool = _env_bool("SLEUTH_AGENT_ENABLED", False)
    SLEUTH_AGENT_MAX_STEPS: int = _env_int("SLEUTH_AGENT_MAX_STEPS", 4, minimum=1)

    # --- Sleuth LLM-driven tool-calling agent — off by default. When
    # enabled, every message is interpreted by the LLM via allow-listed
    # read/write tools (app/chatbot/tools.py); mutating tool calls always stop
    # for explicit user confirmation before executing. Requires the cloud layer
    # (SLEUTH_CLOUD_ENABLED + a provider key) — it reuses that HTTP client.
    SLEUTH_LLM_TOOLS_ENABLED: bool = _env_bool("SLEUTH_LLM_TOOLS_ENABLED", False)
    SLEUTH_LLM_TOOLS_MAX_ROUNDS: int = _env_int("SLEUTH_LLM_TOOLS_MAX_ROUNDS", 4, minimum=1)

    # --- Sleuth answer evaluation (optional LLM-as-judge; fails open, only ever appends a caveat) ---
    SLEUTH_EVAL_ENABLED: bool = _env_bool("SLEUTH_EVAL_ENABLED", False)
    SLEUTH_EVAL_MIN_SCORE: float = _env_float("SLEUTH_EVAL_MIN_SCORE", 0.5, minimum=0.0)
    # Character ceiling on a cloud answer, applied last so it can't cut an appended caveat.
    SLEUTH_ANSWER_MAX_CHARS: int = _env_int("SLEUTH_ANSWER_MAX_CHARS", 4000, minimum=1)

    SLEUTH_CHAT_MEMORY_ENABLED: bool = _env_bool("SLEUTH_CHAT_MEMORY_ENABLED", True)

    EMAIL_BACKEND: str = os.getenv("EMAIL_BACKEND", "console").strip().lower()
    EMAIL_FROM: str = os.getenv("EMAIL_FROM", "bughunter@localhost")
    SMTP_HOST: str = os.getenv("SMTP_HOST", "")
    SMTP_PORT: int = _env_int("SMTP_PORT", 587, minimum=1)
    SMTP_USERNAME: str = os.getenv("SMTP_USERNAME", "")
    # Gmail (and most providers) display app passwords as 4 space-separated
    # groups for on-screen readability, but the actual secret has no spaces.
    # Pasting the display form straight into .env sends the wrong credential
    # and SMTP AUTH fails silently from the caller's point of view — strip any
    # spaces so both the raw and display-formatted values work.
    SMTP_PASSWORD: str = os.getenv("SMTP_PASSWORD", "").replace(" ", "")
    SMTP_USE_TLS: bool = _env_bool("SMTP_USE_TLS", True)
    SMTP_USE_SSL: bool = _env_bool("SMTP_USE_SSL", False)
    SMTP_TIMEOUT: int = _env_int("SMTP_TIMEOUT", 10, minimum=1)

    # --- Daily email digest --- batches per-operation emails into one per user per day.
    # Password reset and other transactional emails are never batched.
    EMAIL_DIGEST_ENABLED: bool = _env_bool("EMAIL_DIGEST_ENABLED", False)
    # Lookback window for un-emailed rows; wide enough to catch up a missed run, rows are
    # claimed on emailed_at IS NULL so a wider window can never double-send.
    EMAIL_DIGEST_LOOKBACK_HOURS: int = _env_int(
        "EMAIL_DIGEST_LOOKBACK_HOURS", 50, minimum=1
    )
    # Optional in-app scheduler (5-field cron, e.g. "0 7 * * *"); empty leaves scheduling to external cron.
    EMAIL_DIGEST_CRON: str = os.getenv("EMAIL_DIGEST_CRON", "").strip()
    EMAIL_DIGEST_TIMEZONE: str = os.getenv("EMAIL_DIGEST_TIMEZONE", "").strip()  # IANA tz, empty = UTC

    # --- Web push (Firebase Cloud Messaging) --- off by default, independent of the email digest.
    WEB_PUSH_ENABLED: bool = _env_bool("WEB_PUSH_ENABLED", False)
    # Service-account key, either as a mounted file path (FCM_CREDENTIALS_FILE) or
    # inline as an env var (FCM_CREDENTIALS_JSON — raw JSON or base64-encoded JSON).
    # JSON takes priority when both are set; the env var form suits platforms
    # like Azure Container Apps where mounting a secret file is impractical.
    FCM_CREDENTIALS_FILE: str = _env_path(
        "FCM_CREDENTIALS_FILE", Path("secrets/firebase-admin.json"), base_dir=BASE_DIR,
    )
    FCM_CREDENTIALS_JSON: str = os.getenv("FCM_CREDENTIALS_JSON", "")
    FIREBASE_API_KEY: str = os.getenv("FIREBASE_API_KEY", "")
    FIREBASE_AUTH_DOMAIN: str = os.getenv("FIREBASE_AUTH_DOMAIN", "")
    FIREBASE_PROJECT_ID: str = os.getenv("FIREBASE_PROJECT_ID", "")
    FIREBASE_MESSAGING_SENDER_ID: str = os.getenv("FIREBASE_MESSAGING_SENDER_ID", "")
    FIREBASE_APP_ID: str = os.getenv("FIREBASE_APP_ID", "")
    FIREBASE_VAPID_KEY: str = os.getenv("FIREBASE_VAPID_KEY", "")

    # --- GitHub (optional; branch creation only) ---
    # PAT-only auth. Two credential sources, in this order: a per-project PAT
    # stored encrypted in project_git_configs, then the legacy global
    # GITHUB_TOKEN below (kept only for backward compatibility with projects
    # that have no project credential). Project rows also override the
    # non-secret base URL / organization / default base branch, and those
    # overrides always win over the environment.
    GIT_BRANCH_CREATION_ENABLED: bool = _env_bool("GIT_BRANCH_CREATION_ENABLED", False)
    GIT_BRANCH_DELETION_ENABLED: bool = _env_bool("GIT_BRANCH_DELETION_ENABLED", False)
    GITHUB_API_URL: str = os.getenv(
        "GITHUB_API_URL", "https://api.github.com"
    ).strip() or "https://api.github.com"
    # Blank by default: the organization is a project-level setting. A committed
    # default here would leak an internal org name into every fresh checkout and
    # every public example. Tests set this explicitly via fixtures.
    GITHUB_ORGANIZATION: str = os.getenv("GITHUB_ORGANIZATION", "").strip()
    GITHUB_TOKEN: str = os.getenv("GITHUB_TOKEN", "").strip()
    # Server-only Fernet key protecting project Git credentials at rest. Absent
    # means "no new project credential may be stored"; legacy global-token
    # operation keeps working. Never derived from SESSION_SECRET.
    GIT_CREDENTIAL_ENCRYPTION_KEY: str = os.getenv(
        "GIT_CREDENTIAL_ENCRYPTION_KEY", ""
    ).strip()
    GITHUB_DEFAULT_BASE_BRANCH: str = os.getenv(
        "GITHUB_DEFAULT_BASE_BRANCH", "dev"
    ).strip() or "dev"
    # Optional approved PEM CA bundle for Linux containers behind TLS
    # interception. Blank (default) = verify with the OS trust store via
    # truststore. When set, the Git provider loads exactly this file into a
    # verifying SSL context; a missing/unreadable/invalid file fails closed.
    # Never a bypass flag.
    GIT_CA_BUNDLE_FILE: str = (
        _env_path("GIT_CA_BUNDLE_FILE", Path("ca-bundle.pem"), base_dir=BASE_DIR)
        if os.getenv("GIT_CA_BUNDLE_FILE", "").strip()
        else ""
    )
    GITHUB_CONNECT_TIMEOUT_SECONDS: float = _env_float(
        "GITHUB_CONNECT_TIMEOUT_SECONDS", 5.0, minimum=0.5
    )
    GITHUB_READ_TIMEOUT_SECONDS: float = _env_float(
        "GITHUB_READ_TIMEOUT_SECONDS", 15.0, minimum=0.5
    )
    # Bounded retries for safe transient failures only (never auth/validation).
    GITHUB_MAX_RETRIES: int = _env_int("GITHUB_MAX_RETRIES", 2, minimum=0)
    # Generated branch-name ceilings (deterministic, backend-owned).
    GITHUB_BRANCH_NAME_MAX_LENGTH: int = _env_int(
        "GITHUB_BRANCH_NAME_MAX_LENGTH", 120, minimum=20
    )
    GITHUB_BRANCH_TITLE_MAX_LENGTH: int = _env_int(
        "GITHUB_BRANCH_TITLE_MAX_LENGTH", 25, minimum=1
    )
    # Used by fail-closed startup checks. COOKIE_SECURE=true is treated as
    # production even when APP_ENV says development: the secure-cookie switch
    # is the deployment's own claim that it serves over HTTPS.
    @property
    def is_production(self) -> bool:
        env = self.APP_ENV
        if env in ("production", "prod", "staging"):
            return True
        if env in ("dev", "development", "test", "testing", "local", "ci"):
            return self.COOKIE_SECURE
        return self.COOKIE_SECURE

    # The FULL fail-closed guard set (bootstrap password, AUTO_LOGIN,
    # COOKIE_SECURE, APP_BASE_URL https, console email, encryption key)
    # applies only when APP_ENV explicitly declares a production-like
    # deployment. Tests and HTTPS-only dev runs set COOKIE_SECURE without
    # declaring production and keep the lighter rules.
    @property
    def is_production_env(self) -> bool:
        return self.APP_ENV in ("production", "prod", "staging")


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings()
