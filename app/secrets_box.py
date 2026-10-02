"""Optional encryption at rest for small stored secrets (TOTP secrets, webhook signing keys).

With FIELD_ENCRYPTION_KEY set (a Fernet key), ``seal`` stores ``enc:v1:<ciphertext>``;
without it the value is stored as given. ``unseal`` reads both forms, so enabling the key
later only affects values written afterwards, and a value written under a key can never be
read back without it.
"""
from __future__ import annotations

import logging

from cryptography.fernet import Fernet, InvalidToken

from app.config import get_settings

logger = logging.getLogger("bug_hunter.secrets_box")

_PREFIX = "enc:v1:"


class SecretUnreadable(Exception):
    """A stored value is encrypted but cannot be decrypted with the configured key."""


def _fernet() -> Fernet | None:
    key = get_settings().FIELD_ENCRYPTION_KEY
    if not key:
        return None
    try:
        return Fernet(key.encode("utf-8"))
    except ValueError:
        logger.error("FIELD_ENCRYPTION_KEY is not a valid Fernet key; values are stored unencrypted")
        return None


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
