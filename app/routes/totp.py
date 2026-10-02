"""Two-factor (TOTP) enrolment endpoints.

  GET  /api/auth/2fa/status                      enrolled? how many recovery codes are left
  POST /api/auth/2fa/begin                       start enrolment: secret and otpauth URI
  POST /api/auth/2fa/confirm                     prove it with a first code; recovery codes issued
  POST /api/auth/2fa/disable                     needs the password
  POST /api/auth/2fa/recovery-codes/regenerate   replace the recovery codes (needs the password)

The two-step sign-in itself lives in routes/auth.py (/login and /login/totp).
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy import delete, func, select
from sqlalchemy.orm import Session

from app.api_docs import BAD_REQUEST_FORBIDDEN_CONFLICT_NOT_FOUND_404
from app.auth import get_current_user, verify_password
from app.config import get_settings
from app.database import get_db
from app.models import Activity, TotpRecoveryCode, User
from app.totp import (
    accept_code,
    generate_recovery_codes,
    generate_secret,
    hash_recovery_code,
    provisioning_uri,
    store_secret,
)

router = APIRouter(prefix="/api/auth/2fa", tags=["auth"])

_DISABLED = "Two-factor authentication is disabled on this server."


class TotpStatus(BaseModel):
    enabled: bool
    available: bool = True
    enrolled_at: datetime | None = None
    unused_recovery_codes: int = 0


class TotpBeginOut(BaseModel):
    secret: str
    otpauth_uri: str


class TotpConfirmIn(BaseModel):
    code: str = Field(min_length=6, max_length=10)


class TotpConfirmOut(BaseModel):
    enabled: bool
    recovery_codes: list[str]


class TotpPasswordIn(BaseModel):
    password: str = Field(min_length=1, max_length=200)


def _audit(db: Session, user: User, action: str, detail: str) -> None:
    db.add(Activity(
        org_id=user.org_id, bug_id=None, entity_type="auth", entity_id=user.id,
        actor_user_id=user.id, actor_name=user.name, action=action, detail=detail,
    ))


def _require_available() -> None:
    if not get_settings().TOTP_ENABLED:
        raise HTTPException(status_code=403, detail=_DISABLED)


def _issue_recovery_codes(db: Session, user: User) -> list[str]:
    codes = generate_recovery_codes(get_settings().TOTP_RECOVERY_CODE_COUNT)
    db.add_all(
        TotpRecoveryCode(user_id=user.id, code_hash=hash_recovery_code(c)) for c in codes
    )
    return codes


@router.get("/status")
def status(user: Annotated[User, Depends(get_current_user)], db: Annotated[Session, Depends(get_db)]) -> TotpStatus:
    if not get_settings().TOTP_ENABLED:
        return TotpStatus(enabled=False, available=False)
    unused = 0
    if user.totp_enabled:
        unused = db.scalar(
            select(func.count()).select_from(TotpRecoveryCode)
            .where(TotpRecoveryCode.user_id == user.id, TotpRecoveryCode.used_at.is_(None))
        ) or 0
    return TotpStatus(
        enabled=bool(user.totp_enabled), enrolled_at=user.totp_enrolled_at,
        unused_recovery_codes=unused,
    )


@router.post("/begin",
             responses=BAD_REQUEST_FORBIDDEN_CONFLICT_NOT_FOUND_404)
def begin(user: Annotated[User, Depends(get_current_user)], db: Annotated[Session, Depends(get_db)]) -> TotpBeginOut:
    _require_available()
    # Never overwrite an active secret: that would lock the user out of their authenticator.
    if user.totp_enabled:
        raise HTTPException(
            status_code=409, detail="2FA is already enabled. Disable it first to enrol again.",
        )
    secret = generate_secret()
    store_secret(user, secret)
    user.totp_last_step = None
    user.totp_enabled = False  # stays off until confirm()
    _audit(db, user, "2fa_begin", f"{user.email} started 2FA enrolment")
    db.commit()
    return TotpBeginOut(
        secret=secret,
        otpauth_uri=provisioning_uri(secret, user.email, issuer=get_settings().APP_NAME),
    )


@router.post("/confirm",
             responses=BAD_REQUEST_FORBIDDEN_CONFLICT_NOT_FOUND_404)
def confirm(
    payload: TotpConfirmIn, user: Annotated[User, Depends(get_current_user)], db: Annotated[Session, Depends(get_db)],
) -> TotpConfirmOut:
    _require_available()
    if not user.totp_secret:
        raise HTTPException(status_code=400, detail="No enrolment in progress. Start with Enable 2FA.")
    if user.totp_enabled:
        raise HTTPException(status_code=409, detail="2FA is already enabled.")
    if not accept_code(db, user, payload.code):
        raise HTTPException(
            status_code=400,
            detail="That code didn't match. Use the current one from your authenticator app.",
        )
    user.totp_enabled = True
    user.totp_enrolled_at = datetime.now(timezone.utc)
    codes = _issue_recovery_codes(db, user)
    _audit(db, user, "2fa_enabled", f"{user.email} enabled 2FA; {len(codes)} recovery codes issued")
    db.commit()
    return TotpConfirmOut(enabled=True, recovery_codes=codes)


@router.post("/disable", status_code=204, responses=BAD_REQUEST_FORBIDDEN_CONFLICT_NOT_FOUND_404)
def disable(
    payload: TotpPasswordIn, user: Annotated[User, Depends(get_current_user)], db: Annotated[Session, Depends(get_db)],
) -> None:
    # Re-authenticate so a hijacked session cannot quietly switch 2FA off.
    if not verify_password(payload.password, user.password_hash):
        raise HTTPException(status_code=400, detail="Current password is incorrect.")
    user.totp_secret = None
    user.totp_enabled = False
    user.totp_enrolled_at = None
    user.totp_last_step = None
    db.execute(delete(TotpRecoveryCode).where(TotpRecoveryCode.user_id == user.id))
    _audit(db, user, "2fa_disabled", f"{user.email} disabled 2FA")
    db.commit()


@router.post("/recovery-codes/regenerate",
             responses=BAD_REQUEST_FORBIDDEN_CONFLICT_NOT_FOUND_404)
def regenerate_recovery_codes(
    payload: TotpPasswordIn, user: Annotated[User, Depends(get_current_user)], db: Annotated[Session, Depends(get_db)],
) -> TotpConfirmOut:
    if not user.totp_enabled:
        raise HTTPException(status_code=400, detail="Enable 2FA before generating recovery codes.")
    if not verify_password(payload.password, user.password_hash):
        raise HTTPException(status_code=400, detail="Current password is incorrect.")
    db.execute(delete(TotpRecoveryCode).where(TotpRecoveryCode.user_id == user.id))
    codes = _issue_recovery_codes(db, user)
    _audit(db, user, "2fa_recovery_regenerated",
           f"{user.email} regenerated {len(codes)} recovery codes")
    db.commit()
    return TotpConfirmOut(enabled=True, recovery_codes=codes)
