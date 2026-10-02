"""ORM models for Bug Hunter Enterprise: organizations (tenants), users, projects, events,
bugs, comments, attachments, audit log, tokens, sessions, notifications, push, chat, links,
agile, git, and the enterprise tables (invitations, 2FA, saved views, webhooks, custom fields)."""

from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy import (
    JSON,
    Boolean,
    Column,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    LargeBinary,
    Numeric,
    String,
    Table,
    Text,
    TypeDecorator,
    UniqueConstraint,
    event,
    select,
    text,
)
from sqlalchemy.orm import Mapped, deferred, mapped_column, relationship

from app.database import Base


def _utcnow() -> datetime:
    return datetime.now(timezone.utc).replace(microsecond=0)


class UTCDateTime(TypeDecorator):
    """A timezone-aware DateTime that always reads back as UTC.

    PostgreSQL returns timestamptz values aware; SQLite stores no offset and
    returns them naive. Every value is written in UTC (_utcnow), so a naive
    result is tagged UTC here. Otherwise the API emits offset-less timestamps
    that browsers parse as local time, shifting every displayed time.
    """

    impl = DateTime(timezone=True)
    cache_ok = True

    def process_result_value(self, value, dialect):
        if value is not None and value.tzinfo is None:
            return value.replace(tzinfo=timezone.utc)
        return value


# FK target strings as constants to avoid repeating the literal.
_FK_BUGS_ID = "bugs.id"
_FK_USERS_ID = "users.id"
_FK_PROJECTS_ID = "projects.id"
_FK_COMMENTS_ID = "comments.id"
_FK_EVENTS_ID = "events.id"
_FK_ORGS_ID = "organizations.id"
_FK_CUSTOM_FIELDS_ID = "custom_fields.id"

# Shared cascade/ondelete strings.
_CASCADE_ALL_DELETE_ORPHAN = "all, delete-orphan"
_ONDELETE_SET_NULL = "SET NULL"

# --- Junctions ---
bug_assignees = Table(
    "bug_assignees",
    Base.metadata,
    Column("bug_id", Integer, ForeignKey(_FK_BUGS_ID, ondelete="CASCADE"), primary_key=True),
    Column("user_id", Integer, ForeignKey(_FK_USERS_ID, ondelete="CASCADE"), primary_key=True),
)

# user_id is the trailing PK column; standalone index for "bugs assigned to X".
Index("idx_bug_assignees_user_id", bug_assignees.c.user_id)

# event_managers: events <-> owning users. Event-level notifications go here;
# tasks inside an event notify their own assignees separately.
event_managers = Table(
    "event_managers",
    Base.metadata,
    Column("event_id", Integer, ForeignKey(_FK_EVENTS_ID, ondelete="CASCADE"), primary_key=True),
    Column("user_id", Integer, ForeignKey(_FK_USERS_ID, ondelete="CASCADE"), primary_key=True),
)

# user_id is the trailing PK column; standalone index for "events managed by X".
Index("idx_event_managers_user_id", event_managers.c.user_id)

# Project roles. A lead may manage the project's members and custom fields.
PROJECT_ROLE_LEAD = "lead"
PROJECT_ROLE_MEMBER = "member"
PROJECT_ROLES = (PROJECT_ROLE_LEAD, PROJECT_ROLE_MEMBER)

# user_projects: project-scoped access. Non-admins see only items/events/stats/
# reports/audit for projects they belong to; a user with no rows sees nothing.
# Admins see every project of their own organization. Both FKs CASCADE.
user_projects = Table(
    "user_projects",
    Base.metadata,
    Column("user_id", Integer, ForeignKey(_FK_USERS_ID, ondelete="CASCADE"), primary_key=True),
    Column("project_id", Integer, ForeignKey(_FK_PROJECTS_ID, ondelete="CASCADE"), primary_key=True),
    Column("role", String(20), nullable=False, default=PROJECT_ROLE_MEMBER,
           server_default=PROJECT_ROLE_MEMBER),
)

# project_id is the trailing PK column; standalone index for "members of X".
Index("idx_user_projects_project_id", user_projects.c.project_id)


# Roles are enforced in app code, not DB constraints. Authoritative rules:
# app/auth.py (can_edit_bug / can_delete_bug).
ROLE_ADMIN = "admin"
ROLE_MANAGER = "manager"
ROLE_USER = "user"


# A tenant. Every user, project, event and audit row belongs to exactly one;
# everything else (items, boards, sprints, ...) is scoped through its project.
class Organization(Base):
    __tablename__ = "organizations"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    name: Mapped[str] = mapped_column(String(120), nullable=False)
    # URL-safe identifier derived from the name; unique system-wide.
    slug: Mapped[str] = mapped_column(String(80), nullable=False, unique=True)
    description: Mapped[str] = mapped_column(Text, nullable=False, default="")
    # Per-organization branding: a data: URL (checked in routes/branding.py), a CSS
    # hex accent colour, and an outgoing-mail From override.
    logo_data_url: Mapped[str | None] = mapped_column(Text, nullable=True)
    accent_color: Mapped[str | None] = mapped_column(String(16), nullable=True)
    email_from_override: Mapped[str | None] = mapped_column(String(254), nullable=True)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime(), default=_utcnow, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(
        UTCDateTime(), default=_utcnow, onupdate=_utcnow, nullable=False
    )

    __table_args__ = (Index("idx_orgs_slug", "slug"),)


class User(Base):
    __tablename__ = "users"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    org_id: Mapped[int] = mapped_column(
        Integer, ForeignKey(_FK_ORGS_ID, ondelete="CASCADE"), nullable=False
    )
    name: Mapped[str] = mapped_column(String(120), nullable=False)
    email: Mapped[str] = mapped_column(String(254), nullable=False, unique=True)
    role: Mapped[str] = mapped_column(String(20), nullable=False, default=ROLE_USER)
    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)

    # bcrypt hash. Nullable to leave room for SSO, but normally always set.
    password_hash: Mapped[str | None] = mapped_column(String(120), nullable=True)

    # Bumped on password change/reset/forced logout; a cookie with an older
    # version is rejected, logging out other devices.
    session_version: Mapped[int] = mapped_column(Integer, nullable=False, default=0)

    # Two-factor (TOTP). The secret stays NULL until enrolment starts; the second factor
    # is required at login only once totp_enabled is true. The secret may be encrypted
    # at rest (app/secrets_box.py).
    totp_secret: Mapped[str | None] = mapped_column(String(255), nullable=True)
    totp_enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    totp_enrolled_at: Mapped[datetime | None] = mapped_column(UTCDateTime(), nullable=True)
    # Time step of the last accepted authenticator code: a code is good once (RFC 6238 5.2).
    totp_last_step: Mapped[int | None] = mapped_column(Integer, nullable=True)

    created_at: Mapped[datetime] = mapped_column(
        UTCDateTime(), default=_utcnow, nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        UTCDateTime(), default=_utcnow, onupdate=_utcnow, nullable=False
    )

    organization: Mapped[Organization] = relationship("Organization")

    # Case-insensitive uniqueness enforced in app code (routes/users.py
    # `_email_in_use`); a DB expression index is skipped since SQLite can't
    # reflect one (breaks the additive-index idempotency check). Emails are
    # unique across all organizations: the address alone identifies the tenant at login.
    __table_args__ = (
        Index("idx_users_email", "email"),
        Index("idx_users_org_id", "org_id"),
    )


# Single-use email password-reset tokens, stored as a sha256 hash never plaintext.
class PasswordResetToken(Base):
    __tablename__ = "password_reset_tokens"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    user_id: Mapped[int] = mapped_column(
        Integer, ForeignKey(_FK_USERS_ID, ondelete="CASCADE"), nullable=False
    )
    token_hash: Mapped[str] = mapped_column(String(64), nullable=False, unique=True)
    expires_at: Mapped[datetime] = mapped_column(UTCDateTime(), nullable=False)
    used_at: Mapped[datetime | None] = mapped_column(UTCDateTime(), nullable=True)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime(), default=_utcnow, nullable=False)

    __table_args__ = (
        Index("idx_prt_token_hash", "token_hash"),
        # Covers the (user_id, used_at) filter in invalidate_outstanding_reset_tokens.
        Index("idx_prt_user_id_used_at", "user_id", "used_at"),
    )


class Project(Base):
    __tablename__ = "projects"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    org_id: Mapped[int] = mapped_column(
        Integer, ForeignKey(_FK_ORGS_ID, ondelete="CASCADE"), nullable=False
    )
    name: Mapped[str] = mapped_column(String(120), nullable=False)
    # Short identifier shown beside the name (e.g. "WEB"); generated from the name
    # on create, editable, unique within the organization.
    key: Mapped[str] = mapped_column(String(16), nullable=False, default="")
    description: Mapped[str] = mapped_column(Text, nullable=False, default="")
    color: Mapped[str] = mapped_column(String(20), nullable=False, default="#c9764f")
    # Agile module activation. Disabled by
    # default; enabling provisions a default board (see app/agile/boards.py).
    agile_enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    # Allow-listed boolean flags only (validated in schemas.AgileFeatureFlagsIn);
    # empty object default, never a string or null (JSON portability contract).
    agile_feature_flags: Mapped[dict] = mapped_column(JSON, nullable=False, default=dict)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime(), default=_utcnow, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(
        UTCDateTime(), default=_utcnow, onupdate=_utcnow, nullable=False
    )

    organization: Mapped[Organization] = relationship("Organization")
    bugs: Mapped[list["Bug"]] = relationship(
        "Bug", back_populates="project", cascade=_CASCADE_ALL_DELETE_ORPHAN
    )

    # One Git configuration per project; deleting the project removes it.
    git_config: Mapped["ProjectGitConfig | None"] = relationship(
        "ProjectGitConfig", back_populates="project", uselist=False,
        cascade=_CASCADE_ALL_DELETE_ORPHAN,
    )

    # Repository allow-list. No delete-orphan: branch history keeps referencing
    # these rows by FK, so removal is an explicit, guarded operation.
    repositories: Mapped[list["ProjectRepository"]] = relationship(
        "ProjectRepository", back_populates="project",
    )

    __table_args__ = (
        # Names and keys only have to be unique inside one organization.
        UniqueConstraint("org_id", "name", name="uq_projects_org_name"),
        UniqueConstraint("org_id", "key", name="uq_projects_org_key"),
        Index("idx_projects_org_id", "org_id"),
    )


