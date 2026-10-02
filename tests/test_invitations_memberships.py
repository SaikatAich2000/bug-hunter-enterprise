"""Invitations (join an organization by emailed link) and project memberships (lead / member)."""
from __future__ import annotations

import pytest

from tests.conftest import new_client, sign_up

PASSWORD = "Passw0rd!x"


@pytest.fixture
def mailbox(monkeypatch):
    """Capture invitation emails instead of sending them."""
    sent = []
    monkeypatch.setattr(
        "app.routes.invitations.notify_invitation",
        lambda to, inviter, org, url, role, sender=None: sent.append({"to": to, "url": url, "org": org, "role": role}),
    )
    return sent


def _token(mail):
    return mail["url"].rsplit("token=", 1)[1]


def _invite(c, email, **extra):
    return c.post("/api/invitations", json={"email": email, **extra})


def _accept(token, name="Invitee", password=PASSWORD):
    c = new_client()
    res = c.post("/api/invitations/accept", json={"token": token, "name": name, "password": password})
    return c, res


def _add_user(c, email, role):
    res = c.post("/api/users", json={"name": email.split("@")[0], "email": email, "role": role, "password": PASSWORD})
    assert res.status_code == 201, res.text
    return res.json()["id"]


def _login(email):
    c = new_client()
    assert c.post("/api/auth/login", json={"email": email, "password": PASSWORD}).status_code == 200
    return c


def test_invitation_lifecycle_preview_accept_and_single_use(client, mailbox):
    owner, me = sign_up("Acme Corp", "owner@acme.test")
    res = _invite(owner, "new@acme.test", role="user")
    assert res.status_code == 201, res.text
    (mail,) = mailbox
    assert mail["to"] == "new@acme.test"
    assert mail["org"] == "Acme Corp"
    token = _token(mail)
    # the raw token is never stored or returned
    assert token not in res.text
    assert owner.get("/api/invitations").json()[0]["email"] == "new@acme.test"

    preview = new_client().get(f"/api/invitations/preview/{token}").json()
    assert preview["organization_name"] == "Acme Corp"
    assert preview["email"] == "new@acme.test"

    c, accepted = _accept(token)
    assert accepted.status_code == 200, accepted.text
    body = accepted.json()
    assert body["org_id"] == me["org_id"]
    assert body["role"] == "user"
    assert c.get("/api/auth/me").status_code == 200

    # used once
    assert new_client().get(f"/api/invitations/preview/{token}").status_code == 400
    assert _accept(token)[1].status_code == 400


def test_unknown_and_revoked_tokens_are_refused(client, mailbox):
    owner, _ = sign_up("Acme Corp", "owner@acme.test")
    assert new_client().get("/api/invitations/preview/not-a-token").status_code == 404
    assert _accept("not-a-token")[1].status_code == 404

    inv_id = _invite(owner, "new@acme.test").json()["id"]
    token = _token(mailbox[0])
    assert owner.delete(f"/api/invitations/{inv_id}").status_code == 200
    assert owner.delete(f"/api/invitations/{inv_id}").json() == {"message": "Already revoked"}
    assert _accept(token)[1].status_code == 400


def test_a_new_invitation_replaces_the_pending_one(client, mailbox):
    owner, _ = sign_up("Acme Corp", "owner@acme.test")
    _invite(owner, "new@acme.test")
    _invite(owner, "new@acme.test")
    first, second = _token(mailbox[0]), _token(mailbox[1])
    assert _accept(first)[1].status_code == 400
    assert _accept(second)[1].status_code == 200


def test_an_expired_invitation_is_refused(client, mailbox):
    from datetime import datetime, timedelta, timezone

    from app.database import SessionLocal
    from app.models import Invitation

    owner, _ = sign_up("Acme Corp", "owner@acme.test")
    _invite(owner, "new@acme.test")
    with SessionLocal() as db:
        inv = db.query(Invitation).one()
        inv.expires_at = datetime.now(timezone.utc) - timedelta(minutes=1)
        db.commit()
    assert _accept(_token(mailbox[0]))[1].status_code == 400


def test_existing_emails_cannot_be_invited_anywhere(client, mailbox):
    owner, _ = sign_up("Acme Corp", "owner@acme.test")
    sign_up("Other Org", "taken@other.test")
    assert _invite(owner, "owner@acme.test").status_code == 409
    assert _invite(owner, "taken@other.test").status_code == 409
    assert mailbox == []


def test_invitations_are_for_admins_and_managers_and_scoped_to_projects(client, mailbox):
    owner, _ = sign_up("Acme Corp", "owner@acme.test")
    project_id = owner.get("/api/projects").json()[0]["id"]
    _add_user(owner, "plain@acme.test", "user")
    manager_id = _add_user(owner, "boss@acme.test", "manager")

    plain = _login("plain@acme.test")
    assert _invite(plain, "x@acme.test").status_code == 403
    assert plain.get("/api/invitations").status_code == 403

    boss = _login("boss@acme.test")
    # a manager may not mint admins, nor attach projects they do not lead
    assert _invite(boss, "x@acme.test", role="admin").status_code == 403
    assert _invite(boss, "x@acme.test", project_ids=[project_id]).status_code == 403
    assert _invite(boss, "x@acme.test", project_ids=[999999]).status_code == 400

    assert owner.post(f"/api/projects/{project_id}/members",
                      json={"user_id": manager_id, "role": "lead"}).status_code == 201
    assert _invite(boss, "x@acme.test", project_ids=[project_id]).status_code == 201


