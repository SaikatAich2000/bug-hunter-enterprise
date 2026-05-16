"""ORM models for Bug Hunter — v4.0 multi-tenant.

The big v4 change: every piece of data now belongs to an Organization
(tenant). A user signs up, becomes the admin of their newly-created org,
and from there invites teammates by email. Teammates that accept land in
the same org and only that org. Cross-org data is strictly invisible.

Tables:
  - organizations    : top-level tenants. One per signup.
  - users            : human members of an org. Email is globally unique.
  - projects         : workspaces inside an org. Names unique per-org.
  - project_memberships : which users belong to which projects, and with
                       what role (lead | member). Admins of the org have
                       implicit access to every project without an entry.
  - bugs             : core entity, scoped via project → org.
  - bug_assignees    : many-to-many between bugs and users.
  - comments         : threaded discussion on a bug.
  - attachments      : file blobs attached to a bug or comment, stored
                       inside the database so backups are atomic.
  - activity_log     : audit trail, scoped to the org of the actor.
  - password_reset_tokens : single-use email-based password reset.
  - sessions         : server-side record of every active login. Lets
                       admins see who's currently signed in (Keycloak-
                       style) and revoke individual sessions.
  - invitations      : pending org invites. Token-based, time-limited.
"""
from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy import (
    Boolean,
    Column,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    LargeBinary,
    String,
    Table,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.database import Base


def _utcnow() -> datetime:
    return datetime.now(timezone.utc).replace(microsecond=0)


# ---------------------------------------------------------------------------
# Roles
#
# Two-tier role system, Jira-style:
#
#   Org-level (User.role):
#     admin   — full control of the org. Implicit access to every project.
#               Manage users, projects, sessions, audit, billing-ish stuff.
#     manager — can create projects (becomes lead of those they create).
#               Sees only projects they're a member of. Can invite users.
#     member  — only sees projects they're a member of. Edits bugs in those.
#
#   Project-level (ProjectMembership.role):
#     lead    — manages this project's membership. Edits/deletes the project.
#     member  — works on bugs in the project.
#
# Org admins always behave as project leads for every project in the org,
# without needing a ProjectMembership row.
# ---------------------------------------------------------------------------
ROLE_ADMIN = "admin"
ROLE_MANAGER = "manager"
ROLE_MEMBER = "member"
VALID_ROLES = (ROLE_ADMIN, ROLE_MANAGER, ROLE_MEMBER)

PROJECT_ROLE_LEAD = "lead"
PROJECT_ROLE_MEMBER = "member"
VALID_PROJECT_ROLES = (PROJECT_ROLE_LEAD, PROJECT_ROLE_MEMBER)


# ---------------------------------------------------------------------------
# Junction tables
# ---------------------------------------------------------------------------
bug_assignees = Table(
    "bug_assignees",
    Base.metadata,
    Column("bug_id", Integer, ForeignKey("bugs.id", ondelete="CASCADE"), primary_key=True),
    Column("user_id", Integer, ForeignKey("users.id", ondelete="CASCADE"), primary_key=True),
)


# ---------------------------------------------------------------------------
# Organization
# ---------------------------------------------------------------------------
class Organization(Base):
    """A tenant. Each user, project, bug, etc. is scoped to exactly one.

    `slug` is a URL-safe identifier we may surface in shareable links or
    deep-link URLs (e.g. /o/acme/bugs/123). It's auto-generated from the
    name on signup and unique system-wide.
    """
    __tablename__ = "organizations"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    name: Mapped[str] = mapped_column(String(120), nullable=False)
    slug: Mapped[str] = mapped_column(String(80), nullable=False, unique=True)
    description: Mapped[str] = mapped_column(Text, nullable=False, default="")

    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow, onupdate=_utcnow, nullable=False
    )

    users: Mapped[list["User"]] = relationship("User", back_populates="organization", cascade="all, delete-orphan")
    projects: Mapped[list["Project"]] = relationship("Project", back_populates="organization", cascade="all, delete-orphan")

    __table_args__ = (
        Index("idx_orgs_slug", "slug"),
    )


