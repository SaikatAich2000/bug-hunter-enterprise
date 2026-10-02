"""Direct unit tests for chatbot nlu/executor/llm and routes/bugs helpers."""
from __future__ import annotations

from datetime import datetime, timezone
from types import SimpleNamespace

import pytest


def test_named_window_today():
    from app.chatbot.nlu import _named_window
    now = datetime(2026, 6, 3, 14, 30, tzinfo=timezone.utc)
    today_start = now.replace(hour=0, minute=0, second=0, microsecond=0)
    win = _named_window("today", today_start, now)
    assert win is not None
    assert win.label == "today"
    assert win.start == today_start
    assert win.end == now


def test_named_window_yesterday():
    from app.chatbot.nlu import _named_window
    now = datetime(2026, 6, 3, 14, 30, tzinfo=timezone.utc)
    today_start = now.replace(hour=0, minute=0, second=0, microsecond=0)
    win = _named_window("yesterday", today_start, now)
    assert win.label == "yesterday"
    assert win.end == today_start


def test_named_window_this_week():
    from app.chatbot.nlu import _named_window
    now = datetime(2026, 6, 3, 14, 30, tzinfo=timezone.utc)
    today_start = now.replace(hour=0, minute=0, second=0, microsecond=0)
    win = _named_window("this week", today_start, now)
    assert win is not None
    assert win.label == "this week"


def test_named_window_last_week():
    from app.chatbot.nlu import _named_window
    now = datetime(2026, 6, 3, 14, 30, tzinfo=timezone.utc)
    today_start = now.replace(hour=0, minute=0, second=0, microsecond=0)
    win = _named_window("last week", today_start, now)
    assert win is not None
    assert win.label == "last week"


def test_named_window_this_month():
    from app.chatbot.nlu import _named_window
    now = datetime(2026, 6, 15, tzinfo=timezone.utc)
    today_start = now.replace(hour=0, minute=0, second=0, microsecond=0)
    win = _named_window("this month", today_start, now)
    assert win.start.day == 1


def test_named_window_last_month():
    from app.chatbot.nlu import _named_window
    now = datetime(2026, 6, 15, tzinfo=timezone.utc)
    today_start = now.replace(hour=0, minute=0, second=0, microsecond=0)
    win = _named_window("last month", today_start, now)
    assert win is not None
    assert win.label == "last month"


def test_named_window_this_quarter():
    from app.chatbot.nlu import _named_window
    now = datetime(2026, 6, 15, tzinfo=timezone.utc)
    today_start = now.replace(hour=0, minute=0, second=0, microsecond=0)
    win = _named_window("this quarter", today_start, now)
    assert win.start.month in (1, 4, 7, 10)


def test_named_window_last_quarter():
    from app.chatbot.nlu import _named_window
    now = datetime(2026, 6, 15, tzinfo=timezone.utc)
    today_start = now.replace(hour=0, minute=0, second=0, microsecond=0)
    win = _named_window("last quarter", today_start, now)
    assert win is not None
    assert win.label == "last quarter"


def test_named_window_this_year():
    from app.chatbot.nlu import _named_window
    now = datetime(2026, 6, 15, tzinfo=timezone.utc)
    today_start = now.replace(hour=0, minute=0, second=0, microsecond=0)
    win = _named_window("this year", today_start, now)
    assert win.start.month == 1
    assert win.start.day == 1


def test_named_window_last_year():
    from app.chatbot.nlu import _named_window
    now = datetime(2026, 6, 15, tzinfo=timezone.utc)
    today_start = now.replace(hour=0, minute=0, second=0, microsecond=0)
    win = _named_window("last year", today_start, now)
    assert win.start.year == 2025


def test_named_window_unknown_returns_none():
    from app.chatbot.nlu import _named_window
    now = datetime(2026, 6, 15, tzinfo=timezone.utc)
    today_start = now.replace(hour=0, minute=0, second=0, microsecond=0)
    assert _named_window("not a phrase", today_start, now) is None


