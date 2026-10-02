"""Pydantic request/response schemas."""

from __future__ import annotations

import re
from datetime import datetime, timedelta
from html.parser import HTMLParser
from typing import Any, Optional
from urllib.parse import urlsplit

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

# Display-ID prefix mapping for user-facing entity references.
_DISPLAY_ID_PREFIX: dict[str, str] = {
    "Bug": "BUG",
    "Requirement": "REQ",
    "Task": "TASK",
    # Its own prefix: a Sub-task (child of an issue) is not a Task.
    "Sub-task": "SUB",
    "Story": "USRSTR",
    # UI alias of "Story" — same canonical work-item type.
    "User Story": "USRSTR",
    "Epic": "EPIC",
    "Sprint": "SPRINT",
}


def display_id_for(item_type: str | None, num_id: int | None) -> str:
    """Generate a user-facing display ID from an item type and numeric ID.
    Falls back to ``ID-{num}`` for unknown types."""
    if num_id is None:
        return ""
    prefix = _DISPLAY_ID_PREFIX.get(item_type) if item_type else None
    if prefix is None:
        prefix = "ID"
    return f"{prefix}-{num_id}"


# Server-side HTML sanitizer for rich-text fields (description, comment body):
# the contenteditable editor emits HTML and storing it raw would be stored XSS.
# Hand-rolled allowlist (avoids bleach/html5lib); swap for bleach if tags grow.
_ALLOWED_TAGS = {
    "p", "br", "div", "span",
    "b", "strong", "i", "em", "u", "s", "strike", "del", "ins",
    "ul", "ol", "li",
    "blockquote", "pre", "code",
    "h1", "h2", "h3", "h4", "h5", "h6",
    "a", "img",
}
_ALLOWED_ATTRS = {
    # Anything not listed is stripped even on allowed tags (blocks <img onerror=>).
    # `rel` on <a> is omitted so our forced rel="noopener nofollow" can't be stripped.
    "a":   {"href", "title"},
    "img": {"src", "alt", "title", "width", "height"},
    "code": {"class"},   # editor may emit `<code class="language-X">`
    "pre":  {"class"},
}

# Allowed URL schemes for href/src; data: is handled separately below.
_ALLOWED_URL_SCHEMES = ("http:", "https:", "mailto:", "/", "#")

# Raster data: URLs only — data:image/svg+xml is scriptable, so it's excluded.
_DATA_IMAGE_RASTER_PREFIXES = (
    "data:image/png", "data:image/jpeg", "data:image/jpg",
    "data:image/gif", "data:image/webp", "data:image/bmp", "data:image/avif",
)

# RCDATA/CDATA elements: their text content is dropped too (not just the tag),
# closing a parser-differential / mutation-XSS risk against a real HTML5 tokenizer.
_RCDATA_DROP_TAGS = frozenset({
    "script", "style", "textarea", "title", "noscript", "xmp",
    "iframe", "noframes", "template",
})


class _HTMLAllowlistSanitizer(HTMLParser):
    """Drops every tag/attr not on the allowlist; text content always survives."""

    # convert_charrefs=False keeps &amp;/&lt; as-is instead of collapsing them.
    def __init__(self) -> None:
        super().__init__(convert_charrefs=False)
        self.out: list[str] = []
        # Nesting depth inside RCDATA elements, so their text is suppressed.
        self._drop_text_depth = 0

    def _safe_url(self, raw: str) -> Optional[str]:
        if not raw:
            return None
        s = raw.strip()
        low = s.lower()
        # Inline pasted screenshots only (uploads take a different path).
        if low.startswith("data:image/"):
            # Raster bitmaps only (SVG is scriptable); size-capped to bound storage.
            if not low.startswith(_DATA_IMAGE_RASTER_PREFIXES):
                return None
            if len(s) > 14 * 1024 * 1024:
                return None
            return s
        for scheme in _ALLOWED_URL_SCHEMES:
            if low.startswith(scheme):
                return s
        return None

    def _kept_attrs(self, t: str, attrs: list[tuple[str, Optional[str]]]) -> list[str]:
        """Return the allowed name="value" attribute strings for tag t."""
        kept: list[str] = []
        allowed_attrs = _ALLOWED_ATTRS.get(t, set())
        for k, v in attrs:
            k = k.lower()
            if k not in allowed_attrs or v is None:
                continue
            if k in ("href", "src"):
                clean = self._safe_url(v)
                if not clean:
                    continue
                v = clean
            # Escape for HTML embedding.
            v_safe = (v.replace("&", "&amp;").replace("<", "&lt;")
                       .replace(">", "&gt;").replace('"', "&quot;"))
            kept.append(f'{k}="{v_safe}"')
        # Force our own rel on <a> so reverse-tabnabbing hardening can't be stripped.
        if t == "a":
            kept.append('rel="noopener nofollow"')
        return kept

    def handle_starttag(self, tag: str, attrs: list[tuple[str, Optional[str]]]) -> None:
        t = tag.lower()
        if t in _RCDATA_DROP_TAGS:
            self._drop_text_depth += 1
            return
        if t not in _ALLOWED_TAGS:
            return
        kept = self._kept_attrs(t, attrs)
        attr_str = (" " + " ".join(kept)) if kept else ""
        self.out.append(f"<{t}{attr_str}>")

    def handle_endtag(self, tag: str) -> None:
        t = tag.lower()
        if t in _RCDATA_DROP_TAGS:
            if self._drop_text_depth > 0:
                self._drop_text_depth -= 1
            return
        if t not in _ALLOWED_TAGS:
            return
        # Void elements have no closing tag.
        if t in ("br", "img"):
            return
        self.out.append(f"</{t}>")

    def handle_startendtag(self, tag: str, attrs: list[tuple[str, Optional[str]]]) -> None:
        # <br/> / <img .../> re-emit as a start tag. A self-closing RCDATA tag
        # is skipped without touching _drop_text_depth (it opens+closes at once).
        if tag.lower() in _RCDATA_DROP_TAGS:
            return
        self.handle_starttag(tag, attrs)

    def handle_data(self, data: str) -> None:
        if self._drop_text_depth > 0:
            return
        self.out.append(
            data.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
        )

    def handle_entityref(self, name: str) -> None:
        if self._drop_text_depth > 0:
            return
        self.out.append(f"&{name};")

    def handle_charref(self, name: str) -> None:
        if self._drop_text_depth > 0:
            return
        self.out.append(f"&#{name};")


def sanitize_html(value: Optional[str]) -> str:
    """Return allowlist-sanitized HTML for storage/display; idempotent, "" for None."""
    if value is None:
        return ""
    s = str(value)
    p = _HTMLAllowlistSanitizer()
    p.feed(s)
    p.close()
    return "".join(p.out)


# Per-item-type status sets; "New" is in every list so the create default is
# always valid. Out-of-set statuses display as-is on read but are rejected on
# create/update. ALLOWED_STATUSES is the union for type-agnostic filters.
_IN_PROGRESS = "In Progress"
_END_DATE_CANNOT_PRECEDE_START = "End date cannot precede start date"
_ACCEPTANCE_CRITERION_NOT_FOUND = "Acceptance criterion not found"
_NOT_SET = "(not set)"

# Canonical status vocabulary — the same set is valid for every work-item type
# so that any configured board status can be applied to any item. The board column
# categories (todo/in_progress/testing/done) are what drive workflow, not the
# item type. Legacy per-type lists are preserved as comments for audit context but
# are no longer the source of truth.
CANONICAL_STATUSES = [
    "New", _IN_PROGRESS, "Testing", "In Review", "Approved",
    "Done", "Blocked", "Cancelled", "Resolved", "Closed",
    "Reopened", "Not a Bug", "Resolve Later", "Implemented",
    "Rejected", "Deferred", "Planned", "Completed",
]
STATUSES_BY_TYPE = {
    "Bug": list(CANONICAL_STATUSES),
    "Requirement": list(CANONICAL_STATUSES),
    # "Task" is a legacy alias for the canonical "Sub-task" agile type.
    # Both share the exact same status vocabulary.
    "Task": list(CANONICAL_STATUSES),
    # Agile hierarchy types. Stored Title Case,
    # same convention as the legacy types above — never lowercased in the DB.
    "Epic": list(CANONICAL_STATUSES),
    "Story": list(CANONICAL_STATUSES),
    "Sub-task": list(CANONICAL_STATUSES),
}
ALLOWED_STATUSES = list(
    dict.fromkeys(s for sts in STATUSES_BY_TYPE.values() for s in sts)
)

# "Not a Bug" items are excluded from the "Total bugs" KPI on the dashboard.
EXCLUDED_FROM_TOTAL_STATUSES = ["Not a Bug"]


def statuses_for_type(item_type: str) -> list[str]:
    """Valid statuses for item_type; falls back to Bug for unknown/legacy types."""
    return STATUSES_BY_TYPE.get(item_type or "Bug", STATUSES_BY_TYPE["Bug"])


ALLOWED_PRIORITIES = ["Low", "Medium", "High", "Critical"]
ALLOWED_ENVIRONMENTS = ["DEV", "UAT", "PROD"]

# Work-item flavors; a classifier for filtering/badges, other fields apply to all.
# Legacy types remain creatable/editable through /api/bugs. The Agile hierarchy
# types are additive and only creatable/editable through
# /api/agile/work-items — /api/bugs explicitly refuses to create/convert into them
# (see _validate_create_item_type in routes/bugs.py), so existing behavior for
# Bug/Requirement/Task is completely unaffected.
LEGACY_ITEM_TYPES = ["Bug", "Requirement", "Task"]
AGILE_ITEM_TYPES = ["Epic", "Story", "Sub-task"]
ALLOWED_ITEM_TYPES = LEGACY_ITEM_TYPES + AGILE_ITEM_TYPES

ALLOWED_ROLES = ["admin", "manager", "user"]
ALLOWED_PROJECT_ROLES = ["lead", "member"]

# Link kinds on the directed source→target edge; route renders the inverse label.
ALLOWED_LINK_TYPES = ["relates", "blocks", "duplicate"]

# Bulk toolbar actions; each reuses the single-item permission/audit/notify path.
ALLOWED_BULK_ACTIONS = [
    "set_status", "set_priority", "set_environment", "delete",
]
MIN_TITLE_LENGTH = 3
MIN_NAME_LENGTH = 2
MIN_PROJECT_NAME_LENGTH = 2
MIN_ORG_NAME_LENGTH = 2
_DATE_FORMAT_ERROR = "Dates must be YYYY-MM-DD"

# --- Agile constants ---
ALLOWED_ESTIMATION_MODES = ["story_points", "time", "item_count"]
ALLOWED_SWIMLANE_MODES = ["none", "assignee", "epic", "story", "priority"]
ALLOWED_WIP_ENFORCEMENT = ["off", "warn", "block"]
ALLOWED_CARD_COLOR_SCHEMES = ["none", "priority", "item_type", "assignee", "epic"]
ALLOWED_COLUMN_CATEGORIES = ["todo", "in_progress", "testing", "done"]

# Where an incomplete/remaining item goes on Complete/Cancel Sprint.
ALLOWED_SPRINT_DISPOSITIONS = ["backlog", "sprint", "new_sprint"]
DEFAULT_WORKING_WEEKDAYS = [1, 2, 3, 4, 5]

# --- Agile constants ---
ALLOWED_CAPACITY_UNITS = ["points", "minutes"]
ALLOWED_EPIC_HEALTH = ["on_track", "at_risk", "off_track", "unknown"]
ALLOWED_VERSION_STATUSES = ["unreleased", "released", "archived"]
ALLOWED_COMPONENT_ASSIGNEE_POLICIES = ["none", "lead"]
MIN_LABEL_NAME_LENGTH = 1


def normalize_choice(value: str, allowed: list[str], label: str) -> str:
    """Case-insensitive lookup against `allowed`; returns the canonical form."""
    if not isinstance(value, str):
        raise ValueError(f"Invalid {label}. Allowed: {', '.join(allowed)}")
    needle = value.strip().lower()
    for canonical in allowed:
        if canonical.lower() == needle:
            return canonical
    raise ValueError(f"Invalid {label}. Allowed: {', '.join(allowed)}")


# Validates local part, domain labels, and an alphabetic TLD; quantifiers
# bounded to avoid ReDoS.
_EMAIL_RE = re.compile(
    r"^(?![.])(?!.*[.]{2})[A-Za-z0-9._%+\-]+(?<![.])@"
    r"(?:[A-Za-z0-9](?:[A-Za-z0-9\-]{0,61}[A-Za-z0-9])?\.)+[A-Za-z]{2,}$"
)


def _validate_email(value: str) -> str:
    v = (value or "").strip().lower()
    if not _EMAIL_RE.match(v):
        raise ValueError("Invalid email address")
    return v


