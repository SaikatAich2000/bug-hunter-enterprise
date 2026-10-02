"""Tests for the item-linking feature.
Directed links + inverse labels, idempotency, guards, per-type permission gating, removal, cascade, migration.
"""
from __future__ import annotations

import sys

from fastapi.testclient import TestClient

_PW = "User12345"
_BOOTSTRAP_PW = "Admin1234"


def _new_client() -> TestClient:
    from app.main import app
    return TestClient(app)


def _login(c: TestClient, email: str, password: str = _PW) -> None:
    c.post("/api/auth/logout")
    r = c.post("/api/auth/login", json={"email": email, "password": password})
    assert r.status_code == 200, r.text


def _mk_user(admin: TestClient, name: str, email: str, role: str = "user") -> dict:
    body = {"name": name, "email": email, "role": role, "password": _PW}
    # Tag the user to every project: both endpoints must be visible before per-type link rules apply.
    pids = [p["id"] for p in admin.get("/api/projects").json()]
    if pids:
        body["project_ids"] = pids
    r = admin.post("/api/users", json=body)
    assert r.status_code == 201, r.text
    return r.json()


def _mk_project(admin: TestClient, name: str = "Link Proj") -> dict:
    r = admin.post("/api/projects", json={"name": name})
    assert r.status_code == 201, r.text
    return r.json()


def _mk_bug(admin: TestClient, project_id: int, title: str, **extra) -> dict:
    body = {"project_id": project_id, "title": title}
    body.update(extra)
    r = admin.post("/api/bugs", json=body)
    assert r.status_code == 201, r.text
    return r.json()


# Create
def test_link_requires_auth(client):
    assert client.get("/api/bugs/1/links").status_code == 401
    assert client.post("/api/bugs/1/links", json={"target_bug_id": 2}).status_code == 401


def test_add_blocks_link_shows_inverse_on_target(admin_client):
    proj = _mk_project(admin_client)
    a = _mk_bug(admin_client, proj["id"], "A blocks B")
    b = _mk_bug(admin_client, proj["id"], "B is blocked")
    r = admin_client.post(f"/api/bugs/{a['id']}/links",
                          json={"target_bug_id": b["id"], "link_type": "blocks"})
    assert r.status_code == 201, r.text
    link = r.json()
    assert link["label"] == "blocks"
    assert link["direction"] == "outgoing"
    assert link["other_bug_id"] == b["id"]
    # The inverse label is checked from B's side.
    b_links = admin_client.get(f"/api/bugs/{b['id']}/links").json()
    assert len(b_links) == 1
    assert b_links[0]["label"] == "is blocked by"
    assert b_links[0]["direction"] == "incoming"
    assert b_links[0]["other_bug_id"] == a["id"]


def test_relates_is_symmetric_label(admin_client):
    proj = _mk_project(admin_client, "Relates")
    a = _mk_bug(admin_client, proj["id"], "Item A")
    b = _mk_bug(admin_client, proj["id"], "Item B")
    admin_client.post(f"/api/bugs/{a['id']}/links", json={"target_bug_id": b["id"], "link_type": "relates"})
    assert admin_client.get(f"/api/bugs/{a['id']}/links").json()[0]["label"] == "relates to"
    assert admin_client.get(f"/api/bugs/{b['id']}/links").json()[0]["label"] == "relates to"


def test_duplicate_link_type(admin_client):
    proj = _mk_project(admin_client, "Dup")
    a = _mk_bug(admin_client, proj["id"], "Dupe")
    b = _mk_bug(admin_client, proj["id"], "Original")
    admin_client.post(f"/api/bugs/{a['id']}/links", json={"target_bug_id": b["id"], "link_type": "duplicate"})
    assert admin_client.get(f"/api/bugs/{a['id']}/links").json()[0]["label"] == "duplicates"
    assert admin_client.get(f"/api/bugs/{b['id']}/links").json()[0]["label"] == "is duplicated by"


def test_link_is_idempotent(admin_client):
    proj = _mk_project(admin_client, "Idem")
    a = _mk_bug(admin_client, proj["id"], "Item A")
    b = _mk_bug(admin_client, proj["id"], "Item B")
    r1 = admin_client.post(f"/api/bugs/{a['id']}/links", json={"target_bug_id": b["id"], "link_type": "blocks"})
    r2 = admin_client.post(f"/api/bugs/{a['id']}/links", json={"target_bug_id": b["id"], "link_type": "blocks"})
    assert r1.json()["id"] == r2.json()["id"]
    assert len(admin_client.get(f"/api/bugs/{a['id']}/links").json()) == 1


def test_self_link_rejected(admin_client):
    proj = _mk_project(admin_client, "Self")
    a = _mk_bug(admin_client, proj["id"], "Lonely")
    r = admin_client.post(f"/api/bugs/{a['id']}/links", json={"target_bug_id": a["id"]})
    assert r.status_code == 400


def test_link_unknown_target_400(admin_client):
    proj = _mk_project(admin_client, "BadTarget")
    a = _mk_bug(admin_client, proj["id"], "Item A")
    assert admin_client.post(f"/api/bugs/{a['id']}/links", json={"target_bug_id": 99999}).status_code == 400


def test_link_missing_source_404(admin_client):
    proj = _mk_project(admin_client, "BadSource")
    b = _mk_bug(admin_client, proj["id"], "Item B")
    assert admin_client.post("/api/bugs/99999/links", json={"target_bug_id": b["id"]}).status_code == 404


