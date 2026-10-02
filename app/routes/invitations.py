"""Invitations API: email-token invitations to join the caller's organization."""
from __future__ import annotations

from datetime import datetime, timezone

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, Request, Response, status
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.access import add_user_project, project_role
from app.api_docs import BAD_REQUEST_FORBIDDEN_CONFLICT_NOT_FOUND_404, BAD_REQUEST_NOT_FOUND_404
from app.auth import INVITATION_TTL, can_invite, generate_token, get_current_user, hash_password, hash_token
from app.config import get_settings
from app.database import get_db
from app.email_service import notify_invitation
from app.models import (
    PROJECT_ROLE_LEAD,
    PROJECT_ROLE_MEMBER,
    ROLE_ADMIN,
    Activity,
    Invitation,
    Project,
    User,
)
from app.routes.auth import start_session, to_me
from app.schemas import (
    InvitationAccept,
    InvitationCreate,
    InvitationOut,
    InvitationPreview,
    MeOut,
)

router = APIRouter(prefix="/api/invitations", tags=["invitations"])

_DETAIL_INVALID = "Invalid or expired invitation"


def _audit(db: Session, org_id: int, actor: User | None, action: str, detail: str, entity_id: int) -> None:
    db.add(Activity(
        org_id=org_id, bug_id=None, entity_type="invitation", entity_id=entity_id,
        actor_user_id=actor.id if actor else None,
        actor_name=actor.name if actor else "system",
        action=action, detail=detail,
    ))


def _require_inviter(actor: User) -> None:
    if not can_invite(actor):
        raise HTTPException(
            status_code=403, detail="Only admins and managers can manage invitations.",
        )


def _validate_projects(db: Session, actor: User, project_ids: list[int]) -> None:
    """Every project must belong to the actor's organization; non-admins must lead each one
    (an invitation grants access to it)."""
    for pid in project_ids:
        project = db.get(Project, pid)
        if project is None or project.org_id != actor.org_id:
            raise HTTPException(status_code=400, detail=f"Unknown project id: {pid}")
        if actor.role != ROLE_ADMIN and project_role(db, actor.id, pid) != PROJECT_ROLE_LEAD:
            raise HTTPException(
                status_code=403,
                detail=f"You're not a lead of project #{pid}; you can't attach it to an invitation.",
            )


def _out(inv: Invitation) -> dict:
    return {
        "id": inv.id, "email": inv.email, "role": inv.role,
        "invited_by_user_id": inv.invited_by_user_id, "invited_by_name": inv.invited_by_name,
        "initial_project_ids": inv.initial_project_ids, "expires_at": inv.expires_at,
        "accepted_at": inv.accepted_at, "revoked_at": inv.revoked_at, "created_at": inv.created_at,
    }


@router.post("", response_model=InvitationOut, status_code=status.HTTP_201_CREATED,
             responses=BAD_REQUEST_FORBIDDEN_CONFLICT_NOT_FOUND_404)
