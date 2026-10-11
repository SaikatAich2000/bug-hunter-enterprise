"""Sleuth NLU helpers and unknown-intent fallback (pure in-process): free-text search,
enum-word guard, quoted-search precedence, "did you mean" suggestions, and the unknown Response shape.
"""
from __future__ import annotations

from datetime import datetime, timezone

from app.chatbot import nlu  # noqa: E402
from app.chatbot.executor import _did_you_mean, _handle_unknown  # noqa: E402


# -- Bare free-text search --------------------------------------------------
def test_bare_text_search_after_cue_words():
    assert nlu._extract_text_search("find bugs about login crash") == "login crash"
    assert nlu._extract_text_search("issues regarding checkout") == "checkout"
    assert nlu._extract_text_search("anything mentioning timeout") == "timeout"
    assert nlu._extract_text_search("tickets concerning the API") == "the API"
    assert nlu._extract_text_search("bugs related to payments") == "payments"


def test_quoted_search_takes_precedence():
    # Quoted search wins over the bare-cue path.
    assert nlu._extract_text_search('bugs about "exact phrase"') == "exact phrase"


def test_no_cue_means_no_text_search():
    assert nlu._extract_text_search("list open bugs") is None
    assert nlu._extract_text_search("how many critical bugs in PROD") is None


def test_enum_only_phrase_is_not_text_search():
    # "high priority"/"in progress" are enum filter values, not search topics.
    assert nlu._extract_text_search("bugs about high priority") is None
    assert nlu._extract_text_search("anything regarding in progress") is None


def test_trailing_filter_clause_is_stripped():
    # Only the topic is returned; the trailing "in project X" clause is stripped.
    assert nlu._extract_text_search("bugs about login in project Apollo") == "login"


# -- "Did you mean" — best-effort, deterministic, never raises ---------------
def test_did_you_mean_is_safe_on_gibberish():
    # No signal in pure noise; must return None without raising.
    assert _did_you_mean("qwerty zxcvbn asdfgh") is None
    assert _did_you_mean("") is None


def test_did_you_mean_returns_known_phrase_or_none():
    # Return value must be a canonical suggestion or None, never an invented string.
    suggestions = set(__import__(
        "app.chatbot.executor", fromlist=["_INTENT_SUGGESTION"],
    )._INTENT_SUGGESTION.values())
    for msg in ("show me the bugs", "count things", "who are the people",
                "export everything", "what changed"):
        out = _did_you_mean(msg)
        assert out is None or out in suggestions


# -- Unknown-intent fallback ------------------------------------------------
def test_handle_unknown_response_shape():
    resp = _handle_unknown("flibber the wobbular")
    assert resp.intent == "unknown"
    assert resp.fallback_eligible is True
    assert resp.blocks
    assert resp.blocks[0].kind == "text"
    assert "help" in resp.blocks[0].payload["text"].lower()


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
