"""Pydantic schemas (request/response DTOs) — v4.0 multi-tenant."""
from __future__ import annotations

import re
from datetime import datetime
from typing import Any, Optional

from pydantic import BaseModel, ConfigDict, Field, field_validator


# ---------------------------------------------------------------------------
# Allowed values
# ---------------------------------------------------------------------------
ALLOWED_STATUSES = [
    "New", "In Progress", "Resolved", "Closed", "Reopened",
    "Not a Bug", "Resolve Later",
]
EXCLUDED_FROM_TOTAL_STATUSES = ["Not a Bug"]
ALLOWED_PRIORITIES = ["Low", "Medium", "High", "Critical"]
ALLOWED_ENVIRONMENTS = ["DEV", "UAT", "PROD"]
ALLOWED_ROLES = ["admin", "manager", "member"]
ALLOWED_PROJECT_ROLES = ["lead", "member"]

MIN_PASSWORD_LENGTH = 8
MIN_TITLE_LENGTH = 3
MIN_NAME_LENGTH = 2
MIN_PROJECT_NAME_LENGTH = 2
MIN_ORG_NAME_LENGTH = 2


# ---------------------------------------------------------------------------
# Validation helpers
# ---------------------------------------------------------------------------
def normalize_choice(value: str, allowed: list[str], label: str) -> str:
    """Case-insensitive match against `allowed`; returns canonical form."""
    if not isinstance(value, str):
        raise ValueError(f"Invalid {label}. Allowed: {', '.join(allowed)}")
    needle = value.strip().lower()
    for canonical in allowed:
        if canonical.lower() == needle:
            return canonical
    raise ValueError(f"Invalid {label}. Allowed: {', '.join(allowed)}")


_normalize_choice = normalize_choice

_EMAIL_RE = re.compile(r"^[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}$")


def _validate_email(value: str) -> str:
    v = (value or "").strip().lower()
    if not _EMAIL_RE.match(v):
        raise ValueError("Invalid email address")
    if len(v) > 254:
        raise ValueError("Email is too long")
    return v


def _strip_and_check_min_length(v: str, min_len: int, label: str) -> str:
    if not isinstance(v, str):
        raise ValueError(f"{label} must be a string")
    v = v.strip()
    if len(v) < min_len:
        if min_len == 1:
            raise ValueError(f"{label} cannot be empty")
        raise ValueError(f"{label} must be at least {min_len} characters")
    return v


def _normalize_role(v: str) -> str:
    if not isinstance(v, str):
        raise ValueError("role must be a string")
    needle = v.strip().lower()
    if needle in ALLOWED_ROLES:
        return needle
    raise ValueError(f"Invalid role. Allowed: {', '.join(ALLOWED_ROLES)}")


def _normalize_project_role(v: str) -> str:
    if not isinstance(v, str):
        raise ValueError("project role must be a string")
    needle = v.strip().lower()
    if needle in ALLOWED_PROJECT_ROLES:
        return needle
    raise ValueError(f"Invalid project role. Allowed: {', '.join(ALLOWED_PROJECT_ROLES)}")


def _check_password_strength(v: str) -> str:
    if not isinstance(v, str):
        raise ValueError("Password must be a string")
    if len(v) < MIN_PASSWORD_LENGTH:
        raise ValueError(f"Password must be at least {MIN_PASSWORD_LENGTH} characters")
    if len(v) > 200:
        raise ValueError("Password is too long")
    has_letter = any(c.isalpha() for c in v)
    has_digit = any(c.isdigit() for c in v)
    if not (has_letter and has_digit):
        raise ValueError("Password must contain at least one letter and one number")
    # Block a small list of obviously-terrible passwords (case-insensitive).
    if v.lower() in {
        "password", "password1", "password123", "admin123",
        "qwerty123", "12345678a", "letmein123", "passw0rd",
        "changeme", "changeme1",
    }:
        raise ValueError("Password is too common — please choose a stronger one")
    return v


# ---------------------------------------------------------------------------
# Organization
# ---------------------------------------------------------------------------
class OrganizationOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: int
    name: str
    slug: str
    description: str
    created_at: datetime


