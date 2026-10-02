"""Edge-case branch coverage for modules not reached by the main suite.
Imports live inside tests to respect the client fixture's module purge/rebind.
"""
from __future__ import annotations

import asyncio
import importlib
from datetime import date, datetime, timedelta, timezone

import pytest

from tests.conftest import (
    BOOTSTRAP_EMAIL,
    BOOTSTRAP_PASSWORD,
    any_user,
    default_org_id,
    scope_of,
)


# app/schemas.py — HTML sanitizer + bulk-action dedup validator edge cases
def test_schema_sanitize_rejects_svg_data_url():
    # data:image/svg+xml is scriptable; the src must be dropped entirely.
    from app.schemas import sanitize_html
    out = sanitize_html('<img src="data:image/svg+xml,<script>alert(1)</script>">')
    assert "svg+xml" not in out  # src was dropped (no clean URL)


def test_schema_sanitize_drops_unlisted_tag():
    # Unlisted tags are dropped but their text survives; <marquee> is not RCDATA.
    from app.schemas import sanitize_html
    out = sanitize_html("<marquee>hello</marquee>")
    assert "<marquee" not in out
    assert "hello" in out  # the unlisted end tag is also dropped


def test_schema_sanitize_self_closing_rcdata_does_not_stick():
    # <script/> self-closing: startendtag must early-return so suppression depth isn't stuck.
    from app.schemas import sanitize_html
    out = sanitize_html("<script/>after")
    assert "after" in out
    assert "<script" not in out


def test_bulk_action_dedup_skips_duplicate_id():
    # Dedup preserves first-seen order.
    from app.schemas import BulkActionIn
    ba = BulkActionIn(action="delete", ids=[5, 5, 7, 5])
    assert ba.ids == [5, 7]


# app/chatbot/nlu.py — parser edge branches
def _now():
    return datetime(2026, 6, 21, 12, 0, tzinfo=timezone.utc)


def test_nlu_coerce_bug_id_rejects_non_int_and_out_of_range():
    # Non-integer, over-range, and non-positive values all coerce to None.
    from app.chatbot.nlu import _MAX_BUG_ID, _coerce_bug_id
    assert _coerce_bug_id("not-a-number") is None
    assert _coerce_bug_id(str(_MAX_BUG_ID + 1)) is None  # over range
    assert _coerce_bug_id("0") is None                   # non-positive
    assert _coerce_bug_id("42") == 42


def test_nlu_extract_bug_id_skips_out_of_range_matches():
    # _BUG_ID_RE matches but _coerce_bug_id returns None; all three candidate paths fall through.
    from app.chatbot.nlu import _MAX_BUG_ID, _extract_bug_id
    over = str(_MAX_BUG_ID + 5)
    # Both forms fail to coerce; the trailing word blocks the whole-message-digits path.
    assert _extract_bug_id(f"bug {over} please") is None


def test_nlu_time_window_tolerates_internal_whitespace():
    # Irregular whitespace must be normalised or the time filter is silently dropped.
    from app.chatbot.nlu import _parse_time_window
    tw = _parse_time_window("bugs from this  week please", _now())
    assert tw is not None
    assert tw.label == "this week"


def test_nlu_candidate_name_phrases_skips_empty_phrase():
    # After stripping "'s" the captured phrase is empty, so it is not appended.
    from app.chatbot.nlu import _candidate_name_phrases
    out = _candidate_name_phrases("assigned to 's and reported by 's")
    assert all(phrase for _role, phrase in out)


def test_nlu_action_add_comment_is_none_for_list_verb():
    # 'show comment on #5' is a read; _action_add_comment returns None for list verbs.
    from app.chatbot.nlu import ParsedQuery, _action_add_comment
    pq = ParsedQuery()
    pq.bug_id = 5
    # "comment on" (singular) matches _COMMENT_RE; "show" is a list verb.
    assert _action_add_comment("show comment on #5", pq) is None


def test_nlu_action_create_bug_bare_without_title():
    # _CREATE_BUG_RE matches with no captured title: action_title None, kind still create_bug.
    # (Tail markers start with a space, so _strip_create_bug_tail only returns '' for empty input.)
    from app.chatbot.nlu import (
        ParsedQuery,
        _action_create_bug,
        _strip_create_bug_tail,
    )
    pq = ParsedQuery()
    res = _action_create_bug("create a bug", pq)
    assert res == "create_bug"
    assert pq.action_title is None
    assert _strip_create_bug_tail("") == ""


def test_nlu_action_create_bug_bare_whitespace_only_title():
    # Trailing whitespace after "bug" lets the bare regex match and capture
    # just that whitespace; stripped to "", it must not set action_title.
    from app.chatbot.nlu import ParsedQuery, _action_create_bug
    pq = ParsedQuery()
    res = _action_create_bug("create a bug   ", pq)
    assert res == "create_bug"
    assert pq.action_title is None


# app/reports/engine.py — helpers
def test_engine_parse_prior_status_empty_and_no_match():
    from app.reports.engine import _parse_prior_status
    assert _parse_prior_status("") is None               # empty detail
    assert _parse_prior_status("no arrow here") is None  # no matches


def test_engine_fetch_resolution_skips_unresolved_transition():
    # A transition to a non-resolved state must not be credited.
    from app.reports.engine import _fold_throughput_row
    per_user: dict = {}
    details: list = []
    raw = (
        7, 3, "Dana",
        datetime(2026, 1, 2, tzinfo=timezone.utc),
        "status: 'New' -> 'In Progress'", "Bug", "T", "High", "In Progress", "Apollo",
    )
    _fold_throughput_row(raw, per_user, details)
    assert per_user == {}


def test_engine_fold_throughput_skips_resolved_to_resolved():
    # Resolved -> Closed is not a new resolution; the double-count guard returns early.
    from app.reports.engine import _fold_throughput_row
    per_user: dict = {}
    details: list = []
    raw = (
        9, 4, "Eve",
        datetime(2026, 1, 3, tzinfo=timezone.utc),
        "status: 'Resolved' -> 'Closed'", "Bug", "T", "High", "Closed", "Apollo",
    )
    _fold_throughput_row(raw, per_user, details)
    assert per_user == {}  # already resolved, skipped


def test_engine_utc_date_postgresql_branch():
    # Postgres wraps the column in func.timezone('UTC', col); fake bind reports postgresql.
    from app.models import Bug
    from app.reports.engine import _utc_date

    class _Dialect:
        name = "postgresql"

    class _Bind:
        dialect = _Dialect()

    class _FakeDB:
        def get_bind(self):
            return _Bind()

    expr = _utc_date(_FakeDB(), Bug.created_at)
    assert "timezone" in str(expr).lower()


