"""Regression tests for defects found while porting the Agile, Git, bulk-import
and Sleuth tool features into Bug Hunter. Each test names the defect it pins."""
from __future__ import annotations

import csv
import io

import pytest

from tests.conftest import BOOTSTRAP_EMAIL, BOOTSTRAP_PASSWORD

USER_EMAIL = "member@test.local"
USER_PASSWORD = "Member12345"


# --- helpers ---------------------------------------------------------------

def _login(client, email, password):
    client.post("/api/auth/logout")
    res = client.post("/api/auth/login", json={"email": email, "password": password})
    assert res.status_code == 200, res.text


def _as_admin(client):
    _login(client, BOOTSTRAP_EMAIL, BOOTSTRAP_PASSWORD)


def _project(client, name, agile=True):
    res = client.post("/api/projects", json={"name": name, "color": "#c9764f"})
    assert res.status_code == 201, res.text
    project = res.json()
    board_id = None
    if agile:
        res = client.post(f"/api/agile/projects/{project['id']}/enable", json={"feature_flags": {}})
        assert res.status_code == 200, res.text
        board_id = res.json()["board_id"]
    return project, board_id


def _member(client, project_id):
    res = client.post("/api/users", json={
        "name": "Member", "email": USER_EMAIL, "role": "user",
        "password": USER_PASSWORD, "project_ids": [project_id],
    })
    assert res.status_code == 201, res.text
    return res.json()


def _item(client, project_id, title="Item under test", item_type="Bug", **extra):
    res = client.post("/api/bugs", json={
        "project_id": project_id, "title": title, "item_type": item_type, **extra,
    })
    assert res.status_code == 201, res.text
    return res.json()


def _agile_item(client, project_id, title, item_type, **extra):
    res = client.post("/api/agile/work-items", json={
        "project_id": project_id, "title": title, "item_type": item_type, **extra,
    })
    assert res.status_code == 201, res.text
    return res.json()


def _sprint(client, board_id, name="Sprint 1"):
    res = client.post(f"/api/agile/sprints?board_id={board_id}", json={"name": name})
    assert res.status_code == 201, res.text
    return res.json()


def _start(client, sprint):
    res = client.post(f"/api/agile/sprints/{sprint['id']}/start", json={"version": sprint["version"]})
    assert res.status_code == 200, res.text
    return res.json()


def _bug_row(bug_id):
    from app.database import SessionLocal
    from app.models import Bug

    with SessionLocal() as db:
        row = db.get(Bug, bug_id)
        db.expunge(row)
        return row


def _history(sprint_id):
    from app.database import SessionLocal
    from app.models import SprintItemHistory

    with SessionLocal() as db:
        return [
            (h.work_item_id, h.event_type)
            for h in db.query(SprintItemHistory).filter(SprintItemHistory.sprint_id == sprint_id)
            .order_by(SprintItemHistory.id)
        ]


def _audit_actions():
    from app.database import SessionLocal
    from app.models import Activity

    with SessionLocal() as db:
        return [a.action for a in db.query(Activity).all()]


# --- Agile edit policy -------------------------------------------------------

def test_user_cannot_change_a_task_through_agile_endpoints(admin_client):
    """Defect: Agile routes skipped can_edit_bug, so a regular user could drag,
    estimate or flag a Task/Requirement that PUT /api/bugs refuses."""
    project, _board = _project(admin_client, "Policy Suite")
    task = _item(admin_client, project["id"], "Locked task", "Task")
    bug = _item(admin_client, project["id"], "Open bug", "Bug")
    _member(admin_client, project["id"])
    _login(admin_client, USER_EMAIL, USER_PASSWORD)

    res = admin_client.post(f"/api/agile/work-items/{task['id']}/transition",
                            json={"to_status": "In Progress", "version": task["version"]})
    assert res.status_code == 403
    res = admin_client.put(f"/api/agile/work-items/{task['id']}/estimate",
                           json={"story_points": 3, "version": task["version"]})
    assert res.status_code == 403
    res = admin_client.post(f"/api/agile/work-items/{task['id']}/flag",
                            json={"flagged": True, "version": task["version"]})
    assert res.status_code == 403
    # Bugs stay editable by users, exactly as on /api/bugs.
    res = admin_client.post(f"/api/agile/work-items/{bug['id']}/transition",
                            json={"to_status": "In Progress", "version": bug["version"]})
    assert res.status_code == 200, res.text


