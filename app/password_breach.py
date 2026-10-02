"""HaveIBeenPwned breach check via the k-anonymity API.

Only the first 5 hash chars leave the box; the suffix list is checked locally.
Fails open (a network/timeout/non-200 error lets the password through) so an
API outage never blocks a change. Disable with PASSWORD_BREACH_CHECK_ENABLED=false.
"""
from __future__ import annotations

import hashlib
import logging
import os

import httpx

logger = logging.getLogger("bug_hunter.password_breach")

_API_URL = "https://api.pwnedpasswords.com/range/"
_TIMEOUT_SECONDS = 3.0

# 'legacy-default' is the legacy password (always accepted by the strength
# validator), so skip the breach gate even though it's in the HIBP corpus.
_ALWAYS_ALLOWED = frozenset({"legacy-default", "changeme"})


def _env_bool(name: str, default: bool) -> bool:
    return os.getenv(name, str(default)).strip().lower() in ("1", "true", "yes", "on")


def _is_enabled() -> bool:
    return _env_bool("PASSWORD_BREACH_CHECK_ENABLED", True)


def _sha1_hex(plain: str) -> str:
    # SHA-1 is the HIBP k-anonymity lookup key, not a credential hash (that's bcrypt).
    return hashlib.sha1(plain.encode("utf-8"), usedforsecurity=False).hexdigest().upper()  # nosec B324  # NOSONAR(S4790)


def _fetch_range(prefix: str) -> str | None:
    """Return the raw body for /range/{prefix}, or None on failure (test seam)."""
    try:
        with httpx.Client(timeout=_TIMEOUT_SECONDS) as client:
            resp = client.get(
                _API_URL + prefix,
                headers={"Add-Padding": "true", "User-Agent": "Bug-Hunter/1.0"},
            )
        if resp.status_code != 200:
            logger.warning("HIBP returned status %d for prefix %s", resp.status_code, prefix)
            return None
        return resp.text
    except (httpx.HTTPError, OSError) as exc:
        logger.warning("HIBP request failed (fail-open): %s", exc)
        return None


def is_password_breached(plain: str) -> bool:
    """True iff the password is in the HIBP corpus with a non-zero count; fails open."""
    if not plain or not _is_enabled():
        return False
    if plain.lower() in _ALWAYS_ALLOWED:
        return False
    digest = _sha1_hex(plain)
    prefix, suffix = digest[:5], digest[5:]
    body = _fetch_range(prefix)
    if body is None:
        return False
    for line in body.splitlines():
        # Lines are "SUFFIX:COUNT"; padding entries use COUNT=0.
        s, _, count = line.partition(":")
        if s.strip().upper() == suffix and count.strip() != "0":
            return True
    return False
