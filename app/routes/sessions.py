"""Sessions admin API — admins see only sessions of users in their org."""
from __future__ import annotations

from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException, Request
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.auth import COOKIE_NAME, parse_session_token, require_admin
from app.database import get_db
from app.models import Activity, Session as SessionRow, User
from app.schemas import SessionOut

router = APIRouter(prefix="/api/sessions", tags=["sessions"])


def _audit(db: Session, org_id: int, actor: User, action: str, detail: str, entity_id: int | None = None) -> None:
    db.add(Activity(
        org_id=org_id, bug_id=None, entity_type="session", entity_id=entity_id,
        actor_user_id=actor.id, actor_name=actor.name,
        action=action, detail=detail,
    ))


def _is_current(request: Request, sess: SessionRow) -> bool:
    token = request.cookies.get(COOKIE_NAME, "")
    parsed = parse_session_token(token)
    if not parsed:
        return False
    _uid, _ver, jti = parsed
    return jti is not None and jti == sess.jti


@router.get("", response_model=list[SessionOut])
def list_sessions(
    request: Request,
    actor: User = Depends(require_admin),
    db: Session = Depends(get_db),
) -> list[dict]:
    now = datetime.now(timezone.utc)

    # Sweep expired rows on read so the panel stays tidy.
    expired = db.scalars(select(SessionRow).where(SessionRow.expires_at < now)).all()
    if expired:
        for s in expired:
            db.delete(s)
        db.commit()

    # Only sessions belonging to users in the admin's org.
    org_user_ids = list(db.scalars(
        select(User.id).where(User.org_id == actor.org_id)
    ).all())
    if not org_user_ids:
        return []

    rows = db.scalars(
        select(SessionRow)
        .where(SessionRow.expires_at >= now, SessionRow.user_id.in_(org_user_ids))
        .order_by(SessionRow.last_seen_at.desc(), SessionRow.id.desc())
    ).all()

    user_map: dict[int, User] = {}
    if rows:
        ids = sorted({r.user_id for r in rows})
        for u in db.scalars(select(User).where(User.id.in_(ids))).all():
            user_map[u.id] = u

    out: list[dict] = []
    for r in rows:
        u = user_map.get(r.user_id)
        out.append({
            "id": r.id,
            "user_id": r.user_id,
            "user_name": u.name if u else None,
            "user_email": u.email if u else None,
            "user_role": u.role if u else None,
            "ip_address": r.ip_address or "",
            "user_agent": r.user_agent or "",
            "created_at": r.created_at,
            "last_seen_at": r.last_seen_at,
            "expires_at": r.expires_at,
            "is_current": _is_current(request, r),
        })
    return out


@router.delete("/{session_id}", status_code=200)
def revoke_session(
    session_id: int,
    request: Request,
    actor: User = Depends(require_admin),
    db: Session = Depends(get_db),
) -> dict[str, str]:
    sess = db.get(SessionRow, session_id)
    if sess is None:
        raise HTTPException(status_code=404, detail="Session not found")

    target = db.get(User, sess.user_id)
    # Org isolation: admin can only revoke sessions of users in their org.
    if target is None or target.org_id != actor.org_id:
        raise HTTPException(status_code=404, detail="Session not found")

    if _is_current(request, sess):
        raise HTTPException(
            status_code=400,
            detail="You can't revoke your own current session — use Log out instead.",
        )

    target_label = f"{target.name} <{target.email}>"
    db.delete(sess)
    _audit(
        db, actor.org_id, actor, "session_revoked",
        f"Revoked session for {target_label}",
        entity_id=sess.user_id,
    )
    db.commit()
    return {"message": "Session revoked"}