def test_board_transition_keeps_a_legacy_task_a_task(admin_client):
    """Defect: the first board drag rewrote item_type Task -> Sub-task, dropping
    it from the Tasks tab and the backlog and changing who may edit it."""
    project, board_id = _project(admin_client, "Task Type Suite")
    task = _item(admin_client, project["id"], "Legacy task", "Task")
    res = admin_client.post(f"/api/agile/work-items/{task['id']}/transition",
                            json={"to_status": "In Progress", "version": task["version"]})
    assert res.status_code == 200, res.text
    assert _bug_row(task["id"]).item_type == "Task"
    backlog = admin_client.get(f"/api/agile/boards/{board_id}/backlog").json()
    assert task["id"] in {row["id"] for row in backlog}


# --- Board view ------------------------------------------------------------

def test_board_cards_carry_display_ids_and_sit_in_one_column(admin_client):
    """The board shows the sprint's issues and their Sub-tasks, each once, with
    its own key; an Epic is never a card (Epics are not planned into sprints)."""
    project, board_id = _project(admin_client, "Board View Suite")
    sprint = _sprint(admin_client, board_id)
    epic = _agile_item(admin_client, project["id"], "An epic", "Epic")
    story = _agile_item(admin_client, project["id"], "A story", "Story",
                        sprint_id=sprint["id"], epic_id=epic["id"])
    sub = _agile_item(admin_client, project["id"], "A sub-task", "Sub-task", parent_id=story["id"])
    task = _item(admin_client, project["id"], "A task", "Task")
    admin_client.post(f"/api/agile/sprints/{sprint['id']}/items", json={"item_ids": [task["id"]]})
    view = admin_client.get(f"/api/agile/boards/{board_id}/view?sprint_id={sprint['id']}").json()
    cards = [c for col in view["columns"] for c in col["cards"]]
    assert sorted(c["id"] for c in cards) == sorted([story["id"], sub["id"], task["id"]])
    by_id = {c["id"]: c for c in cards}
    assert by_id[story["id"]]["display_id"] == f"USRSTR-{story['id']}"
    assert by_id[story["id"]]["epic_id"] == epic["id"]
    assert (by_id[story["id"]]["subtask_count"], by_id[story["id"]]["subtasks_done"]) == (1, 0)
    assert by_id[sub["id"]]["display_id"] == f"SUB-{sub['id']}"
    assert by_id[sub["id"]]["parent_id"] == story["id"]
    assert by_id[sub["id"]]["epic_id"] == epic["id"]  # inherited from its parent
    assert by_id[task["id"]]["display_id"] == f"TASK-{task['id']}"
    for col in view["columns"]:
        assert col["card_count"] == len(col["cards"])
    assert [e["id"] for e in view["epics"]] == [epic["id"]]


def test_wip_limit_counts_only_the_moving_items_sprint(admin_client):
    """Defect: the WIP count covered the whole project (backlog included), so the
    server blocked moves the board showed as under the limit."""
    project, board_id = _project(admin_client, "WIP Suite")
    board = admin_client.get(f"/api/agile/boards/{board_id}").json()
    columns = [
        {"name": c["name"], "category": c["category"], "statuses": c["statuses"],
         "wip_limit": 1 if c["category"] == "in_progress" else None,
         "wip_enforcement": "block" if c["category"] == "in_progress" else "off"}
        for c in board["columns"]
    ]
    res = admin_client.put(f"/api/agile/boards/{board_id}/columns",
                           json={"columns": columns, "version": board["version"]})
    assert res.status_code == 200, res.text
    # Two backlog items already In Progress must not count against the sprint.
    for n in range(2):
        _item(admin_client, project["id"], f"Backlog busy {n}", status="In Progress")
    sprint = _sprint(admin_client, board_id)
    story = _agile_item(admin_client, project["id"], "Sprint story", "Story", sprint_id=sprint["id"])
    res = admin_client.post(f"/api/agile/work-items/{story['id']}/transition",
                            json={"to_status": "In Progress", "version": story["version"]})
    assert res.status_code == 200, res.text