# ---------------------------------------------------------------------------
# User
# ---------------------------------------------------------------------------
class User(Base):
    """A person with a login. Email is globally unique so the login flow
    can identify the user from email alone. If you want to belong to two
    orgs you need two emails — same constraint Notion / Slack imposed for
    years and what keeps the auth layer simple.
    """
    __tablename__ = "users"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    org_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("organizations.id", ondelete="CASCADE"), nullable=False
    )
    name: Mapped[str] = mapped_column(String(120), nullable=False)
    email: Mapped[str] = mapped_column(String(254), nullable=False, unique=True)
    role: Mapped[str] = mapped_column(String(20), nullable=False, default=ROLE_MEMBER)
    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)

    # bcrypt hash of password.
    password_hash: Mapped[str | None] = mapped_column(String(120), nullable=True)

    # Bumped on password change / reset / forced logout. Sessions baked
    # with an old session_version no longer validate. This is what makes
    # "I changed my password" actually log out other devices.
    session_version: Mapped[int] = mapped_column(Integer, nullable=False, default=0)

    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow, onupdate=_utcnow, nullable=False
    )

    organization: Mapped[Organization] = relationship("Organization", back_populates="users")

    __table_args__ = (
        Index("idx_users_email", "email"),
        Index("idx_users_org_id", "org_id"),
    )


# ---------------------------------------------------------------------------
# PasswordResetToken
# ---------------------------------------------------------------------------
class PasswordResetToken(Base):
    """Single-use tokens emailed to users to reset a forgotten password.
    Stored as a sha256 hash; never the plaintext."""
    __tablename__ = "password_reset_tokens"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    user_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("users.id", ondelete="CASCADE"), nullable=False
    )
    token_hash: Mapped[str] = mapped_column(String(64), nullable=False, unique=True)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    used_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow, nullable=False)

    __table_args__ = (Index("idx_prt_token_hash", "token_hash"),)


# ---------------------------------------------------------------------------
# Invitation
#
# Token-emailed invite from an org admin (or project lead, in which case
# the new user lands as a regular member). Acceptance creates the User
# row — we deliberately don't pre-create dormant users.
# ---------------------------------------------------------------------------
class Invitation(Base):
    __tablename__ = "invitations"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    org_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("organizations.id", ondelete="CASCADE"), nullable=False
    )
    email: Mapped[str] = mapped_column(String(254), nullable=False)
    role: Mapped[str] = mapped_column(String(20), nullable=False, default=ROLE_MEMBER)

    # sha256 of the URL-safe token we email out. Plaintext token never
    # touches the database — same pattern as password reset.
    token_hash: Mapped[str] = mapped_column(String(64), nullable=False, unique=True)

    invited_by_user_id: Mapped[int | None] = mapped_column(
        Integer, ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )
    invited_by_name: Mapped[str] = mapped_column(String(120), nullable=False, default="")

    # Comma-separated list of project IDs to auto-add the user to on
    # acceptance. Simpler than a separate junction table — invitations
    # are short-lived and we never query "what projects does this invite
    # cover" except at acceptance time.
    initial_project_ids: Mapped[str] = mapped_column(String(500), nullable=False, default="")

    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    accepted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow, nullable=False)

    __table_args__ = (
        Index("idx_invites_token_hash", "token_hash"),
        Index("idx_invites_org_id", "org_id"),
        Index("idx_invites_email", "email"),
    )


# ---------------------------------------------------------------------------
# Project
# ---------------------------------------------------------------------------
class Project(Base):
    __tablename__ = "projects"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    org_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("organizations.id", ondelete="CASCADE"), nullable=False
    )
    name: Mapped[str] = mapped_column(String(120), nullable=False)
    # Jira-style short identifier (e.g. "WEB", "API"). Auto-derived from
    # name on create but editable. Displayed alongside bug IDs (WEB-42).
    key: Mapped[str] = mapped_column(String(16), nullable=False, default="")
    description: Mapped[str] = mapped_column(Text, nullable=False, default="")
    color: Mapped[str] = mapped_column(String(20), nullable=False, default="#c9764f")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow, onupdate=_utcnow, nullable=False
    )

    organization: Mapped[Organization] = relationship("Organization", back_populates="projects")
    bugs: Mapped[list["Bug"]] = relationship(
        "Bug", back_populates="project", cascade="all, delete-orphan"
    )
    memberships: Mapped[list["ProjectMembership"]] = relationship(
        "ProjectMembership", back_populates="project", cascade="all, delete-orphan"
    )

    __table_args__ = (
        # Name unique within the org, not globally — two different orgs can
        # both have a project called "Website".
        UniqueConstraint("org_id", "name", name="uq_projects_org_name"),
        UniqueConstraint("org_id", "key", name="uq_projects_org_key"),
        Index("idx_projects_org_id", "org_id"),
    )


