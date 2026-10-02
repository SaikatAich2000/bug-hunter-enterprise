"""Sleuth admin document-ingest: two-step preview/confirm flow plus the
parser in app/chatbot/ingest.py (formats, fuzzy column mapping)."""
from __future__ import annotations

import io
import json

from fastapi.testclient import TestClient

_PW = "User12345"
_INGEST = "/api/chat/ingest"
_ASK = "/api/chat/ask"


def _new_client() -> TestClient:
    from app.main import app
    return TestClient(app)


def _login(c: TestClient, email: str, password: str = _PW) -> None:
    c.post("/api/auth/logout")
    r = c.post("/api/auth/login", json={"email": email, "password": password})
    assert r.status_code == 200, r.text


def _mk_user(admin: TestClient, name: str, email: str, role: str = "user") -> dict:
    r = admin.post("/api/users", json={"name": name, "email": email, "role": role, "password": _PW})
    assert r.status_code == 201, r.text
    return r.json()


def _mk_project(admin: TestClient, name: str = "Ingest Proj") -> dict:
    r = admin.post("/api/projects", json={"name": name})
    assert r.status_code == 201, r.text
    return r.json()


def _xlsx_bytes(rows: list[list[str]]) -> bytes:
    from openpyxl import Workbook
    wb = Workbook()
    ws = wb.active
    for r in rows:
        ws.append(r)
    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue()


def _table_rows(resp_json: dict) -> list:
    return next((b["payload"]["rows"] for b in resp_json["blocks"] if b["kind"] == "table"), [])


def _upload(client: TestClient, filename: str, content, ctype: str, project_id: int | None = None) -> dict:
    data = {"project_id": str(project_id)} if project_id is not None else {}
    r = client.post(_INGEST, files={"file": (filename, content, ctype)}, data=data)
    assert r.status_code == 200, r.text
    return r.json()


def _bug_titles(client: TestClient, project_id: int) -> set[str]:
    return {b["title"] for b in client.get(f"/api/bugs?project_id={project_id}").json()["items"]}


# Permissions
def test_ingest_requires_auth(client):
    assert client.post(_INGEST, files={"file": ("x.txt", "A bug here", "text/plain")}).status_code == 401


def test_ingest_is_admin_only(admin_client):
    _mk_user(admin_client, "Mgr", "mgr.ing@test.local", role="manager")
    mgrc = _new_client()
    _login(mgrc, "mgr.ing@test.local")
    r = mgrc.post(_INGEST, files={"file": ("x.txt", "Login crash bug", "text/plain")})
    assert r.status_code == 403


# Conversational flow: preview → create / cancel (nothing auto-created)
def test_upload_previews_without_creating(admin_client):
    proj = _mk_project(admin_client, "PreviewProj")
    doc = "Login button broken [High]\nSearch is stale | index not refreshed\nCheckout crash (Critical)"
    j = _upload(admin_client, "bugs.txt", doc, "text/plain", proj["id"])
    assert j["intent"] == "ingest_preview"
    assert len(_table_rows(j)) == 3
    # Confirm nothing was persisted during preview.
    assert admin_client.get(f"/api/bugs?project_id={proj['id']}").json()["total"] == 0


def test_create_them_creates_staged_items(admin_client):
    proj = _mk_project(admin_client, "CreateProj")
    _upload(admin_client, "bugs.txt", "First real bug here\nSecond real bug here", "text/plain", proj["id"])
    r = admin_client.post(_ASK, json={"message": "create them"})
    assert r.status_code == 200
    assert r.json()["intent"] == "ingest_done"
    assert admin_client.get(f"/api/bugs?project_id={proj['id']}").json()["total"] == 2


def test_cancel_discards_staged_items(admin_client):
    proj = _mk_project(admin_client, "CancelProj")
    _upload(admin_client, "bugs.txt", "Throwaway bug one\nThrowaway bug two", "text/plain", proj["id"])
    r = admin_client.post(_ASK, json={"message": "cancel"})
    assert r.json()["intent"] == "ingest_cancelled"
    assert admin_client.get(f"/api/bugs?project_id={proj['id']}").json()["total"] == 0