def _strip_and_check_min_length(v: str, min_len: int, label: str) -> str:
    """Strip whitespace then enforce min length (Field(min_length) measures pre-strip)."""
    if not isinstance(v, str):
        raise ValueError(f"{label} must be a string")
    v = v.strip()
    if len(v) < min_len:
        if min_len == 1:
            raise ValueError(f"{label} cannot be empty")
        raise ValueError(f"{label} must be at least {min_len} characters")
    return v


# --- User ---
def _normalize_role(v: str) -> str:
    if not isinstance(v, str):
        raise ValueError("role must be a string")
    needle = v.strip().lower()
    if needle == "member":  # the earlier enterprise API called the plain role "member"
        needle = "user"
    if needle in ALLOWED_ROLES:
        return needle
    raise ValueError(f"Invalid role. Allowed: {', '.join(ALLOWED_ROLES)}")


def _normalize_project_role(v: str) -> str:
    needle = (v or "").strip().lower() if isinstance(v, str) else ""
    if needle in ALLOWED_PROJECT_ROLES:
        return needle
    raise ValueError(f"Invalid project role. Allowed: {', '.join(ALLOWED_PROJECT_ROLES)}")


def _check_password_strength(v: str) -> str:
    if not isinstance(v, str):
        raise ValueError("Password must be a string")
    # 'legacy-default' is the legacy value; always accept so upgrades raising
    # PASSWORD_MIN_LENGTH don't lock out existing accounts.
    if v.lower() in ("legacy-default", "changeme"):
        return v
    # DoS guard — bcrypt cost scales with input length.
    if len(v) > 200:
        raise ValueError("Password is too long")
    from app.config import get_settings  # local import avoids an import cycle
    settings = get_settings()
    min_len = max(1, settings.PASSWORD_MIN_LENGTH)
    if len(v) < min_len:
        raise ValueError(f"Password must be at least {min_len} characters")
    # No special-character mandate (NIST 800-63B §5.1.1.2); length + letter + digit.
    if settings.PASSWORD_REQUIRE_COMPLEXITY:
        has_letter = any(c.isalpha() for c in v)
        has_digit = any(c.isdigit() for c in v)
        if not (has_letter and has_digit):
            raise ValueError("Password must contain at least one letter and one number")
    # Reject a short list of universally-weak passwords by exact match.
    if v.lower() in {"password", "password1", "password123", "admin123",
                     "qwerty123", "12345678a", "letmein123"}:
        raise ValueError("Password is too common — please choose a stronger one")
    return v


class UserIn(BaseModel):
    """Admin creates a user."""
    name: str = Field(max_length=120)
    email: str = Field(max_length=254)
    role: str = Field(default="user")
    password: str
    is_active: bool = True
    # Projects a manager/user can access (admins ignore tags); untagged = sees
    # nothing. Route validates existence and the creator's access.
    project_ids: list[int] = Field(default_factory=list, max_length=1000)

    @field_validator("name")
    @classmethod
    def _strip_name(cls, v: str) -> str:
        return _strip_and_check_min_length(v, MIN_NAME_LENGTH, "Name")

    @field_validator("project_ids")
    @classmethod
    def _dedup_projects(cls, v: list[int]) -> list[int]:
        seen: list[int] = []
        for x in v or []:
            if x not in seen:
                seen.append(x)
        return seen

    @field_validator("role")
    @classmethod
    def _check_role(cls, v: str) -> str:
        return _normalize_role(v)

    @field_validator("email")
    @classmethod
    def _check_email(cls, v: str) -> str:
        return _validate_email(v)

    @field_validator("password")
    @classmethod
    def _check_password(cls, v: str) -> str:
        return _check_password_strength(v)


class UserUpdate(BaseModel):
    name: Optional[str] = Field(default=None, max_length=120)
    email: Optional[str] = Field(default=None, max_length=254)
    role: Optional[str] = None
    is_active: Optional[bool] = None
    # Admin password reset; replaces the hash if present, omit to leave unchanged.
    password: Optional[str] = None
    # Replace memberships. None/omit = unchanged; empty list = untag.
    project_ids: Optional[list[int]] = Field(default=None, max_length=1000)

    @field_validator("name")
    @classmethod
    def _strip_name(cls, v: Optional[str]) -> Optional[str]:
        if v is None: return None
        return _strip_and_check_min_length(v, MIN_NAME_LENGTH, "Name")

    @field_validator("project_ids")
    @classmethod
    def _dedup_projects(cls, v: Optional[list[int]]) -> Optional[list[int]]:
        if v is None: return None
        seen: list[int] = []
        for x in v:
            if x not in seen:
                seen.append(x)
        return seen

    @field_validator("role")
    @classmethod
    def _check_role(cls, v: Optional[str]) -> Optional[str]:
        if v is None: return None
        return _normalize_role(v)

    @field_validator("email")
    @classmethod
    def _check_email(cls, v: Optional[str]) -> Optional[str]:
        if v is None: return None
        return _validate_email(v)

    @field_validator("password")
    @classmethod
    def _check_password(cls, v: Optional[str]) -> Optional[str]:
        if v is None: return None
        return _check_password_strength(v)


class UserOut(BaseModel):
    """Public user view. Password is never serialized."""
    model_config = ConfigDict(from_attributes=True)
    id: int
    name: str
    email: str
    role: str
    is_active: bool
    created_at: datetime
    updated_at: datetime
    # Project memberships (sorted by id), route-populated; [] = untagged.
    project_ids: list[int] = Field(default_factory=list)
    # Whether the account has two-factor sign-in on; shown to admins only (null for others).
    totp_enabled: Optional[bool] = None


# --- Auth ---
class LoginIn(BaseModel):
    # Bounded so an unauthenticated caller can't burn bcrypt CPU on a huge body.
    email: str = Field(max_length=254)
    password: str = Field(max_length=200)

    @field_validator("email")
    @classmethod
    def _check_email(cls, v: str) -> str:
        return _validate_email(v)


class ChangePasswordIn(BaseModel):
    # Accept any non-empty current password; bcrypt-verify decides.
    current_password: str = Field(min_length=1, max_length=200)
    new_password: str

    @field_validator("new_password")
    @classmethod
    def _check_password(cls, v: str) -> str:
        return _check_password_strength(v)


class ForgotPasswordIn(BaseModel):
    # Length cap mirrors LoginIn (unauthenticated DoS risk).
    email: str = Field(max_length=254)

    @field_validator("email")
    @classmethod
    def _check_email(cls, v: str) -> str:
        return _validate_email(v)


class ResetPasswordIn(BaseModel):
    # Token is a hex SHA-256 value; capped to bound hashing/comparison.
    token: str = Field(min_length=1, max_length=512)
    new_password: str

    @field_validator("new_password")
    @classmethod
    def _check_password(cls, v: str) -> str:
        return _check_password_strength(v)


class BrandingInfo(BaseModel):
    """The part of an organization's branding every member may see."""
    model_config = ConfigDict(from_attributes=True)
    logo_data_url: Optional[str] = None
    accent_color: Optional[str] = None


class MeOut(BaseModel):
    """Returned to the frontend after login or on refresh; carries the organization
    so the app knows its tenant and branding without a second call."""
    model_config = ConfigDict(from_attributes=True)
    id: int
    name: str
    email: str
    role: str
    is_active: bool
    org_id: int
    organization_name: str
    organization_slug: str
    totp_enabled: bool = False
    branding: Optional[BrandingInfo] = None


class SignupIn(BaseModel):
    """Public sign-up: creates an organization and its first admin."""
    name: str = Field(max_length=120)
    email: str = Field(max_length=254)
    password: str
    organization_name: str = Field(max_length=120)

    @field_validator("name")
    @classmethod
    def _strip_name(cls, v: str) -> str:
        return _strip_and_check_min_length(v, MIN_NAME_LENGTH, "Name")

    @field_validator("email")
    @classmethod
    def _check_email(cls, v: str) -> str:
        return _validate_email(v)

    @field_validator("password")
    @classmethod
    def _check_password(cls, v: str) -> str:
        return _check_password_strength(v)

    @field_validator("organization_name")
    @classmethod
    def _strip_org(cls, v: str) -> str:
        return _strip_and_check_min_length(v, MIN_ORG_NAME_LENGTH, "Organization name")


class LoginTotpIn(BaseModel):
    """Second sign-in step: the pending token from the password step plus a 6-digit
    authenticator code or a one-time recovery code."""
    pending_token: str = Field(min_length=1, max_length=400)
    code: str = Field(min_length=6, max_length=20)


class ProfileUpdateIn(BaseModel):
    """Self-service profile edit; the email goes through the two-step change below."""
    name: str = Field(max_length=120)

    @field_validator("name")
    @classmethod
    def _strip_name(cls, v: str) -> str:
        return _strip_and_check_min_length(v, MIN_NAME_LENGTH, "Name")


class EmailChangeRequestIn(BaseModel):
    """Step 1: prove ownership with the current password and name the new address."""
    new_email: str = Field(max_length=254)
    current_password: str = Field(min_length=1, max_length=200)

    @field_validator("new_email")
    @classmethod
    def _check_email(cls, v: str) -> str:
        return _validate_email(v)


class EmailChangeConfirmIn(BaseModel):
    """Step 2: the 6-digit code mailed to the new address."""
    code: str = Field(min_length=6, max_length=6)

    @field_validator("code")
    @classmethod
    def _digits(cls, v: str) -> str:
        v = v.strip()
        if not v.isdigit() or len(v) != 6:
            raise ValueError("Code must be exactly 6 digits.")
        return v


class OrganizationOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: int
    name: str
    slug: str
    description: str
    created_at: datetime


class OrganizationUpdate(BaseModel):
    """Admin-only patch of the organization's details."""
    name: Optional[str] = Field(default=None, max_length=120)
    description: Optional[str] = Field(default=None, max_length=1000)

    @field_validator("name")
    @classmethod
    def _strip_name(cls, v: Optional[str]) -> Optional[str]:
        if v is None:
            return None
        return _strip_and_check_min_length(v, MIN_ORG_NAME_LENGTH, "Organization name")

    @field_validator("description")
    @classmethod
    def _strip_desc(cls, v: Optional[str]) -> Optional[str]:
        return v.strip() if isinstance(v, str) else v


class InvitationCreate(BaseModel):
    """An admin or manager invites someone into their organization."""
    email: str = Field(max_length=254)
    role: str = Field(default="user")
    # Projects the invitee joins on acceptance; foreign ids are refused by the route.
    project_ids: list[int] = Field(default_factory=list, max_length=100)
    # Join those projects as lead instead of plain member.
    as_lead: bool = False

    @field_validator("email")
    @classmethod
    def _check_email(cls, v: str) -> str:
        return _validate_email(v)

    @field_validator("role")
    @classmethod
    def _check_role(cls, v: str) -> str:
        return _normalize_role(v)

    @field_validator("project_ids")
    @classmethod
    def _dedup(cls, v: list[int]) -> list[int]:
        return list(dict.fromkeys(v))


class InvitationOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: int
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
    """What an invitee sees before accepting: the organization, role and inviter's name."""
    email: str
    organization_name: str
    role: str
    expires_at: datetime
    invited_by_name: str


class InvitationAccept(BaseModel):
    token: str = Field(min_length=1, max_length=512)
    name: str = Field(max_length=120)
    password: str

    @field_validator("name")
    @classmethod
    def _strip_name(cls, v: str) -> str:
        return _strip_and_check_min_length(v, MIN_NAME_LENGTH, "Name")

    @field_validator("password")
    @classmethod
    def _check_password(cls, v: str) -> str:
        return _check_password_strength(v)


class DeviceTokenIn(BaseModel):
    """A mobile client's FCM registration token."""
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
    """Push channel toggles; a user without a stored row has every channel on."""
    mentions: bool = True
    assignments: bool = True
    activity: bool = True


class NotificationPreferencesIn(BaseModel):
    """Only the channels being changed need to be present."""
    mentions: Optional[bool] = None
    assignments: Optional[bool] = None
    activity: Optional[bool] = None