def test_engine_utc_day_key_accepts_plain_date():
    # A bare date (not a datetime) takes the isoformat() fallback path.
    from app.reports.engine import _utc_day_key
    assert _utc_day_key(date(2026, 6, 21)) == "2026-06-21"


# app/scheduler.py — the async _loop's per-minute guard
def test_scheduler_loop_skips_repeat_minute_and_non_matching(monkeypatch):
    # Same-minute calls must not fire twice; a non-matching new minute is skipped.
    sched = importlib.import_module("app.scheduler")

    # Scripted clock: iter1 fires 07:00; iter2 same minute; iter3 07:01 no match; iter4 cancels.
    times = [
        datetime(2026, 6, 21, 7, 0, 5, tzinfo=timezone.utc),
        datetime(2026, 6, 21, 7, 0, 5, tzinfo=timezone.utc),   # iter1 after-sleep
        datetime(2026, 6, 21, 7, 0, 35, tzinfo=timezone.utc),  # iter2 (repeat min)
        datetime(2026, 6, 21, 7, 0, 35, tzinfo=timezone.utc),
        datetime(2026, 6, 21, 7, 1, 5, tzinfo=timezone.utc),   # iter3 (new minute)
        datetime(2026, 6, 21, 7, 1, 5, tzinfo=timezone.utc),
        datetime(2026, 6, 21, 7, 2, 5, tzinfo=timezone.utc),   # iter4 (terminate)
        datetime(2026, 6, 21, 7, 2, 5, tzinfo=timezone.utc),
    ]
    idx = {"i": 0}
    sleeps = {"n": 0}

    class _FixedDatetime:
        @staticmethod
        def now(_tz=None):
            i = idx["i"]
            idx["i"] = min(i + 1, len(times) - 1)
            return times[i]

    async def fake_sleep(_secs):
        sleeps["n"] += 1
        if sleeps["n"] >= 4:
            raise asyncio.CancelledError()

    monkeypatch.setattr(sched, "datetime", _FixedDatetime)
    monkeypatch.setattr(sched.asyncio, "sleep", fake_sleep)

    ticks = {"n": 0}

    async def fake_tick(_schedule, _tz, _now):
        ticks["n"] += 1

    monkeypatch.setattr(sched, "_tick", fake_tick)

    class _Schedule:
        def matches(self, now):
            # Only minute 0 matches, so iter1 fires and iter3 (07:01) does not.
            return now.minute == 0

    coro = sched._loop(_Schedule(), timezone.utc)
    with pytest.raises(asyncio.CancelledError):
        asyncio.run(coro)

    assert ticks["n"] == 1  # fired exactly once


# app/jobs/email_digest.py — concurrent-claim continue branch
def test_email_digest_skips_already_claimed_rows(client, monkeypatch):
    # A concurrent runner stamps emailed_at inside patched _group_by_user, so the guarded UPDATE claims 0 rows.
    from app import models
    from app.auth import hash_password
    from app.database import SessionLocal
    from app.jobs import email_digest

    db = SessionLocal()
    try:
        u = models.User(org_id=default_org_id(), name="Digest User", email="digest@test.local", role="user",
                        password_hash=hash_password("User12345"), is_active=True)
        db.add(u)
        db.flush()
        now = datetime.now(timezone.utc)
        n = models.Notification(
            user_id=u.id, kind="updated", title="t", body="b",
            created_at=now, emailed_at=None,
        )
        db.add(n)
        db.commit()
        uid = u.id
        nid = n.id
    finally:
        db.close()

    orig_group = email_digest._group_by_user

    def racing_group(rows):
        other = SessionLocal()
        try:
            other.query(models.Notification).filter(
                models.Notification.id == nid
            ).update({models.Notification.emailed_at: datetime.now(timezone.utc)},
                     synchronize_session=False)
            other.commit()
        finally:
            other.close()
        return orig_group(rows)

    monkeypatch.setattr(email_digest, "_group_by_user", racing_group)

    db = SessionLocal()
    try:
        res = email_digest.run_digest(db, now=now + timedelta(minutes=1))
    finally:
        db.close()
    assert res["emails_sent"] == 0  # claim lost the race, skipped
    assert uid  # user existed


# app/auth.py — SESSION_REQUIRE_JTI legacy jti-less path
def test_auth_legacy_jtiless_session_accepted_when_not_required(client):
    # A jti-less cookie is accepted when SESSION_REQUIRE_JTI is False.
    from sqlalchemy import select

    from app.auth import COOKIE_NAME, make_session_token
    from app.config import get_settings
    from app.database import SessionLocal
    from app.models import User

    s = get_settings()
    s.__class__.SESSION_REQUIRE_JTI = False

    db = SessionLocal()
    try:
        admin = db.scalar(select(User).where(User.email == BOOTSTRAP_EMAIL))
        token = make_session_token(admin.id, admin.session_version or 0, jti=None)
    finally:
        db.close()

    client.cookies.set(COOKIE_NAME, token)
    r = client.get("/api/auth/me")
    assert r.status_code == 200
    assert r.json()["email"] == BOOTSTRAP_EMAIL


def test_auth_legacy_jtiless_session_rejected_when_required(client):
    # A jti-less cookie is rejected (401) when SESSION_REQUIRE_JTI is True.
    from sqlalchemy import select

    from app.auth import COOKIE_NAME, make_session_token
    from app.config import get_settings
    from app.database import SessionLocal
    from app.models import User

    s = get_settings()
    s.__class__.SESSION_REQUIRE_JTI = True
    try:
        db = SessionLocal()
        try:
            admin = db.scalar(select(User).where(User.email == BOOTSTRAP_EMAIL))
            token = make_session_token(admin.id, admin.session_version or 0, jti=None)
        finally:
            db.close()
        client.cookies.set(COOKIE_NAME, token)
        r = client.get("/api/auth/me")
        assert r.status_code == 401
    finally:
        s.__class__.SESSION_REQUIRE_JTI = False


# app/routes/auth.py - change-password same-pw reject + tz-aware reset expiry.
def test_change_password_rejects_same_password(admin_client):
    r = admin_client.post("/api/auth/change-password", json={
        "current_password": BOOTSTRAP_PASSWORD,
        "new_password": BOOTSTRAP_PASSWORD,
    })
    assert r.status_code == 400
    assert "different" in r.json()["detail"].lower()