def test_invalid_link_type_422(admin_client):
    proj = _mk_project(admin_client, "BadType")
    a = _mk_bug(admin_client, proj["id"], "Item A")
    b = _mk_bug(admin_client, proj["id"], "Item B")
    assert admin_client.post(f"/api/bugs/{a['id']}/links",
                             json={"target_bug_id": b["id"], "link_type": "wormhole"}).status_code == 422


# Permissions
def test_regular_user_cannot_link_a_task(admin_client):
    proj = _mk_project(admin_client, "TaskLink")
    _mk_user(admin_client, "Reg", "reg.link@test.local")
    task = _mk_bug(admin_client, proj["id"], "A task", item_type="Task")
    other = _mk_bug(admin_client, proj["id"], "Other")
    userc = _new_client()
    _login(userc, "reg.link@test.local")
    # Regular users lack edit permission on Tasks, so linking from one is denied.
    r = userc.post(f"/api/bugs/{task['id']}/links", json={"target_bug_id": other["id"]})
    assert r.status_code == 403


def test_regular_user_can_link_a_bug(admin_client):
    proj = _mk_project(admin_client, "BugLinkOK")
    _mk_user(admin_client, "Reg", "reg2.link@test.local")
    a = _mk_bug(admin_client, proj["id"], "Bug A")
    b = _mk_bug(admin_client, proj["id"], "Bug B")
    userc = _new_client()
    _login(userc, "reg2.link@test.local")
    assert userc.post(f"/api/bugs/{a['id']}/links", json={"target_bug_id": b["id"]}).status_code == 201


# Remove + detail integration + cascade
def test_remove_link_from_either_end(admin_client):
    proj = _mk_project(admin_client, "Remove")
    a = _mk_bug(admin_client, proj["id"], "Item A")
    b = _mk_bug(admin_client, proj["id"], "Item B")
    link = admin_client.post(f"/api/bugs/{a['id']}/links",
                             json={"target_bug_id": b["id"], "link_type": "blocks"}).json()
    # Delete through B's endpoint.
    assert admin_client.delete(f"/api/bugs/{b['id']}/links/{link['id']}").status_code == 200
    assert admin_client.get(f"/api/bugs/{a['id']}/links").json() == []


def test_remove_link_via_source(admin_client):
    proj = _mk_project(admin_client, "RemoveSrc")
    a = _mk_bug(admin_client, proj["id"], "Item A")
    b = _mk_bug(admin_client, proj["id"], "Item B")
    link = admin_client.post(f"/api/bugs/{a['id']}/links",
                             json={"target_bug_id": b["id"], "link_type": "blocks"}).json()
    # Delete through A's endpoint — exercises the source branch of remove_link.
    assert admin_client.delete(f"/api/bugs/{a['id']}/links/{link['id']}").status_code == 200
    assert admin_client.get(f"/api/bugs/{b['id']}/links").json() == []


def test_remove_link_wrong_bug_404(admin_client):
    proj = _mk_project(admin_client, "WrongBug")
    a = _mk_bug(admin_client, proj["id"], "Item A")
    b = _mk_bug(admin_client, proj["id"], "Item B")
    c = _mk_bug(admin_client, proj["id"], "C unrelated")
    link = admin_client.post(f"/api/bugs/{a['id']}/links", json={"target_bug_id": b["id"]}).json()
    # C is not an endpoint of this link, so both deletes return 404.
    assert admin_client.delete(f"/api/bugs/{c['id']}/links/{link['id']}").status_code == 404
    assert admin_client.delete(f"/api/bugs/{a['id']}/links/99999").status_code == 404


def test_links_appear_in_detail(admin_client):
    proj = _mk_project(admin_client, "Detail")
    a = _mk_bug(admin_client, proj["id"], "Item A")
    b = _mk_bug(admin_client, proj["id"], "Item B")
    admin_client.post(f"/api/bugs/{a['id']}/links", json={"target_bug_id": b["id"], "link_type": "blocks"})
    detail = admin_client.get(f"/api/bugs/{a['id']}").json()
    assert len(detail["links"]) == 1
    assert detail["links"][0]["other_bug_title"] == "Item B"


def test_link_cascades_on_bug_delete(admin_client):
    proj = _mk_project(admin_client, "Cascade")
    a = _mk_bug(admin_client, proj["id"], "Item A")
    b = _mk_bug(admin_client, proj["id"], "Item B")
    admin_client.post(f"/api/bugs/{a['id']}/links", json={"target_bug_id": b["id"], "link_type": "blocks"})
    # Deleting B should cascade and leave A's link list empty.
    assert admin_client.delete(f"/api/bugs/{b['id']}").status_code == 200
    assert admin_client.get(f"/api/bugs/{a['id']}/links").json() == []


def test_list_links_missing_bug_404(admin_client):
    assert admin_client.get("/api/bugs/99999/links").status_code == 404


# Additive schema
def test_bug_links_table_is_additive_and_idempotent(tmp_path, monkeypatch):
    db_file = tmp_path / "links_schema.db"
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{db_file}")
    monkeypatch.setenv("EMAIL_BACKEND", "disabled")
    monkeypatch.setenv("SESSION_SECRET", "x" * 32)
    monkeypatch.setenv("BOOTSTRAP_ADMIN_EMAIL", "a@a.local")
    monkeypatch.setenv("BOOTSTRAP_ADMIN_PASSWORD", _BOOTSTRAP_PW)
    for mod in list(sys.modules):
        if mod == "app" or mod.startswith("app."):
            del sys.modules[mod]
    from app.config import get_settings
    get_settings.cache_clear()  # type: ignore[attr-defined]
    from sqlalchemy import inspect

    from app.database import engine, init_db

    init_db()
    assert "bug_links" in inspect(engine).get_table_names()
    init_db()
    assert "bug_links" in inspect(engine).get_table_names()
