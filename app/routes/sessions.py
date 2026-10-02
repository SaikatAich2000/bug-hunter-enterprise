"""Sessions admin API: list active sessions and revoke one device at a time.

Admin-only (narrower than user/project management) — silently logging
people out is sensitive. Admins only ever see the sessions of their own organization.
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException, Request
from sqlalchemy import delete, select
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session

from app.api_docs import BAD_REQUEST_NOT_FOUND_404 as NOT_FOUND_400
from app.auth import COOKIE_NAME, parse_session_token, require_admin
from app.database import get_db
from app.models import Activity, User
from app.models import Session as SessionRow
from app.schemas import SessionOut

logger = logging.getLogger("bug_hunter.sessions")

router = APIRouter(prefix="/api/sessions", tags=["sessions"])


def _audit(db: Session, actor: User, action: str, detail: str, entity_id: int | None = None) -> None:
    db.add(Activity(
        org_id=actor.org_id, bug_id=None, entity_type="session", entity_id=entity_id,
        actor_user_id=actor.id, actor_name=actor.name,
        action=action, detail=detail,
    ))


def _is_current(request: Request, sess: SessionRow) -> bool:
    """True when the row matches the caller's cookie — labels it and blocks self-revoke."""
    token = request.cookies.get(COOKIE_NAME, "")
    parsed = parse_session_token(token)
    if not parsed:
        return False
    _uid, _ver, jti = parsed
    return jti is not None and jti == sess.jti


@router.get("", response_model=list[SessionOut])
def list_sessions(
    request: Request,
    db: Session = Depends(get_db),
    actor: User = Depends(require_admin),
) -> list[dict]:
    """List non-expired sessions with user info, sweeping expired rows on read."""
    now = datetime.now(timezone.utc)

    # set-based DELETE avoids the concurrent-admin StaleDataError; a failed
    # sweep is harmless since the SELECT below filters expired rows anyway
    try:
        db.execute(delete(SessionRow).where(SessionRow.expires_at < now))
        db.commit()
    except SQLAlchemyError:
        logger.exception("Session expiry sweep failed; serving the listing anyway.")
        db.rollback()

    rows = db.scalars(
        select(SessionRow)
        .join(User, User.id == SessionRow.user_id)
        .where(SessionRow.expires_at >= now, User.org_id == actor.org_id)
        .order_by(SessionRow.last_seen_at.desc(), SessionRow.id.desc())
    ).all()

    # batch-load users — avoids N+1
    user_ids = sorted({r.user_id for r in rows})
    user_map: dict[int, User] = {}
    if user_ids:
        for u in db.scalars(select(User).where(User.id.in_(user_ids))).all():
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


@router.delete("/{session_id}", status_code=200, responses=NOT_FOUND_400)
def revoke_session(
    session_id: int,
    request: Request,
    db: Session = Depends(get_db),
    actor: User = Depends(require_admin),
) -> dict[str, str]:
    """Revoke one session row; other sessions for the same user are untouched."""
    sess = db.get(SessionRow, session_id)
    target = db.get(User, sess.user_id) if sess is not None else None
    if sess is None or target is None or target.org_id != actor.org_id:
        raise HTTPException(status_code=404, detail="Session not found")

    # no undo — admins must use logout, not self-revocation
    if _is_current(request, sess):
        raise HTTPException(
            status_code=400,
            detail="You can't revoke your own current session — use Log out instead.",
        )

    target_label = f"{target.name} <{target.email}>"
    db.delete(sess)
    _audit(
        db, actor, "session_revoked", f"Revoked session for {target_label}",
        entity_id=sess.user_id,
    )
    db.commit()
    return {"message": "Session revoked"}
