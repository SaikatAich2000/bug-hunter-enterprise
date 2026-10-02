"""TOTP (RFC 6238) helpers for two-factor sign-in.

Enrolment: the server makes a secret and an otpauth:// URI; the user adds it to an
authenticator app and proves it with a first code, which switches 2FA on and issues one-time
recovery codes (shown once, stored hashed). From then on a correct password only yields a
short-lived "pending" token; POST /api/auth/login/totp trades it plus a code for the session.
"""
from __future__ import annotations

import hashlib
import hmac
import secrets
import time
from typing import Optional

import pyotp
from itsdangerous import BadSignature, TimestampSigner
from sqlalchemy import update
from sqlalchemy.orm import Session

from app.auth import session_secret
from app.models import User
from app.secrets_box import seal, unseal

_TOTP_DIGITS = 6
_TOTP_INTERVAL = 30
_TOTP_VALID_WINDOW = 1   # accept the previous and next step to absorb clock drift
_PENDING_TTL_SECONDS = 180
_RECOVERY_ALPHABET = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"  # no 0/O/1/I


def generate_secret() -> str:
    return pyotp.random_base32()


def provisioning_uri(secret: str, account_email: str, issuer: str) -> str:
    return pyotp.totp.TOTP(secret).provisioning_uri(name=account_email, issuer_name=issuer)


def store_secret(user: User, secret: str) -> None:
    user.totp_secret = seal(secret)


def stored_secret(user: User) -> str:
    return unseal(user.totp_secret or "")


def _now() -> float:
    return time.time()


def matching_step(secret: str, code: str) -> Optional[int]:
    """The newest time step (within one step of clock drift) whose code is ``code``, or None."""
    if not secret or not code:
        return None
    code = code.strip().replace(" ", "")
    if not code.isdigit() or len(code) != _TOTP_DIGITS:
        return None
    totp = pyotp.TOTP(secret, interval=_TOTP_INTERVAL)
    current = int(_now() // _TOTP_INTERVAL)
    for step in range(current + _TOTP_VALID_WINDOW, current - _TOTP_VALID_WINDOW - 1, -1):
        if hmac.compare_digest(totp.at(step * _TOTP_INTERVAL), code):
            return step
    return None


def accept_code(db: Session, user: User, code: str) -> bool:
    """Check an authenticator code and spend it: a code, once accepted, never works again.

    The step is recorded with a conditional UPDATE, so two requests racing with the same code
    cannot both succeed. The caller commits."""
    step = matching_step(stored_secret(user), code)
    if step is None:
        return False
    spent = db.execute(
        update(User)
        .where(User.id == user.id, (User.totp_last_step.is_(None)) | (User.totp_last_step < step))
        .values(totp_last_step=step)
    ).rowcount
    if not spent:
        return False
    db.refresh(user, attribute_names=["totp_last_step"])
    return True


def _pending_signer() -> TimestampSigner:
    """A different salt from the session signer, so a pending token is never a session."""
    return TimestampSigner(session_secret(), salt="bh-totp-pending")


def make_pending_token(user_id: int) -> str:
    return _pending_signer().sign(str(user_id).encode("utf-8")).decode("utf-8")


def parse_pending_token(token: str) -> Optional[int]:
    if not token:
        return None
    try:
        raw = _pending_signer().unsign(token, max_age=_PENDING_TTL_SECONDS)
        return int(raw.decode("utf-8"))
    except (BadSignature, ValueError):  # expired, tampered or not a number
        return None


def generate_recovery_codes(n: int) -> list[str]:
    """``n`` one-time codes shaped XXXXX-XXXXX (about 50 bits each)."""
    codes = []
    for _ in range(n):
        chars = [secrets.choice(_RECOVERY_ALPHABET) for _ in range(10)]
        codes.append("".join(chars[:5]) + "-" + "".join(chars[5:]))
    return codes


def hash_recovery_code(code: str) -> str:
    return hashlib.sha256(code.strip().upper().encode("utf-8")).hexdigest()