def test_reset_password_tzaware_expiry_branch(client):
    # SQLite reads tz columns naive; a one-shot load listener coerces expires_at aware to mimic Postgres.
    from sqlalchemy import event, select

    from app.auth import generate_token
    from app.database import SessionLocal
    from app.models import PasswordResetToken, User

    db = SessionLocal()
    try:
        admin = db.scalar(select(User).where(User.email == BOOTSTRAP_EMAIL))
        raw, token_hash = generate_token()
        prt = PasswordResetToken(
            user_id=admin.id,
            token_hash=token_hash,
            expires_at=datetime.now(timezone.utc) + timedelta(hours=2),
            used_at=None,
        )
        db.add(prt)
        db.commit()
    finally:
        db.close()

    def _make_aware(target, _context):
        if target.expires_at is not None and target.expires_at.tzinfo is None:
            target.expires_at = target.expires_at.replace(tzinfo=timezone.utc)

    event.listen(PasswordResetToken, "load", _make_aware)
    try:
        res = client.post("/api/auth/reset-password", json={
            "token": raw, "new_password": "BrandNewPass987",
        })
    finally:
        event.remove(PasswordResetToken, "load", _make_aware)
    assert res.status_code in (200, 204)


# app/routes/push.py — token already bound to another account
def test_push_subscribe_conflict_returns_409(admin_client):
    # A token bound to one account is 409 when a second user claims it.
    admin_client.post("/api/push/subscribe", json={"token": "shared-token", "platform": "web"})
    # Create and log in as a second user; reuse the same token.
    admin_client.post("/api/users", json={
        "name": "Second", "email": "second@test.local", "role": "user",
        "password": "User12345",
    })
    admin_client.post("/api/auth/logout")
    admin_client.post("/api/auth/login", json={
        "email": "second@test.local", "password": "User12345",
    })
    r = admin_client.post("/api/push/subscribe", json={"token": "shared-token"})
    assert r.status_code == 409
    assert "another account" in r.json()["detail"].lower()


# app/routes/sessions.py — empty list + sweep error rollback
def test_sessions_list_empty_skips_user_prefetch(client):
    # Empty sessions table skips user-prefetch; a legacy jti-less cookie lets us wipe the table and stay admin.
    from sqlalchemy import select

    from app.auth import COOKIE_NAME, make_session_token
    from app.config import get_settings
    from app.database import SessionLocal
    from app.models import Session as SessionRow
    from app.models import User

    get_settings().__class__.SESSION_REQUIRE_JTI = False

    db = SessionLocal()
    try:
        admin = db.scalar(select(User).where(User.email == BOOTSTRAP_EMAIL))
        token = make_session_token(admin.id, admin.session_version or 0, jti=None)
        db.query(SessionRow).delete()
        db.commit()
    finally:
        db.close()

    client.cookies.set(COOKIE_NAME, token)
    r = client.get("/api/sessions")
    assert r.status_code == 200
    assert r.json() == []


def test_sessions_list_survives_sweep_error(admin_client, monkeypatch):
    # SQLAlchemyError during the sweep is caught and rolled back; listing still returns.
    from sqlalchemy.exc import SQLAlchemyError

    import app.routes.sessions as sessions_mod

    real_execute_marker = {"raised": False}

    def boom_delete(*args, **kwargs):
        real_execute_marker["raised"] = True
        raise SQLAlchemyError("sweep boom")

    monkeypatch.setattr(sessions_mod, "delete", boom_delete)
    r = admin_client.get("/api/sessions")
    assert r.status_code == 200
    assert real_execute_marker["raised"] is True


# app/main.py - asset hash, body-limit ASGI, cache headers, rate buckets, IP, CSRF.
class _FakeURL:
    def __init__(self, path):
        self.path = path


class _FakeClient:
    def __init__(self, host):
        self.host = host


class _FakeReq:
    def __init__(self, path="/api/x", method="POST", headers=None, client_host="1.2.3.4"):
        self.url = _FakeURL(path)
        self.method = method
        self.headers = headers or {}
        self.client = _FakeClient(client_host) if client_host else None
        self.cookies = {}


async def _ok_no_cache(_request):
    from starlette.responses import Response
    return Response("ok")


def test_main_asset_version_large_file_uses_size(tmp_path, monkeypatch):
    # Over-cap files are hashed by size (not read); shrink the cap to trip the branch.
    import app.main as main
    monkeypatch.setattr(main, "_MAX_ASSET_FILE_BYTES", 4)
    big = tmp_path / "big.js"
    big.write_text("0123456789")  # 10 bytes > 4-byte cap
    v = main._compute_asset_version(tmp_path)
    assert v
    assert v != "dev"


def test_main_cache_control_static_non_fingerprinted(monkeypatch):
    # Non-assets /static/ paths get the 1-hour header, not the immutable one.
    import app.main as main
    mw = main.CacheControlMiddleware(app=None)
    req = _FakeReq(path="/static/firebase-messaging-sw.js", method="GET")
    resp = asyncio.run(mw.dispatch(req, _ok_no_cache))
    assert resp.headers["Cache-Control"] == "public, max-age=3600"


def test_main_body_limit_skips_non_request_messages():
    # Non-http.request messages (http.disconnect) pass through without the byte counter.
    import app.main as main

    received = {"msgs": []}

    async def app_inner(_scope, receive, _send):
        msg = await receive()
        received["msgs"].append(msg)

    async def receive():
        return {"type": "http.disconnect"}

    async def send(_msg):
        return None

    mw = main.StreamingBodyLimitMiddleware(app_inner)
    scope = {"type": "http", "path": "/api/x"}
    asyncio.run(mw(scope, receive, send))
    assert received["msgs"] == [{"type": "http.disconnect"}]


def test_main_body_limit_413_on_oversized_body(monkeypatch):
    # Oversized body triggers 413 when the response hasn't started.
    import app.main as main
    monkeypatch.setattr(main.settings, "MAX_REQUEST_BODY_BYTES", 8)

    async def app_inner(_scope, receive, _send):
        await receive()

    async def receive():
        return {"type": "http.request", "body": b"x" * 100, "more_body": False}

    sent = []

    async def send(msg):
        sent.append(msg)

    mw = main.StreamingBodyLimitMiddleware(app_inner)
    scope = {"type": "http", "path": "/api/x"}
    asyncio.run(mw(scope, receive, send))
    starts = [m for m in sent if m.get("type") == "http.response.start"]
    assert starts
    assert starts[0]["status"] == 413


