"""Regression tests for the remaining-fixes batch (user delete audit, seed boot,
session cookie, upload allowlist, lockout scope, 422 shape, documented statuses)."""
from __future__ import annotations

import logging

import pytest


def _user(c, name, email, role="user"):
    pids = [p["id"] for p in c.get("/api/projects").json()]
    r = c.post("/api/users", json={
        "name": name, "email": email, "role": role,
        "password": "TestUserPwd9X", "project_ids": pids,
    })
    assert r.status_code == 201, r.text
    return r.json()


def _project(c, name):
    r = c.post("/api/projects", json={"name": name, "color": "#c9764f"})
    assert r.status_code == 201, r.text
    return r.json()


# --- deleting a user leaves a trail on every item they are removed from ------
def test_delete_user_logs_activity_on_each_item(admin_client):
    p = _project(admin_client, "Trail")
    u = _user(admin_client, "Leaver", "leaver@x.com", role="manager")
    as_assignee = admin_client.post("/api/bugs", json={
        "project_id": p["id"], "title": "Assigned item", "priority": "Medium",
        "environment": "DEV", "assignee_ids": [u["id"]]}).json()
    as_reporter = admin_client.post("/api/bugs", json={
        "project_id": p["id"], "title": "Reported item", "priority": "Medium",
        "environment": "DEV", "reporter_id": u["id"]}).json()
    ev = admin_client.post("/api/events", json={
        "name": "Managed event", "project_id": p["id"], "manager_ids": [u["id"]]})
    assert ev.status_code == 201, ev.text

    assert admin_client.delete(f"/api/users/{u['id']}").status_code == 200

    for bug in (as_assignee, as_reporter):
        acts = admin_client.get(f"/api/bugs/{bug['id']}/activity").json()
        removed = [a for a in acts if a["action"] == "user_removed"]
        assert len(removed) == 1, acts
        assert "Leaver" in removed[0]["detail"]
    rows = admin_client.get("/api/audit?entity_type=event").json()
    assert any(r["action"] == "user_removed" and r["entity_id"] == ev.json()["id"] for r in rows)


# --- a fresh install does not "repair" the seed data it just wrote ------------
# --- session cookie: __Host- prefix, opaque value, old name still accepted ----
ADMIN = {"email": "admin@test.local", "password": "Admin1234"}


def _set_cookies(response):
    return response.headers.get_list("set-cookie")


def _secure_cookies(monkeypatch):
    import app.config as config
    monkeypatch.setattr(config.Settings, "COOKIE_SECURE", True)


def test_secure_login_uses_host_prefixed_cookie(client, monkeypatch):
    _secure_cookies(monkeypatch)
    r = client.post("/api/auth/login", json=ADMIN)
    assert r.status_code == 200
    host = [c for c in _set_cookies(r) if c.startswith("__Host-bh_session=")]
    assert len(host) == 1, _set_cookies(r)
    assert "Secure" in host[0] and "Path=/" in host[0] and "Domain" not in host[0]
    # The unprefixed name is only ever cleared, never set to a session.
    assert not [c for c in _set_cookies(r) if c.startswith("bh_session=") and not c.startswith('bh_session="";')
                and "Max-Age=0" not in c]


def test_insecure_login_keeps_plain_cookie_name(client):
    # Browsers reject the __Host- prefix without Secure, so plain HTTP dev keeps the old name.
    r = client.post("/api/auth/login", json=ADMIN)
    assert any(c.startswith("bh_session=") for c in _set_cookies(r))
    assert not any(c.startswith("__Host-") for c in _set_cookies(r))


def test_session_cookie_value_does_not_expose_user_id(client):
    from itsdangerous import BadSignature

    from app import auth
    r = client.post("/api/auth/login", json=ADMIN)
    uid = r.json()["id"]
    token = client.cookies.get("bh_session")
    # The old format was a readable signed "uid:version:jti"; this must not be.
    with pytest.raises(BadSignature):
        auth._signer().unsign(token)
    assert f"{uid}:" not in token
    assert auth.parse_session_token(token)[0] == uid


def test_secure_server_still_accepts_old_cookie_name_and_format(client, monkeypatch):
    from app import auth
    r = client.post("/api/auth/login", json=ADMIN)
    uid = r.json()["id"]
    _uid, version, jti = auth.parse_session_token(client.cookies.get("bh_session"))
    old_format = auth._signer().sign(f"{uid}:{version}:{jti}".encode()).decode()
    client.cookies.clear()
    _secure_cookies(monkeypatch)
    me = client.get("/api/auth/me", headers={"Cookie": f"bh_session={old_format}"})
    assert me.status_code == 200, me.text


def test_secure_logout_clears_both_cookie_names(client, monkeypatch):
    _secure_cookies(monkeypatch)
    r = client.post("/api/auth/logout")
    cleared = " ".join(_set_cookies(r))
    assert "__Host-bh_session=" in cleared and "bh_session=" in cleared.replace("__Host-bh_session=", "")


