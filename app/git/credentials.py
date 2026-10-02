"""Project-scoped Git credential encryption and resolution.

Design
------
* The ciphertext lives in ``project_git_configs.credential_encrypted`` and is
  produced by Fernet (AES-128-CBC + HMAC-SHA256) with the server-only
  ``GIT_CREDENTIAL_ENCRYPTION_KEY``. The key is never derived from
  ``SESSION_SECRET`` and never leaves the process.
* A plaintext PAT is never written to the database, never logged, never audited
  and never returned by an API response — the API only ever reports the boolean
  ``credentials_configured``.
* When no encryption key is configured, projects without their own credential
  keep working through the legacy global ``GITHUB_TOKEN`` fallback, but storing a
  new project credential is refused with a clear, sanitized error.

Credential precedence (exactly, no other source exists)
------------------------------------------------------
1. Project encrypted PAT (``project_git_configs.credential_encrypted``).
2. Global ``GITHUB_TOKEN`` legacy fallback, for projects with no project PAT.
3. Not configured → the provider raises ``not_configured``.
"""
from __future__ import annotations

import logging

from app.config import get_settings
from app.git.provider import NOT_CONFIGURED, GitProviderError

logger = logging.getLogger("bug_hunter.git.credentials")

# Stable, client-safe messages. None of them ever contains a key or a token.
ENCRYPTION_KEY_MISSING_MESSAGE = (
    "Git credential encryption is not configured on the server; a project "
    "credential cannot be stored. Set GIT_CREDENTIAL_ENCRYPTION_KEY and retry."
)
ENCRYPTION_KEY_INVALID_MESSAGE = (
    "Git credential encryption is misconfigured on the server; a project "
    "credential cannot be stored. Check GIT_CREDENTIAL_ENCRYPTION_KEY."
)
CREDENTIAL_UNDECRYPTABLE_MESSAGE = (
    "The stored Git credential for this project cannot be decrypted with the "
    "current server key; re-enter the credential in Project Git Settings."
)


class CredentialError(Exception):
    """A credential-storage failure whose ``message`` is safe to expose."""

    def __init__(self, message: str) -> None:
        super().__init__(message)
        self.message = message


def encryption_available() -> bool:
    """True when a project credential could be encrypted right now."""
    return bool((get_settings().GIT_CREDENTIAL_ENCRYPTION_KEY or "").strip())


def _fernet():
    """A Fernet instance, or a CredentialError carrying a safe message."""
    key = (get_settings().GIT_CREDENTIAL_ENCRYPTION_KEY or "").strip()
    if not key:
        raise CredentialError(ENCRYPTION_KEY_MISSING_MESSAGE)
    try:
        from cryptography.fernet import Fernet
    except Exception as exc:  # pragma: no cover - dependency present in prod
        logger.exception(
            "git.credential.crypto_unavailable exception=%s", type(exc).__name__
        )
        raise CredentialError(ENCRYPTION_KEY_MISSING_MESSAGE) from None
    try:
        return Fernet(key.encode("utf-8"))
    except Exception as exc:
        # Never log the key itself — only the exception class.
        logger.exception(
            "git.credential.key_invalid exception=%s", type(exc).__name__
        )
        raise CredentialError(ENCRYPTION_KEY_INVALID_MESSAGE) from None


def encrypt_credential(plaintext: str) -> str:
    """Encrypt a PAT for storage. Raises CredentialError when unavailable."""
    value = (plaintext or "").strip()
    if not value:
        raise CredentialError("The Git credential must not be empty")
    return _fernet().encrypt(value.encode("utf-8")).decode("ascii")


def decrypt_credential(ciphertext: str) -> str:
    """Decrypt a stored PAT. Raises CredentialError on any failure."""
    stored = (ciphertext or "").strip()
    if not stored:
        raise CredentialError(CREDENTIAL_UNDECRYPTABLE_MESSAGE)
    # Key problems surface as key problems (outside the token try/except).
    fernet = _fernet()
    try:
        from cryptography.fernet import InvalidToken
    except Exception as exc:  # pragma: no cover - dependency present in prod
        logger.exception(
            "git.credential.crypto_unavailable exception=%s", type(exc).__name__
        )
        raise CredentialError(ENCRYPTION_KEY_MISSING_MESSAGE) from None
    try:
        return fernet.decrypt(stored.encode("ascii")).decode("utf-8")
    except (InvalidToken, ValueError) as exc:
        # The ciphertext, the key and the plaintext all stay out of the log.
        logger.exception(
            "git.credential.undecryptable exception=%s", type(exc).__name__
        )
        raise CredentialError(CREDENTIAL_UNDECRYPTABLE_MESSAGE) from None


def project_credential_stored(config) -> bool:
    """True when this project row carries its own encrypted credential."""
    return bool((getattr(config, "credential_encrypted", "") or "").strip())


def credentials_configured(config=None) -> bool:
    """True when *some* credential is available (project PAT or global token)."""
    if project_credential_stored(config):
        return True
    return bool((get_settings().GITHUB_TOKEN or "").strip())


def resolve_token(config) -> str | None:
    """Resolve the credential for one project. See the module docstring.

    An unreadable project credential is a hard configuration error: it never
    silently degrades to the global token, which would send Project A's requests
    to Project B's identity.
    """
    if project_credential_stored(config):
        try:
            return decrypt_credential(config.credential_encrypted)
        except CredentialError as exc:
            raise GitProviderError(NOT_CONFIGURED, exc.message) from None
    global_token = (get_settings().GITHUB_TOKEN or "").strip()
    return global_token or None


__all__ = [
    "CREDENTIAL_UNDECRYPTABLE_MESSAGE",
    "ENCRYPTION_KEY_INVALID_MESSAGE",
    "ENCRYPTION_KEY_MISSING_MESSAGE",
    "CredentialError",
    "credentials_configured",
    "decrypt_credential",
    "encrypt_credential",
    "encryption_available",
    "project_credential_stored",
    "resolve_token",
]