def test_accepting_joins_the_listed_projects_as_member_or_lead(client, mailbox):
    owner, _ = sign_up("Acme Corp", "owner@acme.test")
    project_id = owner.get("/api/projects").json()[0]["id"]
    _invite(owner, "member@acme.test", project_ids=[project_id])
    _invite(owner, "lead@acme.test", project_ids=[project_id], as_lead=True)
    assert _accept(_token(mailbox[0]))[1].status_code == 200
    assert _accept(_token(mailbox[1]))[1].status_code == 200

    roles = {m["user_email"]: m["project_role"] for m in owner.get(f"/api/projects/{project_id}/members").json()}
    assert roles["member@acme.test"] == "member"
    assert roles["lead@acme.test"] == "lead"


def test_invitations_never_cross_organizations(client, mailbox):
    a, _ = sign_up("Acme Corp", "owner@acme.test")
    b, _ = sign_up("Other Org", "owner@other.test")
    inv_id = _invite(a, "new@acme.test").json()["id"]
    assert b.get("/api/invitations").json() == []
    assert b.delete(f"/api/invitations/{inv_id}").status_code == 404
    foreign_project = b.get("/api/projects").json()[0]["id"]
    assert _invite(a, "x@acme.test", project_ids=[foreign_project]).status_code == 400


def test_members_can_be_added_changed_and_removed_by_admins_and_leads(client):
    owner, _ = sign_up("Acme Corp", "owner@acme.test")
    project_id = owner.get("/api/projects").json()[0]["id"]
    lead_id = _add_user(owner, "lead@acme.test", "user")
    member_id = _add_user(owner, "member@acme.test", "user")
    base = f"/api/projects/{project_id}/members"

    assert owner.post(base, json={"user_id": lead_id, "role": "lead"}).status_code == 201
    assert owner.post(base, json={"user_id": lead_id}).status_code == 409
    assert owner.post(base, json={"user_id": member_id, "role": "owner"}).status_code == 422

    lead = _login("lead@acme.test")
    assert lead.post(base, json={"user_id": member_id}).status_code == 201
    listing = lead.get(base).json()
    assert [m["project_role"] for m in listing][0] == "lead"
    assert lead.put(f"{base}/{member_id}", json={"role": "lead"}).json()["project_role"] == "lead"
    assert lead.put(f"{base}/{member_id}", json={"role": "member"}).status_code == 200

    member = _login("member@acme.test")
    assert member.post(base, json={"user_id": lead_id}).status_code == 403
    assert member.delete(f"{base}/{lead_id}").status_code == 403
    assert member.get(base).status_code == 200

    assert lead.delete(f"{base}/{member_id}").status_code == 200
    assert lead.delete(f"{base}/{member_id}").status_code == 404


def test_the_last_lead_cannot_be_removed_or_demoted(client):
    owner, _ = sign_up("Acme Corp", "owner@acme.test")
    project_id = owner.get("/api/projects").json()[0]["id"]
    lead_id = _add_user(owner, "lead@acme.test", "user")
    base = f"/api/projects/{project_id}/members"
    owner.post(base, json={"user_id": lead_id, "role": "lead"})
    assert owner.put(f"{base}/{lead_id}", json={"role": "member"}).status_code == 400
    assert owner.delete(f"{base}/{lead_id}").status_code == 400


def test_foreign_and_disabled_users_cannot_become_members(client):
    owner, _ = sign_up("Acme Corp", "owner@acme.test")
    other, other_me = sign_up("Other Org", "owner@other.test")
    project_id = owner.get("/api/projects").json()[0]["id"]
    base = f"/api/projects/{project_id}/members"
    assert owner.post(base, json={"user_id": other_me["id"]}).status_code == 400
    # the other organization cannot even see this project
    assert other.get(base).status_code == 404
    assert other.post(base, json={"user_id": other_me["id"]}).status_code == 404

    gone_id = _add_user(owner, "gone@acme.test", "user")
    assert owner.put(f"/api/users/{gone_id}", json={"is_active": False}).status_code == 200
    assert owner.post(base, json={"user_id": gone_id}).status_code == 400


def test_project_listing_reports_role_and_member_count(client):
    owner, _ = sign_up("Acme Corp", "owner@acme.test")
    project_id = owner.get("/api/projects").json()[0]["id"]
    lead_id = _add_user(owner, "lead@acme.test", "user")
    owner.post(f"/api/projects/{project_id}/members", json={"user_id": lead_id, "role": "lead"})

    lead = _login("lead@acme.test")
    (project,) = lead.get("/api/projects").json()
    assert project["can_manage"] is True
    assert project["member_count"] == 1
    assert owner.get("/api/projects").json()[0]["can_manage"] is True


def test_project_list_costs_the_same_number_of_queries_however_many_projects(client):
    from sqlalchemy import event

    from app.database import engine

    owner, _ = sign_up("Acme Corp", "owner@acme.test")
    lead_id = _add_user(owner, "lead@acme.test", "user")
    lead = _login("lead@acme.test")

    def queries_for_listing():
        seen = []

        def count(_conn, _cursor, statement, *_rest):
            seen.append(statement)

        event.listen(engine, "before_cursor_execute", count)
        try:
            assert lead.get("/api/projects").status_code == 200
        finally:
            event.remove(engine, "before_cursor_execute", count)
        return len(seen)

    first = owner.post("/api/projects", json={"name": "Project 0", "color": "#112233"}).json()["id"]
    owner.post(f"/api/projects/{first}/members", json={"user_id": lead_id, "role": "lead"})
    few = queries_for_listing()
    for n in range(1, 13):
        pid = owner.post("/api/projects", json={"name": f"Project {n}", "color": "#112233"}).json()["id"]
        owner.post(f"/api/projects/{pid}/members", json={"user_id": lead_id, "role": "lead" if n % 2 else "member"})
    assert len(lead.get("/api/projects").json()) == 13
    assert queries_for_listing() == few
