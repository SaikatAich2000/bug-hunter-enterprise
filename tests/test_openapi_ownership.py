"""OpenAPI operation ownership gate.

Fails when a new OpenAPI operation lacks test ownership, when duplicate
operation IDs exist, or when route metadata is inconsistent. Ownership is a
maintainable method+path-prefix map, not a hard-coded count.
"""
from __future__ import annotations

from pathlib import Path

OPERATION_OWNERS: dict[tuple[str, str], list[str]] = {
    ("GET", "/api/health"): ["tests/test_api.py"],
    ("GET", "/api/meta"): ["tests/test_changes.py"],
    ("POST", "/api/auth/login"): ["tests/test_auth_routes.py"],
    ("POST", "/api/auth/logout"): ["tests/test_auth_routes.py"],
    ("POST", "/api/auth/signup"): ["tests/test_tenancy_accounts.py"],
    ("PUT", "/api/auth/profile"): ["tests/test_tenancy_accounts.py"],
    ("POST", "/api/auth/email-change"): ["tests/test_tenancy_accounts.py"],
    ("GET", "/api/auth/data-export"): ["tests/test_tenancy_accounts.py"],
    ("DELETE", "/api/auth/account"): ["tests/test_tenancy_accounts.py"],
    ("POST", "/api/auth/login/totp"): ["tests/test_two_factor.py"],
    ("POST", "/api/users/{user_id}/reset-2fa"): ["tests/test_two_factor.py"],
    ("GET", "/api/auth/2fa"): ["tests/test_two_factor.py"],
    ("POST", "/api/auth/2fa"): ["tests/test_two_factor.py"],
    ("GET", "/api/organization"): ["tests/test_tenancy_accounts.py"],
    ("PUT", "/api/organization"): ["tests/test_tenancy_accounts.py"],
    ("GET", "/api/branding"): ["tests/test_tenancy_accounts.py"],
    ("PUT", "/api/branding"): ["tests/test_tenancy_accounts.py"],
    ("GET", "/api/invitations"): ["tests/test_invitations_memberships.py"],
    ("POST", "/api/invitations"): ["tests/test_invitations_memberships.py"],
    ("DELETE", "/api/invitations"): ["tests/test_invitations_memberships.py"],
    ("GET", "/api/projects/{project_id}/members"): ["tests/test_invitations_memberships.py"],
    ("POST", "/api/projects/{project_id}/members"): ["tests/test_invitations_memberships.py"],
    ("PUT", "/api/projects/{project_id}/members"): ["tests/test_invitations_memberships.py"],
    ("DELETE", "/api/projects/{project_id}/members"): ["tests/test_invitations_memberships.py"],
    ("GET", "/api/projects/{project_id}/custom-fields"): ["tests/test_saved_views_custom_fields.py"],
    ("POST", "/api/projects/{project_id}/custom-fields"): ["tests/test_saved_views_custom_fields.py"],
    ("PUT", "/api/projects/{project_id}/custom-fields"): ["tests/test_saved_views_custom_fields.py"],
    ("DELETE", "/api/projects/{project_id}/custom-fields"): ["tests/test_saved_views_custom_fields.py"],
    ("GET", "/api/bugs/{bug_id}/custom-values"): ["tests/test_saved_views_custom_fields.py"],
    ("PUT", "/api/bugs/{bug_id}/custom-values"): ["tests/test_saved_views_custom_fields.py"],
    ("GET", "/api/saved-views"): ["tests/test_saved_views_custom_fields.py"],
    ("POST", "/api/saved-views"): ["tests/test_saved_views_custom_fields.py"],
    ("PUT", "/api/saved-views"): ["tests/test_saved_views_custom_fields.py"],
    ("DELETE", "/api/saved-views"): ["tests/test_saved_views_custom_fields.py"],
    ("GET", "/api/webhooks"): ["tests/test_webhooks.py"],
    ("POST", "/api/webhooks"): ["tests/test_webhooks.py"],
    ("PUT", "/api/webhooks"): ["tests/test_webhooks.py"],
    ("DELETE", "/api/webhooks"): ["tests/test_webhooks.py"],
    ("POST", "/api/devices"): ["tests/test_devices_metrics_retention.py"],
    ("DELETE", "/api/devices"): ["tests/test_devices_metrics_retention.py"],
    ("GET", "/api/notifications/preferences"): ["tests/test_devices_metrics_retention.py"],
    ("PUT", "/api/notifications/preferences"): ["tests/test_devices_metrics_retention.py"],
    ("GET", "/api/auth/me"): ["tests/test_auth_routes.py"],
    ("POST", "/api/auth/change-password"): ["tests/test_auth_routes.py"],
    ("POST", "/api/auth/forgot-password"): ["tests/test_auth_routes.py"],
    ("POST", "/api/auth/reset-password"): ["tests/test_auth_routes.py"],
    ("GET", "/api/users"): ["tests/test_routes_crud.py"],
    ("POST", "/api/users"): ["tests/test_routes_crud.py"],
    ("PUT", "/api/users"): ["tests/test_routes_crud.py"],
    ("DELETE", "/api/users"): ["tests/test_routes_crud.py"],
    ("GET", "/api/projects"): ["tests/test_routes_crud.py"],
    ("POST", "/api/projects"): ["tests/test_routes_crud.py"],
    ("PUT", "/api/projects"): ["tests/test_routes_crud.py"],
    ("DELETE", "/api/projects"): ["tests/test_routes_crud.py"],
    ("GET", "/api/bugs/import/template.xlsx"): ["tests/test_bulk_import.py"],
    ("POST", "/api/bugs/import"): ["tests/test_bulk_import.py"],
    ("POST", "/api/bugs/bulk"): ["tests/test_bulk.py"],
    ("GET", "/api/bugs"): ["tests/test_api.py"],
    ("POST", "/api/bugs"): ["tests/test_api.py"],
    ("PUT", "/api/bugs"): ["tests/test_api.py"],
    ("DELETE", "/api/bugs"): ["tests/test_api.py"],
    ("GET", "/api/events"): ["tests/test_events.py"],
    ("POST", "/api/events"): ["tests/test_events.py"],
    ("PUT", "/api/events"): ["tests/test_events.py"],
    ("DELETE", "/api/events"): ["tests/test_events.py"],
    ("GET", "/api/stats"): ["tests/test_stats_status_filter.py"],
    ("GET", "/api/reports"): ["tests/test_reports.py"],
    ("POST", "/api/reports"): ["tests/test_reports.py"],
    ("GET", "/api/audit"): ["tests/test_audit_search.py"],
    ("GET", "/api/sessions"): ["tests/test_routes_crud.py"],
    ("DELETE", "/api/sessions"): ["tests/test_routes_crud.py"],
    ("GET", "/api/notifications"): ["tests/test_notifications.py"],
    ("POST", "/api/notifications"): ["tests/test_notifications.py"],
    ("DELETE", "/api/notifications"): ["tests/test_notifications.py"],
    ("GET", "/api/push"): ["tests/test_push.py"],
    ("POST", "/api/push"): ["tests/test_push.py"],
    ("GET", "/api/agile/boards"): ["tests/test_agile_foundation.py"],
    ("PUT", "/api/agile/boards"): ["tests/test_agile_foundation.py"],
    ("POST", "/api/agile/boards"): ["tests/test_agile_foundation.py"],
    ("GET", "/api/agile/projects"): ["tests/test_agile_foundation.py"],
    ("POST", "/api/agile/projects"): ["tests/test_agile_foundation.py"],
    ("PUT", "/api/agile/quick-filters"): ["tests/test_agile_foundation.py"],
    ("DELETE", "/api/agile/quick-filters"): ["tests/test_agile_foundation.py"],
    ("GET", "/api/agile/roadmap"): ["tests/test_agile_foundation.py"],
    ("GET", "/api/agile/labels"): ["tests/test_agile_foundation.py"],
    ("POST", "/api/agile/labels"): ["tests/test_agile_foundation.py"],
    ("PUT", "/api/agile/labels"): ["tests/test_agile_foundation.py"],
    ("DELETE", "/api/agile/labels"): ["tests/test_agile_foundation.py"],
    ("GET", "/api/agile/components"): ["tests/test_agile_taxonomy_routes.py"],
    ("POST", "/api/agile/components"): ["tests/test_agile_taxonomy_routes.py"],
    ("PUT", "/api/agile/components"): ["tests/test_agile_taxonomy_routes.py"],
    ("GET", "/api/agile/epics"): ["tests/test_agile_taxonomy_routes.py"],
    ("POST", "/api/agile/epics"): ["tests/test_agile_taxonomy_routes.py"],
    ("PUT", "/api/agile/epics"): ["tests/test_agile_taxonomy_routes.py"],
    ("GET", "/api/agile/releases"): ["tests/test_git_release_hardening.py"],
    ("POST", "/api/agile/releases"): ["tests/test_git_release_hardening.py"],
    ("PUT", "/api/agile/releases"): ["tests/test_git_release_hardening.py"],
    ("GET", "/api/agile/reports"): ["tests/test_reports_branches.py"],
    ("POST", "/api/agile/reports"): ["tests/test_reports_branches.py"],
    ("GET", "/api/agile/sprints"): ["tests/test_agile_sprints.py"],
    ("POST", "/api/agile/sprints"): ["tests/test_agile_sprints.py"],
    ("PUT", "/api/agile/sprints"): ["tests/test_agile_sprints.py"],
    ("DELETE", "/api/agile/sprints"): ["tests/test_agile_sprints.py"],
    ("PUT", "/api/agile/acceptance-criteria"): ["tests/test_agile_planning_taxonomy_reports.py"],
    ("DELETE", "/api/agile/acceptance-criteria"): ["tests/test_agile_planning_taxonomy_reports.py"],
    ("GET", "/api/agile/work-items"): ["tests/test_routes_work_items.py"],
    ("POST", "/api/agile/work-items"): ["tests/test_routes_work_items.py"],
    ("PUT", "/api/agile/work-items"): ["tests/test_routes_work_items.py"],
    ("GET", "/api/git"): ["tests/test_git_contract.py"],
    ("PUT", "/api/git"): ["tests/test_git_contract.py"],
    ("POST", "/api/git"): ["tests/test_git_contract.py"],
    ("DELETE", "/api/git"): ["tests/test_git_branches.py"],
    ("POST", "/api/chat"): ["tests/test_chat_core.py"],
    ("GET", "/api/chat"): ["tests/test_chat_core.py"],
}


