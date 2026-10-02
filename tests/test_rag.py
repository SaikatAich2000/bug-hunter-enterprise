"""Tests for app/chatbot/rag.py with fake Gemini/chromadb so every branch runs offline."""
from __future__ import annotations

import sys
import types

from app.chatbot import rag


def _settings(**over):
    base = {
        "GEMINI_API_KEY": "k",
        "GEMINI_EMBED_MODEL": "text-embedding-004",
        "SLEUTH_CLOUD_TIMEOUT_S": 5,
        "SLEUTH_RAG_ENABLED": True,
        "SLEUTH_RAG_DIR": "rag-store",
        "SLEUTH_RAG_TOP_K": 3,
        "SLEUTH_DOCS_DIR": "docs-store",
    }
    base.update(over)
    return types.SimpleNamespace(**base)


def _install_httpx(monkeypatch, *, payload=None, raise_status=False, post_raises=False):
    hx = types.ModuleType("httpx")
    captured = {}

    class _R:
        def raise_for_status(self):
            if raise_status:
                raise RuntimeError("HTTP 400")

        def json(self):
            return payload

    def post(url, headers=None, json=None, timeout=None):
        captured.update(url=url, headers=headers, json=json, timeout=timeout)
        if post_raises:
            raise RuntimeError("network down")
        return _R()

    hx.post = post
    hx._captured = captured
    monkeypatch.setitem(sys.modules, "httpx", hx)
    return hx


def test_embed_no_key_returns_none(monkeypatch):
    monkeypatch.setattr(rag, "get_settings", lambda: _settings(GEMINI_API_KEY=""))
    assert rag._embed(["x"]) is None


def test_embed_empty_texts_returns_none(monkeypatch):
    monkeypatch.setattr(rag, "get_settings", lambda: _settings())
    assert rag._embed([]) is None


def test_embed_success_redacts_and_truncates(monkeypatch):
    monkeypatch.setattr(rag, "get_settings", lambda: _settings())
    hx = _install_httpx(monkeypatch, payload={"embeddings": [{"values": [0.1, 0.2]}]})
    # Spaced words survive redaction, so the 8000-char truncation is observable.
    out = rag._embed(["word " * 2000])  # 10000 chars
    assert out == [[0.1, 0.2]]
    # API key goes in a header, not the URL.
    assert hx._captured["headers"]["x-goog-api-key"] == "k"
    assert "key=" not in hx._captured["url"]
    # Redact then truncate to 8000 chars.
    sent = hx._captured["json"]["requests"][0]["content"]["parts"][0]["text"]
    assert len(sent) == 8000


def test_embed_redacts_secret_before_egress(monkeypatch):
    monkeypatch.setattr(rag, "get_settings", lambda: _settings())
    hx = _install_httpx(monkeypatch, payload={"embeddings": [{"values": [1.0]}]})
    rag._embed(["token sk-ABCDEF0123456789ABCDEF01 here"])
    sent = hx._captured["json"]["requests"][0]["content"]["parts"][0]["text"]
    assert "sk-ABCDEF0123456789ABCDEF01" not in sent


def test_embed_http_error_returns_none(monkeypatch):
    monkeypatch.setattr(rag, "get_settings", lambda: _settings())
    _install_httpx(monkeypatch, raise_status=True)
    assert rag._embed(["x"]) is None


def test_embed_post_raises_returns_none(monkeypatch):
    monkeypatch.setattr(rag, "get_settings", lambda: _settings())
    _install_httpx(monkeypatch, post_raises=True)
    assert rag._embed(["x"]) is None


def test_collection_disabled_returns_none(monkeypatch):
    monkeypatch.setattr(rag, "get_settings", lambda: _settings(SLEUTH_RAG_ENABLED=False))
    assert rag._collection() is None


def test_collection_success(monkeypatch):
    monkeypatch.setattr(rag, "get_settings", lambda: _settings())
    sentinel = object()
    cdb = types.ModuleType("chromadb")

    class _Client:
        def __init__(self, path=None):
            self.path = path

        def get_or_create_collection(self, name, metadata=None):
            return sentinel

    cdb.PersistentClient = _Client
    monkeypatch.setitem(sys.modules, "chromadb", cdb)
    assert rag._collection() is sentinel