# --- Sprint membership and status bookkeeping outside the board -------------

def test_put_sprint_change_moves_rank_scope_and_records_history(admin_client):
    """Defect: PUT /api/bugs set sprint_id raw — no rank-scope move and no
    history; taking it back out left it invisible in the backlog."""
    project, board_id = _project(admin_client, "Sprint PUT Suite")
    sprint = _sprint(admin_client, board_id)
    _agile_item(admin_client, project["id"], "Seed", "Story", sprint_id=sprint["id"])
    sprint = _start(admin_client, admin_client.get(f"/api/agile/sprints/{sprint['id']}").json())
    bug = _item(admin_client, project["id"], "Movable bug")

    res = admin_client.put(f"/api/bugs/{bug['id']}", json={"sprint_id": sprint["id"]})
    assert res.status_code == 200, res.text
    assert _bug_row(bug["id"]).rank_scope == f"sprint:{sprint['id']}"
    assert (bug["id"], "added") in _history(sprint["id"])

    res = admin_client.put(f"/api/bugs/{bug['id']}", json={"sprint_id": None})
    assert res.status_code == 200, res.text
    assert _bug_row(bug["id"]).rank_scope == f"backlog:{project['id']}"
    backlog = admin_client.get(f"/api/agile/boards/{board_id}/backlog").json()
    assert bug["id"] in {row["id"] for row in backlog}


def test_status_change_outside_board_is_recorded_for_reports(admin_client):
    """Defect: form/bulk status changes skipped sprint history and resolved_at,
    so burndown and flow reports never saw them."""
    project, board_id = _project(admin_client, "Status History Suite")
    sprint = _sprint(admin_client, board_id)
    story = _agile_item(admin_client, project["id"], "Tracked", "Story", sprint_id=sprint["id"])
    _start(admin_client, admin_client.get(f"/api/agile/sprints/{sprint['id']}").json())
    res = admin_client.put(f"/api/bugs/{story['id']}", json={"status": "Done"})
    assert res.status_code == 200, res.text
    assert (story["id"], "status_changed") in _history(sprint["id"])
    assert _bug_row(story["id"]).resolved_at is not None


def test_project_move_detaches_project_scoped_links(admin_client):
    """Defect: moving an item to another project kept the old project's sprint,
    rank scope and hierarchy links."""
    project, board_id = _project(admin_client, "Move From")
    other, _ = _project(admin_client, "Move To")
    sprint = _sprint(admin_client, board_id)
    story = _agile_item(admin_client, project["id"], "Traveller", "Story", sprint_id=sprint["id"])
    res = admin_client.put(f"/api/bugs/{story['id']}", json={"project_id": other["id"]})
    assert res.status_code == 200, res.text
    row = _bug_row(story["id"])
    assert row.sprint_id is None and row.epic_id is None
    assert row.rank_scope == f"backlog:{other['id']}"


