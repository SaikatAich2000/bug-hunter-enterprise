"""Git connection test: the saved-config check and the unsaved-draft check.

Both always return a result dict (the HTTP layer answers 200; ``ok`` carries the verdict).
Only the saved check persists anything. ``build`` is the provider factory, injected by the
route so tests can replace it.
"""
from __future__ import annotations

from typing import Callable

from sqlalchemy.orm import Session

from app.config import get_settings
from app.git import credentials as credential_svc
from app.git.naming import is_valid_branch_name
from app.git.provider import (
    INVALID_BASE_BRANCH,
    INVALID_BASE_URL,
    NOT_CONFIGURED,
    GitProviderError,
    sanitize_provider_message,
)
from app.models import Project, ProjectGitConfig, User
from app.schemas import GitConnectionDraftIn
from app.services.audit import audit
from app.util import utcnow

_OTHER_SERVER_MESSAGE = (
    "Enter an access token to test a different Git server: a saved token is only "
    "sent to the server it was saved for."
)
_NOT_SAVED = " [draft values were not saved]"


def _result(ok: bool, message: str, target: str, checked_at, code: str) -> dict:
    return {
        "ok": ok, "status": "ok" if ok else "error", "message": message,
        "target": target, "checked_at": checked_at, "error_code": code,
    }


def _log(db: Session, actor: User, project: Project, config: ProjectGitConfig | None,
         action: str, detail: str) -> None:
    audit(db, actor, "git_branch", config.id if config is not None else project.id, action, detail)


def record_connection_result(
    db: Session, config: ProjectGitConfig | None, *, ok: bool, message: str
) -> None:
    if config is None:
        return
    config.connection_status = "ok" if ok else "error"
    config.last_checked_at = utcnow()
    config.last_error = "" if ok else sanitize_provider_message(message)
    config.version += 1
    db.flush()


def check_saved(db: Session, actor: User, project: Project, config: ProjectGitConfig | None,
               build: Callable) -> dict:
    """Test the stored configuration and record the outcome on it."""
    provider = build(config)
    checked_at = utcnow()
    try:
        target = provider.validate_connection()
    except GitProviderError as exc:
        record_connection_result(db, config, ok=False, message=exc.message)
        _log(db, actor, project, config, "git_connection_test_failed",
             f"Connection test failed for project '{project.name}' ({exc.code}): {exc.message}")
        db.commit()
        return _result(False, exc.message, "", checked_at, exc.code)
    record_connection_result(db, config, ok=True, message="")
    _log(db, actor, project, config, "git_connection_tested",
         f"Connection test succeeded for project '{project.name}' (target={target})")
    db.commit()
    return _result(True, f"Connected to {target}", target, checked_at, "")


def _draft_failed(db: Session, actor: User, project: Project, config: ProjectGitConfig | None,
                  code: str, message: str, detail: str | None = None) -> dict:
    """Audit and return a failed draft test (``detail`` overrides the audited reason)."""
    _log(db, actor, project, config, "git_connection_test_failed",
         f"Connection test failed for project '{project.name}' ({code}): {detail or message}{_NOT_SAVED}")
    db.commit()
    return _result(False, message, "", utcnow(), code)


def _draft_base_branch(draft: GitConnectionDraftIn, config: ProjectGitConfig | None) -> str:
    if draft.default_base_branch is not None:
        return (draft.default_base_branch or "").strip()
    if config is not None and (config.default_base_branch or "").strip():
        return (config.default_base_branch or "").strip()
    return (get_settings().GITHUB_DEFAULT_BASE_BRANCH or "").strip()


def _draft_refusal(base_url: str, branch: str) -> tuple[str, str, str] | None:
    """(code, message, audited reason) when the draft cannot be tested at all."""
    if not base_url:
        return INVALID_BASE_URL, "Git base URL is required", "blank base URL"
    if not branch:
        return INVALID_BASE_BRANCH, "Default base branch is required", "blank base branch"
    if not is_valid_branch_name(branch):
        return (INVALID_BASE_BRANCH, "Default base branch is not a valid git branch name",
                "invalid base branch")
    return None


def check_draft(db: Session, actor: User, project: Project, config: ProjectGitConfig | None,
               draft: GitConnectionDraftIn, build: Callable) -> dict:
    """Test the draft exactly as submitted; nothing is persisted."""
    base_url = None if draft.base_url is None else (draft.base_url or "").strip()
    organization = None if draft.organization is None else (draft.organization or "").strip()
    branch = _draft_base_branch(draft, config)
    refusal = _draft_refusal(base_url or "", branch)
    if refusal is not None:
        code, message, reason = refusal
        return _draft_failed(db, actor, project, config, code, message, reason)
    token = draft.credential or None
    if not draft.credential:
        try:
            token = credential_svc.resolve_token(config, base_url)
        except GitProviderError as exc:
            return _draft_failed(db, actor, project, config, exc.code, exc.message)
        if token is None and credential_svc.credentials_configured(config):
            return _draft_failed(db, actor, project, config, NOT_CONFIGURED, _OTHER_SERVER_MESSAGE)
    provider = build(config, base_url=base_url, organization=organization, token=token)
    checked_at = utcnow()
    try:
        target = provider.validate_connection()
    except GitProviderError as exc:
        _log(db, actor, project, config, "git_connection_test_failed",
             f"Connection test failed for project '{project.name}' ({exc.code}): {exc.message}{_NOT_SAVED}")
        db.commit()
        return _result(False, exc.message, "", checked_at, exc.code)
    _log(db, actor, project, config, "git_connection_tested",
         f"Connection test succeeded for project '{project.name}' (target={target}){_NOT_SAVED}")
    db.commit()
    return _result(True, f"Connected to {target}", target, checked_at, "")
