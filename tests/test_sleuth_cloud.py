"""Sleuth cloud LLM layer (Groq / OpenRouter fallback), all providers monkeypatched.

Safety contract: off by default; data questions run the deterministic SQL path (model only picks
the filter); the layer never writes; Groq failure falls back to OpenRouter; outbound text is redacted.
"""
from __future__ import annotations

import os
import sys
import tempfile

from tests.conftest import default_org_id

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

_tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
_tmp.close()
os.environ["DATABASE_URL"] = f"sqlite:///{_tmp.name}"
os.environ["SESSION_SECRET"] = "test-cloud"

import pytest  # noqa: E402

from app import models  # noqa: E402
from app.auth import hash_password  # noqa: E402
from app.chatbot import cloud_llm, executor  # noqa: E402
from app.chatbot.memory import store as memstore  # noqa: E402
from app.config import get_settings  # noqa: E402
from app.database import Base, SessionLocal, engine  # noqa: E402


@pytest.fixture(autouse=True)
def _rebind_app_modules():
    """Re-bind this file's app.* references to the live generation (conftest purges app.* between tests)."""
    import importlib
    g = globals()
    db_mod = importlib.import_module("app.database")
    g["Base"], g["engine"], g["SessionLocal"] = (
        db_mod.Base, db_mod.engine, db_mod.SessionLocal,
    )
    g["models"] = importlib.import_module("app.models")
    g["executor"] = importlib.import_module("app.chatbot.executor")
    g["cloud_llm"] = importlib.import_module("app.chatbot.cloud_llm")
    g["memstore"] = importlib.import_module("app.chatbot.memory").store
    g["hash_password"] = importlib.import_module("app.auth").hash_password
    g["get_settings"] = importlib.import_module("app.config").get_settings


@pytest.fixture
def db():
    Base.metadata.drop_all(bind=engine)
    Base.metadata.create_all(bind=engine)
    memstore._clear_all_for_test()
    s = SessionLocal()
    try:
        admin = models.User(org_id=default_org_id(), name="Admin", email="admin@example.com", role="admin",
                            password_hash=hash_password("AdminPass123!"), is_active=True)
        john = models.User(org_id=default_org_id(), name="John", email="john@example.com", role="manager",
                           password_hash=hash_password("JohnPass123!"), is_active=True)
        s.add_all([admin, john])
        s.flush()
        proj = models.Project(org_id=default_org_id(), name="Mobile", description="")
        s.add(proj)
        s.flush()
        # 2 open, 1 closed.
        s.add_all([
            models.Bug(project_id=proj.id, title="Crash on login", status="New",
                       priority="High", environment="PROD", reporter_id=john.id),
            models.Bug(project_id=proj.id, title="Slow dashboard", status="In Progress",
                       priority="Medium", environment="UAT", reporter_id=john.id),
            models.Bug(project_id=proj.id, title="Typo in footer", status="Closed",
                       priority="Low", environment="DEV", reporter_id=admin.id),
        ])
        s.commit()
        s.actor = s.get(models.User, admin.id)
        yield s
    finally:
        s.close()


@pytest.fixture
def enabled(monkeypatch):
    """Force the cloud layer on by patching is_available() (the single gate), robust to get_settings.cache_clear()."""
    monkeypatch.setattr(cloud_llm, "is_available", lambda: True)


def _groq_returns(monkeypatch, obj_json: str):
    monkeypatch.setattr(cloud_llm, "_call_groq", lambda system, user, **kw: obj_json)


# ---------------------------------------------------------------------------

def test_disabled_by_default(db, monkeypatch):
    st = get_settings()
    monkeypatch.setattr(st, "SLEUTH_CLOUD_ENABLED", False)
    assert cloud_llm.is_available() is False
    assert cloud_llm.try_understand("anything at all", db, db.actor) is None


def test_data_question_uses_real_sql_not_a_guess(db, enabled, monkeypatch):
    # The model picks a canonical filter; the actual data comes from SQL.
    _groq_returns(monkeypatch, '{"mode":"data","canonical_query":"open bugs"}')
    cloud_resp = cloud_llm.try_understand("which things are still outstanding?", db, db.actor)
    direct = executor.execute("open bugs", db, db.actor)
    assert cloud_resp is not None
    assert cloud_resp.intent.startswith("cloud_data:")
    # Must produce identical rows to the deterministic rule query.
    cloud_tables = [b for b in cloud_resp.blocks if b.kind == "table"]
    direct_tables = [b for b in direct.blocks if b.kind == "table"]
    assert cloud_tables
    assert direct_tables
    assert cloud_tables[0].payload["rows"] == direct_tables[0].payload["rows"]


def test_cloud_never_writes(db, enabled, monkeypatch):
    bug = db.query(models.Bug).filter_by(title="Crash on login").first()
    assert bug.status == "New"
    # A write canonical query from the model must be silently dropped.
    _groq_returns(monkeypatch, f'{{"mode":"data","canonical_query":"close bug {bug.id}"}}')
    resp = cloud_llm.try_understand("can you close the login crash please", db, db.actor)
    assert resp is None                      # write dropped, fell through
    db.expire_all()
    assert db.get(models.Bug, bug.id).status == "New"