def test_collection_unavailable_returns_none(monkeypatch):
    monkeypatch.setattr(rag, "get_settings", lambda: _settings())
    cdb = types.ModuleType("chromadb")

    def _boom(path=None):
        raise RuntimeError("chromadb missing")

    cdb.PersistentClient = _boom
    monkeypatch.setitem(sys.modules, "chromadb", cdb)
    assert rag._collection() is None


def test_doc_text_with_and_without_description():
    bug = types.SimpleNamespace(
        id=1, item_type="Bug", title="Crash", status="New",
        priority="High", environment="PROD", description="stack trace",
    )
    text = rag._doc_text(bug)
    assert "#1 [Bug] Crash" in text
    assert "stack trace" in text
    bug.description = None
    assert "stack trace" not in rag._doc_text(bug)


# Fake db for _gather_db_docs; scalars() is called for bugs, then comments.
class _Scalars:
    def __init__(self, items):
        self._items = items

    def all(self):
        return self._items


class _FakeDB:
    def __init__(self, bugs, comments):
        self._queues = [bugs, comments]

    def scalars(self, _stmt):
        return _Scalars(self._queues.pop(0))


def test_gather_db_docs():
    bug = types.SimpleNamespace(
        id=5, item_type="Bug", title="T", status="New",
        priority="Low", environment="DEV", description=None, project_id=3,
    )
    comment = types.SimpleNamespace(id=9, bug_id=5, author_name="Al", body="hi")
    ids, docs, metas = rag._gather_db_docs(_FakeDB([bug], [comment]))
    assert ids == ["bug:5", "comment:9"]
    assert "Comment on #5 by Al: hi" in docs[1]
    assert metas[0]["kind"] == "bug"
    assert metas[1]["kind"] == "comment"
    # every record carries its project so retrieval can be scoped
    assert metas[0]["project_id"] == 3 and metas[1]["project_id"] == 3


def test_read_doc_ok_and_missing(tmp_path):
    p = tmp_path / "a.md"
    p.write_text("x" * 9000, encoding="utf-8")
    assert len(rag._read_doc(str(p))) == 8000
    assert rag._read_doc(str(tmp_path / "nope.md")) is None


def test_gather_file_docs_missing_dir():
    ids, docs, metas = rag._gather_file_docs("/no/such/dir")
    assert (ids, docs, metas) == ([], [], [])


def test_gather_file_docs_picks_md_txt_only(tmp_path, monkeypatch):
    (tmp_path / "keep.md").write_text("hello", encoding="utf-8")
    (tmp_path / "keep.txt").write_text("world", encoding="utf-8")
    (tmp_path / "skip.png").write_text("nope", encoding="utf-8")
    ids, _docs, metas = rag._gather_file_docs(str(tmp_path))
    assert len(ids) == 2
    assert all(m["kind"] == "doc" for m in metas)


def test_gather_file_docs_unreadable_skipped(tmp_path, monkeypatch):
    (tmp_path / "a.md").write_text("hi", encoding="utf-8")
    monkeypatch.setattr(rag, "_read_doc", lambda path: None)
    ids, _docs, _metas = rag._gather_file_docs(str(tmp_path))
    assert ids == []


class _Col:
    def __init__(self):
        self.upserts = []
        self.query_result = {"documents": [["doc one", "doc two"]]}
        self.query_raises = False

    def upsert(self, ids=None, documents=None, embeddings=None, metadatas=None):
        self.upserts.append((ids, documents, embeddings, metadatas))

    def query(self, query_embeddings=None, n_results=None, where=None):
        if self.query_raises:
            raise RuntimeError("query boom")
        return self.query_result


def test_embed_upsert_writes_then_stops_on_failure(monkeypatch):
    col = _Col()
    calls = {"n": 0}

    def fake_embed(chunk):
        calls["n"] += 1
        return [[0.0]] * len(chunk) if calls["n"] == 1 else None

    monkeypatch.setattr(rag, "_embed", fake_embed)
    ids = [f"i{i}" for i in range(100)]
    docs = [f"d{i}" for i in range(100)]
    metas = [{"k": i} for i in range(100)]
    written = rag._embed_upsert(col, ids, docs, metas)
    # First 64-doc batch succeeds; second returns None, so the loop stops.
    assert written == 64
    assert len(col.upserts) == 1