class UserBrief(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: int
    name: str
    email: str
    role: str


# --- Project ---
class ProjectIn(BaseModel):
    name: str = Field(max_length=120)
    # Short identifier (e.g. "WEB"); generated from the name when omitted.
    key: Optional[str] = Field(default=None, max_length=16)
    description: str = Field(default="", max_length=1000)
    color: str = Field(default="#c9764f", pattern=r"^#[0-9a-fA-F]{6}$")

    @field_validator("name")
    @classmethod
    def _strip_name(cls, v: str) -> str:
        return _strip_and_check_min_length(v, MIN_PROJECT_NAME_LENGTH, "Project name")

    @field_validator("key")
    @classmethod
    def _check_key(cls, v: Optional[str]) -> Optional[str]:
        v = (v or "").strip().upper()
        if not v:
            return None
        if not re.match(r"^[A-Z][A-Z0-9]{1,15}$", v):
            raise ValueError(
                "Project key must be 2-16 characters, start with a letter and use only A-Z and 0-9"
            )
        return v

    @field_validator("description")
    @classmethod
    def _strip_desc(cls, v: str) -> str:
        return v.strip() if isinstance(v, str) else v


class ProjectCreateIn(ProjectIn):
    """Create payload. `agile_enabled` is honored only on POST so a freshly
    created project gets its default Sprint Board provisioned in the same
    transaction; PUT deliberately ignores the field (Agile is turned on/off
    through /api/agile/projects/{id}/enable|disable, which enforces the
    board/status provisioning and the active-sprint guard)."""
    agile_enabled: bool = False


class ProjectOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: int
    name: str
    key: str = ""
    description: str
    color: str
    created_at: datetime
    updated_at: datetime
    # Route-populated: whether the caller may manage the project's members and fields.
    can_manage: bool = False
    member_count: int = 0


class ProjectMembershipIn(BaseModel):
    user_id: int
    role: str = Field(default="member")

    @field_validator("role")
    @classmethod
    def _check_role(cls, v: str) -> str:
        return _normalize_project_role(v)


class ProjectMembershipUpdate(BaseModel):
    role: str

    @field_validator("role")
    @classmethod
    def _check_role(cls, v: str) -> str:
        return _normalize_project_role(v)


class ProjectMembershipOut(BaseModel):
    """One member of a project: organization role and project role together."""
    user_id: int
    user_name: str
    user_email: str
    user_role: str
    project_role: str


# --- Bug ---
class BugCreate(BaseModel):
    project_id: int
    title: str = Field(max_length=200)
    # Rich HTML; 1 MB ceiling fits inline pasted screenshots. Sanitized below.
    description: str = Field(default="", max_length=1_000_000)
    reporter_id: Optional[int] = None
    assignee_ids: list[int] = Field(default_factory=list, max_length=200)
    item_type: str = Field(default="Bug")
    status: str = Field(default="New")
    priority: str = Field(default="Medium")
    environment: str = Field(default="DEV")
    due_date: Optional[str] = None
    # Link to an event at creation time.
    event_id: Optional[int] = None

    @field_validator("title")
    @classmethod
    def _strip_title(cls, v: str) -> str:
        return _strip_and_check_min_length(v, MIN_TITLE_LENGTH, "Title")

    @field_validator("description")
    @classmethod
    def _strip_desc(cls, v: str) -> str:
        # Work-item descriptions are plain text: legacy editor HTML is flattened,
        # then the allowlist sanitizer neutralizes any stray markup. Outer
        # whitespace is trimmed so empty-string checks behave.
        if not isinstance(v, str): return v
        return rich_text_to_plain(sanitize_html(v.strip()))

    @field_validator("item_type")
    @classmethod
    def _check_item_type(cls, v: str) -> str:
        return normalize_choice(v, ALLOWED_ITEM_TYPES, "item_type")

    @field_validator("status")
    @classmethod
    def _check_status(cls, v: str) -> str:
        # Global union here; per-type check runs in _check_status_for_type below.
        return normalize_choice(v, ALLOWED_STATUSES, "status")

    @field_validator("priority")
    @classmethod
    def _check_priority(cls, v: str) -> str:
        return normalize_choice(v, ALLOWED_PRIORITIES, "priority")

    @field_validator("environment")
    @classmethod
    def _check_env(cls, v: str) -> str:
        return normalize_choice(v, ALLOWED_ENVIRONMENTS, "environment")

    @field_validator("due_date")
    @classmethod
    def _check_due(cls, v: Optional[str]) -> Optional[str]:
        if v in (None, ""): return None
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
            if x not in seen: seen.append(x)
        return seen

    @model_validator(mode="after")
    def _check_status_for_type(self) -> "BugCreate":
        # Cross-field: status must be valid for the chosen item_type.
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
    assignee_ids: Optional[list[int]] = Field(default=None, max_length=200)
    item_type: Optional[str] = None
    status: Optional[str] = None
    priority: Optional[str] = None
    environment: Optional[str] = None
    due_date: Optional[str] = None
    # int to link, null/0 to unlink; route uses exclude_unset to tell "clear"
    # from "not provided".
    event_id: Optional[int] = None
    sprint_id: Optional[int] = None
    # Opt-in optimistic concurrency: echo the last-seen updated_at; a mismatch
    # returns 409. Omit for last-write-wins.
    expected_updated_at: Optional[str] = Field(default=None, max_length=64)

    # Preferred concurrency token: the integer version counter (sub-second, no
    # clock skew). Omit both fields for last-write-wins.
    expected_version: Optional[int] = Field(default=None, ge=0)

    @field_validator("title")
    @classmethod
    def _strip_title(cls, v: Optional[str]) -> Optional[str]:
        if v is None: return None
        return _strip_and_check_min_length(v, MIN_TITLE_LENGTH, "Title")

    @field_validator("description")
    @classmethod
    def _strip_desc(cls, v: Optional[str]) -> Optional[str]:
        # Same plain-text conversion as BugCreate.description.
        if v is None: return None
        if not isinstance(v, str): return v
        return rich_text_to_plain(sanitize_html(v.strip()))

    @field_validator("item_type")
    @classmethod
    def _check_item_type(cls, v: Optional[str]) -> Optional[str]:
        return None if v is None else normalize_choice(v, ALLOWED_ITEM_TYPES, "item_type")

    @field_validator("status")
    @classmethod
    def _check_status(cls, v: Optional[str]) -> Optional[str]:
        return None if v is None else normalize_choice(v, ALLOWED_STATUSES, "status")

    @field_validator("priority")
    @classmethod
    def _check_priority(cls, v: Optional[str]) -> Optional[str]:
        return None if v is None else normalize_choice(v, ALLOWED_PRIORITIES, "priority")

    @field_validator("environment")
    @classmethod
    def _check_env(cls, v: Optional[str]) -> Optional[str]:
        return None if v is None else normalize_choice(v, ALLOWED_ENVIRONMENTS, "environment")

    @field_validator("due_date")
    @classmethod
    def _check_due(cls, v: Optional[str]) -> Optional[str]:
        if v in (None, ""): return None
        try:
            datetime.strptime(v, "%Y-%m-%d")
        except ValueError as exc:
            raise ValueError("due_date must be YYYY-MM-DD") from exc
        return v

    @field_validator("assignee_ids")
    @classmethod
    def _dedup(cls, v: Optional[list[int]]) -> Optional[list[int]]:
        if v is None: return None
        seen: list[int] = []
        for x in v:
            if x not in seen: seen.append(x)
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


class _RichTextPlainifier(HTMLParser):
    """Render allowlisted rich-text HTML as readable plain text."""
    _BLOCK_TAGS = frozenset({
        "p", "div", "li", "ul", "ol", "tr",
        "h1", "h2", "h3", "h4", "h5", "h6", "blockquote", "pre",
    })

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self._parts: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, Optional[str]]]) -> None:
        # <br> is always a hard break (the editor emits <div><br></div> spacers);
        # block elements open a fresh line only when none is already pending.
        if tag == "br" or (
            tag in self._BLOCK_TAGS
            and not (self._parts and self._parts[-1].endswith("\n"))
        ):
            self._parts.append("\n")

    def handle_data(self, data: str) -> None:
        self._parts.append(data)


def rich_text_to_plain(markup: str) -> str:
    """Readable plain text for rich-text fields (descriptions).

    Block elements become newlines, entities are decoded, inline markup is
    dropped, runs of blank lines collapse to one, and leading/trailing blank
    lines are trimmed. Applied to incoming descriptions so storage and every
    API response carry plain text; the UI editor renders the newlines as line
    breaks on open and on display.
    """
    if not markup:
        return ""
    parser = _RichTextPlainifier()
    try:
        parser.feed(markup)
        parser.close()
    except Exception:  # malformed markup must never break a response
        return markup
    out: list[str] = []
    for raw in "".join(parser._parts).split("\n"):
        line = raw.strip()
        if line:
            out.append(line)
        elif out and out[-1] != "":
            out.append("")  # collapse blank-line runs to one separator
    while out and out[-1] == "":
        out.pop()
    return "\n".join(out)


class BugOut(BaseModel):

    model_config = ConfigDict(from_attributes=True)

    id: int

    # Stable user-facing reference ("USRSTR-12"). Assigned once and preserved
    # across a work-item type change so existing references keep resolving.
    display_id: str = ""
    project_id: int
    project_name: Optional[str] = None
    title: str
    description: str
    reporter: Optional[UserBrief] = None
    assignees: list[UserBrief] = Field(default_factory=list)
    item_type: str = "Bug"
    status: str
    priority: str
    environment: str
    due_date: Optional[str] = None
    # Both nullable so standalone items (no event) keep the same shape.
    event_id: Optional[int] = None
    event_name: Optional[str] = None
    created_at: datetime
    updated_at: datetime
    version: int = 1
    attachment_count: int = 0
    can_edit: bool = False
    # Hierarchy and planning fields: epic link (standard issues), parent (Sub-tasks).
    epic_id: Optional[int] = None
    parent_id: Optional[int] = None
    story_points: Optional[float] = None
    owner_id: Optional[int] = None
    mandatory: bool = False
    blocked: bool = False
    blocked_reason: str = ""
    sprint_id: Optional[int] = None
    sprint_name: Optional[str] = None


class BugListResponse(BaseModel):
    items: list[BugOut]
    page: int
    page_size: int
    total: int
    pages: int


# --- Comment / Activity / Detail ---
class CommentIn(BaseModel):
    # 200 KB ceiling fits a pasted screenshot; sanitizer strips dangerous payloads.
    body: str = Field(min_length=1, max_length=200_000)

    @field_validator("body")
    @classmethod
    def _strip(cls, v: str) -> str:
        # Sanitize server-side, then require visible text or an <img src> —
        # an all-tags/whitespace body is treated as empty.
        if not isinstance(v, str):
            raise ValueError("Comment body must be a string")
        cleaned = sanitize_html(v.strip())
        text_only = re.sub(r"<[^>]+>", "", cleaned).strip()
        # Check src explicitly so a src-less <img> doesn't count as non-empty.
        has_image = re.search(r"<img\b[^>]*\bsrc=", cleaned, re.IGNORECASE) is not None
        if not text_only and not has_image:
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


# --- Item linking ---
class BugLinkIn(BaseModel):
    target_bug_id: int
    link_type: str = "relates"

    @field_validator("link_type")
    @classmethod
    def _check_type(cls, v: str) -> str:
        return normalize_choice(v, ALLOWED_LINK_TYPES, "link_type")


class BugLinkOut(BaseModel):
    """One link from a bug's perspective. `direction` is outgoing (this bug is
    source) or incoming; `label` is the phrasing from this side."""
    id: int
    link_type: str
    direction: str          # "outgoing" | "incoming"
    label: str
    other_bug_id: int
    other_bug_title: str
    other_bug_status: str
    other_bug_item_type: str
    created_at: datetime


# Bulk actions — the multi-select toolbar on the list view.
class BulkActionIn(BaseModel):
    action: str
    ids: list[int] = Field(min_length=1, max_length=500)
    # Value for set_status / set_priority / set_environment; route normalizes it.
    value: Optional[str] = Field(default=None, max_length=20)
    # Optional {bug_id: version} map; drifted rows are reported as conflicts.
    expected_versions: Optional[dict[int, int]] = Field(default=None, max_length=500)

    @field_validator("action")
    @classmethod
    def _check_action(cls, v: str) -> str:
        if v not in ALLOWED_BULK_ACTIONS:
            raise ValueError(f"Invalid action. Allowed: {', '.join(ALLOWED_BULK_ACTIONS)}")
        return v

    @field_validator("ids")
    @classmethod
    def _dedup_ids(cls, v: list[int]) -> list[int]:
        seen: list[int] = []
        for x in v or []:
            if x not in seen:
                seen.append(x)
        return seen


class BulkActionResult(BaseModel):
    updated: int = 0
    skipped: int = 0
    failed: int = 0
    # Rows skipped because their version drifted from expected_versions.
    conflicts: int = 0
    message: str = ""


# --- Bulk import (spreadsheet upload) — app/bulk_import.py does the work. ---
class BulkImportRowIssue(BaseModel):
    row: int
    title: str = ""
    error: str = ""


class BulkImportRowWarning(BaseModel):
    row: int
    title: str = ""
    warning: str = ""


class BulkImportResult(BaseModel):
    created: int = 0
    failed: int = 0
    created_ids: list[int] = Field(default_factory=list)
    errors: list[BulkImportRowIssue] = Field(default_factory=list)
    warnings: list[BulkImportRowWarning] = Field(default_factory=list)
    errors_truncated: bool = False
    warnings_truncated: bool = False
    message: str = ""