class OrganizationUpdate(BaseModel):
    """Admins-only patch of org details."""
    name: Optional[str] = Field(default=None, max_length=120)
    description: Optional[str] = Field(default=None, max_length=1000)

    @field_validator("name")
    @classmethod
    def _name(cls, v: Optional[str]) -> Optional[str]:
        if v is None:
            return None
        return _strip_and_check_min_length(v, MIN_ORG_NAME_LENGTH, "Organization name")

    @field_validator("description")
    @classmethod
    def _desc(cls, v: Optional[str]) -> Optional[str]:
        return v.strip() if isinstance(v, str) else v


# ---------------------------------------------------------------------------
# Sign-up — creates a brand-new organization with the signup user as admin
# ---------------------------------------------------------------------------
class SignupIn(BaseModel):
    """First-time sign-up: creates an org + the admin user in one shot."""
    name: str = Field(max_length=120)
    email: str = Field(max_length=254)
    password: str
    organization_name: str = Field(max_length=120)

    @field_validator("name")
    @classmethod
    def _name(cls, v: str) -> str:
        return _strip_and_check_min_length(v, MIN_NAME_LENGTH, "Name")

    @field_validator("email")
    @classmethod
    def _email(cls, v: str) -> str:
        return _validate_email(v)

    @field_validator("password")
    @classmethod
    def _pw(cls, v: str) -> str:
        return _check_password_strength(v)

    @field_validator("organization_name")
    @classmethod
    def _org(cls, v: str) -> str:
        return _strip_and_check_min_length(v, MIN_ORG_NAME_LENGTH, "Organization name")


# ---------------------------------------------------------------------------
# Invitation
# ---------------------------------------------------------------------------
class InvitationCreate(BaseModel):
    """Admin / manager sends an invite to bring someone into their org."""
    email: str = Field(max_length=254)
    role: str = Field(default="member")
    # Project IDs the new user should be added to on acceptance. Empty
    # is fine — admin can still add them later. Cross-org IDs are
    # rejected at the route layer.
    project_ids: list[int] = Field(default_factory=list)
    # If True, give them lead role on each of those projects instead of
    # plain member. Useful when inviting someone who'll run a project.
    as_lead: bool = False

    @field_validator("email")
    @classmethod
    def _email(cls, v: str) -> str:
        return _validate_email(v)

    @field_validator("role")
    @classmethod
    def _role(cls, v: str) -> str:
        return _normalize_role(v)

    @field_validator("project_ids")
    @classmethod
    def _dedup(cls, v: list[int]) -> list[int]:
        seen: list[int] = []
        for x in v or []:
            if x not in seen:
                seen.append(x)
        return seen


class InvitationOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: int
    org_id: int
    email: str
    role: str
    invited_by_user_id: Optional[int] = None
    invited_by_name: str
    initial_project_ids: str
    expires_at: datetime
    accepted_at: Optional[datetime] = None
    revoked_at: Optional[datetime] = None
    created_at: datetime


class InvitationPreview(BaseModel):
    """Public, unauthenticated view of an invite token — what the invitee
    sees before they accept. Deliberately reveals as little as possible:
    just the org name and the role they'd be joining as. NEVER includes
    the inviter's email."""
    email: str
    organization_name: str
    role: str
    expires_at: datetime
    invited_by_name: str


class InvitationAccept(BaseModel):
    """Invitee fills this in to complete acceptance."""
    token: str
    name: str = Field(max_length=120)
    password: str

    @field_validator("name")
    @classmethod
    def _name(cls, v: str) -> str:
        return _strip_and_check_min_length(v, MIN_NAME_LENGTH, "Name")

    @field_validator("password")
    @classmethod
    def _pw(cls, v: str) -> str:
        return _check_password_strength(v)


# ---------------------------------------------------------------------------
# User
# ---------------------------------------------------------------------------
class UserIn(BaseModel):
    """Admin creates a user directly (alternative to inviting)."""
    name: str = Field(max_length=120)
    email: str = Field(max_length=254)
    role: str = Field(default="member")
    password: str
    is_active: bool = True

    @field_validator("name")
    @classmethod
    def _name(cls, v: str) -> str:
        return _strip_and_check_min_length(v, MIN_NAME_LENGTH, "Name")

    @field_validator("role")
    @classmethod
    def _role(cls, v: str) -> str:
        return _normalize_role(v)

    @field_validator("email")
    @classmethod
    def _email(cls, v: str) -> str:
        return _validate_email(v)

    @field_validator("password")
    @classmethod
    def _pw(cls, v: str) -> str:
        return _check_password_strength(v)


