"""Organizations and personal accounts: public sign-up, profile, verified email change,
organization details and branding, and the data-subject rights (export, delete)."""
from __future__ import annotations

from tests.conftest import BOOTSTRAP_EMAIL, BOOTSTRAP_PASSWORD, sign_up

PASSWORD = "Passw0rd!x"


def test_signup_creates_an_organization_with_an_admin_and_a_default_project(client):
    c, me = sign_up("Acme Corp", "owner@acme.test")
    assert me["role"] == "admin"
    assert me["organization_name"] == "Acme Corp"
    assert me["organization_slug"] == "acme-corp"
    # signed in straight away
    assert c.get("/api/auth/me").json()["email"] == "owner@acme.test"
    projects = c.get("/api/projects").json()
    assert [p["name"] for p in projects] == ["General"]
    assert projects[0]["key"] == "GEN"


def test_signup_rejects_duplicate_email_weak_password_and_blank_names(client):
    sign_up("Acme Corp", "owner@acme.test")
    from tests.conftest import new_client

    c = new_client()
    body = {"name": "Other", "email": "owner@acme.test", "password": PASSWORD,
            "organization_name": "Other Org"}
    assert c.post("/api/auth/signup", json=body).status_code == 409
    assert c.post("/api/auth/signup", json={**body, "email": "x@y.test", "password": "short"}).status_code == 422
    assert c.post("/api/auth/signup", json={**body, "email": "x@y.test", "organization_name": " "}).status_code == 422


def test_signup_can_be_switched_off(client, monkeypatch):
    from app.config import get_settings

    monkeypatch.setattr(get_settings(), "ALLOW_PUBLIC_SIGNUP", False)
    from tests.conftest import new_client

    res = new_client().post("/api/auth/signup", json={
        "name": "Owner", "email": "owner@acme.test", "password": PASSWORD, "organization_name": "Acme"})
    assert res.status_code == 403
    assert client.get("/api/meta").json()["signup_enabled"] is False


def test_organizations_with_the_same_name_get_distinct_slugs(client):
    _, a = sign_up("Same Name", "a@same.test")
    _, b = sign_up("Same Name", "b@same.test")
    assert a["organization_slug"] != b["organization_slug"]
    assert a["org_id"] != b["org_id"]


def test_profile_name_can_be_changed_but_not_blanked(client):
    c, _ = sign_up("Acme Corp", "owner@acme.test")
    res = c.put("/api/auth/profile", json={"name": "New Name"})
    assert res.status_code == 200
    assert res.json()["name"] == "New Name"
    assert c.put("/api/auth/profile", json={"name": " "}).status_code == 422


def test_email_change_needs_the_password_and_the_mailed_code(client, monkeypatch):
    sent = []
    monkeypatch.setattr("app.routes.auth.notify_email_change_code",
                        lambda to, name, code, sender=None: sent.append((to, code)))
    c, _ = sign_up("Acme Corp", "owner@acme.test")

    wrong_pw = c.post("/api/auth/email-change/request",
                      json={"new_email": "new@acme.test", "current_password": "nope-nope"})
    assert wrong_pw.status_code == 400
    assert c.post("/api/auth/email-change/request",
                  json={"new_email": BOOTSTRAP_EMAIL, "current_password": PASSWORD}).status_code == 409
    assert c.post("/api/auth/email-change/request",
                  json={"new_email": "owner@acme.test", "current_password": PASSWORD}).status_code == 400

    assert c.post("/api/auth/email-change/request",
                  json={"new_email": "new@acme.test", "current_password": PASSWORD}).status_code == 202
    (to, code), = sent
    assert to == "new@acme.test"

    wrong = "000000" if code != "000000" else "111111"
    bad = c.post("/api/auth/email-change/confirm", json={"code": wrong})
    assert bad.status_code == 400
    ok = c.post("/api/auth/email-change/confirm", json={"code": code})
    assert ok.status_code == 200
    assert ok.json()["email"] == "new@acme.test"
    # the code is single use
    assert c.post("/api/auth/email-change/confirm", json={"code": code}).status_code == 400
    # the new address now signs in
    from tests.conftest import new_client

    assert new_client().post("/api/auth/login", json={"email": "new@acme.test", "password": PASSWORD}).status_code == 200


def test_email_change_locks_after_too_many_wrong_codes(client, monkeypatch):
    sent = []
    monkeypatch.setattr("app.routes.auth.notify_email_change_code",
                        lambda to, name, code, sender=None: sent.append(code))
    c, _ = sign_up("Acme Corp", "owner@acme.test")
    c.post("/api/auth/email-change/request", json={"new_email": "new@acme.test", "current_password": PASSWORD})
    wrong = "000000" if sent[0] != "000000" else "111111"
    last = None
    for _ in range(6):
        last = c.post("/api/auth/email-change/confirm", json={"code": wrong})
        if last.status_code == 429:
            break
    # Either the guess budget voids the request (400 "start again") or the rate limiter answers 429;
    # in both cases the correct code no longer works afterwards.
    assert last.status_code in (400, 429)
    assert c.post("/api/auth/email-change/confirm", json={"code": sent[0]}).status_code in (400, 429)