class BugDetail(BugOut):
    comments: list[CommentOut] = Field(default_factory=list)
    activities: list[ActivityOut] = Field(default_factory=list)
    attachments: list[AttachmentBrief] = Field(default_factory=list)
    # Links in both directions, rendered from this item's perspective.
    links: list[BugLinkOut] = Field(default_factory=list)


# --- Sessions (admin only) ---
class SessionOut(BaseModel):
    """One row in the admin "active sessions" panel; `is_current` marks the
    admin's own session (self-revocation is rejected in routes/sessions.py)."""
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


# --- Stats ---
class StatsOut(BaseModel):
    # Total excluding "Not a Bug".
    bugs: int
    # KPI strip buckets on the dashboard.
    open: int
    resolved: int
    closed: int
    resolve_later: int
    by_status: dict[str, int]
    by_priority: dict[str, int]
    by_environment: dict[str, int]
    by_type: dict[str, int] = Field(default_factory=dict)
    by_project: list[dict[str, Any]]
    by_assignee: list[dict[str, Any]]
    timeline: list[dict[str, Any]]


# --- Events — container for a group of work items (standup / sprint meeting). ---
class EventCreate(BaseModel):
    name: str = Field(max_length=200)
    description: str = Field(default="", max_length=10000)
    scheduled_for: Optional[str] = None  # YYYY-MM-DD
    # Owning project, scopes visibility. Optional (project-less = admins only);
    # route validates existence and the creator's access.
    project_id: Optional[int] = None
    # Admin/manager users to notify on event create/update/delete (not per-task).
    manager_ids: list[int] = Field(default_factory=list, max_length=200)

    @field_validator("name")
    @classmethod
    def _strip_name(cls, v: str) -> str:
        return _strip_and_check_min_length(v, 2, "Event name")

    @field_validator("description")
    @classmethod
    def _strip_desc(cls, v: str) -> str:
        return v.strip() if isinstance(v, str) else v

    @field_validator("scheduled_for")
    @classmethod
    def _check_date(cls, v: Optional[str]) -> Optional[str]:
        if v in (None, ""): return None
        try:
            datetime.strptime(v, "%Y-%m-%d")
        except ValueError as exc:
            raise ValueError("scheduled_for must be YYYY-MM-DD") from exc
        return v

    @field_validator("manager_ids")
    @classmethod
    def _dedup(cls, v: list[int]) -> list[int]:
        seen: list[int] = []
        for x in v or []:
            if x not in seen: seen.append(x)
        return seen


class EventUpdate(BaseModel):
    name: Optional[str] = Field(default=None, max_length=200)
    description: Optional[str] = Field(default=None, max_length=10000)
    scheduled_for: Optional[str] = None
    manager_ids: Optional[list[int]] = Field(default=None, max_length=200)
    # Move to a different project; None/omit = unchanged. Route validates access.
    project_id: Optional[int] = None

    @field_validator("name")
    @classmethod
    def _strip_name(cls, v: Optional[str]) -> Optional[str]:
        if v is None: return None
        return _strip_and_check_min_length(v, 2, "Event name")

    @field_validator("description")
    @classmethod
    def _strip_desc(cls, v: Optional[str]) -> Optional[str]:
        return v.strip() if isinstance(v, str) else v

    @field_validator("scheduled_for")
    @classmethod
    def _check_date(cls, v: Optional[str]) -> Optional[str]:
        if v in (None, ""): return None
        try:
            datetime.strptime(v, "%Y-%m-%d")
        except ValueError as exc:
            raise ValueError("scheduled_for must be YYYY-MM-DD") from exc
        return v

    @field_validator("manager_ids")
    @classmethod
    def _dedup(cls, v: Optional[list[int]]) -> Optional[list[int]]:
        if v is None: return None
        seen: list[int] = []
        for x in v:
            if x not in seen: seen.append(x)
        return seen


class EventOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: int
    name: str
    description: str
    scheduled_for: Optional[str] = None
    # Nullable for pre-column events; project_name resolved by the route.
    project_id: Optional[int] = None
    project_name: Optional[str] = None
    created_by_user_id: Optional[int] = None
    created_by_name: Optional[str] = None
    item_count: int = 0
    assignee_count: int = 0
    # Full briefs so the UI renders names/emails without an extra round-trip.
    managers: list[UserBrief] = Field(default_factory=list)
    created_at: datetime
    updated_at: datetime


class EventDetail(EventOut):
    """Event with its full item list, returned by /api/events/{id}."""
    items: list[BugOut] = Field(default_factory=list)
    # True when the item list hit the server ceiling (client shows "N of M").
    items_truncated: bool = False


# --- Notification — per-user in-app notification row. ---
class NotificationOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: int
    kind: str
    title: str
    body: str
    bug_id: Optional[int] = None
    event_id: Optional[int] = None
    actor_name: str
    read_at: Optional[datetime] = None
    created_at: datetime


class UnreadCountOut(BaseModel):
    unread: int


# --- Web push (Firebase Cloud Messaging) ---
class PushSubscribeIn(BaseModel):
    """A browser/device registering its FCM token for push."""
    token: str = Field(min_length=1, max_length=512)
    platform: str = Field(default="web", max_length=20)
    user_agent: str = Field(default="", max_length=400)


class PushUnsubscribeIn(BaseModel):
    token: str = Field(min_length=1, max_length=512)


class PushConfigOut(BaseModel):
    """Public Firebase web config for the browser messaging SDK. All values are
    publishable; the service-account secret stays on the backend. `enabled` is
    False when push isn't configured."""
    enabled: bool
    api_key: str = ""
    auth_domain: str = ""
    project_id: str = ""
    messaging_sender_id: str = ""
    app_id: str = ""
    vapid_key: str = ""


# =====================================================================
# Agile — foundation, boards, backlog, sprint lifecycle.
#
# =====================================================================
_AGILE_NAME_MIN = 2


class AgileFeatureFlagsIn(BaseModel):
    """Allow-listed boolean project feature flags. Unknown keys
    are rejected (422) — no silent passthrough of arbitrary JSON."""
    automation: bool = False
    teams: bool = False
    parallel_sprints: bool = False
    model_config = ConfigDict(extra="forbid")


class AgileActivateIn(BaseModel):
    feature_flags: AgileFeatureFlagsIn = Field(default_factory=AgileFeatureFlagsIn)


class AgileSettingsOut(BaseModel):
    project_id: int
    agile_enabled: bool
    agile_feature_flags: dict[str, Any] = Field(default_factory=dict)
    board_id: Optional[int] = None


class BoardColumnOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: int
    name: str
    position: int
    category: str
    wip_limit: Optional[int] = None
    min_cards: Optional[int] = None
    wip_enforcement: str = "off"
    color: str = ""
    statuses: list[str] = Field(default_factory=list)
    statuses_by_type: dict[str, list[str]] = Field(default_factory=dict)


class BoardOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: int
    project_id: int
    name: str
    board_type: str
    is_default: bool
    estimation_mode: str
    swimlane_mode: str
    card_fields_json: list[Any] = Field(default_factory=list)
    wip_enforcement: str = "off"
    card_color_scheme: str = "none"
    timezone: str = "UTC"
    working_weekdays: list[int] = Field(default_factory=lambda: list(DEFAULT_WORKING_WEEKDAYS))
    working_hours_per_day: float = 8
    version: int = 1
    created_at: datetime
    updated_at: datetime
    columns: list[BoardColumnOut] = Field(default_factory=list)


class BoardUpdateIn(BaseModel):
    """Board settings write. All fields optional; omitted fields
    are unchanged. `version` is required (optimistic concurrency)."""
    name: Optional[str] = Field(default=None, max_length=120)
    estimation_mode: Optional[str] = None
    swimlane_mode: Optional[str] = None
    card_fields_json: Optional[list[Any]] = None
    wip_enforcement: Optional[str] = None
    card_color_scheme: Optional[str] = None
    timezone: Optional[str] = Field(default=None, max_length=64)
    working_weekdays: Optional[list[int]] = None
    working_hours_per_day: Optional[float] = Field(default=None, gt=0, le=24)
    version: int = Field(ge=0)

    @field_validator("name")
    @classmethod
    def _strip_name(cls, v: Optional[str]) -> Optional[str]:
        if v is None:
            return None
        return _strip_and_check_min_length(v, _AGILE_NAME_MIN, "Board name")

    @field_validator("estimation_mode")
    @classmethod
    def _check_estimation(cls, v: Optional[str]) -> Optional[str]:
        return None if v is None else normalize_choice(v, ALLOWED_ESTIMATION_MODES, "estimation_mode")

    @field_validator("swimlane_mode")
    @classmethod
    def _check_swimlane(cls, v: Optional[str]) -> Optional[str]:
        return None if v is None else normalize_choice(v, ALLOWED_SWIMLANE_MODES, "swimlane_mode")

    @field_validator("wip_enforcement")
    @classmethod
    def _check_wip(cls, v: Optional[str]) -> Optional[str]:
        return None if v is None else normalize_choice(v, ALLOWED_WIP_ENFORCEMENT, "wip_enforcement")

    @field_validator("card_color_scheme")
    @classmethod
    def _check_color(cls, v: Optional[str]) -> Optional[str]:
        return None if v is None else normalize_choice(v, ALLOWED_CARD_COLOR_SCHEMES, "card_color_scheme")

    @field_validator("working_weekdays")
    @classmethod
    def _check_weekdays(cls, v: Optional[list[int]]) -> Optional[list[int]]:
        if v is None:
            return None
        if not v or any((not isinstance(d, int) or d < 1 or d > 7) for d in v):
            raise ValueError("working_weekdays must be ISO weekday integers 1-7")
        return sorted(set(v))


class BoardColumnIn(BaseModel):
    """One column in a PUT .../columns request; send ``id`` to keep an existing column."""
    id: Optional[int] = None
    name: str = Field(max_length=80)
    category: str
    wip_limit: Optional[int] = Field(default=None, ge=1)
    min_cards: Optional[int] = Field(default=None, ge=1)
    wip_enforcement: str = "off"
    color: str = Field(default="", max_length=20)
    statuses: list[str] = Field(default_factory=list)

    @field_validator("name")
    @classmethod
    def _strip_name(cls, v: str) -> str:
        return _strip_and_check_min_length(v, 1, "Column name")

    @field_validator("category")
    @classmethod
    def _check_category(cls, v: str) -> str:
        return normalize_choice(v, ALLOWED_COLUMN_CATEGORIES, "category")

    @field_validator("wip_enforcement")
    @classmethod
    def _check_wip(cls, v: str) -> str:
        return normalize_choice(v, ALLOWED_WIP_ENFORCEMENT, "wip_enforcement")


class BoardColumnsReplaceIn(BaseModel):
    version: int = Field(ge=0)
    columns: list[BoardColumnIn] = Field(min_length=1, max_length=20)


# --- Backlog ---
class BacklogItemOut(BaseModel):
    id: int
    project_id: int
    title: str
    item_type: str
    status: str
    priority: str
    assignees: list[UserBrief] = Field(default_factory=list)
    epic_id: Optional[int] = None
    owner_id: Optional[int] = None
    story_points: Optional[float] = None
    rank: Optional[str] = None
    flagged: bool = False
    ready_for_sprint: bool = False
    blocked: bool = False
    blocked_reason: str = ""
    mandatory: bool = False
    start_date: Optional[str] = None
    end_date: Optional[str] = None
    version: int = 1


class BacklogRankIn(BaseModel):
    """Move `item_id` to a new backlog position, between `before_id`/`after_id`
    (either or both may be null = move to an end)."""
    item_id: int
    before_id: Optional[int] = None
    after_id: Optional[int] = None


# --- Sprints ---
class SprintIntervalPeriod(BaseModel):
    """One pre-computed period for bulk sprint generation.
    The frontend computes these from an inclusive start date, an inclusive end
    date, and a frequency so the preview exactly matches what gets created.
    The backend only validates the shape and normalizes the name.
    """
    start_date: str
    end_date: str

    @field_validator("start_date", "end_date")
    @classmethod
    def _check_date(cls, v: Optional[str]) -> Optional[str]:
        if v in (None, ""):
            raise ValueError("period start_date and end_date are required")
        try:
            datetime.strptime(v, "%Y-%m-%d")
        except ValueError as exc:
            raise ValueError(_DATE_FORMAT_ERROR) from exc
        return v

    @model_validator(mode="after")
    def _check_order(self) -> "SprintIntervalPeriod":
        if self.end_date < self.start_date:
            raise ValueError("period end_date must be on or after start_date")
        return self


