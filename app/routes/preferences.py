"""Per-user onboarding preferences (welcome screen, product tour progress).

Scoped to the signed-in user: there is no user id in the URL, so nobody can read or
change another person's state. Updates are partial; omitted fields stay as they were.
"""
from __future__ import annotations

from typing import Literal

from fastapi import APIRouter, Depends
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy.orm import Session

from app.api_docs import AUTH_401
from app.auth import get_current_user
from app.database import get_db
from app.models import User, UserPreference

router = APIRouter(prefix="/api/me/preferences", tags=["preferences"])

TourStatus = Literal["none", "running", "dismissed", "done"]


class PreferencesOut(BaseModel):
    welcome_seen: bool = False
    welcome_hidden: bool = False
    tour_status: TourStatus = "none"
    tour_step: int = 0


class PreferencesIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    welcome_seen: bool | None = None
    welcome_hidden: bool | None = None
    tour_status: TourStatus | None = None
    tour_step: int | None = Field(default=None, ge=0, le=50)


def _out(row: UserPreference | None) -> PreferencesOut:
    if row is None:
        return PreferencesOut()
    return PreferencesOut(
        welcome_seen=row.welcome_seen, welcome_hidden=row.welcome_hidden,
        tour_status=row.tour_status, tour_step=row.tour_step,
    )


@router.get("", responses=AUTH_401)
def get_preferences(
    db: Session = Depends(get_db), user: User = Depends(get_current_user),
) -> PreferencesOut:
    """The caller's onboarding state; defaults when nothing was saved yet."""
    return _out(db.get(UserPreference, user.id))


@router.put("", responses=AUTH_401)
def update_preferences(
    payload: PreferencesIn,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
) -> PreferencesOut:
    """Merge the given fields into the caller's onboarding state."""
    # Create-if-missing in one statement: two simultaneous first writes would otherwise both
    # insert and the loser would fail on the primary key.
    if db.get_bind().dialect.name == "postgresql":
        from sqlalchemy.dialects.postgresql import insert
    else:
        from sqlalchemy.dialects.sqlite import insert
    db.execute(insert(UserPreference).values(
        user_id=user.id, welcome_seen=False, welcome_hidden=False, tour_status="none", tour_step=0,
    ).on_conflict_do_nothing(index_elements=[UserPreference.user_id]))
    row = db.get(UserPreference, user.id, populate_existing=True)
    for field, value in payload.model_dump(exclude_none=True).items():
        setattr(row, field, value)
    db.commit()
    return _out(row)
