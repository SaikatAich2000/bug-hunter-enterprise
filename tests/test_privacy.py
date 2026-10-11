"""B.6 privacy: complete data export, organization deletion, and retention of stale rows."""
from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone

from sqlalchemy import func, select

from tests.conftest import new_client, sign_up
from tests.test_saved_views_custom_fields import _add_user, _login

PASSWORD = "Passw0rd!x"
NOW = datetime(2026, 10, 1, tzinfo=timezone.utc)


def _user_id(email):
    from app.database import SessionLocal
    from app.models import User

    with SessionLocal() as db:
        return db.scalar(select(User.id).where(User.email == email))


def _seed_personal_data(c, email):
    """One row of everything the export promises, for the user ``email`` of the client ``c``."""
    from app.database import SessionLocal
    from app.models import (
        ChatConversation,
        ChatMessage,
        EmailChangeRequest,
        Invitation,
        Notification,
        User,
    )

    pid = c.get("/api/projects").json()[0]["id"]
    fid = c.post(f"/api/projects/{pid}/custom-fields", json={"name": "Tier", "field_type": "text"}).json()["id"]
    bug = c.post("/api/bugs", json={"project_id": pid, "title": "Mine to export",
                                    "custom_values": [{"field_id": fid, "value": "Gold"}]}).json()
    c.post("/api/saved-views", json={"name": "My view", "filters": {"status": "New"}})
    assert c.post("/api/devices/register", json={"token": "t" * 30 + email, "platform": "android"}).status_code == 200
    assert c.put("/api/notifications/preferences", json={"mentions": False}).status_code == 200
    uid = _user_id(email)
    with SessionLocal() as db:
        org_id = db.get(User, uid).org_id
        conv = ChatConversation(user_id=uid)
        conv.messages.append(ChatMessage(role="user", content=f"hello from {email}"))
        db.add_all([
            conv,
            Notification(user_id=uid, kind="updated", title=f"note for {email}", body=""),
            Invitation(org_id=org_id, email=f"invitee-of-{email}", role="user", token_hash=f"h-{email}",
                       invited_by_user_id=uid, invited_by_name="x", expires_at=NOW + timedelta(days=3650)),
            EmailChangeRequest(user_id=uid, new_email=f"new-{email}", code_hash="c", expires_at=NOW + timedelta(days=3650)),
        ])
        db.commit()
    return bug["id"]


def test_data_export_covers_chat_notifications_devices_preferences_invitations_views_and_values(client):
    a, _ = sign_up("Acme Corp", "owner@acme.test")
    b, _ = sign_up("Beta Corp", "owner@beta.test")
    bug_a = _seed_personal_data(a, "owner@acme.test")
    _seed_personal_data(b, "owner@beta.test")

    data = a.get("/api/auth/data-export").json()
    assert [m["content"] for conv in data["chat"] for m in conv["messages"]] == ["hello from owner@acme.test"]
    assert [n["title"] for n in data["notifications"] if n["kind"] == "updated"] == ["note for owner@acme.test"]
    assert len(data["devices"]) == 1 and data["devices"][0]["platform"] == "android"
    assert "token" not in data["devices"][0]  # a push token is a credential
    assert data["notification_preferences"][0]["mentions"] is False
    assert [i["email"] for i in data["invitations_sent"]] == ["invitee-of-owner@acme.test"]
    assert "token_hash" not in data["invitations_sent"][0]
    assert [r["new_email"] for r in data["email_change_requests"]] == ["new-owner@acme.test"]
    assert data["saved_views"][0]["filters"] and data["saved_views"][0]["name"] == "My view"
    assert data["custom_values"] == [{"bug_id": bug_a, "field": "Tier", "value": "Gold"}]
    # nothing of the other organization leaks into the export
    blob = str(data)
    assert "beta.test" not in blob and "Beta Corp" not in blob


def test_export_of_one_user_never_includes_a_colleagues_personal_rows(client):
    owner, _ = sign_up("Acme Corp", "owner@acme.test")
    _add_user(owner, "plain@acme.test")
    _seed_personal_data(owner, "owner@acme.test")
    plain = _login("plain@acme.test")
    data = plain.get("/api/auth/data-export").json()
    for key in ("chat", "devices", "invitations_sent", "email_change_requests", "saved_views"):
        assert data[key] == [], key


