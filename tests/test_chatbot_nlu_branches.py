"""Branch-coverage tests for app/chatbot/nlu.py.

The NLU is pure rule-based regex parsing — no DB, no FastAPI client
needed. We import the private helpers directly to exercise each branch
of the time-window resolver, the synonym extractors, the name resolver,
the action detectors, and the final intent classifier.

All time-window tests pin `now` explicitly so the assertions stay
deterministic regardless of when the suite runs.
"""
from __future__ import annotations

import sys
from datetime import datetime, timezone
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.chatbot.nlu import (  # noqa: E402
    Context,
    OPEN_STATUSES,
    ParsedQuery,
    TimeWindow,
    _action_add_comment,
    _action_assign,
    _action_create_bug,
    _action_create_project,
    _action_set_due_date,
    _action_set_priority,
    _action_set_status,
    _candidate_name_phrases,
    _classify_short_intent,
    _detect_action,
    _extract_bug_id,
    _extract_environments,
    _extract_priorities,
    _extract_statuses,
    _extract_text_search,
    _has_pronoun_bug_ref,
    _named_window,
    _normalize,
    _parse_time_window,
    _relative_window,
    _resolve_name,
    _resolve_project,
    _since_weekday_window,
    _strip_create_bug_tail,
    _strip_punct,
    _tokenize,
    _typo_match,
    describe_filters,
    parse,
)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------
@pytest.fixture()
def ctx():
    """A small multi-user, multi-project context for name resolution."""
    return Context(
        users=[
            (1, "john smith", "john", "John Smith"),
            (2, "alice wonder", "alice", "Alice Wonder"),
            (3, "bob builder", "bob.builder", "Bob Builder"),
            (4, "carol singer", "carol", "Carol Singer"),
            # Two Janes to test ambiguity (first-name collision).
            (5, "jane doe", "jane.d", "Jane Doe"),
            (6, "jane roe", "jane.r", "Jane Roe"),
        ],
        projects=[
            (10, "mobile", "Mobile"),
            (11, "api", "API"),
            (12, "billing platform", "Billing Platform"),
        ],
    )


@pytest.fixture()
def now_wed():
    """A fixed Wednesday for stable weekday math (2026-06-03 is a Wed)."""
    return datetime(2026, 6, 3, 15, 0, 0, tzinfo=timezone.utc)


# ---------------------------------------------------------------------------
# Low-level utilities
# ---------------------------------------------------------------------------
def test_normalize_collapses_whitespace_and_lowercases():
    assert _normalize("  Hello   WORLD  ") == "hello world"


def test_normalize_handles_empty_and_none():
    assert _normalize("") == ""
    assert _normalize(None) == ""  # type: ignore[arg-type]


def test_strip_punct_keeps_internal_word_chars():
    assert _strip_punct("John.") == "John"
    assert _strip_punct("O'Brien-ish") == "O'Brien-ish"


def test_tokenize_drops_punctuation_and_digits_first():
    toks = _tokenize("Show me 42 BUGS, please!")
    assert "show" in toks
    assert "bugs" in toks
    # leading-digit tokens are dropped by the regex (must start with a letter)
    assert "42" not in toks


# ---------------------------------------------------------------------------
# Named time windows
# ---------------------------------------------------------------------------
def test_named_window_today_and_yesterday(now_wed):
    today_start = now_wed.replace(hour=0, minute=0, second=0, microsecond=0)
    tw = _named_window("today", today_start, now_wed)
    assert tw is not None and tw.label == "today"
    assert tw.start == today_start
    yw = _named_window("yesterday", today_start, now_wed)
    assert yw is not None and yw.label == "yesterday"
    assert yw.end == today_start


def test_named_window_this_and_last_week(now_wed):
    today_start = now_wed.replace(hour=0, minute=0, second=0, microsecond=0)
    tw = _named_window("this week", today_start, now_wed)
    lw = _named_window("last week", today_start, now_wed)
    assert tw is not None and tw.label == "this week"
    assert lw is not None and lw.label == "last week"
    # last week ends where this week starts
    assert lw.end == tw.start


