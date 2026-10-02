"""Sleuth Layer 2 — statistical intent classifier (pure-Python TF-IDF + cosine).

Catches paraphrases the rule engine in nlu.py misses. Below the confidence
threshold it returns None, leaving dispatch to the LLM or "unknown".
"""
from __future__ import annotations

import math
import re
from collections import Counter
from dataclasses import dataclass

# (intent_label, example_phrasings); labels match nlu.parse() intents.
_CORPUS: list[tuple[str, list[str]]] = [
    ("greeting", [
        "hi", "hello", "hey there", "good morning", "yo", "howdy",
        "hi sleuth", "hello bot", "anyone there", "namaste",
    ]),
    ("thanks", [
        "thanks", "thank you", "thx", "appreciate it", "cheers",
        "ty mate", "much obliged", "thanks a lot",
    ]),
    ("help", [
        "help", "what can you do", "how do i use this", "show me commands",
        "give me examples", "what are your capabilities", "guide me",
        "i need help", "instructions please",
    ]),
    ("list_bugs", [
        "show all bugs", "list bugs", "what bugs are open",
        "give me all open bugs", "find bugs assigned to bob",
        "open bugs in production", "all critical bugs",
        "show me what is open", "pull up the bug list",
        "what's outstanding", "any open issues",
        "tickets in progress", "active issues",
        "the things still open", "what is unresolved",
    ]),
    ("list_users", [
        "list users", "show all users", "who are the users",
        "show me all admins", "list managers", "who are the admins",
        "all the people", "list every user", "team list",
        "people on the system", "active members",
    ]),
    ("list_projects", [
        "list projects", "show all projects", "what projects exist",
        "give me the projects", "all projects please", "project list",
        "what projects do we have",
    ]),
    ("stats", [
        "summary", "overview", "stats", "statistics", "give me a summary",
        "show me the dashboard", "kpi", "metrics", "snapshot",
        "how are we doing", "what's the state of things", "report card",
        "high level view",
    ]),
    ("recent_activity", [
        "recent activity", "what happened recently", "audit log",
        "latest changes", "show audit trail", "recent updates",
        "last few actions", "what was changed",
        "show me the history", "what's been going on",
    ]),
    ("bug_detail", [
        "bug 5", "show bug 12", "details of #42", "tell me about bug 7",
        "info on issue 3", "what is bug 99", "look up bug #1",
    ]),
    ("action_assign", [
        "assign bug 5 to alice", "give bug 5 to bob", "assign #12 to carol",
        "delegate bug 3 to alice", "hand bug 7 over to bob",
        "let bob handle bug 5", "alice should take bug 12",
    ]),
    ("action_unassign", [
        "unassign alice from bug 5", "remove bob from #12",
        "drop alice from bug 3", "take bob off bug 7",
        "deassign carol from #1",
    ]),
    ("action_set_status", [
        "close bug 5", "mark #5 as resolved", "resolve bug 12",
        "fix bug 7", "reopen bug 3", "set bug 5 status to closed",
        "change bug 12 status to in progress", "this one is fixed",
    ]),
    ("action_set_priority", [
        "set bug 5 priority to high", "make bug 5 critical",
        "change priority of #12 to low", "downgrade bug 7 to medium",
        "escalate bug 3 to high",
    ]),
    ("action_add_comment", [
        "comment on bug 5: this is fixed", "add a comment to #12",
        "leave a note on bug 3 saying retest please",
        "post a reply to bug 7", "remark on #1: works for me",
    ]),
    ("action_create_bug", [
        "create a bug titled login broken",
        "file a new bug about checkout failing",
        "open a ticket for the upload error",
        "raise an issue: search returns nothing",
        "log a bug for the date filter",
    ]),
    ("action_create_project", [
        "create a project called mercury",
        "add a new project named sentinel",
        "register a project: customer portal",
        "set up a project for the web portal",
    ]),
]


_TOKEN_RE = re.compile(r"[a-z0-9]+", re.IGNORECASE)
_STOPWORDS = {
    "a", "an", "the", "is", "are", "be", "to", "of", "for", "and", "or",
    "in", "on", "at", "with", "by", "from", "i", "me", "my", "you",
    "your", "this", "that", "those", "these", "do", "does", "did",
    "have", "has", "had", "will", "would", "should", "could", "can",
    "was", "were", "been", "being", "it", "its", "we", "us", "our",
}


def _tokenize(text: str) -> list[str]:
    """Lowercase alphanumeric tokens minus stop-words; digits become ``<num>``
    so "bug 5" and "bug 12" vectorize the same."""
    if not text:
        return []
    tokens: list[str] = []
    for raw in _TOKEN_RE.findall(text.lower()):
        if raw.isdigit():
            tokens.append("<num>")
        elif raw in _STOPWORDS:
            continue
        else:
            tokens.append(raw)
    return tokens