def test_since_weekday_window():
    from app.chatbot.nlu import _since_weekday_window
    # June 3 2026 is a Wednesday (weekday=2). "since monday" → Monday June 1.
    now = datetime(2026, 6, 3, 14, tzinfo=timezone.utc)
    today_start = now.replace(hour=0, minute=0, second=0, microsecond=0)
    win = _since_weekday_window("monday", today_start, now)
    assert win.start.day == 1
    assert "since monday" in win.label


def test_since_weekday_window_same_day_means_week_ago():
    from app.chatbot.nlu import _since_weekday_window
    # June 3 2026 is Wednesday. "since wednesday" → previous Wednesday (7 days ago).
    now = datetime(2026, 6, 3, 14, tzinfo=timezone.utc)
    today_start = now.replace(hour=0, minute=0, second=0, microsecond=0)
    win = _since_weekday_window("wednesday", today_start, now)
    assert (today_start - win.start).days == 7


def test_relative_window_hours():
    from app.chatbot.nlu import _relative_window
    now = datetime(2026, 6, 3, 14, tzinfo=timezone.utc)
    win = _relative_window(6, "hours", now)
    assert (now - win.start).total_seconds() == 6 * 3600


def test_relative_window_days():
    from app.chatbot.nlu import _relative_window
    now = datetime(2026, 6, 3, tzinfo=timezone.utc)
    win = _relative_window(7, "days", now)
    assert (now - win.start).days == 7


def test_relative_window_weeks():
    from app.chatbot.nlu import _relative_window
    now = datetime(2026, 6, 3, tzinfo=timezone.utc)
    win = _relative_window(2, "weeks", now)
    assert (now - win.start).days == 14


def test_relative_window_months_approx():
    from app.chatbot.nlu import _relative_window
    now = datetime(2026, 6, 3, tzinfo=timezone.utc)
    win = _relative_window(2, "months", now)
    assert (now - win.start).days == 60


def test_relative_window_unknown_unit():
    from app.chatbot.nlu import _relative_window
    now = datetime(2026, 6, 3, tzinfo=timezone.utc)
    assert _relative_window(2, "centuries", now) is None


def test_first_int_match():
    from app.chatbot.nlu import _first_int_match
    assert _first_int_match((None, "7", "9")) == 7
    assert _first_int_match((None, None, None)) is None
    assert _first_int_match(("not-an-int", "5")) == 5


def test_first_str_match():
    from app.chatbot.nlu import _first_str_match
    assert _first_str_match((None, "DAYS", "weeks")) == "days"
    assert _first_str_match((None, None)) is None


def test_typo_fallback_no_typo_run_when_filled():
    from app.chatbot.nlu import _PRIORITY_SYNONYMS, _typo_fallback
    out = ["Critical"]
    _typo_fallback("blocker", _PRIORITY_SYNONYMS, out)
    # already-filled list must stay unchanged
    assert out == ["Critical"]


def test_append_unique():
    from app.chatbot.nlu import _append_unique
    out = []
    _append_unique(out, "A")
    _append_unique(out, "A")
    _append_unique(out, "B")
    assert out == ["A", "B"]


def test_clarify_ambiguous_names_returns_response():
    from app.chatbot.executor import _clarify_ambiguous_names
    from app.chatbot.nlu import ParsedQuery
    pq = ParsedQuery(raw_message="anything")
    pq.ambiguous_names = [("john", ["John Smith", "John Doe"])]
    resp = _clarify_ambiguous_names(pq)
    assert resp is not None
    assert resp.intent == "clarify"


def test_clarify_ambiguous_names_returns_none():
    from app.chatbot.executor import _clarify_ambiguous_names
    from app.chatbot.nlu import ParsedQuery
    pq = ParsedQuery(raw_message="anything")
    assert _clarify_ambiguous_names(pq) is None


def test_clarify_unresolved_user_assignee():
    from app.chatbot.executor import _clarify_unresolved_user
    from app.chatbot.nlu import ParsedQuery
    pq = ParsedQuery(raw_message="bugs assigned to ghost")
    pq.unresolved_assignee_names = ["ghost"]
    resp = _clarify_unresolved_user(pq, None)
    assert resp is not None
    assert "ghost" in resp.blocks[0].payload["text"]


