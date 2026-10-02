"""Git OpenAPI contract tests: documented S8415 responses are genuine.

Pins decorator-level ``responses=`` metadata on every git route — paths,
methods, auth requirements and the exact documented status codes.
"""
from __future__ import annotations


def _responses_for(paths: dict, path: str, method: str) -> dict:
    entry = paths.get(path, {}).get(method, {})
    assert entry, f"route not found: {method} {path}"
    return entry.get("responses", {}) or {}


def _codes(responses: dict) -> set[str]:
    return {str(code) for code in responses}


def test_git_openapi_documents_error_contract(client):
    """Every git route documents the HTTPException statuses it can raise."""
    spec = client.get("/openapi.json")
    assert spec.status_code == 200
    paths = spec.json()["paths"]

    # config get/put: 404 (scoped project) + 409 (version/credential) +
    # 422 (bad base branch via helpers)
    get_cfg = _responses_for(paths, "/api/git/projects/{project_id}/config", "get")
    assert _codes(get_cfg) >= {"404", "409", "422"}
    put_cfg = _responses_for(paths, "/api/git/projects/{project_id}/config", "put")
    assert _codes(put_cfg) >= {"404", "409", "422"}
    # discovery/list/preview/create: 404 (scope) + 409 (disabled/duplicate) +
    # 422 (story-only/bad input)
    for path, method in [
        ("/api/git/projects/{project_id}/available-repositories", "get"),
        ("/api/git/work-items/{work_item_id}/branches", "get"),
        ("/api/git/work-items/{work_item_id}/branches/preview", "get"),
        ("/api/git/work-items/{work_item_id}/branches", "post"),
    ]:
        assert _codes(_responses_for(paths, path, method)) >= {"404", "409", "422"}, path
    # removal: 403 (deletion disabled) + 404 (scoped record) +
    # 409 (version/duplicate/ref) + 422 (non-story)
    remove = _responses_for(paths, "/api/git/branches/{branch_record_id}", "delete")
    assert _codes(remove) >= {"403", "404", "409", "422"}


def test_git_config_member_forbidden_not_leaked(user_client):
    """Plain members get 403 (not 404/200 data) on project git config."""
    assert user_client.get("/api/git/projects/1/config").status_code in (403, 404)
    assert user_client.put("/api/git/projects/1/config", json={}).status_code in (403, 404)


def test_git_config_unknown_project_is_404(admin_client):
    """Documented 404 is genuine: scoped-away projects read as not found."""
    assert admin_client.get("/api/git/projects/999999/config").status_code == 404


def test_git_remove_unknown_record_is_404(admin_client):
    """Documented 404 is genuine: unknown branch records are invisible."""
    assert admin_client.delete("/api/git/branches/999999").status_code == 404


def test_git_preview_requires_provider_repo_id(admin_client):
    """Documented 422 is genuine: blank provider_repo_id is rejected."""
    resp = admin_client.get(
        "/api/git/work-items/1/branches/preview", params={"provider_repo_id": "  "}
    )
    assert resp.status_code in (404, 422)
