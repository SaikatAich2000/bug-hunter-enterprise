"""Sleuth chatbot router: POST /ask, POST /ingest (admin), GET /download/{token}.
All three require an authenticated user (session revocation applies as usual)."""
from __future__ import annotations

import logging
import threading
import time
from datetime import timedelta
from typing import Annotated, Optional

from fastapi import APIRouter, Depends, File, Form, HTTPException, UploadFile
from fastapi.responses import Response, StreamingResponse
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from app.api_docs import XLSX_FILE_200
from app.auth import get_current_user, require_admin
from app.config import get_settings
from app.database import get_db
from app.models import ChatConversation, ChatMessage, User, _utcnow

from . import excel, executor

logger = logging.getLogger("bug_hunter.chatbot")

# Annotated dependency aliases keep injection out of parameter defaults.
DbDep = Annotated[Session, Depends(get_db)]
CurrentUser = Annotated[User, Depends(get_current_user)]
AdminUser = Annotated[User, Depends(require_admin)]


def _persist_turn(db: Session, actor: User, user_msg: str,
                  resp: "executor.Response") -> None:
    """Append the turn to the chat transcript (reuses a conversation active in
    the last 30 min, else starts one). Gated by a setting; failures swallowed."""
    if not get_settings().SLEUTH_CHAT_MEMORY_ENABLED:
        return
    try:
        cutoff = _utcnow() - timedelta(minutes=30)
        conv = (
            db.query(ChatConversation)
            .filter(ChatConversation.user_id == actor.id,
                    ChatConversation.updated_at >= cutoff)
            .order_by(ChatConversation.updated_at.desc())
            .first()
        )
        if conv is None:
            conv = ChatConversation(user_id=actor.id)
            db.add(conv)
            db.flush()
        # Engine label for observability, derived from the intent prefix.
        engine = "rules"
        if resp.intent.startswith("cloud_"):
            engine = "cloud"
        elif resp.intent in {"unknown", "error"}:
            engine = ""
        # Prefer text block (real prose) over summary (table/file-only replies).
        said = next(
            (b.payload.get("text", "") for b in resp.blocks if b.kind == "text"),
            "",
        ) or (resp.summary or "")
        db.add(ChatMessage(conversation_id=conv.id, role="user",
                           content=user_msg[:4000], engine=""))
        db.add(ChatMessage(conversation_id=conv.id, role="assistant",
                           content=said[:4000], engine=engine))
        conv.updated_at = _utcnow()
        db.commit()
    except Exception:  # noqa: BLE001 — transcript is non-critical
        db.rollback()
        logger.debug("Sleuth chat transcript persist failed", exc_info=True)

router = APIRouter(prefix="/api/chat", tags=["chatbot"])


# Per-user rate limit (Excel export does real CPU work, unlike the rule engine).
_RATE_WINDOW_SECONDS = 60
_RATE_MAX_REQUESTS = 30   # 30 chat asks / minute / user
_rate_state: dict[int, list[float]] = {}
_rate_lock = threading.Lock()  # sync endpoints run in a thread pool


def _check_rate(user_id: int) -> None:
    now = time.time()
    cutoff = now - _RATE_WINDOW_SECONDS
    with _rate_lock:
        bucket = _rate_state.setdefault(user_id, [])
        # Evict timestamps older than the window.
        while bucket and bucket[0] < cutoff:
            bucket.pop(0)
        if len(bucket) >= _RATE_MAX_REQUESTS:
            raise HTTPException(
                status_code=429,
                detail="Too many chatbot requests, slow down a moment",
            )
        bucket.append(now)


class ChatIn(BaseModel):
    """Inbound chat message, length-capped."""
    message: str = Field(min_length=1, max_length=2000)


class _BlockOut(BaseModel):
    kind: str
    payload: dict


class ChatOut(BaseModel):
    blocks: list[_BlockOut]
    summary: str
    intent: str


@router.post(
    "/ask",
    responses={
        429: {"description": "Rate limit exceeded — too many chatbot requests."},
    },
)
def ask(
    payload: ChatIn,
    db: DbDep,
    actor: CurrentUser,
) -> ChatOut:
    """Answer a natural-language question.

    Always returns 200 unless something truly unexpected happened. "No results"
    and "I didn't understand" are normal responses, not errors.
    """
    _check_rate(actor.id)

    try:
        resp = executor.execute(payload.message, db, actor)
    except HTTPException:
        raise  # auth/role exceptions pass through unchanged
    except Exception as exc:   # noqa: BLE001 — we deliberately never crash the chat
        logger.exception("Sleuth executor failed: %s", exc)
        return ChatOut(
            blocks=[_BlockOut(kind="text", payload={
                "text": "Sorry — something went wrong on my side while "
                        "answering that. The error was logged. Please "
                        "try rephrasing",
            })],
            summary="Internal error",
            intent="error",
        )

    _persist_turn(db, actor, payload.message, resp)

    return ChatOut(
        blocks=[_BlockOut(kind=b.kind, payload=b.payload) for b in resp.blocks],
        summary=resp.summary,
        intent=resp.intent,
    )