def test_project_move_takes_subtasks_along_but_not_epic_children(admin_client):
    """Jira moves an issue together with its Sub-tasks. A Sub-task cannot move
    on its own, and an Epic that still has issues is refused."""
    project, _board = _project(admin_client, "Parent Suite")
    other, _ = _project(admin_client, "Elsewhere")
    parent = _item(admin_client, project["id"], "Parent bug")
    child = _agile_item(admin_client, project["id"], "Child sub-task", "Sub-task", parent_id=parent["id"])

    res = admin_client.put(f"/api/bugs/{child['id']}", json={"project_id": other["id"]})
    assert res.status_code == 409 and "parent" in res.json()["detail"]

    res = admin_client.put(f"/api/bugs/{parent['id']}", json={"project_id": other["id"]})
    assert res.status_code == 200, res.text
    assert _bug_row(child["id"]).project_id == other["id"]

    epic = _agile_item(admin_client, other["id"], "Busy epic", "Epic")
    story = _agile_item(admin_client, other["id"], "Epic story", "Story", epic_id=epic["id"])
    res = admin_client.put(f"/api/bugs/{epic['id']}", json={"project_id": project["id"]})
    assert res.status_code == 409
    assert _bug_row(story["id"]).epic_id == epic["id"]


# --- Agile create ------------------------------------------------------------

def test_agile_create_validates_assignees_and_places_sprint_properly(admin_client):
    """Defect: unknown assignee ids were silently dropped, sprint_id was set
    without a sprint rank or history, and an Epic's sprint landed on bugs.sprint_id."""
    project, board_id = _project(admin_client, "Agile Create Suite")
    res = admin_client.post("/api/agile/work-items", json={
        "project_id": project["id"], "title": "Ghost assignee", "item_type": "Story",
        "assignee_ids": [987654],
    })
    assert res.status_code == 400
    sprint = _sprint(admin_client, board_id)
    admin_client.post(f"/api/agile/sprints/{sprint['id']}/items",
                      json={"item_ids": [_item(admin_client, project["id"], "Filler")["id"]]})
    sprint = _start(admin_client, admin_client.get(f"/api/agile/sprints/{sprint['id']}").json())
    story = _agile_item(admin_client, project["id"], "Planned story", "Story",
                        sprint_id=sprint["id"], status="In Progress")
    row = _bug_row(story["id"])
    assert row.status == "In Progress"
    assert row.rank_scope == f"sprint:{sprint['id']}"
    assert (story["id"], "added") in _history(sprint["id"])
    res = admin_client.post("/api/agile/work-items", json={
        "project_id": project["id"], "title": "Sprint epic", "item_type": "Epic", "sprint_id": sprint["id"],
    })
    assert res.status_code == 422 and "not planned" in res.json()["detail"]


# --- Taxonomy ----------------------------------------------------------------

def test_subtask_sprint_follows_its_parent_on_every_route(admin_client):
    """A Sub-task is always in its parent's sprint: it cannot be planned alone
    through PUT /api/bugs, and it leaves with its parent."""
    project, board_id = _project(admin_client, "Unassign Suite")
    sprint = _sprint(admin_client, board_id)
    story = _agile_item(admin_client, project["id"], "Parent", "Story", sprint_id=sprint["id"])
    sub = _agile_item(admin_client, project["id"], "Child", "Sub-task", parent_id=story["id"])
    assert _bug_row(sub["id"]).sprint_id == sprint["id"]
    res = admin_client.put(f"/api/bugs/{sub['id']}", json={"sprint_id": None})
    assert res.status_code == 422 and "parent" in res.json()["detail"]
    res = admin_client.put(f"/api/bugs/{story['id']}", json={"sprint_id": None})
    assert res.status_code == 200, res.text
    assert _bug_row(sub["id"]).sprint_id is None


def test_archived_epic_refuses_new_issues(admin_client):
    """Defect: the archived check read a column bugs doesn't have (always False)."""
    project, _board = _project(admin_client, "Archive Suite")
    epic = _agile_item(admin_client, project["id"], "Old epic", "Epic")
    assert admin_client.post(f"/api/agile/epics/{epic['id']}/archive").status_code == 200
    res = admin_client.post("/api/agile/work-items", json={
        "project_id": project["id"], "title": "Too late", "item_type": "Story", "epic_id": epic["id"],
    })
    assert res.status_code == 422 and "Archived" in res.json()["detail"]
    story = _agile_item(admin_client, project["id"], "Existing", "Story")
    res = admin_client.put(f"/api/agile/work-items/{story['id']}/hierarchy",
                           json={"epic_id": epic["id"], "version": story["version"]})
    assert res.status_code == 422


