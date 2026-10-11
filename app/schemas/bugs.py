"""Pydantic schemas: bugs."""
from __future__ import annotations

import re
from datetime import datetime
from html.parser import HTMLParser
from typing import Optional

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from app.schemas.common import (
    ALLOWED_BULK_ACTIONS,
    ALLOWED_ENVIRONMENTS,
    ALLOWED_ITEM_TYPES,
    ALLOWED_LINK_TYPES,
    ALLOWED_PRIORITIES,
    ALLOWED_STATUSES,
    MIN_TITLE_LENGTH,
    _reject_explicit_nulls,
    _strip_and_check_min_length,
    normalize_choice,
    plain_text,
    sanitize_html,
    statuses_for_type,
)
from app.schemas.users import UserBrief


# --- Bug ---
class CustomValueIn(BaseModel):
    field_id: int
    value: str = Field(default="", max_length=2000)


class BugCreate(BaseModel):
    project_id: int
    title: str = Field(max_length=200)
    # Rich HTML; 1 MB ceiling fits inline pasted screenshots. Sanitized below.
    description: str = Field(default="", max_length=1_000_000)
    reporter_id: Optional[int] = None
    assignee_ids: list[int] = Field(default_factory=list, max_length=200)
    item_type: str = Field(default="Bug")
    status: str = Field(default="New")
    priority: str = Field(default="Medium")
    environment: str = Field(default="DEV")
    due_date: Optional[str] = None
    # Link to an event at creation time.
    event_id: Optional[int] = None
    custom_values: list[CustomValueIn] = Field(default_factory=list, max_length=200)

    @field_validator("title")
    @classmethod
    def _strip_title(cls, v: str) -> str:
        return _strip_and_check_min_length(v, MIN_TITLE_LENGTH, "Title")

    @field_validator("description")
    @classmethod
    def _strip_desc(cls, v: str) -> str:
        # Work-item descriptions are plain text: legacy editor HTML is flattened,
        # then the allowlist sanitizer neutralizes any stray markup. Outer
        # whitespace is trimmed so empty-string checks behave.
        if not isinstance(v, str): return v
        return plain_text(v)

    @field_validator("item_type")
    @classmethod
    def _check_item_type(cls, v: str) -> str:
        return normalize_choice(v, ALLOWED_ITEM_TYPES, "item_type")

    @field_validator("status")
    @classmethod
    def _check_status(cls, v: str) -> str:
        # Global union here; per-type check runs in _check_status_for_type below.
        return normalize_choice(v, ALLOWED_STATUSES, "status")

    @field_validator("priority")
    @classmethod
    def _check_priority(cls, v: str) -> str:
        return normalize_choice(v, ALLOWED_PRIORITIES, "priority")

    @field_validator("environment")
    @classmethod
    def _check_env(cls, v: str) -> str:
        return normalize_choice(v, ALLOWED_ENVIRONMENTS, "environment")

    @field_validator("due_date")
    @classmethod
    def _check_due(cls, v: Optional[str]) -> Optional[str]:
        if v in (None, ""): return None
        try:
            datetime.strptime(v, "%Y-%m-%d")
        except ValueError as exc:
            raise ValueError("due_date must be YYYY-MM-DD") from exc
        return v

    @field_validator("assignee_ids")
    @classmethod
    def _dedup(cls, v: list[int]) -> list[int]:
        seen: list[int] = []
        for x in v or []:
            if x not in seen: seen.append(x)
        return seen

    @model_validator(mode="after")
    def _check_status_for_type(self) -> "BugCreate":
        # Cross-field: status must be valid for the chosen item_type.
        allowed = statuses_for_type(self.item_type)
        if self.status not in allowed:
            raise ValueError(
                f"Status '{self.status}' is not valid for {self.item_type}. "
                f"Allowed: {', '.join(allowed)}"
            )
        return self


