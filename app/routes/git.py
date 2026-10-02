"""Git branch API (GitHub Enterprise), mounted at /api/git.

Scope is exactly: project Git configuration, read-only repository discovery,
and one feature branch per persisted User Story per provider repository plus
the exact tracked removal of one such branch. There is deliberately no endpoint
for pull requests, merges, rebases, pipeline execution, commit tracking or
repository selection: repository selection happens only while creating a branch
from a persisted User Story.

Authorization lives here, never in the browser: the UI's "Create Feature
Branch" visibility is a usability aid, and every request re-checks project
access, the assignee rule and the Project Admin override server-side. Branch
removal is a project-level action available to every project member (the user
role included) and is never implied by project visibility alone.
"""
from __future__ import annotations

import logging
import re
from datetime import datetime, timezone
from typing import Optional

from fastapi import APIRouter, Body, Depends, HTTPException, Query, Response, status
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.access import accessible_project_ids, can_access_project
from app.api_docs import (
    FORBIDDEN_NOT_FOUND_CONFLICT_VALIDATION_503,
    GIT_REMOVE_FORBIDDEN_NOT_FOUND_CONFLICT_VALIDATION_422,
    NOT_FOUND_404,
)
from app.api_docs import (
    GIT_BRANCH_CONFLICT_NOT_FOUND_VALIDATION_422 as _DISCOVERY_RESPONSES,
)
from app.api_docs import GIT_CONFIG_CONFLICT_NOT_FOUND_VALIDATION_422 as _CONFIG_RESPONSES
from app.api_docs import GIT_REMOVE_FORBIDDEN_NOT_FOUND_CONFLICT_VALIDATION_422 as _REMOVE_RESPONSES
from app.auth import get_current_user, require_manager_or_admin
from app.config import get_settings
from app.database import get_db
from app.git import branches as branch_svc
from app.git import credentials as credential_svc
from app.git.branches import BranchCreationError, BranchTarget
from app.git.github import GitHubEnterpriseProvider
from app.git.naming import (
    display_id_for_branch,
    is_valid_branch_name,
    supports_branch_creation,
)
from app.git.provider import (
    AUTH_FAILED,
    BASE_BRANCH_MISSING,
    BRANCH_EXISTS,
    DNS_ERROR,
    INVALID_BASE_BRANCH,
    INVALID_BASE_URL,
    INVALID_RESPONSE,
    NOT_CONFIGURED,
    NOT_FOUND,
    PERMISSION_DENIED,
    PROXY_ERROR,
    RATE_LIMITED,
    TIMEOUT,
    TLS_TRUST_ERROR,
    UNAVAILABLE,
    GitProviderError,
    ProviderRepository,
    sanitize_provider_message,
)
from app.models import (
    ROLE_ADMIN,
    ROLE_MANAGER,
    Activity,
    Bug,
    Project,
    ProjectGitConfig,
    ProjectRepository,
    User,
    WorkItemBranch,
)
from app.schemas import (
    GitBranchCreateIn,
    GitBranchOut,
    GitBranchPreviewOut,
    GitConnectionDraftIn,
    GitConnectionTestOut,
    GitIntegrationIn,
    GitIntegrationOut,
    GitProviderRepositoryOut,
    GitWorkItemBranchesOut,
)

logger = logging.getLogger("bug_hunter.routes.git")

router = APIRouter(prefix="/api/git", tags=["git"])

_DETAIL_PROJECT_NOT_FOUND = "Project not found"
_DETAIL_WORK_ITEM_NOT_FOUND = "Work item not found"
_DETAIL_VERSION_CONFLICT = "The Git integration configuration changed; reload and retry"
_DETAIL_INTEGRATION_DISABLED = "Git integration is not enabled for this project"
_DETAIL_REPOSITORY_UNREACHABLE = (
    "Repository is not reachable through the configured Git integration"
)
# Story-only branch actions (preview/create/remove) share one stable refusal.
_STORY_ONLY_CODE = "user_story_only"
_STORY_ONLY_MESSAGE = "Feature branches can be created only for User Stories."
# Removal guards: a tracked feature branch may never collide with a long-lived
# collaboration branch, and the stored record must carry an exact heads ref.
_PROTECTED_BRANCH_NAMES = frozenset(
    {"main", "master", "dev", "develop", "release"}
)
_REMOVAL_REASON = "removed_by_user"
# The remote ref was already gone when the removal ran; the row is reconciled
# to Deleted instead of freshly deleted (never silently reported as a delete).
_REMOVAL_REASON_RECONCILED = "removed_by_user_reconciled_absent"
_VERSION_CONFLICT_MESSAGE = "The branch record changed; reload and retry"
# Discovery (the picker) and create-time verification must search the same
# repository window, or a repository the picker offered could fail to verify.
_DISCOVERY_LIMIT = 200

# Provider failure code -> HTTP status. Every provider failure is an expected,
# explainable outcome, so none of these is a 500.
_PROVIDER_STATUS: dict[str, int] = {
    AUTH_FAILED: status.HTTP_502_BAD_GATEWAY,
    PERMISSION_DENIED: status.HTTP_502_BAD_GATEWAY,
    NOT_FOUND: status.HTTP_404_NOT_FOUND,
    BASE_BRANCH_MISSING: status.HTTP_409_CONFLICT,
    INVALID_BASE_BRANCH: status.HTTP_422_UNPROCESSABLE_CONTENT,
    BRANCH_EXISTS: status.HTTP_409_CONFLICT,
    RATE_LIMITED: status.HTTP_503_SERVICE_UNAVAILABLE,
    TIMEOUT: status.HTTP_503_SERVICE_UNAVAILABLE,
    UNAVAILABLE: status.HTTP_503_SERVICE_UNAVAILABLE,
    NOT_CONFIGURED: status.HTTP_409_CONFLICT,
    INVALID_RESPONSE: status.HTTP_502_BAD_GATEWAY,
    # Transport failures are distinct so the operator sees the right cause.
    TLS_TRUST_ERROR: status.HTTP_502_BAD_GATEWAY,
    PROXY_ERROR: status.HTTP_503_SERVICE_UNAVAILABLE,
    DNS_ERROR: status.HTTP_503_SERVICE_UNAVAILABLE,
    # A stored base URL that cannot be used is a configuration problem, not an
    # upstream outage, and the Project Admin can fix it.
    INVALID_BASE_URL: status.HTTP_409_CONFLICT,
}

