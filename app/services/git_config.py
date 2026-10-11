"""Project Git configuration: defaults, validation, credential changes."""
from __future__ import annotations

import re

from fastapi import HTTPException, status
from sqlalchemy import select
from sqlalchemy.orm import Session

from app import net_guard
from app.config import get_settings
from app.git import credentials as credential_svc
from app.git.github import HOST_NOT_ALLOWED_MESSAGE, parse_base_url
from app.models import (
    ProjectGitConfig,
)
from app.schemas import (
    GitIntegrationIn,
)

REENTER_TOKEN_MESSAGE = (
    "Re-enter the access token when changing the Git server: a saved token is only "
    "sent to the server it was saved for."
)


def resolve_provider_token(
    config: ProjectGitConfig | None, draft_token: str | None, base_url: str
) -> str | None:
    """Credential precedence: draft PAT, then project PAT, then global token, each
    saved credential only for the server it belongs to (``credentials.resolve_token``).

    Raises a safe ``not_configured`` error when the project's stored credential
    cannot be decrypted, so a broken credential is never silently downgraded to
    the shared global token.
    """
    if draft_token and draft_token.strip():
        # An explicit draft PAT wins, and is never persisted by this path.
        return draft_token.strip()
    return credential_svc.resolve_token(config, base_url)


def credentials_configured(config: ProjectGitConfig | None = None) -> bool:
    """True when a credential exists for this project (own PAT or fallback)."""
    return credential_svc.credentials_configured(config)


def integration_state(
    db: Session, project_id: int
) -> tuple[ProjectGitConfig | None, bool]:
    """``(config_row, usable)`` — usable is False when disabled anywhere."""
    config = get_config_row(db, project_id)
    if not get_settings().GIT_BRANCH_CREATION_ENABLED:
        return config, False
    return config, bool(config is not None and config.enabled)


def config_out(config: ProjectGitConfig) -> dict:
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
        "credentials_configured": credentials_configured(config),
    }


def config_defaults(project_id: int) -> dict:
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
        "credentials_configured": credentials_configured(None),
    }


def validate_base_branch(base_branch: str | None) -> str | None:
    """Trim and validate an action-specific base branch (never a stored value)."""
    base_branch = (base_branch or "").strip() or None
    if base_branch is not None and not re.fullmatch(r"[\w.\-/]+", base_branch):
        raise HTTPException(
            status_code=422,
            detail="Base branch contains characters that are not valid in a Git ref",
        )
    return base_branch


def default_base_branch(config: ProjectGitConfig | None) -> str:
    return (
        (config.default_base_branch if config is not None else "")
        or get_settings().GITHUB_DEFAULT_BASE_BRANCH
    )


def apply_credential_operation(
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


def check_server_change(config: ProjectGitConfig, payload: GitIntegrationIn) -> None:
    """Refuse a server the deployment may not reach, and a server change that would carry
    the stored project token to a new host without it being re-entered."""
    new_base = (payload.base_url or "").strip() or get_settings().GITHUB_API_URL
    host = parse_base_url(new_base)[2]
    try:
        net_guard.allowed_addresses(host, 443, get_settings().GIT_ALLOW_PRIVATE_HOSTS)
    except net_guard.HostNotAllowed:
        raise HTTPException(status_code=422, detail=HOST_NOT_ALLOWED_MESSAGE) from None
    except OSError:
        pass  # Unresolvable here: saved as-is, and the connection test reports it.
    if (
        credential_svc.project_credential_stored(config)
        and not payload.credential
        and not payload.clear_credential
        and not credential_svc.same_server(new_base, credential_svc.saved_base_url(config))
    ):
        raise HTTPException(status_code=422, detail=REENTER_TOKEN_MESSAGE)


def get_config_row(db: Session, project_id: int) -> ProjectGitConfig | None:
    return db.scalar(
        select(ProjectGitConfig).where(ProjectGitConfig.project_id == project_id)
    )
