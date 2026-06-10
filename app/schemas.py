"""Pydantic schemas (request/response DTOs) — v4.0 multi-tenant."""
from __future__ import annotations

import re
from datetime import datetime
from html.parser import HTMLParser
from typing import Any, Optional

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


# ---------------------------------------------------------------------------
# v2.6 — HTML sanitizer for rich-text fields (description, comment body)
#
# The v2.6 frontend swapped plain textareas for a contenteditable
# rich-text editor that emits HTML (bold/italic/underline/lists/quotes
# /code/images). Storing the HTML lets us preserve the formatting on
# read, but a naïve `innerHTML = body` would be a stored-XSS bug — a
# user could paste `<script>` or an `onerror` attribute and trigger
# arbitrary JS for every viewer.
#
# We sanitize on the server before storing so the database holds clean
# HTML. The allowlist is tight: only the formatting tags the editor
# actually produces, plus inline `<img>` so pasted screenshots survive
# the round-trip (the editor base64-encodes pastes; that's a `data:`
# URL on src, which we whitelist).
#
# Why an in-house sanitizer rather than `bleach`? Bleach pulls in
# `html5lib` and adds 200 KB of dependency surface. For the
# constrained tag set we ship from the editor, a 60-line allowlist
# parser is plenty. If the formatting grows past this, swap in bleach
# behind the same `sanitize_html()` interface.
# ---------------------------------------------------------------------------
_ALLOWED_TAGS = {
    "p", "br", "div", "span",
    "b", "strong", "i", "em", "u", "s", "strike", "del", "ins",
    "ul", "ol", "li",
    "blockquote", "pre", "code",
    "h1", "h2", "h3", "h4", "h5", "h6",
    "a", "img",
}
_ALLOWED_ATTRS = {
    # Per-tag attr allowlist. Anything missing here is stripped, even
    # for whitelisted tags — that's how we avoid `<img onerror=...>` and
    # the like.
    "a":   {"href", "title", "rel"},
    "img": {"src", "alt", "title", "width", "height"},
    "code": {"class"},   # editor sometimes emits `<code class="language-X">`
    "pre":  {"class"},
}
# Schemes allowed on `href` / `src`. `data:` is allowed only for image
# pastes; we check the URL scheme + MIME prefix together below.
_ALLOWED_URL_SCHEMES = ("http:", "https:", "mailto:", "/", "#")


class _HTMLAllowlistSanitizer(HTMLParser):
    """Drops every tag/attr that isn't on the allowlist. Output is the
    surviving HTML — text content always survives even when the parent
    tag is stripped."""
    # convert_charrefs=False so we re-emit `&amp;` / `&lt;` faithfully
    # rather than collapsing them into raw characters that the next
    # serialiser would have to re-escape.
    def __init__(self) -> None:
        super().__init__(convert_charrefs=False)
        self.out: list[str] = []

    def _safe_url(self, raw: str) -> Optional[str]:
        if not raw:
            return None
        s = raw.strip()
        low = s.lower()
        # Explicit data:image/* (pasted screenshot) — capped here at
        # ~10 MB after base64 to avoid runaway storage. Real upload-
        # based attachments don't go through this path; this is for
        # inline pastes only.
        if low.startswith("data:image/"):
            if len(s) > 14 * 1024 * 1024:
                return None
            return s
        for scheme in _ALLOWED_URL_SCHEMES:
            if low.startswith(scheme):
                return s
        return None

    def handle_starttag(self, tag: str, attrs: list[tuple[str, Optional[str]]]) -> None:
        t = tag.lower()
        if t not in _ALLOWED_TAGS:
            return
        kept = []
        allowed_attrs = _ALLOWED_ATTRS.get(t, set())
        for k, v in attrs:
            k = k.lower()
            if k not in allowed_attrs:
                continue
            if v is None:
                continue
            if k in ("href", "src"):
                clean = self._safe_url(v)
                if not clean:
                    continue
                v = clean
            # Escape attribute value for HTML embedding. We never let
            # the value contain quotes / angles.
            v_safe = (v.replace("&", "&amp;").replace("<", "&lt;")
                       .replace(">", "&gt;").replace('"', "&quot;"))
            kept.append(f'{k}="{v_safe}"')
        # `a` tags get a forced rel for any external link.
        if t == "a":
            has_rel = any(p.startswith("rel=") for p in kept)
            if not has_rel:
                kept.append('rel="noopener nofollow"')
        attr_str = (" " + " ".join(kept)) if kept else ""
        self.out.append(f"<{t}{attr_str}>")

    def handle_endtag(self, tag: str) -> None:
        t = tag.lower()
        if t not in _ALLOWED_TAGS:
            return
        # Void elements don't take a closing tag.
        if t in ("br", "img"):
            return
        self.out.append(f"</{t}>")

    def handle_startendtag(self, tag: str, attrs: list[tuple[str, Optional[str]]]) -> None:
        # `<br/>` / `<img .../>` — re-emit as start-tag only.
        self.handle_starttag(tag, attrs)

    def handle_data(self, data: str) -> None:
        self.out.append(
            data.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
        )

    def handle_entityref(self, name: str) -> None:
        self.out.append(f"&{name};")

    def handle_charref(self, name: str) -> None:
        self.out.append(f"&#{name};")