def test_create_after_project_deleted_is_an_error(admin_client):
    proj = _mk_project(admin_client, "GoneProj")
    _upload(admin_client, "bugs.txt", "Bug before the delete here", "text/plain", proj["id"])
    admin_client.delete(f"/api/projects/{proj['id']}")
    r = admin_client.post(_ASK, json={"message": "create them"})
    assert r.json()["intent"] == "ingest_error"


def test_create_more_than_50_shows_remainder(admin_client):
    proj = _mk_project(admin_client, "BigCreate")
    doc = "\n".join(f"Bulk item number {i} here" for i in range(55))
    _upload(admin_client, "many.txt", doc, "text/plain", proj["id"])
    r = admin_client.post(_ASK, json={"message": "create them"})
    assert r.json()["intent"] == "ingest_done"
    assert any("more" in b["payload"].get("text", "") for b in r.json()["blocks"])
    assert admin_client.get(f"/api/bugs?project_id={proj['id']}").json()["total"] == 55


def test_create_them_without_a_staged_doc_is_not_an_ingest(admin_client):
    # Without a prior upload, "create them" must not be treated as an ingest action.
    r = admin_client.post(_ASK, json={"message": "create them"})
    assert r.json()["intent"] not in ("ingest_done", "ingest_cancelled")


def test_unrelated_message_leaves_the_staged_doc_parked(admin_client):
    proj = _mk_project(admin_client, "ParkedProj")
    _upload(admin_client, "bugs.txt", "Parked bug one here\nParked bug two here", "text/plain", proj["id"])
    # An unrelated message should leave the staged doc alone.
    r = admin_client.post(_ASK, json={"message": "what kinds of items are in there"})
    assert r.json()["intent"] not in ("ingest_done", "ingest_cancelled")
    assert admin_client.get(f"/api/bugs?project_id={proj['id']}").json()["total"] == 0
    # A later confirmation should still work.
    admin_client.post(_ASK, json={"message": "yes create them"})
    assert admin_client.get(f"/api/bugs?project_id={proj['id']}").json()["total"] == 2


# Formats — all preview correctly
def test_csv_with_header_previews(admin_client):
    proj = _mk_project(admin_client, "CsvProj")
    doc = "Title,Description,Priority,Type\nAPI 500,trace attached,Critical,Bug\nDark mode,requested,Low,Requirement"
    j = _upload(admin_client, "bugs.csv", doc, "text/csv", proj["id"])
    assert j["intent"] == "ingest_preview"
    rows = _table_rows(j)
    assert ["Dark mode", "Low", "Requirement"] in rows
    # The header row must not appear as an item.
    assert not any(r[0].lower() == "title" for r in rows)


def test_json_array_previews(admin_client):
    proj = _mk_project(admin_client, "JsonProj")
    doc = json.dumps([
        {"title": "NPE in parser", "priority": "high", "environment": "prod"},
        {"summary": "Flaky test", "type": "task"},
    ])
    j = _upload(admin_client, "bugs.json", doc, "application/json", proj["id"])
    rows = _table_rows(j)
    npe = next(r for r in rows if r[0] == "NPE in parser")
    assert npe[1] == "High"


def test_xlsx_nonstandard_header_not_treated_as_item(admin_client):
    # Non-standard headers must be skipped, with columns fuzzy-mapped.
    proj = _mk_project(admin_client, "XlsxProj")
    data = _xlsx_bytes([["Bug Summary", "Sev"], ["Login fails on Safari", "Critical"],
                        ["Checkout total wrong", "High"]])
    j = _upload(admin_client, "bugs.xlsx", data,
                "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet", proj["id"])
    rows = _table_rows(j)
    titles = [r[0] for r in rows]
    assert "Login fails on Safari" in titles
    assert "Bug Summary" not in titles          # header row skipped
    assert ["Login fails on Safari", "Critical", "Bug"] in rows  # "Sev" fuzzy-maps to priority


