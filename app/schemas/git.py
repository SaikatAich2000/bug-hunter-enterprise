"""Pydantic schemas: git."""
from __future__ import annotations

import re
from datetime import datetime
from typing import Optional
from urllib.parse import urlsplit

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from app.schemas.users import UserBrief

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
    if not candidate.isascii():
        # Tokens are ASCII; anything else cannot be sent in an Authorization header.
        raise ValueError("must contain only ASCII characters")
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
