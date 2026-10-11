"""Optional encryption at rest for small stored secrets (TOTP secrets, webhook signing keys).

With FIELD_ENCRYPTION_KEY set (a Fernet key; production requires it), ``seal`` stores
``enc:v1:<ciphertext>``; without it (development only) the value is stored as given. Old keys
go in FIELD_ENCRYPTION_KEY_PREVIOUS while rotating; a malformed key raises instead of
silently storing plaintext. ``unseal`` reads both forms, so enabling the key
later only affects values written afterwards, and a value written under a key can never be
read back without it.
"""
from __future__ import annotations

import logging

from cryptography.fernet import Fernet, InvalidToken, MultiFernet

from app.config import get_settings

logger = logging.getLogger("bug_hunter.secrets_box")

_PREFIX = "enc:v1:"


class SecretUnreadable(Exception):
    """A stored value is encrypted but cannot be decrypted with the configured key."""


class SecretKeyInvalid(RuntimeError):
    """FIELD_ENCRYPTION_KEY (or a previous key) is not a valid Fernet key. Never downgraded to
    storing plaintext: the caller fails instead."""


def _fernet() -> MultiFernet | None:
    """The configured key first (it encrypts), then FIELD_ENCRYPTION_KEY_PREVIOUS (comma-
    separated; they only decrypt), so a key can be rotated without losing stored values."""
    settings = get_settings()
    key = settings.FIELD_ENCRYPTION_KEY
    if not key:
        return None
    previous = [k.strip() for k in (settings.FIELD_ENCRYPTION_KEY_PREVIOUS or "").split(",") if k.strip()]
    try:
        return MultiFernet([Fernet(k.encode("utf-8")) for k in (key, *previous)])
    except ValueError as exc:
        raise SecretKeyInvalid("FIELD_ENCRYPTION_KEY / FIELD_ENCRYPTION_KEY_PREVIOUS is not a valid Fernet key") from exc


def valid_key_problem(key: str, previous: str = "") -> str | None:
    """Why this key setting cannot be used, or None (for the production startup check)."""
    for k in (key, *[p.strip() for p in previous.split(",") if p.strip()]):
        try:
            Fernet(k.encode("utf-8"))
        except ValueError:
            return "is not a valid Fernet key (generate one with Fernet.generate_key())"
    return None


def is_sealed(stored: str) -> bool:
    return stored.startswith(_PREFIX)


def seal(plain: str) -> str:
    """The value to store: encrypted when a key is configured."""
    fernet = _fernet()
    if fernet is None:
        return plain
    return _PREFIX + fernet.encrypt(plain.encode("utf-8")).decode("ascii")


def unseal(stored: str) -> str:
    """The plain value of something ``seal`` produced (or of a legacy plain value)."""
    if not stored.startswith(_PREFIX):
        return stored
    fernet = _fernet()
    if fernet is None:
        raise SecretUnreadable("the value is encrypted but FIELD_ENCRYPTION_KEY is not set")
    try:
        return fernet.decrypt(stored[len(_PREFIX):].encode("ascii")).decode("utf-8")
    except InvalidToken as exc:
        raise SecretUnreadable("the value cannot be decrypted with FIELD_ENCRYPTION_KEY") from exc
