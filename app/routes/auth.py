"""Authentication endpoints: sign-up, login (with two-factor), logout, profile, email change
and password management."""
from __future__ import annotations

import logging
import secrets
from datetime import datetime, timedelta, timezone
from typing import Annotated

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, Request, Response
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app import account_lockout
from app.api_docs import (
    AUTH_401,
    FORGOT_PASSWORD_404,
    PASSWORD_CHANGE_400,
    RESET_TOKEN_400,
)
from app.auth import (
    PASSWORD_RESET_TTL,
    clear_session_cookie,
    generate_token,
    get_current_user,
    hash_password,
    hash_token,
    invalidate_outstanding_reset_tokens,
    new_jti,
    purge_consumed_reset_tokens,
    set_session_cookie,
    trusted_forwarded_ip,
    verify_password,
)
from app.config import get_settings
from app.database import get_db
from app.email_service import notify_email_change_code, notify_password_reset
from app.metrics import record_event
from app.models import (
    ROLE_ADMIN,
    Activity,
    EmailChangeRequest,
    PasswordResetToken,
    TotpRecoveryCode,
    User,
)
from app.models import Session as SessionRow
from app.password_breach import is_password_breached
from app.schemas import (
    ChangePasswordIn,
    EmailChangeConfirmIn,
    EmailChangeRequestIn,
    ForgotPasswordIn,
    LoginIn,
    LoginTotpIn,
    MeOut,
    ProfileUpdateIn,
    ResetPasswordIn,
    SignupIn,
)
from app.tenancy import create_organization
from app.totp import (
    accept_code,
    hash_recovery_code,
    make_pending_token,
    parse_pending_token,
)

logger = logging.getLogger("bug_hunter.auth")

# Verified for unknown emails so timing can't distinguish "no account" from "wrong password".
# Cost depends on construction order vs env: the test suite sets BCRYPT_TEST_ROUNDS
# in tests/conftest.py, but conftest imports app modules first is NOT guaranteed
# across files (pytest resolves conftest at collection; imports of app.routes by
# earlier test modules can happen first). So compute it lazily per call instead
# of baking a production-cost hash at import time — otherwise CI pays rounds=12
# on EVERY unknown-email verify (~0.5s each) even when the override is set.
def _dummy_password_hash() -> str:
    if not hasattr(_dummy_password_hash, "cached"):
        _dummy_password_hash.cached = hash_password("dummy-not-a-real-credential")  # type: ignore[attr-defined]
    return _dummy_password_hash.cached  # type: ignore[attr-defined]

router = APIRouter(prefix="/api/auth", tags=["auth"])


_DETAIL_INVALID_RESET_TOKEN = "Invalid or expired reset token"
_DETAIL_INVALID_LOGIN = "Invalid email or password"
_DETAIL_WRONG_PASSWORD = "Current password is incorrect"
_EMAIL_CHANGE_TTL = timedelta(minutes=15)
_EMAIL_CHANGE_MAX_ATTEMPTS = 5


def _audit(
    db: Session, user: User, action: str, detail: str, entity_id: int | None = None,
    as_system: bool = False,
) -> None:
    """One audit row in ``user``'s organization, attributed to them unless ``as_system``
    (events that are not an action of a signed-in person, such as a reset request)."""
    db.add(Activity(
        org_id=user.org_id, bug_id=None, entity_type="auth", entity_id=entity_id,
        actor_user_id=None if as_system else user.id,
        actor_name="system" if as_system else user.name,
        action=action, detail=detail,
    ))


def _reject_if_breached(plain: str) -> None:
    """Reject an HIBP-breached password; fails open on network errors (see app/password_breach.py)."""
    if is_password_breached(plain):
        raise HTTPException(
            status_code=400,
            detail="This password appears in a known breach corpus. "
                   "Please choose a different one.",
        )


def _mask_email(email: str) -> str:
    """Mask email local part for logs (``alice@x.com`` -> ``a***@x.com``); avoids PII in log stores."""
    if not email or "@" not in email:
        return "***"
    local, _, domain = email.partition("@")
    if not local:
        return "@" + domain
    head = local[0]
    return f"{head}***@{domain}"


def _client_ip(request: Request) -> str:
    """Client IP for sessions/audit. X-Forwarded-For is honoured only when
    TRUST_PROXY_FORWARDED_FOR is set (spoofable otherwise); matches the rate limiter."""
    settings = get_settings()
    if settings.TRUST_PROXY_FORWARDED_FOR:
        fwd = request.headers.get("x-forwarded-for", "")
        if fwd:
            # Right-most proxy-appended entry; the left-most is client-spoofable.
            ip = trusted_forwarded_ip(fwd, settings.TRUST_PROXY_HOP_COUNT)
            if ip is not None:
                return ip[:64]
    if request.client and request.client.host:
        return request.client.host[:64]
    return ""