def test_clarify_unresolved_user_reporter():
    from app.chatbot.executor import _clarify_unresolved_user
    from app.chatbot.nlu import ParsedQuery
    pq = ParsedQuery(raw_message="bugs reported by ghost")
    pq.unresolved_reporter_names = ["ghost"]
    resp = _clarify_unresolved_user(pq, None)
    assert resp is not None
    assert resp.intent == "clarify"


def test_clarify_unresolved_user_none():
    from app.chatbot.executor import _clarify_unresolved_user
    from app.chatbot.nlu import ParsedQuery
    pq = ParsedQuery(raw_message="anything")
    assert _clarify_unresolved_user(pq, None) is None


def test_format_bug_row():
    from app.chatbot.executor import _format_bug_row
    bug = SimpleNamespace(
        id=42, title="A bug", project=SimpleNamespace(name="ProjX"),
        status="New", priority="High", environment="DEV",
        reporter=SimpleNamespace(name="Alice"),
        assignees=[SimpleNamespace(name="Bob"), SimpleNamespace(name="Carol")],
        due_date="2026-07-01",
    )
    row = _format_bug_row(bug)
    assert row[0] == "#42"
    assert row[1] == "A bug"
    assert "Bob" in row[7]
    assert "Carol" in row[7]


def test_format_bug_row_handles_none_project():
    from app.chatbot.executor import _format_bug_row
    bug = SimpleNamespace(
        id=1, title=None, project=None,
        status="New", priority="Low", environment="UAT",
        reporter=None, assignees=[], due_date=None,
    )
    row = _format_bug_row(bug)
    assert row[2] == ""
    assert row[6] == ""
    assert row[8] == ""


def test_build_export_suggestion_block():
    from app.chatbot.executor import _build_export_suggestion_block
    from app.chatbot.nlu import ParsedQuery
    pq = ParsedQuery(raw_message="open bugs in DEV")
    block = _build_export_suggestion_block(pq)
    assert block.kind == "suggestions"
    items = block.payload["items"]
    assert items[0]["label"] == "Export to Excel"
    assert "export to excel" in items[0]["send"]


def test_build_count_response_plural():
    from app.chatbot.executor import _build_count_response
    resp = _build_count_response(0, "open")
    assert resp.intent == "count_bugs"
    assert "are" in resp.blocks[0].payload["text"]


def test_build_count_response_singular():
    from app.chatbot.executor import _build_count_response
    resp = _build_count_response(1, "open")
    assert "is" in resp.blocks[0].payload["text"]


def test_build_no_results_no_filters():
    from app.chatbot.executor import _build_no_results_response
    resp = _build_no_results_response("(no filters)")
    assert "no bugs in the system" in resp.blocks[0].payload["text"]


def test_build_no_results_with_filters():
    from app.chatbot.executor import _build_no_results_response
    resp = _build_no_results_response("in PROD")
    assert "No bugs found" in resp.blocks[0].payload["text"]


def test_list_bugs_order_oldest():
    from app.chatbot.executor import _list_bugs_order
    from app.chatbot.nlu import ParsedQuery
    pq = ParsedQuery(raw_message="oldest open bugs")
    pq.sort_oldest = True
    cols = _list_bugs_order(pq)
    assert "ASC" in str(cols[0])


def test_list_bugs_order_default_desc():
    from app.chatbot.executor import _list_bugs_order
    from app.chatbot.nlu import ParsedQuery
    pq = ParsedQuery(raw_message="recent bugs")
    cols = _list_bugs_order(pq)
    assert "DESC" in str(cols[0])


def test_empty_intent_response():
    from app.chatbot.executor import _empty_intent_response
    resp = _empty_intent_response()
    assert resp.intent == "empty"


def test_classifier_action_invalid():
    from app.chatbot.executor import _classifier_action_invalid
    resp = _classifier_action_invalid("action_assign")
    assert resp.intent == "action_invalid"
    assert "assign" in resp.blocks[0].payload["text"]


def test_dedupe_display_names_preserves_order_and_drops_dupes():
    from app.chatbot.executor import _dedupe_display_names
    pool = {"a": "Alice", "alice": "Alice", "b": "Bob"}
    out = _dedupe_display_names(["a", "alice", "b"], pool)
    assert out == ["Alice", "Bob"]