def test_upload_defaults_to_first_project(admin_client):
    j = _upload(admin_client, "x.txt", "Some default bug here", "text/plain")
    assert j["intent"] == "ingest_preview"


# Edge cases
def test_empty_file_reads_nothing(admin_client):
    j = _upload(admin_client, "empty.txt", "   \n\n---\n", "text/plain")
    assert j["intent"] == "ingest_empty"


def test_truly_empty_upload(admin_client):
    j = _upload(admin_client, "empty.txt", "", "text/plain")
    assert j["intent"] == "ingest_empty"


def test_no_project_available_400(admin_client):
    for p in admin_client.get("/api/projects").json():
        admin_client.delete(f"/api/projects/{p['id']}")
    r = admin_client.post(_INGEST, files={"file": ("x.txt", "Orphan bug here", "text/plain")})
    assert r.status_code == 400


def test_too_large_413(admin_client, monkeypatch):
    monkeypatch.setattr("app.chatbot.ingest.MAX_DOC_BYTES", 16)
    big = "This is a long bug title that exceeds the tiny cap\n" * 5
    r = admin_client.post(_INGEST, files={"file": ("big.txt", big, "text/plain")})
    assert r.status_code == 413


def test_large_preview_truncates(admin_client):
    proj = _mk_project(admin_client, "ManyProj")
    doc = "\n".join(f"Bug number {i} in the list" for i in range(60))
    j = _upload(admin_client, "many.txt", doc, "text/plain", proj["id"])
    assert len(_table_rows(j)) == 50
    assert any("more" in b["payload"].get("text", "") for b in j["blocks"])


# AI extraction path (stubbed, no network)
def test_ingest_uses_ai_when_available(admin_client, monkeypatch):
    import app.chatbot.cloud_llm as cloud
    monkeypatch.setattr(cloud, "is_available", lambda: True)
    monkeypatch.setattr(cloud, "complete_json", lambda system, user: {
        "bugs": [{"title": "AI-extracted crash", "priority": "Critical"}],
    })
    proj = _mk_project(admin_client, "AiProj")
    j = _upload(admin_client, "notes.txt", "messy paragraph about one problem", "text/plain", proj["id"])
    assert "AI" in j["blocks"][0]["payload"]["text"]
    assert [r[0] for r in _table_rows(j)] == ["AI-extracted crash"]


def test_ai_reads_xlsx_as_text(admin_client, monkeypatch):
    import app.chatbot.cloud_llm as cloud
    captured = {}

    def _complete(system, user):
        captured["user"] = user
        return {"bugs": [{"title": "From the AI table read"}]}

    monkeypatch.setattr(cloud, "is_available", lambda: True)
    monkeypatch.setattr(cloud, "complete_json", _complete)
    proj = _mk_project(admin_client, "AiXlsx")
    data = _xlsx_bytes([["Title", "Pri"], ["Row one cell", "High"]])
    _upload(admin_client, "s.xlsx", data,
            "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet", proj["id"])
    # The AI prompt must contain the sheet contents rendered as plain text.
    assert "Row one cell" in captured["user"]


def test_ai_falls_back_to_parser_when_empty(admin_client, monkeypatch):
    import app.chatbot.cloud_llm as cloud
    monkeypatch.setattr(cloud, "is_available", lambda: True)
    monkeypatch.setattr(cloud, "complete_json", lambda system, user: {"bugs": []})
    proj = _mk_project(admin_client, "AiEmpty")
    j = _upload(admin_client, "x.txt", "Parser fallback bug here", "text/plain", proj["id"])
    assert "parsed" in j["blocks"][0]["payload"]["text"]


# Parser unit tests (direct)
def test_parser_json_object_with_bugs_key(admin_client):
    from app.chatbot import ingest
    specs = ingest.parse_document("x.json", json.dumps({"bugs": [{"title": "keyed bug"}]}).encode())
    assert specs[0]["title"] == "keyed bug"


def test_parser_json_single_object(admin_client):
    from app.chatbot import ingest
    specs = ingest.parse_document("x.json", json.dumps({"title": "one bug", "priority": "blocker"}).encode())
    assert specs[0]["priority"] == "Critical"