@event.listens_for(Project, "before_insert")
def _give_project_a_key(_mapper, connection, project: Project) -> None:
    """A project saved without a key gets one derived from its name, unique in its organization."""
    if not project.key:
        from app.project_keys import derive_key, unique_key

        project.key = unique_key(connection, project.org_id, derive_key(project.name))


# Groups work items (standup/sprint). Items link via bugs.event_id; deleting an
# event NULLs the FK rather than deleting items, preserving history.
class Event(Base):
    __tablename__ = "events"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    org_id: Mapped[int] = mapped_column(
        Integer, ForeignKey(_FK_ORGS_ID, ondelete="CASCADE"), nullable=False
    )
    name: Mapped[str] = mapped_column(String(200), nullable=False)
    description: Mapped[str] = mapped_column(Text, nullable=False, default="")
    # YYYY-MM-DD, consistent with Bug.due_date.
    scheduled_for: Mapped[str | None] = mapped_column(String(10), nullable=True)
    # Owning project; scopes visibility. Nullable (no backfill needed).
    # SET NULL so deleting a project doesn't cascade-delete its events.
    project_id: Mapped[int | None] = mapped_column(
        Integer, ForeignKey(_FK_PROJECTS_ID, ondelete=_ONDELETE_SET_NULL), nullable=True
    )
    created_by_user_id: Mapped[int | None] = mapped_column(
        Integer, ForeignKey(_FK_USERS_ID, ondelete=_ONDELETE_SET_NULL), nullable=True
    )
    created_at: Mapped[datetime] = mapped_column(UTCDateTime(), default=_utcnow, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(
        UTCDateTime(), default=_utcnow, onupdate=_utcnow, nullable=False
    )

    items: Mapped[list["Bug"]] = relationship(
        "Bug", back_populates="event",
        # App code nulls the FK; items aren't deleted with the event.
        passive_deletes=True,
    )
    managers: Mapped[list["User"]] = relationship(
        "User", secondary=event_managers, lazy="selectin",
    )
    # Eager-loaded so responses include project_name without N+1.
    project: Mapped["Project | None"] = relationship("Project")
    organization: Mapped[Organization] = relationship("Organization")

    __table_args__ = (
        Index("idx_events_org_id", "org_id"),
        Index("idx_events_scheduled_for", "scheduled_for"),
        Index("idx_events_created_by", "created_by_user_id"),
        Index("idx_events_project_id", "project_id"),
    )


# Bug = work item (Bug / Requirement / Task).
class Bug(Base):
    __tablename__ = "bugs"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    display_id: Mapped[str | None] = mapped_column(String(32), nullable=True, index=True)
    project_id: Mapped[int] = mapped_column(
        Integer, ForeignKey(_FK_PROJECTS_ID, ondelete="CASCADE"), nullable=False,
        active_history=True,
    )
    reporter_id: Mapped[int | None] = mapped_column(
        Integer, ForeignKey(_FK_USERS_ID, ondelete=_ONDELETE_SET_NULL), nullable=True
    )
    # Optional event link; SET NULL so deleting an event doesn't cascade to items.
    event_id: Mapped[int | None] = mapped_column(
        Integer, ForeignKey(_FK_EVENTS_ID, ondelete=_ONDELETE_SET_NULL), nullable=True
    )
    title: Mapped[str] = mapped_column(String(200), nullable=False)
    description: Mapped[str] = mapped_column(Text, nullable=False, default="")
    # Original rich-text (HTML) description saved by 3.x, copied once at the
    # first 4.0 boot before descriptions are handled as plain text. Keeps the
    # formatting recoverable; never written by the application afterwards.
    description_legacy_html: Mapped[str | None] = deferred(mapped_column(Text, nullable=True))
    # Bug / Requirement / Task; default "Bug" so pre-column rows read correctly.
    item_type: Mapped[str] = mapped_column(String(20), nullable=False, default="Bug", active_history=True)
    status: Mapped[str] = mapped_column(String(20), nullable=False, default="New", active_history=True)
    priority: Mapped[str] = mapped_column(String(20), nullable=False, default="Medium")
    # Restricted to DEV / UAT / PROD (enforced in schemas).
    environment: Mapped[str] = mapped_column(String(10), nullable=False, default="DEV")
    due_date: Mapped[str | None] = mapped_column(String(10), nullable=True)
    # Story/Task start date; due_date doubles as the hierarchy "end date".
    start_date: Mapped[str | None] = mapped_column(String(10), nullable=True)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime(), default=_utcnow, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(
        UTCDateTime(), default=_utcnow, onupdate=_utcnow, nullable=False
    )
    # Optimistic-concurrency counter, bumped on every update; catches same-second
    # collisions that updated_at's whole-second resolution misses.
    version: Mapped[int] = mapped_column(
        Integer, nullable=False, default=1, server_default="1"
    )
    # --- Agile fields; all additive/nullable. ---
    # Sub-task-only parent link (Level 3 -> Level 2). NULL for every other type.
    parent_id: Mapped[int | None] = mapped_column(
        Integer, ForeignKey(_FK_BUGS_ID, ondelete=_ONDELETE_SET_NULL), nullable=True,
        active_history=True,
    )
    # Epic link for Level 2 items (settable only via the hierarchy endpoint);
    # denormalized/derived for Sub-tasks from their parent. NULL for Epics.
    epic_id: Mapped[int | None] = mapped_column(
        Integer, ForeignKey(_FK_BUGS_ID, ondelete=_ONDELETE_SET_NULL), nullable=True,
        active_history=True,
    )
    # Sprint assignment for Level 2 items only; Sub-tasks always NULL here and
    # derive their effective sprint from the parent at read time.
    sprint_id: Mapped[int | None] = mapped_column(
        Integer, ForeignKey("sprints.id", ondelete=_ONDELETE_SET_NULL), nullable=True,
        active_history=True,
    )
    # LexoRank backlog/sprint ordering token; app-level uniqueness per
    # (rank_scope, rank) — see app/agile/ranking.py. Nullable until backfilled
    # at Agile activation.
    rank_scope: Mapped[str | None] = mapped_column(String(64), nullable=True)
    rank: Mapped[str | None] = mapped_column(String(64), nullable=True)
    story_points: Mapped[float | None] = mapped_column(Numeric(6, 2), nullable=True, active_history=True)
    original_estimate_minutes: Mapped[int | None] = mapped_column(Integer, nullable=True, active_history=True)
    remaining_estimate_minutes: Mapped[int | None] = mapped_column(Integer, nullable=True)
    time_spent_minutes: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    acceptance_criteria: Mapped[str] = mapped_column(Text, nullable=False, default="")
    resolution: Mapped[str | None] = mapped_column(String(50), nullable=True)
    resolved_at: Mapped[datetime | None] = mapped_column(UTCDateTime(), nullable=True)
    flagged: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    # Distinct from reporter_id (creator) and the assignees M2M (contributors) —
    # the single accountable owner the hierarchy contract requires.
    owner_id: Mapped[int | None] = mapped_column(
        Integer, ForeignKey(_FK_USERS_ID, ondelete=_ONDELETE_SET_NULL), nullable=True
    )
    # Story-only: gates entry into a future Sprint; default false per contract.
    ready_for_sprint: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    # Flag + reason, not a workflow column — applies to Story and Sub-task rows.
    blocked: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    blocked_reason: Mapped[str] = mapped_column(Text, nullable=False, default="")
    # Sub-task-only: mandatory Tasks block their parent Story's completion.
    mandatory: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    project: Mapped[Project] = relationship("Project", back_populates="bugs")
    reporter: Mapped["User | None"] = relationship("User", foreign_keys=[reporter_id])
    sprint: Mapped["Sprint | None"] = relationship("Sprint", foreign_keys=[sprint_id], viewonly=True)
    event: Mapped["Event | None"] = relationship("Event", back_populates="items")
    assignees: Mapped[list["User"]] = relationship(
        "User", secondary=bug_assignees, lazy="selectin"
    )
    comments: Mapped[list["Comment"]] = relationship(
        "Comment", back_populates="bug", cascade=_CASCADE_ALL_DELETE_ORPHAN,
        # Newest first; id DESC breaks same-second ties.
        order_by="(Comment.created_at.desc(), Comment.id.desc())",
    )
    # Audit rows outlive deleted bugs: on delete the route nulls bug_id while
    # entity_id/detail keep the original id and title (not cascade-deleted).
    activities: Mapped[list["Activity"]] = relationship(
        "Activity", back_populates="bug",
        order_by="(Activity.created_at.desc(), Activity.id.desc())",
    )
    attachments: Mapped[list["Attachment"]] = relationship(
        "Attachment", back_populates="bug", cascade=_CASCADE_ALL_DELETE_ORPHAN,
        order_by="Attachment.created_at.desc()",
        primaryjoin="Bug.id == Attachment.bug_id",
    )

    __table_args__ = (
        Index("idx_bugs_project_id", "project_id"),
        Index("idx_bugs_reporter_id", "reporter_id"),
        Index("idx_bugs_status", "status"),
        Index("idx_bugs_priority", "priority"),
        Index("idx_bugs_environment", "environment"),
        Index("idx_bugs_item_type", "item_type"),
        Index("idx_bugs_item_type_status", "item_type", "status"),
        Index("idx_bugs_event_id", "event_id"),
        # Composites for common dashboard queries.
        Index("idx_bugs_project_status", "project_id", "status"),
        Index("idx_bugs_status_priority", "status", "priority"),
        Index("idx_bugs_updated_at", "updated_at"),
        # Backs the default list ordering (updated_at DESC, id DESC).
        Index("idx_bugs_updated_id", "updated_at", "id"),
        # Same ordering within one project (sidebar filter, single-project
        # users): 30k rows, page 20 went from 9.5 ms to 0.6 ms on PostgreSQL.
        Index("idx_bugs_project_updated_id", "project_id", "updated_at", "id"),
        # Backs the stats timeline and oldest-first report scans.
        Index("idx_bugs_created_at", "created_at"),
        # Agile hierarchy/backlog/sprint lookups.
        Index("idx_bugs_parent_id", "parent_id"),
        Index("idx_bugs_epic_id", "epic_id"),
        Index("idx_bugs_sprint_id", "sprint_id"),
        Index("idx_bugs_rank_scope_rank", "rank_scope", "rank"),
        # Owner lookups.
        Index("idx_bugs_owner_id", "owner_id"),
    )


class Comment(Base):
    __tablename__ = "comments"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    bug_id: Mapped[int] = mapped_column(Integer, ForeignKey(_FK_BUGS_ID, ondelete="CASCADE"), nullable=False)
    author_user_id: Mapped[int | None] = mapped_column(
        Integer, ForeignKey(_FK_USERS_ID, ondelete=_ONDELETE_SET_NULL), nullable=True
    )
    author_name: Mapped[str] = mapped_column(String(120), nullable=False)
    body: Mapped[str] = mapped_column(Text, nullable=False)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime(), default=_utcnow, nullable=False)

    bug: Mapped[Bug] = relationship("Bug", back_populates="comments")

    __table_args__ = (Index("idx_comments_bug_id", "bug_id"),)


