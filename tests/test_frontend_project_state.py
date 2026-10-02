"""Source guards for the project-state upsert fix (Phase 6 contract).

Behavior lives in AppContext; these pins keep the monotonic load sequence, the
stale-response rejection, the normalized (dedup + case-insensitive sort) list
and the modal's response capture from drifting during refactors.
"""
from __future__ import annotations

from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
SRC = REPO / "frontend" / "src"
APPCTX = SRC / "state" / "AppContext.jsx"
PROJECT_MODAL = SRC / "modals" / "ProjectModal.jsx"
BUGMODAL = SRC / "modals" / "BugModal.jsx"


def _read(p: Path) -> str:
    return p.read_text(encoding="utf-8")


def test_appcontext_has_a_monotonic_project_load_sequence():
    src = _read(APPCTX)
    assert "projectLoadSeq" in src
    assert "projectMutationSeq" in src
    assert "++projectLoadSeq.current" in src
    assert "projectMutationSeq.current +=" in src


def test_load_projects_rejects_responses_older_than_the_latest_mutation():
    # The staleness rule lives in the shared helper AppContext imports; the
    # guard pins both the import and the rule's source of truth.
    src = _read(APPCTX)
    helper = _read(SRC / "lib" / "projectList.js")
    assert "isStaleProjectResponse({" in src
    assert "seq !== latestSeq" in helper


def test_staleness_rule_requires_a_matching_mutation_generation():
    """The second half of the staleness predicate (S9073: one assert per test)."""
    helper = _read(SRC / "lib" / "projectList.js")
    assert "mutatedAt !== mutationSeq" in helper


def test_projects_are_deduplicated_and_case_insensitively_sorted():
    src = _read(APPCTX)
    assert "normalizeProjects" in src
    helper = _read(SRC / "lib" / "projectList.js")
    assert "byId.has(row.id)" in helper
    assert "localeCompare" in helper and "toLowerCase()" in helper


def test_upsert_project_is_an_immutable_id_deduplicated_upsert():
    src = _read(APPCTX)
    assert "upsertProjectIntoList(current, project)" in src
    assert "lastProjectsSig.current = JSON.stringify(next)" in src
    helper = _read(SRC / "lib" / "projectList.js")
    assert "filter((row) => row.id !== project.id)" in helper


def test_project_modal_captures_the_save_response():
    src = _read(PROJECT_MODAL)
    assert "saved = await api(" in src
    assert "upsertProject(saved)" in src
    assert "await loadProjects()" in src


def test_new_item_options_are_derived_from_live_projects_state():
    src = _read(BUGMODAL)
    # The New Item project selector maps the shared projects state, so an
    # upsert while the modal is open updates the options without a reload.
    assert "projects.map((p) => ({ value: String(p.id), label: p.name }))" in src