# ---- organization deletion ------------------------------------------------------------------

def _counts():
    from app.database import SessionLocal
    from app.models import (
        Activity,
        Bug,
        Comment,
        Invitation,
        Organization,
        Project,
        SavedView,
        User,
        Webhook,
    )

    with SessionLocal() as db:
        return {m.__name__: db.scalar(select(func.count()).select_from(m))
                for m in (Organization, User, Project, Bug, Comment, Webhook, Invitation, SavedView, Activity)}


def _populate(c, email):
    _seed_personal_data(c, email)
    pid = c.get("/api/projects").json()[0]["id"]
    bug_id = c.get("/api/bugs").json()["items"][0]["id"]
    assert c.post(f"/api/bugs/{bug_id}/comments", json={"body": "a comment"}).status_code == 201
    assert c.post("/api/webhooks", json={"name": "H", "url": "https://hooks.example.com/x"}).status_code == 201
    return pid


def test_only_admins_can_see_the_dry_run_or_delete_the_organization(client):
    owner, _ = sign_up("Acme Corp", "owner@acme.test")
    pid = owner.get("/api/projects").json()[0]["id"]
    _add_user(owner, "mgr@acme.test", "manager")
    lead_id = _add_user(owner, "lead@acme.test")
    owner.post(f"/api/projects/{pid}/members", json={"user_id": lead_id, "role": "lead"})
    _add_user(owner, "plain@acme.test")
    body = {"password": PASSWORD, "confirm_name": "Acme Corp"}
    for email in ("mgr@acme.test", "lead@acme.test", "plain@acme.test"):
        c = _login(email)
        assert c.get("/api/organization/deletion-summary").status_code == 403, email
        assert c.post("/api/organization/delete", json=body).status_code == 403, email
    assert new_client().post("/api/organization/delete", json=body).status_code == 401
    assert _counts()["Organization"] >= 1 and _user_id("plain@acme.test")


def test_deletion_needs_the_password_and_the_typed_name_and_the_dry_run_changes_nothing(client):
    owner, _ = sign_up("Acme Corp", "owner@acme.test")
    _populate(owner, "owner@acme.test")
    before = _counts()
    summary = owner.get("/api/organization/deletion-summary").json()
    assert summary["organization"] == "Acme Corp"
    assert summary["will_delete"]["users"] == 1 and summary["will_delete"]["items"] == 1
    assert summary["will_delete"]["comments"] == 1 and summary["will_delete"]["webhooks"] == 1
    assert _counts() == before
    for body in ({"password": "wrong-Pass1!", "confirm_name": "Acme Corp"},
                 {"password": PASSWORD, "confirm_name": "acme corp"},
                 {"password": PASSWORD, "confirm_name": "Acme"}):
        assert owner.post("/api/organization/delete", json=body).status_code == 400
    assert _counts() == before


def test_deleting_an_organization_removes_all_of_it_and_none_of_another(client, caplog):
    a, _ = sign_up("Acme Corp", "owner@acme.test")
    b, _ = sign_up("Beta Corp", "owner@beta.test")
    _add_user(a, "plain@acme.test")
    _populate(a, "owner@acme.test")
    _populate(b, "owner@beta.test")
    acme_only = _counts()

    # Beta's admin cannot delete Acme by naming it: only their own name confirms.
    assert b.post("/api/organization/delete", json={"password": PASSWORD, "confirm_name": "Acme Corp"}).status_code == 400
    with caplog.at_level(logging.WARNING, logger="bug_hunter.audit"):
        assert a.post("/api/organization/delete", json={"password": PASSWORD, "confirm_name": "Acme Corp"}).status_code == 204
    assert any("organization_deleted" in r.message and "Acme Corp" in r.message for r in caplog.records)

    assert a.get("/api/auth/me").status_code == 401
    assert new_client().post("/api/auth/login", json={"email": "plain@acme.test", "password": PASSWORD}).status_code == 401
    after = _counts()
    assert after["Organization"] == acme_only["Organization"] - 1
    # B kept every row it had: each B-owned count equals what the world minus Acme would hold
    assert b.get("/api/auth/me").json()["email"] == "owner@beta.test"
    data = b.get("/api/auth/data-export").json()
    assert len(data["bugs_reported"]) == 1 and len(data["chat"]) == 1 and len(data["saved_views"]) == 1
    assert len(b.get("/api/webhooks").json()) == 1
    from app.database import SessionLocal
    from app.models import Bug, Project, User

    with SessionLocal() as db:
        assert db.scalar(select(func.count()).select_from(User).where(User.email.like("%@acme.test"))) == 0
        assert db.scalar(select(func.count()).select_from(Project).where(Project.name == "Acme Corp")) == 0
        assert db.scalar(select(func.count()).select_from(Bug).where(Bug.title == "Mine to export")) == 1  # Beta's