def sanitize_html(value: Optional[str]) -> str:
    """Return a safe-for-display HTML string. Empty input → empty
    string. Removes every tag/attr outside the allowlist; text content
    survives. Idempotent: sanitize(sanitize(x)) == sanitize(x)."""
    if value is None:
        return ""
    s = str(value)
    p = _HTMLAllowlistSanitizer()
    p.feed(s)
    p.close()
    return "".join(p.out)


# ---------------------------------------------------------------------------
# Allowed values
# ---------------------------------------------------------------------------
# Statuses — v2.5 makes the status set PER-ITEM-TYPE so a workflow term
# only applies to the work flavor where it makes sense ("Not a Bug" is a
# Bug-only verdict; "Blocked" / "Done" are Task-only; "Approved" /
# "Implemented" are Requirement-only). The single shared status — present
# in every list — is "New", so any pre-v2.5 row in the database (which
# defaults to "New" on create) stays valid for its item_type without any
# data fix-up. Existing rows holding a status that's no longer valid for
# their current item_type aren't rejected on read; they're displayed as-is
# and can be UPDATED to a valid value — but updates that try to MOVE to an
# invalid value are rejected by the route layer.
#
# ALLOWED_STATUSES is the UNION (kept for backwards compatibility with the
# filter endpoints and any external client that still POSTs a status
# without an item_type). The per-type sets below are the source of truth
# for create/update validation.
STATUSES_BY_TYPE = {
    "Bug": [
        "New", "In Progress", "Resolved", "Closed", "Reopened",
        "Not a Bug", "Resolve Later",
    ],
    "Requirement": [
        "New", "In Review", "Approved", "Implemented", "Rejected", "Deferred",
    ],
    "Task": [
        "New", "In Progress", "Done", "Blocked", "Cancelled",
    ],
}
# Union of every status anywhere in the system. Used by the list filter
# endpoint (?status=…) which is type-agnostic, and by the legacy single
# global status dropdown some external clients still rely on.
ALLOWED_STATUSES = list(
    dict.fromkeys(s for sts in STATUSES_BY_TYPE.values() for s in sts)
)
EXCLUDED_FROM_TOTAL_STATUSES = ["Not a Bug"]


def statuses_for_type(item_type: str) -> list[str]:
    """Return the valid status list for a given item_type, falling back to
    the Bug list if the type is unknown (preserves legacy behaviour for
    rows from before this column existed)."""
    return STATUSES_BY_TYPE.get(item_type or "Bug", STATUSES_BY_TYPE["Bug"])


ALLOWED_PRIORITIES = ["Low", "Medium", "High", "Critical"]
ALLOWED_ENVIRONMENTS = ["DEV", "UAT", "PROD"]
# v2.4: three work-item flavours sharing one numbering sequence.
ALLOWED_ITEM_TYPES = ["Bug", "Requirement", "Task"]
DEFAULT_ITEM_TYPE = "Bug"
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