def test_dedupe_display_names_skips_missing_keys():
    from app.chatbot.executor import _dedupe_display_names
    pool = {"a": "Alice"}
    out = _dedupe_display_names(["z", "a"], pool)
    assert out == ["Alice"]


@pytest.fixture
def fake_actor():
    return SimpleNamespace(id=42, name="Tester", role="admin")


def _pq(**kw):
    from app.chatbot.nlu import ParsedQuery
    pq = ParsedQuery(raw_message=kw.pop("raw_message", "test"))
    for k, v in kw.items():
        setattr(pq, k, v)
    return pq


def test_plan_assign_missing_target(fake_actor):
    from app.chatbot.executor import _build_action_plan
    pq = _pq(action_kind="assign", bug_id=1, assignee_ids=[])
    plan, err = _build_action_plan(pq, fake_actor)
    assert plan is None
    assert "name" in err.lower()


def test_plan_assign_success(fake_actor):
    from app.chatbot.executor import _build_action_plan
    pq = _pq(action_kind="assign", bug_id=1, assignee_ids=[7], assignee_names=["Alice"])
    plan, err = _build_action_plan(pq, fake_actor)
    assert err is None
    assert plan.target_user_ids == [7]
    assert "Assign Alice to bug #1" in plan.summary_human


def test_plan_unassign_summary(fake_actor):
    from app.chatbot.executor import _build_action_plan
    pq = _pq(action_kind="unassign", bug_id=2, assignee_ids=[3], assignee_names=["Bob"])
    plan, err = _build_action_plan(pq, fake_actor)
    assert err is None
    assert "from bug #2" in plan.summary_human


def test_plan_missing_bug(fake_actor):
    from app.chatbot.executor import _build_action_plan
    pq = _pq(action_kind="set_status", bug_id=None, action_value="Closed")
    plan, err = _build_action_plan(pq, fake_actor)
    assert plan is None
    assert "Which bug" in err


def test_plan_missing_bug_pronoun_hint(fake_actor):
    from app.chatbot.executor import _build_action_plan
    pq = _pq(action_kind="set_status", bug_id=None, used_pronoun_bug=True)
    plan, err = _build_action_plan(pq, fake_actor)
    assert plan is None
    assert "don't know which bug" in err


def test_plan_set_status_missing_value(fake_actor):
    from app.chatbot.executor import _build_action_plan
    pq = _pq(action_kind="set_status", bug_id=1, action_value=None)
    plan, err = _build_action_plan(pq, fake_actor)
    assert plan is None


def test_plan_set_status_success(fake_actor):
    from app.chatbot.executor import _build_action_plan
    pq = _pq(action_kind="set_status", bug_id=1, action_value="Resolved")
    plan, err = _build_action_plan(pq, fake_actor)
    assert err is None
    assert plan.new_value == "Resolved"


def test_plan_set_priority(fake_actor):
    from app.chatbot.executor import _build_action_plan
    pq = _pq(action_kind="set_priority", bug_id=1, action_value="High")
    plan, err = _build_action_plan(pq, fake_actor)
    assert err is None
    assert "priority" in plan.summary_human.lower()


def test_plan_set_environment_missing(fake_actor):
    from app.chatbot.executor import _build_action_plan
    pq = _pq(action_kind="set_environment", bug_id=1, environments=[])
    plan, err = _build_action_plan(pq, fake_actor)
    assert plan is None


def test_plan_set_environment_success(fake_actor):
    from app.chatbot.executor import _build_action_plan
    pq = _pq(action_kind="set_environment", bug_id=1, environments=["PROD"])
    plan, err = _build_action_plan(pq, fake_actor)
    assert err is None
    assert plan.new_value == "PROD"


def test_plan_set_due_date_missing(fake_actor):
    from app.chatbot.executor import _build_action_plan
    pq = _pq(action_kind="set_due_date", bug_id=1, action_value=None)
    plan, err = _build_action_plan(pq, fake_actor)
    assert plan is None


def test_plan_set_due_date_success(fake_actor):
    from app.chatbot.executor import _build_action_plan
    pq = _pq(action_kind="set_due_date", bug_id=1, action_value="2026-07-01")
    plan, err = _build_action_plan(pq, fake_actor)
    assert err is None
    assert "due date" in plan.summary_human.lower()


