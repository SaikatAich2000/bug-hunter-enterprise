"""Pydantic request/response schemas."""

from __future__ import annotations

import re
from html.parser import HTMLParser
from typing import Optional

# Display-ID prefix mapping for user-facing entity references.
_DISPLAY_ID_PREFIX: dict[str, str] = {
    "Bug": "BUG",
    "Requirement": "REQ",
    "Task": "TASK",
    # Its own prefix: a Sub-task (child of an issue) is not a Task.
    "Sub-task": "SUB",
    "Story": "USRSTR",
    # UI alias of "Story" — same canonical work-item type.
    "User Story": "USRSTR",
    "Epic": "EPIC",
    "Sprint": "SPRINT",
}


def display_id_for(item_type: str | None, num_id: int | None) -> str:
    """Generate a user-facing display ID from an item type and numeric ID.
    Falls back to ``ID-{num}`` for unknown types."""
    if num_id is None:
        return ""
    prefix = _DISPLAY_ID_PREFIX.get(item_type) if item_type else None
    if prefix is None:
        prefix = "ID"
    return f"{prefix}-{num_id}"


# Server-side HTML sanitizer for rich-text fields (description, comment body):
# the contenteditable editor emits HTML and storing it raw would be stored XSS.
# Hand-rolled allowlist (avoids bleach/html5lib); swap for bleach if tags grow.
_ALLOWED_TAGS = {
    "p", "br", "div", "span",
    "b", "strong", "i", "em", "u", "s", "strike", "del", "ins",
    "ul", "ol", "li",
    "blockquote", "pre", "code",
    "h1", "h2", "h3", "h4", "h5", "h6",
    "a", "img",
}
_ALLOWED_ATTRS = {
    # Anything not listed is stripped even on allowed tags (blocks <img onerror=>).
    # `rel` on <a> is omitted so our forced rel="noopener nofollow" can't be stripped.
    "a":   {"href", "title"},
    "img": {"src", "alt", "title", "width", "height"},
    "code": {"class"},   # editor may emit `<code class="language-X">`
    "pre":  {"class"},
}

# Allowed URL schemes for href/src; data: is handled separately below.
_ALLOWED_URL_SCHEMES = ("http:", "https:", "mailto:", "/", "#")

# Raster data: URLs only — data:image/svg+xml is scriptable, so it's excluded.
_DATA_IMAGE_RASTER_PREFIXES = (
    "data:image/png", "data:image/jpeg", "data:image/jpg",
    "data:image/gif", "data:image/webp", "data:image/bmp", "data:image/avif",
)

# RCDATA/CDATA elements: their text content is dropped too (not just the tag),
# closing a parser-differential / mutation-XSS risk against a real HTML5 tokenizer.
_RCDATA_DROP_TAGS = frozenset({
    "script", "style", "textarea", "title", "noscript", "xmp",
    "iframe", "noframes", "template",
})


class _HTMLAllowlistSanitizer(HTMLParser):
    """Drops every tag/attr not on the allowlist; text content always survives."""

    # convert_charrefs=False keeps &amp;/&lt; as-is instead of collapsing them.
    def __init__(self) -> None:
        super().__init__(convert_charrefs=False)
        self.out: list[str] = []
        # Nesting depth inside RCDATA elements, so their text is suppressed.
        self._drop_text_depth = 0

    def _safe_url(self, raw: str) -> Optional[str]:
        if not raw:
            return None
        s = raw.strip()
        low = s.lower()
        # Inline pasted screenshots only (uploads take a different path).
        if low.startswith("data:image/"):
            # Raster bitmaps only (SVG is scriptable); size-capped to bound storage.
            if not low.startswith(_DATA_IMAGE_RASTER_PREFIXES):
                return None
            if len(s) > 14 * 1024 * 1024:
                return None
            return s
        for scheme in _ALLOWED_URL_SCHEMES:
            if low.startswith(scheme):
                return s
        return None

    def _kept_attrs(self, t: str, attrs: list[tuple[str, Optional[str]]]) -> list[str]:
        """Return the allowed name="value" attribute strings for tag t."""
        kept: list[str] = []
        allowed_attrs = _ALLOWED_ATTRS.get(t, set())
        for k, v in attrs:
            k = k.lower()
            if k not in allowed_attrs or v is None:
                continue
            if k in ("href", "src"):
                clean = self._safe_url(v)
                if not clean:
                    continue
                v = clean
            # Escape for HTML embedding.
            v_safe = (v.replace("&", "&amp;").replace("<", "&lt;")
                       .replace(">", "&gt;").replace('"', "&quot;"))
            kept.append(f'{k}="{v_safe}"')
        # Force our own rel on <a> so reverse-tabnabbing hardening can't be stripped.
        if t == "a":
            kept.append('rel="noopener nofollow"')
        return kept

    def handle_starttag(self, tag: str, attrs: list[tuple[str, Optional[str]]]) -> None:
        t = tag.lower()
        if t in _RCDATA_DROP_TAGS:
            self._drop_text_depth += 1
            return
        if t not in _ALLOWED_TAGS:
            return
        kept = self._kept_attrs(t, attrs)
        attr_str = (" " + " ".join(kept)) if kept else ""
        self.out.append(f"<{t}{attr_str}>")

    def handle_endtag(self, tag: str) -> None:
        t = tag.lower()
        if t in _RCDATA_DROP_TAGS:
            if self._drop_text_depth > 0:
                self._drop_text_depth -= 1
            return
        if t not in _ALLOWED_TAGS:
            return
        # Void elements have no closing tag.
        if t in ("br", "img"):
            return
        self.out.append(f"</{t}>")

    def handle_startendtag(self, tag: str, attrs: list[tuple[str, Optional[str]]]) -> None:
        # <br/> / <img .../> re-emit as a start tag. A self-closing RCDATA tag
        # is skipped without touching _drop_text_depth (it opens+closes at once).
        if tag.lower() in _RCDATA_DROP_TAGS:
            return
        self.handle_starttag(tag, attrs)

    def handle_data(self, data: str) -> None:
        if self._drop_text_depth > 0:
            return
        self.out.append(
            data.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
        )

    def handle_entityref(self, name: str) -> None:
        if self._drop_text_depth > 0:
            return
        self.out.append(f"&{name};")

    def handle_charref(self, name: str) -> None:
        if self._drop_text_depth > 0:
            return
        self.out.append(f"&#{name};")