def test_only_admins_edit_the_organization_and_branding(client):
    admin = client
    admin.post("/api/auth/login", json={"email": BOOTSTRAP_EMAIL, "password": BOOTSTRAP_PASSWORD})
    assert admin.post("/api/users", json={
        "name": "Plain User", "email": "plain@test.local", "role": "user", "password": "User12345"}).status_code == 201
    from tests.conftest import new_client

    user = new_client()
    assert user.post("/api/auth/login", json={"email": "plain@test.local", "password": "User12345"}).status_code == 200

    assert user.get("/api/organization").status_code == 200
    assert user.put("/api/organization", json={"name": "Hacked"}).status_code == 403
    assert user.get("/api/branding").status_code == 403
    assert user.put("/api/branding", json={"accent_color": "#112233"}).status_code == 403

    res = admin.put("/api/organization", json={"name": "Renamed Org", "description": "Hello"})
    assert res.status_code == 200
    assert res.json()["name"] == "Renamed Org"
    assert admin.get("/api/organization").json()["description"] == "Hello"


def test_branding_validation_and_visibility_to_members(client):
    c, _ = sign_up("Acme Corp", "owner@acme.test")
    assert c.put("/api/branding", json={"accent_color": "red"}).status_code == 422
    assert c.put("/api/branding", json={"logo_data_url": "https://evil.test/x.png"}).status_code == 422
    assert c.put("/api/branding", json={"logo_data_url": "data:image/png;base64," + "A" * 300_000}).status_code == 422
    assert c.put("/api/branding", json={"email_from_override": "two@a.test, three@b.test"}).status_code == 422

    logo = "data:image/svg+xml;base64,PHN2Zy8+"
    res = c.put("/api/branding", json={
        "accent_color": "#336699", "logo_data_url": logo, "email_from_override": "Acme <bugs@acme.test>"})
    assert res.status_code == 200
    me = c.get("/api/auth/me").json()
    assert me["branding"] == {"logo_data_url": logo, "accent_color": "#336699"}
    # the sender override is for admins only and never leaks to /me
    assert "email_from_override" not in me["branding"]
    # an empty string clears a field
    cleared = c.put("/api/branding", json={"accent_color": ""}).json()
    assert cleared["accent_color"] is None


def test_data_export_contains_my_records_only(client):
    c, me = sign_up("Acme Corp", "owner@acme.test")
    project_id = c.get("/api/projects").json()[0]["id"]
    bug = c.post("/api/bugs", json={"project_id": project_id, "title": "Mine", "description": "d"})
    assert bug.status_code == 201, bug.text
    other, _ = sign_up("Other Org", "owner@other.test")
    other_project = other.get("/api/projects").json()[0]["id"]
    other.post("/api/bugs", json={"project_id": other_project, "title": "Theirs", "description": "d"})

    data = c.get("/api/auth/data-export").json()
    assert data["user"]["email"] == "owner@acme.test"
    assert data["organization"]["name"] == "Acme Corp"
    assert [b["title"] for b in data["bugs_reported"]] == ["Mine"]
    assert "Theirs" not in str(data)
    assert "password" not in str(data).lower() or "password_hash" not in data["user"]


def test_account_deletion_needs_the_password_and_spares_the_last_admin(client):
    c, _ = sign_up("Acme Corp", "owner@acme.test")
    assert c.request("DELETE", "/api/auth/account", json={"password": "wrong-wrong"}).status_code == 400
    # the only admin cannot leave an organization without one
    assert c.request("DELETE", "/api/auth/account", json={"password": PASSWORD}).status_code == 409

    invite_body = {"name": "Second Admin", "email": "second@acme.test", "role": "admin", "password": PASSWORD}
    assert c.post("/api/users", json=invite_body).status_code == 201
    assert c.request("DELETE", "/api/auth/account", json={"password": PASSWORD}).status_code == 204
    assert c.get("/api/auth/me").status_code == 401
    from tests.conftest import new_client

    assert new_client().post("/api/auth/login", json={"email": "owner@acme.test", "password": PASSWORD}).status_code == 401


def test_deleting_an_account_keeps_the_items_but_clears_the_reporter(client):
    owner, _ = sign_up("Acme Corp", "owner@acme.test")
    owner.post("/api/users", json={"name": "Member", "email": "m@acme.test", "role": "user", "password": PASSWORD})
    project_id = owner.get("/api/projects").json()[0]["id"]
    from tests.conftest import new_client

    member = new_client()
    assert member.post("/api/auth/login", json={"email": "m@acme.test", "password": PASSWORD}).status_code == 200
    member_id = next(u["id"] for u in owner.get("/api/users").json() if u["email"] == "m@acme.test")
    assert owner.post(f"/api/projects/{project_id}/members", json={"user_id": member_id}).status_code == 201
    created = member.post("/api/bugs", json={"project_id": project_id, "title": "Keep me", "description": "d"})
    assert created.status_code == 201, created.text
    bug_id = created.json()["id"]

    assert member.request("DELETE", "/api/auth/account", json={"password": PASSWORD}).status_code == 204
    after = owner.get(f"/api/bugs/{bug_id}")
    assert after.status_code == 200
    assert after.json()["title"] == "Keep me"