def to_me(user: User) -> dict:
    """The /me payload: the user plus their organization and its branding."""
    org = user.organization
    return {
        "id": user.id, "name": user.name, "email": user.email, "role": user.role,
        "is_active": user.is_active,
        "org_id": org.id, "organization_name": org.name, "organization_slug": org.slug,
        "totp_enabled": bool(user.totp_enabled),
        "branding": {"logo_data_url": org.logo_data_url, "accent_color": org.accent_color},
    }


def start_session(db: Session, user: User, request: Request, response: Response) -> None:
    """Create the session row and set the signed cookie. The caller commits."""
    jti = new_jti()
    db.add(SessionRow(
        user_id=user.id,
        jti=jti,
        user_agent=(request.headers.get("user-agent") or "")[:400],
        ip_address=_client_ip(request),
        expires_at=datetime.now(timezone.utc) + timedelta(seconds=get_settings().SESSION_TTL_SECONDS),
    ))
    set_session_cookie(response, user, jti=jti)


def _email_taken(db: Session, email: str, exclude_id: int | None = None) -> bool:
    stmt = select(User.id).where(User.email == email)
    if exclude_id is not None:
        stmt = stmt.where(User.id != exclude_id)
    return db.scalar(stmt) is not None


@router.post("/signup", response_model=MeOut, status_code=201, responses=AUTH_401)
def signup(
    payload: SignupIn, request: Request, response: Response, db: Session = Depends(get_db),
) -> dict:
    """Create an organization and its first admin, and sign them in."""
    if not get_settings().ALLOW_PUBLIC_SIGNUP:
        raise HTTPException(
            status_code=403,
            detail="Public sign-up is disabled. Ask your administrator for an invite.",
        )
    if _email_taken(db, payload.email):
        raise HTTPException(
            status_code=409,
            detail="An account with that email already exists. Try signing in.",
        )
    _reject_if_breached(payload.password)
    org = create_organization(db, payload.organization_name)
    user = User(
        org_id=org.id, name=payload.name, email=payload.email, role=ROLE_ADMIN,
        is_active=True, password_hash=hash_password(payload.password),
    )
    db.add(user)
    try:
        db.flush()
    except IntegrityError as exc:
        db.rollback()
        raise HTTPException(
            status_code=409, detail="An account with that email already exists.",
        ) from exc
    start_session(db, user, request, response)
    _audit(db, user, "org_created", f"Organization '{org.name}' created by {user.email}",
           entity_id=org.id)
    _audit(db, user, "user_signup", f"{user.email} signed up as admin of '{org.name}'",
           entity_id=user.id)
    db.commit()
    return to_me(user)


def _complete_login(
    db: Session, user: User, request: Request, response: Response,
) -> dict:
    start_session(db, user, request, response)
    _audit(db, user, "login", f"{user.email} logged in")
    db.commit()
    record_event("login_success")
    return to_me(user)


@router.post("/login", responses=AUTH_401)
def login(
    payload: LoginIn, request: Request, response: Response, db: Session = Depends(get_db),
) -> dict:
    """Verify credentials; sign in, or - for a user with two-factor on - return a short-lived
    ``pending_token`` to trade for a session at /login/totp. The cookie's `jti` maps back to
    the session row, enabling per-session admin revocation."""
    # Check lockout before bcrypt so a login flood doesn't become a CPU flood.
    account_lockout.check_locked(payload.email)

    # LoginIn already lowercases the email.
    user = db.scalar(select(User).where(User.email == payload.email))
    # Run bcrypt even for unknown emails to keep response timing uniform.
    if user is None:
        verify_password(payload.password, _dummy_password_hash())
        password_ok = False
    else:
        password_ok = verify_password(payload.password, user.password_hash)
    # Same 401 for all failures so existence/disabled status doesn't leak.
    if user is None or not password_ok:
        record_event("login_failure")
        account_lockout.record_failure(payload.email)
        raise HTTPException(status_code=401, detail=_DETAIL_INVALID_LOGIN)
    if not user.is_active:
        logger.info("Login refused: inactive account %s", _mask_email(user.email))
        account_lockout.record_failure(payload.email)
        raise HTTPException(status_code=401, detail=_DETAIL_INVALID_LOGIN)

    if get_settings().TOTP_ENABLED and user.totp_enabled and user.totp_secret:
        # The login isn't complete until the second factor passes, so the lockout bucket
        # stays; /login/totp clears it.
        _audit(db, user, "login_password_ok_awaiting_2fa",
               f"{user.email} passed the password step, awaiting 2FA")
        db.commit()
        return {"requires_totp": True, "pending_token": make_pending_token(user.id)}

    # Clear the lockout bucket so transient typos don't carry forward.
    account_lockout.clear(payload.email)
    return _complete_login(db, user, request, response)


