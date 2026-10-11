"""Feature-branch payloads and permission checks."""
from __future__ import annotations

from fastapi import HTTPException
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.access import accessible_project_ids, can_access_project
from app.config import get_settings
from app.git.branches import BranchTarget
from app.git.naming import (
    display_id_for_branch,
    supports_branch_creation,
)
from app.models import (
    ROLE_ADMIN,
    ROLE_MANAGER,
    Bug,
    ProjectRepository,
    User,
    WorkItemBranch,
)
from app.services.git_config import (
    integration_state,
)

DETAIL_WORK_ITEM_NOT_FOUND = "Work item not found"

DETAIL_INTEGRATION_DISABLED = "Git integration is not enabled for this project"

# Story-only branch actions (preview/create/remove) share one stable refusal.
STORY_ONLY_CODE = "user_story_only"

STORY_ONLY_MESSAGE = "Feature branches can be created only for User Stories."


def repo_full_name(repository: ProjectRepository) -> str:
    if repository.owner:
        return f"{repository.owner}/{repository.name}"
    return repository.name


def is_project_admin(user: User) -> bool:
    """The application's existing administrative roles act as Project Admin."""
    return user.role in (ROLE_ADMIN, ROLE_MANAGER)


def branch_out(row: WorkItemBranch, *, display_id: str, title: str) -> dict:
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
            repo_full_name(repository) if repository is not None else ""
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


def assignee_briefs(db: Session, target: BranchTarget) -> list[dict]:
    if not target.assignee_ids:
        return []
    rows = db.scalars(
        select(User).where(User.id.in_(sorted(target.assignee_ids))).order_by(User.id)
    ).all()
    return [
        {"id": row.id, "name": row.name, "email": row.email, "role": row.role}
        for row in rows
    ]


def bug_target(db: Session, item_id: int, user: User) -> BranchTarget:
    """Resolve a `bugs` row (Bug/Requirement/Task/Sub-task/Story/Epic)."""
    bug = db.get(Bug, item_id)
    if bug is None or not can_access_project(
        accessible_project_ids(db, user), bug.project_id
    ):
        raise HTTPException(status_code=404, detail=DETAIL_WORK_ITEM_NOT_FOUND)
    item_type = bug.item_type or "Bug"
    return BranchTarget(
        item_type=item_type,
        item_id=bug.id,
        project_id=bug.project_id,
        title=bug.title,
        display_id=display_id_for_branch(item_type, bug.id, bug.display_id),
        assignee_ids=frozenset(assignee.id for assignee in bug.assignees),
    )


def require_story(target: BranchTarget) -> None:
    """Story-only gate for every branch action, before any provider work."""
    if not supports_branch_creation(target.item_type):
        raise HTTPException(
            status_code=422,
            detail=STORY_ONLY_MESSAGE,
            headers={"X-Error-Code": STORY_ONLY_CODE},
        )


def assert_can_create_branch(target: BranchTarget, actor: User) -> None:
    """The assignee-or-Project-Admin rule. The ONLY authorization for creation.

    A reporter, viewer, generic project member or unrelated user does not qualify
    merely because they can view or edit the item.
    """
    if not actor.is_active:
        raise HTTPException(
            status_code=403, detail="A deactivated user cannot create a Git branch"
        )
    if is_project_admin(actor):
        return
    if actor.id not in target.assignee_ids:
        raise HTTPException(
            status_code=403,
            detail=(
                "Only an assignee of this work item or a project administrator "
                "can create its Git branch"
            ),
        )


def creation_permission(
    target: BranchTarget, actor: User, *, integration_enabled: bool
) -> tuple[bool, str]:
    """Non-authoritative hint for the UI, plus the reason when it is False."""
    if not supports_branch_creation(target.item_type):
        return False, STORY_ONLY_MESSAGE
    if not get_settings().GIT_BRANCH_CREATION_ENABLED:
        return False, "Branch creation is disabled for this deployment"
    if not integration_enabled:
        return False, DETAIL_INTEGRATION_DISABLED
    if not actor.is_active:
        return False, "Your account is deactivated"
    if is_project_admin(actor) or actor.id in target.assignee_ids:
        return True, ""
    return False, (
        "Only an assignee of this work item or a project administrator can create "
        "its Git branch"
    )


def work_item_branches_payload(
    db: Session, target: BranchTarget, actor: User
) -> dict:
    """Development section: existing branches plus what the actor may do.

    Branch history is listed for every work-item type; branch ACTIONS are
    Story-only and every action is re-authorized server-side. The internal
    repository row id is never exposed; repositories are discovered live by the
    create dialog instead of being listed here.
    """
    _config, enabled = integration_state(db, target.project_id)
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
        branch_out(row, display_id=target.display_id, title=target.title)
        for row in rows
    ]
    can_create, reason = creation_permission(
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
        "assignees": assignee_briefs(db, target),
        "branch_creation_supported": supported,
        "integration_enabled": enabled,
        "can_create_branch": can_create,
        "reason": reason,
        "branch_deletion_enabled": deletion_enabled,
        "can_remove_branch": can_remove,
        "branches": branches,
    }