class BugUpdate(BaseModel):
    # Replaces the item's custom-field values when given (same as PUT /custom-values).
    custom_values: Optional[list[CustomValueIn]] = Field(default=None, max_length=200)
    project_id: Optional[int] = None
    title: Optional[str] = Field(default=None, max_length=200)
    description: Optional[str] = Field(default=None, max_length=1_000_000)
    reporter_id: Optional[int] = None
    assignee_ids: Optional[list[int]] = Field(default=None, max_length=200)
    item_type: Optional[str] = None
    status: Optional[str] = None
    priority: Optional[str] = None
    environment: Optional[str] = None
    due_date: Optional[str] = None
    # int to link, null/0 to unlink; route uses exclude_unset to tell "clear"
    # from "not provided".
    event_id: Optional[int] = None
    sprint_id: Optional[int] = None
    # Opt-in optimistic concurrency: echo the last-seen updated_at; a mismatch
    # returns 409. Omit for last-write-wins.
    expected_updated_at: Optional[str] = Field(default=None, max_length=64)

    # Preferred concurrency token: the integer version counter (sub-second, no
    # clock skew). Omit both fields for last-write-wins.
    expected_version: Optional[int] = Field(default=None, ge=0)

    @model_validator(mode="before")
    @classmethod
    def _no_nulls(cls, data):
        return _reject_explicit_nulls(
            data, ("project_id", "title", "item_type", "status", "priority", "environment"),
        )

    @field_validator("title")
    @classmethod
    def _strip_title(cls, v: Optional[str]) -> Optional[str]:
        if v is None: return None
        return _strip_and_check_min_length(v, MIN_TITLE_LENGTH, "Title")

    @field_validator("description")
    @classmethod
    def _strip_desc(cls, v: Optional[str]) -> Optional[str]:
        # Same plain-text policy as BugCreate.description.
        if v is None: return None
        if not isinstance(v, str): return v
        return plain_text(v)

    @field_validator("item_type")
    @classmethod
    def _check_item_type(cls, v: Optional[str]) -> Optional[str]:
        return None if v is None else normalize_choice(v, ALLOWED_ITEM_TYPES, "item_type")

    @field_validator("status")
    @classmethod
    def _check_status(cls, v: Optional[str]) -> Optional[str]:
        return None if v is None else normalize_choice(v, ALLOWED_STATUSES, "status")

    @field_validator("priority")
    @classmethod
    def _check_priority(cls, v: Optional[str]) -> Optional[str]:
        return None if v is None else normalize_choice(v, ALLOWED_PRIORITIES, "priority")

    @field_validator("environment")
    @classmethod
    def _check_env(cls, v: Optional[str]) -> Optional[str]:
        return None if v is None else normalize_choice(v, ALLOWED_ENVIRONMENTS, "environment")

    @field_validator("due_date")
    @classmethod
    def _check_due(cls, v: Optional[str]) -> Optional[str]:
        if v in (None, ""): return None
        try:
            datetime.strptime(v, "%Y-%m-%d")
        except ValueError as exc:
            raise ValueError("due_date must be YYYY-MM-DD") from exc
        return v

    @field_validator("assignee_ids")
    @classmethod
    def _dedup(cls, v: Optional[list[int]]) -> Optional[list[int]]:
        if v is None: return None
        seen: list[int] = []
        for x in v:
            if x not in seen: seen.append(x)
        return seen


class AttachmentBrief(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: int
    filename: str
    content_type: str
    size_bytes: int
    uploader_user_id: Optional[int] = None
    uploader_name: str
    comment_id: Optional[int] = None
    created_at: datetime


class _RichTextPlainifier(HTMLParser):
    """Render allowlisted rich-text HTML as readable plain text."""
    _BLOCK_TAGS = frozenset({
        "p", "div", "li", "ul", "ol", "tr",
        "h1", "h2", "h3", "h4", "h5", "h6", "blockquote", "pre",
    })

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self._parts: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, Optional[str]]]) -> None:
        # <br> is always a hard break (the editor emits <div><br></div> spacers);
        # block elements open a fresh line only when none is already pending.
        if tag == "br" or (
            tag in self._BLOCK_TAGS
            and not (self._parts and self._parts[-1].endswith("\n"))
        ):
            self._parts.append("\n")

    def handle_data(self, data: str) -> None:
        self._parts.append(data)


def rich_text_to_plain(markup: str) -> str:
    """Readable plain text for rich-text fields (descriptions).

    Block elements become newlines, entities are decoded, inline markup is
    dropped, runs of blank lines collapse to one, and leading/trailing blank
    lines are trimmed. Applied to incoming descriptions so storage and every
    API response carry plain text; the UI editor renders the newlines as line
    breaks on open and on display.
    """
    if not markup:
        return ""
    parser = _RichTextPlainifier()
    try:
        parser.feed(markup)
        parser.close()
    except Exception:  # malformed markup must never break a response
        return markup
    out: list[str] = []
    for raw in "".join(parser._parts).split("\n"):
        line = raw.strip()
        if line:
            out.append(line)
        elif out and out[-1] != "":
            out.append("")  # collapse blank-line runs to one separator
    while out and out[-1] == "":
        out.pop()
    return "\n".join(out)


class BugOut(BaseModel):

    model_config = ConfigDict(from_attributes=True)

    id: int

    # Stable user-facing reference ("USRSTR-12"). Assigned once and preserved
    # across a work-item type change so existing references keep resolving.
    display_id: str = ""
    project_id: int
    project_name: Optional[str] = None
    title: str
    description: str
    reporter: Optional[UserBrief] = None
    assignees: list[UserBrief] = Field(default_factory=list)
    item_type: str = "Bug"
    status: str
    priority: str
    environment: str
    due_date: Optional[str] = None
    # Both nullable so standalone items (no event) keep the same shape.
    event_id: Optional[int] = None
    event_name: Optional[str] = None
    created_at: datetime
    updated_at: datetime
    version: int = 1
    attachment_count: int = 0
    can_edit: bool = False
    # Hierarchy and planning fields: epic link (standard issues), parent (Sub-tasks).
    epic_id: Optional[int] = None
    parent_id: Optional[int] = None
    story_points: Optional[float] = None
    owner_id: Optional[int] = None
    mandatory: bool = False
    blocked: bool = False
    blocked_reason: str = ""
    sprint_id: Optional[int] = None
    sprint_name: Optional[str] = None


