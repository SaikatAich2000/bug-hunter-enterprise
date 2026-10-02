"""Audit search must match bug numbers (entity_id), entity types, and actor names.
Forgot-password: 204 by default (enumeration-safe), opt-out flag restores 404.
"""
from __future__ import annotations


def _seed_audit_data(admin_client):
    """Drop a couple of bugs in so we have predictable audit rows."""
    proj = admin_client.post("/api/projects",
                             json={"name": "Apollo", "color": "#ffffff"}).json()
    bug1 = admin_client.post("/api/bugs", json={
        "title": "Login broken",
        "project_id": proj["id"],
        "priority": "High",
        "environment": "PROD",
    }).json()
    bug2 = admin_client.post("/api/bugs", json={
        "title": "Crash on save",
        "project_id": proj["id"],
        "priority": "Critical",
        "environment": "UAT",
    }).json()
    return bug1, bug2


def test_audit_search_by_bug_number(admin_client):
    bug1, _bug2 = _seed_audit_data(admin_client)
    # Bug creation produces an Activity with entity_id = bug.id; search should hit it.
    rows = admin_client.get(f"/api/audit?q={bug1['id']}").json()
    assert any(r.get("entity_id") == bug1["id"] for r in rows), \
        f"Expected audit row for bug #{bug1['id']} in {rows}"


def test_audit_search_with_hash_prefix(admin_client):
    bug1, _ = _seed_audit_data(admin_client)
    # A "#<id>" prefix should still resolve to the same bug row.
    rows = admin_client.get(f"/api/audit?q=%23{bug1['id']}").json()
    assert any(r.get("entity_id") == bug1["id"] for r in rows)


def test_audit_search_by_entity_type(admin_client):
    _seed_audit_data(admin_client)
    rows = admin_client.get("/api/audit?q=bug").json()
    assert len(rows) >= 2
    # Every returned row should relate to bugs.
    assert all(
        (r.get("entity_type") == "bug") or ("bug" in (r.get("action") or "").lower())
        for r in rows
    )


def test_audit_search_keeps_existing_behaviour(admin_client):
    _seed_audit_data(admin_client)
    rows = admin_client.get("/api/audit?q=Test").json()  # admin name is "Test Admin"
    assert any("test admin" in (r.get("actor_name") or "").lower() for r in rows)


def test_forgot_password_known_email_returns_204(client):
    r = client.post("/api/auth/forgot-password",
                    json={"email": "admin@test.local"})
    assert r.status_code == 204


def test_forgot_password_unknown_email_returns_204_by_default(client):
    """Enumeration-safe default: unknown emails get the SAME 204 as known ones."""
    r = client.post("/api/auth/forgot-password",
                    json={"email": "ghost@example.com"})
    assert r.status_code == 204


def test_forgot_password_inactive_email_returns_204_by_default(admin_client):
    """Inactive accounts still get 204 under the enumeration-safe default."""
    u = admin_client.post("/api/users", json={
        "name": "Inactive", "email": "off@test.local",
        "role": "user", "password": "Inactive99",
    }).json()
    admin_client.put(f"/api/users/{u['id']}", json={
        "name": "Inactive", "email": "off@test.local",
        "role": "user", "is_active": False,
    })
    # Log out so we hit the public endpoint as an anonymous caller.
    admin_client.post("/api/auth/logout")
    r = admin_client.post("/api/auth/forgot-password",
                          json={"email": "off@test.local"})
    assert r.status_code == 204


def test_forgot_password_enumeration_optout_returns_404(client, monkeypatch):
    """FORGOT_PASSWORD_ENUMERATION_SAFE=False restores the legacy 404 for unknown addresses."""
    import app.config as config
    monkeypatch.setattr(config.Settings, "FORGOT_PASSWORD_ENUMERATION_SAFE", False)
    r = client.post("/api/auth/forgot-password",
                    json={"email": "ghost@example.com"})
    assert r.status_code == 404
    assert "couldn't find" in r.json()["detail"].lower()


def test_audit_csv_export_follows_the_list_filters_and_defuses_formulas(admin_client):
    import csv
    import io

    _seed_audit_data(admin_client)
    admin_client.post("/api/projects", json={"name": "=HYPERLINK(\"http://evil.test\")", "color": "#ffffff"})

    res = admin_client.get("/api/audit/export.csv")
    assert res.status_code == 200
    assert res.headers["content-type"].startswith("text/csv")
    assert 'attachment; filename="audit.csv"' in res.headers["content-disposition"]
    rows = list(csv.reader(io.StringIO(res.text)))
    assert rows[0] == ["id", "created_at", "actor_user_id", "actor_name", "action",
                       "entity_type", "entity_id", "bug_id", "detail"]
    listed = admin_client.get("/api/audit?limit=10000").json()
    assert len(rows) - 1 == len(listed)
    # a cell that starts with a formula character is prefixed so a spreadsheet shows it as text
    assert not any(cell.startswith(("=", "+", "@")) for row in rows[1:] for cell in row)

    only_bugs = list(csv.reader(io.StringIO(admin_client.get("/api/audit/export.csv?entity_type=bug").text)))
    assert 1 < len(only_bugs) < len(rows)
    assert {row[5] for row in only_bugs[1:]} == {"bug"}
    assert len(list(csv.reader(io.StringIO(admin_client.get("/api/audit/export.csv?limit=1").text)))) == 2


def test_audit_csv_export_is_for_managers_and_stays_inside_the_organization(client):
    from tests.conftest import new_client, sign_up

    owner, _ = sign_up("Acme Corp", "owner@acme.test")
    other, _ = sign_up("Other Org", "owner@other.test")
    owner.post("/api/users", json={"name": "Plain", "email": "plain@acme.test", "role": "user", "password": "Passw0rd!x"})
    plain = new_client()
    plain.post("/api/auth/login", json={"email": "plain@acme.test", "password": "Passw0rd!x"})
    assert plain.get("/api/audit/export.csv").status_code == 403
    assert new_client().get("/api/audit/export.csv").status_code == 401
    assert "owner@acme.test" in owner.get("/api/audit/export.csv").text
    assert "owner@acme.test" not in other.get("/api/audit/export.csv").text
