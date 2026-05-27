"""Configuration loaded from environment variables."""
from __future__ import annotations

import os
from functools import lru_cache
from pathlib import Path


def _env_bool(name: str, default: bool = False) -> bool:
    return os.getenv(name, str(default)).strip().lower() in ("1", "true", "yes", "on")


def _env_int(name: str, default: int) -> int:
    try:
        return int(os.getenv(name, str(default)) or str(default))
    except ValueError:
        return default


class Settings:
    BASE_DIR: Path = Path(__file__).resolve().parent.parent
    STATIC_DIR: Path = BASE_DIR / "app" / "static"

    DATABASE_URL: str = os.getenv(
        "DATABASE_URL",
        f"sqlite:///{BASE_DIR / 'bug_hunter.db'}",
    )

    CORS_ORIGINS: list[str] = [
        o.strip() for o in os.getenv("CORS_ORIGINS", "*").split(",") if o.strip()
    ]

    APP_NAME: str = os.getenv("APP_NAME", "Bug Hunter")
    APP_VERSION: str = os.getenv("APP_VERSION", "2.2")
    LOG_LEVEL: str = os.getenv("LOG_LEVEL", "INFO")

    APP_BASE_URL: str = os.getenv("APP_BASE_URL", "http://localhost:8000")

    # --- Authentication ---
    SESSION_SECRET: str = os.getenv("SESSION_SECRET", "")
    SESSION_TTL_SECONDS: int = _env_int("SESSION_TTL_SECONDS", 86400)
    COOKIE_SECURE: bool = _env_bool("COOKIE_SECURE", False)
    TRUST_PROXY_FORWARDED_FOR: bool = _env_bool("TRUST_PROXY_FORWARDED_FOR", False)

    # bcrypt cost factor. Default 10 (≈100 ms on a modern core, manageable
    # on the 0.1-vCPU deployment target). Tighten upward only if you have
    # the CPU budget for it.
    BCRYPT_ROUNDS: int = _env_int("BCRYPT_ROUNDS", 10)

    # --- Multi-tenant signup gate ---
    # When true, anyone hitting /signup can create a new organization.
    # Set to false on locked-down installs that only want invited users.
    ALLOW_PUBLIC_SIGNUP: bool = _env_bool("ALLOW_PUBLIC_SIGNUP", True)

    # --- Email ---
    EMAIL_BACKEND: str = os.getenv("EMAIL_BACKEND", "console").strip().lower()
    EMAIL_FROM: str = os.getenv("EMAIL_FROM", "bughunter@localhost")
    SMTP_HOST: str = os.getenv("SMTP_HOST", "")
    SMTP_PORT: int = _env_int("SMTP_PORT", 587)
    SMTP_USERNAME: str = os.getenv("SMTP_USERNAME", "")
    SMTP_PASSWORD: str = os.getenv("SMTP_PASSWORD", "")
    SMTP_USE_TLS: bool = _env_bool("SMTP_USE_TLS", True)
    SMTP_USE_SSL: bool = _env_bool("SMTP_USE_SSL", False)
    SMTP_TIMEOUT: int = _env_int("SMTP_TIMEOUT", 10)

    # ──────────────────────────────────────────────────────────────────
    # Enterprise features (v2.2 expansion)
    # ──────────────────────────────────────────────────────────────────

    # Forgot-password endpoint behaviour. When false (the enterprise-safe
    # default) we always return 204 for /api/auth/forgot-password — no
    # information about whether the email exists is leaked. When true we
    # return 404 for unknown emails, which is friendlier UX but allows
    # account enumeration. Flip to true only if your threat model permits.
    ALLOW_ACCOUNT_ENUMERATION: bool = _env_bool("ALLOW_ACCOUNT_ENUMERATION", False)

    # CSRF defence-in-depth on top of SameSite=Lax. When enabled, all
    # state-changing API requests (POST/PUT/PATCH/DELETE) must echo back
    # the value of the `bh_csrf` cookie in an `X-CSRF-Token` header. The
    # SPA's fetch wrapper sets this automatically; cross-origin callers
    # have to opt in by reading their own cookie first, which is what
    # makes the double-submit pattern an effective defence.
    CSRF_PROTECTION: bool = _env_bool("CSRF_PROTECTION", True)

    # Structured JSON logging. When true, every log line is emitted as
    # one-line JSON — request_id, user_id, org_id, latency_ms — so SIEM /
    # log-aggregation tools can index it natively. When false we keep the
    # default human-readable text format (useful in dev).
    JSON_LOGGING: bool = _env_bool("JSON_LOGGING", False)

    # /metrics endpoint exposing Prometheus-format counters for request
    # counts, latency buckets, queue depth (when a queue is configured).
    # Off by default so anonymous scrapers can't fingerprint deployments.
    METRICS_ENABLED: bool = _env_bool("METRICS_ENABLED", False)
    # Optional shared secret — if set, /metrics requires
    # `Authorization: Bearer <secret>`. Without it (and with
    # METRICS_ENABLED=true) the endpoint is open, which is fine on a
    # private network but not on a public host.
    METRICS_TOKEN: str = os.getenv("METRICS_TOKEN", "")

    # 2FA (TOTP) — turn on enrolment endpoints / login-step check.
    # Enabled by default at the platform level; each user still has to
    # opt-in via the profile page. Set false to disable site-wide
    # (useful in air-gapped installs).
    TOTP_ENABLED: bool = _env_bool("TOTP_ENABLED", True)
    # Recovery code count for the 2FA backup-code feature. Each is a
    # single-use 10-character string.
    TOTP_RECOVERY_CODE_COUNT: int = _env_int("TOTP_RECOVERY_CODE_COUNT", 10)

    # Audit log retention — rows older than this many days are purged
    # on a periodic sweep (runs at startup + every 24h). Set to 0 to
    # disable purging (keep everything indefinitely). Default 365 days
    # covers common compliance windows (SOC 2, ISO 27001).
    AUDIT_RETENTION_DAYS: int = _env_int("AUDIT_RETENTION_DAYS", 365)

    # Webhook delivery timeout, in seconds. We don't queue webhook
    # deliveries (yet) — they fire in a background task at request
    # commit time. Keep this short so a slow listener can't tie up
    # request workers.
    WEBHOOK_TIMEOUT_SECONDS: int = _env_int("WEBHOOK_TIMEOUT_SECONDS", 8)
    # Hard cap on webhook URL length to avoid abuse / storage bloat.
    WEBHOOK_MAX_URL_LENGTH: int = _env_int("WEBHOOK_MAX_URL_LENGTH", 500)


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings()