def test_named_window_this_and_last_month(now_wed):
    today_start = now_wed.replace(hour=0, minute=0, second=0, microsecond=0)
    tm = _named_window("this month", today_start, now_wed)
    lm = _named_window("last month", today_start, now_wed)
    assert tm is not None and tm.start.day == 1
    assert lm is not None and lm.start.day == 1
    # last month ends at this month's start
    assert lm.end == tm.start


def test_named_window_this_and_last_quarter(now_wed):
    today_start = now_wed.replace(hour=0, minute=0, second=0, microsecond=0)
    tq = _named_window("this quarter", today_start, now_wed)
    lq = _named_window("last quarter", today_start, now_wed)
    # June -> Q2 starts in April
    assert tq is not None and tq.start.month in (1, 4, 7, 10)
    assert tq.start.day == 1
    assert lq is not None and lq.end == tq.start


def test_named_window_this_and_last_year(now_wed):
    today_start = now_wed.replace(hour=0, minute=0, second=0, microsecond=0)
    ty = _named_window("this year", today_start, now_wed)
    ly = _named_window("last year", today_start, now_wed)
    assert ty is not None and ty.start.month == 1 and ty.start.day == 1
    assert ly is not None and ly.start.year == ty.start.year - 1
    assert ly.end == ty.start


def test_named_window_unknown_phrase_returns_none(now_wed):
    today_start = now_wed.replace(hour=0, minute=0, second=0, microsecond=0)
    assert _named_window("never", today_start, now_wed) is None


# ---------------------------------------------------------------------------
# Since-weekday window
# ---------------------------------------------------------------------------
def test_since_weekday_window_earlier_in_week(now_wed):
    # Wednesday saying "since monday" -> Monday of THIS week (two days ago).
    today_start = now_wed.replace(hour=0, minute=0, second=0, microsecond=0)
    w = _since_weekday_window("monday", today_start, now_wed)
    assert w.label == "since monday"
    assert (today_start - w.start).days == 2


def test_since_weekday_window_same_weekday_rolls_back_a_week(now_wed):
    # Wednesday saying "since wednesday" -> 7 days ago, not 0.
    today_start = now_wed.replace(hour=0, minute=0, second=0, microsecond=0)
    w = _since_weekday_window("wednesday", today_start, now_wed)
    assert (today_start - w.start).days == 7


def test_since_weekday_window_future_weekday_falls_back_a_week(now_wed):
    # Wednesday saying "since friday" -> last Friday (5 days back).
    today_start = now_wed.replace(hour=0, minute=0, second=0, microsecond=0)
    w = _since_weekday_window("friday", today_start, now_wed)
    assert (today_start - w.start).days == 5


# ---------------------------------------------------------------------------
# Relative windows
# ---------------------------------------------------------------------------
def test_relative_window_hours(now_wed):
    w = _relative_window(3, "hours", now_wed)
    assert w is not None
    assert (now_wed - w.start).total_seconds() == 3 * 3600


def test_relative_window_days(now_wed):
    w = _relative_window(5, "days", now_wed)
    assert w is not None and (now_wed - w.start).days == 5


def test_relative_window_weeks(now_wed):
    w = _relative_window(2, "weeks", now_wed)
    assert w is not None and (now_wed - w.start).days == 14


def test_relative_window_months_uses_30_days(now_wed):
    w = _relative_window(1, "months", now_wed)
    assert w is not None and (now_wed - w.start).days == 30


def test_relative_window_unknown_unit_returns_none(now_wed):
    assert _relative_window(3, "fortnight", now_wed) is None


