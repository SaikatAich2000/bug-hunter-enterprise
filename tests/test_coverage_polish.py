"""Targeted tests for defensive branches the main suites don't reach.
RAG upsert with no embedding, digest scheduler with empty cron, failed reset delivery.
"""
from __future__ import annotations

import types


# rag.upsert_bug: embedding returns nothing, so nothing is written
def test_upsert_bug_embed_returns_none_skips_upsert(monkeypatch):
    from app.chatbot import rag

    class _Col:
        def __init__(self):
            self.upserts = []

        def upsert(self, **kw):
            self.upserts.append(kw)

    col = _Col()
    bug = types.SimpleNamespace(
        id=5, item_type="Bug", title="T", status="New",
        priority="Low", environment="DEV", description=None,
    )

    class _DB:
        def get(self, model, pk):
            return bug

    monkeypatch.setattr(rag, "_collection", lambda: col)
    monkeypatch.setattr(rag, "_embed", lambda texts: None)  # simulate embedding failure
    rag.upsert_bug(_DB(), 5)
    assert col.upserts == []  # nothing written without vectors


# scheduler.start: empty cron expression should be a no-op
def test_scheduler_start_empty_cron_is_noop(monkeypatch):
    from app import scheduler

    monkeypatch.setattr(
        scheduler, "get_settings",
        lambda: types.SimpleNamespace(
            EMAIL_DIGEST_CRON="", EMAIL_DIGEST_ENABLED=True, AUDIT_RETENTION_DAYS=0,
        ),
    )
    scheduler._task = None
    scheduler.start()
    assert scheduler._task is None  # left unscheduled when cron is blank


# email_service: a failed password-reset delivery should log an error
def test_password_reset_delivery_failure_logs_error(monkeypatch, caplog):
    from app import email_service as es

    monkeypatch.setattr(es, "deliver", lambda subject, to, body, from_address=None: False)
    monkeypatch.setattr(es, "get_settings",
                        lambda: types.SimpleNamespace(EMAIL_BACKEND="smtp", APP_NAME="Bug Hunter"))
    with caplog.at_level("ERROR"):
        es.notify_password_reset("user@bh.local", "User", "http://bh.local/reset?t=x")
    assert any("not delivered" in r.message.lower() for r in caplog.records)
