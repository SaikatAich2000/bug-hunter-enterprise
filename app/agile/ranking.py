"""Sortable rank tokens for ordered lists (`bugs.rank` within `bugs.rank_scope`).

Tokens use a base-36 alphabet ("0"-"9", "a"-"z") whose ASCII order equals its
numeric order, so a token sorts the same way as a Python string and in a
plain VARCHAR ``ORDER BY`` on SQLite and PostgreSQL ("C"-style comparison of
these characters is the same in every collation that matters here: digits
before lowercase letters).

Placement keeps tokens short:

* the bottom/top of a list steps a fixed-width (``WIDTH``) token by ``STEP``,
  so appending or prepending never makes tokens longer (about 1.6 million
  appends fit before the width is exhausted);
* a batch between two neighbours is spread evenly over the gap, at the
  smallest width that fits it;
* when a gap is exhausted (or a token would get longer than ``MAX_LENGTH``),
  :class:`RankExhausted` tells the caller to :func:`spaced_ranks` the whole
  list, which reassigns evenly spaced fixed-width tokens.
"""
from __future__ import annotations

_ALPHABET = "0123456789abcdefghijklmnopqrstuvwxyz"
_BASE = len(_ALPHABET)
WIDTH = 6
STEP = _BASE ** 2
MAX_LENGTH = 32  # well inside the 64-character column


class RankExhausted(ValueError):
    """No token fits between the neighbours: rebalance the list."""


def _value(token: str, width: int) -> int:
    """``token`` read as a base-36 number of ``width`` digits (right-padded)."""
    value = 0
    for i in range(width):
        value = value * _BASE + (_ALPHABET.index(token[i]) if i < len(token) else 0)
    return value


def _token(value: int, width: int) -> str:
    digits = []
    for _ in range(width):
        value, digit = divmod(value, _BASE)
        digits.append(_ALPHABET[digit])
    if value:
        raise RankExhausted("rank overflow")
    return "".join(reversed(digits))


def _check(token: str) -> str:
    if any(c not in _ALPHABET for c in token):
        raise RankExhausted(f"unexpected rank token {token!r}")
    return token


def is_valid(token: str | None) -> bool:
    """A token the generator could have produced (alphabet and length)."""
    return bool(token) and len(token) <= MAX_LENGTH and all(c in _ALPHABET for c in token)


def spaced_ranks(count: int) -> list[str]:
    """``count`` evenly spaced ascending tokens for a whole list."""
    if count <= 0:
        return []
    width = WIDTH
    while _BASE ** width // (count + 1) < 2:
        width += 1
    step = _BASE ** width // (count + 1)
    return [_token(step * (i + 1), width) for i in range(count)]


def _after(lo: str, count: int) -> list[str]:
    # Stepping the first WIDTH characters keeps tokens short (a long token
    # from an earlier insert is followed by a short one again).
    start = _value(lo, WIDTH)
    if start + STEP * count < _BASE ** WIDTH:
        return [_token(start + STEP * (i + 1), WIDTH) for i in range(count)]
    # The fixed-width tail is used up: spread over the remaining gap at a
    # larger width (the caller rebalances if that gets too long).
    return _between(lo, None, count)


def _before(hi: str, count: int) -> list[str]:
    end = _value(hi, WIDTH)
    if end - STEP * count >= 1:
        return [_token(end - STEP * (count - i), WIDTH) for i in range(count)]
    return _between(None, hi, count)


def _between(lo: str | None, hi: str | None, count: int) -> list[str]:
    """``count`` tokens spread evenly between lo and hi (either may be open)."""
    width = max(WIDTH, len(lo or ""), len(hi or ""))
    while width <= MAX_LENGTH:
        low = _value(lo, width) if lo else 0
        high = _value(hi, width) if hi else _BASE ** width
        gap = high - low
        if gap > count:
            step = gap // (count + 1)
            return [_token(low + step * (i + 1), width) for i in range(count)]
        width += 1
    raise RankExhausted("no room between the neighbours")


def ranks_between(lo: str | None, hi: str | None, count: int) -> list[str]:
    """``count`` ascending tokens strictly after ``lo`` and before ``hi``.

    ``None`` means the list has no item on that side. Raises
    :class:`RankExhausted` when the tokens would not fit (or would be longer
    than ``MAX_LENGTH``); the caller then rebalances the list.
    """
    if count <= 0:
        return []
    if lo is not None:
        _check(lo)
    if hi is not None:
        _check(hi)
    if lo is not None and hi is not None and lo >= hi:
        raise RankExhausted("the neighbours are not in order")
    if lo is None and hi is None:
        tokens = spaced_ranks(count)
    elif hi is None:
        tokens = _after(lo, count)
    elif lo is None:
        tokens = _before(hi, count)
    else:
        tokens = _between(lo, hi, count)
    ordered = [lo, *tokens, hi]
    for left, right in zip(ordered, ordered[1:]):
        if left is not None and right is not None and not left < right:
            raise RankExhausted("no room between the neighbours")
    if any(len(t) > MAX_LENGTH for t in tokens):
        raise RankExhausted("rank tokens are getting too long")
    return tokens


def rank_between(lo: str | None, hi: str | None) -> str:
    """One token strictly after ``lo`` and before ``hi`` (see ranks_between)."""
    return ranks_between(lo, hi, 1)[0]