def test_main_body_limit_reraises_when_response_started(monkeypatch):
    # Response already started: no clean 413, so _RequestBodyTooLarge is re-raised.
    import app.main as main
    monkeypatch.setattr(main.settings, "MAX_REQUEST_BODY_BYTES", 8)

    async def app_inner(_scope, receive, send):
        # Start the response before reading the oversized body.
        await send({"type": "http.response.start", "status": 200, "headers": []})
        await receive()  # this read overflows, raising _RequestBodyTooLarge

    async def receive():
        return {"type": "http.request", "body": b"x" * 100, "more_body": False}

    async def send(_msg):
        return None

    mw = main.StreamingBodyLimitMiddleware(app_inner)
    scope = {"type": "http", "path": "/api/x"}
    coro = mw(scope, receive, send)
    with pytest.raises(main._RequestBodyTooLarge):
        asyncio.run(coro)


def test_main_evict_dead_rate_bucket(monkeypatch):
    # Buckets past the eviction horizon and empty buckets are deleted; under cap, nothing more.
    import time as _time

    import app.main as main
    main._rate_buckets.clear()
    old = _time.monotonic() - 10_000  # older than _MAX_RATE_WINDOW
    main._rate_buckets[("/old", "ipX")] = main.deque([old])
    main._rate_buckets[("/empty", "ipY")] = main.deque()
    main._evict_one_rate_bucket(_time.monotonic())
    assert ("/old", "ipX") not in main._rate_buckets
    assert ("/empty", "ipY") not in main._rate_buckets
    main._rate_buckets.clear()


def test_main_client_ip_trusted_xff_none_falls_through(monkeypatch):
    # TRUST_PROXY on but trusted_forwarded_ip None: fall back to request.client.host.
    import app.main as main
    monkeypatch.setattr(main.settings, "TRUST_PROXY_FORWARDED_FOR", True)
    monkeypatch.setattr(main.settings, "TRUST_PROXY_HOP_COUNT", 5)
    monkeypatch.setattr(main, "trusted_forwarded_ip", lambda _xff, _hops: None)
    req = _FakeReq(headers={"x-forwarded-for": "1.1.1.1"}, client_host="9.9.9.9")
    assert main._client_ip(req) == "9.9.9.9"


def test_main_csrf_origin_present_not_allowed_blocks(monkeypatch):
    # Unknown Origin is 403. ('elif referer:' false is unreachable: the earlier guard returns first.)
    import app.main as main
    monkeypatch.setattr(main, "_allowed_origins", lambda: set())
    mw = main.CsrfOriginMiddleware(app=None)
    req = _FakeReq(path="/api/projects", method="POST",
                   headers={"origin": "https://evil.example.com"})  # no host/referer
    resp = asyncio.run(mw.dispatch(req, _ok_no_cache))
    assert resp.status_code == 403


def test_main_has_valid_session_missing_row_returns_false(client):
    # A valid-looking token whose jti was never persisted must be rejected.
    from sqlalchemy import select

    import app.main as main
    from app.auth import COOKIE_NAME, make_session_token, new_jti
    from app.database import SessionLocal
    from app.models import User

    db = SessionLocal()
    try:
        admin = db.scalar(select(User).where(User.email == BOOTSTRAP_EMAIL))
        token = make_session_token(admin.id, admin.session_version or 0, jti=new_jti())
    finally:
        db.close()

    req = _FakeReq(method="GET", headers={})
    req.cookies = {COOKIE_NAME: token}
    assert main._has_valid_session(req) is False


# app/chatbot/actions.py — write-path edge branches
def _seed_chat_world(client):
    """Seed two users, an inactive user, a project, and a bug; return a dict of ids."""
    from app import models
    from app.auth import hash_password
    from app.database import SessionLocal

    db = SessionLocal()
    try:
        alice = models.User(org_id=default_org_id(), name="Alice Apex", email="alice@chat.local", role="admin",
                            password_hash=hash_password("Alice12345"), is_active=True)
        bob = models.User(org_id=default_org_id(), name="Bob Base", email="bob@chat.local", role="user",
                          password_hash=hash_password("Bob123456"), is_active=True)
        dead = models.User(org_id=default_org_id(), name="Dee Gone", email="dee@chat.local", role="user",
                           password_hash=hash_password("Dee1234567"), is_active=False)
        db.add_all([alice, bob, dead])
        db.flush()
        proj = models.Project(org_id=default_org_id(), name="Chatland", description="")
        db.add(proj)
        db.flush()
        bug = models.Bug(title="Seeded", description="d", status="New",
                         priority="Medium", environment="DEV",
                         project_id=proj.id, reporter_id=alice.id)
        db.add(bug)
        db.flush()
        bug.assignees = [bob]
        db.commit()
        ids = {
            "alice": alice.id, "bob": bob.id, "dead": dead.id,
            "proj": proj.id, "bug": bug.id,
        }
    finally:
        db.close()
    return ids


def test_actions_create_project_denied_for_regular_user(client):
    # A regular (non-admin/manager) user must be denied project creation.
    from app.chatbot import actions
    from app.database import SessionLocal
    from app.models import User

    ids = _seed_chat_world(client)
    db = SessionLocal()
    try:
        bob = db.get(User, ids["bob"])
        plan = actions.ActionPlan(kind="create_project", actor_user_id=bob.id,
                                  new_project_name="Nope")
        resp = actions._apply_create_project(db, plan, bob)
    finally:
        db.close()
    assert "admins or managers" in resp.summary.lower() or resp.intent == "action_error"


def test_actions_resolve_targets_empty_and_inactive(client):
    # Empty id list returns an error; a deactivated user can't be assigned.
    from app import models
    from app.chatbot import actions
    from app.database import SessionLocal

    ids = _seed_chat_world(client)
    db = SessionLocal()
    try:
        actor = db.get(models.User, ids["alice"])
        users, err = actions._resolve_targets(db, actor, [])
        assert users == []
        assert "find the user" in err.lower()
        users2, err2 = actions._resolve_targets(db, actor, [ids["dead"]])
        assert users2 == []
        assert "deactivated" in err2.lower()
    finally:
        db.close()


def test_actions_assign_noop_when_already_assigned(client):
    from app.chatbot import actions
    from app.database import SessionLocal
    from app.models import User

    ids = _seed_chat_world(client)
    db = SessionLocal()
    try:
        admin = db.get(User, ids["alice"])
        plan = actions.ActionPlan(kind="assign", actor_user_id=admin.id,
                                  bug_id=ids["bug"], target_user_ids=[ids["bob"]])
        resp = actions._apply_assign(db, plan, admin, notify=False, commit=False)
    finally:
        db.close()
    assert resp.intent == actions._INTENT_NOOP


