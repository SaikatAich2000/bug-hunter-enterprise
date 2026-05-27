"""TOTP (RFC 6238) helpers for login-time 2FA.

Flow:
  1. User clicks "Enable 2FA" on the profile page.
  2. We generate a fresh `pyotp` secret + the otpauth:// URL.
  3. Backend returns both. The frontend renders the otpauth URL as a
     QR code (the frontend has a JS QR library — we don't need a
     bitmap toolchain on the server).
  4. User scans with Google Authenticator / Authy / 1Password / Bitwarden,
     enters the generated 6-digit code.
  5. Backend verifies the code (a `verify` with one-step skew tolerance,
     to soak typical clock drift). On success: mark `totp_enabled=true`,
     issue N one-time recovery codes (plaintext shown to the user once,
     hashed in the DB).
  6. From the next login on, after password OK the login endpoint
     responds with `requires_totp=true` and waits for a follow-up
     /api/auth/login/totp call with the 6-digit code (or one recovery
     code).

We use a SHORT-LIVED signed token to bridge step 5 (the half-logged-in
state between password check and TOTP check). Same `itsdangerous` signer
we already use for session cookies — different salt, 3-minute TTL.
"""
from __future__ import annotations

import hashlib
import secrets
from typing import Optional

import pyotp
from itsdangerous import BadSignature, SignatureExpired, TimestampSigner

from app.auth import _signer  # reuse SESSION_SECRET base
from app.config import get_settings

_TOTP_DIGITS = 6
_TOTP_INTERVAL_SECONDS = 30
_TOTP_VALID_WINDOW = 1   # accept previous + next step to absorb clock drift
_PENDING_TTL_SECONDS = 180


def generate_secret() -> str:
    """Return a fresh base-32 TOTP secret suitable for pyotp."""
    return pyotp.random_base32()


def provisioning_uri(secret: str, account_email: str, issuer: str) -> str:
    """Build an otpauth://totp/... URI the user's authenticator can scan."""
    return pyotp.totp.TOTP(secret).provisioning_uri(name=account_email, issuer_name=issuer)


def verify_code(secret: str, code: str) -> bool:
    """Verify a 6-digit TOTP code with a one-step skew tolerance."""
    if not secret or not code:
        return False
    code = code.strip().replace(" ", "")
    if not code.isdigit() or len(code) != _TOTP_DIGITS:
        return False
    return pyotp.TOTP(secret).verify(code, valid_window=_TOTP_VALID_WINDOW)


# ---------------------------------------------------------------------------
# Pending-login token
# ---------------------------------------------------------------------------
def _pending_signer() -> TimestampSigner:
    """Different salt from the main session signer so a leaked pending
    token can't masquerade as a real session."""
    settings = get_settings()
    secret = settings.SESSION_SECRET or _signer().secret_key  # share the base secret
    return TimestampSigner(secret, salt="bh-totp-pending")


def make_pending_token(user_id: int) -> str:
    return _pending_signer().sign(str(user_id).encode("utf-8")).decode("utf-8")


def parse_pending_token(token: str) -> Optional[int]:
    if not token:
        return None
    try:
        raw = _pending_signer().unsign(token, max_age=_PENDING_TTL_SECONDS)
    except (SignatureExpired, BadSignature):
        return None
    try:
        return int(raw.decode("utf-8"))
    except (UnicodeDecodeError, ValueError):
        return None


# ---------------------------------------------------------------------------
# Recovery codes
# ---------------------------------------------------------------------------
def generate_recovery_codes(n: int) -> list[str]:
    """Issue N human-readable one-time codes. Format: 'XXXXX-XXXXX' (10
    alpha chars + dash). Each character is from a 32-symbol alphabet
    (no easily-confused 0/O/1/I), giving 32**10 ≈ 1.1 * 10**15 entropy
    per code — plenty against guessing.
    """
    alphabet = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"
    out: list[str] = []
    for _ in range(n):
        chars = [secrets.choice(alphabet) for _ in range(10)]
        out.append("".join(chars[:5]) + "-" + "".join(chars[5:]))
    return out


def hash_recovery_code(code: str) -> str:
    return hashlib.sha256(code.strip().upper().encode("utf-8")).hexdigest()
