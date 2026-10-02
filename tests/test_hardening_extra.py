"""Hardening tests: detail caps, email redaction, partial updates, image limits, concurrency, defang.
DB tests use the fixtures' temp SQLite; app.* imports live inside test bodies.
"""
from __future__ import annotations

import io
import logging
import sys

from tests.conftest import default_org_id


# Small API helpers
def _make_project(client, name="V31Proj"):
    r = client.post("/api/projects", json={"name": name, "color": "#123456"})
    assert r.status_code == 201, r.text
    return r.json()


def _make_item(client, project_id, **extra):
    body = {"title": "v31 item", "project_id": project_id,
            "priority": "High", "environment": "DEV", "item_type": "Bug"}
    body.update(extra)
    r = client.post("/api/bugs", json=body)
    assert r.status_code == 201, r.text
    return r.json()


def _become_regular_user(admin_client, email="linker@test.local"):
    """Create a regular user (as admin), add them to all projects, and re-login as them."""
    pids = [p["id"] for p in admin_client.get("/api/projects").json()]
    body = {"name": "Linker", "email": email, "role": "user", "password": "User12345"}
    if pids:
        body["project_ids"] = pids
    r = admin_client.post("/api/users", json=body)
    assert r.status_code == 201, r.text
    admin_client.post("/api/auth/logout")
    r = admin_client.post("/api/auth/login", json={"email": email, "password": "User12345"})
    assert r.status_code == 200, r.text


# Detail caps: attachments, activity, and comments share the same bounding logic.
def test_get_bug_attachments_capped_but_count_exact(admin_client, monkeypatch):
    import app.routes.bugs as bugs_mod
    monkeypatch.setattr(bugs_mod, "_DETAIL_ATTACHMENTS_MAX", 1)
    p = _make_project(admin_client)
    item = _make_item(admin_client, p["id"])
    for n in ("a.txt", "b.txt"):
        r = admin_client.post(f"/api/bugs/{item['id']}/attachments",
                              files={"file": (n, b"hello", "text/plain")})
        assert r.status_code == 201, r.text
    body = admin_client.get(f"/api/bugs/{item['id']}").json()
    assert len(body["attachments"]) == 1          # capped
    assert body["attachment_count"] == 2          # exact total unaffected


def test_activity_list_endpoint_capped(admin_client, monkeypatch):
    import app.routes.bugs as bugs_mod
    monkeypatch.setattr(bugs_mod, "_DETAIL_ACTIVITIES_MAX", 1)
    p = _make_project(admin_client)
    item = _make_item(admin_client, p["id"])
    admin_client.put(f"/api/bugs/{item['id']}", json={"status": "In Progress"})
    admin_client.put(f"/api/bugs/{item['id']}", json={"status": "Resolved"})
    rows = admin_client.get(f"/api/bugs/{item['id']}/activity").json()
    assert len(rows) == 1


def test_comments_list_endpoint_capped(admin_client, monkeypatch):
    import app.routes.bugs as bugs_mod
    monkeypatch.setattr(bugs_mod, "_DETAIL_COMMENTS_MAX", 1)
    p = _make_project(admin_client)
    item = _make_item(admin_client, p["id"])
    admin_client.post(f"/api/bugs/{item['id']}/comments", json={"body": "one"})
    admin_client.post(f"/api/bugs/{item['id']}/comments", json={"body": "two"})
    rows = admin_client.get(f"/api/bugs/{item['id']}/comments").json()
    assert len(rows) == 1


# Console email: body suppressed at INFO (live token leak); CRLF stripped from headers.
def test_console_email_suppresses_body_at_info(caplog):
    from email.message import EmailMessage

    from app import email_service
    msg = EmailMessage()
    msg["From"] = "a@b.c"
    msg["To"] = "x@y.z"
    msg["Subject"] = "Reset link"
    msg.set_content("http://app/reset?token=SUPERSECRET")
    with caplog.at_level(logging.INFO, logger=email_service.logger.name):
        email_service._send_console(msg)
    assert "SUPERSECRET" not in caplog.text   # body suppressed (live reset token)
    assert "Reset link" in caplog.text        # routing metadata still visible


def test_console_email_logs_body_only_at_debug(caplog):
    from email.message import EmailMessage

    from app import email_service
    msg = EmailMessage()
    msg["From"] = "a@b.c"
    msg["To"] = "x@y.z"
    msg["Subject"] = "Hi"
    msg.set_content("BODYTOKEN123")
    with caplog.at_level(logging.DEBUG, logger=email_service.logger.name):
        email_service._send_console(msg)
    assert "BODYTOKEN123" in caplog.text       # full body only at DEBUG