# ---------------------------------------------------------------------------
# _parse_time_window — end-to-end branches
# ---------------------------------------------------------------------------
def test_parse_time_window_today(now_wed):
    w = _parse_time_window("bugs created today", now=now_wed)
    assert w is not None and w.label == "today"


def test_parse_time_window_yesterday(now_wed):
    w = _parse_time_window("yesterday's bugs", now=now_wed)
    assert w is not None and w.label == "yesterday"


def test_parse_time_window_since_monday(now_wed):
    w = _parse_time_window("since monday", now=now_wed)
    assert w is not None and "monday" in w.label


def test_parse_time_window_past_n_days(now_wed):
    w = _parse_time_window("past 3 days", now=now_wed)
    assert w is not None and (now_wed - w.start).days == 3


def test_parse_time_window_last_n_weeks(now_wed):
    w = _parse_time_window("last 2 weeks", now=now_wed)
    assert w is not None and (now_wed - w.start).days == 14


def test_parse_time_window_in_the_last_n_hours(now_wed):
    w = _parse_time_window("anything in the last 6 hours", now=now_wed)
    assert w is not None and (now_wed - w.start).total_seconds() == 6 * 3600


def test_parse_time_window_no_match_returns_none(now_wed):
    assert _parse_time_window("show me everything", now=now_wed) is None


def test_parse_time_window_uses_default_now_when_none():
    # Just verifies the default-now branch doesn't raise.
    w = _parse_time_window("today")
    assert w is not None


# ---------------------------------------------------------------------------
# Status / priority / environment extraction
# ---------------------------------------------------------------------------
def test_extract_statuses_open_expands_to_open_set():
    out = _extract_statuses("show open bugs")
    assert set(out) == set(OPEN_STATUSES)


def test_extract_statuses_multi_word_in_progress():
    out = _extract_statuses("bugs in progress")
    assert out == ["In Progress"]


def test_extract_statuses_multi_word_not_a_bug():
    out = _extract_statuses("anything marked not a bug")
    assert out == ["Not a Bug"]


def test_extract_statuses_done_expands_to_two():
    out = _extract_statuses("done items")
    assert "Closed" in out and "Resolved" in out


def test_extract_statuses_no_match_returns_empty():
    assert _extract_statuses("hello world") == []


def test_extract_priorities_exact_and_alias():
    assert "Critical" in _extract_priorities("urgent bugs")
    assert "Critical" in _extract_priorities("p0 issues")
    assert "Low" in _extract_priorities("p3 stuff")


def test_extract_priorities_typo_fallback():
    # exact match for "critical" misses, fuzzy hits via _typo_fallback.
    out = _extract_priorities("show me ctitical bugs")
    assert out == ["Critical"]


def test_extract_environments_synonyms():
    assert _extract_environments("on production") == ["PROD"]
    assert _extract_environments("staging issues") == ["UAT"]
    assert _extract_environments("dev env") == ["DEV"]


def test_extract_environments_typo_fallback():
    out = _extract_environments("look at produciton")
    assert out == ["PROD"]


def test_extract_environments_none():
    assert _extract_environments("foo bar baz") == []


# ---------------------------------------------------------------------------
# Bug-id & text-search extraction
# ---------------------------------------------------------------------------
def test_extract_bug_id_hash_prefix():
    assert _extract_bug_id("see #42 for context") == 42


def test_extract_bug_id_bug_keyword():
    assert _extract_bug_id("bug 17 is broken") == 17


def test_extract_bug_id_bare_digits():
    assert _extract_bug_id("99") == 99
    assert _extract_bug_id("#5") == 5


def test_extract_bug_id_no_id():
    assert _extract_bug_id("nothing here") is None


def test_extract_text_search_quoted_returns_inner():
    assert _extract_text_search('bugs about "login crash"') == "login crash"


def test_extract_text_search_no_quote_returns_none():
    assert _extract_text_search("nothing quoted here") is None