# IDF weights computed once at import time.
@dataclass
class _TrainedModel:
    docs: list[tuple[str, Counter[str]]]   # [(intent, term_counts), ...]
    idf: dict[str, float]                  # term -> idf weight
    doc_norms: list[float]                 # cached vector norms per doc


def _train(corpus: list[tuple[str, list[str]]]) -> _TrainedModel:
    docs: list[tuple[str, Counter[str]]] = []
    for intent, examples in corpus:
        for ex in examples:
            tokens = _tokenize(ex)
            if not tokens:
                continue
            docs.append((intent, Counter(tokens)))

    n = len(docs)
    df: Counter[str] = Counter()
    for _intent, tc in docs:
        for term in tc:
            df[term] += 1
    # Smoothed IDF: log((1+N)/(1+df)) + 1, matching sklearn's default.
    idf: dict[str, float] = {
        t: math.log((1 + n) / (1 + d)) + 1.0
        for t, d in df.items()
    }
    # Cache doc norms so cosine is a single dot product per query.
    doc_norms: list[float] = []
    for _intent, tc in docs:
        s = 0.0
        for term, count in tc.items():
            w = count * idf.get(term, 0.0)
            s += w * w
        doc_norms.append(math.sqrt(s) or 1.0)

    return _TrainedModel(docs=docs, idf=idf, doc_norms=doc_norms)


_MODEL = _train(_CORPUS)


@dataclass
class Prediction:
    intent: str
    confidence: float   # 0..1, cosine similarity against best-matching doc
    runner_up: str = ""
    runner_up_confidence: float = 0.0


def _vec(tokens: list[str], idf: dict[str, float]) -> tuple[Counter[str], float]:
    tc = Counter(tokens)
    s = 0.0
    for term, count in tc.items():
        w = count * idf.get(term, 0.0)
        s += w * w
    return tc, math.sqrt(s) or 1.0


def _cosine(qc: Counter[str], q_norm: float,
            dc: Counter[str], d_norm: float,
            idf: dict[str, float]) -> float:
    # Iterate the smaller counter for speed.
    if len(qc) > len(dc):
        qc, dc = dc, qc
    dot = 0.0
    for term, q_count in qc.items():
        d_count = dc.get(term)
        if not d_count:
            continue
        w_q = q_count * idf.get(term, 0.0)
        w_d = d_count * idf.get(term, 0.0)
        dot += w_q * w_d
    return dot / (q_norm * d_norm)


def predict(message: str, threshold: float = 0.35) -> Prediction | None:
    """Return the most likely intent for ``message``, or None below the
    confidence threshold (calibrated: paraphrases pass, noise doesn't)."""
    tokens = _tokenize(message)
    if not tokens:
        return None
    # A <num>-only message ("is it 5?") has no signal but can spuriously match
    # corpus docs that also contain <num>.
    if all(t == "<num>" for t in tokens):
        return None
    qc, q_norm = _vec(tokens, _MODEL.idf)

    best_intent = ""
    best_score = -1.0
    runner_intent = ""
    runner_score = -1.0
    for (intent, dc), d_norm in zip(_MODEL.docs, _MODEL.doc_norms):
        score = _cosine(qc, q_norm, dc, d_norm, _MODEL.idf)
        if score > best_score:
            if best_intent != intent:
                # New top intent from a different class — demote old best.
                runner_intent = best_intent
                runner_score = best_score
            best_intent = intent
            best_score = score
        elif score > runner_score and intent != best_intent:
            runner_intent = intent
            runner_score = score

    if best_score < threshold:
        return None
    return Prediction(
        intent=best_intent,
        confidence=best_score,
        runner_up=runner_intent,
        runner_up_confidence=max(runner_score, 0.0),
    )


# Debug helper: per-doc scores for a message.
def explain(message: str, top_k: int = 5) -> list[tuple[str, float]]:
    tokens = _tokenize(message)
    if not tokens:
        return []
    qc, q_norm = _vec(tokens, _MODEL.idf)
    scored: list[tuple[str, float]] = []
    for (intent, dc), d_norm in zip(_MODEL.docs, _MODEL.doc_norms):
        scored.append((intent,
                       _cosine(qc, q_norm, dc, d_norm, _MODEL.idf)))
    scored.sort(key=lambda kv: kv[1], reverse=True)
    return scored[:top_k]


__all__ = ["predict", "explain", "Prediction"]