class BrandingInfo(BaseModel):
    """Lightweight subset of /api/branding that's safe to ship to any
    user (no settings / secrets). Lets the SPA theme itself on boot."""
    model_config = ConfigDict(from_attributes=True)
    logo_data_url: str | None = None
    accent_color: str | None = None


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
    # v2.2 additions — both defaulted so older callers don't break.
    totp_enabled: bool = False
    branding: BrandingInfo | None = None


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
    # v2.6: description is rich HTML; up to 1 MB so multiple inline
    # pasted screenshots (base64 data URLs) fit. Sanitized below.
    description: str = Field(default="", max_length=1_000_000)
    reporter_id: Optional[int] = None
    assignee_ids: list[int] = Field(default_factory=list)
    status: str = Field(default="New")
    priority: str = Field(default="Medium")
    environment: str = Field(default="DEV")
    due_date: Optional[str] = None
    # v2.4: type + optional event container.
    item_type: str = Field(default=DEFAULT_ITEM_TYPE)
    event_id: Optional[int] = None

    @field_validator("title")
    @classmethod
    def _title(cls, v: str) -> str:
        return _strip_and_check_min_length(v, MIN_TITLE_LENGTH, "Title")

    @field_validator("description")
    @classmethod
    def _desc(cls, v: str) -> str:
        # v2.6: description is now rich HTML emitted by the SPA editor.
        # Sanitize against the allowlist before storage; strip surrounding
        # whitespace so an "empty" HTML body (e.g. "<p><br></p>") still
        # round-trips as effectively-empty for length checks downstream.
        if not isinstance(v, str):
            return v
        return sanitize_html(v.strip())

    @field_validator("item_type")
    @classmethod
    def _item_type(cls, v: str) -> str:
        return _normalize_choice(v, ALLOWED_ITEM_TYPES, "item_type")

    @field_validator("status")
    @classmethod
    def _status(cls, v: str) -> str:
        # Type-aware status validation happens in model_validator below
        # (this field-level check just confirms the value is at least in
        # the global union).
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

    @model_validator(mode="after")
    def _check_status_for_type(self) -> "BugCreate":
        # The status must belong to the chosen item_type's set. "New" is in
        # every set so the default value always passes regardless of type.
        # BugUpdate does NOT get this validator — the route layer enforces
        # it there, because item_type may itself be changing on the same
        # request and the validator can't see the pre-existing row.
        allowed = statuses_for_type(self.item_type)
        if self.status not in allowed:
            raise ValueError(
                f"Status '{self.status}' is not valid for {self.item_type}. "
                f"Allowed: {', '.join(allowed)}"
            )
        return self


class BugUpdate(BaseModel):
    project_id: Optional[int] = None
    title: Optional[str] = Field(default=None, max_length=200)
    description: Optional[str] = Field(default=None, max_length=1_000_000)
    reporter_id: Optional[int] = None
    assignee_ids: Optional[list[int]] = None
    status: Optional[str] = None
    priority: Optional[str] = None
    environment: Optional[str] = None
    due_date: Optional[str] = None
    # v2.4: editable type + event link. event_id can be set to null to
    # detach the item from its event.
    item_type: Optional[str] = None
    event_id: Optional[int] = None

    @field_validator("title")
    @classmethod
    def _title(cls, v: Optional[str]) -> Optional[str]:
        if v is None:
            return None
        return _strip_and_check_min_length(v, MIN_TITLE_LENGTH, "Title")

    @field_validator("item_type")
    @classmethod
    def _item_type(cls, v: Optional[str]) -> Optional[str]:
        return None if v is None else _normalize_choice(v, ALLOWED_ITEM_TYPES, "item_type")

    @field_validator("description")
    @classmethod
    def _desc(cls, v: Optional[str]) -> Optional[str]:
        # See BugCreate.description — same sanitization on update.
        if v is None:
            return None
        if not isinstance(v, str):
            return v
        return sanitize_html(v.strip())

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
    # v2.4: item flavour + optional event link.
    item_type: str = DEFAULT_ITEM_TYPE
    event_id: Optional[int] = None
    event_name: Optional[str] = None
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


# ---------------------------------------------------------------------------
# Event (v2.4)
#
# Container for groups of work items. The Create / Update / Out trio
# mirrors the Project schemas. `manager_ids` is admin/manager-only at
# the route layer; the server validates each id belongs to the actor's
# org AND has the admin or manager role.
# ---------------------------------------------------------------------------
class EventCreate(BaseModel):
    name: str = Field(max_length=200)
    description: str = Field(default="", max_length=10000)
    scheduled_for: Optional[str] = None
    manager_ids: list[int] = Field(default_factory=list)

    @field_validator("name")
    @classmethod
    def _name(cls, v: str) -> str:
        return _strip_and_check_min_length(v, 2, "Event name")

    @field_validator("description")
    @classmethod
    def _desc(cls, v: str) -> str:
        return v.strip() if isinstance(v, str) else v

    @field_validator("scheduled_for")
    @classmethod
    def _scheduled(cls, v: Optional[str]) -> Optional[str]:
        if v in (None, ""):
            return None
        try:
            datetime.strptime(v, "%Y-%m-%d")
        except ValueError as exc:
            raise ValueError("scheduled_for must be YYYY-MM-DD") from exc
        return v