def create_invitation(
    payload: InvitationCreate,
    background: BackgroundTasks,
    actor: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> dict:
    _require_inviter(actor)
    if payload.role == ROLE_ADMIN and actor.role != ROLE_ADMIN:
        raise HTTPException(status_code=403, detail="Only admins can invite people as admins.")
    # Emails are unique across organizations, so an existing account blocks the invitation.
    existing = db.scalar(select(User).where(User.email == payload.email))
    if existing is not None:
        raise HTTPException(
            status_code=409,
            detail=("That user is already a member of your organization."
                    if existing.org_id == actor.org_id
                    else "That email is already registered with another organization."),
        )
    _validate_projects(db, actor, payload.project_ids)

    # A new invitation replaces any still-pending one for the same address.
    now = datetime.now(timezone.utc)
    for old in db.scalars(select(Invitation).where(
        Invitation.org_id == actor.org_id, Invitation.email == payload.email,
        Invitation.accepted_at.is_(None), Invitation.revoked_at.is_(None),
    )).all():
        old.revoked_at = now

    raw_token, token_hash = generate_token()
    prefix = "L:" if payload.as_lead else ""
    inv = Invitation(
        org_id=actor.org_id, email=payload.email, role=payload.role, token_hash=token_hash,
        invited_by_user_id=actor.id, invited_by_name=actor.name,
        initial_project_ids=",".join(f"{prefix}{pid}" for pid in payload.project_ids),
        expires_at=now + INVITATION_TTL,
    )
    db.add(inv)
    db.flush()
    _audit(
        db, actor.org_id, actor, "invitation_sent",
        f"Invited {payload.email} as {payload.role}"
        + (f" with access to {len(payload.project_ids)} project(s)" if payload.project_ids else ""),
        inv.id,
    )
    db.commit()
    accept_url = f"{get_settings().APP_BASE_URL.rstrip('/')}/accept-invite.html?token={raw_token}"
    org = actor.organization
    background.add_task(
        notify_invitation, payload.email, actor.name, org.name, accept_url, payload.role,
        org.email_from_override,
    )
    db.refresh(inv)
    return _out(inv)


@router.get("", response_model=list[InvitationOut], responses=BAD_REQUEST_NOT_FOUND_404)
def list_invitations(
    actor: User = Depends(get_current_user), db: Session = Depends(get_db),
) -> list[dict]:
    _require_inviter(actor)
    rows = db.scalars(
        select(Invitation).where(Invitation.org_id == actor.org_id)
        .order_by(Invitation.created_at.desc(), Invitation.id.desc())
    ).all()
    return [_out(i) for i in rows]


@router.delete("/{invitation_id}", responses=BAD_REQUEST_FORBIDDEN_CONFLICT_NOT_FOUND_404)
def revoke_invitation(
    invitation_id: int, actor: User = Depends(get_current_user), db: Session = Depends(get_db),
) -> dict[str, str]:
    inv = db.get(Invitation, invitation_id)
    if inv is None or inv.org_id != actor.org_id:
        raise HTTPException(status_code=404, detail="Invitation not found")
    _require_inviter(actor)
    if inv.accepted_at is not None:
        raise HTTPException(status_code=400, detail="Invitation already accepted")
    if inv.revoked_at is not None:
        return {"message": "Already revoked"}
    inv.revoked_at = datetime.now(timezone.utc)
    _audit(db, actor.org_id, actor, "invitation_revoked", f"Revoked invite for {inv.email}", inv.id)
    db.commit()
    return {"message": "Invitation revoked"}


def _live_invitation(db: Session, token: str) -> Invitation:
    """The pending invitation for ``token``; 400/404 when missing, used, revoked or expired."""
    inv = db.scalar(select(Invitation).where(Invitation.token_hash == hash_token(token)))
    if inv is None:
        raise HTTPException(status_code=404, detail=_DETAIL_INVALID)
    if inv.accepted_at is not None:
        raise HTTPException(status_code=400, detail="This invitation has already been used.")
    if inv.revoked_at is not None:
        raise HTTPException(status_code=400, detail="This invitation has been revoked.")
    if inv.expires_at < datetime.now(timezone.utc):
        raise HTTPException(status_code=400, detail="This invitation has expired.")
    return inv


@router.get("/preview/{token}", response_model=InvitationPreview, responses=BAD_REQUEST_NOT_FOUND_404)
def preview_invitation(token: str, db: Session = Depends(get_db)) -> dict:
    """Public: what an invitee may see before accepting (organization, role, inviter name)."""
    inv = _live_invitation(db, token)
    org = inv.organization
    return {
        "email": inv.email, "organization_name": org.name, "role": inv.role,
        "expires_at": inv.expires_at, "invited_by_name": inv.invited_by_name or "",
    }


def _join_projects(db: Session, inv: Invitation, user: User) -> None:
    """Add the invitee to the invitation's projects ("5" member, "L:5" lead); projects that
    no longer exist or belong elsewhere are skipped."""
    for raw in (inv.initial_project_ids or "").split(","):
        raw = raw.strip()
        as_lead = raw.startswith("L:")
        digits = raw[2:] if as_lead else raw
        if not digits.isdigit():
            continue
        project = db.get(Project, int(digits))
        if project is None or project.org_id != inv.org_id:
            continue
        add_user_project(db, user.id, project.id,
                         PROJECT_ROLE_LEAD if as_lead else PROJECT_ROLE_MEMBER)


@router.post("/accept", response_model=MeOut, responses=BAD_REQUEST_FORBIDDEN_CONFLICT_NOT_FOUND_404)
def accept_invitation(
    payload: InvitationAccept, request: Request, response: Response, db: Session = Depends(get_db),
) -> dict:
    """Public: create the account from a valid token and sign the new user in."""
    inv = _live_invitation(db, payload.token)
    if db.scalar(select(User).where(User.email == inv.email)):
        raise HTTPException(
            status_code=409, detail="That email is already registered. Sign in with it instead.",
        )
    user = User(
        org_id=inv.org_id, name=payload.name, email=inv.email, role=inv.role, is_active=True,
        password_hash=hash_password(payload.password),
    )
    db.add(user)
    try:
        db.flush()
    except IntegrityError as exc:
        db.rollback()
        raise HTTPException(
            status_code=409, detail="That email is already registered. Sign in with it instead.",
        ) from exc
    _join_projects(db, inv, user)
    inv.accepted_at = datetime.now(timezone.utc)
    start_session(db, user, request, response)
    _audit(db, inv.org_id, user, "invitation_accepted",
           f"{user.email} accepted the invitation as {user.role}", inv.id)
    db.commit()
    return to_me(user)

