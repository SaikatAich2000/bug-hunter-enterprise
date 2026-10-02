"""Guards for the Jira-style Sprints frontend.

Behaviour of the React code itself is covered by the Vitest suites under
``frontend/src`` and the browser tests (``test_e2e_browser.py``). This file
pins the seams those cannot see from one side alone:

* every ``/agile`` URL the frontend calls exists on the backend with the
  method the frontend uses (a renamed route would otherwise only show up as a
  404 toast in the browser);
* the Jira rules the UI must keep (sprint lifecycle through dialogs, ranking
  through the board ``move`` endpoint, board drag through the workflow
  transition endpoint, no retired Collection/Feature concepts).
"""
from __future__ import annotations

import re
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
SRC = REPO / "frontend" / "src"
AGILE_DIR = SRC / "views" / "agile"
SPRINTS_VIEW = SRC / "views" / "SprintsView.jsx"
CONFIRM_HOST = SRC / "components" / "ConfirmHost.jsx"

AGILE_SOURCES = sorted(
    [*AGILE_DIR.rglob("*.js"), *AGILE_DIR.rglob("*.jsx"), SPRINTS_VIEW, SRC / "modals" / "BugModal.jsx"]
)
AGILE_SOURCES = [p for p in AGILE_SOURCES if ".test." not in p.name]


def _read(p: Path) -> str:
    return p.read_text(encoding="utf-8")


_CALL = re.compile(r"api\(\s*`(/agile[^`]*)`")
_METHOD = re.compile(r'(?:method:\s*"([A-Z]+)"|json\(\s*"([A-Z]+)")')


def _frontend_calls() -> list[tuple[str, str, str]]:
    """(source file, method, path pattern) for every agile call."""
    calls = []
    for path in AGILE_SOURCES:
        src = _read(path)
        for match in _CALL.finditer(src):
            raw = match.group(1)
            # The path ends where the query string (or a conditional query
            # template) starts; ``${expr}`` segments become placeholders.
            raw = re.split(r"\?|\$\{[^}]*\?", raw, maxsplit=1)[0]
            pattern = re.sub(r"\$\{[^}]+\}", "{}", raw)
            # A trailing ``${params}`` glued to a segment is a query string.
            pattern = re.sub(r"(?<=[^/])\{\}$", "", pattern)
            # The method, if any, follows in the same call's options.
            tail = src[match.end(): match.end() + 160]
            tail = tail.split("api(", 1)[0]
            method_match = _METHOD.search(tail)
            method = next((g for g in method_match.groups() if g), "GET") if method_match else "GET"
            calls.append((path.name, method, "/api" + pattern))
    return calls


def _backend_routes() -> list[tuple[set[str], re.Pattern]]:
    from app.main import app

    # The OpenAPI schema lists every mounted route (included routers are not
    # flattened into ``app.routes`` on recent FastAPI versions).
    routes = []
    for path, operations in app.openapi()["paths"].items():
        if not path.startswith("/api/agile"):
            continue
        regex = re.compile("^" + re.sub(r"\{[^}]+\}", "[^/]+", path) + "$")
        routes.append(({method.upper() for method in operations}, regex))
    assert routes, "no /api/agile routes found"
    return routes


def test_frontend_calls_are_found():
    calls = _frontend_calls()
    # Sanity: the scan sees the agile API module and the modal.
    assert len(calls) >= 40
    assert any(name == "agileApi.js" for name, _m, _p in calls)
    assert any(name == "BugModal.jsx" for name, _m, _p in calls)


@pytest.mark.parametrize("source,method,pattern", _frontend_calls())
def test_every_frontend_agile_call_has_a_backend_route(source, method, pattern):
    concrete = pattern.replace("{}", "1")
    matches = [methods for methods, regex in _backend_routes() if regex.match(concrete)]
    assert matches, f"{source}: no backend route for {pattern}"
    assert any(method in methods for methods in matches), (
        f"{source}: {pattern} exists but not for {method}"
    )


def test_sprints_view_has_the_jira_tabs():
    src = _read(SPRINTS_VIEW)
    keys = re.findall(r'\{ key: "(\w+)"', src)
    assert keys == ["backlog", "board", "hierarchy", "reports", "taxonomy", "planning", "settings"]
    assert 'label: "Active sprint"' in src
    # Board settings are only offered to people who can manage the board.
    assert 't.key !== "settings" || canManage' in src


def test_backlog_ranks_by_drag_through_the_move_endpoint():
    src = _read(AGILE_DIR / "BacklogPanel.jsx")
    assert "DndContext" in src and "useSortable" in src
    assert "agileApi.move(" in src
    # Keyboard users can rank too.
    assert "KeyboardSensor" in src


def test_board_drag_changes_status_through_the_workflow_transition():
    src = _read(AGILE_DIR / "BoardPanel.jsx")
    assert "DndContext" in src
    assert "agileApi.transition(" in src
    # A drop lands in the column's first status; the card menu offers every
    # status of every column for the others.
    assert "dropStatus(column, card)" in src
    assert "Move to ${status} (${col.name})" in src


def test_sprint_lifecycle_uses_dialogs_never_browser_prompts():
    for path in AGILE_SOURCES:
        src = _read(path)
        assert "window.prompt(" not in src, path.name
        assert "window.confirm(" not in src, path.name
        assert "window.alert(" not in src, path.name
    dialogs = _read(AGILE_DIR / "SprintDialogs.jsx")
    for name in ("SprintFormDialog", "StartSprintDialog", "CompleteSprintDialog"):
        assert f"export function {name}" in dialogs
    assert "agileApi.startSprint(" in dialogs
    assert "agileApi.completeSprint(" in dialogs


def test_retired_hierarchy_levels_are_gone_from_the_ui():
    # Collections and Features were replaced by Epics + labels; the UI must
    # not offer them anywhere.
    for name in ("IssueCard.jsx", "BacklogRow.jsx", "HierarchyItemDialog.jsx", "features.js"):
        assert not (AGILE_DIR / name).exists(), name
    for path in AGILE_SOURCES:
        src = _read(path)
        assert "/agile/collections" not in src, path.name
        assert "/agile/features" not in src, path.name
        assert "collection_id" not in src, path.name
        assert "feature_id" not in src, path.name


def test_confirm_host_exports_prompt_dialog_with_input_support():
    src = _read(CONFIRM_HOST)
    assert "export function promptDialog(" in src
    assert "opts.input" in src
    assert 'className="confirm-input"' in src
