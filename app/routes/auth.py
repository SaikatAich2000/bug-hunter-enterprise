"""Authentication endpoints — signup, login, logout, password management."""
from __future__ import annotations

import logging
import re
import secrets
from datetime import datetime, timedelta, timezone

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, Request, Response
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.auth import (
    PASSWORD_RESET_TTL,
    clear_session_cookie,
    generate_reset_token,
    get_current_user,
    hash_password,
    hash_reset_token,
    hash_token,
    invalidate_outstanding_reset_tokens,
    new_jti,
    set_session_cookie,
    verify_password,
)
from app.config import get_settings
from app.database import get_db
from app.email_service import notify_password_reset
from app.models import (
    ROLE_ADMIN,
    Activity,
    Organization,
    PasswordResetToken,
    Session as SessionRow,
    User,
)
from app.schemas import (
    ChangePasswordIn,
    ForgotPasswordIn,
    LoginIn,
    MeOut,
    ResetPasswordIn,
    SignupIn,
)

logger = logging.getLogger("bug_hunter.auth")

router = APIRouter(prefix="/api/auth", tags=["auth"])


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
def _audit(
    db: Session, org_id: int, actor: User | None, action: str,
    detail: str, entity_id: int | None = None,
) -> None:
    db.add(Activity(
        org_id=org_id, bug_id=None, entity_type="auth", entity_id=entity_id,
        actor_user_id=actor.id if actor else None,
        actor_name=actor.name if actor else "system",
        action=action, detail=detail,
    ))


def _client_ip(request: Request) -> str:
    fwd = request.headers.get("x-forwarded-for", "")
    if fwd:
        ip = fwd.split(",")[0].strip()
    elif request.client and request.client.host:
        ip = request.client.host
    else:
        ip = ""
    return ip[:64]


def _to_me(user: User, org: Organization) -> dict:
    return {
        "id": user.id,
        "name": user.name,
        "email": user.email,
        "role": user.role,
        "is_active": user.is_active,
        "org_id": org.id,
        "organization_name": org.name,
        "organization_slug": org.slug,
    }


_SLUG_BAD = re.compile(r"[^a-z0-9]+")


def _slugify(name: str) -> str:
    base = _SLUG_BAD.sub("-", (name or "").lower()).strip("-")
    return base[:60] or "org"


def _make_unique_slug(db: Session, name: str) -> str:
    """Find a free slug derived from `name`. Appends a short random
    suffix on collision so signups race-safe even under contention."""
    base = _slugify(name)
    candidate = base
    for _ in range(8):
        exists = db.scalar(select(Organization.id).where(Organization.slug == candidate))
        if not exists:
            return candidate
        candidate = f"{base}-{secrets.token_hex(3)}"
    # Extremely unlikely; fall through to a fully random slug.
    return f"{base}-{secrets.token_hex(6)}"


# ---------------------------------------------------------------------------
# Sign up — creates org + admin user in one transaction
# ---------------------------------------------------------------------------
@router.post("/signup", response_model=MeOut, status_code=201)
def signup(
    payload: SignupIn,
    request: Request,
    response: Response,
    db: Session = Depends(get_db),
) -> dict:
    settings = get_settings()
    if not settings.ALLOW_PUBLIC_SIGNUP:
        raise HTTPException(
            status_code=403,
            detail="Public sign-up is disabled. Ask your administrator for an invite.",
        )

    # Reject duplicate email up front so we give a clear error instead of
    # a generic IntegrityError. The unique index is still authoritative.
    if db.scalar(select(User).where(User.email == payload.email)):
        raise HTTPException(
            status_code=409,
            detail="An account with that email already exists. Try signing in.",
        )

    org = Organization(
        name=payload.organization_name,
        slug=_make_unique_slug(db, payload.organization_name),
        description="",
    )
    db.add(org)
    db.flush()  # we need org.id for the user FK

    user = User(
        org_id=org.id,
        name=payload.name,
        email=payload.email,
        role=ROLE_ADMIN,
        is_active=True,
        password_hash=hash_password(payload.password),
    )
    db.add(user)
    try:
        db.flush()
    except IntegrityError as exc:
        db.rollback()
        raise HTTPException(
            status_code=409,
            detail="An account with that email already exists.",
        ) from exc

    # Establish a session for the signup user so they land straight in
    # the app — no extra "now log in" hop.
    jti = new_jti()
    expires_at = datetime.now(timezone.utc) + timedelta(seconds=settings.SESSION_TTL_SECONDS)
    db.add(SessionRow(
        user_id=user.id,
        jti=jti,
        user_agent=(request.headers.get("user-agent") or "")[:400],
        ip_address=_client_ip(request),
        expires_at=expires_at,
    ))

    _audit(
        db, org.id, user, "org_created",
        f"Organization '{org.name}' created by {user.email}",
        entity_id=org.id,
    )
    _audit(
        db, org.id, user, "user_signup",
        f"{user.email} signed up as admin of '{org.name}'",
        entity_id=user.id,
    )
    db.commit()

    set_session_cookie(response, user, jti=jti)
    return _to_me(user, org)


