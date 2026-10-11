"""Small dependency-free helpers shared across the app."""
from __future__ import annotations

from datetime import datetime, timezone

# A leading one of these makes a spreadsheet treat the cell as a formula.
_FORMULA_TRIGGERS = ("=", "+", "-", "@", "\t", "\r", "\n")


def utcnow() -> datetime:
    """Second-resolution UTC now, so SQLite and Postgres round-trip identical values."""
    return datetime.now(timezone.utc).replace(microsecond=0)


def utcnow_precise() -> datetime:
    """UTC now with microseconds, for ordering rows written within the same second."""
    return datetime.now(timezone.utc)


def like_escape(needle: str) -> str:
    """Escape SQL LIKE wildcards (use with escape="\\") so user-typed % and _ match literally."""
    return needle.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


def defang_formula(s: str) -> str:
    """Prefix a quote when s starts with a formula trigger (raw or after leading spaces,
    which spreadsheets trim) - blocks CSV/Excel formula injection."""
    if not s:
        return s
    stripped = s.lstrip()
    if s[0] in _FORMULA_TRIGGERS or (stripped and stripped[0] in _FORMULA_TRIGGERS):
        return "'" + s
    return s

# Largest value an int4 primary key can hold; ids above it can never match a row.
MAX_PK_INT = 2**31 - 1