def test_parser_json_list_of_strings(admin_client):
    from app.chatbot import ingest
    specs = ingest.parse_document("x.json", json.dumps(["first issue", "second issue"]).encode())
    assert [s["title"] for s in specs] == ["first issue", "second issue"]


def test_parser_json_scalar_falls_through(admin_client):
    from app.chatbot import ingest
    assert ingest._specs_from_json("12345") is None


def test_parser_invalid_json_falls_through_to_lines(admin_client):
    from app.chatbot import ingest
    specs = ingest.parse_document("x.txt", b"{not valid json but a sentence")
    assert specs
    assert specs[0]["title"].startswith("{not valid")


def test_parser_csv_single_column_no_header(admin_client):
    from app.chatbot import ingest
    # Single column with no recognizable header: every row becomes a title.
    specs = ingest.parse_document("x.csv", b"first only here\nsecond only here")
    assert [s["title"] for s in specs] == ["first only here", "second only here"]


def test_parser_csv_blank_rows_yield_nothing(admin_client):
    from app.chatbot import ingest
    assert ingest._specs_from_csv("\n , \n , ,\n") == []


def test_parser_fuzzy_column_mapping(admin_client):
    from app.chatbot import ingest
    # "Bug Summary" fuzzy-maps to title and "Sev." fuzzy-maps to priority.
    specs = ingest._rows_to_specs([["Bug Summary", "Sev."], ["My title row", "high"]])
    assert specs[0]["title"] == "My title row"
    assert specs[0]["priority"] == "High"


def test_parser_fuzzy_column_skips_empty_cells(admin_client):
    from app.chatbot import ingest
    # A leading blank cell must not shadow the later "Bug Summary" column.
    specs = ingest._rows_to_specs([["Notes", "Bug Summary"], ["", "My real title here"]])
    assert specs[0]["title"] == "My real title here"


def test_parser_synonyms_resolve(admin_client):
    from app.chatbot import ingest
    spec = ingest._clean_spec({"title": "synonym bug", "priority": "p1", "type": "story", "env": "staging"})
    assert spec["priority"] == "High"
    assert spec["item_type"] == "Requirement"
    assert spec["environment"] == "UAT"


def test_parser_whitespace_enum_uses_default(admin_client):
    from app.chatbot import ingest
    spec = ingest._clean_spec({"title": "blank enum bug", "priority": "   "})
    assert spec["priority"] == "Medium"


def test_parser_unknown_enum_uses_default(admin_client):
    from app.chatbot import ingest
    spec = ingest._clean_spec({"title": "weird", "priority": "spicy", "type": "saga", "env": "moon"})
    assert spec["priority"] == "Medium"
    assert spec["item_type"] == "Bug"
    assert spec["environment"] == "DEV"


def test_parser_clean_spec_rejects_non_dict_and_short(admin_client):
    from app.chatbot import ingest
    assert ingest._clean_spec(12345) is None
    assert ingest._clean_spec({"description": "no title here"}) is None
    assert ingest._clean_spec("ab") is None


def test_parser_skips_short_titles(admin_client):
    from app.chatbot import ingest
    specs = ingest.parse_document("x.txt", b"ok\nthis one is long enough")
    assert [s["title"] for s in specs] == ["this one is long enough"]


def test_parser_xlsx_bad_bytes_falls_through(admin_client):
    from app.chatbot import ingest
    specs = ingest.parse_document("fake.xlsx", b"this is not really a workbook file")
    assert specs
    assert "not really a workbook" in specs[0]["title"]


def test_parser_xlsx_single_column_list(admin_client):
    from app.chatbot import ingest
    data = _xlsx_bytes([["just one bug per row"], ["another bug row here"]])
    specs = ingest.parse_document("x.xlsx", data)
    assert [s["title"] for s in specs] == ["just one bug per row", "another bug row here"]


def test_parser_xlsx_empty_sheet(admin_client):
    from app.chatbot import ingest
    assert ingest._specs_from_xlsx(_xlsx_bytes([])) == []