# Files stored as BLOBs in the DB (uploads capped at 50 MB).
# Belongs to a bug directly (comment_id NULL) or to a comment (both set; the
# bug FK keeps it findable by bug-level queries).
class Attachment(Base):
    __tablename__ = "attachments"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    bug_id: Mapped[int] = mapped_column(
        Integer, ForeignKey(_FK_BUGS_ID, ondelete="CASCADE"), nullable=False
    )
    comment_id: Mapped[int | None] = mapped_column(
        Integer, ForeignKey(_FK_COMMENTS_ID, ondelete="CASCADE"), nullable=True
    )
    uploader_user_id: Mapped[int | None] = mapped_column(
        Integer, ForeignKey(_FK_USERS_ID, ondelete=_ONDELETE_SET_NULL), nullable=True
    )
    uploader_name: Mapped[str] = mapped_column(String(120), nullable=False, default="anonymous")
    filename: Mapped[str] = mapped_column(String(255), nullable=False)
    content_type: Mapped[str] = mapped_column(String(120), nullable=False, default="application/octet-stream")
    size_bytes: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    # Deferred so listing doesn't load the BLOB; the download endpoint reads
    # .data lazily. Loading strategy only; schema unchanged.
    data: Mapped[bytes] = deferred(mapped_column(LargeBinary, nullable=False))
    created_at: Mapped[datetime] = mapped_column(UTCDateTime(), default=_utcnow, nullable=False)

    bug: Mapped[Bug] = relationship("Bug", back_populates="attachments", foreign_keys=[bug_id])

    __table_args__ = (
        Index("idx_attachments_bug_id", "bug_id"),
        Index("idx_attachments_comment_id", "comment_id"),
        # Backs the bug-level vs per-comment split query in routes/bugs.py.
        Index("idx_attachments_bug_comment", "bug_id", "comment_id"),
    )


def _activity_org_id(context) -> int | None:
    """Tenant of an audit row written without an explicit org_id: the actor's
    organization, else the item's project's. Rows with neither fail loudly (NOT NULL)."""
    params = context.get_current_parameters()
    conn = context.connection
    actor_id, bug_id = params.get("actor_user_id"), params.get("bug_id")
    if actor_id is not None:
        org_id = conn.scalar(select(User.__table__.c.org_id).where(User.__table__.c.id == actor_id))
        if org_id is not None:
            return org_id
    if bug_id is not None:
        return conn.scalar(
            select(Project.__table__.c.org_id)
            .join(Bug.__table__, Bug.__table__.c.project_id == Project.__table__.c.id)
            .where(Bug.__table__.c.id == bug_id)
        )
    return None


# Audit trail. bug_id nullable so non-bug events (user/project/etc.) log here too.
# org_id is denormalised so the audit view filters by tenant without joins.
class Activity(Base):
    __tablename__ = "activity_log"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    org_id: Mapped[int] = mapped_column(
        Integer, ForeignKey(_FK_ORGS_ID, ondelete="CASCADE"), nullable=False,
        default=_activity_org_id,
    )
    # SET NULL so audit rows survive bug deletion; entity_id/detail keep the
    # original reference.
    bug_id: Mapped[int | None] = mapped_column(
        Integer, ForeignKey(_FK_BUGS_ID, ondelete=_ONDELETE_SET_NULL), nullable=True
    )
    # entity_type + entity_id reference any object type without a FK; metadata only.
    entity_type: Mapped[str] = mapped_column(String(40), nullable=False, default="bug")
    entity_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    actor_user_id: Mapped[int | None] = mapped_column(
        Integer, ForeignKey(_FK_USERS_ID, ondelete=_ONDELETE_SET_NULL), nullable=True
    )
    actor_name: Mapped[str] = mapped_column(String(120), nullable=False, default="system")
    action: Mapped[str] = mapped_column(String(60), nullable=False)
    detail: Mapped[str] = mapped_column(Text, nullable=False, default="")
    created_at: Mapped[datetime] = mapped_column(UTCDateTime(), default=_utcnow, nullable=False)

    bug: Mapped[Bug | None] = relationship("Bug", back_populates="activities")

    __table_args__ = (
        Index("idx_activity_org_created", "org_id", "created_at"),
        Index("idx_activity_bug_id", "bug_id"),
        Index("idx_activity_entity", "entity_type", "entity_id"),
        Index("idx_activity_created", "created_at"),
        # Reports filter action == "status_changed" joined to bugs.
        Index("idx_activity_action_bug", "action", "bug_id"),
        # Lets resolution/throughput/timeline reports avoid a sort over the audit table.
        Index("idx_activity_action_bug_created", "action", "bug_id", "created_at"),
        # Keeps the action + created_at range scan sargable (bug_id sits between
        # the predicates in the index above).
        Index("idx_activity_action_created", "action", "created_at"),
        Index("idx_activity_actor_user_id", "actor_user_id"),
    )


# Server-side login records for admin list/revoke. Keyed by a jti also embedded
# in the signed cookie; every request looks it up, a missing/expired row rejects
# the cookie, and revoke deletes the row. Pre-table cookies carry no jti and are
# accepted but absent from the list until next login.
class Session(Base):
    __tablename__ = "sessions"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    user_id: Mapped[int] = mapped_column(
        Integer, ForeignKey(_FK_USERS_ID, ondelete="CASCADE"), nullable=False
    )
    # Random opaque ID baked into the signed cookie; looked up on every request.
    jti: Mapped[str] = mapped_column(String(64), nullable=False, unique=True)
    # Display metadata for the admin session list; not used for auth.
    user_agent: Mapped[str] = mapped_column(String(400), nullable=False, default="")
    ip_address: Mapped[str] = mapped_column(String(64), nullable=False, default="")
    created_at: Mapped[datetime] = mapped_column(UTCDateTime(), default=_utcnow, nullable=False)
    last_seen_at: Mapped[datetime] = mapped_column(
        UTCDateTime(), default=_utcnow, onupdate=_utcnow, nullable=False
    )
    expires_at: Mapped[datetime] = mapped_column(UTCDateTime(), nullable=False)

    __table_args__ = (
        Index("idx_sessions_jti", "jti"),
        Index("idx_sessions_user_id", "user_id"),
        Index("idx_sessions_expires_at", "expires_at"),
    )


