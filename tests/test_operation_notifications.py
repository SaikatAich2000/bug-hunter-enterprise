"""Notification coverage for the remaining mutating bug operations (links, attachments, comment edit/delete, deletes).
Assertions query the Notification table directly; each test uses a unique assignee email.
"""
from __future__ import annotations

BOOTSTRAP_EMAIL = "admin@test.local"
_PW = "User12345"  # shared test credential


# Helpers
def _session():
    from app.database import SessionLocal
    return SessionLocal()


def _user_id(db, email: str) -> int:
    from sqlalchemy import select

    from app.models import User
    return db.scalar(select(User).where(User.email == email)).id


def _notifs_for(db, user_id: int):
    from sqlalchemy import select

    from app.models import Notification
    return list(db.scalars(
        select(Notification).where(Notification.user_id == user_id)
    ).all())


def _titles_containing(db, user_id: int, needle: str):
    return [n for n in _notifs_for(db, user_id) if needle in n.title]


def _setup(admin_client, suffix: str):
    """Create a project, a fresh assignee (Bob), and a bug assigned to Bob; returns (project, bob, bug)."""
    proj = admin_client.post("/api/projects", json={"name": f"Op Proj {suffix}"}).json()
    bob = admin_client.post("/api/users", json={
        "name": "Bob", "email": f"bob_{suffix}@op.test",
        "role": "user", "password": _PW,
    }).json()
    bug = admin_client.post("/api/bugs", json={
        "project_id": proj["id"], "title": f"Op bug {suffix}",
        "priority": "Medium", "environment": "DEV", "assignee_ids": [bob["id"]],
    }).json()
    return proj, bob, bug


# Links
def test_link_add_notifies_assignee_not_actor(admin_client):
    proj, bob, bug = _setup(admin_client, "linkadd")
    other = admin_client.post("/api/bugs", json={
        "project_id": proj["id"], "title": "Link target",
    }).json()

    r = admin_client.post(f"/api/bugs/{bug['id']}/links", json={
        "target_bug_id": other["id"], "link_type": "relates",
    })
    assert r.status_code == 201, r.text

    db = _session()
    assert _titles_containing(db, bob["id"], "Link added"), "assignee not notified of link"
    admin_id = _user_id(db, BOOTSTRAP_EMAIL)
    assert not _titles_containing(db, admin_id, "Link added"), "actor should not self-notify"


def test_link_remove_notifies_assignee(admin_client):
    proj, bob, bug = _setup(admin_client, "linkrm")
    other = admin_client.post("/api/bugs", json={
        "project_id": proj["id"], "title": "Link target 2",
    }).json()
    link = admin_client.post(f"/api/bugs/{bug['id']}/links", json={
        "target_bug_id": other["id"], "link_type": "blocks",
    }).json()

    r = admin_client.delete(f"/api/bugs/{bug['id']}/links/{link['id']}")
    assert r.status_code == 200, r.text

    db = _session()
    assert _titles_containing(db, bob["id"], "Link removed"), "assignee not notified of unlink"


# Attachments
def test_attachment_add_notifies_assignee(admin_client):
    _proj, bob, bug = _setup(admin_client, "attadd")
    r = admin_client.post(
        f"/api/bugs/{bug['id']}/attachments",
        files={"file": ("note.txt", b"hello world", "text/plain")},
    )
    assert r.status_code == 201, r.text

    db = _session()
    assert _titles_containing(db, bob["id"], "Attachment added"), "assignee not notified of attach"


def test_attachment_delete_notifies_assignee(admin_client):
    _proj, bob, bug = _setup(admin_client, "attdel")
    att = admin_client.post(
        f"/api/bugs/{bug['id']}/attachments",
        files={"file": ("note.txt", b"bye", "text/plain")},
    ).json()

    r = admin_client.delete(f"/api/bugs/{bug['id']}/attachments/{att['id']}")
    assert r.status_code == 200, r.text

    db = _session()
    assert _titles_containing(db, bob["id"], "Attachment removed"), "assignee not notified of detach"


# Comment edit / delete (comment ADD is covered in test_notifications.py)
def test_comment_edit_notifies_assignee(admin_client):
    _proj, bob, bug = _setup(admin_client, "cedit")
    c = admin_client.post(f"/api/bugs/{bug['id']}/comments",
                          json={"body": "<p>first</p>"}).json()

    r = admin_client.put(f"/api/bugs/{bug['id']}/comments/{c['id']}",
                         json={"body": "<p>edited</p>"})
    assert r.status_code == 200, r.text

    db = _session()
    assert _titles_containing(db, bob["id"], "Comment edited"), "assignee not notified of edit"