# ---------------------------------------------------------------------------
# Login
# ---------------------------------------------------------------------------
@router.post("/login", response_model=MeOut)
def login(
    payload: LoginIn, request: Request, response: Response,
    db: Session = Depends(get_db),
) -> dict:
    user = db.scalar(select(User).where(User.email == payload.email))
    if user is None or not verify_password(payload.password, user.password_hash):
        raise HTTPException(status_code=401, detail="Invalid email or password")
    if not user.is_active:
        raise HTTPException(status_code=403, detail="Account is disabled")

    org = db.get(Organization, user.org_id)
    if org is None:
        # Defensive: an active user must have an org. Tampered data — log
        # and reject the login rather than panic.
        logger.error("User %d has no organization (org_id=%s)", user.id, user.org_id)
        raise HTTPException(status_code=500, detail="Account misconfigured. Contact support.")

    settings = get_settings()
    jti = new_jti()
    expires_at = datetime.now(timezone.utc) + timedelta(seconds=settings.SESSION_TTL_SECONDS)
    db.add(SessionRow(
        user_id=user.id,
        jti=jti,
        user_agent=(request.headers.get("user-agent") or "")[:400],
        ip_address=_client_ip(request),
        expires_at=expires_at,
    ))

    set_session_cookie(response, user, jti=jti)
    _audit(db, user.org_id, user, "login", f"{user.email} logged in")
    db.commit()
    return _to_me(user, org)


# ---------------------------------------------------------------------------
# Logout
# ---------------------------------------------------------------------------
@router.post("/logout", status_code=204)
def logout(request: Request, db: Session = Depends(get_db)) -> Response:
    from app.auth import COOKIE_NAME, parse_session_token
    token = request.cookies.get(COOKIE_NAME, "")
    parsed = parse_session_token(token)
    if parsed:
        user_id, _version, jti = parsed
        user = db.get(User, user_id)
        if user:
            _audit(db, user.org_id, user, "logout", f"{user.email} logged out")
        if jti:
            db.execute(SessionRow.__table__.delete().where(SessionRow.jti == jti))
        db.commit()
    response = Response(status_code=204)
    clear_session_cookie(response)
    return response


# ---------------------------------------------------------------------------
# Whoami
# ---------------------------------------------------------------------------
@router.get("/me", response_model=MeOut)
def me(user: User = Depends(get_current_user), db: Session = Depends(get_db)) -> dict:
    org = db.get(Organization, user.org_id)
    if org is None:
        raise HTTPException(status_code=500, detail="Account misconfigured")
    return _to_me(user, org)


# ---------------------------------------------------------------------------
# Change password
# ---------------------------------------------------------------------------
@router.post("/change-password", status_code=204)
def change_password(
    payload: ChangePasswordIn,
    request: Request,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
) -> Response:
    if not verify_password(payload.current_password, user.password_hash):
        raise HTTPException(status_code=400, detail="Current password is incorrect")

    user.password_hash = hash_password(payload.new_password)
    user.session_version = (user.session_version or 0) + 1
    invalidated = invalidate_outstanding_reset_tokens(db, user.id)

    db.execute(SessionRow.__table__.delete().where(SessionRow.user_id == user.id))

    settings = get_settings()
    jti = new_jti()
    new_sess = SessionRow(
        user_id=user.id,
        jti=jti,
        user_agent=(request.headers.get("user-agent") or "")[:400],
        ip_address=_client_ip(request),
        expires_at=datetime.now(timezone.utc) + timedelta(seconds=settings.SESSION_TTL_SECONDS),
    )
    db.add(new_sess)

    _audit(
        db, user.org_id, user, "password_changed",
        f"{user.email} changed their password"
        + (f" (invalidated {invalidated} outstanding reset link(s))" if invalidated else ""),
    )
    db.commit()

    out = Response(status_code=204)
    set_session_cookie(out, user, jti=jti)
    return out


