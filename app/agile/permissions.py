"""Static role-to-permission matrix.

No persisted permission configuration; every check uses the existing global
user role (app.models.ROLE_ADMIN/ROLE_MANAGER/ROLE_USER) plus project
membership (app.access.can_access_project). UI hiding is never authorization
— every route calls has_permission()/require_permission() itself.
"""
from __future__ import annotations

from fastapi import HTTPException, status

from app.models import ROLE_ADMIN, ROLE_MANAGER, ROLE_USER, User

# --- Permission names ---
VIEW_PROJECT = "view_project"
VIEW_BOARD = "view_board"
CREATE_ITEM = "create_item"
EDIT_ITEM = "edit_item"
TRANSITION_ITEM = "transition_item"
MANAGE_BACKLOG = "manage_backlog"
RANK_ITEMS = "rank_items"
MOVE_ITEMS = "move_items"
CREATE_SPRINT = "create_sprint"
START_SPRINT = "start_sprint"
COMPLETE_SPRINT = "complete_sprint"
CANCEL_SPRINT = "cancel_sprint"
MANAGE_BOARD = "manage_board"
MANAGE_WORKFLOWS = "manage_workflows"
MANAGE_EPICS = "manage_epics"
MANAGE_RELEASES = "manage_releases"
MANAGE_COMPONENTS = "manage_components"
MANAGE_LABELS = "manage_labels"
VIEW_AGILE_REPORTS = "view_agile_reports"
EXPORT_AGILE_REPORTS = "export_agile_reports"
CONFIGURE_BOARD = "configure_board"
CONFIGURE_AGILE_SETTINGS = "configure_agile_settings"

_ALL_PERMISSIONS = (
    VIEW_PROJECT, VIEW_BOARD, CREATE_ITEM, EDIT_ITEM, TRANSITION_ITEM,
    MANAGE_BACKLOG, RANK_ITEMS, MOVE_ITEMS,
    CREATE_SPRINT, START_SPRINT, COMPLETE_SPRINT, CANCEL_SPRINT,
    MANAGE_BOARD, MANAGE_WORKFLOWS,
    MANAGE_EPICS, MANAGE_RELEASES, MANAGE_COMPONENTS, MANAGE_LABELS,
    VIEW_AGILE_REPORTS, EXPORT_AGILE_REPORTS,
    CONFIGURE_BOARD, CONFIGURE_AGILE_SETTINGS,
)

# Planning/lifecycle/config permissions a Manager holds (everything except
# user administration, which lives outside the Agile module entirely).
_MANAGER_PERMISSIONS = set(_ALL_PERMISSIONS)

# A regular User may view, create allowed items, edit/transition, but never
# run sprint lifecycle or touch board/workflow/taxonomy configuration.
_USER_PERMISSIONS = {
    VIEW_PROJECT, VIEW_BOARD, CREATE_ITEM, EDIT_ITEM, TRANSITION_ITEM,
    VIEW_AGILE_REPORTS, EXPORT_AGILE_REPORTS,
}

_ROLE_PERMISSIONS = {
    ROLE_ADMIN: set(_ALL_PERMISSIONS),
    ROLE_MANAGER: _MANAGER_PERMISSIONS,
    ROLE_USER: _USER_PERMISSIONS,
}


def has_permission(user: User, permission: str) -> bool:
    """True if `user`'s global role grants `permission` (see the matrix above)."""
    return permission in _ROLE_PERMISSIONS.get(user.role, frozenset())


def require_item_edit(user: User, item) -> None:
    """Apply the work-item edit policy (app.auth.can_edit_bug) to a bugs row.

    The Agile matrix grants EDIT_ITEM/TRANSITION_ITEM to every role, but the
    per-type rule still holds: regular users cannot change Tasks or
    Requirements. Without this check an Agile endpoint (board drag, estimate,
    flags, hierarchy...) would let them change what PUT /api/bugs refuses.
    """
    from app.auth import can_edit_bug

    item_type = getattr(item, "item_type", None) or "Bug"
    if not can_edit_bug(user, getattr(item, "reporter_id", None), [], item_type=item_type):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail=f"You don't have permission to edit this {item_type.lower()}.",
        )


def require_permission(user: User, permission: str) -> None:
    """Raise 403 if `user` lacks `permission`. Callers must separately verify
    project membership (app.access.can_access_project) — this only checks the
    role-based Agile permission matrix."""
    if not has_permission(user, permission):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail=f"Missing required permission: {permission}",
        )
