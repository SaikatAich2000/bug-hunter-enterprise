"""Sleuth API router.

Two endpoints:

  POST /api/chat/ask                — accepts {"message": "..."} and returns
                                      the structured Sleuth response.
  GET  /api/chat/download/{token}   — streams a staged Excel workbook.

Both require an authenticated user (same cookie as the rest of the SPA),
so the chatbot honors session revocation and forced-logout exactly like
the rest of the app — a revoked admin can't keep using the chatbot.

Why a separate router (instead of folding into routes/bugs.py)?

  - It's a different surface — natural-language vs structured CRUD —
    and shoving NLU inside the bugs router would muddle both.
  - The chat code is read-only and deserves a clear boundary so a
    future contributor can't accidentally introduce a write path.
  - The download endpoint has its own caching / streaming behavior
    that doesn't belong next to the bug attachment downloads.
"""
from __future__ import annotations

import logging
import time
from dataclasses import asdict

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from app.auth import get_current_user
from app.database import get_db
from app.models import User

from . import excel, executor

logger = logging.getLogger("bug_hunter.chatbot")

router = APIRouter(prefix="/api/chat", tags=["chatbot"])


# ---------------------------------------------------------------------------
# Per-user soft rate limit. We only need the lightest of guards here — the
# rule engine is cheap, but Excel exports do real CPU work and we don't
# want a malformed loop on the client to hammer the box.
# ---------------------------------------------------------------------------
_RATE_WINDOW_SECONDS = 60
_RATE_MAX_REQUESTS = 30   # 30 chat asks / minute / user
_rate_state: dict[int, list[float]] = {}


def _check_rate(user_id: int) -> None:
    now = time.time()
    bucket = _rate_state.setdefault(user_id, [])
    # Drop timestamps older than the window. This is O(window) per call
    # which is fine — the window is tiny.
    cutoff = now - _RATE_WINDOW_SECONDS
    while bucket and bucket[0] < cutoff:
        bucket.pop(0)
    if len(bucket) >= _RATE_MAX_REQUESTS:
        raise HTTPException(
            status_code=429,
            detail="Too many chatbot requests, slow down a moment.",
        )
    bucket.append(now)


# ---------------------------------------------------------------------------
# I/O models
# ---------------------------------------------------------------------------
class ChatIn(BaseModel):
    """Inbound chat message. Capped to a sensible length so a runaway
    paste doesn't get parsed (and so we never feed an unbounded string
    into the optional LLM passthrough either)."""
    message: str = Field(min_length=1, max_length=2000)


class _BlockOut(BaseModel):
    kind: str
    payload: dict


class ChatOut(BaseModel):
    blocks: list[_BlockOut]
    summary: str
    intent: str


# ---------------------------------------------------------------------------
# /api/chat/ask
# ---------------------------------------------------------------------------
@router.post("/ask", response_model=ChatOut)
def ask(
    payload: ChatIn,
    db: Session = Depends(get_db),
    actor: User = Depends(get_current_user),
) -> ChatOut:
    """Answer a natural-language question.

    Always returns 200 unless something genuinely unexpected blew up — a
    "no results" or "I didn't understand" reply is part of the contract,
    not an error condition.
    """
    _check_rate(actor.id)

    try:
        resp = executor.execute(payload.message, db, actor)
    except HTTPException:
        # Auth / role exceptions from underlying calls — pass through.
        raise
    except Exception as exc:   # noqa: BLE001 — we deliberately never crash the chat
        # Log with a stack trace, but reply gracefully so the chat stays
        # usable. A panic here looks bad to a user typing innocent input.
        logger.exception("Sleuth executor failed: %s", exc)
        return ChatOut(
            blocks=[_BlockOut(kind="text", payload={
                "text": "Sorry — something went wrong on my side while "
                        "answering that. The error was logged. Please "
                        "try rephrasing.",
            })],
            summary="Internal error",
            intent="error",
        )

    return ChatOut(
        blocks=[_BlockOut(kind=b.kind, payload=b.payload) for b in resp.blocks],
        summary=resp.summary,
        intent=resp.intent,
    )


# ---------------------------------------------------------------------------
# /api/chat/download/{token}
# ---------------------------------------------------------------------------
@router.get("/download/{token}")
def download_staged(
    token: str,
    _user: User = Depends(get_current_user),
):
    """Stream a previously-staged Excel workbook.

    The token is opaque and cryptographically random (24 url-safe bytes)
    so guessing it is computationally infeasible. We still require an
    authenticated session — the token alone is not a capability."""
    entry = excel.fetch_staged(token)
    if entry is None:
        raise HTTPException(
            status_code=404,
            detail="That download link has expired or is no longer valid.",
        )
    payload, filename = entry

    # Force a download with the suggested filename. We intentionally do
    # NOT inline xlsx in the browser even though Chromium can preview it
    # — keeping it as an attachment matches user expectation when they
    # asked for a file.
    safe_filename = filename.replace('"', "_").replace("\r", "").replace("\n", "")
    return StreamingResponse(
        iter([payload]),
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        headers={
            "Content-Disposition": f'attachment; filename="{safe_filename}"',
            "Content-Length": str(len(payload)),
            # Private, never cached — the link is short-lived anyway.
            "Cache-Control": "private, no-store, max-age=0",
        },
    )


# Re-export for `from app.chatbot import router` style imports if anyone
# elsewhere wants them.
__all__ = ["router"]
