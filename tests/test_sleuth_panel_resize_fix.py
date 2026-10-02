"""Source guard for the SleuthPanel resize-crash fix.

Before this fix, `clampSize(w)` was missing its `h` parameter while the body
still referenced `h`, so calling it (e.g. from the window `resize` listener)
threw an uncaught `ReferenceError: h is not defined` that crashed the entire
React tree (no error boundary). All three call sites always passed two
arguments — only the function signature was wrong. See
frontend/src/sleuth/SleuthPanel.jsx.
"""
from __future__ import annotations

import re
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
SLEUTH_PANEL = REPO / "frontend" / "src" / "sleuth" / "SleuthPanel.jsx"


def _read() -> str:
    return SLEUTH_PANEL.read_text(encoding="utf-8")


def test_clamp_size_declares_both_width_and_height_params():
    """clampSize must take (w, h); a one-arg signature reintroduces the
    ReferenceError on `h` inside the function body."""
    src = _read()
    m = re.search(r"function clampSize\(([^)]*)\)", src)
    assert m, "clampSize function not found"
    params = [p.strip() for p in m.group(1).split(",")]
    assert params == ["w", "h"], f"clampSize must declare (w, h), got ({m.group(1)})"


def test_clamp_size_body_only_references_declared_params():
    """Guard against a partial revert: the height clamp must use the `h` parameter."""
    src = _read()
    start = src.find("function clampSize(")
    assert start != -1
    end = src.find("\n}", start)
    body = src[start:end]
    assert "Math.min(maxH, h)" in body


def test_resize_listener_calls_clamp_size_with_both_dimensions():
    """The window `resize` handler (the crash trigger) must call clampSize
    with both current dimensions, not a truncated call."""
    src = _read()
    assert "setSize((s) => clampSize(s.w, s.h));" in src, (
        "resize handler must call clampSize(s.w, s.h) with both dimensions"
    )


def test_all_clamp_size_call_sites_pass_two_arguments():
    """Every known clampSize(...) call site must pass two arguments (a
    trailing comma before the closing paren would mean an arg got dropped)."""
    src = _read()
    known_call_sites = [
        "setSize(clampSize(s.w + (s.x - e.clientX), s.h + (s.y - e.clientY)));",
        "clampSize(560, 760)",
        "setSize((s) => clampSize(s.w, s.h));",
    ]
    for call in known_call_sites:
        assert call in src, f"expected call site not found (signature drifted?): {call!r}"