# ---------------------------------------------------------------------------
# Typo match helper
# ---------------------------------------------------------------------------
def test_typo_match_short_token_skipped():
    # Tokens shorter than min_len short-circuit out.
    assert _typo_match("p0", {"production": "PROD"}) is None


def test_typo_match_close_token_returns_key():
    hit = _typo_match("produciton", {"production": "PROD", "dev": "DEV"})
    assert hit == "production"


def test_typo_match_far_token_returns_none():
    assert _typo_match("banana", {"production": "PROD"}) is None


# ---------------------------------------------------------------------------
# Candidate name phrases
# ---------------------------------------------------------------------------
def test_candidate_phrases_assigned_to():
    out = _candidate_name_phrases("bugs assigned to John Smith")
    assert ("assignee", "John Smith") in out


def test_candidate_phrases_reported_by():
    out = _candidate_name_phrases("issues reported by Alice")
    assert ("reporter", "Alice") in out


def test_candidate_phrases_filed_by_and_raised_by():
    out_f = _candidate_name_phrases("bugs filed by Bob")
    out_r = _candidate_name_phrases("tickets raised by Carol")
    assert any(role == "reporter" for role, _ in out_f)
    assert any(role == "reporter" for role, _ in out_r)


def test_candidate_phrases_owned_by():
    out = _candidate_name_phrases("bugs owned by Alice")
    assert ("assignee", "Alice") in out


def test_candidate_phrases_assign_verb_with_pronoun():
    out = _candidate_name_phrases("assign it to Alice")
    assert ("assignee", "Alice") in out


def test_candidate_phrases_unassign_verb():
    out = _candidate_name_phrases("unassign Alice from bug 5")
    assert ("assignee", "Alice") in out


# ---------------------------------------------------------------------------
# Name and project resolution
# ---------------------------------------------------------------------------
def test_resolve_name_exact_full_name(ctx):
    out = _resolve_name("John Smith", ctx)
    assert out == [(1, "John Smith")]


def test_resolve_name_email_local_part(ctx):
    # _strip_punct removes the dot, so an email-local "alice" matches the
    # alice@... user via the email-localpart branch.
    out = _resolve_name("alice", ctx)
    assert (2, "Alice Wonder") in out


def test_resolve_name_prefix(ctx):
    out = _resolve_name("Bob", ctx)
    # Bob is a first-name token match here; should pick exactly one.
    assert (3, "Bob Builder") in out


def test_resolve_name_last_name(ctx):
    out = _resolve_name("Wonder", ctx)
    assert (2, "Alice Wonder") in out


def test_resolve_name_ambiguous_first_name(ctx):
    out = _resolve_name("Jane", ctx)
    # both Janes match
    ids = sorted(uid for uid, _ in out)
    assert ids == [5, 6]


def test_resolve_name_empty_phrase_returns_empty(ctx):
    assert _resolve_name("", ctx) == []


def test_resolve_name_strips_title(ctx):
    out = _resolve_name("Mr. John Smith", ctx)
    assert out == [(1, "John Smith")]


def test_resolve_name_only_title_returns_empty(ctx):
    assert _resolve_name("Mr.", ctx) == []


def test_resolve_name_no_match(ctx):
    assert _resolve_name("Zelig", ctx) == []


def test_resolve_project_exact(ctx):
    assert _resolve_project("Mobile", ctx) == [(10, "Mobile")]


def test_resolve_project_prefix(ctx):
    out = _resolve_project("Billing", ctx)
    assert (12, "Billing Platform") in out


def test_resolve_project_empty_returns_empty(ctx):
    assert _resolve_project("", ctx) == []


# ---------------------------------------------------------------------------
# Action detection helpers
# ---------------------------------------------------------------------------
def test_action_add_comment_with_body():
    pq = ParsedQuery()
    kind = _action_add_comment("comment on #5: this is working now", pq)
    assert kind == "add_comment"
    assert pq.action_comment == "this is working now"