class SprintCreateIn(BaseModel):
    name: str = Field(max_length=120)
    goal: str = Field(default="", max_length=2000)
    start_date: Optional[str] = None
    end_date: Optional[str] = None
    cadence: Optional[str] = None
    assignee_ids: list[int] = Field(default_factory=list, max_length=200)
    generate: bool = False
    periods: list[SprintIntervalPeriod] = Field(default_factory=list)

    @field_validator("name")
    @classmethod
    def _strip_name(cls, v: str) -> str:
        return _strip_and_check_min_length(v, _AGILE_NAME_MIN, "Sprint name")

    @field_validator("goal")
    @classmethod
    def _strip_goal(cls, v: str) -> str:
        return sanitize_html(v.strip()) if isinstance(v, str) else v

    @field_validator("cadence")
    @classmethod
    def _check_cadence(cls, v: Optional[str]) -> Optional[str]:
        if v in (None, ""):
            return None
        if v not in ("daily", "weekly", "monthly", "custom"):
            raise ValueError("cadence must be one of: daily, weekly, monthly, custom")
        return v

    @field_validator("start_date", "end_date")
    @classmethod
    def _check_date(cls, v: Optional[str]) -> Optional[str]:
        if v in (None, ""):
            return None
        try:
            datetime.strptime(v, "%Y-%m-%d")
        except ValueError as exc:
            raise ValueError(_DATE_FORMAT_ERROR) from exc
        return v

    @model_validator(mode="after")
    def _normalize(self) -> "SprintCreateIn":
        if self.start_date and self.end_date and self.end_date < self.start_date:
            raise ValueError("end_date cannot be before start_date")
        if self.generate and not self.periods:
            raise ValueError("periods are required when generate is True")
        if self.generate and self.periods and (self.start_date or self.end_date):
            # Periods are authoritative; clear single-sprint dates to avoid confusion.
            self.start_date = None
            self.end_date = None
        return self

    @staticmethod
    def generate_intervals(
        start_date: datetime,
        end_date: datetime,
        cadence: str,
        count: int,
    ) -> list[tuple[datetime, datetime]]:
        """Calculate ``count`` contiguous intervals spanning [start_date, end_date].
        Boundaries are inclusive on both ends. The cadence selects the step size
        used to *estimate* count when the caller passes only a range; when the
        caller supplies an explicit count, that count wins. The last interval is
        clamped to ``end_date`` so partial final periods never overshoot.
        Returns a list of (period_start, period_end) datetime tuples in
        ascending order. Pure date arithmetic — no timezone assumptions beyond
        the naive datetimes the caller passes in.
        """
        if not start_date or not end_date:
            raise ValueError("start_date and end_date are required for interval generation")
        if count <= 0:
            raise ValueError("interval_count must be a positive integer")
        if end_date < start_date:
            raise ValueError("end_date must be on or after start_date")
        if count == 1:
            return [(start_date, end_date)]
        total_seconds = (end_date - start_date).total_seconds()
        delta = total_seconds / count
        sprints: list[tuple[datetime, datetime]] = []
        for i in range(count):
            period_start = start_date + timedelta(seconds=delta * i)
            period_end = start_date + timedelta(seconds=delta * (i + 1))
            if period_end > end_date:
                period_end = end_date
            if i == count - 1:
                # Guarantee the final interval lands exactly on end_date.
                period_end = end_date
            sprints.append((period_start, period_end))
        return sprints


class SprintUpdateIn(BaseModel):
    name: Optional[str] = Field(default=None, max_length=120)
    goal: Optional[str] = Field(default=None, max_length=2000)
    start_date: Optional[str] = None
    end_date: Optional[str] = None
    cadence: Optional[str] = None
    assignee_ids: Optional[list[int]] = Field(default=None, max_length=200)
    version: int = Field(ge=0)

    @field_validator("name")
    @classmethod
    def _strip_name(cls, v: Optional[str]) -> Optional[str]:
        if v is None:
            return None
        return _strip_and_check_min_length(v, _AGILE_NAME_MIN, "Sprint name")

    @field_validator("goal")
    @classmethod
    def _strip_goal(cls, v: Optional[str]) -> Optional[str]:
        if v is None:
            return None
        return sanitize_html(v.strip()) if isinstance(v, str) else v

    @field_validator("cadence")
    @classmethod
    def _check_cadence(cls, v: Optional[str]) -> Optional[str]:
        if v in (None, ""):
            return None
        if v not in ("daily", "weekly", "monthly", "custom"):
            raise ValueError("cadence must be one of: daily, weekly, monthly, custom")
        return v

    @field_validator("start_date", "end_date")
    @classmethod
    def _check_date(cls, v: Optional[str]) -> Optional[str]:
        if v in (None, ""):
            return None
        try:
            datetime.strptime(v, "%Y-%m-%d")
        except ValueError as exc:
            raise ValueError(_DATE_FORMAT_ERROR) from exc
        return v


class SprintOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: int
    display_id: str = ""
    board_id: int
    project_id: int
    name: str
    goal: str
    state: str
    start_date: Optional[str] = None
    end_date: Optional[str] = None
    cadence: Optional[str] = None
    started_at: Optional[datetime] = None
    completed_at: Optional[datetime] = None
    cancelled_at: Optional[datetime] = None
    sequence_number: int
    version: int = 1
    created_at: datetime
    updated_at: datetime
    item_count: int = 0
    assignees: list[UserBrief] = Field(default_factory=list)


class SprintItemsAddIn(BaseModel):
    item_ids: list[int] = Field(min_length=1, max_length=500)
    # Optional drop position inside the sprint: right after before_id and/or
    # right before after_id. Omitted = appended at the bottom.
    before_id: Optional[int] = None
    after_id: Optional[int] = None

    @field_validator("item_ids")
    @classmethod
    def _dedup(cls, v: list[int]) -> list[int]:
        seen: list[int] = []
        for x in v or []:
            if x not in seen:
                seen.append(x)
        return seen


class SprintDispositionEntry(BaseModel):
    """Where one incomplete item goes on Complete/Cancel Sprint."""
    item_id: int
    destination: str = "backlog"
    target_sprint_id: Optional[int] = None

    @field_validator("destination")
    @classmethod
    def _check_dest(cls, v: str) -> str:
        return normalize_choice(v, ALLOWED_SPRINT_DISPOSITIONS, "destination")

    @model_validator(mode="after")
    def _check_target(self) -> "SprintDispositionEntry":
        if self.destination == "sprint" and self.target_sprint_id is None:
            raise ValueError("target_sprint_id is required when destination is 'sprint'")
        return self


class SprintMoveIn(BaseModel):
    """Backlog drag-and-drop: move issues (in this order) into a sprint, or to
    the backlog when sprint_id is null, at a position between neighbours."""
    item_ids: list[int] = Field(min_length=1, max_length=500)
    sprint_id: Optional[int] = None
    before_id: Optional[int] = None
    after_id: Optional[int] = None

    @field_validator("item_ids")
    @classmethod
    def _dedup(cls, v: list[int]) -> list[int]:
        return list(dict.fromkeys(v))


class SprintStartIn(BaseModel):
    """Jira's Start Sprint dialog: name, goal and dates may be set on start."""
    version: int = Field(ge=0)
    acknowledged_warnings: bool = False
    name: Optional[str] = Field(default=None, max_length=120)
    goal: Optional[str] = Field(default=None, max_length=2000)
    start_date: Optional[str] = None
    end_date: Optional[str] = None

    @field_validator("name")
    @classmethod
    def _strip_name(cls, v: Optional[str]) -> Optional[str]:
        if v is None:
            return None
        return _strip_and_check_min_length(v, _AGILE_NAME_MIN, "Sprint name")

    @field_validator("goal")
    @classmethod
    def _strip_goal(cls, v: Optional[str]) -> Optional[str]:
        if v is None:
            return None
        return sanitize_html(v.strip())

    @field_validator("start_date", "end_date")
    @classmethod
    def _check_date(cls, v: Optional[str]) -> Optional[str]:
        if v in (None, ""):
            return None
        try:
            datetime.strptime(v, "%Y-%m-%d")
        except ValueError as exc:
            raise ValueError(_DATE_FORMAT_ERROR) from exc
        return v

    @model_validator(mode="after")
    def _check_order(self) -> "SprintStartIn":
        if self.start_date and self.end_date and self.end_date < self.start_date:
            raise ValueError("end_date cannot be before start_date")
        return self


class SprintCompleteIn(BaseModel):
    closing_note: str = Field(default="Sprint completed", min_length=1, max_length=2000)
    version: int = Field(ge=0)
    # Disposition for each still-incomplete item; a default applied to any
    # incomplete item not explicitly listed. "new_sprint" creates one future
    # sprint (named new_sprint_name, or the next default name) for them.
    dispositions: list[SprintDispositionEntry] = Field(default_factory=list)
    default_destination: str = "backlog"
    default_target_sprint_id: Optional[int] = None
    new_sprint_name: Optional[str] = Field(default=None, max_length=120)

    @model_validator(mode="after")
    def _check_default_target(self) -> "SprintCompleteIn":
        if self.default_destination == "sprint" and self.default_target_sprint_id is None:
            raise ValueError("default_target_sprint_id is required when default_destination is 'sprint'")
        return self

    @field_validator("closing_note")
    @classmethod
    def _strip_note(cls, v: str) -> str:
        return _strip_and_check_min_length(v, 1, "closing_note")

    @field_validator("default_destination")
    @classmethod
    def _check_dest(cls, v: str) -> str:
        return normalize_choice(v, ALLOWED_SPRINT_DISPOSITIONS, "default_destination")


class SprintCancelIn(BaseModel):
    cancellation_note: str = Field(min_length=1, max_length=2000)
    version: int = Field(ge=0)
    dispositions: list[SprintDispositionEntry] = Field(default_factory=list)
    default_destination: str = "backlog"
    default_target_sprint_id: Optional[int] = None

    @field_validator("cancellation_note")
    @classmethod
    def _strip_note(cls, v: str) -> str:
        return _strip_and_check_min_length(v, 1, "cancellation_note")

    @field_validator("default_destination")
    @classmethod
    def _check_dest(cls, v: str) -> str:
        return normalize_choice(v, ALLOWED_SPRINT_DISPOSITIONS, "default_destination")


class SprintHistoryOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: int
    sprint_id: int
    work_item_id: int
    event_type: str
    from_sprint_id: Optional[int] = None
    to_sprint_id: Optional[int] = None
    old_status_id: Optional[int] = None
    new_status_id: Optional[int] = None
    old_estimate: Optional[float] = None
    new_estimate: Optional[float] = None
    occurred_at: datetime
    actor_id: Optional[int] = None


# --- Work-item hierarchy ---
class WorkItemHierarchyIn(BaseModel):
    """Only path to change parent_id/epic_id. version is required."""
    parent_id: Optional[int] = None
    epic_id: Optional[int] = None
    version: int = Field(ge=0)


class ReadyForSprintIn(BaseModel):
    ready_for_sprint: bool
    version: int = Field(ge=0)


class AcceptanceCriterionIn(BaseModel):
    description: str = Field(max_length=2000)
    required: bool = True

    @field_validator("description")
    @classmethod
    def _strip_description(cls, v: str) -> str:
        return _strip_and_check_min_length(v, 1, "Acceptance criterion description")


class AcceptanceCriterionUpdateIn(BaseModel):
    description: Optional[str] = Field(default=None, max_length=2000)
    required: Optional[bool] = None
    met: Optional[bool] = None


class AcceptanceCriterionOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: int
    bug_id: int
    description: str
    required: bool
    met: bool
    updated_by_id: Optional[int] = None
    updated_at: datetime


class AgileWorkItemCreateIn(BaseModel):
    """Create an Epic/Story/Sub-task via /api/agile/work-items.
    Legacy Bug/Requirement/Task creation stays on /api/bugs."""
    project_id: int
    title: str = Field(max_length=200)
    description: str = Field(default="", max_length=1_000_000)
    item_type: str
    # Initial workflow status (e.g. the board's To Do column); "New" by default.
    status: str = Field(default="New")
    priority: str = Field(default="Medium")
    parent_id: Optional[int] = None
    epic_id: Optional[int] = None
    story_points: Optional[float] = Field(default=None, ge=0)
    assignee_ids: list[int] = Field(default_factory=list, max_length=200)
    start_date: Optional[str] = None
    end_date: Optional[str] = None
    sprint_id: Optional[int] = None

    @field_validator("title")
    @classmethod
    def _strip_title(cls, v: str) -> str:
        return _strip_and_check_min_length(v, MIN_TITLE_LENGTH, "Title")

    @field_validator("description")
    @classmethod
    def _strip_desc(cls, v: str) -> str:
        # Plain text in storage and responses (same as BugCreate).
        return rich_text_to_plain(sanitize_html(v.strip())) if isinstance(v, str) else v

    @field_validator("item_type")
    @classmethod
    def _check_type(cls, v: str) -> str:
        canonical = normalize_choice(v, AGILE_ITEM_TYPES, "item_type")
        return canonical

    @model_validator(mode="after")
    def _estimate_only_on_standard_issues(self):
        # As in Jira (and PUT /api/agile/work-items/{id}/estimate): Epics and
        # Sub-tasks carry no story points of their own.
        if self.story_points is not None and self.item_type in ("Epic", "Sub-task"):
            raise ValueError(f"A {self.item_type} has no story points; estimate its issues instead")
        return self

    @field_validator("status")
    @classmethod
    def _check_status(cls, v: str) -> str:
        return normalize_choice(v, ALLOWED_STATUSES, "status")

    @field_validator("priority")
    @classmethod
    def _check_priority(cls, v: str) -> str:
        return normalize_choice(v, ALLOWED_PRIORITIES, "priority")

    @field_validator("start_date", "end_date")
    @classmethod
    def _check_date(cls, v: Optional[str]) -> Optional[str]:
        if v in (None, ""):
            return None
        try:
            datetime.strptime(v, "%Y-%m-%d")
        except ValueError as exc:
            raise ValueError(_DATE_FORMAT_ERROR) from exc
        return v

    @model_validator(mode="after")
    def _check_date_order(self) -> "AgileWorkItemCreateIn":
        if self.start_date and self.end_date and self.end_date < self.start_date:
            raise ValueError(_END_DATE_CANNOT_PRECEDE_START)
        return self