def sanitize_html(value: Optional[str]) -> str:
    """Return allowlist-sanitized HTML for storage/display; idempotent, "" for None."""
    if value is None:
        return ""
    s = str(value)
    p = _HTMLAllowlistSanitizer()
    p.feed(s)
    p.close()
    return "".join(p.out)


# Per-item-type status sets; "New" is in every list so the create default is
# always valid. Out-of-set statuses display as-is on read but are rejected on
# create/update. ALLOWED_STATUSES is the union for type-agnostic filters.
_IN_PROGRESS = "In Progress"
_END_DATE_CANNOT_PRECEDE_START = "End date cannot precede start date"
_RELEASE_BEFORE_START = "The release date cannot be before the start date"
_ACCEPTANCE_CRITERION_NOT_FOUND = "Acceptance criterion not found"
_NOT_SET = "(not set)"

# Canonical status vocabulary — the same set is valid for every work-item type
# so that any configured board status can be applied to any item. The board column
# categories (todo/in_progress/testing/done) are what drive workflow, not the
# item type. Legacy per-type lists are preserved as comments for audit context but
# are no longer the source of truth.
CANONICAL_STATUSES = [
    "New", _IN_PROGRESS, "Testing", "In Review", "Approved",
    "Done", "Blocked", "Cancelled", "Resolved", "Closed",
    "Reopened", "Not a Bug", "Resolve Later", "Implemented",
    "Rejected", "Deferred", "Planned", "Completed",
]
STATUSES_BY_TYPE = {
    "Bug": list(CANONICAL_STATUSES),
    "Requirement": list(CANONICAL_STATUSES),
    # "Task" is a legacy alias for the canonical "Sub-task" agile type.
    # Both share the exact same status vocabulary.
    "Task": list(CANONICAL_STATUSES),
    # Agile hierarchy types. Stored Title Case,
    # same convention as the legacy types above — never lowercased in the DB.
    "Epic": list(CANONICAL_STATUSES),
    "Story": list(CANONICAL_STATUSES),
    "Sub-task": list(CANONICAL_STATUSES),
}
ALLOWED_STATUSES = list(
    dict.fromkeys(s for sts in STATUSES_BY_TYPE.values() for s in sts)
)

# "Not a Bug" items are excluded from the "Total bugs" KPI on the dashboard.
EXCLUDED_FROM_TOTAL_STATUSES = ["Not a Bug"]


def statuses_for_type(item_type: str) -> list[str]:
    """Valid statuses for item_type; falls back to Bug for unknown/legacy types."""
    return STATUSES_BY_TYPE.get(item_type or "Bug", STATUSES_BY_TYPE["Bug"])


ALLOWED_PRIORITIES = ["Low", "Medium", "High", "Critical"]
ALLOWED_ENVIRONMENTS = ["DEV", "UAT", "PROD"]

# Work-item flavors; a classifier for filtering/badges, other fields apply to all.
# Legacy types remain creatable/editable through /api/bugs. The Agile hierarchy
# types are additive and only creatable/editable through
# /api/agile/work-items — /api/bugs explicitly refuses to create/convert into them
# (see _validate_create_item_type in routes/bugs.py), so existing behavior for
# Bug/Requirement/Task is completely unaffected.
LEGACY_ITEM_TYPES = ["Bug", "Requirement", "Task"]
AGILE_ITEM_TYPES = ["Epic", "Story", "Sub-task"]
ALLOWED_ITEM_TYPES = LEGACY_ITEM_TYPES + AGILE_ITEM_TYPES

ALLOWED_ROLES = ["admin", "manager", "user"]
ALLOWED_PROJECT_ROLES = ["lead", "member"]

# Link kinds on the directed source→target edge; route renders the inverse label.
ALLOWED_LINK_TYPES = ["relates", "blocks", "duplicate"]