def test_email_header_safe_strips_crlf():
    from app.config import get_settings
    from app.email_service import _build, _header_safe
    assert "\n" not in _header_safe("Subject\r\nBcc: evil@x.com")
    msg = _build("S\r\nInjected: y", ["ok\n@x.com"], "body", get_settings())
    assert "\n" not in str(msg["Subject"])
    assert "\r" not in str(msg["Subject"])


# Project PATCH is a true partial update; omitted fields must not be cleared.
def test_project_partial_update_keeps_unsent_fields(admin_client):
    r = admin_client.post("/api/projects", json={
        "name": "PartialP", "description": "keep me", "color": "#abcdef"})
    pid = r.json()["id"]
    r2 = admin_client.put(f"/api/projects/{pid}", json={"name": "PartialP2"})
    assert r2.status_code == 200, r2.text
    assert r2.json()["description"] == "keep me"   # not reset to ""


# Re-sending the same values is a no-op: no audit row, no field mutation.
def test_project_update_with_no_actual_changes_skips_audit(admin_client):
    r = admin_client.post("/api/projects", json={
        "name": "NoOpProject", "description": "same", "color": "#111111"})
    pid = r.json()["id"]
    before = admin_client.get("/api/audit").json()
    r2 = admin_client.put(f"/api/projects/{pid}", json={
        "name": "NoOpProject", "description": "same", "color": "#111111"})
    assert r2.status_code == 200, r2.text
    after = admin_client.get("/api/audit").json()
    assert len(after) == len(before), "identical PUT must not write an audit row"


# Oversized images must be returned unchanged without being decoded.
def test_image_oversized_skipped_before_decode(monkeypatch):
    from PIL import Image

    from app import image_strip
    img = Image.new("RGB", (8, 8), (0, 0, 255))
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    data = buf.getvalue()
    monkeypatch.setattr(image_strip, "_MAX_IMAGE_PIXELS", 10)   # force the oversized path
    out = image_strip.strip_image_metadata(data, "image/png")
    assert out == data   # returned as-is, not decoded


# Unhandled errors return a sanitized 500 with security headers; covers get_db rollback.
def test_unhandled_exception_clean_500_with_headers(client):
    from fastapi import Depends
    from sqlalchemy import text as sqltext
    from sqlalchemy.orm import Session
    from starlette.testclient import TestClient

    from app.database import get_db
    from app.main import app

    @app.get("/api/_boom_v31")
    def _boom(db: Session = Depends(get_db)):   # noqa: ANN202
        db.execute(sqltext("SELECT 1"))         # touch the session before raising
        raise RuntimeError("kaboom-internal-detail")

    c2 = TestClient(app, raise_server_exceptions=False)
    r = c2.get("/api/_boom_v31")
    assert r.status_code == 500
    assert r.json() == {"detail": "Internal server error."}
    assert "kaboom-internal-detail" not in r.text   # no internal detail leaked
    assert "Content-Security-Policy" in r.headers    # security headers on 500


def test_csrf_403_carries_security_headers(admin_client):
    r = admin_client.post("/api/projects", json={"name": "x"},
                          headers={"Origin": "https://evil.example.test"})
    assert r.status_code == 403
    assert "Content-Security-Policy" in r.headers     # headers present even on short-circuit


# Docs are exposed in dev mode but must be disabled in production (COOKIE_SECURE).
def test_docs_open_in_dev(client):
    assert client.get("/openapi.json").status_code == 200


def _reload_main(monkeypatch, **env):
    for key, value in env.items():
        monkeypatch.setenv(key, value)
    for mod in [m for m in sys.modules if m == "app" or m.startswith("app.")]:
        del sys.modules[mod]
    from app.config import get_settings
    get_settings.cache_clear()  # type: ignore[attr-defined]
    import app.main as main_mod
    return main_mod, {getattr(route, "path", None) for route in main_mod.app.routes}


# Production (COOKIE_SECURE) hides the endpoint map unless explicitly opted in.
def test_docs_disabled_when_cookie_secure(monkeypatch):
    main_mod, paths = _reload_main(
        monkeypatch, COOKIE_SECURE="true", SESSION_SECRET="x" * 40, ENABLE_API_DOCS="false",
    )
    assert main_mod.app.docs_url is None
    assert main_mod.app.openapi_url is None
    assert "/docs" not in paths
    assert "/redoc" not in paths