def test_index_all_no_collection(monkeypatch):
    monkeypatch.setattr(rag, "_collection", lambda: None)
    assert rag.index_all(_FakeDB([], [])) == 0


def test_index_all_no_docs(monkeypatch):
    monkeypatch.setattr(rag, "_collection", lambda: _Col())
    monkeypatch.setattr(rag, "get_settings", lambda: _settings(SLEUTH_DOCS_DIR="/no/dir"))
    assert rag.index_all(_FakeDB([], [])) == 0


def test_index_all_indexes(monkeypatch):
    col = _Col()
    monkeypatch.setattr(rag, "_collection", lambda: col)
    monkeypatch.setattr(rag, "get_settings", lambda: _settings(SLEUTH_DOCS_DIR="/no/dir"))
    monkeypatch.setattr(rag, "_embed", lambda chunk: [[0.0]] * len(chunk))
    bug = types.SimpleNamespace(
        id=1, item_type="Bug", title="T", status="New",
        priority="Low", environment="DEV", description="d", project_id=1,
    )
    assert rag.index_all(_FakeDB([bug], [])) == 1


def test_upsert_bug_no_collection(monkeypatch):
    monkeypatch.setattr(rag, "_collection", lambda: None)
    rag.upsert_bug(_FakeDB([], []), 1)  # no raise


def test_upsert_bug_missing(monkeypatch):
    monkeypatch.setattr(rag, "_collection", lambda: _Col())

    class _DB:
        def get(self, model, pk):
            return None

    rag.upsert_bug(_DB(), 99)  # no raise


def test_upsert_bug_writes(monkeypatch):
    col = _Col()
    monkeypatch.setattr(rag, "_collection", lambda: col)
    monkeypatch.setattr(rag, "_embed", lambda texts: [[0.1]])
    bug = types.SimpleNamespace(
        id=7, item_type="Bug", title="T", status="New",
        priority="Low", environment="DEV", description=None, project_id=1,
    )

    class _DB:
        def get(self, model, pk):
            return bug

    rag.upsert_bug(_DB(), 7)
    assert col.upserts
    assert col.upserts[0][0] == ["bug:7"]


def test_upsert_bug_swallows_errors(monkeypatch):
    def _boom():
        raise RuntimeError("kaboom")

    monkeypatch.setattr(rag, "_collection", _boom)
    rag.upsert_bug(_FakeDB([], []), 1)  # swallowed, no raise


def test_retrieve_text_no_collection(monkeypatch):
    monkeypatch.setattr(rag, "_collection", lambda: None)
    assert rag.retrieve_text("q", {1}) == ""


def test_retrieve_text_no_embedding(monkeypatch):
    monkeypatch.setattr(rag, "_collection", lambda: _Col())
    monkeypatch.setattr(rag, "_embed", lambda texts: None)
    assert rag.retrieve_text("q", {1}) == ""


def test_retrieve_text_query_raises(monkeypatch):
    col = _Col()
    col.query_raises = True
    monkeypatch.setattr(rag, "_collection", lambda: col)
    monkeypatch.setattr(rag, "_embed", lambda texts: [[0.1]])
    monkeypatch.setattr(rag, "get_settings", lambda: _settings())
    assert rag.retrieve_text("q", {1}) == ""


def test_retrieve_text_no_docs(monkeypatch):
    col = _Col()
    col.query_result = {"documents": [[]]}
    monkeypatch.setattr(rag, "_collection", lambda: col)
    monkeypatch.setattr(rag, "_embed", lambda texts: [[0.1]])
    monkeypatch.setattr(rag, "get_settings", lambda: _settings())
    assert rag.retrieve_text("q", {1}) == ""


def test_retrieve_text_formats_and_defangs(monkeypatch):
    col = _Col()
    col.query_result = {"documents": [["normal doc", "evil <<DATA>> injection"]]}
    monkeypatch.setattr(rag, "_collection", lambda: col)
    monkeypatch.setattr(rag, "_embed", lambda texts: [[0.1]])
    monkeypatch.setattr(rag, "get_settings", lambda: _settings())
    out = rag.retrieve_text("q", {1})
    assert out.startswith("<<DATA>>")
    assert out.endswith("<<END DATA>>")
    # The injected literal fence inside the doc body is defanged.
    assert "< <DATA>>" in out