def _consume_second_factor(db: Session, user: User, code: str) -> bool | None:
    """Check ``code`` as an authenticator code, else as an unused recovery code (which it
    consumes). Returns True/False for authenticator/recovery success, None for a wrong code."""
    if accept_code(db, user, code):
        return False
    recovery = db.scalar(
        select(TotpRecoveryCode).where(
            TotpRecoveryCode.user_id == user.id,
            TotpRecoveryCode.code_hash == hash_recovery_code(code),
            TotpRecoveryCode.used_at.is_(None),
        )
    )
    if recovery is None:
        return None
    recovery.used_at = datetime.now(timezone.utc)
    return True


@router.post("/login/totp", responses=AUTH_401)
def login_totp(
    payload: LoginTotpIn, request: Request, response: Response, db: Session = Depends(get_db),
) -> dict:
    """Second step of a two-factor login: a 6-digit authenticator code or a recovery code."""
    user_id = parse_pending_token(payload.pending_token)
    user = db.get(User, user_id) if user_id is not None else None
    if user_id is None:
        raise HTTPException(status_code=400, detail="Login session expired. Sign in again.")
    if user is None or not user.is_active or not user.totp_enabled or not user.totp_secret:
        raise HTTPException(status_code=400, detail="Login session invalid. Sign in again.")
    # The second factor is a credential too: rate-limit it like the password.
    account_lockout.check_locked(user.email)
    used_recovery = _consume_second_factor(db, user, payload.code.strip().upper())
    if used_recovery is None:
        record_event("login_totp_failure")
        account_lockout.record_failure(user.email)
        raise HTTPException(
            status_code=400, detail="Invalid code. Try again or use a recovery code.",
        )
    _audit(
        db, user,
        "login_recovery_code_used" if used_recovery else "login_totp_ok",
        f"{user.email} signed in with "
        + ("a one-time recovery code" if used_recovery else "a 2FA code"),
    )
    account_lockout.clear(user.email)
    return _complete_login(db, user, request, response)


@router.post("/logout", status_code=204)
def logout(request: Request, db: Session = Depends(get_db)) -> Response:
    """Clear the session cookie and its server-side row; always 204 (idempotent)."""
    from app.auth import COOKIE_NAME, parse_session_token
    token = request.cookies.get(COOKIE_NAME, "")
    parsed = parse_session_token(token)
    if parsed:
        user_id, _version, jti = parsed
        user = db.get(User, user_id)
        if user:
            _audit(db, user, "logout", f"{user.email} logged out")
        if jti:
            # Only this session; the user's other sessions remain.
            db.execute(
                SessionRow.__table__.delete().where(SessionRow.jti == jti)
            )
        db.commit()
    response = Response(status_code=204)
    clear_session_cookie(response)
    return response


@router.get("/me", response_model=MeOut)
def me(user: User = Depends(get_current_user)) -> dict:
    """Return the currently logged-in user with their organization."""
    return to_me(user)


@router.put("/profile", response_model=MeOut)
def update_profile(
    payload: ProfileUpdateIn,
    user: Annotated[User, Depends(get_current_user)],
    db: Session = Depends(get_db),
) -> dict:
    """Change your own display name. The email goes through the verified change below;
    the role is set by an admin."""
    if payload.name != user.name:
        old = user.name
        user.name = payload.name
        _audit(db, user, "profile_updated", f"Display name: '{old}' → '{user.name}'",
               entity_id=user.id)
        db.commit()
    return to_me(user)