# ENABLE_API_DOCS=true keeps the self-hosted (CSP-safe) docs in production.
def test_docs_opt_in_in_production(monkeypatch):
    main_mod, paths = _reload_main(
        monkeypatch, COOKIE_SECURE="true", SESSION_SECRET="x" * 40, ENABLE_API_DOCS="true",
    )
    assert main_mod.app.docs_url is None  # FastAPI's CDN/inline page stays off
    assert main_mod.app.openapi_url == "/openapi.json"
    assert "/docs" in paths
    assert "/redoc" in paths


# DB down at boot degrades gracefully; /health reflects the degraded state.
def test_safe_init_db_degrades_on_failure(client):
    from sqlalchemy.exc import SQLAlchemyError

    import app.main as main_mod

    def boom(**_):
        raise SQLAlchemyError("init failed")

    orig = main_mod.init_db
    main_mod.init_db = boom
    try:
        assert main_mod._safe_init_db() is False   # error swallowed
    finally:
        main_mod.init_db = orig


def test_check_db_health_false_on_error(client, monkeypatch):
    from sqlalchemy.exc import SQLAlchemyError

    import app.main as main_mod

    def boom(**_):
        raise SQLAlchemyError("down")

    monkeypatch.setattr(main_mod, "SessionLocal", boom)
    assert main_mod._check_db_health() is False


def test_health_reports_degraded_when_db_unreachable(admin_client, monkeypatch):
    import app.main as main_mod
    monkeypatch.setattr(main_mod, "_check_db_health", lambda: False)
    body = admin_client.get("/api/health").json()
    assert body["status"] == "degraded"
    assert body["database"] == "unavailable"


# JSON extractor must skip stray braces in prose and find the real object.
def test_extract_json_scans_past_leading_prose_brace():
    from app.chatbot.cloud_llm import _extract_json as ej_cloud
    from app.chatbot.llm import _extract_json as ej_local
    reply = 'Sure! Here you go {note} {"mode": "answer", "text": "hi"}'
    assert ej_cloud(reply) == {"mode": "answer", "text": "hi"}
    assert ej_local(reply) == {"mode": "answer", "text": "hi"}


# Sleuth writes validate like REST; drive execute_plan to cover _apply_set_field.
def test_sleuth_set_invalid_value_rejected_via_execute(admin_client):
    from sqlalchemy import select

    from app.chatbot.actions import ActionPlan, execute_plan
    from app.database import SessionLocal
    from app.models import User
    p = _make_project(admin_client)
    item = _make_item(admin_client, p["id"])
    db = SessionLocal()
    try:
        admin = db.scalar(select(User).where(User.role == "admin"))
        resp = execute_plan(ActionPlan(
            kind="set_due_date", actor_user_id=admin.id, bug_id=item["id"],
            new_value="2026-99-99", summary_human="set due"), db, admin)
        assert resp.intent == "action_error"
        assert "valid date" in resp.blocks[0].payload["text"].lower()
    finally:
        db.close()


# _validate_field_value covers every enum and date field.
def test_sleuth_field_validation(admin_client):
    from app.chatbot.actions import _validate_field_value
    from app.database import SessionLocal
    from app.models import Bug
    p = _make_project(admin_client)
    item = _make_item(admin_client, p["id"])
    db = SessionLocal()
    try:
        bug = db.get(Bug, item["id"])
        assert _validate_field_value(bug, "due_date", "2026-99-99") is not None
        assert _validate_field_value(bug, "priority", "Ultra") is not None
        assert _validate_field_value(bug, "environment", "MARS") is not None
        assert _validate_field_value(bug, "status", "Bogus") is not None
        # Valid values should return None.
        assert _validate_field_value(bug, "due_date", "2026-01-15") is None
        assert _validate_field_value(bug, "priority", "High") is None
        assert _validate_field_value(bug, "environment", "PROD") is None
    finally:
        db.close()


# Staged actions expire after the confirm window to prevent stale confirmations.
def test_pending_action_expires_after_confirm_window(client):
    import app.chatbot.memory as mm
    mm.store._clear_all_for_test()
    mm.store.stage_pending(1, {"kind": "set_status"})
    sess = mm.store._all_sessions_for_test()[1]
    sess.pending_staged_at -= (mm._CONFIRM_TTL_SECONDS + 10)   # backdate past window
    assert mm.store.take_pending(1) is None                     # stale → dropped
    # A fresh proposal is still returned immediately.
    mm.store.stage_pending(1, {"kind": "set_priority"})
    assert mm.store.take_pending(1) == {"kind": "set_priority"}


