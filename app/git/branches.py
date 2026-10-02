"""Branch-creation flow for one work item + one repository.

Routes own authentication/authorization; this module owns the flow itself:
idempotency, base-branch SHA resolution, remote-existence reconciliation, the
single database write, and failure classification.

Nothing here changes a work item's workflow status, and nothing deletes or
retires a branch.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime, timezone

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.git.naming import (
    branch_prefix_for,
    build_branch_name,
    stable_reference,
    supports_branch_creation,
)
from app.git.provider import (
    BASE_BRANCH_MISSING,
    GitProviderError,
    sanitize_provider_message,
)
from app.models import ProjectRepository, User, WorkItemBranch

logger = logging.getLogger("bug_hunter.git.branches")

STATUS_ACTIVE = "Active"
STATUS_UNKNOWN = "Unknown"

# Domain refusals (never provider failures) that the route maps to HTTP statuses.
UNSUPPORTED_ITEM_TYPE = "unsupported_item_type"
REPOSITORY_MISMATCH = "repository_mismatch"
REPOSITORY_INACTIVE = "repository_inactive"
BASE_BRANCH_UNCONFIGURED = "base_branch_missing"
UNVERIFIED_BRANCH_NAME = "unverified_branch_name"
DUPLICATE_BRANCH = "duplicate_branch"
BRANCH_ALREADY_REMOVED = "branch_already_removed"

STATUS_DELETED = "Deleted"


class BranchCreationError(Exception):
    """A refusal the caller can explain to the user without leaking internals."""

    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code
        self.message = message


@dataclass(frozen=True)
class BranchTarget:
    """The work item a branch belongs to.

    ``item_type`` is the canonical work-item type (Tip: "Feature" for a
    `features` row, otherwise the `bugs.item_type` value).
    """

    item_type: str
    item_id: int
    project_id: int
    title: str
    display_id: str
    assignee_ids: frozenset[int]


def find_existing_branch(
    db: Session, *, target: BranchTarget, repository: ProjectRepository
) -> WorkItemBranch | None:
    """The Active branch already recorded for this work item in this repository.

    One ACTIVE branch per work item per repository is the rule, so the lookup
    ignores the branch name (a renamed title must not create a second branch).
    Deleted history rows never block a new branch.
    """
    return db.scalar(
        select(WorkItemBranch)
        .where(
            WorkItemBranch.work_item_type == target.item_type,
            WorkItemBranch.work_item_id == target.item_id,
            WorkItemBranch.repository_id == repository.id,
            WorkItemBranch.status == STATUS_ACTIVE,
        )
        .order_by(WorkItemBranch.id)
        .limit(1)
    )


def find_existing_branch_by_provider(
    db: Session, *, target: BranchTarget, provider_repo_id: str
) -> WorkItemBranch | None:
    """Active record for this work item + provider repository id.

    Read-only lookups (preview) use this so no internal identity row is needed.
    """
    if not provider_repo_id:
        return None
    return db.scalar(
        select(WorkItemBranch)
        .where(
            WorkItemBranch.work_item_type == target.item_type,
            WorkItemBranch.work_item_id == target.item_id,
            WorkItemBranch.project_id == target.project_id,
            WorkItemBranch.provider_repo_id == provider_repo_id,
            WorkItemBranch.status == STATUS_ACTIVE,
        )
        .order_by(WorkItemBranch.id)
        .limit(1)
    )


def preview_branch_name(
    target: BranchTarget,
    base_branch_override: str | None = None,
    *,
    default_base_branch: str = "",
) -> tuple[str, str, str]:
    """``(branch_name, base_branch, display_id)`` for the confirmation dialog.

    ``base_branch_override`` is the action-specific branch the user picked in
    the create dialog; when absent the project/repository default is used.
    """
    _require_supported(target)
    branch_name = build_branch_name(
        target.item_type,
        target.title,
        item_id=target.item_id,
        display_id=target.display_id,
    )
    _require_ownership(target, branch_name)
    base_branch = (
        (base_branch_override or "").strip() or (default_base_branch or "").strip()
    )
    if not base_branch:
        raise BranchCreationError(
            BASE_BRANCH_UNCONFIGURED,
            "No base branch is configured for this repository",
        )
    return branch_name, base_branch, target.display_id


def _now() -> datetime:
    # Mirrors models._utcnow(): second-resolution UTC, so SQLite and Postgres
    # round-trip the same values.
    return datetime.now(timezone.utc).replace(microsecond=0)


def _repo_label(repository: ProjectRepository) -> str:
    return f"{repository.owner}/{repository.name}" if repository.owner else repository.name


def _require_supported(target: BranchTarget) -> None:
    if not supports_branch_creation(target.item_type):
        raise BranchCreationError(
            UNSUPPORTED_ITEM_TYPE,
            f"Branch creation is not supported for {target.item_type} work items",
        )


def _require_repository_usable(target: BranchTarget, repository: ProjectRepository) -> None:
    if repository.project_id != target.project_id:
        raise BranchCreationError(
            REPOSITORY_MISMATCH, "Repository does not belong to this project"
        )
    if not repository.active:
        raise BranchCreationError(
            REPOSITORY_INACTIVE, f"Repository '{repository.name}' is inactive"
        )
    if not repository.branch_creation_enabled:
        raise BranchCreationError(
            REPOSITORY_INACTIVE,
            f"Branch creation is disabled for repository '{repository.name}'",
        )


def _require_ownership(target: BranchTarget, branch_name: str) -> None:
    """The generated ref must embed this work item's stable number.

    Without this, an unrelated remote branch that happens to share the name could
    be adopted (the create flow only reconciles branches that clearly belong
    here).
    """
    reference = stable_reference(
        target.item_type, target.item_id, target.display_id
    ).lower()
    prefix = branch_prefix_for(target.item_type)
    if not branch_name.lower().startswith(f"{prefix}_{reference}_"):
        raise BranchCreationError(
            UNVERIFIED_BRANCH_NAME,
            "Generated branch name could not be tied to this work item",
        )


def _persist(
    db: Session,
    provider,
    target: BranchTarget,
    repository: ProjectRepository,
    *,
    branch_name: str,
    base_branch: str,
    sha: str,
    actor: User,
) -> WorkItemBranch:
    row = WorkItemBranch(
        work_item_type=target.item_type,
        work_item_id=target.item_id,
        project_id=target.project_id,
        repository_id=repository.id,
        provider_repo_id=repository.provider_repo_id or "",
        branch_name=branch_name,
        base_branch=base_branch,
        base_commit_sha=sha or "",
        branch_url=(
            provider.build_branch_url(repository.owner, repository.name, branch_name) or ""
        ),
        provider_branch_ref=f"refs/heads/{branch_name}",
        status=STATUS_ACTIVE,
        created_by_id=actor.id,
        created_by_name=actor.name,
        last_checked_at=_now(),
    )
    db.add(row)
    # A unique-index violation here means a concurrent request won the race; the
    # route rolls back, re-reads and returns the winner's row (never a 500 and
    # never a second remote branch).
    db.flush()
    return row


def record_failure(
    db: Session, *, target: BranchTarget, repository: ProjectRepository, error: Exception
) -> None:
    """Attach a sanitized error to an existing record.

    A failed creation deliberately does NOT insert a new row: a row that looks
    Active must never exist unless the remote branch actually does.
    """
    existing = find_existing_branch(db, target=target, repository=repository)
    if existing is None:
        return
    message = getattr(error, "message", None) or str(error)
    existing.last_error = sanitize_provider_message(message)
    existing.last_checked_at = _now()
    db.flush()


def create_branch(
    db: Session,
    provider,
    *,
    target: BranchTarget,
    repository: ProjectRepository,
    actor: User,
    base_branch_override: str | None = None,
) -> tuple[WorkItemBranch, bool]:
    """Create — or reconcile — the branch. Returns ``(row, created)``.

    Idempotent by construction: an existing record is returned untouched, and a
    remote branch that already exists is adopted rather than duplicated under
    another name. That covers a double click, a refresh, a retry after a
    timeout, and a database failure that happened after the remote creation.
    """
    _require_supported(target)
    _require_repository_usable(target, repository)

    branch_name, base_branch, _ = preview_branch_name(
        target, base_branch_override,
        default_base_branch=repository.default_base_branch,
    )

    # Steps 9+10 of the contract: verify the base branch and capture the latest
    # tip SHA immediately before any decision to create.
    base_sha = provider.get_branch_sha(repository.owner, repository.name, base_branch)
    if not base_sha:
        raise GitProviderError(
            BASE_BRANCH_MISSING,
            f"Base branch '{base_branch}' was not found in {_repo_label(repository)}",
        )

    # Step 13: an active local record is authoritative and blocks a second branch.
    existing = find_existing_branch(db, target=target, repository=repository)
    if existing is not None:
        return existing, False

    if provider.branch_exists(repository.owner, repository.name, branch_name):
        # Step 14 reconciliation: uncertain earlier result, or the branch was
        # created outside this app. Adopt it instead of failing.
        remote_sha = (
            provider.get_branch_sha(repository.owner, repository.name, branch_name)
            or base_sha
        )
        return (
            _persist(
                db, provider, target, repository,
                branch_name=branch_name, base_branch=base_branch,
                sha=remote_sha, actor=actor,
            ),
            False,
        )

    try:
        created_sha = provider.create_branch(
            repository.owner, repository.name, branch_name, base_sha
        )
    except GitProviderError as exc:
        # Step 16: reconcile an uncertain remote success — the ref may exist
        # even though the create response was lost.
        if exc.retryable and provider.branch_exists(
            repository.owner, repository.name, branch_name
        ):
            remote_sha = (
                provider.get_branch_sha(
                    repository.owner, repository.name, branch_name
                )
                or base_sha
            )
            return (
                _persist(
                    db, provider, target, repository,
                    branch_name=branch_name, base_branch=base_branch,
                    sha=remote_sha, actor=actor,
                ),
                True,
            )
        raise
    return (
        _persist(
            db, provider, target, repository,
            branch_name=branch_name, base_branch=base_branch,
            sha=created_sha or base_sha, actor=actor,
        ),
        True,
    )


__all__ = [
    "BASE_BRANCH_UNCONFIGURED",
    "BRANCH_ALREADY_REMOVED",
    "BranchCreationError",
    "BranchTarget",
    "DUPLICATE_BRANCH",
    "REPOSITORY_INACTIVE",
    "REPOSITORY_MISMATCH",
    "STATUS_ACTIVE",
    "STATUS_DELETED",
    "STATUS_UNKNOWN",
    "UNSUPPORTED_ITEM_TYPE",
    "UNVERIFIED_BRANCH_NAME",
    "create_branch",
    "find_existing_branch",
    "find_existing_branch_by_provider",
    "preview_branch_name",
    "record_failure",
]