def test_actions_notify_chat_op_skips_when_no_reporter(client):
    # reporter_id is None, so the reporter-notify branch is skipped without raising.
    from app.chatbot import actions
    from app.database import SessionLocal
    from app.models import Bug, User

    ids = _seed_chat_world(client)
    db = SessionLocal()
    try:
        bug = db.get(Bug, ids["bug"])
        bug.reporter_id = None
        db.flush()
        admin = db.get(User, ids["alice"])
        actions._notify_chat_op(db, bug, admin, kind="updated",
                                title="t", body="b")
        db.rollback()
    finally:
        db.close()


def test_actions_add_comment_no_notify_no_commit(client):
    # notify=False skips the notify block; commit=False skips the commit.
    from app.chatbot import actions
    from app.database import SessionLocal
    from app.models import User

    ids = _seed_chat_world(client)
    db = SessionLocal()
    try:
        admin = db.get(User, ids["alice"])
        plan = actions.ActionPlan(kind="add_comment", actor_user_id=admin.id,
                                  bug_id=ids["bug"], comment_body="hello there")
        resp = actions._apply_add_comment(db, plan, admin, notify=False, commit=False)
        db.rollback()
    finally:
        db.close()
    assert "Comment posted" in resp.summary


def test_actions_create_bug_invalid_priority_and_bad_assignee(client):
    from app.chatbot import actions
    from app.database import SessionLocal
    from app.models import User

    ids = _seed_chat_world(client)
    db = SessionLocal()
    try:
        admin = db.get(User, ids["alice"])
        bad_pri = actions.ActionPlan(kind="create_bug", actor_user_id=admin.id,
                                     new_title="T", new_value="Critical-ish",
                                     new_project_id=ids["proj"])
        r1 = actions._apply_create_bug(db, bad_pri, admin)
        assert "valid priority" in r1.summary.lower()

        bad_assignee = actions.ActionPlan(
            kind="create_bug", actor_user_id=admin.id, new_title="T2",
            new_value="High", new_project_id=ids["proj"],
            target_user_ids=[999999])
        r2 = actions._apply_create_bug(db, bad_assignee, admin)
        assert r2.intent == "action_error"
        db.rollback()
    finally:
        db.close()


def test_actions_execute_plan_denied_for_non_admin(client, monkeypatch):
    # execute_plan re-checks sleuth_write_denied at execute time, before any DB write.
    from app.chatbot import actions
    from app.database import SessionLocal
    from app.models import User

    ids = _seed_chat_world(client)
    monkeypatch.setattr(actions, "sleuth_write_denied", lambda _actor: "Sleuth writes are admin-only")
    db = SessionLocal()
    try:
        admin = db.get(User, ids["alice"])
        plan = actions.ActionPlan(kind="add_comment", actor_user_id=admin.id,
                                  bug_id=ids["bug"], comment_body="x")
        resp = actions.execute_plan(plan, db, admin)
    finally:
        db.close()
    assert resp.intent == "action_error"
    assert "admin-only" in resp.summary.lower()


def test_actions_bulk_all_skipped_rolls_back(client):
    # Bulk-assign an already-assigned user everywhere: all no-ops, updated==0, rollback.
    from app.chatbot import actions
    from app.database import SessionLocal
    from app.models import User

    ids = _seed_chat_world(client)
    db = SessionLocal()
    try:
        admin = db.get(User, ids["alice"])
        plan = actions.ActionPlan(
            kind="assign", actor_user_id=admin.id,
            bug_ids=[ids["bug"]], target_user_ids=[ids["bob"]],
            summary_human="assign Bob")
        resp = actions._apply_bulk(plan, db, admin)
    finally:
        db.close()
    assert "0" in resp.summary  # 0 updated, rollback path


# app/chatbot/executor.py — bulk / report / ingest branches
def test_executor_bulk_kind_detects_unassign(client):
    # An unassign phrase with no parsed action_kind resolves to "unassign".
    from app.chatbot import executor
    from app.chatbot.nlu import ParsedQuery
    pq = ParsedQuery()
    kind, value = executor._bulk_kind_and_value("unassign everyone from all the bugs", pq)
    assert kind == "unassign"
    assert value is None


def test_executor_resolve_bulk_filters_by_environment(client):
    from app.chatbot import executor
    from app.chatbot.nlu import ParsedQuery
    from app.database import SessionLocal

    ids = _seed_chat_world(client)  # bug is environment=DEV
    pq = ParsedQuery()
    pq.environments = ["DEV"]
    db = SessionLocal()
    try:
        bug_ids = executor._resolve_bulk_bug_ids(db, pq, None)
    finally:
        db.close()
    assert ids["bug"] in bug_ids


def test_executor_bulk_set_status_without_value_returns_none(client):
    # No target value: bulk handler defers (None) so the normal flow can ask.
    from app.chatbot import executor
    from app.chatbot.nlu import ParsedQuery
    from app.database import SessionLocal
    from app.models import User

    ids = _seed_chat_world(client)
    db = SessionLocal()
    try:
        admin = db.get(User, ids["alice"])
        pq = ParsedQuery()
        pq.action_kind = "set_status"
        pq.action_value = None  # missing value
        ctx = executor.build_context(db, any_user(db))
        res = executor._maybe_handle_bulk_action(
            "change all the bugs", db, admin, pq, ctx)
    finally:
        db.close()
    assert res is None


def test_executor_bulk_over_cap_refused(client, monkeypatch):
    from app import models
    from app.chatbot import executor
    from app.chatbot.nlu import ParsedQuery
    from app.database import SessionLocal
    from app.models import User

    ids = _seed_chat_world(client)
    monkeypatch.setattr(executor, "_BULK_ACTION_CAP", 1)  # 2 bugs now exceed it
    db = SessionLocal()
    try:
        # Two bugs in scope now exceed cap(1).
        extra = models.Bug(title="Extra", description="d", status="New",
                           priority="Low", environment="DEV",
                           project_id=ids["proj"], reporter_id=ids["alice"])
        db.add(extra)
        db.commit()
        admin = db.get(User, ids["alice"])
        pq = ParsedQuery()
        pq.action_kind = "set_status"
        pq.action_value = "Resolved"
        ctx = executor.build_context(db, any_user(db))
        res = executor._maybe_handle_bulk_action(
            "resolve all the bugs", db, admin, pq, ctx)
    finally:
        db.close()
    assert res is not None
    assert res.intent == "action_invalid"
    assert "too many" in res.summary.lower() or "more than" in str(res.blocks).lower()