# Bulk toolbar actions; each reuses the single-item permission/audit/notify path.
ALLOWED_BULK_ACTIONS = [
    "set_status", "set_priority", "set_environment", "delete",
]
MIN_TITLE_LENGTH = 3
MIN_NAME_LENGTH = 2
MIN_PROJECT_NAME_LENGTH = 2
MIN_ORG_NAME_LENGTH = 2
_DATE_FORMAT_ERROR = "Dates must be YYYY-MM-DD"

# --- Agile constants ---
ALLOWED_ESTIMATION_MODES = ["story_points", "time", "item_count"]
ALLOWED_SWIMLANE_MODES = ["none", "assignee", "epic", "story", "priority"]
ALLOWED_WIP_ENFORCEMENT = ["off", "warn", "block"]
ALLOWED_CARD_COLOR_SCHEMES = ["none", "priority", "item_type", "assignee", "epic"]
ALLOWED_COLUMN_CATEGORIES = ["todo", "in_progress", "testing", "done"]

# Where an incomplete/remaining item goes on Complete/Cancel Sprint.
ALLOWED_SPRINT_DISPOSITIONS = ["backlog", "sprint", "new_sprint"]
DEFAULT_WORKING_WEEKDAYS = [1, 2, 3, 4, 5]

# --- Agile constants ---
ALLOWED_CAPACITY_UNITS = ["points", "minutes"]
ALLOWED_EPIC_HEALTH = ["on_track", "at_risk", "off_track", "unknown"]
ALLOWED_VERSION_STATUSES = ["unreleased", "released", "archived"]
ALLOWED_COMPONENT_ASSIGNEE_POLICIES = ["none", "lead"]
MIN_LABEL_NAME_LENGTH = 1


def normalize_choice(value: str, allowed: list[str], label: str) -> str:
    """Case-insensitive lookup against `allowed`; returns the canonical form."""
    if not isinstance(value, str):
        raise ValueError(f"Invalid {label}. Allowed: {', '.join(allowed)}")
    needle = value.strip().lower()
    for canonical in allowed:
        if canonical.lower() == needle:
            return canonical
    raise ValueError(f"Invalid {label}. Allowed: {', '.join(allowed)}")


# Validates local part, domain labels, and an alphabetic TLD; quantifiers
# bounded to avoid ReDoS.
_EMAIL_RE = re.compile(
    r"^(?![.])(?!.*[.]{2})[A-Za-z0-9._%+\-]+(?<![.])@"
    r"(?:[A-Za-z0-9](?:[A-Za-z0-9\-]{0,61}[A-Za-z0-9])?\.)+[A-Za-z]{2,}$"
)


def _validate_email(value: str) -> str:
    v = (value or "").strip().lower()
    if not _EMAIL_RE.match(v):
        raise ValueError("Invalid email address")
    return v


# Control characters and the Unicode line/paragraph separators: never part of a name or a
# title, and they break single-line consumers (spreadsheet cells, email headers, logs).
_LINE_BREAKS_AND_CONTROLS = re.compile(r"[\x00-\x1f\x7f-\x9f\u2028\u2029]+")
# Plain text keeps newlines and tabs; other control characters are dropped.
_TEXT_CONTROLS = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f-\x84\x86-\x9f]")


def single_line(value: str) -> str:
    """A name or title on one line: control characters and line breaks become one space."""
    return _LINE_BREAKS_AND_CONTROLS.sub(" ", value).strip()


def plain_text(value: Optional[str]) -> str:
    """User-written plain text (descriptions, goals, notes), stored as typed. It is never HTML:
    every renderer escapes it, so ``a < b`` and ``<div>`` survive exactly as written."""
    if not value:
        return ""
    text = str(value).replace("\r\n", "\n").replace("\r", "\n")
    text = text.replace("\u2028", "\n").replace("\u2029", "\n").replace("\x85", "\n")
    return _TEXT_CONTROLS.sub("", text).strip()


def validate_timezone(value: str) -> str:
    """An IANA timezone name (``Europe/Berlin``, ``UTC``); anything else is a clear error."""
    from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

    name = value.strip()
    try:
        ZoneInfo(name)
    except (ZoneInfoNotFoundError, ValueError):
        raise ValueError(f"'{name}' is not a known timezone; use an IANA name such as Europe/Berlin") from None
    return name


def _reject_explicit_nulls(data, fields: tuple[str, ...]):
    """A field that may be left out of an update but never set to null."""
    if isinstance(data, dict):
        nulls = [f for f in fields if f in data and data[f] is None]
        if nulls:
            raise ValueError(f"{', '.join(nulls)} cannot be null")
    return data


def _strip_and_check_min_length(v: str, min_len: int, label: str) -> str:
    """Single-line text, then the minimum length (Field(min_length) measures pre-strip)."""
    if not isinstance(v, str):
        raise ValueError(f"{label} must be a string")
    v = single_line(v)
    if len(v) < min_len:
        if min_len == 1:
            raise ValueError(f"{label} cannot be empty")
        raise ValueError(f"{label} must be at least {min_len} characters")
    return v