# ---------------------------------------------------------------------------
# Forgot password
# ---------------------------------------------------------------------------
@router.post("/forgot-password", status_code=204)
def forgot_password(
    payload: ForgotPasswordIn,
    background: BackgroundTasks,
    db: Session = Depends(get_db),
) -> Response:
    user = db.scalar(select(User).where(User.email == payload.email))
    if user is not None and user.is_active:
        raw_token, token_hash = generate_reset_token()
        prt = PasswordResetToken(
            user_id=user.id,
            token_hash=token_hash,
            expires_at=datetime.now(timezone.utc) + PASSWORD_RESET_TTL,
        )
        db.add(prt)
        _audit(
            db, user.org_id, None, "password_reset_requested",
            f"Password reset requested for {user.email}",
        )
        db.commit()

        base = get_settings().APP_BASE_URL.rstrip("/")
        reset_url = f"{base}/reset.html?token={raw_token}"
        background.add_task(notify_password_reset, user.email, user.name, reset_url)
    return Response(status_code=204)


# ---------------------------------------------------------------------------
# Reset password
# ---------------------------------------------------------------------------
@router.post("/reset-password", status_code=204)
def reset_password(payload: ResetPasswordIn, db: Session = Depends(get_db)) -> Response:
    h = hash_reset_token(payload.token)
    prt = db.scalar(select(PasswordResetToken).where(PasswordResetToken.token_hash == h))
    if prt is None:
        raise HTTPException(status_code=400, detail="Invalid or expired reset token")
    now = datetime.now(timezone.utc)
    expires = prt.expires_at
    if expires.tzinfo is None:
        expires = expires.replace(tzinfo=timezone.utc)
    if prt.used_at is not None or expires < now:
        raise HTTPException(status_code=400, detail="Invalid or expired reset token")

    user = db.get(User, prt.user_id)
    if user is None or not user.is_active:
        raise HTTPException(status_code=400, detail="Invalid or expired reset token")

    user.password_hash = hash_password(payload.new_password)
    user.session_version = (user.session_version or 0) + 1
    prt.used_at = now
    invalidated = invalidate_outstanding_reset_tokens(db, user.id)

    db.execute(SessionRow.__table__.delete().where(SessionRow.user_id == user.id))

    _audit(
        db, user.org_id, user, "password_reset",
        f"{user.email} reset their password via token"
        + (f" (invalidated {invalidated - 1} other outstanding reset link(s))" if invalidated > 1 else ""),
    )
    db.commit()
    return Response(status_code=204)


# ---------------------------------------------------------------------------
# Profile — self-service
# ---------------------------------------------------------------------------
import secrets as _secrets
from app.email_service import notify_email_change_code as _notify_email_change_code
from app.models import EmailChangeRequest
from app.schemas import (
    EmailChangeConfirmIn,
    EmailChangeRequestIn,
    ProfileUpdateIn,
)

EMAIL_CHANGE_TTL = timedelta(minutes=15)
EMAIL_CHANGE_MAX_ATTEMPTS = 5