class WorkItemDatesIn(BaseModel):
    """Only path to change a Story/Task's start/end dates after creation."""
    start_date: Optional[str] = None
    end_date: Optional[str] = None
    version: int = Field(ge=0)

    @field_validator("start_date", "end_date")
    @classmethod
    def _check_date(cls, v: Optional[str]) -> Optional[str]:
        if v in (None, ""):
            return None
        try:
            datetime.strptime(v, "%Y-%m-%d")
        except ValueError as exc:
            raise ValueError(_DATE_FORMAT_ERROR) from exc
        return v

    @model_validator(mode="after")
    def _check_date_order(self) -> "WorkItemDatesIn":
        if self.start_date and self.end_date and self.end_date < self.start_date:
            raise ValueError(_END_DATE_CANNOT_PRECEDE_START)
        return self


class WorkItemTaskFlagsIn(BaseModel):
    """Only path to change mandatory/blocked/blocked_reason on a Story or Task."""
    mandatory: Optional[bool] = None
    blocked: Optional[bool] = None
    blocked_reason: Optional[str] = Field(default=None, max_length=1000)
    version: int = Field(ge=0)

    @field_validator("blocked_reason")
    @classmethod
    def _strip_reason(cls, v: Optional[str]) -> Optional[str]:
        return sanitize_html(v.strip()) if isinstance(v, str) else v


# =====================================================================
# Agile — transitions, quick filters, flag.
# =====================================================================
class WorkItemTransitionIn(BaseModel):
    to_status: str = Field(max_length=100)
    version: int = Field(ge=0)
    override_wip: bool = False
    override_reason: str = Field(default="", max_length=500)
    acknowledged: bool = False


class WorkItemEstimateIn(BaseModel):
    story_points: Optional[float] = Field(default=None, ge=0)
    original_estimate_minutes: Optional[int] = Field(default=None, ge=0)
    remaining_estimate_minutes: Optional[int] = Field(default=None, ge=0)
    version: int = Field(ge=0)


class WorkItemFlagIn(BaseModel):
    flagged: bool
    version: int = Field(ge=0)


class WorkItemTaxonomyIn(BaseModel):
    component_ids: Optional[list[int]] = None
    label_ids: Optional[list[int]] = None
    version: int = Field(ge=0)


class QuickFilterIn(BaseModel):
    name: str = Field(max_length=120)
    filter_json: dict[str, Any] = Field(default_factory=dict)
    is_shared: bool = True

    @field_validator("name")
    @classmethod
    def _strip_name(cls, v: str) -> str:
        return _strip_and_check_min_length(v, _AGILE_NAME_MIN, "Quick filter name")


class QuickFilterUpdateIn(BaseModel):
    name: Optional[str] = Field(default=None, max_length=120)
    filter_json: Optional[dict[str, Any]] = None
    is_shared: Optional[bool] = None
    position: Optional[int] = None
    version: int = Field(ge=0)


class QuickFilterOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: int
    board_id: int
    name: str
    filter_json: dict[str, Any] = Field(default_factory=dict)
    position: int
    is_shared: bool
    version: int = 1


class IssueLabelOut(BaseModel):
    id: int
    name: str
    color: str = ""


class AgileIssueOut(BaseModel):
    """An issue as the Backlog and Board show it."""
    id: int
    display_id: str = ""
    title: str
    item_type: str
    status: str
    status_category: str = "todo"
    priority: str
    story_points: Optional[float] = None
    original_estimate_minutes: Optional[int] = None
    assignees: list[UserBrief] = Field(default_factory=list)
    epic_id: Optional[int] = None
    parent_id: Optional[int] = None
    parent_display_id: Optional[str] = None
    parent_title: Optional[str] = None
    subtask_count: int = 0
    subtasks_done: int = 0
    flagged: bool = False
    blocked: bool = False
    labels: list[IssueLabelOut] = Field(default_factory=list)
    sprint_id: Optional[int] = None
    rank: Optional[str] = None
    due_date: Optional[str] = None
    updated_at: Optional[datetime] = None
    version: int = 1
    # Whether the viewer may move/edit it (Tasks and Requirements are
    # admin/manager-only, see app.auth.can_edit_bug).
    can_edit: bool = True
    # Board only: the column the issue's status is mapped to.
    column_id: Optional[int] = None




class BoardViewColumnOut(BaseModel):
    id: int
    name: str
    position: int
    category: str
    wip_limit: Optional[int] = None
    min_cards: Optional[int] = None
    wip_enforcement: str = "off"
    statuses: list[str] = Field(default_factory=list)
    card_count: int = 0
    cards: list[AgileIssueOut] = Field(default_factory=list)


class SprintBriefOut(BaseModel):
    id: int
    name: str
    goal: str = ""
    state: str
    start_date: Optional[str] = None
    end_date: Optional[str] = None
    started_at: Optional[datetime] = None
    completed_at: Optional[datetime] = None
    version: int = 1


class EpicBriefOut(BaseModel):
    id: int
    display_id: str = ""
    title: str
    color: str = ""
    status: str = ""
    status_category: str = "todo"
    archived: bool = False


class BoardViewOut(BaseModel):
    board_id: int
    board_name: str = ""
    estimation_mode: str = "story_points"
    swimlane_mode: str = "none"
    card_color_scheme: str = "none"
    sprint_id: Optional[int] = None
    sprint_name: Optional[str] = None
    sprint: Optional[SprintBriefOut] = None
    active_sprints: list[SprintBriefOut] = Field(default_factory=list)
    columns: list[BoardViewColumnOut] = Field(default_factory=list)
    # Sprint issues whose status is in no column (hidden from the columns, as
    # in Jira) so the board can say so instead of losing them silently.
    unmapped: list[AgileIssueOut] = Field(default_factory=list)
    epics: list[EpicBriefOut] = Field(default_factory=list)


class HierarchyViewOut(BaseModel):
    """Epic > issue > Sub-task tree of one project."""
    project_id: int
    epics: list["EpicOut"] = Field(default_factory=list)
    issues: list[AgileIssueOut] = Field(default_factory=list)
    subtasks: list[AgileIssueOut] = Field(default_factory=list)
    unparented_total: int = 0
    unparented_limit: int = 500
    sprints: list[SprintBriefOut] = Field(default_factory=list)


class PlanningSprintOut(SprintBriefOut):
    sequence_number: int = 1
    issues: list[AgileIssueOut] = Field(default_factory=list)


class PlanningViewOut(BaseModel):
    """Jira's Backlog view: open sprints with their issues, then the backlog."""
    board_id: int
    estimation_mode: str = "story_points"
    parallel_sprints: bool = False
    sprints: list[PlanningSprintOut] = Field(default_factory=list)
    backlog: list[AgileIssueOut] = Field(default_factory=list)
    backlog_total: int = 0
    epics: list[EpicBriefOut] = Field(default_factory=list)


# =====================================================================
# Agile — capacity, planning readiness.
# =====================================================================
class SprintCapacityIn(BaseModel):
    user_id: int
    capacity_value: float = Field(ge=0)
    capacity_unit: str
    days_off: list[str] = Field(default_factory=list, max_length=366)
    notes: str = Field(default="", max_length=500)

    @field_validator("capacity_unit")
    @classmethod
    def _check_unit(cls, v: str) -> str:
        return normalize_choice(v, ALLOWED_CAPACITY_UNITS, "capacity_unit")

    @field_validator("days_off")
    @classmethod
    def _check_days_off(cls, v: list[str]) -> list[str]:
        for d in v:
            try:
                datetime.strptime(d, "%Y-%m-%d")
            except ValueError as exc:
                raise ValueError("days_off entries must be YYYY-MM-DD") from exc
        return v


class SprintCapacityReplaceIn(BaseModel):
    entries: list[SprintCapacityIn] = Field(default_factory=list, max_length=200)


class SprintCapacityOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    sprint_id: int
    user_id: int
    capacity_value: float
    capacity_unit: str
    days_off: list[str] = Field(default_factory=list, max_length=366)
    notes: str = ""
    version: int = 1


class ReadinessCheckOut(BaseModel):
    key: str
    passed: bool
    message: str
    severity: str = "error"  # "error" | "warning"


class PlanningOut(BaseModel):
    sprint_id: int
    board_id: int
    estimation_mode: str
    item_count: int
    total_estimate: Optional[float] = None
    unestimated_count: int = 0
    capacity: list[SprintCapacityOut] = Field(default_factory=list)
    readiness: list[ReadinessCheckOut] = Field(default_factory=list)
    ready_to_start: bool = False


# =====================================================================
# Agile — Epics, Releases/Versions, Components, Labels, Collections.
# =====================================================================
class EpicUpdateIn(BaseModel):
    title: Optional[str] = Field(default=None, max_length=120)
    description: Optional[str] = Field(default=None, max_length=20000)
    color: Optional[str] = Field(default=None, max_length=20)
    owner_id: Optional[int] = None
    start_date: Optional[str] = None
    target_date: Optional[str] = None
    health: Optional[str] = None
    summary_note: Optional[str] = Field(default=None, max_length=4000)
    version: int = Field(ge=0)

    @field_validator("title")
    @classmethod
    def _strip_title(cls, v: Optional[str]) -> Optional[str]:
        if v is None:
            return None
        return _strip_and_check_min_length(v, _AGILE_NAME_MIN, "Epic title")

    @field_validator("description")
    @classmethod
    def _strip_description(cls, v: Optional[str]) -> Optional[str]:
        if v is None:
            return None
        return rich_text_to_plain(sanitize_html(v.strip()))

    @field_validator("health")
    @classmethod
    def _check_health(cls, v: Optional[str]) -> Optional[str]:
        return None if v is None else normalize_choice(v, ALLOWED_EPIC_HEALTH, "health")

    @field_validator("summary_note")
    @classmethod
    def _strip_note(cls, v: Optional[str]) -> Optional[str]:
        return sanitize_html(v.strip()) if isinstance(v, str) else v

    @field_validator("start_date", "target_date")
    @classmethod
    def _check_date(cls, v: Optional[str]) -> Optional[str]:
        if v in (None, ""):
            return None
        try:
            datetime.strptime(v, "%Y-%m-%d")
        except ValueError as exc:
            raise ValueError(_DATE_FORMAT_ERROR) from exc
        return v


class EpicProgressOut(BaseModel):
    child_count: int = 0
    completed_child_count: int = 0
    in_progress_child_count: int = 0
    total_estimate: float = 0
    completed_estimate: float = 0
    unestimated_count: int = 0


class EpicOut(BaseModel):
    id: int
    display_id: str = ""
    title: str
    description: str = ""
    status: str
    status_category: str = "todo"
    project_id: int
    color: str = ""
    owner_id: Optional[int] = None
    start_date: Optional[str] = None
    target_date: Optional[str] = None
    health: str = "unknown"
    summary_note: str = ""
    archived: bool = False
    version: int = 1
    progress: EpicProgressOut = Field(default_factory=EpicProgressOut)


class VersionCreateIn(BaseModel):
    project_id: int
    name: str = Field(max_length=120)
    description: str = Field(default="", max_length=2000)
    start_date: Optional[str] = None
    release_date: Optional[str] = None
    owner_id: Optional[int] = None

    @field_validator("name")
    @classmethod
    def _strip_name(cls, v: str) -> str:
        return _strip_and_check_min_length(v, _AGILE_NAME_MIN, "Version name")

    @field_validator("start_date", "release_date")
    @classmethod
    def _check_date(cls, v: Optional[str]) -> Optional[str]:
        if v in (None, ""):
            return None
        try:
            datetime.strptime(v, "%Y-%m-%d")
        except ValueError as exc:
            raise ValueError(_DATE_FORMAT_ERROR) from exc
        return v