def test_plan_add_comment_missing(fake_actor):
    from app.chatbot.executor import _build_action_plan
    pq = _pq(action_kind="add_comment", bug_id=1, action_comment=None)
    plan, err = _build_action_plan(pq, fake_actor)
    assert plan is None


def test_plan_add_comment_success(fake_actor):
    from app.chatbot.executor import _build_action_plan
    pq = _pq(action_kind="add_comment", bug_id=1, action_comment="works for me")
    plan, err = _build_action_plan(pq, fake_actor)
    assert err is None
    assert "works for me" in plan.summary_human


def test_plan_add_comment_truncated_preview(fake_actor):
    from app.chatbot.executor import _build_action_plan
    long_text = "x" * 100
    pq = _pq(action_kind="add_comment", bug_id=1, action_comment=long_text)
    plan, err = _build_action_plan(pq, fake_actor)
    assert err is None
    assert "..." in plan.summary_human


def test_plan_create_bug_missing_title(fake_actor):
    from app.chatbot.executor import _build_action_plan
    pq = _pq(action_kind="create_bug", action_title=None)
    plan, err = _build_action_plan(pq, fake_actor)
    assert plan is None


def test_plan_create_bug_success(fake_actor):
    from app.chatbot.executor import _build_action_plan
    pq = _pq(action_kind="create_bug", action_title="Login broken",
             project_ids=[3], project_names=["Apollo"], priorities=["High"],
             assignee_ids=[5], assignee_names=["Alice"])
    plan, err = _build_action_plan(pq, fake_actor)
    assert err is None
    assert plan.new_title == "Login broken"
    assert plan.new_project_id == 3
    assert plan.target_user_ids == [5]


def test_plan_create_project_missing_title(fake_actor):
    from app.chatbot.executor import _build_action_plan
    pq = _pq(action_kind="create_project", action_title=None)
    plan, err = _build_action_plan(pq, fake_actor)
    assert plan is None


def test_plan_create_project_success(fake_actor):
    from app.chatbot.executor import _build_action_plan
    pq = _pq(action_kind="create_project", action_title="Mercury")
    plan, err = _build_action_plan(pq, fake_actor)
    assert err is None
    assert plan.new_project_name == "Mercury"


def test_plan_unknown_kind(fake_actor):
    from app.chatbot.executor import _build_action_plan
    pq = _pq(action_kind="nope")
    plan, err = _build_action_plan(pq, fake_actor)
    assert plan is None
    assert "don't know how" in err


def test_build_pq_from_llm_clean():
    from app.chatbot.llm import _build_pq_from_llm
    parsed = {
        "filters": {
            "status": ["New", "BogusStatus"],
            "priority": ["High", "BogusPriority"],
            "environment": ["PROD", "BogusEnv"],
        },
        "bug_id": 17,
    }
    pq = _build_pq_from_llm("show me bugs", parsed)
    assert pq.statuses == ["New"]
    assert pq.priorities == ["High"]
    assert pq.environments == ["PROD"]
    assert pq.bug_id == 17


def test_build_pq_from_llm_no_filters():
    from app.chatbot.llm import _build_pq_from_llm
    pq = _build_pq_from_llm("anything", {})
    assert pq.statuses == []
    assert pq.bug_id is None


def test_build_pq_from_llm_negative_bid_ignored():
    from app.chatbot.llm import _build_pq_from_llm
    pq = _build_pq_from_llm("x", {"bug_id": -1})
    assert pq.bug_id is None


def test_normalize_choice_list_empties_stripped():
    from app.routes.bugs import _normalize_choice_list
    out = _normalize_choice_list(["New", None, "", "Closed"],
                                 ["New", "Closed"], "status")
    assert out == ["New", "Closed"]


def test_normalize_choice_list_none_input():
    from app.routes.bugs import _normalize_choice_list
    assert _normalize_choice_list(None, ["a"], "x") == []


def test_normalize_choice_list_rejects_unknown():
    from fastapi import HTTPException

    from app.routes.bugs import _normalize_choice_list
    with pytest.raises(HTTPException) as exc:
        _normalize_choice_list(["bogus"], ["New", "Closed"], "status")
    assert exc.value.status_code == 400
