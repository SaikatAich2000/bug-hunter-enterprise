"""TOTP (2FA) enrolment + verification endpoints.

Endpoints:
  GET    /api/auth/2fa/status            — am I enrolled?
  POST   /api/auth/2fa/begin             — start enrollment, returns secret + otpauth URI
  POST   /api/auth/2fa/confirm           — confirm with first 6-digit code; issues recovery codes
  POST   /api/auth/2fa/disable           — requires password; clears secret + codes
  POST   /api/auth/2fa/recovery-codes/regenerate — invalidate old, issue new

The login flow itself lives in routes/auth.py — the two-step "password
then TOTP" handshake uses /api/auth/login (returns requires_totp:true if
the user has TOTP on) and /api/auth/login/totp.
"""
from __future__ import annotations

from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from app.auth import get_current_user, verify_password
from app.config import get_settings
from app.database import get_db
from app.models import Activity, TotpRecoveryCode, User
from app.totp import (
    generate_recovery_codes, generate_secret, hash_recovery_code,
    provisioning_uri, verify_code,
)

router = APIRouter(prefix="/api/auth/2fa", tags=["auth"])


class TotpStatus(BaseModel):
    enabled: bool
    enrolled_at: datetime | None = None
    unused_recovery_codes: int = 0


class TotpBeginOut(BaseModel):
    secret: str
    otpauth_uri: str


class TotpConfirmIn(BaseModel):
    code: str = Field(..., min_length=6, max_length=10)


class TotpConfirmOut(BaseModel):
    enabled: bool
    recovery_codes: list[str]


class TotpDisableIn(BaseModel):
    password: str = Field(..., min_length=1, max_length=200)


@router.get("/status", response_model=TotpStatus)
def status(
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> TotpStatus:
    settings = get_settings()
    if not settings.TOTP_ENABLED:
        return TotpStatus(enabled=False)
    unused = 0
    if user.totp_enabled:
        unused = db.query(TotpRecoveryCode).filter(
            TotpRecoveryCode.user_id == user.id,
            TotpRecoveryCode.used_at.is_(None),
        ).count()
    return TotpStatus(
        enabled=bool(user.totp_enabled),
        enrolled_at=user.totp_enrolled_at,
        unused_recovery_codes=unused,
    )


@router.post("/begin", response_model=TotpBeginOut)
def begin(
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> TotpBeginOut:
    settings = get_settings()
    if not settings.TOTP_ENABLED:
        raise HTTPException(status_code=403, detail="Two-factor auth is disabled site-wide.")
    # If they're already enrolled, force them to disable first — never
    # silently overwrite the existing secret (would lock them out of
    # their authenticator app).
    if user.totp_enabled:
        raise HTTPException(status_code=409, detail="2FA is already enabled. Disable it first to re-enrol.")
    secret = generate_secret()
    user.totp_secret = secret
    user.totp_enabled = False  # stays false until confirm()
    db.add(Activity(
        org_id=user.org_id, bug_id=None, entity_type="auth", entity_id=user.id,
        actor_user_id=user.id, actor_name=user.name,
        action="2fa_begin", detail=f"{user.email} started 2FA enrolment",
    ))
    db.commit()
    uri = provisioning_uri(secret, user.email, issuer=settings.APP_NAME)
    return TotpBeginOut(secret=secret, otpauth_uri=uri)


@router.post("/confirm", response_model=TotpConfirmOut)
def confirm(
    payload: TotpConfirmIn,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> TotpConfirmOut:
    settings = get_settings()
    if not settings.TOTP_ENABLED:
        raise HTTPException(status_code=403, detail="Two-factor auth is disabled site-wide.")
    if not user.totp_secret:
        raise HTTPException(status_code=400, detail="No enrolment in progress. Click Enable 2FA first.")
    if user.totp_enabled:
        raise HTTPException(status_code=409, detail="2FA is already enabled.")
    if not verify_code(user.totp_secret, payload.code):
        raise HTTPException(status_code=400, detail="That code didn't match. Try the current one in your authenticator app.")
    user.totp_enabled = True
    user.totp_enrolled_at = datetime.now(timezone.utc)
    # Issue recovery codes
    codes = generate_recovery_codes(settings.TOTP_RECOVERY_CODE_COUNT)
    for c in codes:
        db.add(TotpRecoveryCode(user_id=user.id, code_hash=hash_recovery_code(c)))
    db.add(Activity(
        org_id=user.org_id, bug_id=None, entity_type="auth", entity_id=user.id,
        actor_user_id=user.id, actor_name=user.name,
        action="2fa_enabled",
        detail=f"{user.email} enabled 2FA; {len(codes)} recovery codes issued",
    ))
    db.commit()
    return TotpConfirmOut(enabled=True, recovery_codes=codes)


@router.post("/disable", status_code=204)
def disable(
    payload: TotpDisableIn,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    # Re-authenticate with password (sudo-mode) so a hijacked session
    # can't silently disable 2FA.
    if not verify_password(payload.password, user.password_hash):
        raise HTTPException(status_code=400, detail="Current password is incorrect.")
    user.totp_secret = None
    user.totp_enabled = False
    user.totp_enrolled_at = None
    # Invalidate any unused recovery codes.
    db.query(TotpRecoveryCode).filter(
        TotpRecoveryCode.user_id == user.id,
        TotpRecoveryCode.used_at.is_(None),
    ).delete(synchronize_session=False)
    db.add(Activity(
        org_id=user.org_id, bug_id=None, entity_type="auth", entity_id=user.id,
        actor_user_id=user.id, actor_name=user.name,
        action="2fa_disabled",
        detail=f"{user.email} disabled 2FA",
    ))
    db.commit()


@router.post("/recovery-codes/regenerate", response_model=TotpConfirmOut)
def regenerate_recovery_codes(
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> TotpConfirmOut:
    settings = get_settings()
    if not user.totp_enabled:
        raise HTTPException(status_code=400, detail="Enable 2FA before generating recovery codes.")
    db.query(TotpRecoveryCode).filter(
        TotpRecoveryCode.user_id == user.id,
    ).delete(synchronize_session=False)
    codes = generate_recovery_codes(settings.TOTP_RECOVERY_CODE_COUNT)
    for c in codes:
        db.add(TotpRecoveryCode(user_id=user.id, code_hash=hash_recovery_code(c)))
    db.add(Activity(
        org_id=user.org_id, bug_id=None, entity_type="auth", entity_id=user.id,
        actor_user_id=user.id, actor_name=user.name,
        action="2fa_recovery_regenerated",
        detail=f"{user.email} regenerated {len(codes)} recovery codes",
    ))
    db.commit()
    return TotpConfirmOut(enabled=True, recovery_codes=codes)