@router.post("/email-change/request", status_code=202, responses=PASSWORD_CHANGE_400)
def request_email_change(
    payload: EmailChangeRequestIn,
    background: BackgroundTasks,
    user: Annotated[User, Depends(get_current_user)],
    db: Session = Depends(get_db),
) -> dict[str, str]:
    """Step 1: re-authenticate, stage the new address and mail it a 6-digit code."""
    if not verify_password(payload.current_password, user.password_hash):
        raise HTTPException(status_code=400, detail=_DETAIL_WRONG_PASSWORD + ".")
    if payload.new_email == user.email:
        raise HTTPException(status_code=400, detail="That's already your email.")
    if _email_taken(db, payload.new_email):
        raise HTTPException(
            status_code=409,
            detail="That email is already in use. Try signing in with it instead.",
        )
    code = f"{secrets.randbelow(1_000_000):06d}"
    now = datetime.now(timezone.utc)
    # Only one pending change at a time.
    db.execute(
        EmailChangeRequest.__table__.update()
        .where(EmailChangeRequest.user_id == user.id, EmailChangeRequest.used_at.is_(None))
        .values(used_at=now)
    )
    db.add(EmailChangeRequest(
        user_id=user.id, new_email=payload.new_email, code_hash=hash_token(code),
        expires_at=now + _EMAIL_CHANGE_TTL,
    ))
    _audit(db, user, "email_change_requested", f"Requested email change to {payload.new_email}",
           entity_id=user.id)
    db.commit()
    background.add_task(
        notify_email_change_code, payload.new_email, user.name, code,
        user.organization.email_from_override,
    )
    return {"message": f"Verification code sent to {payload.new_email}."}


def _void(db: Session, req: EmailChangeRequest, now: datetime, detail: str) -> HTTPException:
    req.used_at = now
    db.commit()
    return HTTPException(status_code=400, detail=detail)


@router.post("/email-change/confirm", response_model=MeOut, responses=PASSWORD_CHANGE_400)
def confirm_email_change(
    payload: EmailChangeConfirmIn,
    user: Annotated[User, Depends(get_current_user)],
    db: Session = Depends(get_db),
) -> dict:
    """Step 2: complete the change with the code mailed to the new address."""
    req = db.scalar(
        select(EmailChangeRequest)
        .where(EmailChangeRequest.user_id == user.id, EmailChangeRequest.used_at.is_(None))
        .order_by(EmailChangeRequest.created_at.desc(), EmailChangeRequest.id.desc())
    )
    if req is None:
        raise HTTPException(status_code=400, detail="No pending email change. Request one first.")
    now = datetime.now(timezone.utc)
    if req.expires_at < now:
        raise _void(db, req, now, "Code expired. Start the change again.")
    if req.attempts >= _EMAIL_CHANGE_MAX_ATTEMPTS:
        raise _void(db, req, now, "Too many wrong codes. Start the change again.")
    if not secrets.compare_digest(hash_token(payload.code), req.code_hash):
        req.attempts += 1
        db.commit()
        left = _EMAIL_CHANGE_MAX_ATTEMPTS - req.attempts
        detail = (f"Wrong code. {left} attempt(s) left." if left > 0
                  else "Too many wrong codes. Start the change again.")
        raise HTTPException(status_code=400, detail=detail)
    # Still unclaimed after the wait?
    if _email_taken(db, req.new_email, exclude_id=user.id):
        req.used_at = now
        db.commit()
        raise HTTPException(
            status_code=409,
            detail="That email was claimed by someone else while we waited. Try a different address.",
        )
    old_email = user.email
    user.email = req.new_email
    req.used_at = now
    _audit(db, user, "email_changed", f"Email: {old_email} → {user.email}", entity_id=user.id)
    try:
        db.commit()
    except IntegrityError as exc:
        db.rollback()
        raise HTTPException(status_code=409, detail="That email is already in use.") from exc
    return to_me(user)


@router.post("/change-password", status_code=204, responses=PASSWORD_CHANGE_400)
def change_password(
    payload: ChangePasswordIn,
    request: Request,
    user: Annotated[User, Depends(get_current_user)],
    db: Session = Depends(get_db),
) -> Response:
    """Change own password: bumps session_version (killing other sessions), invalidates
    outstanding reset tokens, and re-issues a fresh session so the caller stays logged in."""
    if not verify_password(payload.current_password, user.password_hash):
        raise HTTPException(status_code=400, detail=_DETAIL_WRONG_PASSWORD)

    # Reject same-password change: it would boot every other device for no gain.
    if verify_password(payload.new_password, user.password_hash):
        raise HTTPException(
            status_code=400,
            detail="New password must be different from the current password.",
        )

    _reject_if_breached(payload.new_password)

    user.password_hash = hash_password(payload.new_password)
    user.session_version = (user.session_version or 0) + 1
    invalidated = invalidate_outstanding_reset_tokens(db, user.id)

    # Rows are already invalid via the version bump; delete so the admin session list stays clean.
    db.execute(SessionRow.__table__.delete().where(SessionRow.user_id == user.id))

    # Fresh session for the current device so the user isn't bounced to login.
    out = Response(status_code=204)
    start_session(db, user, request, out)

    _audit(db, user, "password_changed",
           f"{user.email} changed their password"
           + (f" (invalidated {invalidated} outstanding reset link(s))" if invalidated else ""))
    db.commit()
    return out