# --- every status code a route can raise is documented ---------------------------
def _raised_codes(fn, seen=None, depth=0):
    """Status codes of HTTPException(...) raised by ``fn`` and the app functions it calls by name."""
    import ast
    import inspect
    import textwrap

    seen = set() if seen is None else seen
    if fn in seen or depth > 4:
        return set()
    seen.add(fn)
    try:
        tree = ast.parse(textwrap.dedent(inspect.getsource(fn)))
    except (OSError, TypeError, SyntaxError):
        return set()
    scope = getattr(fn, "__globals__", {})
    codes: set[int] = set()
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        name = node.func.id if isinstance(node.func, ast.Name) else getattr(node.func, "attr", None)
        if name == "HTTPException":
            for kw in node.keywords:
                if kw.arg == "status_code" and isinstance(kw.value, ast.Constant):
                    codes.add(kw.value.value)
        elif isinstance(node.func, ast.Name):
            target = scope.get(node.func.id)
            if inspect.isfunction(target) and target.__module__.startswith("app."):
                codes |= _raised_codes(target, seen, depth + 1)
    return codes


def test_every_status_a_route_raises_is_documented(client):
    from fastapi.routing import APIRoute

    from app.main import app

    documented = app.openapi()["paths"]
    gaps = []
    for top in app.routes:
        routes = getattr(getattr(top, "original_router", None), "routes", [top])
        for route in routes:
            if not isinstance(route, APIRoute) or not route.path.startswith("/api"):
                continue
            if not route.include_in_schema:  # hidden routes (e.g. /api/metrics) have no documentation
                continue
            listed = set()
            for method in route.methods:
                listed |= {int(c) for c in documented[route.path][method.lower()]["responses"] if c.isdigit()}
            missing = _raised_codes(route.endpoint) - listed
            if missing:
                gaps.append(f"{sorted(route.methods)} {route.path}: {sorted(missing)}")
    assert not gaps, "undocumented status codes:\n" + "\n".join(gaps)


# --- every 422 has the same shape ----------------------------------------------
def _assert_422_shape(response):
    assert response.status_code == 422, response.text
    detail = response.json()["detail"]
    assert isinstance(detail, list) and detail, detail
    for entry in detail:
        assert set(entry) == {"type", "loc", "msg"}, entry
        assert isinstance(entry["type"], str) and isinstance(entry["loc"], list)
        assert isinstance(entry["msg"], str) and entry["msg"]


def test_422_from_body_validation_has_the_common_shape(admin_client):
    _assert_422_shape(admin_client.post("/api/bugs", json={"title": ""}))


def test_422_from_out_of_range_value_has_the_common_shape(admin_client):
    _assert_422_shape(admin_client.get(f"/api/bugs/{10 ** 30}"))


@pytest.mark.parametrize("detail,expected_msg", [
    ("Dates must be YYYY-MM-DD", "Dates must be YYYY-MM-DD"),
    ({"message": "Not allowed here", "code": "x"}, "Not allowed here"),
    ({"code": "x"}, "{'code': 'x'}"),
    ([{"type": "value_error", "loc": ["body", "x"], "msg": "bad", "input": "secret"}], "bad"),
])
def test_422_raised_by_routes_is_normalised(client, detail, expected_msg):
    from fastapi import HTTPException

    from app.main import app

    def boom():
        raise HTTPException(status_code=422, detail=detail)

    app.add_api_route("/api/_422_probe", boom)
    r = client.get("/api/_422_probe")
    _assert_422_shape(r)
    assert r.json()["detail"][0]["msg"] == expected_msg


def test_non_422_errors_keep_their_detail(client):
    r = client.get("/api/auth/me")
    assert r.status_code == 401 and r.json()["detail"] == "Not authenticated"


# --- lockout: per email+IP, higher global per-email threshold ------------------
def _login_from(client, ip, password):
    return client.post("/api/auth/login", json={"email": ADMIN["email"], "password": password},
                       headers={"X-Forwarded-For": ip})


@pytest.fixture
def behind_proxy(monkeypatch):
    import app.config as config
    monkeypatch.setattr(config.Settings, "TRUST_PROXY_FORWARDED_FOR", True)
    monkeypatch.setattr(config.Settings, "TRUST_PROXY_HOP_COUNT", 1)


def test_stranger_cannot_lock_the_account_for_its_owner(client, behind_proxy, monkeypatch):
    from app import account_lockout
    # Low enough to trip below the separate 8/min per-IP rate limit on /login.
    monkeypatch.setattr(account_lockout, "_LOGIN_FAIL_LIMIT", 3)
    for _ in range(3):
        assert _login_from(client, "203.0.113.9", "wrong-password-1").status_code == 401
    # The attacker's own address is now refused ...
    assert _login_from(client, "203.0.113.9", "wrong-password-1").status_code == 429
    assert _login_from(client, "203.0.113.9", ADMIN["password"]).status_code == 429
    # ... but the real owner, elsewhere, still signs in.
    assert _login_from(client, "198.51.100.4", ADMIN["password"]).status_code == 200


