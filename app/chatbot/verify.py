"""Deterministic citation checks for cloud answers.

Partitions cited bug numbers into grounded/ungrounded and appends a caveat
for ungrounded ones; answer text is never rewritten.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Iterable

# "#42" / "bug 42" / "issue #42"; (?!\d) keeps long numeric runs from matching.
_CITE_RE = re.compile(r"(?:#|\bbug\s*#?|\bissue\s*#?)(\d{1,9})(?!\d)", re.IGNORECASE)

_CAVEAT = (
    "\n\n_Note: I could not ground {refs} in the records I retrieved — "
    "please verify {pron} directly._"
)

# Strong "I did X" forms only; "create/update" would false-positive in prose.
_WRITE_CLAIM_RE = re.compile(
    r"\bI(?:'ve| have| just| already| then| also| went ahead and)?\s+"
    r"(?:closed|reopened|assigned|unassigned|deleted|removed|resolved|"
    r"commented|marked)\b",
    re.IGNORECASE,
)

_WRITE_CLAIM_CAVEAT = (
    "\n\n_Note: I can't change anything in the tracker myself. To make that "
    'change, type the command (for example "close #12") and confirm the prompt._'
)


def cited_bug_ids(text: str) -> set[int]:
    """Bug numbers referenced in an answer, from '#42' / 'bug 42' forms."""
    return {int(m) for m in _CITE_RE.findall(text or "")}


@dataclass
class Verdict:
    """Citations checked against the grounding set."""
    grounded: set[int] = field(default_factory=set)
    ungrounded: set[int] = field(default_factory=set)

    @property
    def ok(self) -> bool:
        return not self.ungrounded


def verify(text: str, grounded_ids: Iterable[int]) -> Verdict:
    """Partition an answer's bug citations into grounded vs ungrounded."""
    allowed = set(grounded_ids or ())
    cited = cited_bug_ids(text)
    return Verdict(grounded=cited & allowed, ungrounded=cited - allowed)


def annotate(text: str, grounded_ids: Iterable[int]) -> str:
    """Append a caveat naming ungrounded citations; grounded answers pass unchanged."""
    verdict = verify(text, grounded_ids)
    if verdict.ok:
        return text
    refs_sorted = sorted(verdict.ungrounded)
    refs = ", ".join(f"#{i}" for i in refs_sorted)
    pron = "it" if len(refs_sorted) == 1 else "them"
    return text + _CAVEAT.format(refs=refs, pron=pron)


def flag_write_claims(text: str) -> str:
    """Append a correction when an answer falsely claims it performed a write."""
    if _WRITE_CLAIM_RE.search(text or ""):
        return text + _WRITE_CLAIM_CAVEAT
    return text


__all__ = ["Verdict", "cited_bug_ids", "verify", "annotate", "flag_write_claims"]