# Domain refusal code -> HTTP status.
_BRANCH_ERROR_STATUS: dict[str, int] = {
    branch_svc.UNSUPPORTED_ITEM_TYPE: status.HTTP_422_UNPROCESSABLE_CONTENT,
    branch_svc.REPOSITORY_MISMATCH: status.HTTP_404_NOT_FOUND,
    branch_svc.BASE_BRANCH_UNCONFIGURED: status.HTTP_409_CONFLICT,
    branch_svc.UNVERIFIED_BRANCH_NAME: status.HTTP_409_CONFLICT,
    branch_svc.DUPLICATE_BRANCH: status.HTTP_409_CONFLICT,
    branch_svc.BRANCH_ALREADY_REMOVED: status.HTTP_409_CONFLICT,
}

def _now() -> datetime:
    return datetime.now(timezone.utc).replace(microsecond=0)

def _repo_full_name(repository: ProjectRepository) -> str:
    if repository.owner:
        return f"{repository.owner}/{repository.name}"
    return repository.name

def _is_project_admin(user: User) -> bool:
    """The application's existing administrative roles act as Project Admin."""
    return user.role in (ROLE_ADMIN, ROLE_MANAGER)

def _audit(
    db: Session, actor: User | None, action: str, entity_id: int, detail: str,
    project_id: int | None = None,
) -> None:
    """One audit row; without an actor (system work) the organization comes from the project."""
    org_id = actor.org_id if actor is not None else db.scalar(
        select(Project.org_id).where(Project.id == project_id)
    )
    db.add(Activity(
        org_id=org_id, bug_id=None, entity_type="git_branch", entity_id=entity_id,
        actor_user_id=getattr(actor, "id", None),
        actor_name=getattr(actor, "name", "") or "system",
        action=action, detail=detail,
    ))

def _translate_provider_error(exc: GitProviderError) -> HTTPException:
    """Map a provider failure to an HTTP status; ``detail`` is already sanitized."""
    return HTTPException(
        status_code=_PROVIDER_STATUS.get(exc.code, status.HTTP_502_BAD_GATEWAY),
        detail=exc.message,
    )

def _get_project_or_404(db: Session, project_id: int, user: User) -> Project:
    project = db.get(Project, project_id)
    # 404 not 403: project scoping must never leak what exists.
    # Documented on every route via _CONFIG_RESPONSES/_DISCOVERY_RESPONSES
    # (Sonar S8415: this helper raises inside config/discovery/branch routes).
    if project is None or not can_access_project(
        accessible_project_ids(db, user), project_id
    ):
        raise HTTPException(status_code=404, detail=_DETAIL_PROJECT_NOT_FOUND)  # NOSONAR(S8415)
    return project

def _get_config_row(db: Session, project_id: int) -> ProjectGitConfig | None:
    return db.scalar(
        select(ProjectGitConfig).where(ProjectGitConfig.project_id == project_id)
    )

def build_provider(
    config: ProjectGitConfig | None,
    *,
    base_url: str | None = None,
    organization: str | None = None,
    token: str | None = None,
) -> GitHubEnterpriseProvider:
    """Construct the fully-resolved provider for one project.

    Configuration precedence (exactly):

    1. Explicit draft values (``base_url``/``organization`` arguments, used
       only by the Test Connection endpoint). ``None`` means no override;
       ``""`` is an explicit blank tested as-is, never replaced.
    2. Saved project values, whenever the row has a non-blank value.
    3. Environment defaults, only for blank legacy fields or a missing row.

    A saved non-empty base URL, organization or credential is NEVER replaced by an
    environment value. Credential precedence: explicit draft PAT, then the
    project's encrypted PAT, then the global ``GITHUB_TOKEN`` legacy fallback.

    The resolved token is passed to the provider explicitly, so this provider can
    only ever authenticate as THIS project. Tests replace this function to mock
    all provider calls.
    """
    settings = get_settings()
    # None = no override supplied; an explicit blank is tested as-is.
    if base_url is None:
        if config is not None and (config.base_url or "").strip():
            resolved_base_url = (config.base_url or "").strip()
        else:
            resolved_base_url = (settings.GITHUB_API_URL or "").strip()
    else:
        resolved_base_url = (base_url or "").strip()

    if organization is None:
        if config is not None and (config.organization or "").strip():
            resolved_organization = (config.organization or "").strip()
        else:
            resolved_organization = (settings.GITHUB_ORGANIZATION or "").strip()
    else:
        resolved_organization = (organization or "").strip()

    return GitHubEnterpriseProvider(
        base_url=resolved_base_url,
        organization=resolved_organization,
        token=_resolve_provider_token(config, token),
    )

def _resolve_provider_token(
    config: ProjectGitConfig | None, draft_token: str | None
) -> str | None:
    """Credential precedence: draft PAT, then project PAT, then global token.

    Raises a safe ``not_configured`` error when the project's stored credential
    cannot be decrypted, so a broken credential is never silently downgraded to
    the shared global token.
    """
    if draft_token and draft_token.strip():
        # An explicit draft PAT wins, and is never persisted by this path.
        return draft_token.strip()
    return credential_svc.resolve_token(config)

def _credentials_configured(config: ProjectGitConfig | None = None) -> bool:
    """True when a credential exists for this project (own PAT or fallback)."""
    return credential_svc.credentials_configured(config)

def _integration_state(
    db: Session, project_id: int
) -> tuple[ProjectGitConfig | None, bool]:
    """``(config_row, usable)`` — usable is False when disabled anywhere."""
    config = _get_config_row(db, project_id)
    if not get_settings().GIT_BRANCH_CREATION_ENABLED:
        return config, False
    return config, bool(config is not None and config.enabled)

# =====================================================================
# Serialization (secret-free by construction)
# =====================================================================

def _config_out(config: ProjectGitConfig) -> dict:
    settings = get_settings()
    return {
        "configured": True,
        "id": config.id,
        "project_id": config.project_id,
        "provider": config.provider,
        "enabled": config.enabled,
        "base_url": config.base_url,
        "organization": config.organization,
        "default_base_branch": config.default_base_branch,
        "connection_status": config.connection_status,
        "last_checked_at": config.last_checked_at,
        "last_error": config.last_error,
        "version": config.version,
        "branch_creation_enabled": settings.GIT_BRANCH_CREATION_ENABLED,
        # A boolean only. The credential itself is never serialized anywhere.
        "credentials_configured": _credentials_configured(config),
    }

