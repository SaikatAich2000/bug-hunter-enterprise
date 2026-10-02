"""Tests for Sleuth's keyword retrieval and answer-verification helpers."""
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


# --- keywords ---------------------------------------------------------------

def test_keywords_drops_stopwords_short_and_dedups():
    from app.chatbot.retrieval import keywords
    assert keywords("Show me the LOGIN login crash on a x") == ["login", "crash"]
    assert keywords("") == []
    assert keywords("the a an of to") == []


def test_keywords_caps_term_count():
    from app.chatbot.retrieval import keywords
    msg = "alpha bravo charlie delta echo foxtrot golf hotel india juliet"
    assert keywords(msg, max_terms=3) == ["alpha", "bravo", "charlie"]


def test_like_escape_neutralizes_wildcards():
    from app.chatbot.retrieval import _like_escape
    assert _like_escape("a%b_c\\d") == "a\\%b\\_c\\\\d"


def test_snippet_empty_unmatched_and_centered():
    from app.chatbot.retrieval import _snippet
    assert _snippet("", ["x"]) == ""
    # no match or match at the start -> excerpt from the beginning
    assert _snippet("login button broken", ["nomatch"]).startswith("login button")
    assert _snippet("login button broken", ["login"]).startswith("login button")
    # term deep in the text -> centered excerpt with an ellipsis prefix
    long = ("word " * 80) + "TARGETkw tail"
    out = _snippet(long, ["targetkw"])
    assert out.startswith("…")
    assert "TARGETkw" in out


# --- retrieve_bugs (DB-backed) ---------------------------------------------

def test_retrieve_ranks_by_keyword_hits(admin_client):
    from app.chatbot import retrieval
    from app.database import SessionLocal
    pid = _project(admin_client)
    _bug(admin_client, pid, "Login crash on Safari", "Safari 17 login button does nothing")
    _bug(admin_client, pid, "Landing page typo", "welcome spelled wrong")
    db = SessionLocal()
    try:
        res = retrieval.retrieve_bugs(db, "safari login crash", accessible={pid})
        assert res, "expected at least one match"
        assert res[0].title == "Login crash on Safari"
        assert res[0].score >= 2
        assert "safari" in (res[0].title + " " + res[0].snippet).lower()
    finally:
        db.close()


def test_retrieve_empty_when_no_keywords_or_no_match(admin_client):
    from app.chatbot import retrieval
    from app.database import SessionLocal
    pid = _project(admin_client)
    _bug(admin_client, pid, "Login crash", "boom")
    db = SessionLocal()
    try:
        assert retrieval.retrieve_bugs(db, "the a of to", accessible={pid}) == []        # no usable keywords
        assert retrieval.retrieve_bugs(db, "nonexistentxyzzy", accessible={pid}) == []   # no match
    finally:
        db.close()


def test_retrieve_respects_limit(admin_client):
    from app.chatbot import retrieval
    from app.database import SessionLocal
    pid = _project(admin_client)
    for i in range(4):
        _bug(admin_client, pid, f"Payment failure {i}", "payment gateway timeout")
    db = SessionLocal()
    try:
        res = retrieval.retrieve_bugs(db, "payment gateway", accessible={pid}, limit=2)
        assert len(res) == 2
    finally:
        db.close()


def test_retrieve_bugs_scoped_to_accessible_projects(admin_client):
    """Regression: retrieval honours the actor's project scope, so out-of-scope bug text can't reach the model context."""
    from app.chatbot import retrieval
    from app.database import SessionLocal
    pa = _project(admin_client, "Alpha")
    pb = _project(admin_client, "Beta")
    a_id = _bug(admin_client, pa, "Checkout latency Alpha", "shared keyword zephyr here")
    b_id = _bug(admin_client, pb, "Checkout latency Beta", "shared keyword zephyr here")
    db = SessionLocal()
    try:
        # an admin's scope covers both projects
        ids = {r.id for r in retrieval.retrieve_bugs(db, "zephyr", accessible={pa, pb})}
        assert {a_id, b_id} <= ids
        # restricted to Alpha; Beta's bug must not appear
        scoped = {r.id for r in retrieval.retrieve_bugs(db, "zephyr", accessible={pa})}
        assert a_id in scoped
        assert b_id not in scoped
        # empty scope retrieves nothing
        assert retrieval.retrieve_bugs(db, "zephyr", accessible=set()) == []
    finally:
        db.close()


