"""Sleuth NLU helpers and unknown-intent fallback (pure in-process): free-text search,
enum-word guard, quoted-search precedence, "did you mean" suggestions, and the unknown Response shape.
"""
from __future__ import annotations

import os

# Disable the cloud layer and point at a throwaway DB so imports never touch a real database.
os.environ.setdefault("SLEUTH_CLOUD_ENABLED", "0")
os.environ.setdefault("DATABASE_URL", "sqlite:///./_sleuth_smart_test.db")
os.environ.setdefault("SESSION_SECRET", "x" * 32)

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