def _config_defaults(project_id: int) -> dict:
    """Payload for a project with no Git row yet — no secrets, no surprises."""
    settings = get_settings()
    return {
        "configured": False,
        "project_id": project_id,
        "provider": "github_enterprise",
        "enabled": False,
        "base_url": settings.GITHUB_API_URL,
        "organization": settings.GITHUB_ORGANIZATION,
        "default_base_branch": settings.GITHUB_DEFAULT_BASE_BRANCH,
        "connection_status": "unknown",
        "version": 1,
        "branch_creation_enabled": settings.GIT_BRANCH_CREATION_ENABLED,
        # No project row yet, so only the legacy global token can be present.
        "credentials_configured": _credentials_configured(None),
    }

def _validate_base_branch(base_branch: str | None) -> str | None:
    """Trim and validate an action-specific base branch (never a stored value)."""
    base_branch = (base_branch or "").strip() or None
    if base_branch is not None and not re.fullmatch(r"[\w.\-/]+", base_branch):
        raise HTTPException(
            status_code=422,
            detail="Base branch contains characters that are not valid in a Git ref",
        )
    return base_branch

def _default_base_branch(config: ProjectGitConfig | None) -> str:
    return (
        (config.default_base_branch if config is not None else "")
        or get_settings().GITHUB_DEFAULT_BASE_BRANCH
    )

def _verified_provider_metadata(
    provider, provider_repo_id: str
) -> ProviderRepository:
    """Match a provider repository id against the provider's own listing.

    Nothing the browser says about the repository is trusted; the metadata must
    come from the provider under the project credential.
    """
    for candidate in provider.list_repositories(limit=_DISCOVERY_LIMIT):
        if candidate.provider_repo_id == provider_repo_id:
            return candidate
    raise HTTPException(status_code=404, detail=_DETAIL_REPOSITORY_UNREACHABLE)

def _repository_identity_from_provider(
    db: Session,
    *,
    project_id: int,
    metadata: ProviderRepository,
    config: ProjectGitConfig | None,
) -> ProjectRepository:
    """Find-or-create the internal branch-history identity for one provider repo.

    ``project_repositories`` is NOT an allow-list: it exists only so branch
    records keep a stable internal identity. The stable key is
    ``(project_id, provider, provider_repo_id)``. An existing row is kept (its
    id and branch history are preserved) and only safe metadata is refreshed;
    one legacy name-matched row without a provider id is reconciled. Concurrent
    inserts are resolved by re-reading the winner.
    """
    provider_name = (
        (config.provider if config is not None else "") or "github_enterprise"
    )
    repository = db.scalar(
        select(ProjectRepository).where(
            ProjectRepository.project_id == project_id,
            ProjectRepository.provider == provider_name,
            ProjectRepository.provider_repo_id == metadata.provider_repo_id,
        )
    )
    if repository is None:
        # Legacy reconciliation: one row matched on verified owner/name that
        # never carried a provider id adopts the provider identity.
        legacy = db.scalar(
            select(ProjectRepository).where(
                ProjectRepository.project_id == project_id,
                ProjectRepository.provider_repo_id == "",
                func.lower(ProjectRepository.name) == metadata.name.lower(),
                ProjectRepository.owner.in_(
                    [metadata.owner, ""] if metadata.owner else [""]
                ),
            ).order_by(ProjectRepository.id)
        )
        if legacy is not None:
            legacy.provider = provider_name
            legacy.provider_repo_id = metadata.provider_repo_id
            repository = legacy
    if repository is None:
        repository = ProjectRepository(
            project_id=project_id,
            provider=provider_name,
            provider_repo_id=metadata.provider_repo_id,
            name=metadata.name,
            owner=metadata.owner,
            url=metadata.url,
            default_base_branch=(
                (config.default_base_branch if config is not None else "")
                or metadata.default_branch
                or get_settings().GITHUB_DEFAULT_BASE_BRANCH
            ),
            active=True,
            branch_creation_enabled=True,
            last_checked_at=_now(),
        )
        db.add(repository)
        try:
            db.flush()
        except IntegrityError:
            # A concurrent request created the same identity first; its row wins.
            db.rollback()
            repository = db.scalar(
                select(ProjectRepository).where(
                    ProjectRepository.project_id == project_id,
                    ProjectRepository.provider == provider_name,
                    ProjectRepository.provider_repo_id == metadata.provider_repo_id,
                )
            )
            if repository is None:
                raise
            return repository
        _audit(
            db, None, "git_repository_identity_created", repository.id,
            f"project={project_id} repository={metadata.full_name} "
            f"provider_repo_id={metadata.provider_repo_id}",
            project_id=project_id,
        )
        return repository
    # Safe metadata refresh only — a renamed repository keeps its row id and
    # every branch-history reference.
    changed = []
    if (repository.name or "") != metadata.name:
        changed.append(f"name: {repository.name!r} -> {metadata.name!r}")
        repository.name = metadata.name
    if metadata.owner and (repository.owner or "") != metadata.owner:
        changed.append(f"owner: {repository.owner!r} -> {metadata.owner!r}")
        repository.owner = metadata.owner
    if metadata.url and (repository.url or "") != metadata.url:
        repository.url = metadata.url
    repository.last_checked_at = _now()
    if changed:
        _audit(
            db, None, "git_repository_identity_updated", repository.id,
            f"project={project_id} provider_repo_id={metadata.provider_repo_id} "
            + "; ".join(changed),
            project_id=project_id,
        )
    return repository

def _resolve_creation_repository(
    db: Session,
    project_id: int,
    payload: GitBranchCreateIn,
    config: ProjectGitConfig | None,
) -> tuple[ProjectRepository, str | None]:
    """Resolve the verified provider repository for a confirmed creation.

    Only the provider repository id is accepted — the historical internal
    ``repository_id`` is never a client input. The default base branch for the
    action comes from the project configuration.
    """
    base_branch = _validate_base_branch(payload.base_branch)
    if not get_settings().GIT_BRANCH_CREATION_ENABLED or config is None:
        raise HTTPException(status_code=404, detail=_DETAIL_REPOSITORY_UNREACHABLE)
    provider = build_provider(config)
    try:
        metadata = _verified_provider_metadata(
            provider, (payload.provider_repo_id or "").strip()
        )
        repository = _repository_identity_from_provider(
            db, project_id=project_id, metadata=metadata, config=config
        )
    except GitProviderError as exc:
        raise _translate_provider_error(exc) from exc
    return repository, base_branch