def test_action_add_comment_no_verb_returns_none():
    assert _action_add_comment("show bugs", ParsedQuery()) is None


def test_action_create_project_with_quoted_name():
    pq = ParsedQuery()
    kind = _action_create_project('create a project called "Mobile App"', pq)
    assert kind == "create_project"
    assert pq.action_title and "Mobile App" in pq.action_title


def test_action_create_project_no_verb_returns_none():
    assert _action_create_project("show all bugs", ParsedQuery()) is None


def test_action_create_bug_with_quoted_title():
    pq = ParsedQuery()
    kind = _action_create_bug('create a bug titled "login fails"', pq)
    assert kind == "create_bug"
    assert pq.action_title == "login fails"


def test_action_create_bug_bare_title_strips_tail():
    pq = ParsedQuery()
    kind = _action_create_bug(
        "file a bug login broken in project Mobile with priority high", pq,
    )
    assert kind == "create_bug"
    # bare-title capture should NOT include the "in project ..." tail.
    assert pq.action_title is not None
    assert "project" not in pq.action_title.lower()
    assert "priority" not in pq.action_title.lower()


def test_action_create_bug_no_verb_returns_none():
    assert _action_create_bug("show me bugs", ParsedQuery()) is None


def test_action_set_status_via_verb():
    pq = ParsedQuery()
    kind = _action_set_status("close bug 5", pq)
    assert kind == "set_status"
    assert pq.action_value == "Closed"


def test_action_set_status_via_phrase_consumes_status_filter():
    # "change status" hits the phrase branch (not the verb branch), so the
    # already-extracted status filter is consumed as the write target.
    # We pre-populate with a non-verb status to avoid the _STATUS_VERB_RE
    # path: "New" isn't in the verb map.
    pq = ParsedQuery(statuses=["New"])
    kind = _action_set_status("change status to new on bug 5", pq)
    assert kind == "set_status"
    assert pq.action_value == "New"
    assert pq.statuses == []  # consumed


def test_action_set_status_no_verb_returns_none():
    assert _action_set_status("hello world", ParsedQuery()) is None


def test_action_set_priority_consumes_priority():
    pq = ParsedQuery(priorities=["Critical"])
    kind = _action_set_priority("set priority to critical on #5", pq)
    assert kind == "set_priority"
    assert pq.action_value == "Critical"
    assert pq.priorities == []


def test_action_set_priority_no_priority_filter():
    pq = ParsedQuery()
    assert _action_set_priority("set priority to high", pq) is None


def test_action_set_due_date_with_iso():
    pq = ParsedQuery()
    kind = _action_set_due_date("set due date 2026-12-31 on #5", pq)
    assert kind == "set_due_date"
    assert pq.action_value == "2026-12-31"


def test_action_set_due_date_no_match():
    assert _action_set_due_date("hello", ParsedQuery()) is None


def test_action_assign_returns_assign():
    pq = ParsedQuery(assignee_ids=[1])
    assert _action_assign("assign bug 5 to john", pq) == "assign"


def test_action_assign_list_verb_is_not_action():
    pq = ParsedQuery(assignee_ids=[1])
    # "show bugs assigned to john" should NOT be a write.
    assert _action_assign("show bugs assigned to john", pq) is None


def test_action_assign_unassign():
    pq = ParsedQuery(assignee_ids=[1])
    assert _action_assign("unassign john from bug 5", pq) == "unassign"


def test_action_assign_no_assignee_returns_none():
    assert _action_assign("assign bug 5 to nobody-known", ParsedQuery()) is None


def test_detect_action_order_create_project_before_bug():
    # The text contains both "create" and "project" verbs — must resolve
    # to create_project not create_bug.
    pq = ParsedQuery()
    assert _detect_action("create a project called Mobile", pq) == "create_project"


def test_detect_action_none_for_plain_query():
    assert _detect_action("show all bugs", ParsedQuery()) is None