# ---------------------------------------------------------------------------
# ProjectMembership
#
# (user, project) pair. Org admins have implicit access without a row;
# everyone else MUST have a membership row to see / edit a project's
# bugs. The role field distinguishes between project leads (can manage
# this project's members) and regular contributors.
# ---------------------------------------------------------------------------
class ProjectMembership(Base):
    __tablename__ = "project_memberships"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    project_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("projects.id", ondelete="CASCADE"), nullable=False
    )
    user_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("users.id", ondelete="CASCADE"), nullable=False
    )
    role: Mapped[str] = mapped_column(String(20), nullable=False, default=PROJECT_ROLE_MEMBER)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow, nullable=False)

    project: Mapped[Project] = relationship("Project", back_populates="memberships")
    user: Mapped[User] = relationship("User")

    __table_args__ = (
        UniqueConstraint("project_id", "user_id", name="uq_pm_project_user"),
        Index("idx_pm_user_id", "user_id"),
        Index("idx_pm_project_id", "project_id"),
    )


# ---------------------------------------------------------------------------
# Bug
# ---------------------------------------------------------------------------
class Bug(Base):
    __tablename__ = "bugs"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    project_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("projects.id", ondelete="CASCADE"), nullable=False
    )
    reporter_id: Mapped[int | None] = mapped_column(
        Integer, ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )
    title: Mapped[str] = mapped_column(String(200), nullable=False)
    description: Mapped[str] = mapped_column(Text, nullable=False, default="")
    status: Mapped[str] = mapped_column(String(20), nullable=False, default="New")
    priority: Mapped[str] = mapped_column(String(20), nullable=False, default="Medium")
    environment: Mapped[str] = mapped_column(String(10), nullable=False, default="DEV")
    due_date: Mapped[str | None] = mapped_column(String(10), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow, onupdate=_utcnow, nullable=False
    )

    project: Mapped[Project] = relationship("Project", back_populates="bugs")
    reporter: Mapped["User | None"] = relationship("User", foreign_keys=[reporter_id])
    assignees: Mapped[list["User"]] = relationship(
        "User", secondary=bug_assignees, lazy="selectin"
    )
    comments: Mapped[list["Comment"]] = relationship(
        "Comment", back_populates="bug", cascade="all, delete-orphan",
        order_by="Comment.created_at",
    )
    activities: Mapped[list["Activity"]] = relationship(
        "Activity", back_populates="bug",
        order_by="(Activity.created_at.desc(), Activity.id.desc())",
    )
    attachments: Mapped[list["Attachment"]] = relationship(
        "Attachment", back_populates="bug", cascade="all, delete-orphan",
        order_by="Attachment.created_at.desc()",
        primaryjoin="Bug.id == Attachment.bug_id",
    )

    __table_args__ = (
        Index("idx_bugs_project_id", "project_id"),
        Index("idx_bugs_reporter_id", "reporter_id"),
        Index("idx_bugs_status", "status"),
        Index("idx_bugs_priority", "priority"),
        Index("idx_bugs_environment", "environment"),
        Index("idx_bugs_project_status", "project_id", "status"),
        Index("idx_bugs_status_priority", "status", "priority"),
        Index("idx_bugs_updated_at", "updated_at"),
    )


# ---------------------------------------------------------------------------
# Comment
# ---------------------------------------------------------------------------
class Comment(Base):
    __tablename__ = "comments"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    bug_id: Mapped[int] = mapped_column(Integer, ForeignKey("bugs.id", ondelete="CASCADE"), nullable=False)
    author_user_id: Mapped[int | None] = mapped_column(
        Integer, ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )
    author_name: Mapped[str] = mapped_column(String(120), nullable=False)
    body: Mapped[str] = mapped_column(Text, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow, nullable=False)

    bug: Mapped[Bug] = relationship("Bug", back_populates="comments")

    __table_args__ = (Index("idx_comments_bug_id", "bug_id"),)