def _branch_out(row: WorkItemBranch, *, display_id: str, title: str) -> dict:
    repository = row.repository
    return {
        "id": row.id,
        "work_item_type": row.work_item_type,
        "work_item_id": row.work_item_id,
        "project_id": row.project_id,
        "display_id": display_id,
        "work_item_title": title,
        "repository_name": repository.name if repository is not None else "",
        "repository_full_name": (
            _repo_full_name(repository) if repository is not None else ""
        ),
        "provider_repo_id": row.provider_repo_id,
        "branch_name": row.branch_name,
        "base_branch": row.base_branch,
        "base_commit_sha": row.base_commit_sha,
        "branch_url": row.branch_url,
        "provider_branch_ref": row.provider_branch_ref,
        "status": row.status,
        "created_by_id": row.created_by_id,
        "created_by_name": row.created_by_name,
        "created_at": row.created_at,
        "last_checked_at": row.last_checked_at,
        "last_error": row.last_error,
        # Removal metadata: safe, human-facing fields only. The internal
        # repository row id is never exposed.
        "removed_at": row.removed_at,
        "removed_by_name": row.removed_by_name,
        "removal_reason": row.removal_reason,
        "version": row.version,
    }

# =====================================================================
# 1+2. Project Git configuration (Project Admin only)
# =====================================================================

_CONFIG_FIELDS = (
    "enabled",
    "base_url",
    "organization",
    "default_base_branch",
)

@router.get("/projects/{project_id}/config", response_model=GitIntegrationOut,
            responses=_CONFIG_RESPONSES)
def get_git_config(
    project_id: int,
    db: Session = Depends(get_db),
    actor: User = Depends(require_manager_or_admin),
) -> dict:
    """Non-secret configuration only; credentials are never returned."""
    _get_project_or_404(db, project_id, actor)
    config = _get_config_row(db, project_id)
    return _config_out(config) if config is not None else _config_defaults(project_id)

def _apply_credential_operation(
    config: ProjectGitConfig, payload: GitIntegrationIn
) -> str:
    """Apply the credential part of a PUT; return the audit phrase (never a token).

    Semantics:

    * ``clear_credential=True`` removes the stored project credential (idempotent:
      an already-empty credential is simply kept empty).
    * A non-blank ``credential`` replaces the stored project credential.
    * Both blank/False means "preserve whatever is stored" — the frontend's
      password input is cleared after every submit, so a blank field can never
      wipe an existing credential.

    The plaintext PAT is encrypted immediately and is never logged, audited or
    written to the database in the clear.
    """
    if payload.clear_credential:
        if not (config.credential_encrypted or "").strip():
            return ""
        config.credential_encrypted = ""
        return "credential cleared"
    if not payload.credential:
        # Empty credential field: preserve the current credential.
        return ""
    if not credential_svc.encryption_available():
        # No key: legacy global-token operation continues, but a project
        # credential cannot be stored. Fail closed, with a safe explanation.
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=credential_svc.ENCRYPTION_KEY_MISSING_MESSAGE,
        )
    replaced = bool((config.credential_encrypted or "").strip())
    try:
        config.credential_encrypted = credential_svc.encrypt_credential(
            payload.credential
        )
    except credential_svc.CredentialError as exc:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT, detail=exc.message
        ) from None
    return "credential replaced" if replaced else "credential configured"

@router.put("/projects/{project_id}/config", response_model=GitIntegrationOut,
            responses=_CONFIG_RESPONSES)
def update_git_config(
    project_id: int,
    payload: GitIntegrationIn,
    db: Session = Depends(get_db),
    actor: User = Depends(require_manager_or_admin),
) -> dict:
    """Create or update the project's Git configuration (upsert).

    Accepts an optional write-only ``credential`` and the explicit boolean
    ``clear_credential``. The response never contains a credential field.
    """
    project = _get_project_or_404(db, project_id, actor)
    config = _get_config_row(db, project_id)
    if config is None:
        if payload.version is not None:
            raise HTTPException(status_code=409, detail=_DETAIL_VERSION_CONFLICT)
        config = ProjectGitConfig(project_id=project.id)
        db.add(config)
        db.flush()
    elif payload.version is not None and payload.version != config.version:
        raise HTTPException(status_code=409, detail=_DETAIL_VERSION_CONFLICT)

    changes: list[str] = []
    for field in _CONFIG_FIELDS:
        value = getattr(payload, field)
        previous = getattr(config, field)
        if previous != value:
            changes.append(f"{field}: {previous!r} -> {value!r}")
            setattr(config, field, value)
    # Credential last: a rejected credential must not leave a half-applied edit.
    credential_note = _apply_credential_operation(config, payload)
    config.version += 1
    # Any configuration edit invalidates the previously observed connection state.
    config.connection_status = "unknown"
    config.last_error = ""
    db.flush()
    # The audit trail records WHAT happened to the credential, never its value.
    if credential_note:
        changes.append(credential_note)
        _audit(
            db, actor, f"git_{credential_note.replace(' ', '_')}", config.id,
            f"Git credential {credential_note.split(' ', 1)[1]} for project "
            f"'{project.name}'",
        )
    _audit(
        db, actor, "git_config_updated", config.id,
        f"Updated Git integration for project '{project.name}': "
        + ("; ".join(changes) or "no changes"),
    )
    db.commit()
    db.refresh(config)
    return _config_out(config)

def _record_connection_result(
    db: Session, config: ProjectGitConfig | None, *, ok: bool, message: str
) -> None:
    if config is None:
        return
    config.connection_status = "ok" if ok else "error"
    config.last_checked_at = _now()
    config.last_error = "" if ok else sanitize_provider_message(message)
    config.version += 1
    db.flush()