# Per-user in-app notifications, parallel to email_service. Same recipients
# (reporter + assignees minus actor, event managers). Scoped to user_id,
# cascade-deletes with the user; no endpoint returns another user's rows.
class Notification(Base):
    __tablename__ = "notifications"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    user_id: Mapped[int] = mapped_column(
        Integer, ForeignKey(_FK_USERS_ID, ondelete="CASCADE"), nullable=False
    )
    # Drives the frontend icon/label:
    # "assigned" | "reported" | "updated" | "comment" | "event".
    kind: Mapped[str] = mapped_column(String(30), nullable=False)
    title: Mapped[str] = mapped_column(String(200), nullable=False)
    body: Mapped[str] = mapped_column(Text, nullable=False, default="")
    # Deep-link targets; CASCADE so the row is removed with its bug or event.
    bug_id: Mapped[int | None] = mapped_column(
        Integer, ForeignKey(_FK_BUGS_ID, ondelete="CASCADE"), nullable=True
    )
    event_id: Mapped[int | None] = mapped_column(
        Integer, ForeignKey(_FK_EVENTS_ID, ondelete="CASCADE"), nullable=True
    )
    # No FK, so the snapshot survives if the actor's account is deleted.
    actor_name: Mapped[str] = mapped_column(String(120), nullable=False, default="")
    # NULL means unread.
    read_at: Mapped[datetime | None] = mapped_column(UTCDateTime(), nullable=True)
    # NULL until the daily digest sends it, then stamped (idempotent). Independent
    # of read_at.
    emailed_at: Mapped[datetime | None] = mapped_column(
        UTCDateTime(), nullable=True
    )
    created_at: Mapped[datetime] = mapped_column(
        UTCDateTime(), default=_utcnow, nullable=False
    )

    __table_args__ = (
        Index("idx_notifications_user_id", "user_id"),
        # Unread badge/list filter on (user_id, read_at).
        Index("idx_notifications_user_read", "user_id", "read_at"),
        # Panel lists newest-first per user.
        Index("idx_notifications_user_created", "user_id", "created_at"),
        # Digest job scans emailed_at IS NULL.
        Index("idx_notifications_emailed_at", "emailed_at"),
        # Composite lets the digest seek into the created_at window.
        Index("idx_notifications_emailed_created", "emailed_at", "created_at"),
    )


# Web push (FCM): one row per device that granted permission, keyed by the FCM
# token. Also serves native clients (platform). Sent immediately, not digested.
class PushSubscription(Base):
    __tablename__ = "push_subscriptions"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    user_id: Mapped[int] = mapped_column(
        Integer, ForeignKey(_FK_USERS_ID, ondelete="CASCADE"), nullable=False
    )
    # FCM registration token; unique per device/browser install.
    token: Mapped[str] = mapped_column(String(512), nullable=False, unique=True)
    # "web" default; "android"/"ios" for native clients.
    platform: Mapped[str] = mapped_column(String(20), nullable=False, default="web")
    # Coarse device hint for the device list and debugging.
    user_agent: Mapped[str] = mapped_column(String(400), nullable=False, default="")
    created_at: Mapped[datetime] = mapped_column(
        UTCDateTime(), default=_utcnow, nullable=False
    )
    # Refreshed on re-subscribe (tokens rotate); a cleanup job can drop stale ones.
    last_seen_at: Mapped[datetime] = mapped_column(
        UTCDateTime(), default=_utcnow, onupdate=_utcnow, nullable=False
    )

    __table_args__ = (
        Index("idx_push_subscriptions_user_id", "user_id"),
        Index("idx_push_subscriptions_token", "token"),
    )


# Per-user push opt-outs. A missing row means every channel is on.
class NotificationPreference(Base):
    __tablename__ = "notification_preferences"

    user_id: Mapped[int] = mapped_column(
        Integer, ForeignKey(_FK_USERS_ID, ondelete="CASCADE"), primary_key=True
    )
    mentions: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    assignments: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    activity: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    updated_at: Mapped[datetime] = mapped_column(
        UTCDateTime(), default=_utcnow, onupdate=_utcnow, nullable=False
    )


# Staged email change: the new address gets a 6-digit code and nothing changes until
# it is entered, so a hijacked session cannot silently swap the recovery address.
class EmailChangeRequest(Base):
    __tablename__ = "email_change_requests"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    user_id: Mapped[int] = mapped_column(
        Integer, ForeignKey(_FK_USERS_ID, ondelete="CASCADE"), nullable=False
    )
    new_email: Mapped[str] = mapped_column(String(254), nullable=False)
    code_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    expires_at: Mapped[datetime] = mapped_column(UTCDateTime(), nullable=False)
    used_at: Mapped[datetime | None] = mapped_column(UTCDateTime(), nullable=True)
    # Failed tries; the request is voided after a handful so the code cannot be guessed.
    attempts: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime(), default=_utcnow, nullable=False)

    __table_args__ = (Index("idx_ecr_user", "user_id"),)


# Emailed invitation to join an organization. Accepting it creates the user row.
class Invitation(Base):
    __tablename__ = "invitations"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    org_id: Mapped[int] = mapped_column(
        Integer, ForeignKey(_FK_ORGS_ID, ondelete="CASCADE"), nullable=False
    )
    email: Mapped[str] = mapped_column(String(254), nullable=False)
    role: Mapped[str] = mapped_column(String(20), nullable=False, default=ROLE_USER)
    # sha256 of the emailed token; the plaintext is never stored.
    token_hash: Mapped[str] = mapped_column(String(64), nullable=False, unique=True)
    invited_by_user_id: Mapped[int | None] = mapped_column(
        Integer, ForeignKey(_FK_USERS_ID, ondelete=_ONDELETE_SET_NULL), nullable=True
    )
    invited_by_name: Mapped[str] = mapped_column(String(120), nullable=False, default="")
    # Comma-separated project ids joined on acceptance; "L:7" joins project 7 as its lead.
    initial_project_ids: Mapped[str] = mapped_column(String(500), nullable=False, default="")
    expires_at: Mapped[datetime] = mapped_column(UTCDateTime(), nullable=False)
    accepted_at: Mapped[datetime | None] = mapped_column(UTCDateTime(), nullable=True)
    revoked_at: Mapped[datetime | None] = mapped_column(UTCDateTime(), nullable=True)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime(), default=_utcnow, nullable=False)

    organization: Mapped[Organization] = relationship("Organization")

    __table_args__ = (
        Index("idx_invites_token_hash", "token_hash"),
        Index("idx_invites_org_id", "org_id"),
        Index("idx_invites_email", "email"),
    )


# One-time backup codes for a 2FA user; only hashes are stored.
class TotpRecoveryCode(Base):
    __tablename__ = "totp_recovery_codes"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    user_id: Mapped[int] = mapped_column(
        Integer, ForeignKey(_FK_USERS_ID, ondelete="CASCADE"), nullable=False
    )
    code_hash: Mapped[str] = mapped_column(String(64), nullable=False, unique=True)
    used_at: Mapped[datetime | None] = mapped_column(UTCDateTime(), nullable=True)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime(), default=_utcnow, nullable=False)

    __table_args__ = (
        Index("idx_trc_user", "user_id"),
        Index("idx_trc_hash", "code_hash"),
    )


# A named filter set shown above the item list. Private to its owner unless a
# manager/admin shares it with the whole organization.
class SavedView(Base):
    __tablename__ = "saved_views"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    org_id: Mapped[int] = mapped_column(
        Integer, ForeignKey(_FK_ORGS_ID, ondelete="CASCADE"), nullable=False
    )
    owner_user_id: Mapped[int] = mapped_column(
        Integer, ForeignKey(_FK_USERS_ID, ondelete="CASCADE"), nullable=False
    )
    name: Mapped[str] = mapped_column(String(80), nullable=False)
    # JSON object mirroring the list view's filter state.
    filters_json: Mapped[str] = mapped_column(Text, nullable=False, default="{}")
    shared_with_org: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime(), default=_utcnow, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(
        UTCDateTime(), default=_utcnow, onupdate=_utcnow, nullable=False
    )

    __table_args__ = (
        Index("idx_views_org", "org_id"),
        Index("idx_views_owner", "owner_user_id"),
    )


# Outbound HTTP destination for organization events. Bodies are signed with HMAC-SHA256
# using `secret` (which may be encrypted at rest); ten failures in a row suspend the hook.
class Webhook(Base):
    __tablename__ = "webhooks"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    org_id: Mapped[int] = mapped_column(
        Integer, ForeignKey(_FK_ORGS_ID, ondelete="CASCADE"), nullable=False
    )
    name: Mapped[str] = mapped_column(String(80), nullable=False)
    url: Mapped[str] = mapped_column(String(500), nullable=False)
    secret: Mapped[str] = mapped_column(String(255), nullable=False)
    # Comma-separated event names; "*" subscribes to everything, "bug.*" to a family.
    events: Mapped[str] = mapped_column(String(500), nullable=False, default="*")
    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    consecutive_failures: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    last_delivered_at: Mapped[datetime | None] = mapped_column(UTCDateTime(), nullable=True)
    last_status_code: Mapped[int | None] = mapped_column(Integer, nullable=True)
    last_error: Mapped[str | None] = mapped_column(String(500), nullable=True)
    created_by_user_id: Mapped[int | None] = mapped_column(
        Integer, ForeignKey(_FK_USERS_ID, ondelete=_ONDELETE_SET_NULL), nullable=True
    )
    created_at: Mapped[datetime] = mapped_column(UTCDateTime(), default=_utcnow, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(
        UTCDateTime(), default=_utcnow, onupdate=_utcnow, nullable=False
    )

    __table_args__ = (
        Index("idx_webhooks_org", "org_id"),
        Index("idx_webhooks_active", "is_active"),
    )


# Per-project user-defined fields on items. Definitions and values are separate so a
# definition can change without touching stored answers.
class CustomField(Base):
    __tablename__ = "custom_fields"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    project_id: Mapped[int] = mapped_column(
        Integer, ForeignKey(_FK_PROJECTS_ID, ondelete="CASCADE"), nullable=False
    )
    name: Mapped[str] = mapped_column(String(80), nullable=False)
    # text | number | date | select
    field_type: Mapped[str] = mapped_column(String(20), nullable=False, default="text")
    # Pipe-separated choices for select fields.
    options: Mapped[str] = mapped_column(String(500), nullable=False, default="")
    is_required: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    position: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime(), default=_utcnow, nullable=False)

    __table_args__ = (
        UniqueConstraint("project_id", "name", name="uq_cf_project_name"),
        Index("idx_cf_project", "project_id"),
    )