def test_comment_delete_notifies_assignee(admin_client):
    _proj, bob, bug = _setup(admin_client, "cdel")
    c = admin_client.post(f"/api/bugs/{bug['id']}/comments",
                          json={"body": "<p>to be gone</p>"}).json()

    r = admin_client.delete(f"/api/bugs/{bug['id']}/comments/{c['id']}")
    assert r.status_code == 200, r.text

    db = _session()
    assert _titles_containing(db, bob["id"], "Comment deleted"), "assignee not notified of delete"


# Bug delete — notification must outlive the deleted row
def test_bug_delete_notifies_assignee_and_survives(admin_client):
    _proj, bob, bug = _setup(admin_client, "bugdel")

    r = admin_client.delete(f"/api/bugs/{bug['id']}")
    assert r.status_code == 200, r.text

    db = _session()
    deleted = [n for n in _notifs_for(db, bob["id"]) if "deleted" in n.title.lower()]
    assert deleted, "assignee not notified of bug delete"
    # bug_id is set to NULL so the FK cascade doesn't erase the notification.
    assert deleted[0].bug_id is None


# Digest — operations must be batched into the daily email
def test_new_operation_is_batched_into_digest(admin_client, monkeypatch):
    from app.config import get_settings
    # With digest mode on, notify() leaves emailed_at NULL and the job sends the email.
    monkeypatch.setattr(get_settings(), "EMAIL_DIGEST_ENABLED", True)

    sent: list[tuple[str, list[str], str]] = []
    monkeypatch.setattr(
        "app.email_service.deliver",
        lambda subject, to, body, from_address=None: sent.append((subject, to, body)),
    )

    proj, bob, bug = _setup(admin_client, "digest")
    other = admin_client.post("/api/bugs", json={
        "project_id": proj["id"], "title": "Digest link target",
    }).json()
    r = admin_client.post(f"/api/bugs/{bug['id']}/links", json={
        "target_bug_id": other["id"], "link_type": "relates",
    })
    assert r.status_code == 201, r.text

    from app.jobs.email_digest import run_digest
    db = _session()
    stats = run_digest(db)
    db.commit()
    assert stats["emails_sent"] >= 1

    bob_emails = [body for _subj, to, body in sent if bob["email"] in to]
    assert bob_emails, f"no digest email reached the assignee; sent={sent}"
    assert any("Link added" in body for body in bob_emails), bob_emails


# Bulk delete — was previously silent; must behave the same as single delete
def test_bulk_delete_notifies_assignee_and_survives(admin_client):
    _proj, bob, bug = _setup(admin_client, "bulkdel")

    r = admin_client.post("/api/bugs/bulk", json={
        "action": "delete", "ids": [bug["id"]],
    })
    assert r.status_code == 200, r.text
    assert r.json()["updated"] == 1

    db = _session()
    deleted = [n for n in _notifs_for(db, bob["id"]) if "deleted" in n.title.lower()]
    assert deleted, "assignee not notified of bulk delete"
    # bug_id is NULL (link_bug=False) so the FK cascade doesn't erase the row.
    assert deleted[0].bug_id is None


# Both endpoints notified: a 'blocks' link affects the target's assignees too.
def test_link_add_notifies_both_endpoint_assignees(admin_client):
    proj, bob, bug = _setup(admin_client, "linkboth")
    carol = admin_client.post("/api/users", json={
        "name": "Carol", "email": "carol_linkboth@op.test",
        "role": "user", "password": _PW,
    }).json()
    target = admin_client.post("/api/bugs", json={
        "project_id": proj["id"], "title": "Target with owner",
        "priority": "Medium", "environment": "DEV", "assignee_ids": [carol["id"]],
    }).json()

    r = admin_client.post(f"/api/bugs/{bug['id']}/links", json={
        "target_bug_id": target["id"], "link_type": "blocks",
    })
    assert r.status_code == 201, r.text

    db = _session()
    assert _titles_containing(db, bob["id"], "Link added"), "source assignee not notified"
    assert _titles_containing(db, carol["id"], "Link added"), "target assignee not notified"