# ---------------------------------------------------------------------------
# Strip-create-bug-tail
# ---------------------------------------------------------------------------
def test_strip_create_bug_tail_in_project():
    assert _strip_create_bug_tail("login broken in project Mobile") == "login broken"


def test_strip_create_bug_tail_priority():
    assert _strip_create_bug_tail("login broken with priority high") == "login broken"


def test_strip_create_bug_tail_assigned_to():
    assert _strip_create_bug_tail("login broken assigned to alice") == "login broken"


def test_strip_create_bug_tail_no_marker_returns_input():
    assert _strip_create_bug_tail("login broken") == "login broken"


def test_strip_create_bug_tail_empty():
    assert _strip_create_bug_tail("") == ""


# ---------------------------------------------------------------------------
# Pronoun helper
# ---------------------------------------------------------------------------
def test_has_pronoun_bug_ref_yes():
    assert _has_pronoun_bug_ref("close it") is True
    assert _has_pronoun_bug_ref("that bug is broken") is True


def test_has_pronoun_bug_ref_no():
    assert _has_pronoun_bug_ref("close bug 5") is False


# ---------------------------------------------------------------------------
# Short-intent classification
# ---------------------------------------------------------------------------
def test_classify_short_intent_greeting():
    pq = ParsedQuery()
    assert _classify_short_intent("hi there", pq) is True
    assert pq.intent == "greeting"


def test_classify_short_intent_help():
    pq = ParsedQuery()
    assert _classify_short_intent("help", pq) is True
    assert pq.intent == "help"


def test_classify_short_intent_thanks():
    pq = ParsedQuery()
    assert _classify_short_intent("thanks", pq) is True
    assert pq.intent == "thanks"


def test_classify_short_intent_yes_confirm():
    pq = ParsedQuery()
    assert _classify_short_intent("yes", pq) is True
    assert pq.confirmation == "yes"


def test_classify_short_intent_no_confirm():
    pq = ParsedQuery()
    assert _classify_short_intent("no", pq) is True
    assert pq.confirmation == "no"


def test_classify_short_intent_returns_false_for_long_query():
    pq = ParsedQuery()
    # Greeting present but the message is too long.
    msg = "hello can you please show me all open bugs in production"
    assert _classify_short_intent(msg, pq) is False


# ---------------------------------------------------------------------------
# Full parse() — intent & wiring
# ---------------------------------------------------------------------------
def test_parse_empty_message(ctx):
    pq = parse("", ctx)
    assert pq.intent == "empty"


def test_parse_greeting(ctx):
    pq = parse("hi", ctx)
    assert pq.intent == "greeting"


def test_parse_list_bugs_with_filters(ctx, now_wed):
    pq = parse(
        "show open critical bugs in PROD assigned to John Smith in project Mobile",
        ctx, now=now_wed,
    )
    assert pq.intent == "list_bugs"
    assert set(pq.statuses) == set(OPEN_STATUSES)
    assert pq.priorities == ["Critical"]
    assert pq.environments == ["PROD"]
    assert pq.assignee_ids == [1]
    assert pq.project_ids == [10]


def test_parse_count_intent_sets_wants_count(ctx):
    pq = parse("how many open bugs are there", ctx)
    assert pq.wants_count is True
    assert pq.intent == "list_bugs"


def test_parse_export_intent_sets_wants_export(ctx):
    pq = parse("export all bugs to excel", ctx)
    assert pq.wants_export is True


def test_parse_bug_detail_short_message(ctx):
    pq = parse("bug 42", ctx)
    assert pq.intent == "bug_detail"
    assert pq.bug_id == 42


def test_parse_action_close_bug(ctx):
    pq = parse("close bug 5", ctx)
    assert pq.action_kind == "set_status"
    assert pq.action_value == "Closed"
    assert pq.intent == "action_set_status"