@router.put("/profile", response_model=MeOut)
def update_profile(
    payload: ProfileUpdateIn,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> dict:
    """Update the caller's own name. Email is NOT editable here — it
    goes through the two-step verification flow below. Role is set by
    admins via /api/users/{id}."""
    if payload.name and payload.name != user.name:
        old = user.name
        user.name = payload.name
        _audit(
            db, user.org_id, user, "profile_updated",
            f"Display name: '{old}' → '{user.name}'",
            entity_id=user.id,
        )
    db.commit()
    org = db.get(Organization, user.org_id)
    if org is None:
        raise HTTPException(status_code=500, detail="Account misconfigured")
    return _to_me(user, org)


@router.post("/email-change/request", status_code=202)
def request_email_change(
    payload: EmailChangeRequestIn,
    background: BackgroundTasks,
    request: Request,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> dict[str, str]:
    """Step 1: verify current password, stage the new email, mail a code."""
    # Re-authenticate with current password — same idea as "sudo mode".
    # Without this, a session hijack would let an attacker change the
    # recovery email at leisure.
    if not verify_password(payload.current_password, user.password_hash):
        raise HTTPException(status_code=400, detail="Current password is incorrect.")

    new_email = payload.new_email
    if new_email == user.email:
        raise HTTPException(status_code=400, detail="That's already your email.")

    # Globally unique check — same constraint as signup. The DB unique
    # index would also catch this, but we want a friendly error first.
    other = db.scalar(select(User).where(User.email == new_email))
    if other is not None:
        # Don't reveal which account if it's in another org — same
        # tenant-isolation pattern as the invite endpoint, but here we
        # do leak enough to let the user reuse a forgotten account.
        raise HTTPException(
            status_code=409,
            detail="That email is already in use. Try signing in with it instead.",
        )

    # Generate a 6-digit code. Six digits is enough entropy for a
    # 15-minute window when paired with rate limiting and attempt cap.
    code = f"{_secrets.randbelow(1_000_000):06d}"
    code_hash = hash_token(code)

    # Invalidate any outstanding requests for this user — only one
    # pending email change at a time.
    now = datetime.now(timezone.utc)
    db.execute(
        EmailChangeRequest.__table__.update()
        .where(
            EmailChangeRequest.user_id == user.id,
            EmailChangeRequest.used_at.is_(None),
        )
        .values(used_at=now)
    )
    req = EmailChangeRequest(
        user_id=user.id,
        new_email=new_email,
        code_hash=code_hash,
        expires_at=now + EMAIL_CHANGE_TTL,
    )
    db.add(req)
    _audit(
        db, user.org_id, user, "email_change_requested",
        f"Requested email change to {new_email}",
        entity_id=user.id,
    )
    db.commit()

    background.add_task(_notify_email_change_code, new_email, user.name, code)
    return {"message": f"Verification code sent to {new_email}."}


@router.post("/email-change/confirm", response_model=MeOut)
def confirm_email_change(
    payload: EmailChangeConfirmIn,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> dict:
    """Step 2: complete the change by entering the code sent to the
    new address."""
    req = db.scalar(
        select(EmailChangeRequest)
        .where(
            EmailChangeRequest.user_id == user.id,
            EmailChangeRequest.used_at.is_(None),
        )
        .order_by(EmailChangeRequest.created_at.desc())
    )
    if req is None:
        raise HTTPException(
            status_code=400,
            detail="No pending email change. Request one first.",
        )
    now = datetime.now(timezone.utc)
    expires = req.expires_at
    if expires.tzinfo is None:
        expires = expires.replace(tzinfo=timezone.utc)
    if expires < now:
        req.used_at = now
        db.commit()
        raise HTTPException(status_code=400, detail="Code expired. Start the change again.")

    if req.attempts >= EMAIL_CHANGE_MAX_ATTEMPTS:
        req.used_at = now
        db.commit()
        raise HTTPException(
            status_code=400,
            detail="Too many wrong codes. Start the change again.",
        )

    if hash_token(payload.code) != req.code_hash:
        req.attempts = (req.attempts or 0) + 1
        db.commit()
        remaining = EMAIL_CHANGE_MAX_ATTEMPTS - req.attempts
        if remaining > 0:
            raise HTTPException(
                status_code=400,
                detail=f"Wrong code. {remaining} attempt(s) left.",
            )
        raise HTTPException(
            status_code=400,
            detail="Too many wrong codes. Start the change again.",
        )

    # Double-check email is still unclaimed (race during the 15-min window).
    other = db.scalar(select(User).where(
        User.email == req.new_email, User.id != user.id,
    ))
    if other is not None:
        req.used_at = now
        db.commit()
        raise HTTPException(
            status_code=409,
            detail="That email was claimed by someone else while we waited. Try a different address.",
        )

    old_email = user.email
    user.email = req.new_email
    req.used_at = now
    _audit(
        db, user.org_id, user, "email_changed",
        f"Email: {old_email} → {user.email}",
        entity_id=user.id,
    )
    db.commit()
    db.refresh(user)
    org = db.get(Organization, user.org_id)
    if org is None:
        raise HTTPException(status_code=500, detail="Account misconfigured")
    return _to_me(user, org)