class UserUpdate(BaseModel):
    name: Optional[str] = Field(default=None, max_length=120)
    email: Optional[str] = Field(default=None, max_length=254)
    role: Optional[str] = None
    is_active: Optional[bool] = None
    password: Optional[str] = None

    @field_validator("name")
    @classmethod
    def _name(cls, v: Optional[str]) -> Optional[str]:
        if v is None:
            return None
        return _strip_and_check_min_length(v, MIN_NAME_LENGTH, "Name")

    @field_validator("role")
    @classmethod
    def _role(cls, v: Optional[str]) -> Optional[str]:
        if v is None:
            return None
        return _normalize_role(v)

    @field_validator("email")
    @classmethod
    def _email(cls, v: Optional[str]) -> Optional[str]:
        if v is None:
            return None
        return _validate_email(v)

    @field_validator("password")
    @classmethod
    def _pw(cls, v: Optional[str]) -> Optional[str]:
        if v is None:
            return None
        return _check_password_strength(v)


class UserOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: int
    name: str
    email: str
    role: str
    is_active: bool
    created_at: datetime
    updated_at: datetime


# ---------------------------------------------------------------------------
# Auth
# ---------------------------------------------------------------------------
class LoginIn(BaseModel):
    email: str
    password: str

    @field_validator("email")
    @classmethod
    def _email(cls, v: str) -> str:
        return _validate_email(v)


class ChangePasswordIn(BaseModel):
    current_password: str = Field(min_length=1, max_length=200)
    new_password: str

    @field_validator("new_password")
    @classmethod
    def _pw(cls, v: str) -> str:
        return _check_password_strength(v)


class ForgotPasswordIn(BaseModel):
    email: str

    @field_validator("email")
    @classmethod
    def _email(cls, v: str) -> str:
        return _validate_email(v)


class ResetPasswordIn(BaseModel):
    token: str
    new_password: str

    @field_validator("new_password")
    @classmethod
    def _pw(cls, v: str) -> str:
        return _check_password_strength(v)


# ----- Profile (self-service) -----

class ProfileUpdateIn(BaseModel):
    """Self-edit your own name. Email and role aren't editable here —
    email goes through the two-step EmailChange flow (so a hijacked
    session can't quietly swap recovery contact); role is set by an
    admin via the user-admin endpoints."""
    name: str = Field(min_length=2, max_length=120)

    @field_validator("name")
    @classmethod
    def _strip(cls, v: str) -> str:
        return v.strip()


class EmailChangeRequestIn(BaseModel):
    """Step 1 of the email change: prove ownership of the account (with
    current password) and nominate a new address. We'll email a code to
    the new address; it must be entered via EmailChangeConfirmIn to finish."""
    new_email: str
    current_password: str = Field(min_length=1, max_length=200)

    @field_validator("new_email")
    @classmethod
    def _email(cls, v: str) -> str:
        return _validate_email(v)


class EmailChangeConfirmIn(BaseModel):
    """Step 2: enter the 6-digit code that was emailed to the new address."""
    code: str = Field(min_length=6, max_length=6)

    @field_validator("code")
    @classmethod
    def _digits(cls, v: str) -> str:
        v = v.strip()
        if not v.isdigit() or len(v) != 6:
            raise ValueError("Code must be exactly 6 digits.")
        return v


class MeOut(BaseModel):
    """Returned to the frontend after login or on refresh. Now includes
    org info so the SPA knows which tenant the user is in without
    having to call a second endpoint."""
    model_config = ConfigDict(from_attributes=True)
    id: int
    name: str
    email: str
    role: str
    is_active: bool
    org_id: int
    organization_name: str
    organization_slug: str


