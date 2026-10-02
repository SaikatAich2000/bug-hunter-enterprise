"""Users OpenAPI contract tests: documented S8415 responses are genuine.

Guards the decorator-level ``responses=`` metadata added for Sonar
python:S8415 against drift — route paths, methods, auth requirements and
the exact documented status codes are pinned here.
"""
from __future__ import annotations


def _responses_for(routes: dict, path: str, method: str) -> dict:
    entry = routes.get(path, {}).get(method, {})
    assert entry, f"route not found: {method} {path}"
    return entry.get("responses", {}) or {}


def _codes(responses: dict) -> set[str]:
    return {str(code) for code in responses}


def test_users_openapi_documents_error_contract(client):
    """Every users route documents the HTTPException statuses it can raise."""
    spec = client.get("/openapi.json")
    assert spec.status_code == 200
    paths = spec.json()["paths"]

    # create: 400 (breach/unknown projects), 403 (non-admin grant), 409 (email)
    create = _responses_for(paths, "/api/users", "post")
    assert _codes(create) >= {"400", "403", "409"}
    # update: 400 (self/last-admin guardrails), 403 (manager limits),
    #         404 (unknown user), 409 (email race)
    update = _responses_for(paths, "/api/users/{user_id}", "put")
    assert _codes(update) >= {"400", "403", "404", "409"}
    # delete: 400 (self/last-admin), 404 (unknown user)
    delete = _responses_for(paths, "/api/users/{user_id}", "delete")
    assert _codes(delete) >= {"400", "404"}
    # get one: 404 unknown user
    get_one = _responses_for(paths, "/api/users/{user_id}", "get")
    assert "404" in _codes(get_one)


def test_users_routes_require_auth(client):
    """All mutating users routes reject anonymous callers with 401."""
    assert client.post("/api/users", json={}).status_code == 401
    assert client.put("/api/users/1", json={}).status_code == 401
    assert client.delete("/api/users/1").status_code == 401
    assert client.get("/api/users/1").status_code == 401


def test_create_user_conflict_when_email_in_use(admin_client):
    """Documented 409 is genuine: duplicate email is rejected."""
    payload = {
        "name": "Dup",
        "email": "dup@test.local",
        "password": "SomePass123!",
        "role": "user",
        "is_active": True,
        "project_ids": [],
    }
    first = admin_client.post("/api/users", json=payload)
    assert first.status_code == 201
    second = admin_client.post("/api/users", json=payload)
    assert second.status_code == 409


def test_delete_user_not_found_is_404(admin_client):
    """Documented 404 is genuine: unknown user id."""
    resp = admin_client.delete("/api/users/999999")
    assert resp.status_code == 404


def test_delete_self_is_rejected_as_400(admin_client):
    """Documented 400 is genuine: admins cannot delete themselves."""
    me = admin_client.get("/api/auth/me")
    assert me.status_code == 200
    resp = admin_client.delete(f"/api/users/{me.json()['id']}")
    assert resp.status_code == 400
