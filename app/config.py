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
    APP_VERSION: str = os.getenv("APP_VERSION", "4.0.0")
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


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings()
