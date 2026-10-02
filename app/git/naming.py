"""Deterministic branch-name generation (backend-owned, never user-supplied).

Format: ``feature_<number>_<normalized-short-title>``:

    feature_125_login-page

The same User Story + title always yields the same name, so a double click, a
refresh or a retried request can never produce a second, differently named
branch. No part of this module performs I/O or talks to a provider.
"""
from __future__ import annotations

import re

from app.config import get_settings

# Branch creation is User Story-only. ``Story`` is the canonical stored type;
# the ``User Story`` label is accepted as a UI alias.
BRANCH_PREFIX_BY_ITEM_TYPE: dict[str, str] = {
    "Story": "feature",
    "User Story": "feature",
}

# Used when a title normalizes to nothing (e.g. "***" or "  ").
FALLBACK_TITLE = "story"

# The title slug is built from the first 25 characters of the (Unicode) title,
# taken BEFORE normalization, then normalized into a ref-safe slug.
_TITLE_SLUG_SOURCE_CHARS = 25

# Public alias for callers/tests that want the slug source length.
TITLE_SLUG_CHARS = _TITLE_SLUG_SOURCE_CHARS

_MAX_REFERENCE_CHARS = 32

# Everything that is not a lowercase alphanumeric becomes a single separator,
# which removes every character git refuses in a ref ('/', '~', '^', ':', '?',
# '*', '[', '\\', '@{', control chars and whitespace) in one pass.
_UNSAFE_RUN = re.compile(r"[^a-z0-9]+")
_INVALID_REF_CHARS = re.compile(r"[^A-Za-z0-9._-]")


class UnsupportedItemType(ValueError):
    """Branch creation is not offered for this work-item type."""


def supports_branch_creation(item_type: str | None) -> bool:
    return (item_type or "").strip() in BRANCH_PREFIX_BY_ITEM_TYPE


def is_valid_branch_name(name: str | None) -> bool:
    """True when git would accept ``name`` as a branch (no exception raised)."""
    try:
        ensure_valid_branch_name(name or "")
    except ValueError:
        return False
    return True


def branch_prefix_for(item_type: str | None) -> str:
    prefix = BRANCH_PREFIX_BY_ITEM_TYPE.get((item_type or "").strip())
    if prefix is None:
        raise UnsupportedItemType(
            "Feature branches can be created only for User Stories."
        )
    return prefix


def display_id_for_branch(item_type: str | None, item_id: int | None,
                          stored: str | None = None) -> str:
    """The persisted work-item number shown to users (display strings only)."""
    if stored:
        return stored.strip()
    from app.schemas import display_id_for

    return display_id_for(item_type, item_id)


def _stable_reference(item_type: str | None, item_id: int | None,
                      stored: str | None = None) -> str:
    """The stable work-item number embedded in a branch name.

    The canonical stored Story number (``item_id``) is preferred so the same
    Story always yields the same ref even if its display string changes; the
    stored display value is only a fallback for callers without a numeric id.
    """
    if item_id is not None:
        reference = str(item_id)
    else:
        reference = display_id_for_branch(item_type, item_id, stored)
    reference = _INVALID_REF_CHARS.sub("-", reference.strip())[
        :_MAX_REFERENCE_CHARS
    ].strip("-_.")
    return reference or (f"ID-{item_id}" if item_id is not None else "ID")


# Public alias: the stable reference used inside a branch name.
stable_reference = _stable_reference


def normalize_branch_title(title: str | None, *, max_length: int | None = None) -> str:
    """Lowercase, single-hyphenated, ref-safe short title; never empty."""
    limit = (
        max_length if max_length is not None
        else get_settings().GITHUB_BRANCH_TITLE_MAX_LENGTH
    )
    source = (title or "").strip()[:limit]
    slug = _UNSAFE_RUN.sub("-", source.lower()).strip("-_.")
    return slug or FALLBACK_TITLE


def ensure_valid_branch_name(name: str) -> str:
    """Reject (never silently rewrite) a name git would refuse as a ref."""
    invalid = (
        not name
        or name in (".", "..")
        or name.startswith(("-", "."))
        or name.endswith((".", ".lock"))
        or ".." in name
        or "@{" in name
        or _INVALID_REF_CHARS.search(name) is not None
    )
    if invalid:
        raise ValueError(f"Refusing to use an invalid git branch name: {name!r}")
    return name


def build_branch_name(
    item_type: str | None,
    title: str | None,
    *,
    item_id: int | None = None,
    display_id: str | None = None,
) -> str:
    """``feature_<stable-number>_<normalized-title>``, deterministic per item+title."""
    prefix = branch_prefix_for(item_type)
    reference = _stable_reference(item_type, item_id, display_id)

    settings = get_settings()
    # The slug source is the first 25 characters of the raw (Unicode) title,
    # normalized afterwards into a single-hyphenated ref-safe slug.
    title_slug = normalize_branch_title(
        (title or "")[:_TITLE_SLUG_SOURCE_CHARS],
        max_length=_TITLE_SLUG_SOURCE_CHARS,
    )
    # 2 separators + prefix + reference are fixed; the title absorbs the rest of
    # the length budget so the total stays inside GITHUB_BRANCH_NAME_MAX_LENGTH.
    budget = max(
        settings.GITHUB_BRANCH_NAME_MAX_LENGTH - len(prefix) - len(reference) - 2, 1
    )
    name = f"{prefix}_{reference}_{title_slug[:budget]}".rstrip("-_.")
    return ensure_valid_branch_name(name)


__all__ = [
    "BRANCH_PREFIX_BY_ITEM_TYPE",
    "FALLBACK_TITLE",
    "TITLE_SLUG_CHARS",
    "UnsupportedItemType",
    "branch_prefix_for",
    "build_branch_name",
    "display_id_for_branch",
    "ensure_valid_branch_name",
    "is_valid_branch_name",
    "normalize_branch_title",
    "stable_reference",
    "supports_branch_creation",
]