def test_document_text_empty_xlsx_returns_blank(admin_client):
    from app.chatbot import ingest
    # Empty xlsx yields no text so the AI layer is never called with a blank doc.
    assert ingest._document_text("x.xlsx", _xlsx_bytes([])) == ""


def test_parser_comma_doc_with_no_rows_falls_to_lines(admin_client):
    from app.chatbot import ingest
    assert ingest.parse_document("x.txt", b",,\n,,\n") == []


def test_clean_all_respects_max_specs(admin_client, monkeypatch):
    from app.chatbot import ingest
    monkeypatch.setattr(ingest, "MAX_SPECS", 2)
    specs = ingest._clean_all([f"bug number {i} here" for i in range(10)])
    assert len(specs) == 2


def test_document_text_for_text_file(admin_client):
    from app.chatbot import ingest
    assert ingest._document_text("x.txt", b"hello world") == "hello world"


# create_bugs_from_specs (direct): cap and project resolution
def test_create_from_specs_caps_and_resolves_default_project(admin_client, monkeypatch):
    from app.chatbot import ingest
    from app.database import SessionLocal
    from app.models import User
    monkeypatch.setattr(ingest, "MAX_SPECS", 1)
    db = SessionLocal()
    try:
        actor = db.query(User).filter(User.role == "admin").first()
        specs = [{"title": "spec one here", "priority": "Medium", "item_type": "Bug", "environment": "DEV"},
                 {"title": "spec two here", "priority": "Medium", "item_type": "Bug", "environment": "DEV"}]
        summary = ingest.create_bugs_from_specs(db, specs, actor, project_id=None)
        assert summary["created"] == 1
        assert summary["project_name"]
    finally:
        db.close()


def test_create_from_specs_no_project_raises(admin_client):
    from app.chatbot import ingest
    from app.database import SessionLocal
    from app.models import Project, User
    db = SessionLocal()
    try:
        for p in db.query(Project).all():
            db.delete(p)
        db.commit()
        actor = db.query(User).filter(User.role == "admin").first()
        import pytest
        with pytest.raises(ValueError):
            ingest.create_bugs_from_specs(db, [{"title": "orphan spec"}], actor, project_id=None)
    finally:
        db.close()


def test_ai_extract_returns_none_when_unavailable(admin_client, monkeypatch):
    import app.chatbot.cloud_llm as cloud
    from app.chatbot import ingest
    monkeypatch.setattr(cloud, "is_available", lambda: False)
    assert ingest.ai_extract_specs("some text") is None


def test_ai_extract_returns_none_on_missing_bugs_key(admin_client, monkeypatch):
    import app.chatbot.cloud_llm as cloud
    from app.chatbot import ingest
    monkeypatch.setattr(cloud, "is_available", lambda: True)
    monkeypatch.setattr(cloud, "complete_json", lambda system, user: {"note": "no bugs"})
    assert ingest.ai_extract_specs("text") is None
    monkeypatch.setattr(cloud, "complete_json", lambda system, user: None)
    assert ingest.ai_extract_specs("text") is None


def test_extract_specs_skips_ai_when_document_text_is_blank(admin_client, monkeypatch):
    # A blank/whitespace-only document has nothing for the AI reader to read;
    # extract_specs must go straight to the deterministic parser fallback.
    from app.chatbot import ingest
    called = []
    monkeypatch.setattr(ingest, "ai_extract_specs", lambda text: called.append(text) or None)
    specs, method = ingest.extract_specs("empty.txt", b"   \n\t  ")
    assert method == "parser"
    assert called == []


def test_ai_extract_swallows_errors(admin_client, monkeypatch):
    import app.chatbot.cloud_llm as cloud
    from app.chatbot import ingest
    monkeypatch.setattr(cloud, "is_available", lambda: True)

    def _boom(system, user):
        raise RuntimeError("provider exploded")

    monkeypatch.setattr(cloud, "complete_json", _boom)
    assert ingest.ai_extract_specs("text") is None