# ---- retention ------------------------------------------------------------------------------

def test_retention_removes_expired_invitations_and_stale_email_changes_in_every_org(client):
    from app.database import SessionLocal
    from app.jobs.retention import INVITATION_GRACE_DAYS, run_retention
    from app.models import EmailChangeRequest, Invitation, User

    a, _ = sign_up("Acme Corp", "owner@acme.test")
    b, _ = sign_up("Beta Corp", "owner@beta.test")
    long_ago = NOW - timedelta(days=INVITATION_GRACE_DAYS + 5)
    with SessionLocal() as db:
        for email in ("owner@acme.test", "owner@beta.test"):
            user = db.scalar(select(User).where(User.email == email))
            for tag, expires in (("stale", long_ago), ("recent", NOW - timedelta(days=1)), ("live", NOW + timedelta(days=5))):
                db.add(Invitation(org_id=user.org_id, email=f"{tag}@{email}", role="user", token_hash=f"{tag}{email}",
                                  invited_by_name="x", expires_at=expires))
            db.add_all([
                EmailChangeRequest(user_id=user.id, new_email=f"old-{email}", code_hash="1", expires_at=NOW - timedelta(hours=1)),
                EmailChangeRequest(user_id=user.id, new_email=f"used-{email}", code_hash="2", expires_at=NOW + timedelta(hours=1), used_at=NOW),
                EmailChangeRequest(user_id=user.id, new_email=f"open-{email}", code_hash="3", expires_at=NOW + timedelta(hours=1)),
            ])
        db.commit()
        stats = run_retention(db, now=NOW)
        assert stats["invitations"] == 2 and stats["email_change_requests"] == 4
        assert sorted(i.email.split("@")[0] for i in db.scalars(select(Invitation)).all()) == ["live"] * 2 + ["recent"] * 2
        assert sorted(r.new_email.split("-")[0] for r in db.scalars(select(EmailChangeRequest)).all()) == ["open", "open"]


def test_audit_retention_deletes_in_id_chunks(admin_client, monkeypatch):
    from sqlalchemy import event
    from sqlalchemy.orm import Session

    from app.database import SessionLocal
    from app.jobs import audit_retention, retention
    from app.models import Activity, User

    monkeypatch.setattr(retention, "BATCH_SIZE", 3)
    old = datetime.now(timezone.utc) - timedelta(days=400)
    with SessionLocal() as db:
        uid = db.scalar(select(User.id))
        db.add_all(Activity(entity_type="x", entity_id=i, actor_user_id=uid, actor_name="t",
                            action="old", detail="", created_at=old) for i in range(8))
        db.add(Activity(entity_type="x", entity_id=99, actor_user_id=uid, actor_name="t", action="fresh", detail=""))
        db.commit()
        commits = []
        listener = lambda _s: commits.append(1)  # noqa: E731
        event.listen(Session, "after_commit", listener)
        try:
            assert audit_retention.purge_expired(db, 365) == 8
        finally:
            event.remove(Session, "after_commit", listener)
        assert len(commits) == 3  # 3 + 3 + 2 rows: one short transaction per chunk
        assert db.scalar(select(func.count()).select_from(Activity).where(Activity.action == "fresh")) == 1
        assert db.scalar(select(func.count()).select_from(Activity).where(Activity.action == "old")) == 0
