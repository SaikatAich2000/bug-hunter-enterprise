"""Sleuth NLU — turn a free-form English message into a structured query.

This is a small but carefully-scoped rule engine. We never want to ship
an LLM into a 1-vCPU / 2 GB box, so instead we lean on the fact that
real users of a bug tracker ask a *narrow* set of question shapes:

  - "show me all open bugs assigned to John"
  - "how many critical bugs are in PROD?"
  - "export all closed bugs in project Mobile to excel"
  - "bug 42"
  - "what did Alice change today?"
  - "list active managers"

So we extract the same handful of entities (status, priority,
environment, assignee/reporter, project, bug id, time window) and the
same handful of intents (list / count / export / lookup / help / about).
The result is a dataclass the executor can turn into SQL.

Design rules:
  * **Read-only.** We never produce a query that writes.
  * **Match canonical values from the live DB**, not hard-coded names.
    Users / projects come in via context so any rename, add, or removal
    is reflected immediately on the next request.
  * **No external dependencies.** Pure stdlib regex.
  * **Order-independent.** "open bugs assigned to john" and "bugs assigned
    to john that are open" parse to the same query.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
from typing import Optional


# ---------------------------------------------------------------------------
# Canonical enums (mirrored from app.schemas — duplicated here so this
# module stays decoupled from Pydantic at parse time; the executor still
# validates against the live schema constants when it builds the query).
# ---------------------------------------------------------------------------
STATUSES_CANONICAL = [
    "New", "In Progress", "Resolved", "Closed", "Reopened",
    "Not a Bug", "Resolve Later",
]
PRIORITIES_CANONICAL = ["Low", "Medium", "High", "Critical"]
ENVIRONMENTS_CANONICAL = ["DEV", "UAT", "PROD"]
ROLES_CANONICAL = ["admin", "manager", "user"]

# "Open" in product-speak = work that hasn't been parked or finished.
# Statuses considered open by the dashboard KPI: New / In Progress / Reopened.
OPEN_STATUSES = ["New", "In Progress", "Reopened"]

# Synonym → canonical. We accept casual phrasing.
_STATUS_SYNONYMS: dict[str, list[str]] = {
    "open":           OPEN_STATUSES,                # "open bugs"
    "active":         OPEN_STATUSES,
    "ongoing":        OPEN_STATUSES,
    "in-progress":    ["In Progress"],
    "in progress":    ["In Progress"],
    "wip":            ["In Progress"],
    "new":            ["New"],
    "resolved":       ["Resolved"],
    "fixed":          ["Resolved"],
    "closed":         ["Closed"],
    "done":           ["Closed", "Resolved"],
    "reopened":       ["Reopened"],
    "reopen":         ["Reopened"],
    "not a bug":      ["Not a Bug"],
    "invalid":        ["Not a Bug"],
    "not-a-bug":      ["Not a Bug"],
    "resolve later":  ["Resolve Later"],
    "deferred":       ["Resolve Later"],
    "parked":         ["Resolve Later"],
    "later":          ["Resolve Later"],
}

_PRIORITY_SYNONYMS: dict[str, str] = {
    "low":      "Low",
    "medium":   "Medium",
    "med":      "Medium",
    "normal":   "Medium",
    "high":     "High",
    "critical": "Critical",
    "crit":     "Critical",
    "blocker":  "Critical",
    "urgent":   "Critical",
    "p0":       "Critical",
    "p1":       "High",
    "p2":       "Medium",
    "p3":       "Low",
}

# Environment is already short — accept lowercase and a couple of common
# expansions ("production", "staging" → UAT in many shops).
_ENVIRONMENT_SYNONYMS: dict[str, str] = {
    "dev":          "DEV",
    "develop":      "DEV",
    "development":  "DEV",
    "uat":          "UAT",
    "staging":      "UAT",
    "stage":        "UAT",
    "qa":           "UAT",
    "test":         "UAT",
    "testing":      "UAT",
    "prod":         "PROD",
    "production":   "PROD",
    "live":         "PROD",
}


# ---------------------------------------------------------------------------
# Stop-words for fuzzy name matching (so "the bugs against john" doesn't
# match a user named "the").
# ---------------------------------------------------------------------------
_STOPWORDS = {
    # articles / pronouns / connectives
    "a", "an", "the", "this", "that", "these", "those", "and", "or", "but",
    "to", "of", "in", "on", "for", "by", "with", "without", "from", "at",
    "as", "is", "are", "was", "were", "be", "been", "being", "have", "has",
    "had", "do", "does", "did", "will", "would", "should", "could", "can",
    "may", "might", "must", "shall", "i", "you", "he", "she", "it", "we",
    "they", "me", "my", "your", "his", "her", "their", "our", "us", "them",
    "him", "any", "all", "some", "no", "not", "yes", "if", "then", "than",
    "so", "such", "just", "only", "very", "also", "too", "much", "many",
    "more", "most", "less", "least", "few", "every", "each", "both",
    # bug-tracker-specific noise tokens we strip before name matching
    "bug", "bugs", "issue", "issues", "ticket", "tickets", "list", "show",
    "give", "find", "get", "fetch", "pull", "create", "make", "export",
    "download", "send", "tell", "what", "where", "when", "how", "why",
    "who", "which", "please", "thanks", "thank", "now", "today", "yesterday",
    "open", "closed", "resolved", "active", "new", "reopened", "fixed",
    "high", "low", "medium", "critical", "priority", "status", "environment",
    "project", "projects", "assigned", "assignee", "assignees", "reporter",
    "reported", "filed", "raised", "owned", "owner", "against", "for", "by",
    "to", "on", "in", "into", "under", "over", "above", "below", "between",
    "regarding", "about", "during", "before", "after", "around", "into",
    "many", "much", "count", "total", "summary", "overview", "stats",
    "statistics", "dashboard", "report", "reports", "analytics", "kpi",
    "user", "users", "team", "member", "members", "name", "names",
    "excel", "xlsx", "csv", "spreadsheet", "sheet", "file", "files",
    "view", "details", "detail", "info", "information", "audit", "trail",
    "history", "log", "logs", "recent", "latest", "newest", "old", "oldest",
    "week", "weeks", "day", "days", "month", "months", "year", "years",
    "hour", "hours", "minute", "minutes",
    "dev", "uat", "prod", "production", "staging", "qa", "test", "testing",
    # politeness / hedging
    "could", "would", "kindly", "really", "actually", "maybe", "perhaps",
}


# ---------------------------------------------------------------------------
# Time-window patterns
# ---------------------------------------------------------------------------
_TIME_RE = re.compile(
    r"\b("
    r"today|yesterday|"
    r"this\s+week|last\s+week|this\s+month|last\s+month|"
    r"past\s+(\d+)\s+(day|days|week|weeks|month|months|hour|hours)|"
    r"last\s+(\d+)\s+(day|days|week|weeks|month|months|hour|hours)|"
    r"in\s+the\s+last\s+(\d+)\s+(day|days|week|weeks|month|months|hour|hours)"
    r")\b",
    re.IGNORECASE,
)


# ---------------------------------------------------------------------------
# Action-verb patterns (intent detection)
# ---------------------------------------------------------------------------
_EXPORT_RE = re.compile(
    r"\b(export(?:\s+to)?|download(?:\s+as)?|save(?:\s+as)?|to\s+excel|"
    r"as\s+excel|excel\s+(?:file|export|sheet|spreadsheet)?|"
    r"xlsx|spreadsheet|generate\s+(?:an\s+)?(?:excel|xlsx|spreadsheet)|"
    r"give\s+me\s+(?:an\s+)?(?:excel|xlsx))\b",
    re.IGNORECASE,
)
_COUNT_RE = re.compile(
    r"\b(how\s+many|count(?:\s+of)?|total\s+(?:number|count|amount)?|"
    r"number\s+of|tally)\b",
    re.IGNORECASE,
)
_LIST_RE = re.compile(
    r"\b(list|show|display|give\s+me|fetch|find|get|pull(?:\s+up)?|"
    r"what\s+are|which\s+are)\b",
    re.IGNORECASE,
)
_HELP_RE = re.compile(
    r"\b(help|what\s+can\s+you\s+do|capabilities|commands|"
    r"how\s+do\s+i\s+(?:use)?|what\s+do\s+you\s+do|guide|"
    r"examples|instructions)\b",
    re.IGNORECASE,
)
_GREETING_RE = re.compile(
    r"^\s*(hi|hello|hey|yo|howdy|sup|good\s+(?:morning|afternoon|evening)|"
    r"hiya|namaste)\b",
    re.IGNORECASE,
)
_THANKS_RE = re.compile(
    r"\b(thanks|thank\s+you|thx|ty|cheers|appreciate(?:d)?)\b",
    re.IGNORECASE,
)
_BUG_ID_RE = re.compile(r"(?:bug\s*#?|issue\s*#?|ticket\s*#?|#)(\d+)\b", re.IGNORECASE)
# Also catch a bare integer when the message is essentially just an id:
#   "42", "show 42", "bug 42"
_BARE_ID_HINT = re.compile(
    r"\b(?:bug|issue|ticket|details?\s+of|info\s+(?:on|about))\s+(\d+)\b",
    re.IGNORECASE,
)

# ---------------------------------------------------------------------------
# Action / write verbs. These are checked AFTER entity extraction so the
# parser already knows about bug ids, names, projects, statuses, etc.
# ---------------------------------------------------------------------------
# Yes / no answers to a previously staged action.
_CONFIRM_YES_RE = re.compile(
    r"^\s*(?:y|yes|yeah|yep|yup|sure|ok|okay|confirm(?:ed)?|"
    r"do\s+it|proceed|go\s+ahead|please\s+do)\b\s*[!.]?\s*$",
    re.IGNORECASE,
)
_CONFIRM_NO_RE = re.compile(
    r"^\s*(?:n|no|nope|cancel|abort|stop|never\s*mind|nvm|"
    r"don'?t|do\s+not|leave\s+it)\b\s*[!.]?\s*$",
    re.IGNORECASE,
)

# Verbs that imply ASSIGN (giving a bug to someone)
_ASSIGN_RE = re.compile(
    r"\b(?:assign|reassign|allocate|allot|hand(?:\s+over)?|"
    r"give|delegate|put)\b",
    re.IGNORECASE,
)
# Verbs that imply UNASSIGN (taking a bug AWAY from someone)
_UNASSIGN_RE = re.compile(
    r"\b(?:unassign|deassign|remove|drop|take\s+(?:off|away)|"
    r"deallocate|pull\s+off)\b",
    re.IGNORECASE,
)
# Verbs that imply STATUS change. The status itself comes from
# _STATUS_SYNONYMS — this just tells us the user wants a write.
_STATUS_CHANGE_RE = re.compile(
    r"\b(?:close|closed|reopen|reopened|resolve|resolved|fix|fixed|"
    r"mark\s+(?:as|it)|set\s+(?:status|state)(?:\s+to)?|"
    r"change\s+status|update\s+status|status\s+to|"
    r"move\s+(?:it\s+|this\s+)?to\s+(?:status|state)?)\b",
    re.IGNORECASE,
)
# Verbs that imply PRIORITY change.
_PRIORITY_CHANGE_RE = re.compile(
    r"\b(?:set\s+(?:.*?\s+)?priority(?:\s+to)?|"
    r"set\s+severity(?:\s+to)?|"
    r"change\s+(?:.*?\s+)?priority|update\s+(?:.*?\s+)?priority|"
    r"make\s+(?:.*?\s+)?(?:critical|high|medium|low|urgent|blocker|p[0-3])|"
    r"raise\s+priority|escalate|de[\-\s]?escalate|downgrade|upgrade)\b",
    re.IGNORECASE,
)
# Comments
_COMMENT_RE = re.compile(
    r"\b(?:comment(?:\s+on)?|leave\s+(?:a\s+)?comment|"
    r"add\s+(?:a\s+)?comment|reply|note|post\s+(?:a\s+)?(?:comment|note))\b",
    re.IGNORECASE,
)
# Create a bug
_CREATE_BUG_RE = re.compile(
    r"\b(?:create|file|open|raise|add|new|log|report|register|submit)\s+"
    r"(?:a\s+|an\s+|the\s+)?(?:bug|issue|ticket|defect)\b",
    re.IGNORECASE,
)
# Create a project
_CREATE_PROJECT_RE = re.compile(
    r"\b(?:create|add|new|register|set\s+up)\s+"
    r"(?:a\s+|an\s+|the\s+)?project\b",
    re.IGNORECASE,
)
# Due date verbs
_DUE_DATE_RE = re.compile(
    r"\b(?:due\s+(?:date|by|on)?|set\s+(?:the\s+)?due(?:\s+date)?|"
    r"deadline|by\s+(?:next\s+)?(?:monday|tuesday|wednesday|thursday|"
    r"friday|saturday|sunday)|by\s+\d{4}-\d{2}-\d{2})\b",
    re.IGNORECASE,
)
# Pronouns that refer to the previously-mentioned bug.
_PRONOUN_BUG_RE = re.compile(
    r"\b(?:it|this|that|that\s+(?:bug|issue|ticket)|"
    r"this\s+(?:bug|issue|ticket)|the\s+(?:bug|issue|ticket))\b",
    re.IGNORECASE,
)

# Project handling — projects are referenced by name in user speech.
# We require an explicit cue word ("in project X", "for project X") because
# free-form names are too easy to confuse with regular words.
_PROJECT_CUE_RE = re.compile(
    r"(?:in|for|under|from|on|of)\s+(?:the\s+)?project\s+([A-Za-z0-9_\-\s]+?)"
    r"(?=$|[,.;!?]|\s+(?:and|or|with|by|to|that|which|who|where|when|"
    r"assigned|reported|owned|status|priority|environment|created|updated))",
    re.IGNORECASE,
)
# A looser fallback — "in MobileApp" style. Less precise; only used if the
# strict pattern misses and we have a 1-token project candidate.
_PROJECT_LOOSE_CUE_RE = re.compile(
    r"\bproject\s+([A-Za-z0-9_\-]+)\b",
    re.IGNORECASE,
)

# Role queries
_ROLE_CUE_RE = re.compile(
    r"\b(admin|admins|administrator|administrators|"
    r"manager|managers|"
    r"regular\s+user|regular\s+users|users)\b",
    re.IGNORECASE,
)


# ---------------------------------------------------------------------------
# Entities the parser produces. Kept as a plain dataclass so the executor
# can pattern-match on it without any framework awareness.
# ---------------------------------------------------------------------------
@dataclass
class TimeWindow:
    """A relative time range. start/end may be None for open-ended."""
    start: Optional[datetime] = None
    end: Optional[datetime] = None
    label: str = ""


@dataclass
class ParsedQuery:
    """Structured representation of a chat message."""
    intent: str = "unknown"
    # filters that map directly onto Bug columns
    statuses: list[str] = field(default_factory=list)
    priorities: list[str] = field(default_factory=list)
    environments: list[str] = field(default_factory=list)
    project_ids: list[int] = field(default_factory=list)
    project_names: list[str] = field(default_factory=list)   # for messaging
    assignee_ids: list[int] = field(default_factory=list)
    assignee_names: list[str] = field(default_factory=list)
    reporter_ids: list[int] = field(default_factory=list)
    reporter_names: list[str] = field(default_factory=list)
    bug_id: Optional[int] = None
    text_search: Optional[str] = None
    time_window: Optional[TimeWindow] = None
    role_filter: Optional[str] = None  # admin/manager/user

    # output preferences
    wants_export: bool = False    # excel
    wants_count: bool = False
    limit: int = 100              # default cap on rows shown in chat

    # ----- ACTION FIELDS (write-side) ------------------------------------
    # Set when the user is asking Sleuth to DO something, not just retrieve.
    # The executor inspects these to build an ActionPlan.
    action_kind: Optional[str] = None   # "assign" | "unassign" | "set_status"
                                         # | "set_priority" | "set_environment"
                                         # | "set_due_date" | "add_comment"
                                         # | "create_bug" | "create_project"
    action_value: Optional[str] = None   # canonical new value for set_*
    action_comment: Optional[str] = None # body for add_comment
    action_title: Optional[str] = None   # title for create_bug
    action_description: Optional[str] = None
    # Whether this message is a yes/no answer to a previously staged action
    confirmation: Optional[str] = None   # "yes" | "no" | None
    # When the user uses a pronoun ("it", "that bug"), the executor
    # falls back to the conversation memory's last_bug_id.
    used_pronoun_bug: bool = False
    used_pronoun_user: bool = False

    # parser feedback
    raw_message: str = ""
    notes: list[str] = field(default_factory=list)
    # If we couldn't disambiguate (e.g. "John" matched two users), the
    # executor surfaces these as a clarifying reply.
    ambiguous_names: list[tuple[str, list[str]]] = field(default_factory=list)
    # Name phrases the user gave that didn't match ANY user. The executor
    # uses this to ask for clarification instead of silently dropping the
    # filter and returning every bug — the latter is misleading when the
    # user clearly expressed an assignee / reporter intent.
    unresolved_assignee_names: list[str] = field(default_factory=list)
    unresolved_reporter_names: list[str] = field(default_factory=list)


# ---------------------------------------------------------------------------
# Parser
# ---------------------------------------------------------------------------
@dataclass
class Context:
    """Live data the parser uses to resolve names → IDs.

    Passed in fresh per call by the executor so renames / new users /
    deletions are reflected immediately. Each entry is (id, normalized_name,
    display_name) — the normalized form is lowercased, single-spaced, and
    stripped of punctuation for fuzzy matching."""
    users: list[tuple[int, str, str, str]]      # (id, normalized_name, normalized_email_local, display_name)
    projects: list[tuple[int, str, str]]        # (id, normalized_name, display_name)
    user_role_map: dict[int, str] = field(default_factory=dict)


def _normalize(s: str) -> str:
    """Lowercase + collapse whitespace. Keeps internal punctuation intact —
    we strip it only at name-match time."""
    return re.sub(r"\s+", " ", (s or "").strip().lower())


def _strip_punct(s: str) -> str:
    """Strip leading/trailing punctuation around a word for fuzzy matching."""
    return re.sub(r"[^\w\s\-']", "", s).strip()


def _tokenize(s: str) -> list[str]:
    """Lowercased token list, punctuation stripped, stopwords kept (the
    caller decides). This is intentionally simple — full POS would be
    overkill for the question shapes we accept."""
    return re.findall(r"[a-zA-Z][a-zA-Z0-9\-']+", s.lower())


# ---------- time parsing -------------------------------------------------
def _parse_time_window(message: str, now: Optional[datetime] = None) -> Optional[TimeWindow]:
    """Look for a time hint in the message. Returns None if none found."""
    now = now or datetime.now(timezone.utc)
    today_start = now.replace(hour=0, minute=0, second=0, microsecond=0)

    m = _TIME_RE.search(message)
    if not m:
        return None

    phrase = m.group(0).lower().strip()

    if phrase == "today":
        return TimeWindow(today_start, now, "today")
    if phrase == "yesterday":
        y_start = today_start - timedelta(days=1)
        return TimeWindow(y_start, today_start, "yesterday")
    if phrase == "this week":
        weekday = today_start.weekday()  # Monday=0
        wk_start = today_start - timedelta(days=weekday)
        return TimeWindow(wk_start, now, "this week")
    if phrase == "last week":
        weekday = today_start.weekday()
        this_wk_start = today_start - timedelta(days=weekday)
        last_wk_start = this_wk_start - timedelta(days=7)
        return TimeWindow(last_wk_start, this_wk_start, "last week")
    if phrase == "this month":
        m_start = today_start.replace(day=1)
        return TimeWindow(m_start, now, "this month")
    if phrase == "last month":
        m_start = today_start.replace(day=1)
        # First of last month: subtract one day from this month's first, then snap to day 1.
        last_m_end = m_start
        prev_day = last_m_end - timedelta(days=1)
        last_m_start = prev_day.replace(day=1)
        return TimeWindow(last_m_start, last_m_end, "last month")

    # "past N days/weeks/months/hours" or "last N ..."
    qty: Optional[int] = None
    unit: Optional[str] = None
    for grp in (m.group(2), m.group(4), m.group(6)):
        if grp:
            try:
                qty = int(grp)
            except ValueError:
                continue
            break
    for grp in (m.group(3), m.group(5), m.group(7)):
        if grp:
            unit = grp.lower()
            break
    if qty is not None and unit:
        if unit.startswith("hour"):
            delta = timedelta(hours=qty)
        elif unit.startswith("day"):
            delta = timedelta(days=qty)
        elif unit.startswith("week"):
            delta = timedelta(weeks=qty)
        elif unit.startswith("month"):
            # Approximate: 30 days. Good enough for relative queries.
            delta = timedelta(days=30 * qty)
        else:
            return None
        return TimeWindow(now - delta, now, f"last {qty} {unit}")
    return None


# ---------- enum extraction ---------------------------------------------
def _extract_statuses(text: str) -> list[str]:
    """Find every status synonym. Order-preserving, dedup."""
    out: list[str] = []
    norm = " " + text.lower() + " "
    # Try multi-word synonyms first ("in progress", "not a bug", "resolve later")
    # so they don't get fragmented into single-token matches.
    for syn in sorted(_STATUS_SYNONYMS.keys(), key=len, reverse=True):
        # Use word boundaries — but the synonym may contain spaces, so build a
        # simple boundary check by surrounding it with whitespace in the search.
        pattern = re.compile(rf"(?<![\w-]){re.escape(syn)}(?![\w-])", re.IGNORECASE)
        if pattern.search(norm):
            for canon in _STATUS_SYNONYMS[syn]:
                if canon not in out:
                    out.append(canon)
    return out


def _extract_priorities(text: str) -> list[str]:
    out: list[str] = []
    for syn, canon in _PRIORITY_SYNONYMS.items():
        # Allow plural forms like "blockers", "criticals". The trailing
        # "s?" is harmless on words ending in s already (e.g., "p0s" — we
        # don't pluralise codes in practice but the regex stays safe).
        if re.search(rf"\b{re.escape(syn)}s?\b", text, re.IGNORECASE):
            if canon not in out:
                out.append(canon)
    return out


def _extract_environments(text: str) -> list[str]:
    out: list[str] = []
    for syn, canon in _ENVIRONMENT_SYNONYMS.items():
        if re.search(rf"\b{re.escape(syn)}\b", text, re.IGNORECASE):
            if canon not in out:
                out.append(canon)
    return out


# ---------- name extraction ---------------------------------------------
def _candidate_name_phrases(message: str) -> list[tuple[str, str]]:
    """Pull name-like phrases out of the message.

    Returns a list of (role_hint, phrase) tuples where role_hint is one of
    'assignee', 'reporter', or '' (unknown — caller decides). We extract
    cues like:

        "assigned to John Smith"
        "for John"
        "against Mr. X"
        "owned by Alice"
        "reporter is Bob"
        "filed by Bob"
        "John Smith's bugs"
    """
    out: list[tuple[str, str]] = []

    # Assignee cues -------------------------------------------------------
    # The character class for the name phrase includes `()` so a trailing
    # parenthetical like "(export to excel)" from the chat suggestion
    # can't be absorbed into the name. The lookahead's alternation also
    # accepts "export" / "download" / "to xlsx" so suggestion text
    # appended in any common shape ends the assignee phrase cleanly.
    name_terminator_lookahead = (
        r"$|[,.;!?()]|\s+(?:and|or|with|that|which|in|for|on|by|status|"
        r"priority|environment|project|created|updated|reported|filed|"
        r"owned|export|exported|exporting|download|downloaded|"
        r"to\s+excel|as\s+excel|to\s+xlsx|as\s+xlsx)"
    )
    short_terminator_lookahead = (
        r"$|[,.;!?()]|\s+(?:and|or|in|for|on|by|status|priority|"
        r"environment|project|export|exported|exporting|download|"
        r"to\s+excel|as\s+excel|to\s+xlsx|as\s+xlsx)"
    )
    assignee_pats = [
        r"assigned\s+to\s+([^,.;!?()]+?)(?=" + name_terminator_lookahead + r")",
        r"against\s+([^,.;!?()]+?)(?=" + short_terminator_lookahead + r")",
        r"owner\s+is\s+([^,.;!?()]+?)(?=$|[,.;!?()]|\s+(?:and|or|in|for|on|by))",
        r"owned\s+by\s+([^,.;!?()]+?)(?=" + short_terminator_lookahead + r")",
        r"under\s+([^,.;!?()]+?)'s?\s+name",
        # Action verbs: "assign bug 5 to alice", "give bug 5 to alice",
        # "delegate #5 to bob", "hand over #5 to alice", "assign it to alice".
        # The optional pronoun group lets pronoun-with-memory cases parse
        # the name without first resolving the bug.
        r"(?:assign|reassign|allocate|allot|delegate|hand(?:\s+over)?|give|"
        r"put)\s+(?:the\s+)?(?:bug|issue|ticket|defect|it|this|that)?\s*"
        r"(?:#|no\.?)?\d*\s*(?:over\s+)?to\s+([^,.;!?()]+?)"
        r"(?=" + name_terminator_lookahead + r")",
        # "unassign alice from #5" / "remove alice from bug 5"
        r"(?:unassign|deassign|remove|drop|deallocate)\s+([^,.;!?()]+?)\s+"
        r"from\s+(?:the\s+)?(?:bug|issue|ticket|defect|it|this|that)?\s*"
        r"(?:#|no\.?)?\s*\d*",
    ]
    for pat in assignee_pats:
        for m in re.finditer(pat, message, re.IGNORECASE):
            phrase = m.group(1).strip().strip("'s").strip()
            if phrase:
                out.append(("assignee", phrase))

    # Reporter cues -------------------------------------------------------
    reporter_pats = [
        r"reported\s+by\s+([^,.;!?()]+?)(?=" + name_terminator_lookahead + r")",
        r"filed\s+by\s+([^,.;!?()]+?)(?=" + name_terminator_lookahead + r")",
        r"raised\s+by\s+([^,.;!?()]+?)(?=" + name_terminator_lookahead + r")",
        r"created\s+by\s+([^,.;!?()]+?)(?=" + name_terminator_lookahead + r")",
        r"opened\s+by\s+([^,.;!?()]+?)(?=" + name_terminator_lookahead + r")",
        r"reporter\s+is\s+([^,.;!?()]+?)(?=$|[,.;!?()]|\s+(?:and|or|in|for|on|by))",
    ]
    for pat in reporter_pats:
        for m in re.finditer(pat, message, re.IGNORECASE):
            phrase = m.group(1).strip().strip("'s").strip()
            if phrase:
                out.append(("reporter", phrase))

    return out


def _resolve_name(phrase: str, ctx: Context) -> list[tuple[int, str]]:
    """Best-effort match of a name phrase against the user list.

    Strategy — most specific to least:

      1. Exact normalized name match.
      2. Exact email-localpart match.
      3. Exact case-insensitive prefix on full name.
      4. Last-name match (word-boundary).
      5. First-name match (token boundary, fuzzy on multi-word names).

    Returns (id, display_name) tuples. Empty list = no match. >1 match =
    caller should ask for clarification.
    """
    norm = _normalize(_strip_punct(phrase))
    if not norm:
        return []

    # Drop obvious title prefixes ("mr", "ms", "mrs", "dr", "sir", "madam")
    # so "Mr. X" matches the user named "X".
    parts = norm.split()
    title_drop = {"mr", "mrs", "ms", "miss", "dr", "sir", "madam", "prof", "professor"}
    parts = [p for p in parts if p not in title_drop]
    if not parts:
        return []
    norm = " ".join(parts)

    # 1. Exact full-name match.
    exact = [(uid, disp) for (uid, n, _email, disp) in ctx.users if n == norm]
    if exact:
        return exact

    # 2. Email local-part match.
    email_match = [
        (uid, disp) for (uid, _n, email, disp) in ctx.users if email and email == norm
    ]
    if email_match:
        return email_match

    # 3. Prefix match on full name.
    prefix = [
        (uid, disp) for (uid, n, _email, disp) in ctx.users
        if n.startswith(norm + " ") or n == norm
    ]
    if prefix:
        return prefix

    # 4. Last-name (final-token) exact match.
    if " " not in norm:
        last_name = [
            (uid, disp) for (uid, n, _email, disp) in ctx.users
            if n.split()[-1] == norm
        ]
        if last_name:
            return last_name

        # 5. First-name (initial-token) exact match.
        first_name = [
            (uid, disp) for (uid, n, _email, disp) in ctx.users
            if n.split()[0] == norm
        ]
        if first_name:
            return first_name

    return []


def _resolve_project(phrase: str, ctx: Context) -> list[tuple[int, str]]:
    """Match a project name phrase. Same strategy as users: exact, then prefix.

    We do NOT do fuzzy single-token matching for projects because project
    names tend to be one word ("Mobile", "API") which would clash with
    regular speech.
    """
    norm = _normalize(_strip_punct(phrase))
    if not norm:
        return []
    exact = [(pid, disp) for (pid, n, disp) in ctx.projects if n == norm]
    if exact:
        return exact
    prefix = [(pid, disp) for (pid, n, disp) in ctx.projects if n.startswith(norm)]
    return prefix


# ---------- bug id ------------------------------------------------------
def _extract_bug_id(message: str) -> Optional[int]:
    m = _BUG_ID_RE.search(message)
    if m:
        try:
            return int(m.group(1))
        except ValueError:
            pass
    m = _BARE_ID_HINT.search(message)
    if m:
        try:
            return int(m.group(1))
        except ValueError:
            pass
    # If the WHOLE message is just digits (with maybe a #), accept it.
    s = message.strip().lstrip("#")
    if s.isdigit():
        try:
            return int(s)
        except ValueError:
            return None
    return None


# ---------- Free-text search -------------------------------------------
# Quoted strings → free-text search clause. e.g.  bugs about "login crash"
_QUOTED_RE = re.compile(r'"([^"]{2,})"')


def _extract_text_search(message: str) -> Optional[str]:
    m = _QUOTED_RE.search(message)
    if m:
        return m.group(1).strip() or None
    return None


# ---------------------------------------------------------------------------
# Action verb detection
# ---------------------------------------------------------------------------
# Map a (possibly past-tense) status verb to its canonical status.
_STATUS_VERB_MAP: dict[str, str] = {
    "close":      "Closed",
    "closed":     "Closed",
    "resolve":    "Resolved",
    "resolved":   "Resolved",
    "fix":        "Resolved",
    "fixed":      "Resolved",
    "reopen":     "Reopened",
    "reopened":   "Reopened",
}

# Comment body extraction. We accept several shapes:
#   "comment on bug 5: this is fixed"
#   "comment on #5 saying 'this is fixed'"
#   "add a comment to 5: works for me"
#   "leave a note on bug 5 — try again"
_COMMENT_BODY_RE = re.compile(
    r"(?:comment|note|reply)(?:\s+on\s+\S+|\s+to\s+\S+|"
    r"\s+about\s+\S+|\s+(?:#|bug\s+#?|issue\s+#?)\d+|\s+saying|\s+with)?"
    r"\s*[:\-—]\s*(.+)$",
    re.IGNORECASE | re.DOTALL,
)
# "create a bug titled X in project Y"
_CREATE_BUG_TITLE_RE = re.compile(
    r"(?:bug|issue|ticket|defect)\s+"
    r"(?:titled|named|called|with\s+title|saying)?\s*"
    r"[\"\'\u201c\u201d\u2018\u2019]([^\"\'\u201c\u201d\u2018\u2019]+)"
    r"[\"\'\u201c\u201d\u2018\u2019]",
    re.IGNORECASE,
)
_CREATE_BUG_BARE_RE = re.compile(
    r"(?:create|file|open|raise|add|new|log|report|register|submit)\s+"
    r"(?:a\s+|an\s+|the\s+)?(?:bug|issue|ticket|defect)\s+"
    r"(?:titled|named|called|saying|that\s+says)?\s*[:\-\u2014]?\s*(.+?)$",
    re.IGNORECASE,
)
_CREATE_PROJECT_NAME_RE = re.compile(
    r"(?:create|add|new|register|set\s+up)\s+"
    r"(?:a\s+|an\s+|the\s+)?project\s+"
    r"(?:called|named|titled)?\s*"
    r"[\"\'\u201c\u201d\u2018\u2019]?([A-Za-z0-9_\- ]{1,120}?)"
    r"[\"\'\u201c\u201d\u2018\u2019]?\s*$",
    re.IGNORECASE,
)


def _has_pronoun_bug_ref(msg: str) -> bool:
    """True if the message contains 'it' / 'that bug' / 'this issue'."""
    return bool(_PRONOUN_BUG_RE.search(msg))


def _detect_action(msg: str, pq: ParsedQuery) -> Optional[str]:
    """Decide whether the user is asking Sleuth to PERFORM something.

    Returns one of: "assign", "unassign", "set_status", "set_priority",
    "set_environment", "set_due_date", "add_comment", "create_bug",
    "create_project". Returns None if no write intent is detected.
    Mutates pq in place to populate action_value / action_comment / etc.
    """
    # Comments first: a colon-introduced clause is a strong signal.
    if _COMMENT_RE.search(msg):
        m = _COMMENT_BODY_RE.search(msg)
        if m:
            body = m.group(1).strip().strip("\"'")
            if body:
                pq.action_comment = body
                return "add_comment"
        return "add_comment"   # body missing — executor asks

    # Create project — check before create_bug because both share verbs.
    if _CREATE_PROJECT_RE.search(msg):
        m = _CREATE_PROJECT_NAME_RE.search(msg)
        if m:
            pq.action_title = m.group(1).strip()
        return "create_project"

    # Create bug.
    if _CREATE_BUG_RE.search(msg):
        m = _CREATE_BUG_TITLE_RE.search(msg)
        if m:
            pq.action_title = m.group(1).strip()
        else:
            m2 = _CREATE_BUG_BARE_RE.search(msg)
            if m2:
                title = m2.group(1).strip()
                title = re.sub(
                    r"\s+(?:in|for|under)\s+(?:the\s+)?project\s+.+$",
                    "", title, flags=re.IGNORECASE)
                title = re.sub(
                    r"\s+(?:with|having)\s+priority\s+\w+.*$",
                    "", title, flags=re.IGNORECASE)
                title = re.sub(
                    r"\s+(?:assign(?:ed)?\s+to|for)\s+\w+.*$",
                    "", title, flags=re.IGNORECASE)
                if title:
                    pq.action_title = title
        return "create_bug"

    # Status change via verb (close/resolve/reopen/fix)
    sm = re.search(
        r"\b(close|closed|resolve|resolved|fix|fixed|reopen|reopened)\b",
        msg, re.IGNORECASE)
    if sm:
        pq.action_value = _STATUS_VERB_MAP[sm.group(1).lower()]
        return "set_status"

    # "mark as <status>" / "set status to <status>"
    if _STATUS_CHANGE_RE.search(msg):
        if pq.statuses:
            pq.action_value = pq.statuses[0]
            pq.statuses = []   # consumed as the write target, not a filter
            return "set_status"
        return "set_status"

    # Priority change verb + extracted priority.
    if _PRIORITY_CHANGE_RE.search(msg) and pq.priorities:
        pq.action_value = pq.priorities[0]
        pq.priorities = []
        return "set_priority"

    # Due date change.
    if _DUE_DATE_RE.search(msg):
        date_m = re.search(r"\b(\d{4}-\d{2}-\d{2})\b", msg)
        if date_m:
            pq.action_value = date_m.group(1)
        return "set_due_date"

    # Assign / unassign — last, because verbs like "give" overlap with reads.
    if _UNASSIGN_RE.search(msg) and pq.assignee_ids:
        return "unassign"
    if _ASSIGN_RE.search(msg) and pq.assignee_ids:
        # If the user is asking for a list ("show me bugs assigned to bob"),
        # don't treat it as a write.
        if re.search(
            r"\b(?:show|list|find|get|fetch|how\s+many|count|display|"
            r"give\s+me\s+(?:all|the))\b",
            msg, re.IGNORECASE,
        ):
            return None
        return "assign"

    return None


# ---------- Main entry --------------------------------------------------
def parse(message: str, ctx: Context, now: Optional[datetime] = None) -> ParsedQuery:
    """Public NLU entry point. Returns a ParsedQuery the executor consumes.

    `now` is injected by tests so time-window cases are deterministic; in
    production it defaults to "now" UTC.
    """
    msg = (message or "").strip()
    pq = ParsedQuery(raw_message=msg)
    if not msg:
        pq.intent = "empty"
        return pq

    # Greetings / thanks / help short-circuit before query parsing.
    if _GREETING_RE.match(msg) and len(msg.split()) <= 4:
        pq.intent = "greeting"
        return pq
    if _HELP_RE.search(msg) and len(msg.split()) <= 8:
        pq.intent = "help"
        return pq
    if _THANKS_RE.search(msg) and len(msg.split()) <= 5:
        pq.intent = "thanks"
        return pq

    # Yes / no answers to a previously staged action are intent
    # 'confirm_yes' / 'confirm_no'. The router consults memory.store to
    # see whether there's a pending plan for this user; if there is,
    # apply or discard it. If there isn't, the executor handles these as
    # a soft "nothing to confirm" reply.
    if _CONFIRM_YES_RE.match(msg):
        pq.intent = "confirm_yes"
        pq.confirmation = "yes"
        return pq
    if _CONFIRM_NO_RE.match(msg):
        pq.intent = "confirm_no"
        pq.confirmation = "no"
        return pq

    # Extract bug_id but don't short-circuit — we may yet detect an
    # action verb ("close bug 5") that wants this id, not bug_detail.
    bid = _extract_bug_id(msg)
    if bid is not None:
        pq.bug_id = bid

    # Filters --------------------------------------------------------------
    pq.statuses = _extract_statuses(msg)
    pq.priorities = _extract_priorities(msg)
    pq.environments = _extract_environments(msg)
    pq.text_search = _extract_text_search(msg)
    pq.time_window = _parse_time_window(msg, now=now)

    # Names — do this before stripping anything else from the message.
    name_phrases = _candidate_name_phrases(msg)
    seen_assignee_ids: set[int] = set()
    seen_reporter_ids: set[int] = set()
    for role, phrase in name_phrases:
        matches = _resolve_name(phrase, ctx)
        if not matches:
            pq.notes.append(f"No user matched '{phrase}'.")
            # Track which kind of name the user meant so the executor can
            # ask for clarification rather than silently dropping the
            # filter and returning every bug.
            if role == "assignee":
                pq.unresolved_assignee_names.append(phrase)
            elif role == "reporter":
                pq.unresolved_reporter_names.append(phrase)
            continue
        if len(matches) > 1:
            pq.ambiguous_names.append(
                (phrase, [disp for _uid, disp in matches]),
            )
            continue
        uid, disp = matches[0]
        if role == "assignee" and uid not in seen_assignee_ids:
            pq.assignee_ids.append(uid)
            pq.assignee_names.append(disp)
            seen_assignee_ids.add(uid)
        elif role == "reporter" and uid not in seen_reporter_ids:
            pq.reporter_ids.append(uid)
            pq.reporter_names.append(disp)
            seen_reporter_ids.add(uid)

    # Project — strict cue first, loose fallback only if nothing matched.
    seen_proj_ids: set[int] = set()
    for m in _PROJECT_CUE_RE.finditer(msg):
        cand = m.group(1).strip()
        for pid, pdisp in _resolve_project(cand, ctx):
            if pid not in seen_proj_ids:
                pq.project_ids.append(pid)
                pq.project_names.append(pdisp)
                seen_proj_ids.add(pid)
    if not pq.project_ids:
        for m in _PROJECT_LOOSE_CUE_RE.finditer(msg):
            cand = m.group(1).strip()
            for pid, pdisp in _resolve_project(cand, ctx):
                if pid not in seen_proj_ids:
                    pq.project_ids.append(pid)
                    pq.project_names.append(pdisp)
                    seen_proj_ids.add(pid)
    # Final fallback: any project NAME that literally appears in the message
    # as a whole word, even without "project" keyword. This catches phrases
    # like "bugs in apollo" or "export all bugs in beacon to excel". Walk
    # the actual project list (already loaded in ctx) so we never match
    # arbitrary words — only registered project names.
    if not pq.project_ids:
        for pid, norm_name, pdisp in ctx.projects:
            if not norm_name:
                continue
            # Word-boundary match, case-insensitive. Skip 1-char names —
            # they cause too much accidental matching.
            if len(norm_name) >= 2 and re.search(
                rf"\b{re.escape(norm_name)}\b", msg, re.IGNORECASE
            ):
                if pid not in seen_proj_ids:
                    pq.project_ids.append(pid)
                    pq.project_names.append(pdisp)
                    seen_proj_ids.add(pid)

    # Role queries ("list managers", "all admins") --------------------------
    role_match = _ROLE_CUE_RE.search(msg)
    if role_match:
        token = role_match.group(0).lower()
        if token.startswith("admin"):
            pq.role_filter = "admin"
        elif token.startswith("manager"):
            pq.role_filter = "manager"
        elif "regular user" in token:
            pq.role_filter = "user"

    # Action / output preference -------------------------------------------
    pq.wants_export = bool(_EXPORT_RE.search(msg))
    pq.wants_count = bool(_COUNT_RE.search(msg))

    # Pronoun bug-reference: record so the executor can fall back to
    # memory.store.last_bug_id when no explicit id was given.
    if pq.bug_id is None and _has_pronoun_bug_ref(msg):
        pq.used_pronoun_bug = True

    # ---- WRITE-INTENT DETECTION ------------------------------------------
    # If the user is asking Sleuth to DO something, classify it now.
    # _detect_action() inspects the verbs and the entity fields we just
    # populated, and returns a kind string ("assign", "add_comment", ...).
    # We map that to an "action_<kind>" intent the executor dispatches.
    action_kind = _detect_action(msg, pq)
    if action_kind is not None:
        pq.action_kind = action_kind
        pq.intent = "action_" + action_kind
        return pq

    # ---- READ-INTENT BUG-DETAIL SHORT-CIRCUIT ----------------------------
    # Restored from the original parser, but now AFTER action detection.
    # A short message that names a bug id (and isn't a write) is a
    # detail request. Long messages that happen to mention "#42" stay as
    # filter queries.
    if pq.bug_id is not None and len(msg.split()) <= 8 and not (
            pq.statuses or pq.priorities or pq.environments or pq.assignee_ids
            or pq.reporter_ids or pq.project_ids or pq.text_search
            or pq.time_window or pq.wants_export or pq.wants_count):
        pq.intent = "bug_detail"
        return pq

    # Final intent --------------------------------------------------------
    # Order matters: most specific → least.
    msg_lower = msg.lower()
    has_user_word = "user" in msg_lower
    has_list_verb = any(w in msg_lower for w in ("list", "show", "all",
                                                  "give", "who are"))
    # Role-only queries like "list all managers" or "show admins" don't
    # contain the word "user" but should still resolve to list_users.
    if (has_user_word or pq.role_filter is not None) and (
        has_list_verb or pq.role_filter is not None
    ):
        if not (pq.statuses or pq.priorities or pq.environments
                or pq.project_ids or pq.assignee_ids or pq.reporter_ids
                or pq.bug_id or pq.text_search or pq.time_window):
            pq.intent = "list_users"
            return pq

    if "project" in msg.lower() and (
        "list" in msg.lower() or "show" in msg.lower()
        or "what" in msg.lower() or "which" in msg.lower()
    ) and not (pq.statuses or pq.priorities or pq.environments
               or pq.assignee_ids or pq.reporter_ids):
        if "bug" not in msg.lower() and "issue" not in msg.lower():
            pq.intent = "list_projects"
            return pq

    if re.search(r"\b(stat|stats|statistics|summary|overview|dashboard|"
                 r"kpi|metrics|analytics)\b", msg, re.IGNORECASE):
        pq.intent = "stats"
        return pq

    if re.search(r"\brecent(\s+activity)?|audit\s+(?:log|trail)|"
                 r"what\s+happened|history\b",
                 msg, re.IGNORECASE):
        pq.intent = "recent_activity"
        return pq

    # If we got here and we have any bug-shape filter or list/count/export
    # intent, treat it as a bug query.
    if (pq.wants_export or pq.wants_count or _LIST_RE.search(msg)
            or pq.statuses or pq.priorities or pq.environments
            or pq.project_ids or pq.assignee_ids or pq.reporter_ids
            or pq.text_search or pq.time_window):
        pq.intent = "list_bugs"
        return pq

    # Last resort — try to interpret ANY name in the message as an
    # assignee filter. e.g. "John's bugs", "bugs of John".
    poss = re.search(r"\b([A-Z][a-zA-Z\-']+(?:\s+[A-Z][a-zA-Z\-']+)?)\b's\s+(?:bugs|issues|tickets)",
                     msg)
    if poss:
        cand = poss.group(1)
        matches = _resolve_name(cand, ctx)
        if len(matches) == 1:
            uid, disp = matches[0]
            pq.assignee_ids.append(uid)
            pq.assignee_names.append(disp)
            pq.intent = "list_bugs"
            return pq

    # "tell me about X" / "what are the statuses" / "what statuses exist"
    if re.search(r"^\s*(tell|explain|describe|what\s+(?:is|are|'s)|what's|whats)\b",
                 msg, re.IGNORECASE):
        pq.intent = "about"
        return pq
    # Bare "what <noun> ..." question — likely an about-style query.
    if re.search(
        r"\bwhat\s+(?:status|statuses|priority|priorities|environment|"
        r"environments|role|roles)\b",
        msg, re.IGNORECASE,
    ):
        pq.intent = "about"
        return pq

    pq.intent = "unknown"
    return pq


# ---------------------------------------------------------------------------
# Helpers shared with the executor for building human-readable summaries
# of a parsed query — useful in the chat reply ("Found 12 open bugs in PROD
# assigned to John") and in error messages ("Couldn't find a user named ...").
# ---------------------------------------------------------------------------
def describe_filters(pq: ParsedQuery) -> str:
    parts: list[str] = []
    if pq.statuses:
        # Re-collapse "open" if it matches the canonical open-set exactly.
        if set(pq.statuses) == set(OPEN_STATUSES):
            parts.append("open")
        else:
            parts.append(" or ".join(s.lower() for s in pq.statuses))
    if pq.priorities:
        parts.append(" or ".join(p.lower() for p in pq.priorities) + " priority")
    if pq.environments:
        parts.append("in " + " or ".join(pq.environments))
    if pq.project_names:
        parts.append("in project " + " or ".join(pq.project_names))
    if pq.assignee_names:
        parts.append("assigned to " + " or ".join(pq.assignee_names))
    if pq.reporter_names:
        parts.append("reported by " + " or ".join(pq.reporter_names))
    if pq.text_search:
        parts.append(f'matching "{pq.text_search}"')
    if pq.time_window and pq.time_window.label:
        parts.append(f"({pq.time_window.label})")
    return " ".join(parts)


__all__ = [
    "Context",
    "ParsedQuery",
    "TimeWindow",
    "parse",
    "describe_filters",
    "STATUSES_CANONICAL",
    "PRIORITIES_CANONICAL",
    "ENVIRONMENTS_CANONICAL",
    "OPEN_STATUSES",
]