@router.post("/projects/{project_id}/config/test", response_model=GitConnectionTestOut, responses=NOT_FOUND_404)
def test_git_connection(
    project_id: int,
    draft: GitConnectionDraftIn | None = Body(default=None),
    db: Session = Depends(get_db),
    actor: User = Depends(require_manager_or_admin),
) -> dict:
    """Explicit connection check. Always 200; `ok` carries the verdict.

    Two distinct paths:

    * **Draft test** (a body was supplied): the visible draft is tested exactly
      as submitted. ``None`` means "no override" (saved, then env default);
      ``""`` is an explicit blank tested as-is and NEVER replaced. NOTHING is
      persisted: no status, error, timestamp, version, config or credential
      change, even when the draft equals the saved row (a body may carry an
      unsaved PAT). A draft PAT is request-only and never stored.
    * **Saved test** (no body): the stored configuration is tested and its
      ``connection_status`` recorded. Only this path persists.
    """
    project = _get_project_or_404(db, project_id, actor)
    config = _get_config_row(db, project_id)
    settings = get_settings()
    if draft is None:
        provider = build_provider(config)
        checked_at = _now()
        try:
            target = provider.validate_connection()
        except GitProviderError as exc:
            _record_connection_result(db, config, ok=False, message=exc.message)
            _audit(
                db, actor, "git_connection_test_failed",
                config.id if config is not None else project.id,
                "Connection test failed for project "
                f"'{project.name}' ({exc.code}): {exc.message}",
            )
            db.commit()
            return {
                "ok": False, "status": "error", "message": exc.message,
                "target": "", "checked_at": checked_at, "error_code": exc.code,
            }
        _record_connection_result(db, config, ok=True, message="")
        _audit(
            db, actor, "git_connection_tested",
            config.id if config is not None else project.id,
            "Connection test succeeded for project "
            f"'{project.name}' (target={target})",
        )
        db.commit()
        return {
            "ok": True, "status": "ok", "message": f"Connected to {target}",
            "target": target, "checked_at": checked_at, "error_code": "",
        }
    draft_base = None if draft.base_url is None else (draft.base_url or "").strip()
    draft_org = None if draft.organization is None else (draft.organization or "").strip()
    if draft.default_base_branch is None:
        if config is not None and (config.default_base_branch or "").strip():
            draft_branch = (config.default_base_branch or "").strip()
        else:
            draft_branch = (settings.GITHUB_DEFAULT_BASE_BRANCH or "").strip()
    else:
        draft_branch = (draft.default_base_branch or "").strip()
    if draft.credential:
        draft_token: str | None = draft.credential
    elif config is not None and credential_svc.project_credential_stored(config):
        try:
            draft_token = credential_svc.decrypt_credential(config.credential_encrypted)
        except credential_svc.CredentialError as exc:
            _audit(
                db, actor, "git_connection_test_failed",
                config.id if config is not None else project.id,
                "Connection test failed for project "
                f"'{project.name}' ({NOT_CONFIGURED}): {exc.message} "
                "[draft values were not saved]",
            )
            db.commit()
            return {
                "ok": False, "status": "error", "message": exc.message,
                "target": "", "checked_at": _now(), "error_code": NOT_CONFIGURED,
            }
    else:
        draft_token = (settings.GITHUB_TOKEN or "").strip() or None
    if not draft_base:
        _audit(
            db, actor, "git_connection_test_failed",
            config.id if config is not None else project.id,
            "Connection test failed for project "
            f"'{project.name}' ({INVALID_BASE_URL}): blank base URL "
            "[draft values were not saved]",
        )
        db.commit()
        return {
            "ok": False, "status": "error", "message": "Git base URL is required",
            "target": "", "checked_at": _now(), "error_code": INVALID_BASE_URL,
        }
    if not draft_branch:
        _audit(
            db, actor, "git_connection_test_failed",
            config.id if config is not None else project.id,
            "Connection test failed for project "
            f"'{project.name}' ({INVALID_BASE_BRANCH}): blank base branch "
            "[draft values were not saved]",
        )
        db.commit()
        return {
            "ok": False, "status": "error",
            "message": "Default base branch is required",
            "target": "", "checked_at": _now(), "error_code": INVALID_BASE_BRANCH,
        }
    if draft_branch and not is_valid_branch_name(draft_branch):
        _audit(
            db, actor, "git_connection_test_failed",
            config.id if config is not None else project.id,
            "Connection test failed for project "
            f"'{project.name}' ({INVALID_BASE_BRANCH}): invalid base branch "
            "[draft values were not saved]",
        )
        db.commit()
        return {
            "ok": False, "status": "error",
            "message": "Default base branch is not a valid git branch name",
            "target": "", "checked_at": _now(),
            "error_code": INVALID_BASE_BRANCH,
        }
    provider = build_provider(
        config, base_url=draft_base, organization=draft_org, token=draft_token,
    )
    checked_at = _now()
    try:
        target = provider.validate_connection()
    except GitProviderError as exc:
        _audit(
            db, actor, "git_connection_test_failed",
            config.id if config is not None else project.id,
            "Connection test failed for project "
            f"'{project.name}' ({exc.code}): {exc.message} "
            "[draft values were not saved]",
        )
        db.commit()
        return {
            "ok": False, "status": "error", "message": exc.message,
            "target": "", "checked_at": checked_at, "error_code": exc.code,
        }
    _audit(
        db, actor, "git_connection_tested",
        config.id if config is not None else project.id,
        "Connection test succeeded for project "
        f"'{project.name}' (target={target}) [draft values were not saved]",
    )
    db.commit()
    return {
        "ok": True, "status": "ok", "message": f"Connected to {target}",
        "target": target, "checked_at": checked_at, "error_code": "",
    }

# =====================================================================
# 3. Read-only repository discovery (no selection, no persistence)
# =====================================================================