def test_taxonomy_changes_are_audited(admin_client):
    project, _board = _project(admin_client, "Audit Suite")
    admin_client.post("/api/agile/releases", json={"project_id": project["id"], "name": "v1"})
    admin_client.post("/api/agile/components", json={"project_id": project["id"], "name": "API"})
    admin_client.post("/api/agile/labels", json={"project_id": project["id"], "display_name": "urgent"})
    actions = _audit_actions()
    for action in ("release_created", "component_created", "label_created"):
        assert action in actions


# --- Reports -----------------------------------------------------------------

def test_report_csv_neutralises_formulas(admin_client):
    project, board_id = _project(admin_client, "CSV Suite")
    sprint = _sprint(admin_client, board_id)
    story = _agile_item(admin_client, project["id"], "=HYPERLINK(\"http://x\")", "Story")
    admin_client.post(f"/api/agile/sprints/{sprint['id']}/items", json={"item_ids": [story["id"]]})
    _start(admin_client, admin_client.get(f"/api/agile/sprints/{sprint['id']}").json())
    extra = _agile_item(admin_client, project["id"], "@SUM(A1)", "Story")
    admin_client.post(f"/api/agile/sprints/{sprint['id']}/items", json={"item_ids": [extra["id"]]})
    res = admin_client.post(f"/api/agile/reports/export?report_key=scope-change&sprint_id={sprint['id']}")
    assert res.status_code == 200, res.text
    rows = list(csv.DictReader(io.StringIO(res.text)))
    assert rows and all(not str(v).startswith(("=", "@", "+", "-")) for r in rows for v in r.values())


def test_flow_reports_validate_the_date_range(admin_client):
    _project_, board_id = _project(admin_client, "Range Suite")
    base = f"/api/agile/reports/cumulative-flow?board_id={board_id}"
    assert admin_client.get(f"{base}&date_from=bad&date_to=2026-01-01").status_code == 422
    assert admin_client.get(f"{base}&date_from=2026-02-01&date_to=2026-01-01").status_code == 422
    assert admin_client.get(f"{base}&date_from=2020-01-01&date_to=2026-01-01").status_code == 422
    assert admin_client.get(f"{base}&date_from=2026-01-01&date_to=2026-01-10").status_code == 200


def test_sprint_workbook_export_survives_a_non_ascii_name(admin_client):
    """Defect: a non-ASCII sprint name went into the Content-Disposition header."""
    _project_, board_id = _project(admin_client, "Unicode Suite")
    sprint = _sprint(admin_client, board_id, name="Sprint Ωmega")
    res = admin_client.get(f"/api/agile/reports/sprint/{sprint['id']}/export.xlsx")
    assert res.status_code == 200, res.text


# --- Planning ------------------------------------------------------------------

def test_capacity_rejects_duplicates_and_unknown_users(admin_client):
    """Defect: both reached the database and surfaced as a 500."""
    _project_, board_id = _project(admin_client, "Capacity Suite")
    sprint = _sprint(admin_client, board_id)
    entry = {"user_id": 1, "capacity_value": 5, "capacity_unit": "points"}
    res = admin_client.put(f"/api/agile/sprints/{sprint['id']}/capacity",
                           json={"entries": [entry, entry]})
    assert res.status_code == 422
    res = admin_client.put(f"/api/agile/sprints/{sprint['id']}/capacity",
                           json={"entries": [{**entry, "user_id": 424242}]})
    assert res.status_code == 400
    res = admin_client.put(f"/api/agile/sprints/{sprint['id']}/capacity", json={"entries": [entry]})
    assert res.status_code == 200, res.text
    assert "sprint_capacity_updated" in _audit_actions()


# --- Bulk import -------------------------------------------------------------

