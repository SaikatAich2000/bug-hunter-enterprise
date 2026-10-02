"""System-trust TLS context for every outbound Git provider call.

Why this module exists
----------------------
Corporate TLS interception (Zscaler, Netskope, a self-signed internal root,
...), or simply a Python build whose bundled ``certifi`` roots are older than
the corporate CA, makes httpx raise ``CERTIFICATE_VERIFY_FAILED: unable to get
local issuer certificate`` even though the operating system trusts the chain
perfectly — ``curl`` succeeds against the same URL.

``truststore`` fixes that correctly: instead of shipping another CA bundle it
delegates verification to the platform trust store (Windows SChannel / CryptoAPI,
macOS Keychain, OpenSSL on Linux).

Safety rules enforced here
--------------------------
* Verification is ALWAYS on. There is no code path that turns certificate
  checking off, and no environment variable can disable it.
* If the system trust store cannot be initialized, the provider fails CLOSED with
  a sanitized configuration error instead of silently falling back to an
  unverified connection.
* No exception text is ever placed in the error message; only the exception class
  name reaches the log.

The context is cached because building one re-reads the platform store; it is
thread-safe and shared by concurrent httpx clients.
"""
from __future__ import annotations

import logging
import ssl
from functools import lru_cache
from pathlib import Path

from app.git.provider import TLS_TRUST_ERROR, GitProviderError

logger = logging.getLogger("bug_hunter.git.tls")

# The one message the UI, the audit row and the API response may show for a
# certificate problem. Deliberately free of hostnames, paths and hints.
TLS_TRUST_MESSAGE = (
    "GitHub TLS certificate could not be verified using the system trust store."
)

# Markers that identify a certificate-verification failure across CPython,
# OpenSSL and urllib3 wordings. Matched case-insensitively against the exception
# chain text; the text itself is never logged or returned.
_TLS_MARKERS = (
    "certificate verify failed",
    "unable to get local issuer certificate",
    "unable to get issuer certificate",
    "self-signed certificate",
    "self signed certificate",
    "certificate has expired",
    "hostname mismatch",
    "doesn't match either of the certificate's names",
    "does not match either of the certificate's names",
)

try:  # pragma: no cover - import outcome depends on the deployment image
    import truststore as _truststore

    _TRUSTSTORE_AVAILABLE = True
    _TRUSTSTORE_IMPORT_ERROR = ""
except Exception as _import_error:  # pragma: no cover
    _truststore = None  # type: ignore[assignment]
    _TRUSTSTORE_AVAILABLE = False
    _TRUSTSTORE_IMPORT_ERROR = type(_import_error).__name__


def trust_store_available() -> bool:
    """True when the ``truststore`` dependency imported successfully."""
    return _TRUSTSTORE_AVAILABLE


def ca_bundle_configured() -> str:
    """Approved PEM bundle path, or ``""`` when the OS trust store applies."""
    try:
        from app.config import get_settings

        return (get_settings().GIT_CA_BUNDLE_FILE or "").strip()
    except Exception:  # pragma: no cover - settings always load in practice
        return ""


def _load_verified_bundle_context(bundle_path: str) -> ssl.SSLContext:
    """Verified context from an approved PEM file; fail closed on any problem."""
    candidate = (bundle_path or "").strip()
    if not candidate:
        raise GitProviderError(TLS_TRUST_ERROR, TLS_TRUST_MESSAGE)
    try:
        path = Path(candidate)
        if not path.is_file():
            raise OSError(f"CA bundle is not a file: {path.name}")
        context = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
        context.load_verify_locations(cafile=str(path))
    except GitProviderError:
        raise
    except Exception as exc:
        logger.exception(
            "git.tls.ca_bundle_failed exception=%s",
            type(exc).__name__,
        )
        raise GitProviderError(TLS_TRUST_ERROR, TLS_TRUST_MESSAGE) from None
    if context.verify_mode != ssl.CERT_REQUIRED:
        raise GitProviderError(TLS_TRUST_ERROR, TLS_TRUST_MESSAGE)
    return context


def is_certificate_verification_error(exc: BaseException) -> bool:
    """True when ``exc`` (or anything it was raised from) is a TLS trust failure.

    Walks the ``__cause__``/``__context__`` chain because httpx wraps the
    original ``ssl.SSLCertVerificationError`` inside its own ``ConnectError``.
    """
    current: BaseException | None = exc
    depth = 0
    while current is not None and depth < 12:
        if isinstance(current, ssl.SSLCertVerificationError):
            return True
        if type(current).__name__ in (
            "SSLCertVerificationError",
            "CertificateVerifyError",
        ):
            return True
        text = str(current).lower()
        if any(marker in text for marker in _TLS_MARKERS):
            return True
        current = current.__cause__ or current.__context__
        depth += 1
    return False


def _build_context() -> ssl.SSLContext:
    """Build one platform-trust context, or fail closed with a safe error."""
    if not _TRUSTSTORE_AVAILABLE:
        # Fail closed: never silently continue with an unverified connection.
        logger.error(
            "git.tls.trust_store_missing dependency=truststore reason=%s",
            _TRUSTSTORE_IMPORT_ERROR,
        )
        raise GitProviderError(TLS_TRUST_ERROR, TLS_TRUST_MESSAGE)
    try:
        return _truststore.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    except Exception as exc:  # pragma: no cover - platform-dependent
        # Only the class name is logged; the message could name internal hosts.
        logger.exception(
            "git.tls.trust_store_init_failed exception=%s reason=%s",
            type(exc).__name__,
            type(exc).__name__,
        )
        raise GitProviderError(TLS_TRUST_ERROR, TLS_TRUST_MESSAGE) from None


@lru_cache(maxsize=1)
def system_trust_context() -> ssl.SSLContext:
    """Shared verifying context: OS trust store, or the approved PEM bundle.

    Precedence: ``GIT_CA_BUNDLE_FILE`` when configured, else ``truststore``.
    Both paths verify; failures raise a sanitized trust error, never an
    unverified context.
    """
    bundle = ca_bundle_configured()
    if bundle:
        return _load_verified_bundle_context(bundle)
    return _build_context()


def reset_tls_context_cache() -> None:
    """Drop the cached context (tests, and trust-store changes without restart)."""
    system_trust_context.cache_clear()


__all__ = [
    "TLS_TRUST_MESSAGE",
    "ca_bundle_configured",
    "is_certificate_verification_error",
    "reset_tls_context_cache",
    "system_trust_context",
    "trust_store_available",
]
