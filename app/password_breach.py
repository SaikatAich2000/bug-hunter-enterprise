"""HaveIBeenPwned breach check (T4).

Implements the privacy-preserving k-anonymity API: we send only the
first 5 hex characters of the password's SHA-1 hash, and check the
returned suffix list locally. The plaintext (and the full hash) never
leave the server.

API:  GET https://api.pwnedpasswords.com/range/{PREFIX}
Doc:  https://haveibeenpwned.com/API/v3#PwnedPasswords

Behaviour:
  - Default: enabled. Set PASSWORD_BREACH_CHECK_ENABLED=false to disable
    in airgapped deploys (no outbound HTTPS) or test environments.
  - Fail-OPEN on network error / timeout / non-200 response. We log a
    warning and let the password through. Blocking legitimate password
    changes because Cloudflare hiccuped would be worse than the rare
    miss; the password still has to pass the local strength checks.
  - Short timeout (3 s) so a slow API doesn't slow every password set.
  - Add-Padding: true so the response size is constant — the server
    can't infer the prefix's true hit-count from the byte count.

Tests monkeypatch ``_fetch_range`` to return a controlled response.
"""
from __future__ import annotations

import hashlib
import logging
import os

import httpx

logger = logging.getLogger("bug_hunter.password_breach")

_API_URL = "https://api.pwnedpasswords.com/range/"
_TIMEOUT_SECONDS = 3.0


def _env_bool(name: str, default: bool) -> bool:
    return os.getenv(name, str(default)).strip().lower() in ("1", "true", "yes", "on")


def _is_enabled() -> bool:
    return _env_bool("PASSWORD_BREACH_CHECK_ENABLED", True)


def _sha1_hex(plain: str) -> str:
    # SHA-1 is the format HIBP's API expects — used here only as a
    # k-anonymity index, NEVER as the stored password hash (that's
    # bcrypt; see app/auth.py). Sonar python:S4790 would flag SHA-1 as
    # weak in a hashing context; here it's intentionally an API-format
    # constraint, not a security primitive.
    return hashlib.sha1(plain.encode("utf-8")).hexdigest().upper()  # NOSONAR


def _fetch_range(prefix: str) -> str | None:
    """Return the raw text body for /range/{prefix}, or None on failure.

    Public so tests can monkeypatch this seam without mocking httpx
    transport internals.
    """
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
    """Return True iff this password appears in the HIBP breach corpus
    with a non-zero count. Fail-open on any error."""
    if not plain or not _is_enabled():
        return False
    digest = _sha1_hex(plain)
    prefix, suffix = digest[:5], digest[5:]
    body = _fetch_range(prefix)
    if body is None:
        return False
    for line in body.splitlines():
        # Lines are "SUFFIX:COUNT". The padding entries have COUNT=0 to
        # distinguish them; we treat them as not-breached.
        s, _, count = line.partition(":")
        if s.strip().upper() == suffix and count.strip() != "0":
            return True
    return False