class BugListResponse(BaseModel):
    items: list[BugOut]
    page: int
    page_size: int
    total: int
    pages: int


# --- Comment / Activity / Detail ---
class CommentIn(BaseModel):
    # 200 KB ceiling fits a pasted screenshot; sanitizer strips dangerous payloads.
    body: str = Field(min_length=1, max_length=200_000)

    @field_validator("body")
    @classmethod
    def _strip(cls, v: str) -> str:
        # Sanitize server-side, then require visible text or an <img src> —
        # an all-tags/whitespace body is treated as empty.
        if not isinstance(v, str):
            raise ValueError("Comment body must be a string")
        cleaned = sanitize_html(v.strip())
        text_only = re.sub(r"<[^>]+>", "", cleaned).strip()
        # Check src explicitly so a src-less <img> doesn't count as non-empty.
        has_image = re.search(r"<img\b[^>]*\bsrc=", cleaned, re.IGNORECASE) is not None
        if not text_only and not has_image:
            raise ValueError("Comment body cannot be empty")
        return cleaned


class CommentOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: int
    bug_id: int
    author_user_id: Optional[int] = None
    author_name: str
    body: str
    created_at: datetime
    attachments: list[AttachmentBrief] = Field(default_factory=list)


class ActivityOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: int
    bug_id: Optional[int] = None
    entity_type: str
    entity_id: Optional[int] = None
    actor_user_id: Optional[int] = None
    actor_name: str
    action: str
    detail: str
    created_at: datetime


# --- Item linking ---
class BugLinkIn(BaseModel):
    target_bug_id: int
    link_type: str = "relates"

    @field_validator("link_type")
    @classmethod
    def _check_type(cls, v: str) -> str:
        return normalize_choice(v, ALLOWED_LINK_TYPES, "link_type")


class BugLinkOut(BaseModel):
    """One link from a bug's perspective. `direction` is outgoing (this bug is
    source) or incoming; `label` is the phrasing from this side."""
    id: int
    link_type: str
    direction: str          # "outgoing" | "incoming"
    label: str
    other_bug_id: int
    other_bug_title: str
    other_bug_status: str
    other_bug_item_type: str
    created_at: datetime


# Bulk actions — the multi-select toolbar on the list view.
class BulkActionIn(BaseModel):
    action: str
    ids: list[int] = Field(min_length=1, max_length=500)
    # Value for set_status / set_priority / set_environment; route normalizes it.
    value: Optional[str] = Field(default=None, max_length=20)
    # Optional {bug_id: version} map; drifted rows are reported as conflicts.
    expected_versions: Optional[dict[int, int]] = Field(default=None, max_length=500)

    @field_validator("action")
    @classmethod
    def _check_action(cls, v: str) -> str:
        if v not in ALLOWED_BULK_ACTIONS:
            raise ValueError(f"Invalid action. Allowed: {', '.join(ALLOWED_BULK_ACTIONS)}")
        return v

    @field_validator("ids")
    @classmethod
    def _dedup_ids(cls, v: list[int]) -> list[int]:
        seen: list[int] = []
        for x in v or []:
            if x not in seen:
                seen.append(x)
        return seen


class BulkActionResult(BaseModel):
    updated: int = 0
    skipped: int = 0
    failed: int = 0
    # Rows skipped because their version drifted from expected_versions.
    conflicts: int = 0
    message: str = ""


# --- Bulk import (spreadsheet upload) — app/bulk_import.py does the work. ---
class BulkImportRowIssue(BaseModel):
    row: int
    title: str = ""
    error: str = ""


class BulkImportRowWarning(BaseModel):
    row: int
    title: str = ""
    warning: str = ""


class BulkImportResult(BaseModel):
    created: int = 0
    failed: int = 0
    created_ids: list[int] = Field(default_factory=list)
    errors: list[BulkImportRowIssue] = Field(default_factory=list)
    warnings: list[BulkImportRowWarning] = Field(default_factory=list)
    errors_truncated: bool = False
    warnings_truncated: bool = False
    message: str = ""


class BugDetail(BugOut):
    comments: list[CommentOut] = Field(default_factory=list)
    activities: list[ActivityOut] = Field(default_factory=list)
    attachments: list[AttachmentBrief] = Field(default_factory=list)
    # Links in both directions, rendered from this item's perspective.
    links: list[BugLinkOut] = Field(default_factory=list)
