"""Sleuth NLU — turn a free-form English message into a structured query or
action. Extracts entities (status, priority, env, names, project, bug id, time
window) and an intent into a dataclass the executor turns into SQL. Canonical
values come from live DB context, so renames reflect on the next request.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Optional

# Canonical enums, mirrored from app.schemas so parse-time stays decoupled from
# Pydantic; the executor re-validates against live schema constants.
_S_NEW = "New"
_S_IN_PROGRESS = "In Progress"
_S_RESOLVED = "Resolved"
_S_CLOSED = "Closed"
_S_REOPENED = "Reopened"
_S_NOT_A_BUG = "Not a Bug"
_S_RESOLVE_LATER = "Resolve Later"

STATUSES_CANONICAL = [
    _S_NEW, _S_IN_PROGRESS, _S_RESOLVED, _S_CLOSED, _S_REOPENED,
    _S_NOT_A_BUG, _S_RESOLVE_LATER,
]
PRIORITIES_CANONICAL = ["Low", "Medium", "High", "Critical"]
ENVIRONMENTS_CANONICAL = ["DEV", "UAT", "PROD"]

# "Open" = active work, matching the dashboard KPI: New / In Progress / Reopened.
OPEN_STATUSES = [_S_NEW, _S_IN_PROGRESS, _S_REOPENED]

# Synonym -> canonical.
_STATUS_SYNONYMS: dict[str, list[str]] = {
    "open":           OPEN_STATUSES,
    "active":         OPEN_STATUSES,
    "ongoing":        OPEN_STATUSES,
    "in-progress":    [_S_IN_PROGRESS],
    "in progress":    [_S_IN_PROGRESS],
    "wip":            [_S_IN_PROGRESS],
    "new":            [_S_NEW],
    "resolved":       [_S_RESOLVED],
    "fixed":          [_S_RESOLVED],
    "closed":         [_S_CLOSED],
    "done":           [_S_CLOSED, _S_RESOLVED],
    "reopened":       [_S_REOPENED],
    "reopen":         [_S_REOPENED],
    "not a bug":      [_S_NOT_A_BUG],
    "invalid":        [_S_NOT_A_BUG],
    "not-a-bug":      [_S_NOT_A_BUG],
    "resolve later":  [_S_RESOLVE_LATER],
    "deferred":       [_S_RESOLVE_LATER],
    "parked":         [_S_RESOLVE_LATER],
    # Bare "later" excluded so "I'll look at this later" can't inject a filter.
}

_PRIORITY_SYNONYMS: dict[str, str] = {
    "low":          "Low",
    "minor":        "Low",
    "trivial":      "Low",
    "medium":       "Medium",
    "med":          "Medium",
    "normal":       "Medium",
    "high":         "High",
    "important":    "High",
    "major":        "High",
    "critical":     "Critical",
    "crit":         "Critical",
    "blocker":      "Critical",
    "urgent":       "Critical",
    "showstopper":  "Critical",
    "p0":           "Critical",
    "p1":           "High",
    "p2":           "Medium",
    "p3":           "Low",
}

_ENVIRONMENT_SYNONYMS: dict[str, str] = {
    "dev":          "DEV",
    "develop":      "DEV",
    "development":  "DEV",
    "sandbox":      "DEV",
    "uat":          "UAT",
    "staging":      "UAT",
    "stage":        "UAT",
    "preprod":      "UAT",
    "pre-prod":     "UAT",
    "pre-production": "UAT",
    "qa":           "UAT",
    "testing":      "UAT",
    "prod":         "PROD",
    "production":   "PROD",
}

# Words that double as env names; kept out of the main table so the typo
# fallback can't grab them unqualified (matched only with an adjacent cue).
_AMBIGUOUS_ENV_SYNONYMS: dict[str, str] = {
    "test": "UAT",
    "live": "PROD",
    "local": "DEV",
}


# Stop-words for fuzzy name matching, so common words aren't read as user names.
_STOPWORDS = {
    "a", "an", "the", "this", "that", "these", "those", "and", "or", "but",
    "to", "of", "in", "on", "for", "by", "with", "without", "from", "at",
    "as", "is", "are", "was", "were", "be", "been", "being", "have", "has",
    "had", "do", "does", "did", "will", "would", "should", "could", "can",
    "may", "might", "must", "shall", "i", "you", "he", "she", "it", "we",
    "they", "me", "my", "your", "his", "her", "their", "our", "us", "them",
    "him", "any", "all", "some", "no", "not", "yes", "if", "then", "than",
    "so", "such", "just", "only", "very", "also", "too", "much", "many",
    "more", "most", "less", "least", "few", "every", "each", "both",
    # bug-tracker terms and verbs that appear in context but not in names
    "bug", "bugs", "issue", "issues", "ticket", "tickets", "list", "show",
    "give", "find", "get", "fetch", "pull", "create", "make", "export",
    "download", "send", "tell", "what", "where", "when", "how", "why",
    "who", "which", "please", "thanks", "thank", "now", "today", "yesterday",
    "open", "closed", "resolved", "active", "new", "reopened", "fixed",
    "high", "low", "medium", "critical", "priority", "status", "environment",
    "project", "projects", "assigned", "assignee", "assignees", "reporter",
    "reported", "filed", "raised", "owned", "owner", "against", "into", "under", "over", "above", "below", "between",
    "regarding", "about", "during", "before", "after", "around", "count", "total", "summary", "overview", "stats",
    "statistics", "dashboard", "report", "reports", "analytics", "kpi",
    "user", "users", "team", "member", "members", "name", "names",
    "excel", "xlsx", "csv", "spreadsheet", "sheet", "file", "files",
    "view", "details", "detail", "info", "information", "audit", "trail",
    "history", "log", "logs", "recent", "latest", "newest", "old", "oldest",
    "week", "weeks", "day", "days", "month", "months", "year", "years",
    "hour", "hours", "minute", "minutes",
    "dev", "uat", "prod", "production", "staging", "qa", "test", "testing",
    "kindly", "really", "actually", "maybe", "perhaps",
}


_TIME_RE = re.compile(
    r"\b("
    r"today|yesterday|"
    r"this\s+week|last\s+week|this\s+month|last\s+month|"
    r"this\s+quarter|last\s+quarter|this\s+year|last\s+year|"
    r"since\s+(monday|tuesday|wednesday|thursday|friday|saturday|sunday)|"
    r"past\s+(\d+)\s+(day|days|week|weeks|month|months|hour|hours)|"
    r"last\s+(\d+)\s+(day|days|week|weeks|month|months|hour|hours)|"
    r"in\s+the\s+last\s+(\d+)\s+(day|days|week|weeks|month|months|hour|hours)"
    r")\b",
    re.IGNORECASE,
)


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
# Cue word + number ("bug 42", "details of 42", "info on 7").
_BARE_ID_HINT = re.compile(
    r"\b(?:bug|issue|ticket|details?\s+of|info\s+(?:on|about))\s+(\d+)\b",
    re.IGNORECASE,
)

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

# Assign-intent verbs.
_ASSIGN_RE = re.compile(
    r"\b(?:assign|reassign|allocate|allot|hand(?:\s+over)?|"
    r"give|delegate|put)\b",
    re.IGNORECASE,
)
# Unassign-intent verbs.
_UNASSIGN_RE = re.compile(
    r"\b(?:unassign|deassign|remove|drop|take\s+(?:off|away)|"
    r"deallocate|pull\s+off)\b",
    re.IGNORECASE,
)
# Status-change verbs. The actual status value comes from _STATUS_SYNONYMS;
# this just signals that the user wants a write.
_STATUS_CHANGE_RE = re.compile(
    r"\b(?:close|closed|reopen|reopened|resolve|resolved|fix|fixed|"
    r"mark\s+(?:as|it)|set\s+(?:status|state)(?:\s+to)?|"
    r"change\s+status|update\s+status|status\s+to|"
    r"move\s+(?:it\s+|this\s+)?to\s+(?:status|state)?)\b",
    re.IGNORECASE,
)
# Priority-change verbs.
_PRIORITY_CHANGE_RE = re.compile(
    r"\b(?:set\s+(?:.*?\s+)?priority(?:\s+to)?|"
    r"set\s+severity(?:\s+to)?|"
    r"change\s+(?:.*?\s+)?priority|update\s+(?:.*?\s+)?priority|"
    r"make\s+(?:.*?\s+)?(?:critical|high|medium|low|urgent|blocker|p[0-3])|"
    r"raise\s+priority|escalate|de[\-\s]?escalate|downgrade|upgrade)\b",
    re.IGNORECASE,
)
_COMMENT_RE = re.compile(
    r"\b(?:comment(?:\s+on)?|leave\s+(?:a\s+)?comment|"
    r"add\s+(?:a\s+)?comment|reply|note|post\s+(?:a\s+)?(?:comment|note))\b",
    re.IGNORECASE,
)
_CREATE_BUG_RE = re.compile(
    r"\b(?:create|file|open|raise|add|new|log|report|register|submit)\s+"
    r"(?:a\s+|an\s+|the\s+)?(?:bug|issue|ticket|defect)\b",
    re.IGNORECASE,
)
_CREATE_PROJECT_RE = re.compile(
    r"\b(?:create|add|new|register|set\s+up)\s+"
    r"(?:a\s+|an\s+|the\s+)?project\b",
    re.IGNORECASE,
)
_DUE_DATE_RE = re.compile(
    r"\b(?:due\s+(?:date|by|on)?|set\s+(?:the\s+)?due(?:\s+date)?|"
    r"deadline|by\s+(?:next\s+)?(?:monday|tuesday|wednesday|thursday|"
    r"friday|saturday|sunday)|by\s+\d{4}-\d{2}-\d{2})\b",
    re.IGNORECASE,
)
# Pronouns referring to a previously-mentioned bug (resolved by the executor
# from conversation memory).
_PRONOUN_BUG_RE = re.compile(
    r"\b(?:it|this|that|that\s+(?:bug|issue|ticket)|"
    r"this\s+(?:bug|issue|ticket)|the\s+(?:bug|issue|ticket))\b",
    re.IGNORECASE,
)

# Require an explicit cue word ("in project X", "for project X") before
# accepting a project name, since free-form names are too easy to confuse
# with ordinary words.
_PROJECT_CUE_RE = re.compile(
    r"(?:in|for|under|from|on|of)\s+(?:the\s+)?project\s+([A-Za-z0-9_\-\s]+?)"
    r"(?=$|[,.;!?]|\s+(?:and|or|with|by|to|that|which|who|where|when|"
    r"assigned|reported|owned|status|priority|environment|created|updated))",
    re.IGNORECASE,
)
# Looser fallback for "project WebPortal" style. Less precise; only fires if
# the strict pattern misses and the candidate is a single token.
_PROJECT_LOOSE_CUE_RE = re.compile(
    r"\bproject\s+([a-z0-9_-]+)\b",
    re.IGNORECASE,
)

_ROLE_CUE_RE = re.compile(
    r"\b(admin|admins|administrator|administrators|"
    r"manager|managers|"
    r"regular\s+user|regular\s+users|users)\b",
    re.IGNORECASE,
)

# Unassigned / no-assignee filter.
_UNASSIGNED_RE = re.compile(
    r"\b(unassigned|"
    r"(?:with\s+)?no\s+(?:assignee|owner|one\s+assigned)|"
    r"(?:without|w/o)\s+(?:an?\s+)?(?:assignee|owner)|"
    r"nobody(?:'s)?\s+(?:assigned|on\s+it)|"
    r"no\s+one(?:'s)?\s+(?:assigned|on\s+it)|"
    r"orphan(?:ed)?\s+bugs)\b",
    re.IGNORECASE,
)

# Sort hint for long-running work. Doesn't change filters, just sort order.
_OLDEST_RE = re.compile(
    r"\b(oldest|longest(?:\s+(?:open|running))?|"
    r"stale|stalest|aging|oldest\s+first|"
    r"least\s+recent(?:ly)?(?:\s+updated)?)\b",
    re.IGNORECASE,
)

_NEWEST_RE = re.compile(
    r"\b(newest|most\s+recent|latest|freshest|"
    r"most\s+recent(?:ly)?(?:\s+updated)?|"
    r"newest\s+first)\b",
    re.IGNORECASE,
)

# First-person self-references; the executor resolves "me" to the current user.
_ME_ASSIGNEE_RE = re.compile(
    r"\b(?:my\s+(?:bug|bugs|issue|issues|ticket|tickets|stuff|work|"
    r"plate|backlog|queue|assignments?)|"
    r"assigned\s+to\s+me\b|"
    r"on\s+my\s+plate\b|"
    r"mine\b|"
    r"bugs?\s+i\s+(?:own|have|got)|"
    r"my\s+open\s+(?:bug|bugs|issues?|tickets?))\b",
    re.IGNORECASE,
)
_ME_REPORTER_RE = re.compile(
    r"\b(?:bugs?\s+i\s+(?:filed|reported|raised|opened|logged|created|submitted)|"
    r"reported\s+by\s+me|"
    r"raised\s+by\s+me|"
    r"filed\s+by\s+me|"
    r"i\s+(?:reported|filed|raised|opened|logged|created|submitted))\b",
    re.IGNORECASE,
)
# "assign … to me/myself" — paired with an assign verb so a bare "to me"
# elsewhere isn't misread as self-assign.
_TO_SELF_RE = re.compile(r"\b(?:to|for)\s+(?:me|myself)\b", re.IGNORECASE)


# Entities the parser produces. Plain dataclasses, no framework dependency.
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
    statuses: list[str] = field(default_factory=list)
    priorities: list[str] = field(default_factory=list)
    environments: list[str] = field(default_factory=list)
    # Empty = all types, so a generic "list items" stays type-agnostic;
    # explicit "tasks" / "bugs" scopes the query to that type.
    item_types: list[str] = field(default_factory=list)
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

    wants_export: bool = False    # excel
    wants_count: bool = False
    limit: int = 100              # default cap on rows shown in chat

    # Default off; when set, ANDed with the other filters.
    unassigned: bool = False
    sort_oldest: bool = False
    sort_newest: bool = False
    # "me" / "mine" — executor swaps for the actor's id.
    used_pronoun_me: bool = False
    me_role: Optional[str] = None    # "assignee" or "reporter"

    # Write-side fields, populated when the user wants Sleuth to act; the
    # executor reads these to build an ActionPlan.
    action_kind: Optional[str] = None   # "assign" | "unassign" | "set_status"
                                         # | "set_priority" | "set_environment"
                                         # | "set_due_date" | "add_comment"
                                         # | "create_bug" | "create_project"
    action_value: Optional[str] = None   # canonical new value for set_*
    action_comment: Optional[str] = None # body for add_comment
    action_title: Optional[str] = None   # title for create_bug
    action_description: Optional[str] = None
    confirmation: Optional[str] = None   # "yes" | "no" | None
    # Pronoun ref ("it", "that bug") — executor resolves via memory's last_bug_id.
    used_pronoun_bug: bool = False
    used_pronoun_user: bool = False

    raw_message: str = ""
    notes: list[str] = field(default_factory=list)
    # Names that matched more than one user; the executor asks for clarification.
    ambiguous_names: list[tuple[str, list[str]]] = field(default_factory=list)
    # Names that matched nobody; executor asks rather than dropping the filter
    # and returning every bug.
    unresolved_assignee_names: list[str] = field(default_factory=list)
    unresolved_reporter_names: list[str] = field(default_factory=list)


@dataclass
class Context:
    """Live data for resolving names to IDs, passed fresh per call so renames
    and deletions reflect immediately."""
    users: list[tuple[int, str, str, str]]      # (id, normalized_name, normalized_email_local, display_name)
    projects: list[tuple[int, str, str]]        # (id, normalized_name, display_name)
    user_role_map: dict[int, str] = field(default_factory=dict)


def _normalize(s: str) -> str:
    """Lowercase and collapse whitespace; internal punctuation is preserved."""
    return re.sub(r"\s+", " ", (s or "").strip().lower())


def _strip_punct(s: str) -> str:
    """Strip leading/trailing punctuation around a word for fuzzy matching."""
    return re.sub(r"[^\w\s\-']", "", s).strip()


def _tokenize(s: str) -> list[str]:
    """Lowercased, punctuation-stripped token list (caller filters stopwords)."""
    return re.findall(r"[a-zA-Z][a-zA-Z0-9\-']+", s.lower())


_WEEKDAY_TO_DOW = {
    "monday": 0, "tuesday": 1, "wednesday": 2, "thursday": 3,
    "friday": 4, "saturday": 5, "sunday": 6,
}


def _named_window(phrase: str, today_start: datetime, now: datetime) -> Optional[TimeWindow]:
    """Resolve a named time phrase (today, this week, last quarter, etc.), or None."""
    if phrase == "today":
        return TimeWindow(today_start, now, "today")
    if phrase == "yesterday":
        return TimeWindow(today_start - timedelta(days=1), today_start, "yesterday")
    if phrase == "this week":
        wk_start = today_start - timedelta(days=today_start.weekday())
        return TimeWindow(wk_start, now, "this week")
    if phrase == "last week":
        this_wk_start = today_start - timedelta(days=today_start.weekday())
        return TimeWindow(this_wk_start - timedelta(days=7), this_wk_start, "last week")
    if phrase == "this month":
        return TimeWindow(today_start.replace(day=1), now, "this month")
    if phrase == "last month":
        m_start = today_start.replace(day=1)
        last_m_start = (m_start - timedelta(days=1)).replace(day=1)
        return TimeWindow(last_m_start, m_start, "last month")
    if phrase == "this quarter":
        q_start_month = ((today_start.month - 1) // 3) * 3 + 1
        return TimeWindow(today_start.replace(month=q_start_month, day=1), now, "this quarter")
    if phrase == "last quarter":
        q_start_month = ((today_start.month - 1) // 3) * 3 + 1
        this_q_start = today_start.replace(month=q_start_month, day=1)
        prev_day = this_q_start - timedelta(days=1)
        prev_q_month = ((prev_day.month - 1) // 3) * 3 + 1
        return TimeWindow(prev_day.replace(month=prev_q_month, day=1), this_q_start, "last quarter")
    if phrase == "this year":
        return TimeWindow(today_start.replace(month=1, day=1), now, "this year")
    if phrase == "last year":
        this_y_start = today_start.replace(month=1, day=1)
        return TimeWindow(this_y_start.replace(year=this_y_start.year - 1), this_y_start, "last year")
    return None


def _since_weekday_window(weekday_name: str, today_start: datetime, now: datetime) -> TimeWindow:
    """Resolve "since Monday" relative to today; if today IS Monday, look back a
    full week rather than a zero-length window."""
    target_dow = _WEEKDAY_TO_DOW[weekday_name.lower()]
    delta_days = (today_start.weekday() - target_dow) % 7
    if delta_days == 0:
        delta_days = 7
    anchor = today_start - timedelta(days=delta_days)
    return TimeWindow(anchor, now, f"since {weekday_name.lower()}")


def _first_int_match(matches: tuple) -> Optional[int]:
    for grp in matches:
        if grp:
            try:
                return int(grp)
            except ValueError:
                continue
    return None


def _first_str_match(matches: tuple) -> Optional[str]:
    for grp in matches:
        if grp:
            return grp.lower()
    return None


def _relative_window(qty: int, unit: str, now: datetime) -> Optional[TimeWindow]:
    """Map "past N days/weeks/months/hours" or "last N ..." to a window."""
    if unit.startswith("hour"):
        delta = timedelta(hours=qty)
    elif unit.startswith("day"):
        delta = timedelta(days=qty)
    elif unit.startswith("week"):
        delta = timedelta(weeks=qty)
    elif unit.startswith("month"):
        delta = timedelta(days=30 * qty)
    else:
        return None
    return TimeWindow(now - delta, now, f"last {qty} {unit}")


def _parse_time_window(message: str, now: Optional[datetime] = None) -> Optional[TimeWindow]:
    """Extract a time-window hint from the message, or return None."""
    now = now or datetime.now(timezone.utc)
    today_start = now.replace(hour=0, minute=0, second=0, microsecond=0)
    m = _TIME_RE.search(message)
    if not m:
        return None
    # Collapse internal whitespace so the exact-equality lookup still hits.
    phrase = re.sub(r"\s+", " ", m.group(0).lower().strip())

    named = _named_window(phrase, today_start, now)
    if named is not None:
        return named

    weekday_match = m.group(2)
    if weekday_match:
        return _since_weekday_window(weekday_match, today_start, now)

    # Group indices: 2=since-weekday, 3/4=past N, 5/6=last N, 7/8=in-the-last N.
    qty = _first_int_match((m.group(3), m.group(5), m.group(7)))
    unit = _first_str_match((m.group(4), m.group(6), m.group(8)))
    if qty is not None and unit:
        return _relative_window(qty, unit, now)
    return None  # pragma: no cover - unreachable while every _TIME_RE branch is handled above


def _extract_statuses(text: str) -> list[str]:
    """Find every status synonym. Order-preserving, deduped."""
    out: list[str] = []
    norm = " " + text.lower() + " "
    # Longest synonyms first so multi-word phrases aren't fragmented.
    for syn in sorted(_STATUS_SYNONYMS.keys(), key=len, reverse=True):
        # Char-class boundary, not \b, since a synonym may contain spaces.
        pattern = re.compile(rf"(?<![\w-]){re.escape(syn)}(?![\w-])", re.IGNORECASE)
        if pattern.search(norm):
            for canon in _STATUS_SYNONYMS[syn]:
                if canon not in out:
                    out.append(canon)
    return out


def _append_unique(out: list, value) -> None:
    if value not in out:
        out.append(value)


def _typo_fallback(text: str, synonyms: dict, out: list[str]) -> None:
    """Token-level fuzzy match, run only when the exact extractor found nothing."""
    if out:
        return
    for tok in _tokenize(text):
        hit = _typo_match(tok, synonyms)
        if hit:
            _append_unique(out, synonyms[hit])


def _extract_priorities(text: str) -> list[str]:
    out: list[str] = []
    for syn, canon in _PRIORITY_SYNONYMS.items():
        # Accept simple plurals ("blockers", "criticals").
        if re.search(rf"\b{re.escape(syn)}s?\b", text, re.IGNORECASE):
            _append_unique(out, canon)
    _typo_fallback(text, _PRIORITY_SYNONYMS, out)
    return out


def _typo_match(token: str, candidates: dict, min_len: int = 4) -> Optional[str]:
    """Return the best difflib match for `token` in `candidates`, or None.

    Short tokens (< min_len) are skipped to avoid noise ("low" -> "log"); the
    0.82 cutoff admits typos like "ctitical" -> "critical".
    """
    import difflib
    if len(token) < min_len:
        return None
    matches = difflib.get_close_matches(
        token.lower(), candidates.keys(), n=1, cutoff=0.82,
    )
    if not matches:
        return None
    return matches[0]


def _extract_environments(text: str) -> list[str]:
    out: list[str] = []
    for syn, canon in _ENVIRONMENT_SYNONYMS.items():
        if re.search(rf"\b{re.escape(syn)}\b", text, re.IGNORECASE):
            _append_unique(out, canon)
    # Ambiguous words (test/live/local) only match with an adjacent env cue,
    # so bare prose ("test the export", "go live") is left alone.
    for syn, canon in _AMBIGUOUS_ENV_SYNONYMS.items():
        if canon in out:
            continue
        pat = (rf"\b(?:in|on|to|env|environment)\s+{syn}\b"
               rf"|\b{syn}\s+(?:env|environment|bug|bugs|issue|issues|ticket|"
               rf"tickets|item|items|requirement|requirements|task|tasks|"
               rf"defect|defects)\b")
        if re.search(pat, text, re.IGNORECASE):
            _append_unique(out, canon)
    # Typo fallback runs only when the exact extractor found nothing.
    _typo_fallback(text, _ENVIRONMENT_SYNONYMS, out)
    return out


def _candidate_name_phrases(message: str) -> list[tuple[str, str]]:
    """Pull name-like phrases as (role_hint, phrase) tuples, where role_hint is
    'assignee', 'reporter', or '' (e.g. "assigned to John", "filed by Bob")."""
    out: list[tuple[str, str]] = []

    # Assignee cues. The negated char class and lookahead exclude parentheticals
    # and export/download suffixes so the UI's "(export to excel)" isn't absorbed.
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
        # Action verbs ("assign bug 5 to alice"); the optional pronoun group lets
        # memory cases extract the name without first resolving the bug.
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

    # Reporter cues.
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
    """Match a name phrase against the user list, most to least specific: exact
    name, email local-part, prefix, last name, first name. Returns (id, display)
    pairs; empty = no match, >1 = caller asks for clarification."""
    norm = _normalize(_strip_punct(phrase))
    if not norm:
        return []

    # Strip title prefixes so "Mr. X" matches user "X".
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

    # Dedupe looser matches by id instead of short-circuiting on the first tier,
    # so genuine ambiguity (len > 1) still reaches the caller.
    found: dict[int, str] = {}

    def _add(matches) -> None:
        for uid, disp in matches:
            found.setdefault(uid, disp)

    _add((uid, disp) for (uid, _n, email, disp) in ctx.users if email and email == norm)  # 2
    _add((uid, disp) for (uid, n, _e, disp) in ctx.users                                  # 3
         if n.startswith(norm + " ") or n == norm)
    if " " not in norm:
        _add((uid, disp) for (uid, n, _e, disp) in ctx.users if n.split()[-1] == norm)   # 4
        _add((uid, disp) for (uid, n, _e, disp) in ctx.users if n.split()[0] == norm)    # 5

    return list(found.items())


def _resolve_project(phrase: str, ctx: Context) -> list[tuple[int, str]]:
    """Match a project name phrase (exact, then prefix). No fuzzy matching:
    short project names would clash with ordinary speech."""
    norm = _normalize(_strip_punct(phrase))
    if not norm:
        return []
    exact = [(pid, disp) for (pid, n, disp) in ctx.projects if n == norm]
    if exact:
        return exact
    prefix = [(pid, disp) for (pid, n, disp) in ctx.projects if n.startswith(norm)]
    return prefix


# Postgres int4 max; anything higher would DataError at the DB layer.
_MAX_BUG_ID = 2_147_483_647


def _coerce_bug_id(raw: str) -> Optional[int]:
    """Parse a bug id, rejecting zero, negatives, and values over int4 max."""
    try:
        n = int(raw)
    except (TypeError, ValueError):
        return None
    if n < 1 or n > _MAX_BUG_ID:
        return None
    return n


def _extract_bug_id(message: str) -> Optional[int]:
    m = _BUG_ID_RE.search(message)
    if m:
        n = _coerce_bug_id(m.group(1))
        if n is not None:
            return n
    m = _BARE_ID_HINT.search(message)
    if m:
        n = _coerce_bug_id(m.group(1))
        if n is not None:
            return n
    # Accept a message that is nothing but a bare number (optionally prefixed #).
    s = message.strip().lstrip("#")
    if s.isdigit():
        return _coerce_bug_id(s)
    return None


# Quoted strings become a free-text search clause, e.g. bugs about "login crash".
_QUOTED_RE = re.compile(r'"([^"]{2,})"')


# Bare free-text after a topic cue ("bugs about login crash"). Anchored to \S.*
# to keep matching linear and avoid backtracking on \s+(.+).
_FREE_TEXT_CUE_RE = re.compile(
    r"\b(?:about|regarding|mentioning|containing|concerning|related\s+to)\s+(\S.*)$",
    re.IGNORECASE,
)


def _extract_text_search(message: str) -> Optional[str]:
    m = _QUOTED_RE.search(message)
    if m:
        return m.group(1).strip() or None
    m = _FREE_TEXT_CUE_RE.search(message)
    if m:
        # Strip trailing filter clauses ("in project X", "with priority Y").
        term = _strip_create_bug_tail(m.group(1).strip()).strip(" .?!,")
        # A short term that's entirely a status/priority word is a filter, not
        # a search topic — let the enum extractors handle it.
        only_enum = bool(_extract_statuses(term) or _extract_priorities(term))
        if term and not (only_enum and len(term.split()) <= 2):
            return term or None
    return None


# Maps status verbs (including past tense) to their canonical status value.
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

# Extract the comment body from phrases like "comment on bug 5: this is fixed"
# or "leave a note on bug 5 — try again".
_COMMENT_BODY_RE = re.compile(
    r"(?:comment|note|reply)(?:\s+on\s+\S+|\s+to\s+\S+|"
    r"\s+about\s+\S+|\s+(?:#|bug\s+#?|issue\s+#?)\d+|\s+saying|\s+with)?"
    r"\s*[:\-—]\s*(.+)$",
    re.IGNORECASE | re.DOTALL,
)
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


_STATUS_VERB_RE = re.compile(
    r"\b(close|closed|resolve|resolved|fix|fixed|reopen|reopened)\b",
    re.IGNORECASE,
)
_LIST_VERB_RE = re.compile(
    r"\b(?:show|list|find|get|fetch|how\s+many|count|display|"
    r"give\s+me\s+(?:all|the))\b",
    re.IGNORECASE,
)
_ISO_DATE_RE = re.compile(r"\b(\d{4}-\d{2}-\d{2})\b")


def _action_add_comment(msg: str, pq: ParsedQuery) -> Optional[str]:
    if not _COMMENT_RE.search(msg):
        return None
    # "show comments on #5" is a read, not a write.
    if _LIST_VERB_RE.search(msg):
        return None
    # A comment needs a specific target — don't stage a write with no bug id.
    # (Comments are never bulk.)
    if not (pq.bug_id or pq.used_pronoun_bug):
        return None
    m = _COMMENT_BODY_RE.search(msg)
    if m:
        body = m.group(1).strip().strip("\"'")
        if body:
            pq.action_comment = body
    return "add_comment"


def _action_create_project(msg: str, pq: ParsedQuery) -> Optional[str]:
    if not _CREATE_PROJECT_RE.search(msg):
        return None
    m = _CREATE_PROJECT_NAME_RE.search(msg)
    if m:
        pq.action_title = m.group(1).strip()
    return "create_project"


_TAIL_MARKERS = (
    " in project ", " for project ", " under project ",
    " in the project ", " for the project ", " under the project ",
    " with priority ", " having priority ", " with status ",
    " having status ", " with environment ", " in environment ",
    " assigned to ", " assign to ",
    " reported by ", " created by ", " filed by ", " raised by ",
    " opened by ", " owned by ",
    " for ",
)


def _strip_create_bug_tail(title: str) -> str:
    """Remove trailing filter clauses so the bare-title capture doesn't absorb
    them. Literal substring search, not regex, to avoid backtracking."""
    if not title:
        return title
    lower = title.lower()
    cut = len(title)
    for marker in _TAIL_MARKERS:
        idx = lower.find(marker)
        if 0 <= idx < cut:
            cut = idx
    return title[:cut].rstrip()


def _action_create_bug(msg: str, pq: ParsedQuery) -> Optional[str]:
    if not _CREATE_BUG_RE.search(msg):
        return None
    m = _CREATE_BUG_TITLE_RE.search(msg)
    if m:
        pq.action_title = m.group(1).strip()
        return "create_bug"
    m2 = _CREATE_BUG_BARE_RE.search(msg)
    if m2:
        title = _strip_create_bug_tail(m2.group(1).strip())
        if title:
            pq.action_title = title
    return "create_bug"


def _action_set_status(msg: str, pq: ParsedQuery) -> Optional[str]:
    # "list resolved bugs" is a filter, not a write; bail on list/count verbs.
    # Bulk writes ("close all bugs") carry no list verb, so they still pass.
    if _LIST_VERB_RE.search(msg):
        return None
    sm = _STATUS_VERB_RE.search(msg)
    if sm:
        pq.action_value = _STATUS_VERB_MAP[sm.group(1).lower()]
        return "set_status"
    if not _STATUS_CHANGE_RE.search(msg):
        return None
    if pq.statuses:
        pq.action_value = pq.statuses[0]
        pq.statuses = []   # consumed as write target, not a read filter
    return "set_status"


def _action_set_priority(msg: str, pq: ParsedQuery) -> Optional[str]:
    if not (_PRIORITY_CHANGE_RE.search(msg) and pq.priorities):
        return None
    pq.action_value = pq.priorities[0]
    pq.priorities = []
    return "set_priority"


def _action_set_due_date(msg: str, pq: ParsedQuery) -> Optional[str]:
    if not _DUE_DATE_RE.search(msg):
        return None
    date_m = _ISO_DATE_RE.search(msg)
    if date_m:
        pq.action_value = date_m.group(1)
    return "set_due_date"


def _action_assign(msg: str, pq: ParsedQuery) -> Optional[str]:
    # "assign … to me": the executor resolves the current user after parse, so
    # accept it here even though no assignee id is set yet.
    self_assign = pq.used_pronoun_me and pq.me_role == "assignee"
    if _UNASSIGN_RE.search(msg) and pq.assignee_ids:
        return "unassign"
    if not (_ASSIGN_RE.search(msg) and (pq.assignee_ids or self_assign)):
        return None
    if _LIST_VERB_RE.search(msg):  # "show bugs assigned to bob" is a read
        return None
    return "assign"


def _detect_action(msg: str, pq: ParsedQuery) -> Optional[str]:
    """Return the write-intent kind (assign, set_status, create_bug, ...) or None,
    mutating pq with the action value. Order matters: create_project before
    create_bug (overlapping verbs); assign/unassign last ("give" overlaps reads)."""
    for detector in (
        _action_add_comment,
        _action_create_project,
        _action_create_bug,
        _action_set_status,
        _action_set_priority,
        _action_set_due_date,
        _action_assign,
    ):
        kind = detector(msg, pq)
        if kind is not None:
            return kind
    return None


_STATS_RE = re.compile(
    r"\b(stat|stats|statistics|summary|overview|dashboard|kpi|metrics|analytics)\b",
    re.IGNORECASE,
)
# Stronger signal than stats, so it wins precedence in the classifier.
_REPORT_RE = re.compile(
    r"\b(report|reporting|throughput|pending\s+(?:items|bugs|snapshot)|"
    r"aging\s+report|breakdown\s+(?:by|of)|time\s+to\s+resolution|"
    r"who\s+(?:resolved|closed|fixed|solved)|who\s+resolved\s+how\s+many|"
    r"distribution\s+(?:by|of))\b",
    re.IGNORECASE,
)
# Keyword to report-key mapping. Checked in order; first match wins.
# Unmatched messages fall back to the item_detail export (full bug/task list).
_REPORT_KEY_KEYWORDS: list[tuple[str, str]] = [
    ("throughput",            r"\bthroughput\b"),
    ("throughput",            r"\bwho\s+(?:resolved|closed|fixed|solved)\b"),
    ("throughput",            r"\bresolved\s+(?:by|how\s+many)\b"),
    ("throughput",            r"\bsolved\s+(?:how\s+many|last\s+week)\b"),
    ("pending_snapshot",      r"\bpending\b"),
    ("pending_snapshot",      r"\bstill\s+open\b"),
    ("pending_snapshot",      r"\bnot\s+(?:yet\s+)?(?:resolved|closed|fixed|done)\b"),
    ("aging",                 r"\baging\b"),
    ("aging",                 r"\boldest\s+(?:bug|item|tickets|open)\b"),
    ("aging",                 r"\blongest\s+(?:open|running)\b"),
    ("time_to_resolution",    r"\btime\s+to\s+resolution\b"),
    ("time_to_resolution",    r"\bhow\s+long\s+(?:do|did)\s+(?:bugs?|issues?|items?)\s+take\b"),
    ("status_distribution",   r"\bdistribution\s+(?:by\s+)?status\b"),
    ("status_distribution",   r"\bby\s+status\b"),
    ("priority_distribution", r"\bdistribution\s+(?:by\s+)?priority\b"),
    ("priority_distribution", r"\bby\s+priority\b"),
    ("project_breakdown",     r"\b(?:breakdown\s+(?:by\s+)?project|by\s+project)\b"),
    ("timeline",              r"\b(?:created\s+vs\s+resolved|timeline|trend(?:line)?)\b"),
]


def pick_report_key(message: str) -> Optional[str]:
    """Return the best-matching report key, or None to fall back to item_detail.
    Exposed so the executor can reuse the same mapping."""
    if not message:
        return None
    for key, pat in _REPORT_KEY_KEYWORDS:
        if re.search(pat, message, re.IGNORECASE):
            return key
    return None

# \b wraps the whole alternation so "prehistory" doesn't accidentally match.
_RECENT_RE = re.compile(
    r"\b(?:recent(?:\s+activity)?|audit\s+(?:log|trail)|what\s+happened|history)\b",
    re.IGNORECASE,
)
_ABOUT_LEAD_RE = re.compile(
    r"^\s*(tell|explain|describe|what\s+(?:is|are|'s)|what's|whats)\b",
    re.IGNORECASE,
)
_ABOUT_BARE_RE = re.compile(
    r"\bwhat\s+(?:status|statuses|priority|priorities|environment|"
    r"environments|role|roles)\b",
    re.IGNORECASE,
)
_POSSESSIVE_NAME_RE = re.compile(
    r"\b([A-Z][a-zA-Z\-']+(?:\s+[A-Z][a-zA-Z\-']+)?)\b's\s+(?:bugs|issues|tickets)",
)


def _classify_short_intent(msg: str, pq: ParsedQuery) -> bool:
    """Handle greetings, thanks, help, and confirmation short-circuits.
    Mutates pq.intent and returns True if a short intent was matched."""
    if _GREETING_RE.match(msg) and len(msg.split()) <= 4:
        pq.intent = "greeting"
        return True
    if _HELP_RE.search(msg) and len(msg.split()) <= 8:
        pq.intent = "help"
        return True
    if _THANKS_RE.search(msg) and len(msg.split()) <= 5:
        pq.intent = "thanks"
        return True
    if _CONFIRM_YES_RE.match(msg):
        pq.intent = "confirm_yes"
        pq.confirmation = "yes"
        return True
    if _CONFIRM_NO_RE.match(msg):
        pq.intent = "confirm_no"
        pq.confirmation = "no"
        return True
    return False


def _populate_filters(msg: str, pq: ParsedQuery, now: Optional[datetime]) -> None:
    bid = _extract_bug_id(msg)
    if bid is not None:
        pq.bug_id = bid
    pq.statuses = _extract_statuses(msg)
    pq.priorities = _extract_priorities(msg)
    pq.environments = _extract_environments(msg)
    pq.text_search = _extract_text_search(msg)
    pq.time_window = _parse_time_window(msg, now=now)


def _record_name_match(role: str, uid: int, disp: str, pq: ParsedQuery,
                       seen_a: set[int], seen_r: set[int]) -> None:
    if role == "assignee" and uid not in seen_a:
        pq.assignee_ids.append(uid)
        pq.assignee_names.append(disp)
        seen_a.add(uid)
    elif role == "reporter" and uid not in seen_r:
        pq.reporter_ids.append(uid)
        pq.reporter_names.append(disp)
        seen_r.add(uid)


def _record_unresolved_name(role: str, phrase: str, pq: ParsedQuery) -> None:
    pq.notes.append(f"No user matched '{phrase}'.")
    if role == "assignee":
        pq.unresolved_assignee_names.append(phrase)
    elif role == "reporter":
        pq.unresolved_reporter_names.append(phrase)


def _populate_names(msg: str, pq: ParsedQuery, ctx: Context) -> None:
    seen_assignee_ids: set[int] = set()
    seen_reporter_ids: set[int] = set()
    for role, phrase in _candidate_name_phrases(msg):
        matches = _resolve_name(phrase, ctx)
        if not matches:
            _record_unresolved_name(role, phrase, pq)
            continue
        if len(matches) > 1:
            pq.ambiguous_names.append(
                (phrase, [disp for _uid, disp in matches]),
            )
            continue
        uid, disp = matches[0]
        _record_name_match(role, uid, disp, pq, seen_assignee_ids, seen_reporter_ids)


def _add_resolved_projects(cand: str, pq: ParsedQuery, ctx: Context, seen: set[int]) -> None:
    for pid, pdisp in _resolve_project(cand, ctx):
        if pid not in seen:
            pq.project_ids.append(pid)
            pq.project_names.append(pdisp)
            seen.add(pid)


# Project names colliding with common words ("Open", "Test") need an explicit
# cue, so "show me open bugs" can't silently scope to a project named "Open".
_PROJECT_NAME_VOCAB = (
    _STOPWORDS
    | set(_STATUS_SYNONYMS)
    | set(_PRIORITY_SYNONYMS)
    | set(_ENVIRONMENT_SYNONYMS)
)


def _populate_projects(msg: str, pq: ParsedQuery, ctx: Context) -> None:
    seen: set[int] = set()
    for m in _PROJECT_CUE_RE.finditer(msg):
        _add_resolved_projects(m.group(1).strip(), pq, ctx, seen)
    if not pq.project_ids:
        for m in _PROJECT_LOOSE_CUE_RE.finditer(msg):
            _add_resolved_projects(m.group(1).strip(), pq, ctx, seen)
    # Last resort: bare project-name match. Skip single-char names.
    if not pq.project_ids:
        for pid, norm_name, pdisp in ctx.projects:
            if (
                norm_name
                and len(norm_name) >= 2
                and norm_name not in _PROJECT_NAME_VOCAB
                and re.search(rf"\b{re.escape(norm_name)}\b", msg, re.IGNORECASE)
                and pid not in seen
            ):
                pq.project_ids.append(pid)
                pq.project_names.append(pdisp)
                seen.add(pid)


def _populate_role_filter(msg: str, pq: ParsedQuery) -> None:
    role_match = _ROLE_CUE_RE.search(msg)
    if not role_match:
        return
    token = role_match.group(0).lower()
    if token.startswith("admin"):
        pq.role_filter = "admin"
    elif token.startswith("manager"):
        pq.role_filter = "manager"
    elif "regular user" in token:
        pq.role_filter = "user"


# Work-item type nouns to canonical type. "defect" is a Bug synonym.
# Word-boundary matching keeps "debug" and "multitask" from scoping the query.
_ITEM_TYPE_KEYWORDS: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("Bug",         ("bug", "bugs", "defect", "defects")),
    ("Requirement", ("requirement", "requirements")),
    ("Task",        ("task", "tasks")),
)


def _populate_item_types(msg: str, pq: ParsedQuery) -> None:
    """Scope to the work-item types the user named, leaving item_types empty for
    generic requests ("list items") so they stay type-agnostic."""
    low = msg.lower()
    for canonical, needles in _ITEM_TYPE_KEYWORDS:
        if any(re.search(rf"\b{n}\b", low) for n in needles) and canonical not in pq.item_types:
            pq.item_types.append(canonical)


def _populate_output_prefs(msg: str, pq: ParsedQuery) -> None:
    pq.wants_export = bool(_EXPORT_RE.search(msg))
    pq.wants_count = bool(_COUNT_RE.search(msg))
    if _UNASSIGNED_RE.search(msg):
        pq.unassigned = True
    if _OLDEST_RE.search(msg):
        pq.sort_oldest = True
    if _NEWEST_RE.search(msg):
        pq.sort_newest = True
    if _ME_REPORTER_RE.search(msg):
        pq.used_pronoun_me = True
        pq.me_role = "reporter"
    elif _ME_ASSIGNEE_RE.search(msg) or (
        _ASSIGN_RE.search(msg) and _TO_SELF_RE.search(msg)  # "assign bug 5 to me"
    ):
        pq.used_pronoun_me = True
        pq.me_role = "assignee"
    if pq.bug_id is None and _has_pronoun_bug_ref(msg):
        pq.used_pronoun_bug = True


def _is_bare_bug_detail(msg: str, pq: ParsedQuery) -> bool:
    """A short message that names a bug id with no other filters is a detail
    request. Longer messages mentioning "#42" in passing stay as filter queries."""
    if pq.bug_id is None or len(msg.split()) > 8:
        return False
    return not (
        pq.statuses or pq.priorities or pq.environments or pq.assignee_ids
        or pq.reporter_ids or pq.project_ids or pq.text_search
        or pq.time_window or pq.wants_export or pq.wants_count
    )


def _has_any_bug_filter(pq: ParsedQuery) -> bool:
    return bool(
        pq.statuses or pq.priorities or pq.environments
        or pq.project_ids or pq.assignee_ids or pq.reporter_ids
        or pq.text_search or pq.time_window
        or pq.unassigned or pq.sort_oldest or pq.sort_newest
        or pq.used_pronoun_me
    )


def _is_list_users(msg_lower: str, pq: ParsedQuery) -> bool:
    has_user_word = "user" in msg_lower
    has_list_verb = any(w in msg_lower for w in ("list", "show", "all", "give", "who are"))
    head_matches = (has_user_word or pq.role_filter is not None) and (
        has_list_verb or pq.role_filter is not None
    )
    if not head_matches:
        return False
    return not (
        pq.statuses or pq.priorities or pq.environments
        or pq.project_ids or pq.assignee_ids or pq.reporter_ids
        or pq.bug_id or pq.text_search or pq.time_window
    )


def _is_list_projects(msg_lower: str, pq: ParsedQuery) -> bool:
    if "project" not in msg_lower:
        return False
    has_verb = any(w in msg_lower for w in ("list", "show", "what", "which"))
    if not has_verb:
        return False
    has_blocker = (pq.statuses or pq.priorities or pq.environments
                   or pq.assignee_ids or pq.reporter_ids
                   or "bug" in msg_lower or "issue" in msg_lower)
    return not has_blocker


def _try_possessive_assignee(msg: str, pq: ParsedQuery, ctx: Context) -> bool:
    """Last-resort possessive match: "John's bugs" or "bugs of John"."""
    poss = _POSSESSIVE_NAME_RE.search(msg)
    if not poss:
        return False
    matches = _resolve_name(poss.group(1), ctx)
    if len(matches) != 1:
        return False
    uid, disp = matches[0]
    pq.assignee_ids.append(uid)
    pq.assignee_names.append(disp)
    return True


def _classify_final_intent(msg: str, pq: ParsedQuery, ctx: Context) -> str:
    """Classify a non-action message and return the intent string."""
    msg_lower = msg.lower()
    if _is_list_users(msg_lower, pq):
        return "list_users"
    if _is_list_projects(msg_lower, pq):
        return "list_projects"
    # Report beats stats and list_bugs — it's a more specific signal. parse()
    # already short-circuits on this via _REPORT_RE, but this function is also
    # unit-tested as an independent classifier, so it must not depend on that.
    if _REPORT_RE.search(msg):
        return "report"
    if _STATS_RE.search(msg):
        return "stats"
    if _RECENT_RE.search(msg):
        return "recent_activity"
    if pq.wants_export or pq.wants_count or _LIST_RE.search(msg) or _has_any_bug_filter(pq):
        return "list_bugs"
    if _try_possessive_assignee(msg, pq, ctx):
        return "list_bugs"
    if _ABOUT_LEAD_RE.search(msg) or _ABOUT_BARE_RE.search(msg):
        return "about"
    return "unknown"


def parse(message: str, ctx: Context, now: Optional[datetime] = None) -> ParsedQuery:
    """Parse a chat message and return a ParsedQuery for the executor.

    `now` can be injected by tests to make time-window assertions deterministic;
    in production it defaults to UTC now.
    """
    msg = (message or "").strip()
    pq = ParsedQuery(raw_message=msg)
    if not msg:
        pq.intent = "empty"
        return pq

    if _classify_short_intent(msg, pq):
        return pq

    _populate_filters(msg, pq, now)
    _populate_names(msg, pq, ctx)
    _populate_projects(msg, pq, ctx)
    _populate_role_filter(msg, pq)
    _populate_item_types(msg, pq)
    _populate_output_prefs(msg, pq)

    # Reports short-circuit before _detect_action, otherwise "report of who
    # resolved how many bugs last week" gets consumed by the status-change
    # detector (verb "resolved") and misclassified as action_set_status.
    if _REPORT_RE.search(msg):
        pq.intent = "report"
        return pq

    action_kind = _detect_action(msg, pq)
    if action_kind is not None:
        pq.action_kind = action_kind
        pq.intent = "action_" + action_kind
        return pq

    if _is_bare_bug_detail(msg, pq):
        pq.intent = "bug_detail"
        return pq

    pq.intent = _classify_final_intent(msg, pq, ctx)
    return pq


# Helpers shared with the executor for building human-readable filter summaries
# used in chat replies and error messages.
def describe_filters(pq: ParsedQuery) -> str:
    parts: list[str] = []
    if pq.statuses:
        # Collapse back to "open" if the statuses match the canonical open set.
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
    if pq.unassigned:
        parts.append("with no assignee")
    if pq.text_search:
        parts.append(f'matching "{pq.text_search}"')
    if pq.time_window and pq.time_window.label:
        parts.append(f"({pq.time_window.label})")
    if pq.sort_oldest:
        parts.append("(oldest first)")
    return " ".join(parts)


__all__ = [
    "Context",
    "ParsedQuery",
    "TimeWindow",
    "parse",
    "describe_filters",
    "pick_report_key",
    "STATUSES_CANONICAL",
    "PRIORITIES_CANONICAL",
    "ENVIRONMENTS_CANONICAL",
    "OPEN_STATUSES",
]