@router.post("/forgot-password", status_code=204, responses=FORGOT_PASSWORD_404)
def forgot_password(
    payload: ForgotPasswordIn,
    background: BackgroundTasks,
    db: Session = Depends(get_db),
) -> Response:
    """Issue a password-reset email. With FORGOT_PASSWORD_ENUMERATION_SAFE (default)
    always 204 so account existence never leaks; otherwise 404s on unknown addresses."""
    settings = get_settings()
    user = db.scalar(select(User).where(User.email == payload.email))
    if user is None or not user.is_active:
        # Run (and discard) the same token work so timing doesn't reveal account existence.
        if settings.FORGOT_PASSWORD_ENUMERATION_SAFE:
            generate_token()
        if user is not None:
            _audit(db, user, "password_reset_no_account",
                   f"Password reset attempted for inactive account {_mask_email(payload.email)}",
                   as_system=True)
            db.commit()
        else:
            logger.info("Password reset requested for unknown email %s", _mask_email(payload.email))
        if settings.FORGOT_PASSWORD_ENUMERATION_SAFE:
            return Response(status_code=204)
        raise HTTPException(
            status_code=404,
            detail="We couldn't find an account with that email. Check the address or contact an administrator",
        )
    # Purge stale tokens inline; there's no background job for this table.
    purge_consumed_reset_tokens(db)
    raw_token, token_hash = generate_token()
    prt = PasswordResetToken(
        user_id=user.id,
        token_hash=token_hash,
        expires_at=datetime.now(timezone.utc) + PASSWORD_RESET_TTL,
    )
    db.add(prt)
    _audit(db, user, "password_reset_requested", f"Password reset requested for {user.email}",
           as_system=True)
    db.commit()

    base = get_settings().APP_BASE_URL.rstrip("/")
    reset_url = f"{base}/reset.html?token={raw_token}"
    background.add_task(
        notify_password_reset, user.email, user.name, reset_url,
        user.organization.email_from_override,
    )
    return Response(status_code=204)


@router.post("/reset-password", status_code=204, responses=RESET_TOKEN_400)
def reset_password(payload: ResetPasswordIn, db: Session = Depends(get_db)) -> Response:
    """Set a new password via reset token; bumps session_version and
    invalidates the user's other outstanding reset tokens."""
    h = hash_token(payload.token)
    prt = db.scalar(select(PasswordResetToken).where(PasswordResetToken.token_hash == h))
    if prt is None:
        raise HTTPException(status_code=400, detail=_DETAIL_INVALID_RESET_TOKEN)
    now = datetime.now(timezone.utc)
    expires = prt.expires_at
    if expires.tzinfo is None:
        expires = expires.replace(tzinfo=timezone.utc)
    if expires < now:
        raise HTTPException(status_code=400, detail=_DETAIL_INVALID_RESET_TOKEN)

    user = db.get(User, prt.user_id)
    if user is None or not user.is_active:
        raise HTTPException(status_code=400, detail=_DETAIL_INVALID_RESET_TOKEN)

    # HIBP check after token validation so invalid-token callers can't probe the breach signal.
    _reject_if_breached(payload.new_password)

    # Guarded UPDATE consumes the token once: a racing request sees rowcount 0, closing replay.
    consumed = (
        db.query(PasswordResetToken)
        .filter(
            PasswordResetToken.id == prt.id,
            PasswordResetToken.used_at.is_(None),
        )
        .update({PasswordResetToken.used_at: now}, synchronize_session=False)
    )
    if not consumed:
        db.rollback()
        raise HTTPException(status_code=400, detail=_DETAIL_INVALID_RESET_TOKEN)

    user.password_hash = hash_password(payload.new_password)
    user.session_version = (user.session_version or 0) + 1
    invalidated = invalidate_outstanding_reset_tokens(db, user.id)

    # Rows are already invalid via the version bump; delete so the admin session list stays clean.
    db.execute(SessionRow.__table__.delete().where(SessionRow.user_id == user.id))

    _audit(db, user, "password_reset",
           f"{user.email} reset their password via token"
           + (f" (invalidated {invalidated} other outstanding reset link(s))"
              if invalidated else ""))
    db.commit()
    return Response(status_code=204)