def test_bulk_import_reports_an_overlong_title_instead_of_failing(admin_client):
    """Defect: a >200-char title passed validation and crashed the insert on Postgres."""
    project, _board = _project(admin_client, "Import Suite", agile=False)
    body = f"Title,Project\n{'x' * 201},{project['name']}\nFine title,{project['name']}\n"
    res = admin_client.post("/api/bugs/import", files={"file": ("items.csv", body.encode(), "text/csv")})
    assert res.status_code == 200, res.text
    out = res.json()
    assert out["created"] == 1 and out["failed"] == 1
    assert "at most 200" in out["errors"][0]["error"]


# --- Sleuth tools --------------------------------------------------------------

def test_sleuth_tools_match_rest_create_rules(admin_client):
    from app.chatbot import tools
    from app.chatbot.llm_tools_agent import confirm_pending_tool_call
    from app.database import SessionLocal
    from app.models import User

    plain, _ = _project(admin_client, "Tools Plain", agile=False)
    agile_project, board_id = _project(admin_client, "Tools Agile")
    with SessionLocal() as db:
        actor = db.query(User).filter(User.email == BOOTSTRAP_EMAIL).first()
        with pytest.raises(tools.ToolError, match="Agile is not enabled"):
            tools.create_work_item(db, actor, {"project_name": plain["name"], "title": "Epic here", "item_type": "Epic"})
        with pytest.raises(tools.ToolError, match="parent"):
            tools.create_work_item(db, actor, {"project_name": agile_project["name"], "title": "Orphan", "item_type": "Sub-task"})
        db.rollback()
        resp = confirm_pending_tool_call(db, actor, {
            "tool": "create_work_item",
            "args": {"project_name": agile_project["name"], "title": "From chat", "item_type": "Bug"},
        })
        assert resp.intent == "action_done"
    backlog = admin_client.get(f"/api/agile/boards/{board_id}/backlog").json()
    assert "From chat" in {row["title"] for row in backlog}
    assert "bug_created" in _audit_actions()


# --- Git -------------------------------------------------------------------------

def test_branch_verification_searches_the_same_window_as_discovery(monkeypatch):
    """Defect: discovery listed up to 200 repositories but create verified
    against the provider default (100)."""
    from app.git.provider import ProviderRepository
    from app.routes import git as git_routes

    seen = {}

    class Provider:
        def list_repositories(self, *, limit=100):
            seen["limit"] = limit
            return [ProviderRepository(provider_repo_id="150", name="r", owner="o", url="")]

    repo = git_routes._verified_provider_metadata(Provider(), "150")
    assert repo.provider_repo_id == "150"
    assert seen["limit"] == git_routes._DISCOVERY_LIMIT == 200


# --- Startup ----------------------------------------------------------------------

def test_https_deploy_without_app_env_keeps_the_lighter_gate():
    """Defect: COOKIE_SECURE alone triggered the APP_ENV=production gate, so an
    existing HTTPS deploy using console email refused to boot after upgrading."""
    import types

    from app.main import _production_config_errors

    base = dict(
        SESSION_SECRET="x" * 40, BOOTSTRAP_ADMIN_EMAIL="admin@x.test", BOOTSTRAP_ADMIN_PASSWORD="Strong-Pass-1",
        AUTO_LOGIN_ENABLED=False,
        COOKIE_SECURE=True, APP_BASE_URL="http://intranet", EMAIL_BACKEND="console",
        GIT_CREDENTIAL_ENCRYPTION_KEY="",
    )
    assert _production_config_errors(types.SimpleNamespace(**base, is_production_env=False)) == []
    strict = _production_config_errors(types.SimpleNamespace(**base, is_production_env=True))
    assert any("https" in e for e in strict) and any("console" in e for e in strict)
    weak = _production_config_errors(types.SimpleNamespace(
        **{**base, "SESSION_SECRET": "short", "AUTO_LOGIN_ENABLED": True}, is_production_env=False,
    ))
    assert any("SESSION_SECRET" in e for e in weak) and any("AUTO_LOGIN" in e for e in weak)