# Redaction covers secrets and PII before egress.
def test_redaction_covers_more_secret_shapes():
    from app.chatbot.redaction import redact
    # Tokens built by concatenation so no complete literal sits in source (secret scanners).
    body = "abcdefghij1234567890"
    hex32 = "0123456789abcdef0123456789abcdef"
    samples = [
        "glpat-" + body,                       # GitLab PAT
        "SG." + body + "." + body + "zz",      # SendGrid
        "SK" + hex32,                          # Twilio API-key SID
        "xapp-1-" + "ABCDEF123456",            # Slack app token
    ]
    for s in samples:
        assert s not in redact(f"the token is {s} ok"), s
    # Phone numbers (PII).
    assert "+1 415 555 0123" not in redact("call +1 415 555 0123 today")
    assert "555-123-4567" not in redact("dial 555-123-4567 now")


# xlsx row count is bounded as a decompression-bomb guard.
def test_xlsx_rows_capped(monkeypatch):
    import openpyxl

    from app.chatbot import ingest
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.append(["row one"])
    ws.append(["row two"])
    ws.append(["row three"])
    buf = io.BytesIO()
    wb.save(buf)
    monkeypatch.setattr(ingest, "_MAX_XLSX_ROWS", 1)
    rows = ingest._xlsx_rows(buf.getvalue())
    assert rows is not None
    assert len(rows) == 1


# Over-range numeric q must fall back to a text search rather than raising.
def test_q_huge_number_does_not_500(admin_client):
    p = _make_project(admin_client)
    _make_item(admin_client, p["id"], title="findme")
    r = admin_client.get("/api/bugs", params={"q": "9" * 40})
    assert r.status_code == 200   # over-range digits → LIKE path, no DataError


# Reverse-direction directional links are rejected; symmetric ones are not.
def test_reverse_directional_link_rejected(admin_client):
    p = _make_project(admin_client)
    a = _make_item(admin_client, p["id"], title="ra-item")
    b = _make_item(admin_client, p["id"], title="rb-item")
    assert admin_client.post(f"/api/bugs/{a['id']}/links", json={
        "target_bug_id": b["id"], "link_type": "blocks"}).status_code == 201
    # B blocks A contradicts A blocks B → 400.
    assert admin_client.post(f"/api/bugs/{b['id']}/links", json={
        "target_bug_id": a["id"], "link_type": "blocks"}).status_code == 400
    # 'relates' is symmetric, so the reverse is fine.
    assert admin_client.post(f"/api/bugs/{b['id']}/links", json={
        "target_bug_id": a["id"], "link_type": "relates"}).status_code == 201


# Linking requires edit rights on both endpoints.
def test_link_to_task_requires_target_edit_rights(admin_client):
    p = _make_project(admin_client)
    bug = _make_item(admin_client, p["id"], item_type="Bug")
    task = _make_item(admin_client, p["id"], item_type="Task")
    _become_regular_user(admin_client)
    # The user can edit the Bug (collaborative) but not the Task → 403.
    r = admin_client.post(f"/api/bugs/{bug['id']}/links", json={
        "target_bug_id": task["id"], "link_type": "relates"})
    assert r.status_code == 403


# Optimistic concurrency via a version counter (sub-second safe).
def test_update_stale_version_409_and_bump(admin_client):
    p = _make_project(admin_client)
    item = _make_item(admin_client, p["id"])
    assert item["version"] == 1
    # A matching version succeeds and bumps the counter.
    r = admin_client.put(f"/api/bugs/{item['id']}", json={
        "title": "first edit here", "expected_version": 1})
    assert r.status_code == 200
    assert r.json()["version"] == 2
    # Re-using the now-stale version is rejected.
    r2 = admin_client.put(f"/api/bugs/{item['id']}", json={
        "title": "second edit here", "expected_version": 1})
    assert r2.status_code == 409


def test_expected_version_negative_is_422(admin_client):
    p = _make_project(admin_client)
    item = _make_item(admin_client, p["id"])
    r = admin_client.put(f"/api/bugs/{item['id']}", json={
        "title": "x title here", "expected_version": -1})
    assert r.status_code == 422


def test_optimistic_check_noop_when_row_vanished(client):
    # Concurrent-delete race: locked read returns nothing -> check is a no-op.
    from app.database import SessionLocal
    from app.routes.bugs import _enforce_optimistic_concurrency
    db = SessionLocal()
    try:
        _enforce_optimistic_concurrency(db, 999999, 5, None)   # must not raise
    finally:
        db.close()