@router.get(
    "/projects/{project_id}/available-repositories",
    response_model=list[GitProviderRepositoryOut],
    responses=_DISCOVERY_RESPONSES,
)
def list_available_repositories(
    project_id: int,
    db: Session = Depends(get_db),
    actor: User = Depends(get_current_user),
) -> list[dict]:
    """Repositories the provider reports as reachable with the project credential.

    Readable by any project member: the branch-create dialog needs the picker.
    Creating a branch is authorized separately on the create endpoint. Only
    safe metadata is returned — never selection state, allow-list membership or
    an internal identity row id. The only write is the audit row recording the
    lookup: no configuration change and no repository-history row.
    """
    project = _get_project_or_404(db, project_id, actor)
    config, enabled = _integration_state(db, project.id)
    if not enabled:
        raise HTTPException(status_code=409, detail=_DETAIL_INTEGRATION_DISABLED)
    provider = build_provider(config)
    try:
        discovered = provider.list_repositories(limit=_DISCOVERY_LIMIT)
    except GitProviderError as exc:
        _audit(
            db, actor, "git_repositories_discovery_failed", project.id,
            f"project={project.id} error={exc.code} result=failed",
        )
        db.commit()
        raise _translate_provider_error(exc) from exc
    discovered = discovered[:_DISCOVERY_LIMIT]
    _audit(
        db, actor, "git_repositories_discovered", project.id,
        f"project={project.id} result=ok count={len(discovered)}",
    )
    db.commit()
    return [
        {
            "provider_repo_id": repository.provider_repo_id,
            "name": repository.name,
            "owner": repository.owner,
            "full_name": repository.full_name,
            "url": repository.url,
            "default_branch": repository.default_branch,
        }
        for repository in discovered
    ]

# =====================================================================
# 4-9. Branch creation for a work item
# =====================================================================

def _assignee_briefs(db: Session, target: BranchTarget) -> list[dict]:
    if not target.assignee_ids:
        return []
    rows = db.scalars(
        select(User).where(User.id.in_(sorted(target.assignee_ids))).order_by(User.id)
    ).all()
    return [
        {"id": row.id, "name": row.name, "email": row.email, "role": row.role}
        for row in rows
    ]

def _bug_target(db: Session, item_id: int, user: User) -> BranchTarget:
    """Resolve a `bugs` row (Bug/Requirement/Task/Sub-task/Story/Epic)."""
    bug = db.get(Bug, item_id)
    if bug is None or not can_access_project(
        accessible_project_ids(db, user), bug.project_id
    ):
        raise HTTPException(status_code=404, detail=_DETAIL_WORK_ITEM_NOT_FOUND)
    item_type = bug.item_type or "Bug"
    return BranchTarget(
        item_type=item_type,
        item_id=bug.id,
        project_id=bug.project_id,
        title=bug.title,
        display_id=display_id_for_branch(item_type, bug.id, bug.display_id),
        assignee_ids=frozenset(assignee.id for assignee in bug.assignees),
    )

def _require_story(target: BranchTarget) -> None:
    """Story-only gate for every branch action, before any provider work."""
    if not supports_branch_creation(target.item_type):
        raise HTTPException(
            status_code=422,
            detail=_STORY_ONLY_MESSAGE,
            headers={"X-Error-Code": _STORY_ONLY_CODE},
        )

def _assert_can_create_branch(target: BranchTarget, actor: User) -> None:
    """The assignee-or-Project-Admin rule. The ONLY authorization for creation.

    A reporter, viewer, generic project member or unrelated user does not qualify
    merely because they can view or edit the item.
    """
    if not actor.is_active:
        raise HTTPException(
            status_code=403, detail="A deactivated user cannot create a Git branch"
        )
    if _is_project_admin(actor):
        return
    if actor.id not in target.assignee_ids:
        raise HTTPException(
            status_code=403,
            detail=(
                "Only an assignee of this work item or a project administrator "
                "can create its Git branch"
            ),
        )

def _creation_permission(
    target: BranchTarget, actor: User, *, integration_enabled: bool
) -> tuple[bool, str]:
    """Non-authoritative hint for the UI, plus the reason when it is False."""
    if not supports_branch_creation(target.item_type):
        return False, _STORY_ONLY_MESSAGE
    if not get_settings().GIT_BRANCH_CREATION_ENABLED:
        return False, "Branch creation is disabled for this deployment"
    if not integration_enabled:
        return False, _DETAIL_INTEGRATION_DISABLED
    if not actor.is_active:
        return False, "Your account is deactivated"
    if _is_project_admin(actor) or actor.id in target.assignee_ids:
        return True, ""
    return False, (
        "Only an assignee of this work item or a project administrator can create "
        "its Git branch"
    )

def _work_item_branches_payload(
    db: Session, target: BranchTarget, actor: User
) -> dict:
    """Development section: existing branches plus what the actor may do.

    Branch history is listed for every work-item type; branch ACTIONS are
    Story-only and every action is re-authorized server-side. The internal
    repository row id is never exposed; repositories are discovered live by the
    create dialog instead of being listed here.
    """
    _config, enabled = _integration_state(db, target.project_id)
    supported = supports_branch_creation(target.item_type)
    branches: list[dict] = []
    rows = db.scalars(
        select(WorkItemBranch)
        .where(
            WorkItemBranch.work_item_type == target.item_type,
            WorkItemBranch.work_item_id == target.item_id,
        )
        .order_by(WorkItemBranch.id)
    ).all()
    branches = [
        _branch_out(row, display_id=target.display_id, title=target.title)
        for row in rows
    ]
    can_create, reason = _creation_permission(
        target, actor, integration_enabled=enabled
    )
    deletion_enabled = bool(
        get_settings().GIT_BRANCH_DELETION_ENABLED and supported and enabled
    )
    can_remove = bool(deletion_enabled)
    return {
        "work_item_type": target.item_type,
        "work_item_id": target.item_id,
        "project_id": target.project_id,
        "display_id": target.display_id,
        "title": target.title,
        "assignees": _assignee_briefs(db, target),
        "branch_creation_supported": supported,
        "integration_enabled": enabled,
        "can_create_branch": can_create,
        "reason": reason,
        "branch_deletion_enabled": deletion_enabled,
        "can_remove_branch": can_remove,
        "branches": branches,
    }