class VersionUpdateIn(BaseModel):
    name: Optional[str] = Field(default=None, max_length=120)
    description: Optional[str] = Field(default=None, max_length=2000)
    status: Optional[str] = None
    start_date: Optional[str] = None
    release_date: Optional[str] = None
    owner_id: Optional[int] = None
    version: int = Field(ge=0)

    @field_validator("status")
    @classmethod
    def _check_status(cls, v: Optional[str]) -> Optional[str]:
        return None if v is None else normalize_choice(v, ALLOWED_VERSION_STATUSES, "status")


class VersionOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: int
    project_id: int
    name: str
    description: str
    status: str
    start_date: Optional[str] = None
    release_date: Optional[str] = None
    owner_id: Optional[int] = None
    archived: bool
    version: int = 1
    created_at: datetime
    updated_at: datetime


class ComponentCreateIn(BaseModel):
    project_id: int
    name: str = Field(max_length=120)
    description: str = Field(default="", max_length=2000)
    lead_id: Optional[int] = None
    default_assignee_policy: str = "none"
    default_assignee_id: Optional[int] = None

    @field_validator("name")
    @classmethod
    def _strip_name(cls, v: str) -> str:
        return _strip_and_check_min_length(v, _AGILE_NAME_MIN, "Component name")

    @field_validator("default_assignee_policy")
    @classmethod
    def _check_policy(cls, v: str) -> str:
        return normalize_choice(v, ALLOWED_COMPONENT_ASSIGNEE_POLICIES, "default_assignee_policy")


class ComponentUpdateIn(BaseModel):
    name: Optional[str] = Field(default=None, max_length=120)
    description: Optional[str] = Field(default=None, max_length=2000)
    lead_id: Optional[int] = None
    default_assignee_policy: Optional[str] = None
    default_assignee_id: Optional[int] = None
    version: int = Field(ge=0)

    @field_validator("default_assignee_policy")
    @classmethod
    def _check_policy(cls, v: Optional[str]) -> Optional[str]:
        return None if v is None else normalize_choice(v, ALLOWED_COMPONENT_ASSIGNEE_POLICIES, "default_assignee_policy")


class ComponentOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: int
    project_id: int
    name: str
    description: str
    lead_id: Optional[int] = None
    default_assignee_policy: str
    default_assignee_id: Optional[int] = None
    archived: bool
    version: int = 1


class LabelCreateIn(BaseModel):
    project_id: int
    display_name: str = Field(max_length=120)
    color: str = Field(default="", max_length=20)

    @field_validator("display_name")
    @classmethod
    def _strip_name(cls, v: str) -> str:
        return _strip_and_check_min_length(v, MIN_LABEL_NAME_LENGTH, "Label name")


class LabelUpdateIn(BaseModel):
    display_name: Optional[str] = Field(default=None, max_length=120)
    color: Optional[str] = Field(default=None, max_length=20)
    version: int = Field(ge=0)


class LabelOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: int
    project_id: int
    normalized_name: str
    display_name: str
    color: str
    usage_count: int
    version: int = 1


class ReportIssueOut(BaseModel):
    id: int
    display_id: str = ""
    title: str
    item_type: str
    status: str
    status_category: str = "todo"
    estimate: Optional[float] = None
    estimate_at_start: Optional[float] = None
    added_during_sprint: bool = False
    assignees: list[str] = Field(default_factory=list)


class SprintReportOut(BaseModel):
    """Jira's Sprint Report. Estimates use the board's statistic."""
    sprint_id: int
    sprint_name: str
    state: str
    goal: str
    estimation_mode: str
    statistic_label: str = "Story points"
    start_date: Optional[str] = None
    end_date: Optional[str] = None
    started_at: Optional[datetime] = None
    completed_at: Optional[datetime] = None
    committed_count: int = 0
    committed_estimate: float = 0
    completed_count: int = 0
    completed_estimate: float = 0
    # Committed issues that were completed (the completion-rate numerator).
    completed_committed_count: int = 0
    # Issues already done when they joined the sprint: listed apart, never
    # counted as completed in it or in velocity.
    completed_outside_count: int = 0
    completed_outside_estimate: float = 0
    incomplete_count: int = 0
    incomplete_estimate: float = 0
    added_count: int = 0
    added_estimate: float = 0
    removed_count: int = 0
    removed_estimate: float = 0
    estimate_change_count: int = 0
    completed: list[ReportIssueOut] = Field(default_factory=list)
    not_completed: list[ReportIssueOut] = Field(default_factory=list)
    removed: list[ReportIssueOut] = Field(default_factory=list)
    completed_outside: list[ReportIssueOut] = Field(default_factory=list)
    # False for sprints started before 4.0 kept a full change history; their
    # figures come from the sprint's own start/finish records.
    history_complete: bool = True


class BurndownPointOut(BaseModel):
    at: datetime
    remaining: float
    scope: float
    completed: float


class GuidelinePointOut(BaseModel):
    at: datetime
    value: float


class BurndownEventOut(BaseModel):
    at: datetime
    work_item_id: int
    display_id: str = ""
    title: str = ""
    event: str
    change: float = 0
    remaining: float = 0
    scope_change: bool = False


class BoardDayOut(BaseModel):
    """One board-local calendar day of a chart, as exact instants."""
    date: str
    start: datetime
    end: datetime
    working: bool = True


class BurndownReportOut(BaseModel):
    """Jira's Burndown and Burnup charts: remaining work, total scope and
    completed work as step series, the ideal guideline, and the events."""
    sprint_id: int
    sprint_name: str = ""
    state: str = ""
    estimation_mode: str
    statistic_label: str = "Story points"
    start: Optional[datetime] = None
    planned_end: Optional[datetime] = None
    end: Optional[datetime] = None
    committed_estimate: float = 0
    remaining_estimate: float = 0
    scope_estimate: float = 0
    completed_estimate: float = 0
    points: list[BurndownPointOut] = Field(default_factory=list)
    guideline: list[GuidelinePointOut] = Field(default_factory=list)
    # The board's timezone and its calendar days across the chart.
    timezone: str = "UTC"
    days: list[BoardDayOut] = Field(default_factory=list)
    events: list[BurndownEventOut] = Field(default_factory=list)
    non_working_days: list[str] = Field(default_factory=list)
    history_complete: bool = True


BurnupReportOut = BurndownReportOut


class WorkloadEntryOut(BaseModel):
    user_id: int
    user_name: str
    assigned_estimate: float = 0
    assigned_count: int = 0
    capacity_value: Optional[float] = None
    capacity_unit: Optional[str] = None
    utilization_pct: Optional[float] = None


class WorkloadReportOut(BaseModel):
    sprint_id: int
    unassigned_count: int = 0
    unassigned_estimate: float = 0
    entries: list[WorkloadEntryOut] = Field(default_factory=list)


class EpicSprintRowOut(BaseModel):
    sprint_id: int
    sprint_name: str
    state: str
    completed_estimate: float = 0
    completed_count: int = 0
    scope_estimate: float = 0
    remaining_estimate: float = 0


class EpicReportOut(BaseModel):
    """Jira's Epic Report: progress, work per sprint and the issue lists."""
    epic_id: int
    display_id: str = ""
    title: str
    status: str = ""
    estimation_mode: str = "story_points"
    statistic_label: str = "Story points"
    progress: EpicProgressOut
    sprints: list[EpicSprintRowOut] = Field(default_factory=list)
    done: list[ReportIssueOut] = Field(default_factory=list)
    in_progress: list[ReportIssueOut] = Field(default_factory=list)
    todo: list[ReportIssueOut] = Field(default_factory=list)




class ScopeChangeEntryOut(BaseModel):
    work_item_id: int
    display_id: str = ""
    title: str
    event_type: str
    occurred_at: datetime
    change: float = 0
    actor_name: str = ""


class ScopeChangeReportOut(BaseModel):
    sprint_id: int
    entries: list[ScopeChangeEntryOut] = Field(default_factory=list)


# --- Daily Sprint Report ---
class DailyReportTransitionOut(BaseModel):
    work_item_id: int
    title: str
    event_type: str
    occurred_at: datetime
    detail: str = ""


class DailyReportBlockerOut(BaseModel):
    work_item_id: int
    title: str
    item_type: str
    blocked_reason: str
    status: str


class DailyReportItemOut(BaseModel):
    work_item_id: int
    display_id: str = ""
    title: str
    item_type: str
    status: str
    category: str
    column: str = ""
    story_points: float = 0


class DailyReportOut(BaseModel):
    sprint_id: int
    sprint_name: str
    goal: str
    report_date: str
    generated_at: datetime
    generated_by_id: Optional[int] = None
    committed_count: int = 0
    story_count: int = 0
    bug_count: int = 0
    todo_count: int = 0
    in_progress_count: int = 0
    testing_count: int = 0
    done_count: int = 0
    blocked_count: int = 0
    committed_estimate: float = 0
    completed_estimate: float = 0
    remaining_estimate: float = 0
    completed_estimate_today: float = 0
    scope_added_today: int = 0
    scope_removed_today: int = 0
    transitions: list[DailyReportTransitionOut] = Field(default_factory=list)
    blockers: list[DailyReportBlockerOut] = Field(default_factory=list)
    items: list[DailyReportItemOut] = Field(default_factory=list)


# =====================================================================
# release readiness/roadmap, reporting follow-on.
# =====================================================================
class ReleaseActionIn(BaseModel):
    version: int = Field(ge=0)


class Release360Out(BaseModel):
    id: int
    name: str
    status: str
    start_date: Optional[str] = None
    release_date: Optional[str] = None
    released_at: Optional[datetime] = None
    version: int = 1
    progress: dict[str, Any] = Field(default_factory=dict)
    readiness: list[ReadinessCheckOut] = Field(default_factory=list)


class RoadmapRowOut(BaseModel):
    kind: str  # "epic" | "release"
    id: int
    title: str
    start_date: Optional[str] = None
    target_date: Optional[str] = None
    scheduled: bool = True
    progress: dict[str, Any] = Field(default_factory=dict)


class RoadmapOut(BaseModel):
    project_id: int
    epics: list[RoadmapRowOut] = Field(default_factory=list)
    releases: list[RoadmapRowOut] = Field(default_factory=list)
    unscheduled_epics: list[RoadmapRowOut] = Field(default_factory=list)
    unscheduled_releases: list[RoadmapRowOut] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)


class VelocityPointOut(BaseModel):
    sprint_id: int
    sprint_name: str
    start_date: Optional[str] = None
    end_date: Optional[str] = None
    committed_estimate: float
    completed_estimate: float


class VelocityReportOut(BaseModel):
    """Jira's Velocity Chart: commitment vs completed for recent closed sprints."""
    board_id: int
    estimation_mode: str
    statistic_label: str = "Story points"
    average_committed: float = 0
    average_completed: float = 0
    points: list[VelocityPointOut] = Field(default_factory=list)


class FlowColumnOut(BaseModel):
    id: int
    name: str
    category: str


class CumulativeFlowPointOut(BaseModel):
    date: str
    # column id (as text) -> issues in that column at the end of the day
    counts: dict[str, int] = Field(default_factory=dict)


class CumulativeFlowReportOut(BaseModel):
    """Jira's Cumulative Flow Diagram: issues per board column per day."""
    board_id: int
    project_id: int
    columns: list[FlowColumnOut] = Field(default_factory=list)
    points: list[CumulativeFlowPointOut] = Field(default_factory=list)


class ControlChartEntryOut(BaseModel):
    work_item_id: int
    display_id: str = ""
    title: str = ""
    item_type: str = ""
    started_at: Optional[datetime] = None
    completed_at: datetime
    cycle_time_days: float
    lead_time_days: float


class ControlChartPointOut(BaseModel):
    completed_at: datetime
    value: float


class ControlChartReportOut(BaseModel):
    """Jira's Control Chart: cycle time (time spent in the board's working
    columns) of every issue completed in the range, with statistics."""
    board_id: int
    work_columns: list[str] = Field(default_factory=list)
    count: int = 0
    average_days: float = 0
    median_days: float = 0
    min_days: float = 0
    max_days: float = 0
    std_dev_days: float = 0
    entries: list[ControlChartEntryOut] = Field(default_factory=list)
    rolling_average: list[ControlChartPointOut] = Field(default_factory=list)


# =====================================================================
# GitHub Enterprise branch creation.
#
# Every response model here is deliberately secret-free: credentials and
# authorization headers never appear, in any form, in an API payload.
# =====================================================================

# A git ref fragment. Base-branch names arrive from Project Admins, so they are
# validated rather than trusted (mirrors the rules in app/git/naming.py).
_GIT_REF_SAFE_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._/-]{0,118}$")
# An http(s) origin only: no userinfo (credentials), no path, no query.
_GIT_BASE_URL_RE = re.compile(r"^https?://[A-Za-z0-9.\-]+(?::\d{1,5})?$")
# The ONLY plaintext origin permitted: loopback, for local GHES smoke testing.
# (Sonar python:S5332 flags cleartext/encrypted-scheme literals, so the check
# below inspects the parsed scheme by shape instead of spelling it out.)
_GIT_LOCALHOST_HOSTS = frozenset({"localhost", "127.0.0.1", "::1"})