# Admin uploads a doc; ingest.py extracts candidates, this returns a preview.
_INGEST_CHUNK = 256 * 1024


async def _read_upload_limited(file: UploadFile, limit: int) -> bytes:
    """Stream the upload, aborting with 413 before it can exceed `limit`."""
    buf = bytearray()
    while True:
        chunk = await file.read(_INGEST_CHUNK)
        if not chunk:
            break
        buf.extend(chunk)
        if len(buf) > limit:
            raise HTTPException(
                status_code=413,
                detail=f"Document too large. Max {limit // (1024 * 1024)} MB.",
            )
    return bytes(buf)


def _ingest_text_reply(text: str, intent: str) -> ChatOut:
    return ChatOut(
        blocks=[_BlockOut(kind="text", payload={"text": text})],
        summary=text[:80], intent=intent,
    )


def _ingest_preview_reply(specs: list, method: str, filename: str, project_name: str) -> ChatOut:
    """Preview reply listing extracted items; creation waits for 'create them'."""
    n = len(specs)
    how = "read it with AI" if method == "ai" else "parsed it"
    head = (
        f"📄 I {how} and found **{n}** work item{'' if n == 1 else 's'} in "
        f"**{filename}**. If you want, I'll add them to project "
        f"**{project_name}** — just reply **create them** (or **cancel**)."
    )
    rows = [[s["title"], s.get("priority", "Medium"), s.get("item_type", "Bug")]
            for s in specs[:50]]
    blocks = [
        _BlockOut(kind="text", payload={"text": head}),
        _BlockOut(kind="table", payload={"headers": ["Title", "Priority", "Type"], "rows": rows}),
    ]
    if n > len(rows):
        blocks.append(_BlockOut(kind="text", payload={"text": f"…and {n - len(rows)} more."}))
    blocks.append(_BlockOut(kind="suggestions", payload={"items": [
        {"label": f"✓ Create all {n}", "send": "create them"},
        {"label": "Cancel", "send": "cancel"},
    ]}))
    return ChatOut(blocks=blocks, summary=f"Found {n} items — awaiting confirmation",
                   intent="ingest_preview")


@router.post(
    "/ingest",
    responses={
        400: {"description": "No project to file the imported items under."},
        403: {"description": "Document ingest is admin-only."},
        413: {"description": "Uploaded document is too large."},
        429: {"description": "Rate limit exceeded — too many requests."},
    },
)
async def ingest_document(
    db: DbDep,
    actor: AdminUser,
    file: Annotated[UploadFile, File()],
    project_id: Annotated[Optional[int], Form()] = None,
) -> ChatOut:
    """Admin-only document import: extract candidates, stage them, return a
    preview. Nothing is created until the admin replies 'create them'."""
    _check_rate(actor.id)
    from app.chatbot import ingest as _ingest
    from app.chatbot.memory import store as _mem

    data = await _read_upload_limited(file, _ingest.MAX_DOC_BYTES)
    if not data:
        return _ingest_text_reply("That file was empty — there was nothing to read.", "ingest_empty")

    specs, method = _ingest.extract_specs(file.filename or "", data)
    if not specs:
        return _ingest_text_reply(
            "I read that document but couldn't find any work items in it. I work "
            "best with a list (one item per line), a CSV / Excel sheet with a "
            "**Title** column, or a JSON array of `{title, priority, ...}` objects.",
            "ingest_empty",
        )
    project = _ingest.resolve_project_for_preview(db, actor, project_id)
    if project is None:
        raise HTTPException(
            status_code=400,
            detail="There's no project to file these into yet — create one first.",
        )
    _mem.stage_ingest(actor.id, {
        "specs": specs, "filename": file.filename or "document",
        "project_id": project.id, "project_name": project.name, "method": method,
    })
    return _ingest_preview_reply(specs, method, file.filename or "document", project.name)


@router.get(
    "/download/{token}",
    response_class=Response,
    responses={
        **XLSX_FILE_200,
        404: {"description": "Download link has expired or is no longer valid."},
        429: {"description": "Rate limit exceeded — too many requests."},
    },
)
def download_staged(
    token: str,
    _user: CurrentUser,
):
    """Stream a staged Excel workbook. Token is bound to its creating user
    (fetch_staged enforces it) so a leaked token can't be redeemed by anyone
    else; a mismatch returns 404 rather than confirming the token exists."""
    _check_rate(_user.id)
    entry = excel.fetch_staged(token, _user.id)
    if entry is None:
        raise HTTPException(
            status_code=404,
            detail="That download link has expired or is no longer valid",
        )
    payload, filename = entry

    # Sanitize the filename for the Content-Disposition header before use.
    safe_filename = filename.replace('"', "_").replace("\r", "").replace("\n", "")
    return StreamingResponse(
        iter([payload]),
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        headers={
            "Content-Disposition": f'attachment; filename="{safe_filename}"',
            "Content-Length": str(len(payload)),
            # Tokens are short-lived; never cache the response.
            "Cache-Control": "private, no-store, max-age=0",
        },
    )


__all__ = ["router"]