def _preview_payload(
    db: Session,
    target: BranchTarget,
    actor: User,
    *,
    provider_repo_id: str,
    base_branch: str | None,
) -> dict:
    """Confirmation-dialog data for one provider repository.

    Read-only by contract: provider reads are allowed, but nothing is written —
    no ProjectRepository identity row, no WorkItemBranch, no flush, no audit.
    """
    _require_story(target)
    _assert_can_create_branch(target, actor)
    config, enabled = _integration_state(db, target.project_id)
    if not enabled:
        raise HTTPException(status_code=409, detail=_DETAIL_INTEGRATION_DISABLED)
    provider = build_provider(config)
    try:
        metadata = _verified_provider_metadata(provider, provider_repo_id.strip())
    except GitProviderError as exc:
        raise _translate_provider_error(exc) from exc

    payload = {
        "work_item_type": target.item_type,
        "work_item_id": target.item_id,
        "display_id": target.display_id,
        "work_item_title": target.title,
        "assignees": _assignee_briefs(db, target),
        "repository_name": metadata.name,
        "repository_full_name": metadata.full_name,
        "base_branch": base_branch.strip() if base_branch else _default_base_branch(config),
        "branch_name": "",
        "can_create": True,
        "reason": "",
        "existing_branch": None,
    }
    existing = branch_svc.find_existing_branch_by_provider(
        db, target=target, provider_repo_id=metadata.provider_repo_id
    )
    if existing is not None:
        # Duplicates are prevented: the dialog shows the existing branch instead.
        payload["can_create"] = False
        payload["reason"] = (
            "A branch already exists for this work item in this repository"
        )
        payload["branch_name"] = existing.branch_name
        payload["base_branch"] = existing.base_branch or payload["base_branch"]
        payload["existing_branch"] = _branch_out(
            existing, display_id=target.display_id, title=target.title
        )
        return payload
    # Pure reads below: base-branch verification plus deterministic naming.
    try:
        branch_name, base_branch, _display_id = branch_svc.preview_branch_name(
            target, base_branch, default_base_branch=_default_base_branch(config)
        )
        payload["base_branch"] = base_branch
        if not provider.get_branch_sha(metadata.owner, metadata.name, base_branch):
            payload["can_create"] = False
            payload["reason"] = (
                f"Base branch '{base_branch}' was not found in {metadata.full_name}"
            )
            return payload
        payload["branch_name"] = branch_name
    except BranchCreationError as exc:
        payload["can_create"] = False
        payload["reason"] = exc.message
        return payload
    return payload

def _audit_branch_success(
    db: Session, actor: User, target: BranchTarget, row: WorkItemBranch,
    repository: ProjectRepository, *, created: bool,
) -> None:
    """Audit record: project, work item (numeric + display id), repository, branch,
    base branch, base SHA, actor and result. Never a credential."""
    _audit(
        db, actor, "git_branch_created" if created else "git_branch_reused", row.id,
        f"project={target.project_id} work_item={target.item_type}#{target.item_id} "
        f"display_id={target.display_id} repository_ref={repository.id} "
        f"repository={_repo_full_name(repository)} branch={row.branch_name} "
        f"base={row.base_branch} sha={(row.base_commit_sha or '')[:12]} "
        f"actor={actor.id} result={'created' if created else 'existing'}",
    )

def _audit_branch_failure(
    db: Session, actor: User, target: BranchTarget, message: str
) -> None:
    """Sanitized failure audit entry (no provider internals)."""
    _audit(
        db, actor, "git_branch_creation_failed", target.item_id,
        f"project={target.project_id} work_item={target.item_type}#{target.item_id} "
        f"display_id={target.display_id} actor={actor.id} result=failed "
        f"error={sanitize_provider_message(message)}",
    )

def _create_branch(
    db: Session, target: BranchTarget, payload: GitBranchCreateIn, actor: User
) -> tuple[dict, bool]:
    """Run the create/reconcile flow. Returns ``(payload, created)``.

    The transaction is committed here because an idempotent replay must be able
    to read back what a concurrent request already wrote — on both the
    IntegrityError path and the "remote branch already exists" path.
    """
    _require_story(target)
    _assert_can_create_branch(target, actor)
    config, enabled = _integration_state(db, target.project_id)
    if not enabled:
        _audit_branch_failure(db, actor, target, _DETAIL_INTEGRATION_DISABLED)
        db.commit()
        raise HTTPException(status_code=409, detail=_DETAIL_INTEGRATION_DISABLED)
    repository, base_branch = _resolve_creation_repository(
        db, target.project_id, payload, config
    )

    provider = build_provider(config)
    try:
        row, created = branch_svc.create_branch(
            db, provider, target=target, repository=repository, actor=actor,
            base_branch_override=base_branch,
        )
    except BranchCreationError as exc:
        _audit_branch_failure(db, actor, target, exc.message)
        db.commit()
        raise HTTPException(
            status_code=_BRANCH_ERROR_STATUS.get(
                exc.code, status.HTTP_409_CONFLICT
            ),
            detail=exc.message,
        ) from exc
    except GitProviderError as exc:
        # No row is written for a failed creation; only an existing record's
        # last_error is updated. Remote state is re-checked on the next attempt.
        branch_svc.record_failure(db, target=target, repository=repository, error=exc)
        _audit_branch_failure(db, actor, target, exc.message)
        db.commit()
        raise _translate_provider_error(exc) from exc
    except IntegrityError as err:
        # A concurrent request inserted the same row first: its row is canonical.
        db.rollback()
        existing = branch_svc.find_existing_branch(
            db, target=target, repository=repository
        )
        if existing is None:
            _audit_branch_failure(
                db, actor, target, "The generated branch record could not be saved"
            )
            db.commit()
            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                detail=(
                    "The branch record could not be saved; check the branch in "
                    "GitHub and retry"
                ),
            ) from err
        row, created = existing, False

    _audit_branch_success(db, actor, target, row, repository, created=created)
    db.commit()
    db.refresh(row)
    return _branch_out(row, display_id=target.display_id, title=target.title), created

def _list_route(db: Session, target: BranchTarget, actor: User) -> dict:
    return _work_item_branches_payload(db, target, actor)

def _preview_route(
    db: Session,
    target: BranchTarget,
    actor: User,
    *,
    provider_repo_id: str,
    base_branch: str | None,
) -> dict:
    return _preview_payload(
        db, target, actor,
        provider_repo_id=provider_repo_id, base_branch=base_branch,
    )

