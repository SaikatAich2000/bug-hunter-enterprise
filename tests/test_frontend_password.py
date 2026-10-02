"""Source guards for password-field UX: the reveal toggle and legacy exception.
Guards refactors that drop the toggle or reject the legacy value client-side when the server accepts it.
"""
from __future__ import annotations

from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
SRC = REPO / "frontend" / "src"

PASSWORD_INPUT = SRC / "components" / "PasswordInput.jsx"
CONSTANTS = SRC / "lib" / "constants.js"
STYLES = SRC / "styles" / "styles.css"

# Every form that collects a password.
LOGIN = SRC / "login" / "LoginPage.jsx"
RESET = SRC / "reset" / "ResetPage.jsx"
CHANGE_PW = SRC / "modals" / "ChangePasswordModal.jsx"
USER_MODAL = SRC / "modals" / "UserModal.jsx"

PASSWORD_FORMS = [LOGIN, RESET, CHANGE_PW, USER_MODAL]


def _read(p: Path) -> str:
    return p.read_text(encoding="utf-8")


# The reusable PasswordInput component
def test_password_input_component_exists():
    assert PASSWORD_INPUT.exists(), "PasswordInput.jsx must exist"


def test_password_input_toggles_type_and_renders_eye():
    src = _read(PASSWORD_INPUT)
    # Toggling between text/password drives the eye button's state.
    assert 'type={visible ? "text" : "password"}' in src
    # CSS class hooks that the stylesheet targets.
    assert 'className="pw-wrap"' in src
    assert 'className="pw-toggle"' in src
    # Aria label must reflect current visibility state.
    assert "Show password" in src
    assert "Hide password" in src
    # Explicit type="button" prevents accidental form submission.
    assert 'type="button"' in src
    # forwardRef lets callers reach the DOM node for focus management.
    assert "forwardRef" in src


# Every password form uses PasswordInput (no bare type="password" left)
@pytest.mark.parametrize("path", PASSWORD_FORMS, ids=lambda p: p.name)
def test_password_forms_use_password_input(path):
    src = _read(path)
    assert 'from "../components/PasswordInput"' in src, (
        f"{path.name} must import PasswordInput"
    )
    assert "<PasswordInput" in src, f"{path.name} must render PasswordInput"
    # No bare password input may remain; everything routes through PasswordInput.
    assert 'type="password"' not in src, (
        f'{path.name} still has a bare type="password" input'
    )


def test_password_input_count_matches_fields():
    # One PasswordInput per field — no field may share or be skipped.
    assert _read(CHANGE_PW).count("<PasswordInput") == 3  # current/new/confirm
    assert _read(RESET).count("<PasswordInput") == 2  # new/confirm


# CSS hooks for the toggle
@pytest.mark.parametrize("cls", [".pw-wrap", ".pw-toggle"])
def test_password_toggle_css_present(cls):
    assert cls in _read(STYLES), f"styles.css missing {cls!r}"


# The legacy client-side exception
def test_client_validator_allows_legacy_default():
    # The legacy value is whitelisted case-insensitively before length/complexity so UI matches backend.
    assert 'toLowerCase() === "legacy-default"' in _read(CONSTANTS)
