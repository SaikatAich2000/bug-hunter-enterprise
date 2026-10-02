"""Source guards for the Story feature-branch list UI.

Pins the three presentation requirements so a refactor cannot silently drop
them:

1. one Remove control per live branch (no bulk button row);
2. removed branches are sidelined (ordered last, greyed out, dashed);
3. the status chip carries a faint, status-relevant colour (green when live,
   red once removed, amber when the provider state is unknown).

Behavior of the underlying helpers is covered by
``frontend/src/components/gitBranches.test.jsx``; this module only pins the
wiring between those helpers, the markup and the stylesheet.
"""
from __future__ import annotations

from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
SRC = REPO / "frontend" / "src"
PANEL = SRC / "components" / "GitBranchesPanel.jsx"
REQUESTS = SRC / "lib" / "gitBranchRequests.js"
STYLES = SRC / "styles" / "styles.css"


def _read(p: Path) -> str:
    return p.read_text(encoding="utf-8")


def test_remove_control_is_per_branch_row():
    src = _read(PANEL)
    # The Remove button lives inside the row's own action cluster, beside the
    # status chip, and is rendered once per removable branch.
    assert 'className="git-branch-row-side"' in src


def test_remove_control_wires_confirmation_per_branch():
    """The row action confirms and removes exactly that branch."""
    src = _read(PANEL)
    assert "git-branch-remove-btn" in src
    assert "confirmAndRemove(branch)" in src


def test_bulk_remove_section_stays_removed():
    """The old bulk strip below the list must stay gone."""
    src = _read(PANEL)
    assert "renderRemoveSection" not in src
    assert 'className="git-branch-remove"' not in src


def test_only_live_branches_offer_removal():
    """Only live (non-removed) branches offer removal (one assert)."""
    src = _read(PANEL)
    assert "!isRemoved && canRemove" in src


def test_removed_branches_are_sidelined_and_greyed_out():
    panel = _read(PANEL)
    helpers = _read(REQUESTS)
    # Ordering + classification come from the tested helpers, not inline logic.
    assert "partitionBranches" in helpers
    assert "partitionBranches" in panel


def test_removed_branch_classification_is_shared():
    """The panel uses the shared removed-branch classifier."""
    panel = _read(PANEL)
    helpers = _read(REQUESTS)
    assert "isRemovedBranch" in helpers
    assert "isRemovedBranch" in panel


def test_removed_rows_carry_the_greyed_out_class():
    """The greyed-out class is applied conditionally to removed rows (one assert)."""
    panel = _read(PANEL)
    assert 'isRemoved ? " is-removed" : ""' in panel


def test_removed_rows_are_faded_in_the_stylesheet():
    """The stylesheet fades removed rows via opacity (one assert)."""
    styles = _read(STYLES)
    assert ".git-branch-row.is-removed" in styles
    assert "opacity" in styles.split(".git-branch-row.is-removed")[1].split("}")[0]


def test_branch_status_chip_has_faint_status_colours():
    styles = _read(STYLES)
    panel = _read(PANEL)
    assert "data-branch-status={branch.status}" in panel
    for status, variable in (
        ("Active", "var(--ok)"),
        ("Deleted", "var(--danger)"),
        ("Unknown", "var(--warn)"),
    ):
        needle = f'.badge[data-branch-status="{status}"]'
        assert needle in styles, f"missing faint {status} chip colour"
        assert variable in styles.split(needle)[1].split("}")[0]
