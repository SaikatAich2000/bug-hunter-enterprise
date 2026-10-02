"""Build/refresh the Sleuth RAG index (bugs, comments, docs/) in the local Chroma store.

Safe to re-run — upserts, so existing vectors are replaced, not duplicated.
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.chatbot import rag  # noqa: E402
from app.database import SessionLocal  # noqa: E402


def _misconfig_reasons() -> list[str]:
    """Reasons RAG indexing can't work at all."""
    from app.config import get_settings
    s = get_settings()
    reasons: list[str] = []
    if not s.SLEUTH_RAG_ENABLED:
        reasons.append("SLEUTH_RAG_ENABLED is off")
    if not s.GEMINI_API_KEY:
        reasons.append("GEMINI_API_KEY is empty")
    try:
        import chromadb  # noqa: F401
    except ImportError:
        reasons.append("chromadb is not installed")
    return reasons


def main() -> int:
    db = SessionLocal()
    try:
        n = rag.index_all(db)
    finally:
        db.close()
    if n == 0:
        reasons = _misconfig_reasons()
        if reasons:
            # Fail so a cron/CI wrapper notices.
            print("Indexed 0 documents — misconfigured: " + "; ".join(reasons))
            return 1
        # Empty corpus is a no-op, not a failure — don't alert schedulers.
        print("Indexed 0 documents — nothing to index yet (empty corpus).")
        return 0
    print(f"Indexed {n} documents into the Sleuth RAG store.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