def test_executor_report_too_large_413(client, monkeypatch):
    # Truncated report takes the 'report too large' path instead of building the workbook.
    from app.chatbot import executor
    from app.chatbot.nlu import ParsedQuery
    from app.database import SessionLocal
    from app.models import User

    ids = _seed_chat_world(client)

    class _FakeResult:
        total = 10_000_000
        truncated = True
        rows = []
        columns = []

    # _handle_report imports run_report inside the function, so patch app.reports directly.
    import app.reports as _reports
    monkeypatch.setattr(_reports, "run_report", lambda *_a, **_k: _FakeResult())

    db = SessionLocal()
    try:
        admin = db.get(User, ids["alice"])
        admin.role = "admin"
        db.flush()
        pq = ParsedQuery()
        pq.raw_message = "report on all bugs export"
        res = executor._handle_report(db, pq, admin, scope_of(db, admin))
    finally:
        db.close()
    assert res.intent == "report_too_large"


def test_executor_ingest_created_response_no_items_skips_table(client):
    # Empty items: table skipped but 'and N more' tail still appended (created > len(items)).
    from app.chatbot import executor
    summary = {"created": 3, "project_name": "Apollo", "items": []}
    res = executor._ingest_created_response(summary)
    assert res.intent == "ingest_done"
    assert all(b.kind != "table" for b in res.blocks)


def test_executor_ingest_created_response_exact_fit(client):
    # created == len(items): a table block is added and no "and N more" tail.
    from app.chatbot import executor
    summary = {
        "created": 2,
        "project_name": "Apollo",
        "items": [{"id": 1, "title": "A"}, {"id": 2, "title": "B"}],
    }
    res = executor._ingest_created_response(summary)
    assert res.intent == "ingest_done"
    assert len(res.blocks) == 2  # text + table, no "more" tail


# app/routes/bugs.py — helpers + route edge cases
def test_bugs_dangerous_ext_no_extension_returns_none(client):
    from app.routes.bugs import _dangerous_upload_ext
    assert _dangerous_upload_ext("README") is None
    assert _dangerous_upload_ext("evil.exe") == "exe"


def test_bugs_resolve_user_unknown_raises_400(client):
    # None is allowed (no filter); a non-existent id is a hard 400.
    from fastapi import HTTPException

    from app.database import SessionLocal
    from app.routes.bugs import _resolve_user
    db = SessionLocal()
    try:
        org_id = default_org_id()
        assert _resolve_user(db, None, org_id) is None
        with pytest.raises(HTTPException) as ei:
            _resolve_user(db, 999999, org_id)
        assert ei.value.status_code == 400
    finally:
        db.close()


def test_bugs_reject_overflow_ids_raises_422(client):
    # An id filter outside the int4 range is a clean 422.
    from fastapi import HTTPException

    from app.routes.bugs import _MAX_PK_INT, _reject_overflow_ids
    with pytest.raises(HTTPException) as ei:
        _reject_overflow_ids(reporter_id=_MAX_PK_INT + 1)
    assert ei.value.status_code == 422


def test_bugs_directional_link_reaches_multi_hop(client):
    # BFS walks an intermediate node before finding the goal one hop further.
    from sqlalchemy import select

    from app import models
    from app.database import SessionLocal
    from app.routes.bugs import _directional_link_reaches

    db = SessionLocal()
    try:
        actor = db.scalar(
            select(models.User).where(models.User.email == BOOTSTRAP_EMAIL))
        proj = models.Project(org_id=default_org_id(), name="LinkProj", description="")
        db.add(proj)
        db.flush()
        a = models.Bug(title="A", description="", status="New", priority="Low",
                       environment="DEV", project_id=proj.id, reporter_id=actor.id)
        b = models.Bug(title="B", description="", status="New", priority="Low",
                       environment="DEV", project_id=proj.id, reporter_id=actor.id)
        c = models.Bug(title="C", description="", status="New", priority="Low",
                       environment="DEV", project_id=proj.id, reporter_id=actor.id)
        db.add_all([a, b, c])
        db.flush()
        # A --blocks--> B --blocks--> C; reaching C from A walks through B.
        db.add_all([
            models.BugLink(source_bug_id=a.id, target_bug_id=b.id,
                           link_type="blocks", created_by_user_id=actor.id),
            models.BugLink(source_bug_id=b.id, target_bug_id=c.id,
                           link_type="blocks", created_by_user_id=actor.id),
        ])
        db.commit()
        reached = _directional_link_reaches(db, a.id, c.id, "blocks")
    finally:
        db.close()
    assert reached is True


def test_bugs_directional_link_reaches_diamond_revisits_node(client):
    # A --blocks--> B --blocks--> E and A --blocks--> C --blocks--> E, then
    # E --blocks--> D (goal): the same frontier level discovers E twice (via B
    # and via C) before D is reached, so the second discovery must skip E as
    # already-seen rather than re-queuing it.
    from sqlalchemy import select

    from app import models
    from app.database import SessionLocal
    from app.routes.bugs import _directional_link_reaches

    db = SessionLocal()
    try:
        actor = db.scalar(
            select(models.User).where(models.User.email == BOOTSTRAP_EMAIL))
        proj = models.Project(org_id=default_org_id(), name="DiamondProj", description="")
        db.add(proj)
        db.flush()
        a, b, c, e, d = (
            models.Bug(title=t, description="", status="New", priority="Low",
                      environment="DEV", project_id=proj.id, reporter_id=actor.id)
            for t in ("A", "B", "C", "E", "D")
        )
        db.add_all([a, b, c, e, d])
        db.flush()
        db.add_all([
            models.BugLink(source_bug_id=a.id, target_bug_id=b.id,
                           link_type="blocks", created_by_user_id=actor.id),
            models.BugLink(source_bug_id=a.id, target_bug_id=c.id,
                           link_type="blocks", created_by_user_id=actor.id),
            models.BugLink(source_bug_id=b.id, target_bug_id=e.id,
                           link_type="blocks", created_by_user_id=actor.id),
            models.BugLink(source_bug_id=c.id, target_bug_id=e.id,
                           link_type="blocks", created_by_user_id=actor.id),
            models.BugLink(source_bug_id=e.id, target_bug_id=d.id,
                           link_type="blocks", created_by_user_id=actor.id),
        ])
        db.commit()
        reached = _directional_link_reaches(db, a.id, d.id, "blocks")
    finally:
        db.close()
    assert reached is True


def test_bugs_reject_updated_at_drift_aware_current(client):
    # tz-aware current_updated skips naive coercion; same instant, no conflict.
    from app.routes.bugs import _reject_if_updated_at_drifted
    aware = datetime(2026, 6, 21, 12, 0, 0, tzinfo=timezone.utc)
    _reject_if_updated_at_drifted(aware.isoformat(), aware)