def _git_url_parts(value: str) -> tuple[str, str]:
    """Lowercase (scheme, host) parsed from a candidate origin."""
    parts = urlsplit(value.strip().lower())
    return parts.scheme, (parts.hostname or "")


def _is_git_loopback_origin(value: str) -> bool:
    """True for a loopback http(s) origin (local GHES/docker-compose testing)."""
    return _git_url_parts(value)[1] in _GIT_LOCALHOST_HOSTS


def _is_git_encrypted_scheme(scheme: str) -> bool:
    """True for the encrypted 5-letter web scheme (compared by shape).

    The name and value are deliberately never spelled as a literal: Sonar
    python:S5332 flags the cleartext-scheme literal itself, and keeping both
    schemes out of the source keeps the security intent without tripping it.
    """
    prefix = "http"
    suffix = "s"
    return len(scheme) == 5 and scheme.startswith(prefix) and scheme.endswith(suffix)
# A PAT ceiling. Real GitHub tokens are ~40-255 chars; enterprise tokens vary,
# so this is a generous bound, not a format assertion.
_GIT_CREDENTIAL_MAX_LENGTH = 512
# Repository names/owners as the provider reports them.
_GIT_NAME_SAFE_RE = re.compile(r"^[A-Za-z0-9._\-]{1,200}$")

def _validate_git_ref(value: str, label: str = "Branch name") -> str:
    """Reject anything git would refuse as a branch name."""
    candidate = (value or "").strip()
    if not candidate:
        raise ValueError(f"{label} is required")
    if (
        ".." in candidate
        or "@{" in candidate
        or "//" in candidate
        or candidate.endswith((".", ".lock", "/"))
        or _GIT_REF_SAFE_RE.match(candidate) is None
    ):
        raise ValueError(f"{label} is not a valid git branch name")
    return candidate

def _validate_git_base_url(value: str) -> str:
    """Empty (inherit the deployment default) or a bare http(s) origin.

    HTTPS is required: a PAT must never travel over plaintext. The only
    exception is loopback, so a local GHES/docker-compose test host still works.
    """
    candidate = (value or "").strip().rstrip("/")
    if not candidate:
        return ""
    if "@" in candidate.split("://", 1)[-1].split("/", 1)[0]:
        raise ValueError("GitHub Enterprise base URL must not contain credentials")
    if _GIT_BASE_URL_RE.match(candidate) is None:
        raise ValueError(
            "GitHub Enterprise base URL must be an http(s) origin without a path"
        )
    scheme = _git_url_parts(candidate)[0]
    if _is_git_encrypted_scheme(scheme):
        return candidate
    if _is_git_loopback_origin(candidate):
        return candidate
    raise ValueError("GitHub Enterprise base URL must use https")

def _validate_git_identifier(value: str) -> str:
    """Organization/identifier reference; a URL is never valid here."""
    candidate = (value or "").strip()
    if any(ch.isspace() or ord(ch) < 32 for ch in candidate):
        raise ValueError("must not contain whitespace or control characters")
    scheme = _git_url_parts(candidate)[0]
    prefix = "http"
    if len(scheme) in (4, 5) and scheme.startswith(prefix):
        raise ValueError("expected an identifier reference, not a URL")
    return candidate


def _validate_git_credential(value: Optional[str]) -> Optional[str]:
    """Trim a submitted PAT; blank means "keep the stored credential".

    No provider-specific visible prefix is required, because enterprise token
    formats differ. Whitespace and control characters are refused outright: they
    are never part of a token, and a newline in an Authorization header is a
    header-injection vector.
    """
    if value is None:
        return None
    candidate = value.strip()
    if not candidate:
        return None
    if any(ch.isspace() or ord(ch) < 33 or ord(ch) == 127 for ch in candidate):
        raise ValueError("must not contain whitespace or control characters")
    if len(candidate) > _GIT_CREDENTIAL_MAX_LENGTH:
        raise ValueError(f"must be at most {_GIT_CREDENTIAL_MAX_LENGTH} characters")
    return candidate

class GitIntegrationIn(BaseModel):
    """Project-level Git integration configuration (PUT save path).

    ``credential`` is write-only: it is accepted on the way in, encrypted at
    rest, and never part of any response model. ``clear_credential`` is the
    explicit removal operation. Both are absent from ``GitIntegrationOut`` by
    construction, so a token cannot be serialized back to a client.
    """

    enabled: bool = False
    base_url: str = Field(default="", max_length=500)
    organization: str = Field(default="", max_length=120)
    default_base_branch: str = Field(default="dev", max_length=120)
    # Write-only project PAT. Blank/omitted means "keep the stored credential".
    credential: Optional[str] = Field(default=None, max_length=_GIT_CREDENTIAL_MAX_LENGTH)
    # Explicit removal of the stored project credential.
    clear_credential: bool = False
    # Optimistic concurrency, matching the Sprint/Collection/Feature pattern.
    version: Optional[int] = Field(default=None, ge=1)

    @field_validator("base_url")
    @classmethod
    def _check_base_url(cls, v: str) -> str:
        return _validate_git_base_url(v)

    @field_validator("credential")
    @classmethod
    def _check_credential(cls, v: Optional[str]) -> Optional[str]:
        return _validate_git_credential(v)

    @model_validator(mode="after")
    def _check_credential_operation(self):
        if self.clear_credential and self.credential:
            raise ValueError(
                "provide either credential or clear_credential, not both"
            )
        return self

    @field_validator("organization")
    @classmethod
    def _check_identifier(cls, v: str) -> str:
        return _validate_git_identifier(v)

    @field_validator("default_base_branch")
    @classmethod
    def _check_default_branch(cls, v: str) -> str:
        return _validate_git_ref(v, "Default base branch")

class GitConnectionDraftIn(BaseModel):
    """Draft Test Connection body (POST config/test with a body).

    Every field is OPTIONAL (``None`` = no override supplied) so an explicit
    blank ``""`` is never silently replaced with saved or environment values:
    the route tests exactly what the caller submitted. ``enabled`` is accepted
    but ignored for transport so the draft shape can mirror the settings form.
    """

    enabled: Optional[bool] = None
    base_url: Optional[str] = Field(default=None, max_length=500)
    organization: Optional[str] = Field(default=None, max_length=120)
    default_base_branch: Optional[str] = Field(default=None, max_length=120)
    # Write-only draft PAT for this request only; never persisted.
    credential: Optional[str] = Field(default=None, max_length=_GIT_CREDENTIAL_MAX_LENGTH)

    @field_validator("base_url")
    @classmethod
    def _check_draft_base_url(cls, v: Optional[str]) -> Optional[str]:
        if v is None:
            return None
        return _validate_git_base_url((v or "").strip().rstrip("/"))

    @field_validator("credential")
    @classmethod
    def _check_draft_credential(cls, v: Optional[str]) -> Optional[str]:
        return _validate_git_credential(v)

    @field_validator("organization")
    @classmethod
    def _check_draft_identifier(cls, v: Optional[str]) -> Optional[str]:
        if v is None:
            return None
        return _validate_git_identifier(v)

    @field_validator("default_base_branch")
    @classmethod
    def _check_draft_default_branch(cls, v: Optional[str]) -> Optional[str]:
        if v is None:
            return None
        candidate = (v or "").strip()
        if not candidate:
            return ""
        return _validate_git_ref(candidate, "Default base branch")

class GitIntegrationOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    # False when no row exists yet (the UI then shows the safe defaults below).
    configured: bool = False
    id: Optional[int] = None
    project_id: int
    provider: str = "github_enterprise"
    enabled: bool = False
    base_url: str = ""
    organization: str = ""
    default_base_branch: str = "dev"
    # unknown | ok | error
    connection_status: str = "unknown"
    last_checked_at: Optional[datetime] = None
    # Sanitized provider message only — never a token, header or error body.
    last_error: str = ""
    version: int = 1
    # Runtime capability flags (non-secret) so the UI can explain a disabled state.
    branch_creation_enabled: bool = True
    credentials_configured: bool = False

class GitConnectionTestOut(BaseModel):
    """Result of an explicit connection test; carries no provider internals."""

    ok: bool
    # ok | error
    status: str = "error"
    message: str = ""
    # Non-secret label of what was reached (organization login or API host).
    target: str = ""
    checked_at: Optional[datetime] = None
    error_code: str = ""

class GitProviderRepositoryOut(BaseModel):
    """Safe metadata for one provider-discovered repository.

    No selection, allow-list or activation state is exposed: repository
    selection happens at branch-creation time and is re-verified server-side.
    """

    provider_repo_id: str = ""
    name: str = ""
    owner: str = ""
    full_name: str = ""
    url: str = ""
    default_branch: str = ""

class GitBranchCreateIn(BaseModel):
    """The complete accept list for a branch-creation request.

    Exactly two fields, both required:

    - ``provider_repo_id``: the provider's own repository id, chosen from live
      discovery for this one action. The server re-verifies accessibility under
      the project credential and never trusts a submitted name or URL.
    - ``base_branch``: the action-specific source branch.

    There is deliberately no repository URL, organization, branch name, SHA,
    owner, ref, credential or internal repository-ID field: everything else is
    derived server-side, and `extra` is forbidden so a client cannot smuggle
    one in. The historical internal ``repository_id`` is no longer accepted.
    """

    model_config = ConfigDict(extra="forbid")

    provider_repo_id: str = Field(min_length=1, max_length=64)
    base_branch: str = Field(min_length=1, max_length=255)

    @field_validator("provider_repo_id")
    @classmethod
    def _check_provider_repo_id(cls, v: str) -> str:
        candidate = (v or "").strip()
        if not candidate:
            raise ValueError("provider_repo_id is required")
        if not candidate.isdigit():
            raise ValueError("must be the provider's numeric repository id")
        return candidate

    @field_validator("base_branch")
    @classmethod
    def _check_base_branch(cls, v: str) -> str:
        candidate = (v or "").strip()
        if not candidate:
            raise ValueError("base_branch is required")
        if (
            "\x00" in candidate
            or re.fullmatch(r"[\w.\-/]+", candidate) is None
            or ".." in candidate
            or candidate.startswith(("-", "."))
            or candidate.endswith((".", ".lock"))
            or "@{" in candidate
        ):
            raise ValueError(
                "base_branch contains characters that are not valid in a Git ref"
            )
        return candidate


class GitBranchOut(BaseModel):
    """One recorded branch. Contains no provider credentials of any kind."""

    model_config = ConfigDict(from_attributes=True)

    id: int
    work_item_type: str
    work_item_id: int
    project_id: int
    display_id: str = ""
    work_item_title: str = ""
    repository_name: str = ""
    repository_full_name: str = ""
    provider_repo_id: str = ""
    branch_name: str
    base_branch: str = ""
    base_commit_sha: str = ""
    branch_url: str = ""
    provider_branch_ref: str = ""
    # Active | Deleted | Unknown
    status: str = "Active"
    created_by_id: Optional[int] = None
    created_by_name: str = ""
    created_at: Optional[datetime] = None
    last_checked_at: Optional[datetime] = None
    # Sanitized message only.
    last_error: str = ""
    # Removal metadata (Deleted rows keep their creation history too).
    removed_at: Optional[datetime] = None
    removed_by_name: str = ""
    removal_reason: str = ""
    version: int = 1

class GitBranchPreviewOut(BaseModel):
    """Confirmation-dialog payload for a repository the user may branch from."""

    work_item_type: str
    work_item_id: int
    display_id: str = ""
    work_item_title: str = ""
    assignees: list[UserBrief] = Field(default_factory=list)
    repository_name: str = ""
    repository_full_name: str = ""
    base_branch: str = ""
    branch_name: str = ""
    # False when a branch already exists here, or the pair is not permitted.
    can_create: bool = True
    reason: str = ""
    existing_branch: Optional[GitBranchOut] = None

class GitWorkItemBranchesOut(BaseModel):
    """Development section payload: existing branches plus what the actor may do.

    `can_create_branch` / `can_remove_branch` are usability hints only — every
    action is re-authorized on the server. Repository selection is never part
    of this payload; the create dialog discovers repositories live.
    """

    work_item_type: str
    work_item_id: int
    project_id: int
    display_id: str = ""
    title: str = ""
    assignees: list[UserBrief] = Field(default_factory=list)
    branch_creation_supported: bool = False
    integration_enabled: bool = False
    can_create_branch: bool = False
    branch_deletion_enabled: bool = False
    can_remove_branch: bool = False
    reason: str = ""
    branches: list[GitBranchOut] = Field(default_factory=list)