def _owner_key(method: str, path: str) -> tuple[str, str] | None:
    method = method.upper()
    best: tuple[str, str] | None = None
    for m, prefix in OPERATION_OWNERS:
        if m == method and path.startswith(prefix):
            if best is None or len(prefix) > len(best[1]):
                best = (m, prefix)
    return best


def test_openapi_operation_ids_unique():
    from app.main import app

    spec = app.openapi()
    seen: dict[str, str] = {}
    dupes: list[str] = []
    for path, methods in spec["paths"].items():
        for method, detail in methods.items():
            op = detail.get("operationId")
            assert op, f"missing operationId for {method.upper()} {path}"
            key = f"{method.upper()} {path}"
            if op in seen:
                dupes.append(f"{op} ({seen[op]} vs {key})")
            else:
                seen[op] = key
    assert not dupes, f"duplicate operationIds: {dupes}"


def test_every_operation_has_test_owner():
    from app.main import app

    spec = app.openapi()
    missing = [
        f"{method.upper()} {path}"
        for path, methods in spec["paths"].items()
        for method in methods
        if _owner_key(method, path) is None
    ]
    assert not missing, f"operations without test ownership: {missing}"


def test_owner_files_exist():
    root = Path(__file__).resolve().parents[1]
    seen: set[str] = set()
    missing_files: list[str] = []
    for files in OPERATION_OWNERS.values():
        for f in files:
            if f in seen:
                continue
            seen.add(f)
            if not (root / f).exists():
                missing_files.append(f)
    assert not missing_files, f"owner test files missing: {missing_files}"


def test_every_owner_entry_matches_a_live_operation_and_an_existing_file():
    from app.main import app

    live = {(m.upper(), path) for path, methods in app.openapi()["paths"].items() for m in methods}
    root = Path(__file__).resolve().parents[1]
    stale = [key for key in OPERATION_OWNERS
             if not any(m == key[0] and path.startswith(key[1]) for m, path in live)]
    missing = sorted({f for files in OPERATION_OWNERS.values() for f in files if not (root / f).is_file()})
    assert not stale, f"ownership entries without an operation: {stale}"
    assert not missing, f"ownership entries naming missing test files: {missing}"