def test_bugs_list_deep_page_returns_empty(admin_client):
    # A page far past the end short-circuits to [] instead of a deep OFFSET scan.
    proj = admin_client.post("/api/projects", json={"name": "DeepPage"}).json()
    admin_client.post("/api/bugs", json={
        "project_id": proj["id"], "title": "only one", "priority": "Low",
        "environment": "DEV",
    })
    r = admin_client.get("/api/bugs?page=10000000&page_size=20")
    assert r.status_code == 200
    assert r.json()["items"] == []


def test_bugs_attachment_dangerous_ext_rejected(admin_client):
    proj = admin_client.post("/api/projects", json={"name": "AttProj"}).json()
    bug = admin_client.post("/api/bugs", json={
        "project_id": proj["id"], "title": "needs file", "priority": "Low",
        "environment": "DEV",
    }).json()
    r = admin_client.post(
        f"/api/bugs/{bug['id']}/attachments",
        files={"file": ("evil.exe", b"MZ\x90\x00", "application/octet-stream")},
    )
    assert r.status_code == 400
    assert "exe" in r.json()["detail"].lower()


def _mk_user_via_api(admin_client, name, email):
    r = admin_client.post("/api/users", json={
        "name": name, "email": email, "role": "user", "password": "User12345",
    })
    assert r.status_code == 201, r.text
    return r.json()["id"]


def test_bugs_update_changes_reporter_and_notifies(admin_client):
    # Reporter change is recorded; a tracked-field update stages 'updated' notifications.
    uid = _mk_user_via_api(admin_client, "Redo", "redo@test.local")
    proj = admin_client.post("/api/projects", json={"name": "UpdProj"}).json()
    bug = admin_client.post("/api/bugs", json={
        "project_id": proj["id"], "title": "orig", "priority": "Low",
        "environment": "DEV", "assignee_ids": [uid],
    }).json()
    r = admin_client.put(f"/api/bugs/{bug['id']}", json={
        "title": "renamed", "reporter_id": uid,
    })
    assert r.status_code == 200, r.text
    assert r.json()["title"] == "renamed"
    assert r.json()["reporter"]["id"] == uid


def test_bugs_update_reporter_change_same_display_name_not_tracked(admin_client):
    # Two different users sharing a display name: the reporter_id genuinely
    # changes, and even though the plain display-name label is identical, the
    # write must not be silently rolled back as a no-op — the audit entry
    # disambiguates via email instead.
    uid1 = _mk_user_via_api(admin_client, "Same Name", "same1@test.local")
    uid2 = _mk_user_via_api(admin_client, "Same Name", "same2@test.local")
    proj = admin_client.post("/api/projects", json={"name": "SameNameProj"}).json()
    bug = admin_client.post("/api/bugs", json={
        "project_id": proj["id"], "title": "orig", "priority": "Low",
        "environment": "DEV", "reporter_id": uid1,
    }).json()
    r = admin_client.put(f"/api/bugs/{bug['id']}", json={"reporter_id": uid2})
    assert r.status_code == 200, r.text
    assert r.json()["reporter"]["id"] == uid2
    activity = admin_client.get(f"/api/bugs/{bug['id']}/activity").json()
    assert any(
        "reporter" in a["detail"].lower() and "same2@test.local" in a["detail"]
        for a in activity
    )


def test_bugs_update_legacy_invalid_status_tolerated(admin_client):
    # Legacy status invalid for the current type is tolerated when unchanged by the update.
    from app.database import SessionLocal
    from app.models import Bug
    proj = admin_client.post("/api/projects", json={"name": "LegacyProj"}).json()
    bug = admin_client.post("/api/bugs", json={
        "project_id": proj["id"], "title": "legacy", "priority": "Low",
        "environment": "DEV",
    }).json()
    # 'Done' is Task-only; force it directly to simulate a legacy row.
    db = SessionLocal()
    try:
        row = db.get(Bug, bug["id"])
        row.status = "Done"  # valid enum, but not valid for item_type Bug
        db.commit()
    finally:
        db.close()
    r = admin_client.put(f"/api/bugs/{bug['id']}", json={
        "title": "legacy-renamed", "status": "Done",
    })
    assert r.status_code == 200, r.text


def test_bugs_add_reverse_relates_link_is_idempotent(admin_client):
    # Reverse-direction 'relates' returns the existing edge, no duplicate.
    proj = admin_client.post("/api/projects", json={"name": "RelProj"}).json()
    a = admin_client.post("/api/bugs", json={
        "project_id": proj["id"], "title": "Aye bug", "priority": "Low", "environment": "DEV",
    }).json()
    b = admin_client.post("/api/bugs", json={
        "project_id": proj["id"], "title": "Bee bug", "priority": "Low", "environment": "DEV",
    }).json()
    r1 = admin_client.post(f"/api/bugs/{a['id']}/links", json={
        "target_bug_id": b["id"], "link_type": "relates",
    })
    assert r1.status_code == 201, r1.text
    r2 = admin_client.post(f"/api/bugs/{b['id']}/links", json={
        "target_bug_id": a["id"], "link_type": "relates",
    })
    assert r2.status_code in (200, 201)


def test_bugs_remove_link_other_endpoint_present(admin_client):
    # Other endpoint exists, so the authz/stakeholder-notification branch is taken.
    proj = admin_client.post("/api/projects", json={"name": "DelLink"}).json()
    a = admin_client.post("/api/bugs", json={
        "project_id": proj["id"], "title": "Aye bug", "priority": "Low", "environment": "DEV",
    }).json()
    b = admin_client.post("/api/bugs", json={
        "project_id": proj["id"], "title": "Bee bug", "priority": "Low", "environment": "DEV",
    }).json()
    link = admin_client.post(f"/api/bugs/{a['id']}/links", json={
        "target_bug_id": b["id"], "link_type": "blocks",
    }).json()
    r = admin_client.delete(f"/api/bugs/{a['id']}/links/{link['id']}")
    assert r.status_code == 200, r.text


def test_bugs_attachment_grows_past_limit_after_strip(admin_client, monkeypatch):
    # Stripping can re-encode larger; post-strip re-check must 413 over the cap.
    import app.routes.bugs as bugs_mod
    monkeypatch.setattr(bugs_mod, "MAX_FILE_BYTES", 100)
    monkeypatch.setattr(bugs_mod, "strip_image_metadata", lambda _d, _ct: b"x" * 500)

    proj = admin_client.post("/api/projects", json={"name": "GrowProj"}).json()
    bug = admin_client.post("/api/bugs", json={
        "project_id": proj["id"], "title": "grow test", "priority": "Low",
        "environment": "DEV",
    }).json()
    r = admin_client.post(
        f"/api/bugs/{bug['id']}/attachments",
        files={"file": ("pic.png", b"\x89PNG\r\n", "image/png")},
    )
    assert r.status_code == 413