class BugCustomValue(Base):
    __tablename__ = "bug_custom_values"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    bug_id: Mapped[int] = mapped_column(
        Integer, ForeignKey(_FK_BUGS_ID, ondelete="CASCADE"), nullable=False
    )
    field_id: Mapped[int] = mapped_column(
        Integer, ForeignKey(_FK_CUSTOM_FIELDS_ID, ondelete="CASCADE"), nullable=False
    )
    value: Mapped[str] = mapped_column(Text, nullable=False, default="")
    created_at: Mapped[datetime] = mapped_column(UTCDateTime(), default=_utcnow, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(
        UTCDateTime(), default=_utcnow, onupdate=_utcnow, nullable=False
    )

    __table_args__ = (
        UniqueConstraint("bug_id", "field_id", name="uq_bcv_bug_field"),
        Index("idx_bcv_bug", "bug_id"),
        Index("idx_bcv_field", "field_id"),
    )


# Sleuth chat memory: durable transcript surviving restarts, giving the LLM a
# rolling history. Both tables scoped to user_id, cascade-delete with the user.
class ChatConversation(Base):
    __tablename__ = "chat_conversations"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    user_id: Mapped[int] = mapped_column(
        Integer, ForeignKey(_FK_USERS_ID, ondelete="CASCADE"), nullable=False
    )
    created_at: Mapped[datetime] = mapped_column(UTCDateTime(), default=_utcnow, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(
        UTCDateTime(), default=_utcnow, onupdate=_utcnow, nullable=False
    )

    messages: Mapped[list["ChatMessage"]] = relationship(
        "ChatMessage", back_populates="conversation",
        cascade=_CASCADE_ALL_DELETE_ORPHAN,
        # id tiebreaker: _utcnow() truncates to whole seconds, so a same-second
        # turn would otherwise replay in an unstable order.
        order_by="ChatMessage.created_at, ChatMessage.id",
    )

    __table_args__ = (Index("idx_chat_conv_user_id", "user_id"),)


class ChatMessage(Base):
    __tablename__ = "chat_messages"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    conversation_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("chat_conversations.id", ondelete="CASCADE"), nullable=False
    )
    # "user" | "assistant"
    role: Mapped[str] = mapped_column(String(16), nullable=False)
    content: Mapped[str] = mapped_column(Text, nullable=False)
    # Engine that produced the response: "rules" | "classifier" | "llm" | "cloud" | "".
    # Observability only; never used for auth.
    engine: Mapped[str] = mapped_column(String(20), nullable=False, default="")
    created_at: Mapped[datetime] = mapped_column(UTCDateTime(), default=_utcnow, nullable=False)

    conversation: Mapped[ChatConversation] = relationship(
        "ChatConversation", back_populates="messages"
    )

    __table_args__ = (Index("idx_chat_msg_conversation_id", "conversation_id"),)


# Directed relationship between two items (e.g. "#12 blocks #34"). One edge
# (source -> target) with a link_type; the route derives the inverse label, so
# no reverse rows. Both FKs CASCADE.
class BugLink(Base):
    __tablename__ = "bug_links"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    source_bug_id: Mapped[int] = mapped_column(
        Integer, ForeignKey(_FK_BUGS_ID, ondelete="CASCADE"), nullable=False
    )
    target_bug_id: Mapped[int] = mapped_column(
        Integer, ForeignKey(_FK_BUGS_ID, ondelete="CASCADE"), nullable=False
    )
    # "relates" | "blocks" | "duplicate"; validated in the schema layer.
    link_type: Mapped[str] = mapped_column(String(20), nullable=False, default="relates")
    created_by_user_id: Mapped[int | None] = mapped_column(
        Integer, ForeignKey(_FK_USERS_ID, ondelete=_ONDELETE_SET_NULL), nullable=True
    )
    created_at: Mapped[datetime] = mapped_column(
        UTCDateTime(), default=_utcnow, nullable=False
    )

    source: Mapped["Bug"] = relationship("Bug", foreign_keys=[source_bug_id])
    target: Mapped["Bug"] = relationship("Bug", foreign_keys=[target_bug_id])

    __table_args__ = (
        # One edge per (source, target, type); re-linking is a no-op.
        Index("idx_bug_links_unique", "source_bug_id", "target_bug_id", "link_type", unique=True),
        Index("idx_bug_links_source", "source_bug_id"),
        Index("idx_bug_links_target", "target_bug_id"),
    )


# =====================================================================
# Agile — foundation, boards, backlog, sprint lifecycle.
#
# All additive: new tables only, plus the Project/Bug columns declared above.
# =====================================================================
_FK_BOARDS_ID = "boards.id"
_FK_SPRINTS_ID = "sprints.id"
_FK_WORKFLOW_STATUSES_ID = "workflow_statuses.id"


# Global (scope_key="global") rows are seeded at boot from app/schemas.py
# STATUSES_BY_TYPE. Project-scoped rows ("project:<id>") are
# reserved for later use; only global rows are seeded and used today.
class WorkflowStatus(Base):
    __tablename__ = "workflow_statuses"
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    scope_key: Mapped[str] = mapped_column(String(50), nullable=False, default="global")
    work_item_type: Mapped[str] = mapped_column(String(20), nullable=False)
    key: Mapped[str] = mapped_column(String(50), nullable=False)
    # Exact string written to bugs.status for this status.
    persisted_status_value: Mapped[str] = mapped_column(String(50), nullable=False)
    name: Mapped[str] = mapped_column(String(100), nullable=False)
    category: Mapped[str] = mapped_column(String(20), nullable=False)  # category: to-do / in_progress / done
    color: Mapped[str] = mapped_column(String(20), nullable=False, default="")
    position: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    is_initial: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    is_terminal: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)

    __table_args__ = (
        Index(
            "idx_workflow_statuses_unique", "scope_key", "work_item_type", "key",
            unique=True,
        ),
        Index("idx_workflow_statuses_scope", "scope_key", "work_item_type"),
    )


class Board(Base):
    __tablename__ = "boards"
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    display_id: Mapped[str | None] = mapped_column(String(32), nullable=True, index=True)
    project_id: Mapped[int] = mapped_column(
        Integer, ForeignKey(_FK_PROJECTS_ID, ondelete="CASCADE"), nullable=False
    )
    name: Mapped[str] = mapped_column(String(120), nullable=False)
    name_normalized: Mapped[str] = mapped_column(String(120), nullable=False)
    board_type: Mapped[str] = mapped_column(String(20), nullable=False, default="scrum")
    is_default: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    filter_json: Mapped[dict] = mapped_column(JSON, nullable=False, default=dict)
    estimation_mode: Mapped[str] = mapped_column(String(20), nullable=False, default="story_points")
    swimlane_mode: Mapped[str] = mapped_column(String(20), nullable=False, default="none")
    card_fields_json: Mapped[list] = mapped_column(JSON, nullable=False, default=list)
    wip_enforcement: Mapped[str] = mapped_column(String(10), nullable=False, default="off")
    card_color_scheme: Mapped[str] = mapped_column(String(20), nullable=False, default="none")
    timezone: Mapped[str] = mapped_column(String(64), nullable=False, default="UTC")
    working_weekdays: Mapped[list] = mapped_column(JSON, nullable=False, default=lambda: [1, 2, 3, 4, 5])
    working_hours_per_day: Mapped[float] = mapped_column(Numeric(4, 2), nullable=False, default=8)
    created_by_id: Mapped[int | None] = mapped_column(
        Integer, ForeignKey(_FK_USERS_ID, ondelete=_ONDELETE_SET_NULL), nullable=True
    )
    version: Mapped[int] = mapped_column(Integer, nullable=False, default=1, server_default="1")
    created_at: Mapped[datetime] = mapped_column(UTCDateTime(), default=_utcnow, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(
        UTCDateTime(), default=_utcnow, onupdate=_utcnow, nullable=False
    )
    columns: Mapped[list["BoardColumn"]] = relationship(
        "BoardColumn", back_populates="board", cascade=_CASCADE_ALL_DELETE_ORPHAN,
        order_by="BoardColumn.position",
    )

    __table_args__ = (
        Index("idx_boards_unique_name", "project_id", "name_normalized", unique=True),
        Index("idx_boards_project_id", "project_id"),
    )


class BoardColumn(Base):
    __tablename__ = "board_columns"
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    board_id: Mapped[int] = mapped_column(
        Integer, ForeignKey(_FK_BOARDS_ID, ondelete="CASCADE"), nullable=False
    )
    name: Mapped[str] = mapped_column(String(80), nullable=False)
    position: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    category: Mapped[str] = mapped_column(String(20), nullable=False)  # category: to-do / in_progress / done
    wip_limit: Mapped[int | None] = mapped_column(Integer, nullable=True)
    min_cards: Mapped[int | None] = mapped_column(Integer, nullable=True)
    wip_enforcement: Mapped[str] = mapped_column(String(10), nullable=False, default="off")
    color: Mapped[str] = mapped_column(String(20), nullable=False, default="")
    board: Mapped[Board] = relationship("Board", back_populates="columns")
    statuses: Mapped[list["BoardColumnStatus"]] = relationship(
        "BoardColumnStatus", back_populates="column", cascade=_CASCADE_ALL_DELETE_ORPHAN,
    )

    __table_args__ = (
        Index("idx_board_columns_board_id", "board_id"),
        Index("idx_board_columns_board_position", "board_id", "position"),
    )


# Composite-PK join: which workflow statuses map into which board column.
# No independent version — mutated only via the parent Board's optimistic
# lock.
class BoardColumnStatus(Base):
    __tablename__ = "board_column_statuses"
    board_column_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("board_columns.id", ondelete="CASCADE"), primary_key=True
    )
    workflow_status_id: Mapped[int] = mapped_column(
        Integer, ForeignKey(_FK_WORKFLOW_STATUSES_ID, ondelete="CASCADE"), primary_key=True
    )
    column: Mapped[BoardColumn] = relationship("BoardColumn", back_populates="statuses")
    workflow_status: Mapped[WorkflowStatus] = relationship("WorkflowStatus")

    __table_args__ = (
        Index("idx_board_column_statuses_status", "workflow_status_id"),
    )


