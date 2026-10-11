"""Pydantic schemas: users."""
from __future__ import annotations

from datetime import datetime
from typing import Optional

from pydantic import BaseModel, ConfigDict, Field, field_validator

from app.schemas.common import (
    ALLOWED_PROJECT_ROLES,
    ALLOWED_ROLES,
    MIN_NAME_LENGTH,
    MIN_ORG_NAME_LENGTH,
    _strip_and_check_min_length,
    _validate_email,
)


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


class VerifyEmailIn(BaseModel):
    token: str = Field(min_length=1, max_length=512)


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


class UserBrief(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: int
    name: str
    email: str
    role: str

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