# Case-insensitive email uniqueness, including legacy mixed-case rows.
def test_email_uniqueness_is_case_insensitive(admin_client):
    from app.auth import hash_password
    from app.database import SessionLocal
    from app.models import User
    db = SessionLocal()
    try:   # seed a legacy mixed-case row the exact-unique constraint wouldn't catch
        db.add(User(org_id=default_org_id(), name="Legacy", email="Legacy@x.com", role="user",
                    is_active=True, password_hash=hash_password("Whatever1")))
        db.commit()
    finally:
        db.close()
    r = admin_client.post("/api/users", json={
        "name": "New", "email": "legacy@x.com", "role": "user", "password": "Abcd1234"})
    assert r.status_code == 409


def test_update_user_email_case_insensitive_conflict(admin_client):
    u1 = admin_client.post("/api/users", json={
        "name": "U1", "email": "u1@x.com", "role": "user", "password": "Abcd1234"}).json()
    admin_client.post("/api/users", json={
        "name": "U2", "email": "u2@x.com", "role": "user", "password": "Abcd1234"})
    # Renaming U1 onto U2's email (different case) is a conflict ...
    assert admin_client.put(f"/api/users/{u1['id']}", json={"email": "U2@x.com"}).status_code == 409
    # ... but a genuinely free email is fine (covers the no-conflict branch).
    assert admin_client.put(f"/api/users/{u1['id']}", json={"email": "u1b@x.com"}).status_code == 200


def test_create_user_db_unique_race_still_409(admin_client, monkeypatch):
    # Defense-in-depth: the DB unique constraint must 409 (not 500) if the pre-check races.
    import app.routes.users as users_mod
    admin_client.post("/api/users", json={
        "name": "Race", "email": "race@x.com", "role": "user", "password": "Abcd1234"})
    monkeypatch.setattr(users_mod, "_email_in_use", lambda *a, **k: False)
    r = admin_client.post("/api/users", json={
        "name": "Race2", "email": "race@x.com", "role": "user", "password": "Abcd1234"})
    assert r.status_code == 409


def test_update_user_db_unique_race_still_409(admin_client, monkeypatch):
    import app.routes.users as users_mod
    ra = admin_client.post("/api/users", json={
        "name": "Racer A", "email": "arace@x.com", "role": "user", "password": "Abcd1234"})
    assert ra.status_code == 201, ra.text
    rb = admin_client.post("/api/users", json={
        "name": "Racer B", "email": "brace@x.com", "role": "user", "password": "Abcd1234"})
    assert rb.status_code == 201, rb.text
    monkeypatch.setattr(users_mod, "_email_in_use", lambda *a, **k: False)
    r = admin_client.put(f"/api/users/{rb.json()['id']}", json={"email": "arace@x.com"})
    assert r.status_code == 409, r.text


# Stats buckets must be int-typed (NULL-key safe).
def test_stats_buckets_are_int_typed(admin_client):
    p = _make_project(admin_client)
    _make_item(admin_client, p["id"], priority="High", environment="PROD")
    body = admin_client.get("/api/stats").json()
    assert all(isinstance(v, int) for v in body["by_priority"].values())
    assert all(isinstance(v, int) for v in body["by_environment"].values())


# can_edit on event items is computed per-user, not hardcoded.
def test_event_item_can_edit_per_user(admin_client):
    p = _make_project(admin_client)
    ev = admin_client.post("/api/events", json={
        "name": "Standup v31", "scheduled_for": "2026-05-28",
        "project_id": p["id"]}).json()
    task = _make_item(admin_client, p["id"], item_type="Task", event_id=ev["id"])
    admin_view = admin_client.get(f"/api/events/{ev['id']}").json()
    assert next(i for i in admin_view["items"] if i["id"] == task["id"])["can_edit"] is True
    _become_regular_user(admin_client)
    user_view = admin_client.get(f"/api/events/{ev['id']}").json()
    assert next(i for i in user_view["items"] if i["id"] == task["id"])["can_edit"] is False


# Spreadsheet formula defang must also catch leading-whitespace and newline forms.
def test_spreadsheet_defang_handles_whitespace_and_newline():
    from app.chatbot.excel import _defang_formula_text as defang_excel
    from app.reports.xlsx import _defang_formula_text as defang_xlsx
    for defang in (defang_xlsx, defang_excel):
        assert defang(" =1+1").startswith("'")    # leading space then formula
        assert defang("\n=cmd").startswith("'")    # leading newline
        assert defang("=1+1").startswith("'")      # plain trigger
        assert defang("@SUM(A1)").startswith("'")
        assert defang("normal text") == "normal text"
        assert defang("") == ""


# Report filter id-lists are length-capped as a DoS guard.
def test_report_filter_id_list_capped(admin_client):
    r = admin_client.post("/api/reports/run", json={
        "report_key": "status_distribution",
        "filters": {"project_ids": list(range(1001))}})
    assert r.status_code == 422