sprint_assignees = Table(
    "sprint_assignees",
    Base.metadata,
    Column("sprint_id", Integer, ForeignKey(_FK_SPRINTS_ID, ondelete="CASCADE"), primary_key=True),
    Column("user_id", Integer, ForeignKey(_FK_USERS_ID, ondelete="CASCADE"), primary_key=True),
)
Index("idx_sprint_assignees_user_id", sprint_assignees.c.user_id)


class Sprint(Base):
    __tablename__ = "sprints"
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    board_id: Mapped[int] = mapped_column(
        Integer, ForeignKey(_FK_BOARDS_ID, ondelete="CASCADE"), nullable=False
    )
    project_id: Mapped[int] = mapped_column(
        Integer, ForeignKey(_FK_PROJECTS_ID, ondelete="CASCADE"), nullable=False
    )
    name: Mapped[str] = mapped_column(String(120), nullable=False)
    name_normalized: Mapped[str] = mapped_column(String(120), nullable=False)
    goal: Mapped[str] = mapped_column(Text, nullable=False, default="")
    state: Mapped[str] = mapped_column(String(20), nullable=False, default="future")
    start_date: Mapped[str | None] = mapped_column(String(10), nullable=True)
    end_date: Mapped[str | None] = mapped_column(String(10), nullable=True)
    # daily|weekly|monthly|custom — purely advisory, used to auto-suggest the
    # next sprint's dates; never drives background automation (no workers).
    cadence: Mapped[str | None] = mapped_column(String(20), nullable=True)
    # Exact start instant (sub-second). Reports read sprint membership and
    # estimates "as of" this moment from work_item_changes. NULL for sprints
    # started before 4.0 tracked it; their reports fall back to the history rows.
    started_at: Mapped[datetime | None] = mapped_column(UTCDateTime(), nullable=True)
    completed_at: Mapped[datetime | None] = mapped_column(UTCDateTime(), nullable=True)
    cancelled_at: Mapped[datetime | None] = mapped_column(UTCDateTime(), nullable=True)
    sequence_number: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    position: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    created_by_id: Mapped[int | None] = mapped_column(
        Integer, ForeignKey(_FK_USERS_ID, ondelete=_ONDELETE_SET_NULL), nullable=True
    )
    started_by_id: Mapped[int | None] = mapped_column(
        Integer, ForeignKey(_FK_USERS_ID, ondelete=_ONDELETE_SET_NULL), nullable=True
    )
    completed_by_id: Mapped[int | None] = mapped_column(
        Integer, ForeignKey(_FK_USERS_ID, ondelete=_ONDELETE_SET_NULL), nullable=True
    )
    version: Mapped[int] = mapped_column(Integer, nullable=False, default=1, server_default="1")
    created_at: Mapped[datetime] = mapped_column(UTCDateTime(), default=_utcnow, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(
        UTCDateTime(), default=_utcnow, onupdate=_utcnow, nullable=False
    )
    assignees: Mapped[list["User"]] = relationship(
        "User", secondary=sprint_assignees, lazy="selectin"
    )

    __table_args__ = (
        Index("idx_sprints_unique_name", "board_id", "name_normalized", unique=True),
        Index("idx_sprints_board_id", "board_id"),
        Index("idx_sprints_project_id", "project_id"),
        Index("idx_sprints_board_state", "board_id", "state"),
    )


# Immutable sprint-item event log. Never updated after insert;
# never backfilled from current state.
# Sprint assignees: M2M following the same convention as bug_assignees.
class SprintItemHistory(Base):
    __tablename__ = "sprint_item_history"
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    sprint_id: Mapped[int] = mapped_column(
        Integer, ForeignKey(_FK_SPRINTS_ID, ondelete="CASCADE"), nullable=False
    )
    work_item_id: Mapped[int] = mapped_column(
        Integer, ForeignKey(_FK_BUGS_ID, ondelete="CASCADE"), nullable=False
    )
    # committed|added|removed|status_changed|estimate_changed|completed|reopened|carried_over
    event_type: Mapped[str] = mapped_column(String(30), nullable=False)
    from_sprint_id: Mapped[int | None] = mapped_column(
        Integer, ForeignKey(_FK_SPRINTS_ID, ondelete=_ONDELETE_SET_NULL), nullable=True
    )
    to_sprint_id: Mapped[int | None] = mapped_column(
        Integer, ForeignKey(_FK_SPRINTS_ID, ondelete=_ONDELETE_SET_NULL), nullable=True
    )
    # No ondelete action: a workflow_status referenced by history is never
    # hard-deleted (deactivated via is_active instead)
    old_status_id: Mapped[int | None] = mapped_column(
        Integer, ForeignKey(_FK_WORKFLOW_STATUSES_ID), nullable=True
    )
    new_status_id: Mapped[int | None] = mapped_column(
        Integer, ForeignKey(_FK_WORKFLOW_STATUSES_ID), nullable=True
    )
    old_estimate: Mapped[float | None] = mapped_column(Numeric(10, 2), nullable=True)
    new_estimate: Mapped[float | None] = mapped_column(Numeric(10, 2), nullable=True)
    occurred_at: Mapped[datetime] = mapped_column(UTCDateTime(), default=_utcnow, nullable=False)
    actor_id: Mapped[int | None] = mapped_column(
        Integer, ForeignKey(_FK_USERS_ID, ondelete=_ONDELETE_SET_NULL), nullable=True
    )
    metadata_json: Mapped[dict] = mapped_column(JSON, nullable=False, default=dict)

    __table_args__ = (
        Index("idx_sprint_item_history_sprint_id", "sprint_id"),
        Index("idx_sprint_item_history_work_item_id", "work_item_id"),
        Index("idx_sprint_item_history_sprint_item", "sprint_id", "work_item_id"),
        Index("idx_sprint_item_history_occurred_at", "occurred_at"),
    )


def _utcnow_precise() -> datetime:
    return datetime.now(timezone.utc)


# Append-only change log of the work-item fields that agile reports replay
# (Jira's issue changelog). Rows are written by app.agile.integrity on every
# flush that changes a tracked field, whatever the write path; nothing else
# inserts, updates or deletes them.
class WorkItemChange(Base):
    __tablename__ = "work_item_changes"
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    work_item_id: Mapped[int] = mapped_column(
        Integer, ForeignKey(_FK_BUGS_ID, ondelete="CASCADE"), nullable=False
    )
    # The item's project when the change happened (a project move records the
    # new one), so board-level reports can select by project.
    project_id: Mapped[int] = mapped_column(
        Integer, ForeignKey(_FK_PROJECTS_ID, ondelete="CASCADE"), nullable=False
    )
    # created | status | sprint | story_points | original_estimate | epic |
    # parent | item_type | project
    field: Mapped[str] = mapped_column(String(32), nullable=False)
    old_value: Mapped[str | None] = mapped_column(String(64), nullable=True)
    new_value: Mapped[str | None] = mapped_column(String(64), nullable=True)
    changed_at: Mapped[datetime] = mapped_column(UTCDateTime(), default=_utcnow_precise, nullable=False)
    actor_id: Mapped[int | None] = mapped_column(
        Integer, ForeignKey(_FK_USERS_ID, ondelete=_ONDELETE_SET_NULL), nullable=True
    )
    # Why the change happened when it was a side effect, e.g. "sprint_completed"
    # for issues carried over by Complete Sprint. NULL for direct edits.
    reason: Mapped[str | None] = mapped_column(String(32), nullable=True)
    work_item: Mapped["Bug"] = relationship("Bug", foreign_keys=[work_item_id])

    __table_args__ = (
        Index("idx_work_item_changes_item_time", "work_item_id", "changed_at"),
        Index("idx_work_item_changes_project_field_time", "project_id", "field", "changed_at"),
    )


# Replay-safe mutation dedup for Start/Complete/Cancel Sprint and bulk ops
#. Rows past expires_at are cleanup-eligible.
class IdempotencyKey(Base):
    __tablename__ = "idempotency_keys"
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    user_id: Mapped[int] = mapped_column(
        Integer, ForeignKey(_FK_USERS_ID, ondelete="CASCADE"), nullable=False
    )
    project_id: Mapped[int] = mapped_column(
        Integer, ForeignKey(_FK_PROJECTS_ID, ondelete="CASCADE"), nullable=False
    )
    operation_key: Mapped[str] = mapped_column(String(100), nullable=False)
    idempotency_key: Mapped[str] = mapped_column(String(200), nullable=False)
    request_fingerprint: Mapped[str] = mapped_column(String(64), nullable=False)
    response_status: Mapped[int] = mapped_column(Integer, nullable=False)
    response_body: Mapped[str] = mapped_column(Text, nullable=False, default="")
    created_at: Mapped[datetime] = mapped_column(UTCDateTime(), default=_utcnow, nullable=False)
    expires_at: Mapped[datetime] = mapped_column(UTCDateTime(), nullable=False)

    __table_args__ = (
        Index(
            "idx_idempotency_keys_unique",
            "user_id", "project_id", "operation_key", "idempotency_key",
            unique=True,
        ),
        Index("idx_idempotency_keys_expires_at", "expires_at"),
    )


# =====================================================================
# Agile — workflow transitions, quick filters (board UX).
# =====================================================================
# Global (unscoped) transition rule between two workflow statuses for a work
# item type. Only global workflow_statuses are seeded, so transitions are
# global too; project-specific transition overrides are a later slice.
class WorkflowTransition(Base):
    __tablename__ = "workflow_transitions"
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    work_item_type: Mapped[str] = mapped_column(String(20), nullable=False)
    from_status_id: Mapped[int] = mapped_column(
        Integer, ForeignKey(_FK_WORKFLOW_STATUSES_ID), nullable=False
    )
    to_status_id: Mapped[int] = mapped_column(
        Integer, ForeignKey(_FK_WORKFLOW_STATUSES_ID), nullable=False
    )
    name: Mapped[str] = mapped_column(String(100), nullable=False, default="")
    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)

    __table_args__ = (
        Index(
            "idx_workflow_transitions_unique",
            "work_item_type", "from_status_id", "to_status_id",
            unique=True,
        ),
        Index("idx_workflow_transitions_from", "from_status_id"),
    )


