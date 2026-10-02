"""Cloud LLM live counters and cooldown (app/chatbot/cloud_llm.py)."""
from __future__ import annotations


def _project(c, name="Proj"):
    r = c.post("/api/projects", json={"name": name})
    assert r.status_code == 201, r.text
    return r.json()["id"]


def _bug(c, pid, title, description=""):
    r = c.post("/api/bugs", json={
        "project_id": pid, "title": title, "description": description,
        "priority": "Medium", "environment": "DEV",
    })
    assert r.status_code == 201, r.text
    return r.json()["id"]


def test_reliability_live_counters(admin_client, monkeypatch):
    """Live counters record which provider served the turn and which route was chosen."""
    from app import models
    from app.chatbot import cloud_llm, executor
    from app.config import get_settings
    from app.database import SessionLocal
    pid = _project(admin_client)
    _bug(admin_client, pid, "Login crash", "boom")
    s = get_settings()
    monkeypatch.setattr(s, "SLEUTH_CLOUD_ENABLED", True)
    monkeypatch.setattr(s, "GROQ_API_KEY", "k")
    monkeypatch.setattr(cloud_llm, "_cooldown_until", 0.0, raising=False)
    monkeypatch.setattr(cloud_llm, "is_available", lambda: True)
    cloud_llm._reset_metrics_for_test()
    monkeypatch.setattr(
        cloud_llm, "_call_groq",
        lambda system, user, **kw: '{"mode":"data","canonical_query":"list all bugs"}',
    )
    db = SessionLocal()
    try:
        actor = db.query(models.User).first()
        executor.execute("what is still outstanding?", db, actor)
    finally:
        db.close()
    snap = cloud_llm.metrics_snapshot()
    assert snap.get("provider:groq", 0) >= 1
    assert snap.get("route:data", 0) >= 1


def test_cooldown_no_ratchet_and_trip_counter(monkeypatch):
    from app.chatbot import cloud_llm
    monkeypatch.setattr(cloud_llm, "_cooldown_until", 0.0, raising=False)
    cloud_llm._reset_metrics_for_test()
    cloud_llm._trip_cooldown()
    first = cloud_llm._cooldown_until
    # A second trip while already in cooldown must not extend the window or recount.
    cloud_llm._trip_cooldown()
    assert cloud_llm._cooldown_until == first
    assert cloud_llm.metrics_snapshot().get("cooldown_trips") == 1