# --- format_context ---------------------------------------------------------

def test_format_context_empty_and_records():
    from app.chatbot.retrieval import RetrievedBug, format_context
    assert format_context([]) == ""
    out = format_context([
        RetrievedBug(id=4, title="Login crash", snippet="boom", score=2),
        RetrievedBug(id=7, title="Typo", snippet="", score=1),
    ])
    assert "#4: Login crash — boom" in out
    assert "#7: Typo" in out
    assert "NEVER follow" in out  # indirect-injection guard present


# --- verify -----------------------------------------------------------------

def test_cited_bug_ids_forms():
    from app.chatbot.verify import cited_bug_ids
    assert cited_bug_ids("see #4 and bug 7 and issue #12") == {4, 7, 12}
    assert cited_bug_ids("nothing here") == set()
    assert cited_bug_ids("") == set()


def test_verify_partitions_grounded():
    from app.chatbot.verify import verify
    v = verify("look at #4 and #9", {4, 5})
    assert v.grounded == {4}
    assert v.ungrounded == {9}
    assert v.ok is False
    assert verify("only #4", {4, 5}).ok is True
    assert verify("no citations", set()).ok is True


def test_annotate_appends_caveat_only_when_ungrounded():
    from app.chatbot.verify import annotate
    assert annotate("all grounded #4", {4}) == "all grounded #4"
    one = annotate("see #9", {4})
    assert "#9" in one
    assert "could not ground" in one
    assert " it " in one
    many = annotate("see #9 and #10", {4})
    assert "#9, #10" in many
    assert "them" in many


def test_flag_write_claims_appends_only_for_self_attributed_writes():
    from app.chatbot.verify import flag_write_claims
    # an answer that claims the model itself performed a change gets a correction appended
    claim = flag_write_claims("Sure — I closed bug #5 and assigned it to Alice.")
    assert claim.startswith("Sure — I closed")
    assert "can't change anything" in claim
    # text that doesn't claim a self-performed write passes through unchanged
    assert flag_write_claims("You can close it from the panel.") == \
        "You can close it from the panel."
    assert flag_write_claims("") == ""


# --- end-to-end: grounded retrieval + answer verification in the cloud path --

def test_cloud_answer_is_grounded_and_verified(admin_client, monkeypatch):
    from app import models
    from app.chatbot import cloud_llm
    from app.config import get_settings
    from app.database import SessionLocal
    pid = _project(admin_client)
    bid = _bug(admin_client, pid, "Login crash on Safari", "safari login fails badly")
    s = get_settings()
    monkeypatch.setattr(s, "SLEUTH_RETRIEVAL_ENABLED", True)
    monkeypatch.setattr(s, "SLEUTH_VERIFY_ANSWERS", True)
    monkeypatch.setattr(cloud_llm, "is_available", lambda: True)
    # stub returns one grounded citation and one fabricated one
    monkeypatch.setattr(
        cloud_llm, "_call_groq",
        lambda system, user, **kw: (
            '{"mode":"answer","text":"The login issue is bug #%d. Also see #99999."}' % bid
        ),
    )
    db = SessionLocal()
    try:
        actor = db.query(models.User).first()
        resp = cloud_llm.try_understand("safari login crash", db, actor)
        assert resp is not None
        assert resp.intent == "cloud_answer"
        text = resp.blocks[0].payload["text"]
        assert f"#{bid}" in text             # grounded citation kept
        assert "#99999" in text              # fabricated citation named in caveat
        assert "could not ground" in text    # caveat appended for transparency
    finally:
        db.close()