def test_parse_action_assign(ctx):
    pq = parse("assign bug 5 to John Smith", ctx)
    assert pq.action_kind == "assign"
    assert 1 in pq.assignee_ids


def test_parse_action_create_bug_quoted(ctx):
    pq = parse('create a bug titled "login fails"', ctx)
    assert pq.action_kind == "create_bug"
    assert pq.action_title == "login fails"


def test_parse_action_create_project(ctx):
    pq = parse('create a project called "Marketing Site"', ctx)
    assert pq.action_kind == "create_project"
    assert pq.action_title and "Marketing" in pq.action_title


def test_parse_action_add_comment(ctx):
    pq = parse("comment on #5: looks good", ctx)
    assert pq.action_kind == "add_comment"
    assert pq.action_comment == "looks good"


def test_parse_list_users_intent(ctx):
    pq = parse("list all users", ctx)
    assert pq.intent == "list_users"


def test_parse_list_users_role_filter_admin(ctx):
    pq = parse("show admins", ctx)
    assert pq.role_filter == "admin"


def test_parse_list_users_role_filter_managers(ctx):
    pq = parse("list managers", ctx)
    assert pq.role_filter == "manager"


def test_parse_list_projects_intent(ctx):
    pq = parse("list projects", ctx)
    assert pq.intent == "list_projects"


def test_parse_stats_intent(ctx):
    pq = parse("show me the dashboard kpi", ctx)
    assert pq.intent == "stats"


def test_parse_recent_activity_intent(ctx):
    pq = parse("what happened recently", ctx)
    assert pq.intent == "recent_activity"


def test_parse_unresolved_assignee_recorded(ctx):
    pq = parse("show bugs assigned to Zelig", ctx)
    assert "Zelig" in pq.unresolved_assignee_names


def test_parse_ambiguous_name_recorded(ctx):
    pq = parse("show bugs assigned to Jane", ctx)
    # Both Janes match -> recorded as ambiguous, not added as an assignee
    assert any("Jane" in phrase for phrase, _ in pq.ambiguous_names)
    assert pq.assignee_ids == []


def test_parse_pronoun_bug_ref_flag(ctx):
    pq = parse("close it", ctx)
    # action sets status; pronoun flag still set in _populate_output_prefs.
    assert pq.used_pronoun_bug is True


def test_parse_quoted_text_search(ctx):
    pq = parse('show bugs about "login crash"', ctx)
    assert pq.text_search == "login crash"


def test_parse_time_window_propagates(ctx, now_wed):
    pq = parse("yesterday's bugs", ctx, now=now_wed)
    assert pq.time_window is not None and pq.time_window.label == "yesterday"


def test_parse_project_loose_cue(ctx):
    pq = parse("project mobile bugs", ctx)
    assert 10 in pq.project_ids


def test_parse_project_literal_name_fallback(ctx):
    # No "in project ..." cue — falls back to the literal-name walk.
    pq = parse("show me Billing Platform bugs", ctx)
    assert 12 in pq.project_ids


# ---------------------------------------------------------------------------
# describe_filters output
# ---------------------------------------------------------------------------
def test_describe_filters_open_collapses_to_open_token():
    pq = ParsedQuery(statuses=list(OPEN_STATUSES))
    assert "open" in describe_filters(pq)


def test_describe_filters_with_assignee_and_env_and_window():
    pq = ParsedQuery(
        statuses=["Closed"],
        priorities=["High"],
        environments=["PROD"],
        project_names=["Mobile"],
        assignee_names=["John"],
        reporter_names=["Alice"],
        text_search="crash",
        time_window=TimeWindow(label="today"),
    )
    out = describe_filters(pq)
    for needle in ("closed", "high priority", "in PROD", "Mobile",
                   "John", "Alice", "crash", "today"):
        assert needle in out


def test_describe_filters_empty_query_returns_empty_string():
    assert describe_filters(ParsedQuery()) == ""