class EventUpdate(BaseModel):
    name: Optional[str] = Field(default=None, max_length=200)
    description: Optional[str] = Field(default=None, max_length=10000)
    scheduled_for: Optional[str] = None
    manager_ids: Optional[list[int]] = None

    @field_validator("name")
    @classmethod
    def _name(cls, v: Optional[str]) -> Optional[str]:
        return None if v is None else _strip_and_check_min_length(v, 2, "Event name")

    @field_validator("description")
    @classmethod
    def _desc(cls, v: Optional[str]) -> Optional[str]:
        return v.strip() if isinstance(v, str) else v

    @field_validator("scheduled_for")
    @classmethod
    def _scheduled(cls, v: Optional[str]) -> Optional[str]:
        if v in (None, ""):
            return None
        try:
            datetime.strptime(v, "%Y-%m-%d")
        except ValueError as exc:
            raise ValueError("scheduled_for must be YYYY-MM-DD") from exc
        return v


class EventOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: int
    name: str
    description: str
    scheduled_for: Optional[str] = None
    managers: list[UserBrief] = Field(default_factory=list)
    item_count: int = 0
    created_at: datetime
    updated_at: datetime
    can_edit: bool = False
    can_delete: bool = False


class EventItemBrief(BaseModel):
    """One row in the event-detail item list — same columns as the
    main work-items table so the UI can render them identically."""
    model_config = ConfigDict(from_attributes=True)
    id: int
    item_type: str
    title: str
    project_id: int
    project_name: Optional[str] = None
    project_key: Optional[str] = None
    status: str
    priority: str
    environment: str
    due_date: Optional[str] = None
    assignees: list[UserBrief] = Field(default_factory=list)
    attachment_count: int = 0


class EventDetailOut(EventOut):
    items: list[EventItemBrief] = Field(default_factory=list)


class CommentIn(BaseModel):
    # v2.6: allow up to 200 KB so a pasted screenshot (base64 data URL)
    # fits. The HTML is sanitized below, so dangerous payloads are
    # stripped even if a client tries to abuse the larger ceiling.
    body: str = Field(min_length=1, max_length=200_000)

    @field_validator("body")
    @classmethod
    def _body(cls, v: str) -> str:
        # v2.6: comments are now rich HTML. We sanitize on the server
        # to block stored-XSS regardless of the SPA editor's behaviour,
        # then verify the visible-text length is at least 1 char so a
        # whitespace-only post still gets rejected. An image-only
        # comment (e.g. a pasted screenshot with no caption) is allowed
        # — the presence of an <img> tag counts as non-empty.
        if not isinstance(v, str):
            raise ValueError("Comment body must be a string")
        cleaned = sanitize_html(v.strip())
        text_only = re.sub(r"<[^>]+>", "", cleaned).strip()
        if not text_only and "<img" not in cleaned.lower():
            raise ValueError("Comment body cannot be empty")
        return cleaned


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
    # v2.4: type-breakdown counts. Always GLOBAL (not filtered by the
    # item_type query param) so the tab badges keep showing reality.
    # Includes an "Event" key with the event count even though events
    # live in a separate table — the SPA renders one unified row.
    by_type: dict[str, int] = Field(default_factory=dict)
    by_project: list[dict[str, Any]]
    by_assignee: list[dict[str, Any]]
    timeline: list[dict[str, Any]]


# ---------------------------------------------------------------------------
# v2.10 — Push notifications (FCM)
# ---------------------------------------------------------------------------
class DeviceTokenIn(BaseModel):
    """Body the Android client POSTs to /api/devices/register.

    `token` is the opaque Firebase registration token (~200 chars in
    practice). `platform` is a free-form tag — Android sends 'android';
    we accept anything ≤ 16 chars so a future iOS client doesn't need a
    schema bump.
    """
    token: str = Field(min_length=10, max_length=512)
    platform: str = Field(default="android", max_length=16)

    @field_validator("platform")
    @classmethod
    def _platform_lower(cls, v: str) -> str:
        return (v or "android").strip().lower() or "android"


class DeviceTokenOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    platform: str
    last_seen_at: Optional[datetime] = None
    created_at: datetime


class NotificationPreferencesOut(BaseModel):
    """Per-user push channel toggles. Missing row = all-on; the response
    reflects that derived default rather than 404-ing the client."""
    mentions: bool = True
    assignments: bool = True
    activity: bool = True


class NotificationPreferencesIn(BaseModel):
    """PATCH-style: only the toggled channels need to be present."""
    mentions: Optional[bool] = None
    assignments: Optional[bool] = None
    activity: Optional[bool] = None