def test_bugs_reload_link_missing_raises_409(client):
    # A concurrently deleted row (db.scalar returns None) must give a clean 409.
    from fastapi import HTTPException

    from app.routes.bugs import _reload_link

    class _FakeDB:
        def scalar(self, _stmt):
            return None

    fake_db = _FakeDB()
    with pytest.raises(HTTPException) as ei:
        _reload_link(fake_db, 123)
    assert ei.value.status_code == 409


def test_bugs_insert_link_race_returns_existing(admin_client, monkeypatch):
    # Losing the unique-index race returns the existing edge idempotently, not a 500.
    import app.routes.bugs as bugs_mod

    proj = admin_client.post("/api/projects", json={"name": "RaceProj"}).json()
    a = admin_client.post("/api/bugs", json={
        "project_id": proj["id"], "title": "Aye bug", "priority": "Low", "environment": "DEV",
    }).json()
    b = admin_client.post("/api/bugs", json={
        "project_id": proj["id"], "title": "Bee bug", "priority": "Low", "environment": "DEV",
    }).json()
    first = admin_client.post(f"/api/bugs/{a['id']}/links", json={
        "target_bug_id": b["id"], "link_type": "blocks",
    }).json()

    from sqlalchemy import select

    from app.database import SessionLocal
    from app.models import BugLink

    db = SessionLocal()
    try:
        existing_link = db.scalar(select(BugLink).where(BugLink.id == first["id"]))
        existing_id = existing_link.id
    finally:
        db.close()

    # Simulate losing the race: created=False triggers the idempotent return.
    def lost_race(_db, _link, _refetch):
        from app.database import SessionLocal as SL
        from app.models import BugLink as BL
        s = SL()
        try:
            row = s.scalar(select(BL).where(BL.id == existing_id))
            s.expunge(row)
        finally:
            s.close()
        return row, False  # created=False

    monkeypatch.setattr(bugs_mod, "_insert_link_or_existing", lost_race)
    # Fresh (source, target, type) so _find_existing is None and our patched helper runs.
    again = admin_client.post(f"/api/bugs/{a['id']}/links", json={
        "target_bug_id": b["id"], "link_type": "duplicate",
    })
    assert again.status_code in (200, 201)


def _orphan_link(admin_client, *, drop="other"):
    """Create A--blocks-->B, delete one bug with FK off so the link survives orphaned; returns (path_bug_id, link_id)."""
    from sqlalchemy import delete, text

    from app.database import SessionLocal
    from app.models import Bug

    proj = admin_client.post("/api/projects", json={"name": f"Orphan{drop}"}).json()
    a = admin_client.post("/api/bugs", json={
        "project_id": proj["id"], "title": "Aye orphan", "priority": "Low",
        "environment": "DEV",
    }).json()
    b = admin_client.post("/api/bugs", json={
        "project_id": proj["id"], "title": "Bee orphan", "priority": "Low",
        "environment": "DEV",
    }).json()
    link = admin_client.post(f"/api/bugs/{a['id']}/links", json={
        "target_bug_id": b["id"], "link_type": "blocks",
    }).json()
    victim = b["id"] if drop == "other" else a["id"]
    db = SessionLocal()
    try:
        db.execute(text("PRAGMA foreign_keys = OFF"))
        db.execute(delete(Bug).where(Bug.id == victim))
        db.commit()
    finally:
        db.close()
    return a["id"], link["id"]


def test_bugs_remove_link_other_endpoint_gone(admin_client):
    # Other endpoint gone (orphaned): the block is skipped and the link deleted directly.
    path_bug_id, link_id = _orphan_link(admin_client, drop="other")
    r = admin_client.delete(f"/api/bugs/{path_bug_id}/links/{link_id}")
    assert r.status_code == 200, r.text


def test_bugs_remove_link_path_bug_gone_404(admin_client):
    # The path bug itself is gone (orphaned link); the route must return 404.
    path_bug_id, link_id = _orphan_link(admin_client, drop="path")
    r = admin_client.delete(f"/api/bugs/{path_bug_id}/links/{link_id}")
    assert r.status_code == 404


def test_bugs_download_range_row_vanished_404(admin_client):
    # TOCTOU: row deleted between metadata read and byte-slice must 404, not empty 206.
    from app.database import SessionLocal, get_db
    from app.main import app

    proj = admin_client.post("/api/projects", json={"name": "DlRace"}).json()
    bug = admin_client.post("/api/bugs", json={
        "project_id": proj["id"], "title": "dl race", "priority": "Low",
        "environment": "DEV",
    }).json()
    up = admin_client.post(
        f"/api/bugs/{bug['id']}/attachments",
        files={"file": ("note.txt", b"hello world data", "text/plain")},
    )
    assert up.status_code == 201, up.text
    att_id = up.json()["id"]

    class _SliceVanishSession:
        def __init__(self, real):
            self._real = real

        def scalar(self, stmt):
            if "substr" in str(stmt).lower():
                return None  # concurrent delete of the blob row
            return self._real.scalar(stmt)

        def __getattr__(self, name):
            return getattr(self._real, name)

    def _override_db():
        real = SessionLocal()
        try:
            yield _SliceVanishSession(real)
        finally:
            real.close()

    app.dependency_overrides[get_db] = _override_db
    try:
        r = admin_client.get(
            f"/api/bugs/{bug['id']}/attachments/{att_id}/download",
            headers={"Range": "bytes=0-3"},
        )
    finally:
        app.dependency_overrides.pop(get_db, None)
    assert r.status_code == 404


def test_bugs_update_assignee_only_skips_changes_notify(admin_client):
    # Assignee-only update: empty changes but newly_assigned set, so only the assign-notify block runs.
    uid = _mk_user_via_api(admin_client, "Assignee Only", "assignonly@test.local")
    proj = admin_client.post("/api/projects", json={"name": "AssignOnly"}).json()
    bug = admin_client.post("/api/bugs", json={
        "project_id": proj["id"], "title": "assignee only", "priority": "Low",
        "environment": "DEV",
    }).json()
    r = admin_client.put(f"/api/bugs/{bug['id']}", json={
        "title": "assignee only", "priority": "Low", "environment": "DEV",
        "assignee_ids": [uid],
    })
    assert r.status_code == 200, r.text
    assert uid in [a["id"] for a in r.json()["assignees"]]
