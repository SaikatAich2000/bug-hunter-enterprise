"""Invitations API — email-based token invites to join an org."""
from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, Request, Response, status
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.auth import (
    INVITATION_TTL,
    can_invite,
    generate_random_token,
    get_current_user,
    hash_password,
    hash_token,
    new_jti,
    set_session_cookie,
)
from app.config import get_settings
from app.database import get_db
from app.email_service import notify_invitation
from app.models import (
    PROJECT_ROLE_LEAD,
    PROJECT_ROLE_MEMBER,
    ROLE_ADMIN,
    Activity,
    Invitation,
    Organization,
    Project,
    ProjectMembership,
    Session as SessionRow,
    User,
)
from app.schemas import (
    InvitationAccept,
    InvitationCreate,
    InvitationOut,
    InvitationPreview,
    MeOut,
)

logger = logging.getLogger("bug_hunter.invitations")

router = APIRouter(prefix="/api/invitations", tags=["invitations"])


def _audit(
    db: Session, org_id: int, actor: User | None, action: str,
    detail: str, entity_id: int | None = None,
) -> None:
    db.add(Activity(
        org_id=org_id, bug_id=None, entity_type="invitation", entity_id=entity_id,
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


# ---------------------------------------------------------------------------
# Create — admin or manager
# ---------------------------------------------------------------------------
@router.post("", response_model=InvitationOut, status_code=status.HTTP_201_CREATED)
def create_invitation(
    payload: InvitationCreate,
    background: BackgroundTasks,
    actor: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> dict:
    if not can_invite(actor):
        raise HTTPException(
            status_code=403,
            detail="Only admins and managers can send invitations.",
        )
    # Managers can't promote anyone to admin via an invite.
    if payload.role == ROLE_ADMIN and actor.role != ROLE_ADMIN:
        raise HTTPException(
            status_code=403,
            detail="Only admins can invite people as admins.",
        )

    # Reject if email already a user *anywhere* — globally unique. We
    # reveal this for invites because the inviter clearly knows the
    # invitee's email and would just be confused by a silent failure.
    existing = db.scalar(select(User).where(User.email == payload.email))
    if existing is not None:
        if existing.org_id == actor.org_id:
            raise HTTPException(
                status_code=409,
                detail="That user is already a member of your organization.",
            )
        raise HTTPException(
            status_code=409,
            detail="That email is already registered with another organization.",
        )

    # Validate every project the inviter wants to add the new user to —
    # must belong to this org. Managers can only attach projects they
    # themselves manage; admins can attach anything in the org.
    project_ids: list[int] = []
    for pid in payload.project_ids or []:
        p = db.get(Project, pid)
        if p is None or p.org_id != actor.org_id:
            raise HTTPException(status_code=400, detail=f"Unknown project id: {pid}")
        if actor.role != ROLE_ADMIN:
            pm = db.scalar(
                select(ProjectMembership).where(
                    ProjectMembership.project_id == pid,
                    ProjectMembership.user_id == actor.id,
                    ProjectMembership.role == PROJECT_ROLE_LEAD,
                )
            )
            if pm is None:
                raise HTTPException(
                    status_code=403,
                    detail=f"You're not a lead of project #{pid}; can't attach it to an invite.",
                )
        project_ids.append(pid)

    # Revoke any outstanding (still-pending, non-expired) invite to the
    # same email + org — prevents a confusing pile of multiple links.
    now = datetime.now(timezone.utc)
    existing_pending = db.scalars(
        select(Invitation).where(
            Invitation.org_id == actor.org_id,
            Invitation.email == payload.email,
            Invitation.accepted_at.is_(None),
            Invitation.revoked_at.is_(None),
        )
    ).all()
    for inv in existing_pending:
        inv.revoked_at = now

    raw_token, token_hash = generate_random_token()
    inv = Invitation(
        org_id=actor.org_id,
        email=payload.email,
        role=payload.role,
        token_hash=token_hash,
        invited_by_user_id=actor.id,
        invited_by_name=actor.name,
        initial_project_ids=",".join(str(p) for p in project_ids),
        expires_at=now + INVITATION_TTL,
    )
    # Store as_lead in role-on-each-project sense: we don't keep an
    # explicit column for it; if as_lead was true, we attach a marker
    # to initial_project_ids by prefixing each id with "L:". Simpler
    # than a second table.
    if payload.as_lead and project_ids:
        inv.initial_project_ids = ",".join(f"L:{p}" for p in project_ids)

    db.add(inv)
    db.flush()
    _audit(
        db, actor.org_id, actor, "invitation_sent",
        f"Invited {payload.email} as {payload.role}"
        + (f" with project access ({len(project_ids)} project(s))" if project_ids else ""),
        entity_id=inv.id,
    )
    db.commit()

    org = db.get(Organization, actor.org_id)
    base = get_settings().APP_BASE_URL.rstrip("/")
    accept_url = f"{base}/accept-invite.html?token={raw_token}"
    background.add_task(
        notify_invitation,
        payload.email, actor.name, org.name if org else "", accept_url, payload.role,
    )
    db.refresh(inv)
    return _invite_dict(inv)


def _invite_dict(inv: Invitation) -> dict:
    return {
        "id": inv.id,
        "org_id": inv.org_id,
        "email": inv.email,
        "role": inv.role,
        "invited_by_user_id": inv.invited_by_user_id,
        "invited_by_name": inv.invited_by_name,
        "initial_project_ids": inv.initial_project_ids,
        "expires_at": inv.expires_at,
        "accepted_at": inv.accepted_at,
        "revoked_at": inv.revoked_at,
        "created_at": inv.created_at,
    }


# ---------------------------------------------------------------------------
# List — admin or manager. Only invites for THEIR org.
# ---------------------------------------------------------------------------
@router.get("", response_model=list[InvitationOut])
def list_invitations(
    actor: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> list[dict]:
    if not can_invite(actor):
        raise HTTPException(
            status_code=403,
            detail="Only admins and managers can view invitations.",
        )
    rows = db.scalars(
        select(Invitation)
        .where(Invitation.org_id == actor.org_id)
        .order_by(Invitation.created_at.desc())
    ).all()
    return [_invite_dict(i) for i in rows]


# ---------------------------------------------------------------------------
# Revoke — admin / manager. Can only revoke invites in their own org,
# and managers can't revoke invites sent by admins.
# ---------------------------------------------------------------------------
@router.delete("/{invitation_id}", status_code=200)
def revoke_invitation(
    invitation_id: int,
    actor: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> dict:
    inv = db.get(Invitation, invitation_id)
    if inv is None or inv.org_id != actor.org_id:
        raise HTTPException(status_code=404, detail="Invitation not found")
    if not can_invite(actor):
        raise HTTPException(status_code=403, detail="Forbidden")
    if inv.accepted_at is not None:
        raise HTTPException(status_code=400, detail="Invitation already accepted")
    if inv.revoked_at is not None:
        return {"message": "Already revoked"}

    inv.revoked_at = datetime.now(timezone.utc)
    _audit(
        db, actor.org_id, actor, "invitation_revoked",
        f"Revoked invite for {inv.email}", entity_id=inv.id,
    )
    db.commit()
    return {"message": "Invitation revoked"}


# ---------------------------------------------------------------------------
# Preview — public, by token. Used by the accept-invite page to show
# the invitee who invited them and to which org, BEFORE they fill in
# any details. Validates the token; never reveals other org metadata.
# ---------------------------------------------------------------------------
@router.get("/preview/{token}", response_model=InvitationPreview)
def preview_invitation(token: str, db: Session = Depends(get_db)) -> dict:
    h = hash_token(token)
    inv = db.scalar(select(Invitation).where(Invitation.token_hash == h))
    if inv is None:
        raise HTTPException(status_code=404, detail="Invalid or expired invitation")
    now = datetime.now(timezone.utc)
    expires = inv.expires_at
    if expires.tzinfo is None:
        expires = expires.replace(tzinfo=timezone.utc)
    if inv.accepted_at is not None:
        raise HTTPException(status_code=400, detail="This invitation has already been used.")
    if inv.revoked_at is not None:
        raise HTTPException(status_code=400, detail="This invitation has been revoked.")
    if expires < now:
        raise HTTPException(status_code=400, detail="This invitation has expired.")

    org = db.get(Organization, inv.org_id)
    return {
        "email": inv.email,
        "organization_name": org.name if org else "",
        "role": inv.role,
        "expires_at": inv.expires_at,
        "invited_by_name": inv.invited_by_name or "",
    }


# ---------------------------------------------------------------------------
# Accept — public. Creates the User and starts a session.
# ---------------------------------------------------------------------------
@router.post("/accept", response_model=MeOut)
def accept_invitation(
    payload: InvitationAccept,
    request: Request,
    response: Response,
    db: Session = Depends(get_db),
) -> dict:
    h = hash_token(payload.token)
    inv = db.scalar(select(Invitation).where(Invitation.token_hash == h))
    if inv is None:
        raise HTTPException(status_code=400, detail="Invalid or expired invitation")
    now = datetime.now(timezone.utc)
    expires = inv.expires_at
    if expires.tzinfo is None:
        expires = expires.replace(tzinfo=timezone.utc)
    if inv.accepted_at is not None:
        raise HTTPException(status_code=400, detail="This invitation has already been used.")
    if inv.revoked_at is not None:
        raise HTTPException(status_code=400, detail="This invitation has been revoked.")
    if expires < now:
        raise HTTPException(status_code=400, detail="This invitation has expired.")

    # Email collision check: someone else may have signed up with this
    # email in another org between when the invite was sent and accepted.
    if db.scalar(select(User).where(User.email == inv.email)):
        raise HTTPException(
            status_code=409,
            detail="That email is already registered. Sign in with it instead.",
        )

    user = User(
        org_id=inv.org_id,
        name=payload.name,
        email=inv.email,
        role=inv.role,
        is_active=True,
        password_hash=hash_password(payload.password),
    )
    db.add(user)
    db.flush()

    # Parse initial_project_ids — entries look like "5" (regular member)
    # or "L:5" (lead). Silently skip malformed or no-longer-existing IDs;
    # admins can fix membership manually if a project was deleted.
    for raw in (inv.initial_project_ids or "").split(","):
        raw = raw.strip()
        if not raw:
            continue
        as_lead = False
        if raw.startswith("L:"):
            as_lead = True
            raw = raw[2:]
        try:
            pid = int(raw)
        except ValueError:
            continue
        p = db.get(Project, pid)
        if p is None or p.org_id != inv.org_id:
            continue
        db.add(ProjectMembership(
            project_id=p.id,
            user_id=user.id,
            role=PROJECT_ROLE_LEAD if as_lead else PROJECT_ROLE_MEMBER,
        ))

    inv.accepted_at = now

    settings = get_settings()
    jti = new_jti()
    db.add(SessionRow(
        user_id=user.id,
        jti=jti,
        user_agent=(request.headers.get("user-agent") or "")[:400],
        ip_address=_client_ip(request),
        expires_at=now + timedelta(seconds=settings.SESSION_TTL_SECONDS),
    ))

    _audit(
        db, inv.org_id, user, "invitation_accepted",
        f"{user.email} accepted invite ({user.role})", entity_id=inv.id,
    )
    db.commit()

    set_session_cookie(response, user, jti=jti)
    org = db.get(Organization, user.org_id)
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