def _remove_branch_record(
    db: Session, branch_record_id: int, expected_version: int | None, actor: User
) -> WorkItemBranch:
    """Remove exactly one tracked feature branch (project-member action).

    Every destructive field is derived from the stored record: the stored
    ``refs/heads/<name>`` ref, the stored repository identity and the stored
    project. The browser supplies only the record id (and its expected
    version); it can never steer what is deleted.
    """
    row = db.get(WorkItemBranch, branch_record_id)
    # Scoped 404: a record of another project is simply invisible. This is the
    # whole authorization: removal is a project-level action available to any
    # project member (the user role included); outsiders get the same 404.
    if row is None or not can_access_project(
        accessible_project_ids(db, actor), row.project_id
    ):
        raise HTTPException(status_code=404, detail="Branch record was not found")
    if not get_settings().GIT_BRANCH_DELETION_ENABLED:
        raise HTTPException(
            status_code=403,
            detail="Remote branch removal is disabled for this deployment",
        )
    if expected_version is not None and expected_version != row.version:
        raise HTTPException(
            status_code=409,
            detail=_VERSION_CONFLICT_MESSAGE,
            headers={"X-Error-Code": "version_conflict"},
        )
    if not supports_branch_creation(row.work_item_type):
        raise HTTPException(
            status_code=422,
            detail=_STORY_ONLY_MESSAGE,
            headers={"X-Error-Code": _STORY_ONLY_CODE},
        )
    if row.status == branch_svc.STATUS_DELETED:
        raise HTTPException(
            status_code=409, detail="This branch has already been removed"
        )
    ref = row.provider_branch_ref or ""
    if (
        not ref.startswith("refs/heads/")
        or "*" in ref
        or ref != f"refs/heads/{row.branch_name}"
        or not row.branch_name.startswith("feature_")
    ):
        raise HTTPException(
            status_code=409,
            detail="The stored branch record has no exact provider ref; "
            "it cannot be removed safely",
        )
    short_name = row.branch_name
    if (
        short_name.lower() in _PROTECTED_BRANCH_NAMES
        or short_name == (row.base_branch or "")
        or short_name == _default_base_branch(_get_config_row(db, row.project_id))
    ):
        raise HTTPException(
            status_code=409,
            detail="A base or protected branch cannot be removed here",
        )
    repository = None
    if row.repository_id is not None:
        repository = db.get(ProjectRepository, row.repository_id)
    if (
        repository is None
        or not (row.provider_repo_id or repository.provider_repo_id or "").strip()
        or repository.project_id != row.project_id
    ):
        raise HTTPException(
            status_code=409,
            detail="The repository identity for this branch is no longer available; "
            "it cannot be removed safely",
        )

    config = _get_config_row(db, row.project_id)
    provider = build_provider(config)
    try:
        already_absent = not provider.delete_branch(repository.owner, repository.name, ref)
    except GitProviderError as exc:
        if exc.code != NOT_FOUND:
            # Auth/rate-limit/TLS/timeout/5xx: the record stays Active with a
            # sanitized error so a safe retry remains possible.
            row.last_error = sanitize_provider_message(exc.message)
            row.last_checked_at = _now()
            db.commit()
            raise _translate_provider_error(exc) from exc
        # Exact 404 from a provider that raises instead of returning False:
        # the remote branch is already absent — treat as removed.
        already_absent = True
    row.status = branch_svc.STATUS_DELETED
    row.version += 1
    row.removed_at = _now()
    row.removed_by_id = actor.id
    row.removed_by_name = actor.name
    row.removal_reason = (
        _REMOVAL_REASON_RECONCILED if already_absent else _REMOVAL_REASON
    )
    row.last_error = ""
    row.last_checked_at = _now()
    db.flush()
    _audit(
        db, actor, "git_branch_removed", row.id,
        f"project={row.project_id} work_item={row.work_item_type}#{row.work_item_id} "
        f"branch={row.branch_name} ref={row.provider_branch_ref} "
        f"repository={_repo_full_name(repository)} actor={actor.id} "
        f"result={'reconciled_absent' if already_absent else 'removed'}",
    )
    db.commit()
    db.refresh(row)
    return row

def _create_route(
    db: Session, target: BranchTarget, payload: GitBranchCreateIn,
    actor: User, response: Response,
) -> dict:
    body, created = _create_branch(db, target, payload, actor)
    # 201 on first creation, 200 when an existing record is returned — the
    # request was idempotent, not a new resource.
    response.status_code = (
        status.HTTP_201_CREATED if created else status.HTTP_200_OK
    )
    return body

# --- Bug-table work items (Bug / Requirement / Task / Sub-task / Story) ---

@router.get("/work-items/{work_item_id}/branches", response_model=GitWorkItemBranchesOut,
            responses=_DISCOVERY_RESPONSES)
def list_work_item_branches(
    work_item_id: int,
    db: Session = Depends(get_db),
    actor: User = Depends(get_current_user),
) -> dict:
    return _list_route(db, _bug_target(db, work_item_id, actor), actor)

@router.get(
    "/work-items/{work_item_id}/branches/preview", response_model=GitBranchPreviewOut,
    responses=GIT_REMOVE_FORBIDDEN_NOT_FOUND_CONFLICT_VALIDATION_422,
)
def preview_work_item_branch(
    work_item_id: int,
    provider_repo_id: str = Query(max_length=64),
    base_branch: Optional[str] = Query(default=None, max_length=255),
    db: Session = Depends(get_db),
    actor: User = Depends(get_current_user),
) -> dict:
    if not provider_repo_id.strip():
        raise HTTPException(status_code=422, detail="provider_repo_id is required")
    _validate_base_branch(base_branch)
    return _preview_route(
        db, _bug_target(db, work_item_id, actor), actor,
        provider_repo_id=provider_repo_id, base_branch=base_branch,
    )

@router.post("/work-items/{work_item_id}/branches", response_model=GitBranchOut,
             responses=FORBIDDEN_NOT_FOUND_CONFLICT_VALIDATION_503)
def create_work_item_branch(
    work_item_id: int,
    payload: GitBranchCreateIn,
    response: Response,
    db: Session = Depends(get_db),
    actor: User = Depends(get_current_user),
) -> dict:
    return _create_route(
        db, _bug_target(db, work_item_id, actor), payload, actor, response
    )

@router.delete("/branches/{branch_record_id}", response_model=GitBranchOut,
               responses=_REMOVE_RESPONSES)
def remove_branch_record(
    branch_record_id: int,
    expected_version: Optional[int] = Query(default=None, gt=0),
    db: Session = Depends(get_db),
    actor: User = Depends(get_current_user),
) -> dict:
    """Delete exactly one tracked remote branch; any project member may do it.

    Creation stays assignee-only; removal is a project-level cleanup action
    (user role included). The row is retained as Deleted history; creation
    metadata is preserved.
    """
    row = _remove_branch_record(db, branch_record_id, expected_version, actor)
    return _branch_out(
        row,
        display_id=display_id_for_branch(row.work_item_type, row.work_item_id),
        title="",
    )