class QuickFilter(Base):
    __tablename__ = "quick_filters"
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    board_id: Mapped[int] = mapped_column(
        Integer, ForeignKey(_FK_BOARDS_ID, ondelete="CASCADE"), nullable=False
    )
    name: Mapped[str] = mapped_column(String(120), nullable=False)
    filter_json: Mapped[dict] = mapped_column(JSON, nullable=False, default=dict)
    position: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    is_shared: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    created_by_id: Mapped[int | None] = mapped_column(
        Integer, ForeignKey(_FK_USERS_ID, ondelete=_ONDELETE_SET_NULL), nullable=True
    )
    version: Mapped[int] = mapped_column(Integer, nullable=False, default=1, server_default="1")

    __table_args__ = (Index("idx_quick_filters_board_id", "board_id"),)


# =====================================================================
# Agile — per-sprint, per-person capacity (Planning tab).
# =====================================================================
class SprintCapacity(Base):
    __tablename__ = "sprint_capacity"
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    sprint_id: Mapped[int] = mapped_column(
        Integer, ForeignKey(_FK_SPRINTS_ID, ondelete="CASCADE"), nullable=False
    )
    user_id: Mapped[int] = mapped_column(
        Integer, ForeignKey(_FK_USERS_ID, ondelete="CASCADE"), nullable=False
    )
    capacity_value: Mapped[float] = mapped_column(Numeric(10, 2), nullable=False, default=0)
    capacity_unit: Mapped[str] = mapped_column(String(20), nullable=False)  # points | minutes
    days_off: Mapped[list] = mapped_column(JSON, nullable=False, default=list)
    notes: Mapped[str] = mapped_column(Text, nullable=False, default="")
    version: Mapped[int] = mapped_column(Integer, nullable=False, default=1, server_default="1")
    created_at: Mapped[datetime] = mapped_column(UTCDateTime(), default=_utcnow, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(
        UTCDateTime(), default=_utcnow, onupdate=_utcnow, nullable=False
    )

    __table_args__ = (
        Index("idx_sprint_capacity_unique", "sprint_id", "user_id", unique=True),
    )


# =====================================================================
# Agile — Epics, Releases/Versions, Components, Labels, Collections.
# =====================================================================
# Epic-only extension fields; the Epic itself is a Bug row (item_type="Epic").
class EpicDetail(Base):
    __tablename__ = "epic_details"
    epic_id: Mapped[int] = mapped_column(
        Integer, ForeignKey(_FK_BUGS_ID, ondelete="CASCADE"), primary_key=True
    )
    color: Mapped[str] = mapped_column(String(20), nullable=False, default="")
    owner_id: Mapped[int | None] = mapped_column(
        Integer, ForeignKey(_FK_USERS_ID, ondelete=_ONDELETE_SET_NULL), nullable=True
    )
    sprint_id: Mapped[int | None] = mapped_column(
        Integer, ForeignKey(_FK_SPRINTS_ID, ondelete=_ONDELETE_SET_NULL), nullable=True
    )
    start_date: Mapped[str | None] = mapped_column(String(10), nullable=True)
    target_date: Mapped[str | None] = mapped_column(String(10), nullable=True)
    health: Mapped[str] = mapped_column(String(20), nullable=False, default="unknown")
    summary_note: Mapped[str] = mapped_column(Text, nullable=False, default="")
    archived: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    version: Mapped[int] = mapped_column(Integer, nullable=False, default=1, server_default="1")


# Structured acceptance criteria for a Story (Bug row, item_type="Story"); the
# legacy Bug.acceptance_criteria free-text column remains for backward compatibility.
class AcceptanceCriterion(Base):
    __tablename__ = "acceptance_criteria_items"
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    bug_id: Mapped[int] = mapped_column(Integer, ForeignKey(_FK_BUGS_ID, ondelete="CASCADE"), nullable=False)
    description: Mapped[str] = mapped_column(Text, nullable=False, default="")
    required: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    met: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    updated_by_id: Mapped[int | None] = mapped_column(
        Integer, ForeignKey(_FK_USERS_ID, ondelete=_ONDELETE_SET_NULL), nullable=True
    )
    updated_at: Mapped[datetime] = mapped_column(
        UTCDateTime(), default=_utcnow, onupdate=_utcnow, nullable=False
    )

    __table_args__ = (Index("idx_acceptance_criteria_items_bug_id", "bug_id"),)


class Version(Base):
    __tablename__ = "versions"
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    display_id: Mapped[str | None] = mapped_column(String(32), nullable=True, index=True)
    project_id: Mapped[int] = mapped_column(
        Integer, ForeignKey(_FK_PROJECTS_ID, ondelete="CASCADE"), nullable=False
    )
    name: Mapped[str] = mapped_column(String(120), nullable=False)
    name_normalized: Mapped[str] = mapped_column(String(120), nullable=False)
    description: Mapped[str] = mapped_column(Text, nullable=False, default="")
    status: Mapped[str] = mapped_column(String(20), nullable=False, default="unreleased")
    start_date: Mapped[str | None] = mapped_column(String(10), nullable=True)
    release_date: Mapped[str | None] = mapped_column(String(10), nullable=True)
    released_at: Mapped[datetime | None] = mapped_column(UTCDateTime(), nullable=True)
    owner_id: Mapped[int | None] = mapped_column(
        Integer, ForeignKey(_FK_USERS_ID, ondelete=_ONDELETE_SET_NULL), nullable=True
    )
    archived: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    version: Mapped[int] = mapped_column(Integer, nullable=False, default=1, server_default="1")
    created_at: Mapped[datetime] = mapped_column(UTCDateTime(), default=_utcnow, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(
        UTCDateTime(), default=_utcnow, onupdate=_utcnow, nullable=False
    )

    __table_args__ = (
        Index("idx_versions_unique_name", "project_id", "name_normalized", unique=True),
        Index("idx_versions_project_id", "project_id"),
    )


class WorkItemVersion(Base):
    __tablename__ = "work_item_versions"
    work_item_id: Mapped[int] = mapped_column(
        Integer, ForeignKey(_FK_BUGS_ID, ondelete="CASCADE"), primary_key=True
    )
    version_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("versions.id", ondelete="CASCADE"), primary_key=True
    )
    relation_type: Mapped[str] = mapped_column(String(10), nullable=False, default="fix", primary_key=True)

    __table_args__ = (Index("idx_work_item_versions_version", "version_id"),)


class Component(Base):
    __tablename__ = "components"
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    display_id: Mapped[str | None] = mapped_column(String(32), nullable=True, index=True)
    project_id: Mapped[int] = mapped_column(
        Integer, ForeignKey(_FK_PROJECTS_ID, ondelete="CASCADE"), nullable=False
    )
    name: Mapped[str] = mapped_column(String(120), nullable=False)
    name_normalized: Mapped[str] = mapped_column(String(120), nullable=False)
    description: Mapped[str] = mapped_column(Text, nullable=False, default="")
    lead_id: Mapped[int | None] = mapped_column(
        Integer, ForeignKey(_FK_USERS_ID, ondelete=_ONDELETE_SET_NULL), nullable=True
    )
    default_assignee_policy: Mapped[str] = mapped_column(String(20), nullable=False, default="none")
    default_assignee_id: Mapped[int | None] = mapped_column(
        Integer, ForeignKey(_FK_USERS_ID, ondelete=_ONDELETE_SET_NULL), nullable=True
    )
    archived: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    version: Mapped[int] = mapped_column(Integer, nullable=False, default=1, server_default="1")
    created_at: Mapped[datetime] = mapped_column(UTCDateTime(), default=_utcnow, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(
        UTCDateTime(), default=_utcnow, onupdate=_utcnow, nullable=False
    )

    __table_args__ = (
        Index("idx_components_unique_name", "project_id", "name_normalized", unique=True),
        Index("idx_components_project_id", "project_id"),
    )


class WorkItemComponent(Base):
    __tablename__ = "work_item_components"
    work_item_id: Mapped[int] = mapped_column(
        Integer, ForeignKey(_FK_BUGS_ID, ondelete="CASCADE"), primary_key=True
    )
    component_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("components.id", ondelete="CASCADE"), primary_key=True
    )

    __table_args__ = (Index("idx_work_item_components_component", "component_id"),)


class Label(Base):
    __tablename__ = "labels"
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    display_id: Mapped[str | None] = mapped_column(String(32), nullable=True, index=True)
    project_id: Mapped[int] = mapped_column(
        Integer, ForeignKey(_FK_PROJECTS_ID, ondelete="CASCADE"), nullable=False
    )
    normalized_name: Mapped[str] = mapped_column(String(120), nullable=False)
    display_name: Mapped[str] = mapped_column(String(120), nullable=False)
    color: Mapped[str] = mapped_column(String(20), nullable=False, default="")
    usage_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    version: Mapped[int] = mapped_column(Integer, nullable=False, default=1, server_default="1")
    created_at: Mapped[datetime] = mapped_column(UTCDateTime(), default=_utcnow, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(
        UTCDateTime(), default=_utcnow, onupdate=_utcnow, nullable=False
    )

    __table_args__ = (
        Index("idx_labels_unique_name", "project_id", "normalized_name", unique=True),
        Index("idx_labels_project_id", "project_id"),
    )


class WorkItemLabel(Base):
    __tablename__ = "work_item_labels"
    work_item_id: Mapped[int] = mapped_column(
        Integer, ForeignKey(_FK_BUGS_ID, ondelete="CASCADE"), primary_key=True
    )
    label_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("labels.id", ondelete="CASCADE"), primary_key=True
    )

    __table_args__ = (Index("idx_work_item_labels_label", "label_id"),)


# =====================================================================
# GitHub Enterprise integration: per-project configuration, the repository
# allow-list and the per-work-item branch records.
#
# No secret ever lives in these tables. Credentials come from the environment
# layer (app/config.py + app/git/github.py). API responses expose only the safe
# metadata below.
# =====================================================================

class ProjectGitConfig(Base):
    """One Git provider configuration per project (unique on project_id)."""

    __tablename__ = "project_git_configs"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    project_id: Mapped[int] = mapped_column(
        Integer, ForeignKey(_FK_PROJECTS_ID, ondelete="CASCADE"), nullable=False
    )
    provider: Mapped[str] = mapped_column(
        String(24), nullable=False, default="github_enterprise"
    )
    enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    base_url: Mapped[str] = mapped_column(String(500), nullable=False, default="")
    organization: Mapped[str] = mapped_column(String(120), nullable=False, default="")
    # Project-scoped PAT, Fernet-encrypted with the server-only
    # GIT_CREDENTIAL_ENCRYPTION_KEY (app/git/credentials.py). Empty string means
    # "no project credential" and the legacy global GITHUB_TOKEN is used as a
    # fallback. A plaintext token is never stored here, never logged, never
    # audited and never returned by any API response.
    credential_encrypted: Mapped[str] = mapped_column(
        Text, nullable=False, default="", server_default=""
    )
    default_base_branch: Mapped[str] = mapped_column(String(120), nullable=False, default="dev")
    # unknown | ok | error
    connection_status: Mapped[str] = mapped_column(String(20), nullable=False, default="unknown")
    last_checked_at: Mapped[datetime | None] = mapped_column(
        UTCDateTime(), nullable=True
    )
    # Sanitized provider error (never a token/header/body); "" while healthy.
    last_error: Mapped[str] = mapped_column(String(500), nullable=False, default="")
    version: Mapped[int] = mapped_column(Integer, nullable=False, default=1, server_default="1")
    created_at: Mapped[datetime] = mapped_column(
        UTCDateTime(), default=_utcnow, nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        UTCDateTime(), default=_utcnow, onupdate=_utcnow, nullable=False
    )

    project: Mapped[Project] = relationship("Project", back_populates="git_config")

    __table_args__ = (
        Index("idx_project_git_configs_unique_project", "project_id", unique=True),
    )

class ProjectRepository(Base):
    """Internal branch-history identity for one provider repository.

    This row is NOT an allow-list and is never user-selected: repositories are
    discovered live from the provider and re-verified at branch-creation time.
    The stable identity key is ``(project_id, provider, provider_repo_id)``.
    Branch records reference this row, so it is never deleted — branch history
    must survive, and a renamed repository keeps its row id.
    """

    __tablename__ = "project_repositories"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    project_id: Mapped[int] = mapped_column(
        Integer, ForeignKey(_FK_PROJECTS_ID, ondelete="CASCADE"), nullable=False
    )
    provider: Mapped[str] = mapped_column(
        String(24), nullable=False, default="github_enterprise"
    )
    # Provider-side identifier kept as text so the column stays provider-agnostic.
    provider_repo_id: Mapped[str] = mapped_column(String(64), nullable=False, default="")
    name: Mapped[str] = mapped_column(String(200), nullable=False)
    owner: Mapped[str] = mapped_column(String(120), nullable=False, default="")
    url: Mapped[str] = mapped_column(String(500), nullable=False, default="")
    default_base_branch: Mapped[str] = mapped_column(String(120), nullable=False, default="dev")
    active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    branch_creation_enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    last_checked_at: Mapped[datetime | None] = mapped_column(
        UTCDateTime(), nullable=True
    )
    last_error: Mapped[str] = mapped_column(String(500), nullable=False, default="")
    created_at: Mapped[datetime] = mapped_column(
        UTCDateTime(), default=_utcnow, nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        UTCDateTime(), default=_utcnow, onupdate=_utcnow, nullable=False
    )

    project: Mapped[Project] = relationship("Project", back_populates="repositories")
    branches: Mapped[list["WorkItemBranch"]] = relationship(
        "WorkItemBranch", back_populates="repository"
    )

    __table_args__ = (
        # Stable identity: one provider repository per project per provider.
        Index(
            "idx_project_repositories_identity",
            "project_id", "provider", "provider_repo_id",
            unique=True,
        ),
        # Name uniqueness is kept only as a legacy-constraint backstop; identity
        # is provider-id based, so two same-named repositories from different
        # owners are distinct rows.
        Index(
            "idx_project_repositories_unique_name",
            "project_id", "name", "provider_repo_id",
            unique=True,
        ),
        Index("idx_project_repositories_project_id", "project_id"),
        Index("idx_project_repositories_provider_id", "provider_repo_id"),
    )

class WorkItemBranch(Base):
    """One remote branch created for one work item inside one repository.

    `work_item_type` + `work_item_id` are a polymorphic reference (they point at
    either a `bugs` row or a `features` row) and therefore carry no FK, exactly
    like Activity.entity_type/entity_id. Keeping the row after the work item,
    repository configuration or user changes is deliberate: this is branch
    history.
    """

    __tablename__ = "work_item_branches"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    # Bug | Requirement | Task | Sub-task | Story | Feature (never Epic/Sprint).
    work_item_type: Mapped[str] = mapped_column(String(20), nullable=False)
    work_item_id: Mapped[int] = mapped_column(Integer, nullable=False)
    project_id: Mapped[int] = mapped_column(
        Integer, ForeignKey(_FK_PROJECTS_ID, ondelete="CASCADE"), nullable=False
    )
    # SET NULL, not RESTRICT/CASCADE: branch history must survive a repository
    # identity row being removed. The API never deletes these rows, so the
    # unique index below stays effective in normal operation.
    repository_id: Mapped[int | None] = mapped_column(
        Integer,
        ForeignKey("project_repositories.id", ondelete=_ONDELETE_SET_NULL),
        nullable=True,
    )
    provider_repo_id: Mapped[str] = mapped_column(String(64), nullable=False, default="")
    branch_name: Mapped[str] = mapped_column(String(255), nullable=False)
    base_branch: Mapped[str] = mapped_column(String(120), nullable=False, default="")
    base_commit_sha: Mapped[str] = mapped_column(String(64), nullable=False, default="")
    branch_url: Mapped[str] = mapped_column(String(500), nullable=False, default="")
    provider_branch_ref: Mapped[str] = mapped_column(String(255), nullable=False, default="")
    # Active | Deleted | Unknown
    status: Mapped[str] = mapped_column(String(20), nullable=False, default="Active")
    created_by_id: Mapped[int | None] = mapped_column(
        Integer, ForeignKey(_FK_USERS_ID, ondelete=_ONDELETE_SET_NULL), nullable=True
    )
    created_by_name: Mapped[str] = mapped_column(String(120), nullable=False, default="")
    created_at: Mapped[datetime] = mapped_column(
        UTCDateTime(), default=_utcnow, nullable=False
    )
    last_checked_at: Mapped[datetime | None] = mapped_column(
        UTCDateTime(), nullable=True
    )
    last_error: Mapped[str] = mapped_column(String(500), nullable=False, default="")
    # Removal metadata. The row is retained as Deleted history: creation
    # metadata is preserved and removal is recorded with safe, human-facing
    # fields only (never a token, never an internal identity).
    removed_at: Mapped[datetime | None] = mapped_column(
        UTCDateTime(), nullable=True
    )
    removed_by_id: Mapped[int | None] = mapped_column(
        Integer, ForeignKey(_FK_USERS_ID, ondelete=_ONDELETE_SET_NULL), nullable=True
    )
    removed_by_name: Mapped[str] = mapped_column(String(120), nullable=False, default="")
    removal_reason: Mapped[str] = mapped_column(String(120), nullable=False, default="")
    version: Mapped[int] = mapped_column(Integer, nullable=False, default=1, server_default="1")

    repository: Mapped[ProjectRepository] = relationship(
        "ProjectRepository", back_populates="branches"
    )

    __table_args__ = (
        # One ACTIVE feature branch per work item per provider repository.
        # Deleted rows never block a recreated branch, and the partial predicate
        # keeps legacy blank-provider-id rows out of the key.
        Index(
            "idx_work_item_branches_active_unique",
            "project_id", "work_item_type", "work_item_id", "provider_repo_id",
            unique=True,
            sqlite_where=text("status = 'Active' AND provider_repo_id <> ''"),
            postgresql_where=text("status = 'Active' AND provider_repo_id <> ''"),
        ),
        Index("idx_work_item_branches_work_item", "work_item_type", "work_item_id"),
        Index("idx_work_item_branches_repository_id", "repository_id"),
        Index("idx_work_item_branches_project_id", "project_id"),
    )

