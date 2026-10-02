"""Offline (message -> expected intent + fields) battery for Sleuth's deterministic parser; a routing regression guard."""
from __future__ import annotations

# (message, expected_intent, {field: expected_value})
CASES = [
    ("how many critical bugs in prod", "list_bugs",
     {"wants_count": True, "priorities": ["Critical"], "environments": ["PROD"]}),
    ("show all p0 bugs", "list_bugs", {"priorities": ["Critical"]}),
    ("unassigned bugs", "list_bugs", {"unassigned": True}),
    ("export bugs to excel", "list_bugs", {"wants_export": True}),
    ("bug #7", "bug_detail", {"bug_id": 7}),
    ("list all managers", "list_users", {"role_filter": "manager"}),
    ("show users", "list_users", {}),
    ("list projects", "list_projects", {}),
    ("summary", "stats", {}),
    ("recent activity", "recent_activity", {}),
    ("hi", "greeting", {}),
    ("help", "help", {}),
    ("thanks!", "thanks", {}),
]


def test_sleuth_understanding_battery(client):
    from app import models
    from app.chatbot.executor import build_context
    from app.chatbot.nlu import parse
    from app.database import SessionLocal
    db = SessionLocal()
    try:
        ctx = build_context(db, db.query(models.User).first())
        failures = []
        for message, intent, fields in CASES:
            pq = parse(message, ctx)
            if pq.intent != intent:
                failures.append(f"{message!r}: intent {pq.intent!r} != {intent!r}")
                continue
            for field, expected in fields.items():
                actual = getattr(pq, field)
                if actual != expected:
                    failures.append(f"{message!r}: {field}={actual!r} != {expected!r}")
        assert not failures, "Sleuth eval failures:\n" + "\n".join(failures)
    finally:
        db.close()