class UserBrief(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: int
    name: str
    email: str
    role: str


# ---------------------------------------------------------------------------
# Project
# ---------------------------------------------------------------------------
class ProjectIn(BaseModel):
    name: str = Field(max_length=120)
    key: Optional[str] = Field(default=None, max_length=16)
    description: str = Field(default="", max_length=1000)
    color: str = Field(default="#c9764f", pattern=r"^#[0-9a-fA-F]{6}$")

    @field_validator("name")
    @classmethod
    def _name(cls, v: str) -> str:
        return _strip_and_check_min_length(v, MIN_PROJECT_NAME_LENGTH, "Project name")

    @field_validator("key")
    @classmethod
    def _key(cls, v: Optional[str]) -> Optional[str]:
        if v is None:
            return None
        v = v.strip().upper()
        if not v:
            return None
        if not re.match(r"^[A-Z][A-Z0-9]{1,15}$", v):
            raise ValueError(
                "Project key must be 2-16 chars, start with a letter, and contain only A-Z / 0-9"
            )
        return v

    @field_validator("description")
    @classmethod
    def _desc(cls, v: str) -> str:
        return v.strip() if isinstance(v, str) else v


class ProjectOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: int
    name: str
    key: str
    description: str
    color: str
    created_at: datetime
    updated_at: datetime
    # Convenience flag the SPA uses to decide whether to show
    # "manage members" / "delete project" UI on each card.
    can_manage: bool = False
    member_count: int = 0


# ---------------------------------------------------------------------------
# Project membership
# ---------------------------------------------------------------------------
class ProjectMembershipIn(BaseModel):
    user_id: int
    role: str = Field(default="member")

    @field_validator("role")
    @classmethod
    def _role(cls, v: str) -> str:
        return _normalize_project_role(v)


class ProjectMembershipUpdate(BaseModel):
    role: str

    @field_validator("role")
    @classmethod
    def _role(cls, v: str) -> str:
        return _normalize_project_role(v)


class ProjectMembershipOut(BaseModel):
    """One member of a project, with their org-level + project-level info
    rolled together for the membership panel."""
    id: int
    user_id: int
    user_name: str
    user_email: str
    user_role: str           # org-level role
    project_role: str        # project-level role (lead | member)
    created_at: datetime


# ---------------------------------------------------------------------------
# Bug
# ---------------------------------------------------------------------------
class BugCreate(BaseModel):
    project_id: int
    title: str = Field(max_length=200)
    description: str = Field(default="", max_length=10000)
    reporter_id: Optional[int] = None
    assignee_ids: list[int] = Field(default_factory=list)
    status: str = Field(default="New")
    priority: str = Field(default="Medium")
    environment: str = Field(default="DEV")
    due_date: Optional[str] = None

    @field_validator("title")
    @classmethod
    def _title(cls, v: str) -> str:
        return _strip_and_check_min_length(v, MIN_TITLE_LENGTH, "Title")

    @field_validator("description")
    @classmethod
    def _desc(cls, v: str) -> str:
        return v.strip() if isinstance(v, str) else v

    @field_validator("status")
    @classmethod
    def _status(cls, v: str) -> str:
        return _normalize_choice(v, ALLOWED_STATUSES, "status")

    @field_validator("priority")
    @classmethod
    def _priority(cls, v: str) -> str:
        return _normalize_choice(v, ALLOWED_PRIORITIES, "priority")

    @field_validator("environment")
    @classmethod
    def _env(cls, v: str) -> str:
        return _normalize_choice(v, ALLOWED_ENVIRONMENTS, "environment")

    @field_validator("due_date")
    @classmethod
    def _due(cls, v: Optional[str]) -> Optional[str]:
        if v in (None, ""):
            return None
        try:
            datetime.strptime(v, "%Y-%m-%d")
        except ValueError as exc:
            raise ValueError("due_date must be YYYY-MM-DD") from exc
        return v

    @field_validator("assignee_ids")
    @classmethod
    def _dedup(cls, v: list[int]) -> list[int]:
        seen: list[int] = []
        for x in v or []:
            if x not in seen:
                seen.append(x)
        return seen


class BugUpdate(BaseModel):
    project_id: Optional[int] = None
    title: Optional[str] = Field(default=None, max_length=200)
    description: Optional[str] = Field(default=None, max_length=10000)
    reporter_id: Optional[int] = None
    assignee_ids: Optional[list[int]] = None
    status: Optional[str] = None
    priority: Optional[str] = None
    environment: Optional[str] = None
    due_date: Optional[str] = None

    @field_validator("title")
    @classmethod
    def _title(cls, v: Optional[str]) -> Optional[str]:
        if v is None:
            return None
        return _strip_and_check_min_length(v, MIN_TITLE_LENGTH, "Title")

    @field_validator("description")
    @classmethod
    def _desc(cls, v: Optional[str]) -> Optional[str]:
        return v.strip() if isinstance(v, str) else v

    @field_validator("status")
    @classmethod
    def _status(cls, v: Optional[str]) -> Optional[str]:
        return None if v is None else _normalize_choice(v, ALLOWED_STATUSES, "status")

    @field_validator("priority")
    @classmethod
    def _priority(cls, v: Optional[str]) -> Optional[str]:
        return None if v is None else _normalize_choice(v, ALLOWED_PRIORITIES, "priority")

    @field_validator("environment")
    @classmethod
    def _env(cls, v: Optional[str]) -> Optional[str]:
        return None if v is None else _normalize_choice(v, ALLOWED_ENVIRONMENTS, "environment")

    @field_validator("due_date")
    @classmethod
    def _due(cls, v: Optional[str]) -> Optional[str]:
        if v in (None, ""):
            return None
        try:
            datetime.strptime(v, "%Y-%m-%d")
        except ValueError as exc:
            raise ValueError("due_date must be YYYY-MM-DD") from exc
        return v

    @field_validator("assignee_ids")
    @classmethod
    def _dedup(cls, v: Optional[list[int]]) -> Optional[list[int]]:
        if v is None:
            return None
        seen: list[int] = []
        for x in v:
            if x not in seen:
                seen.append(x)
        return seen


class AttachmentBrief(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: int
    filename: str
    content_type: str
    size_bytes: int
    uploader_user_id: Optional[int] = None
    uploader_name: str
    comment_id: Optional[int] = None
    created_at: datetime


class BugOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: int
    project_id: int
    project_name: Optional[str] = None
    project_key: Optional[str] = None
    title: str
    description: str
    reporter: Optional[UserBrief] = None
    assignees: list[UserBrief] = Field(default_factory=list)
    status: str
    priority: str
    environment: str
    due_date: Optional[str]
    created_at: datetime
    updated_at: datetime
    attachment_count: int = 0
    can_edit: bool = False


class BugListResponse(BaseModel):
    items: list[BugOut]
    page: int
    page_size: int
    total: int
    pages: int


class CommentIn(BaseModel):
    body: str = Field(min_length=1, max_length=10000)

    @field_validator("body")
    @classmethod
    def _body(cls, v: str) -> str:
        return _strip_and_check_min_length(v, 1, "Comment body")


class CommentOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: int
    bug_id: int
    author_user_id: Optional[int] = None
    author_name: str
    body: str
    created_at: datetime
    attachments: list[AttachmentBrief] = Field(default_factory=list)


class ActivityOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: int
    bug_id: Optional[int] = None
    entity_type: str
    entity_id: Optional[int] = None
    actor_user_id: Optional[int] = None
    actor_name: str
    action: str
    detail: str
    created_at: datetime


class BugDetail(BugOut):
    comments: list[CommentOut] = Field(default_factory=list)
    activities: list[ActivityOut] = Field(default_factory=list)
    attachments: list[AttachmentBrief] = Field(default_factory=list)


# ---------------------------------------------------------------------------
# Sessions (admin only)
# ---------------------------------------------------------------------------
class SessionOut(BaseModel):
    id: int
    user_id: int
    user_name: Optional[str] = None
    user_email: Optional[str] = None
    user_role: Optional[str] = None
    ip_address: str
    user_agent: str
    created_at: datetime
    last_seen_at: datetime
    expires_at: datetime
    is_current: bool = False


# ---------------------------------------------------------------------------
# Stats
# ---------------------------------------------------------------------------
class StatsOut(BaseModel):
    bugs: int
    open: int
    resolved: int
    closed: int
    resolve_later: int
    projects: int = 0
    users: int = 0
    by_status: dict[str, int]
    by_priority: dict[str, int]
    by_environment: dict[str, int]
    by_project: list[dict[str, Any]]
    by_assignee: list[dict[str, Any]]
    timeline: list[dict[str, Any]]