def test_falls_back_to_openrouter(db, enabled, monkeypatch):
    monkeypatch.setattr(cloud_llm, "_call_groq", lambda system, user, **kw: None)
    monkeypatch.setattr(cloud_llm, "_call_openrouter",
                        lambda system, user, **kw: '{"mode":"answer","text":"Bug #1 is the login crash."}')
    resp = cloud_llm.try_understand("explain the login crash", db, db.actor)
    assert resp is not None
    assert resp.intent == "cloud_answer"
    assert "login crash" in resp.blocks[0].payload["text"]


def test_secrets_redacted_before_send(db, enabled, monkeypatch):
    seen = {}

    def capture(system, user, **kw):
        seen["user"] = user
        return '{"mode":"unknown"}'

    monkeypatch.setattr(cloud_llm, "_call_groq", capture)
    cloud_llm.try_understand("my key is AIzaSyABCDEFGHIJKLMNOaBcDeFgHiJkLmNoPqRs", db, db.actor)
    assert "AIzaSy" not in seen["user"]
    assert "[REDACTED]" in seen["user"]


def test_report_person_date_matches_rule_path(db, enabled, monkeypatch):
    # Model picks the report filter; counts come from the same SQL handler the rules use.
    _groq_returns(
        monkeypatch,
        '{"mode":"data","canonical_query":"report of who solved how many bugs last week"}',
    )
    cloud_resp = cloud_llm.try_understand("how many did John close last week?", db, db.actor)
    direct = executor.execute("report of who solved how many bugs last week", db, db.actor)
    assert cloud_resp is not None
    assert cloud_resp.summary == direct.summary   # same deterministic output


def test_answer_mode_returns_conversational_text(db, enabled, monkeypatch):
    _groq_returns(
        monkeypatch,
        '{"mode":"answer","text":"I can\'t add bugs myself, but you can type '
        '\'create bug Login crash\' and confirm it."}',
    )
    resp = cloud_llm.try_understand("can you add bugs?", db, db.actor)
    assert resp is not None
    assert resp.intent == "cloud_answer"
    assert "create bug" in resp.blocks[0].payload["text"].lower()


def test_cloud_preempts_weak_classifier_in_execute(db, enabled, monkeypatch):
    # A message the rules can't parse should reach the cloud layer when enabled.
    _groq_returns(monkeypatch,
                    '{"mode":"answer","text":"Happy to help with your bugs!"}')
    resp = executor.execute("be smart, not dumb", db, db.actor)
    assert resp.intent == "cloud_answer"
    assert "help" in resp.blocks[0].payload["text"].lower()


def test_ai_first_beats_greedy_rule_match(db, enabled, monkeypatch):
    # "admin" would greedy-match list_users in the keyword rules; cloud answers first.
    _groq_returns(
        monkeypatch,
        '{"mode":"answer","text":"No - only an admin can revoke sessions, '
        'from the Sessions panel. Ask an admin to help."}',
    )
    resp = executor.execute(
        "assume I am not the admin, can you revoke someone's session?", db, db.actor
    )
    assert resp.intent == "cloud_answer"
    assert "admin" in resp.blocks[0].payload["text"].lower()


def test_trivial_greetings_skip_the_cloud(db, enabled, monkeypatch):
    # One-word greetings get their canned replies even in AI-first mode.
    def boom(system, user, **kw):  # pragma: no cover - must never run
        raise AssertionError("cloud should not be called for 'hi'")

    monkeypatch.setattr(cloud_llm, "_call_groq", boom)
    monkeypatch.setattr(cloud_llm, "_call_openrouter", boom)
    resp = executor.execute("hi", db, db.actor)
    assert resp.intent == "greeting"


def test_multiword_greetings_skip_the_cloud(db, monkeypatch):
    # Regression ("hi AI" -> "no bugs"): the cloud hop is gated on parsed intent, so the whole
    # greeting/thanks family stays canned. Verified by counting is_available(), the single cloud gate.
    seen = {"n": 0}

    def counting_is_available():
        seen["n"] += 1
        return True

    monkeypatch.setattr(cloud_llm, "is_available", counting_is_available)
    monkeypatch.setattr(cloud_llm, "_call_groq", lambda *a, **k: None)
    monkeypatch.setattr(cloud_llm, "_call_openrouter", lambda *a, **k: None)
    monkeypatch.setattr(cloud_llm, "_cooldown_until", 0.0, raising=False)

    for msg, intent in [("hi AI", "greeting"), ("hello there", "greeting"),
                        ("good morning team", "greeting"), ("thanks a lot", "thanks")]:
        assert executor.execute(msg, db, db.actor).intent == intent, msg
    assert seen["n"] == 0   # cloud layer never consulted for greetings

    # A genuine free-form question should consult the cloud layer.
    executor.execute("why is the login page slow lately", db, db.actor)
    assert seen["n"] >= 1


def test_history_passed_to_model(db, enabled, monkeypatch):
    # Prior turns must be included in the prompt so follow-ups have context.
    conv = models.ChatConversation(user_id=db.actor.id)
    db.add(conv)
    db.flush()
    db.add(models.ChatMessage(conversation_id=conv.id, role="user",
                              content="show me critical bugs"))
    db.commit()
    seen = {}

    def capture(system, user, **kw):
        seen["user"] = user
        return '{"mode":"answer","text":"ok"}'

    monkeypatch.setattr(cloud_llm, "_call_groq", capture)
    cloud_llm.try_understand("and the high priority ones?", db, db.actor)
    assert "show me critical bugs" in seen["user"]   # prior turn made it into the prompt