# ---------------------------------------------------------------------------
# Attachment
#
# Same approach as v3.x — files stored INSIDE the database as BLOBs so
# backups are atomic and there's no S3 dependency. 50 MB hard cap per
# upload. Can belong to a bug directly or to a comment.
# ---------------------------------------------------------------------------
class Attachment(Base):
    __tablename__ = "attachments"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    bug_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("bugs.id", ondelete="CASCADE"), nullable=False
    )
    comment_id: Mapped[int | None] = mapped_column(
        Integer, ForeignKey("comments.id", ondelete="CASCADE"), nullable=True
    )
    uploader_user_id: Mapped[int | None] = mapped_column(
        Integer, ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )
    uploader_name: Mapped[str] = mapped_column(String(120), nullable=False, default="anonymous")
    filename: Mapped[str] = mapped_column(String(255), nullable=False)
    content_type: Mapped[str] = mapped_column(String(120), nullable=False, default="application/octet-stream")
    size_bytes: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    data: Mapped[bytes] = mapped_column(LargeBinary, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow, nullable=False)

    bug: Mapped[Bug] = relationship("Bug", back_populates="attachments", foreign_keys=[bug_id])

    __table_args__ = (
        Index("idx_attachments_bug_id", "bug_id"),
        Index("idx_attachments_comment_id", "comment_id"),
        Index("idx_attachments_bug_comment", "bug_id", "comment_id"),
    )


# ---------------------------------------------------------------------------
# Activity (audit trail)
#
# org_id is denormalised onto each row so the audit view query can filter
# by tenant without joining through to bug→project→org for every event
# (and so non-bug events like "user_invited" can still be tenant-scoped).
# ---------------------------------------------------------------------------
class Activity(Base):
    __tablename__ = "activity_log"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    org_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("organizations.id", ondelete="CASCADE"), nullable=False
    )
    bug_id: Mapped[int | None] = mapped_column(
        Integer, ForeignKey("bugs.id", ondelete="CASCADE"), nullable=True
    )
    entity_type: Mapped[str] = mapped_column(String(40), nullable=False, default="bug")
    entity_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    actor_user_id: Mapped[int | None] = mapped_column(
        Integer, ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )
    actor_name: Mapped[str] = mapped_column(String(120), nullable=False, default="system")
    action: Mapped[str] = mapped_column(String(60), nullable=False)
    detail: Mapped[str] = mapped_column(Text, nullable=False, default="")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow, nullable=False)

    bug: Mapped[Bug | None] = relationship("Bug", back_populates="activities")

    __table_args__ = (
        Index("idx_activity_org_id", "org_id"),
        Index("idx_activity_bug_id", "bug_id"),
        Index("idx_activity_entity", "entity_type", "entity_id"),
        Index("idx_activity_created", "created_at"),
    )


# ---------------------------------------------------------------------------
# Session
# ---------------------------------------------------------------------------
class Session(Base):
    """Server-side record of every active login. Keyed by `jti` which is
    also baked into the signed session cookie. Lets admins list / revoke
    individual sessions Keycloak-style.

    Sessions are implicitly scoped to a user (and therefore to the user's
    org); the admin sessions panel filters by users in the admin's own
    organization to enforce tenant isolation.
    """
    __tablename__ = "sessions"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    user_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("users.id", ondelete="CASCADE"), nullable=False
    )
    jti: Mapped[str] = mapped_column(String(64), nullable=False, unique=True)
    user_agent: Mapped[str] = mapped_column(String(400), nullable=False, default="")
    ip_address: Mapped[str] = mapped_column(String(64), nullable=False, default="")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow, nullable=False)
    last_seen_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow, onupdate=_utcnow, nullable=False
    )
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)

    __table_args__ = (
        Index("idx_sessions_jti", "jti"),
        Index("idx_sessions_user_id", "user_id"),
        Index("idx_sessions_expires_at", "expires_at"),
    )