def test_failures_spread_over_many_addresses_still_lock_the_email(client, behind_proxy, monkeypatch):
    from app import account_lockout
    monkeypatch.setattr(account_lockout, "_LOGIN_FAIL_LIMIT", 3)
    monkeypatch.setattr(account_lockout, "_LOGIN_FAIL_EMAIL_LIMIT", 6)
    for n in range(6):
        assert _login_from(client, f"192.0.2.{n + 1}", "wrong-password-1").status_code == 401
    assert _login_from(client, "198.51.100.77", ADMIN["password"]).status_code == 429


def test_email_limit_is_never_below_the_per_address_limit(client, monkeypatch):
    from app import account_lockout
    monkeypatch.setattr(account_lockout, "_LOGIN_FAIL_EMAIL_LIMIT", 1)
    assert account_lockout._email_limit() >= account_lockout._LOGIN_FAIL_LIMIT


# --- uploads: extension allowlist verified by magic bytes ---------------------
_PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 16
_SAMPLES = {
    "shot.png": _PNG,
    "photo.jpg": b"\xff\xd8\xff\xe0" + b"\x00" * 16,
    "anim.gif": b"GIF89a" + b"\x00" * 16,
    "doc.pdf": b"%PDF-1.7\n" + b"x" * 16,
    "clip.mp4": b"\x00\x00\x00\x18ftypmp42" + b"\x00" * 16,
    "spec.docx": b"PK\x03\x04" + b"\x00" * 16,
    "sheet.xlsx": b"PK\x03\x04" + b"\x00" * 16,
    "old.doc": b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1" + b"\x00" * 16,
    "notes.txt": b"plain notes\n",
    "run.log": b"\x1b[32mok\x1b[0m line\n",
    "rows.csv": b"a,b\n1,2\n",
    "bundle.zip": b"PK\x03\x04" + b"\x00" * 16,
}


def _attach(c, bug_id, name, data, ctype="application/octet-stream"):
    return c.post(f"/api/bugs/{bug_id}/attachments", files={"file": (name, data, ctype)})


@pytest.fixture
def an_item(admin_client):
    p = _project(admin_client, "Uploads")
    return admin_client.post("/api/bugs", json={
        "project_id": p["id"], "title": "Needs files", "priority": "Medium",
        "environment": "DEV"}).json()["id"]


@pytest.mark.parametrize("name", sorted(_SAMPLES))
def test_allowlisted_upload_with_matching_bytes_is_accepted(admin_client, an_item, name):
    r = _attach(admin_client, an_item, name, _SAMPLES[name])
    assert r.status_code in (200, 201), r.text


@pytest.mark.parametrize("name,data", [
    ("tool.exe", b"MZ\x90\x00"),
    ("lib.so", b"\x7fELF\x02\x01"),
    ("page.html", b"<script>alert(1)</script>"),
    ("img.svg", b"<svg xmlns='http://www.w3.org/2000/svg'><script/></svg>"),
    ("app.js", b"alert(1)"),
    ("blob.bin", b"\x01\x02\x03"),
    ("noextension", b"hello"),
    ("macro.docm", b"PK\x03\x04" + b"\x00" * 8),
    ("archive.7z", b"7z\xbc\xaf\x27\x1c"),
])
def test_everything_off_the_allowlist_is_refused(admin_client, an_item, name, data):
    r = _attach(admin_client, an_item, name, data)
    assert r.status_code == 400, r.text
    assert "can't be attached" in r.json()["detail"]


@pytest.mark.parametrize("name,data", [
    ("fake.png", b"MZ\x90\x00this is not a png"),
    ("fake.pdf", b"<html>not a pdf</html>"),
    ("fake.zip", b"MZ\x90\x00"),
    ("fake.mp4", b"\x7fELF\x02\x01\x01\x00\x00"),
    ("fake.txt", b"MZ\x90\x00\x03\x00\x00\x00\x04"),
    ("fake.csv", b"a,b\x00\x00\x01\n"),
])
def test_allowed_extension_with_wrong_bytes_is_refused(admin_client, an_item, name, data):
    r = _attach(admin_client, an_item, name, data)
    assert r.status_code == 400, r.text
    assert "isn't a valid" in r.json()["detail"]


def test_trailing_dot_cannot_hide_a_disallowed_extension(admin_client, an_item):
    assert _attach(admin_client, an_item, "evil.exe.", b"MZ\x90\x00").status_code == 400


def test_stored_content_type_comes_from_the_allowlist_not_the_client(admin_client, an_item):
    r = _attach(admin_client, an_item, "notes.txt", b"hello", ctype="text/html")
    assert r.status_code in (200, 201), r.text
    assert r.json()["content_type"] == "text/plain"


@pytest.fixture
def boot_info_logs(caplog):
    """Listed before ``client`` so the app's INFO startup lines are captured."""
    caplog.set_level(logging.INFO, logger="bug_hunter")


def test_fresh_boot_does_not_reconcile_its_own_seed(boot_info_logs, client, caplog):
    messages = [r.getMessage() for r in caplog.get_records("setup")]
    assert not [m for m in messages if m.startswith("Reconciled")], messages
    # Seeding still produced the full permissive transition set.
    from app.database import SessionLocal
    from app.models import WorkflowTransition
    with SessionLocal() as db:
        assert db.query(WorkflowTransition).count() > 0
