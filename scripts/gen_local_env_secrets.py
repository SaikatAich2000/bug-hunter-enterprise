"""Generate strong local-only secrets for .env without printing them.

Usage (from the repo root):
    .venv\\Scripts\\python.exe scripts/gen_local_env_secrets.py

Creates .env from .env.example when it does not exist yet, then fills
SESSION_SECRET, BOOTSTRAP_ADMIN_PASSWORD, POSTGRES_PASSWORD and
GIT_CREDENTIAL_ENCRYPTION_KEY when they are blank or still hold a placeholder.
Existing real values are preserved, so it is safe to re-run (a changed
POSTGRES_PASSWORD would lock the app out of an existing database volume).
Prints key names only, never values.
"""
from __future__ import annotations

import base64
import secrets
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

# Letters, digits and a few symbols that are safe unquoted in .env for both
# python-dotenv and Docker Compose ($ would be interpolated, # may start a
# comment) and inside the postgresql:// URL Compose builds (@ # % / : ? break it).
_PASSWORD_SYMBOLS = "-_!*"
_PASSWORD_ALPHABET = (
    "abcdefghijkmnopqrstuvwxyzABCDEFGHJKLMNPQRSTUVWXYZ23456789" + _PASSWORD_SYMBOLS
)

# Substrings that mark a value as a template placeholder rather than a secret.
_PLACEHOLDER_MARKERS = (
    "replace_with",
    "change-me",
    "change_me",
    "changeme",
    "your_random",
    "bugtracker_pw",
)


def _strong_password() -> str:
    while True:
        candidate = "".join(secrets.choice(_PASSWORD_ALPHABET) for _ in range(24))
        if (
            any(c.islower() for c in candidate)
            and any(c.isupper() for c in candidate)
            and any(c.isdigit() for c in candidate)
            and any(c in _PASSWORD_SYMBOLS for c in candidate)
        ):
            return candidate


def _fernet_key() -> str:
    # Same format as cryptography.fernet.Fernet.generate_key(), without the import.
    return base64.urlsafe_b64encode(secrets.token_bytes(32)).decode("ascii")


_GENERATORS = {
    "SESSION_SECRET": lambda: secrets.token_hex(32),
    "BOOTSTRAP_ADMIN_PASSWORD": _strong_password,
    "POSTGRES_PASSWORD": _strong_password,
    "GIT_CREDENTIAL_ENCRYPTION_KEY": _fernet_key,
}


def _needs_value(value: str) -> bool:
    cleaned = value.strip().strip("\"'")
    lowered = cleaned.lower()
    return len(cleaned) < 16 or any(marker in lowered for marker in _PLACEHOLDER_MARKERS)


def main() -> int:
    env_path = ROOT / ".env"
    if not env_path.exists():
        env_path.write_text(
            (ROOT / ".env.example").read_text(encoding="utf-8"), encoding="utf-8"
        )
        print("created .env from .env.example")
    lines = env_path.read_text(encoding="utf-8").splitlines()
    updated: list[str] = []
    for index, line in enumerate(lines):
        stripped = line.strip()
        if not stripped or stripped.startswith("#") or "=" not in stripped:
            continue
        key, value = (part.strip() for part in stripped.split("=", 1))
        if key in _GENERATORS and key not in updated and _needs_value(value):
            lines[index] = f"{key}={_GENERATORS[key]()}"
            updated.append(key)
    env_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    for key in updated:
        print(f"updated {key}")
    if not updated:
        print("no placeholder secrets needed replacement")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
