"""Role x endpoint matrix, derived from the OpenAPI routes.

Every /api operation must be in POLICY (a new route fails ``test_policy_covers_every_route`` until a
decision is written down). Each role then calls every operation against organization A's real
records and must be allowed or refused as declared; an attacker from organization B must be refused
everything that touches A's records.

Tiers: public (no session needed), auth (any signed-in user), manager (manager or admin),
admin (org admin only), lead (project lead, manager or admin), self (acts on the caller only).
"""
from __future__ import annotations

import pytest

from tests.conftest import new_client, sign_up
from tests.test_cross_tenant import _OWN_SCOPE, PASSWORD, World, _operations

ROLES = ["anonymous", "user", "lead", "manager", "admin", "attacker"]


def _login(email):
    c = new_client()
    assert c.post("/api/auth/login", json={"email": email, "password": PASSWORD}).status_code == 200
    return c


def client_for(world: World, role: str):
    if role == "anonymous":
        return new_client()
    if role == "admin":
        return world.a
    if role == "attacker":
        return world.b
    email = f"{role}@a.test"
    res = world.a.post("/api/users", json={
        "name": role.title(), "email": email, "role": "manager" if role == "manager" else "user",
        "password": PASSWORD, "project_ids": [world.project_id]})
    assert res.status_code == 201, res.text
    if role == "lead":
        up = world.a.put(f"/api/projects/{world.project_id}/members/{res.json()['id']}", json={"role": "lead"})
        assert up.status_code == 200, up.text
    return _login(email)


def _bodies(w: World) -> dict[tuple[str, str], dict]:
    """Valid bodies for calls whose authorization check runs after body validation."""
    pid = w.project_id
    return {
        ("POST", "/api/invitations"): {"email": "probe@a.test"},
        ("POST", "/api/projects/{project_id}/members"): {"user_id": w.user_id, "role": "member"},
        ("PUT", "/api/projects/{project_id}/members/{user_id}"): {"role": "member"},
        ("POST", "/api/projects/{project_id}/custom-fields"): {"name": "Probe"},
        ("POST", "/api/events"): {"name": "Probe event"},
        ("POST", "/api/saved-views"): {"name": "probe", "filters": {}},
        ("POST", "/api/bugs"): {"project_id": pid, "title": "probe"},
        ("POST", "/api/agile/releases"): {"project_id": pid, "name": "probe"},
        ("POST", "/api/agile/components"): {"project_id": pid, "name": "probe"},
        ("POST", "/api/agile/labels"): {"project_id": pid, "display_name": "probe"},
        ("POST", "/api/agile/sprints"): {"name": "Probe", "goal": "g"},
    }


_METHOD_RANK = {"GET": 0, "PUT": 1, "PATCH": 1, "POST": 2, "DELETE": 3}


def _order(op):
    """Reads first, then writes, destructive calls last, so earlier calls do not destroy the data
    later ones need."""
    method, template = op[0], op[1]
    late = any(w in template for w in ("disable", "archive", "release", "complete", "cancel", "/delete"))
    last = template in ("/api/projects/{project_id}", "/api/users/{user_id}", "/api/agile/projects/{project_id}/disable")
    return (last, method == "DELETE", late, _METHOD_RANK.get(method, 2), template)


def probe(world: World, role: str) -> dict[tuple[str, str], int]:
    c = client_for(world, role)
    bodies = _bodies(world)
    out = {}
    for method, template, url, query in sorted(_operations(world), key=_order):
        if (method, template) in SKIP and role != "anonymous":
            continue
        body = bodies.get((method, template), {}) if method in ("POST", "PUT", "PATCH") else None
        out[(method, template)] = c.request(method, url, params=query, json=body).status_code
    return out


# Calls that end or rewrite the caller's own session / the organization itself.
SKIP: set[tuple[str, str]] = {
    ("POST", "/api/auth/logout"), ("DELETE", "/api/auth/account"),
}


# Declared policy. "auth" rows whose authorization lives in the request body (422 for an empty body
# before any role check) are asserted only for the anonymous 401 here; their per-role rules are
# covered by the feature suites (test_role_policy, test_type_permissions, test_agile_*).
POLICY: dict[tuple[str, str], str] = {
    ("DELETE", "/api/auth/account"): "auth",  # signed-in roles are not probed: it ends their own session
    ("POST", "/api/auth/logout"): "public",  # idempotent: clearing a cookie needs no session
    ("DELETE", "/api/agile/acceptance-criteria/{criterion_id}"): "auth",
    ("PUT", "/api/agile/acceptance-criteria/{criterion_id}"): "auth",
    ("GET", "/api/agile/boards"): "auth",
    ("GET", "/api/agile/boards/{board_id}"): "auth",
    ("PUT", "/api/agile/boards/{board_id}"): "auth",
    ("GET", "/api/agile/boards/{board_id}/backlog"): "auth",
    ("PUT", "/api/agile/boards/{board_id}/columns"): "auth",
    ("POST", "/api/agile/boards/{board_id}/move"): "auth",
    ("GET", "/api/agile/boards/{board_id}/planning"): "auth",
    ("GET", "/api/agile/boards/{board_id}/quick-filters"): "auth",
    ("POST", "/api/agile/boards/{board_id}/quick-filters"): "auth",
    ("POST", "/api/agile/boards/{board_id}/rank"): "auth",
    ("GET", "/api/agile/boards/{board_id}/view"): "auth",
    ("GET", "/api/agile/components"): "auth",
    ("POST", "/api/agile/components"): "manager",
    ("GET", "/api/agile/components/{component_id}"): "auth",
    ("PUT", "/api/agile/components/{component_id}"): "auth",
    ("POST", "/api/agile/components/{component_id}/archive"): "manager",
    ("GET", "/api/agile/epics"): "auth",
    ("GET", "/api/agile/epics/{epic_id}"): "auth",
    ("PUT", "/api/agile/epics/{epic_id}"): "auth",
    ("POST", "/api/agile/epics/{epic_id}/archive"): "manager",
    ("GET", "/api/agile/labels"): "auth",
    ("POST", "/api/agile/labels"): "manager",
    ("DELETE", "/api/agile/labels/{label_id}"): "manager",
    ("GET", "/api/agile/labels/{label_id}"): "auth",
    ("PUT", "/api/agile/labels/{label_id}"): "auth",
    ("POST", "/api/agile/projects/{project_id}/disable"): "manager",
    ("POST", "/api/agile/projects/{project_id}/enable"): "manager",
    ("GET", "/api/agile/projects/{project_id}/hierarchy"): "auth",
    ("GET", "/api/agile/projects/{project_id}/settings"): "auth",
    ("DELETE", "/api/agile/quick-filters/{filter_id}"): "manager",
    ("PUT", "/api/agile/quick-filters/{filter_id}"): "auth",
    ("GET", "/api/agile/releases"): "auth",
    ("POST", "/api/agile/releases"): "manager",
    ("GET", "/api/agile/releases/{version_id}"): "auth",
    ("PUT", "/api/agile/releases/{version_id}"): "auth",
    ("GET", "/api/agile/releases/{version_id}/360"): "auth",
    ("POST", "/api/agile/releases/{version_id}/archive"): "manager",
    ("POST", "/api/agile/releases/{version_id}/release"): "auth",
    ("GET", "/api/agile/reports/burndown"): "auth",
    ("GET", "/api/agile/reports/burnup"): "auth",
    ("GET", "/api/agile/reports/control-chart"): "auth",
    ("GET", "/api/agile/reports/cumulative-flow"): "auth",
    ("GET", "/api/agile/reports/daily"): "auth",
    ("GET", "/api/agile/reports/epic/{epic_id}"): "auth",
    ("POST", "/api/agile/reports/export"): "auth",
    ("GET", "/api/agile/reports/release/{version_id}"): "auth",
    ("GET", "/api/agile/reports/scope-change"): "auth",
    ("GET", "/api/agile/reports/sprint/{sprint_id}"): "auth",
    ("GET", "/api/agile/reports/sprint/{sprint_id}/export.xlsx"): "auth",
    ("GET", "/api/agile/reports/velocity"): "auth",
    ("GET", "/api/agile/reports/workload"): "auth",
    ("GET", "/api/agile/roadmap"): "auth",
    ("GET", "/api/agile/sprints"): "auth",
    ("POST", "/api/agile/sprints"): "manager",
    ("GET", "/api/agile/sprints/{sprint_id}"): "auth",
    ("PUT", "/api/agile/sprints/{sprint_id}"): "auth",
    ("POST", "/api/agile/sprints/{sprint_id}/cancel"): "auth",
    ("PUT", "/api/agile/sprints/{sprint_id}/capacity"): "manager",
    ("POST", "/api/agile/sprints/{sprint_id}/complete"): "auth",
    ("GET", "/api/agile/sprints/{sprint_id}/history"): "auth",
    ("POST", "/api/agile/sprints/{sprint_id}/items"): "auth",
    ("DELETE", "/api/agile/sprints/{sprint_id}/items/{work_item_id}"): "manager",
    ("GET", "/api/agile/sprints/{sprint_id}/planning"): "auth",
    ("POST", "/api/agile/sprints/{sprint_id}/start"): "auth",
    ("POST", "/api/agile/work-items"): "auth",
    ("GET", "/api/agile/work-items/{item_id}/acceptance-criteria"): "auth",
    ("POST", "/api/agile/work-items/{item_id}/acceptance-criteria"): "auth",
    ("PUT", "/api/agile/work-items/{item_id}/dates"): "auth",
    ("PUT", "/api/agile/work-items/{item_id}/estimate"): "auth",
    ("POST", "/api/agile/work-items/{item_id}/flag"): "auth",
    ("PUT", "/api/agile/work-items/{item_id}/hierarchy"): "auth",
    ("PUT", "/api/agile/work-items/{item_id}/ready-for-sprint"): "auth",
    ("PUT", "/api/agile/work-items/{item_id}/task-flags"): "auth",
    ("PUT", "/api/agile/work-items/{item_id}/taxonomy"): "auth",
    ("POST", "/api/agile/work-items/{item_id}/transition"): "auth",
    ("GET", "/api/audit"): "manager",
    ("GET", "/api/audit/export.csv"): "manager",
    ("POST", "/api/auth/2fa/begin"): "auth",
    ("POST", "/api/auth/2fa/confirm"): "auth",
    ("POST", "/api/auth/2fa/disable"): "auth",
    ("POST", "/api/auth/2fa/recovery-codes/regenerate"): "auth",
    ("GET", "/api/auth/2fa/status"): "auth",
    ("POST", "/api/auth/change-password"): "auth",
    ("GET", "/api/auth/data-export"): "auth",
    ("POST", "/api/auth/email-change/confirm"): "auth",
    ("POST", "/api/auth/email-change/request"): "auth",
    ("POST", "/api/auth/forgot-password"): "public",
    ("POST", "/api/auth/login"): "public",
    ("POST", "/api/auth/login/totp"): "public",
    ("GET", "/api/auth/me"): "auth",
    ("PUT", "/api/auth/profile"): "auth",
    ("POST", "/api/auth/resend-verification"): "public",
    ("POST", "/api/auth/reset-password"): "public",
    ("POST", "/api/auth/signup"): "public",
    ("POST", "/api/auth/verify-email"): "public",
    ("GET", "/api/branding"): "admin",
    ("PUT", "/api/branding"): "admin",
    ("GET", "/api/bugs"): "auth",
    ("POST", "/api/bugs"): "auth",
    ("POST", "/api/bugs/bulk"): "auth",
    ("POST", "/api/bugs/import"): "manager",
    ("GET", "/api/bugs/import/template.xlsx"): "manager",
    ("DELETE", "/api/bugs/{bug_id}"): "admin",
    ("GET", "/api/bugs/{bug_id}"): "auth",
    ("PUT", "/api/bugs/{bug_id}"): "auth",
    ("GET", "/api/bugs/{bug_id}/activity"): "auth",
    ("POST", "/api/bugs/{bug_id}/attachments"): "auth",
    ("DELETE", "/api/bugs/{bug_id}/attachments/{att_id}"): "auth",
    ("GET", "/api/bugs/{bug_id}/attachments/{att_id}/download"): "auth",
    ("GET", "/api/bugs/{bug_id}/comments"): "auth",
    ("POST", "/api/bugs/{bug_id}/comments"): "auth",
    ("DELETE", "/api/bugs/{bug_id}/comments/{comment_id}"): "admin",
    ("PUT", "/api/bugs/{bug_id}/comments/{comment_id}"): "auth",
    ("GET", "/api/bugs/{bug_id}/custom-values"): "auth",
    ("PUT", "/api/bugs/{bug_id}/custom-values"): "auth",
    ("GET", "/api/bugs/{bug_id}/links"): "auth",
    ("POST", "/api/bugs/{bug_id}/links"): "auth",
    ("DELETE", "/api/bugs/{bug_id}/links/{link_id}"): "auth",
    ("POST", "/api/chat/ask"): "auth",
    ("GET", "/api/chat/download/{token}"): "auth",
    ("POST", "/api/chat/ingest"): "admin",
    ("POST", "/api/devices/register"): "auth",
    ("DELETE", "/api/devices/{token}"): "auth",
    ("GET", "/api/events"): "auth",
    ("POST", "/api/events"): "manager",
    ("DELETE", "/api/events/{event_id}"): "admin",
    ("GET", "/api/events/{event_id}"): "auth",
    ("PUT", "/api/events/{event_id}"): "manager",
    ("DELETE", "/api/git/branches/{branch_record_id}"): "auth",
    ("GET", "/api/git/projects/{project_id}/available-repositories"): "auth",
    ("GET", "/api/git/projects/{project_id}/config"): "manager",
    ("PUT", "/api/git/projects/{project_id}/config"): "manager",
    ("POST", "/api/git/projects/{project_id}/config/test"): "manager",
    ("GET", "/api/git/work-items/{work_item_id}/branches"): "auth",
    ("POST", "/api/git/work-items/{work_item_id}/branches"): "auth",
    ("GET", "/api/git/work-items/{work_item_id}/branches/preview"): "auth",
    ("GET", "/api/health"): "public",
    ("GET", "/api/invitations"): "manager",
    ("POST", "/api/invitations"): "manager",
    ("POST", "/api/invitations/accept"): "public",
    ("GET", "/api/invitations/preview/{token}"): "public",
    ("DELETE", "/api/invitations/{invitation_id}"): "manager",
    ("GET", "/api/me/preferences"): "auth",
    ("PUT", "/api/me/preferences"): "auth",
    ("GET", "/api/meta"): "public",
    ("GET", "/api/notifications"): "auth",
    ("GET", "/api/notifications/preferences"): "auth",
    ("PUT", "/api/notifications/preferences"): "auth",
    ("POST", "/api/notifications/read-all"): "auth",
    ("GET", "/api/notifications/unread_count"): "auth",
    ("DELETE", "/api/notifications/{notif_id}"): "auth",
    ("POST", "/api/notifications/{notif_id}/read"): "auth",
    ("GET", "/api/organization"): "auth",
    ("PUT", "/api/organization"): "admin",
    ("POST", "/api/organization/delete"): "admin",
    ("GET", "/api/organization/deletion-summary"): "admin",
    ("GET", "/api/projects"): "auth",
    ("POST", "/api/projects"): "manager",
    ("DELETE", "/api/projects/{project_id}"): "admin",
    ("GET", "/api/projects/{project_id}"): "auth",
    ("PUT", "/api/projects/{project_id}"): "manager",
    ("GET", "/api/projects/{project_id}/custom-fields"): "auth",
    ("POST", "/api/projects/{project_id}/custom-fields"): "lead",
    ("DELETE", "/api/projects/{project_id}/custom-fields/{field_id}"): "lead",
    ("PUT", "/api/projects/{project_id}/custom-fields/{field_id}"): "lead",
    ("GET", "/api/projects/{project_id}/members"): "auth",
    ("POST", "/api/projects/{project_id}/members"): "lead",
    ("DELETE", "/api/projects/{project_id}/members/{user_id}"): "lead",
    ("PUT", "/api/projects/{project_id}/members/{user_id}"): "lead",
    ("GET", "/api/push/config"): "public",
    ("POST", "/api/push/subscribe"): "auth",
    ("POST", "/api/push/unsubscribe"): "auth",
    ("POST", "/api/reports/export.xlsx"): "manager",
    ("POST", "/api/reports/run"): "manager",
    ("GET", "/api/reports/types"): "manager",
    ("GET", "/api/saved-views"): "auth",
    ("POST", "/api/saved-views"): "auth",
    ("DELETE", "/api/saved-views/{view_id}"): "admin",
    ("GET", "/api/saved-views/{view_id}"): "auth",
    ("PUT", "/api/saved-views/{view_id}"): "admin",
    ("GET", "/api/sessions"): "admin",
    ("DELETE", "/api/sessions/{session_id}"): "admin",
    ("GET", "/api/stats"): "auth",
    ("GET", "/api/users"): "auth",
    ("POST", "/api/users"): "manager",
    ("DELETE", "/api/users/{user_id}"): "admin",
    ("GET", "/api/users/{user_id}"): "auth",
    ("PUT", "/api/users/{user_id}"): "manager",
    ("POST", "/api/users/{user_id}/reset-2fa"): "admin",
    ("GET", "/api/webhooks"): "admin",
    ("POST", "/api/webhooks"): "admin",
    ("DELETE", "/api/webhooks/{hook_id}"): "admin",
    ("GET", "/api/webhooks/{hook_id}"): "admin",
    ("PUT", "/api/webhooks/{hook_id}"): "admin",
    ("POST", "/api/webhooks/{hook_id}/rotate-secret"): "admin",
    ("POST", "/api/webhooks/{hook_id}/test"): "admin",
}
# Some rows are owner-scoped rather than role-scoped (saved views, comments): "admin" there means the
# organization owner who created the record in this world.

ALLOWED = {
    "public": {"anonymous", "user", "lead", "manager", "admin", "attacker"},
    "auth": {"user", "lead", "manager", "admin", "attacker"},
    "manager": {"manager", "admin", "attacker"},
    "lead": {"lead", "admin", "attacker"},
    "admin": {"admin", "attacker"},
}


def _declared_operations():
    from app.main import app

    return {(m.upper(), p) for p, ms in app.openapi()["paths"].items() if p.startswith("/api") for m in ms}


def test_policy_covers_every_route():
    """A new route fails here until someone decides who may call it."""
    actual = _declared_operations()
    assert not actual - set(POLICY), f"routes without a declared policy: {sorted(actual - set(POLICY))}"
    assert not set(POLICY) - actual, f"policy rows for routes that no longer exist: {sorted(set(POLICY) - actual)}"
    assert set(POLICY.values()) <= set(ALLOWED)


@pytest.mark.parametrize("role", ["anonymous", "user", "lead", "manager", "admin", "attacker"])
def test_every_role_gets_exactly_the_declared_access(client, role):
    world = World()
    observed = probe(world, role)
    wrong = []
    for op, status in observed.items():
        tier = POLICY[op]
        refused = status in (401, 403)
        if role in ALLOWED[tier]:
            if refused:
                wrong.append(f"{op}: {role} should be allowed ({tier}), got {status}")
        else:
            expected = 401 if role == "anonymous" else 403
            if status != expected:
                wrong.append(f"{op}: {role} should get {expected} ({tier}), got {status}")
    assert len(observed) >= 190, "the matrix shrank"
    assert not wrong, "\n".join(wrong)


def test_the_attacker_from_another_org_is_refused_every_foreign_record(client):
    """The attacker is an admin of org B: admin-only tiers open to it, but never on A's ids."""
    world = World()
    observed = probe(world, "attacker")
    leaks = []
    for (method, template), status in observed.items():
        if "{" not in template or (method, template) in _OWN_SCOPE or POLICY[(method, template)] == "public":
            continue
        if status < 400:
            leaks.append(f"{method} {template} -> {status}")
    assert not leaks, "\n".join(leaks)


SECRETS = ("Secret bug", "Secret event", "classified", "secret note", "Colleague", "owner@a.test",
           "colleague@a.test", "pending@a.test", "Org A", "Customer", "A view", "hooks.example.com/a",
           "Sprint A", "Epic A")


def test_nothing_of_org_a_appears_in_any_org_b_read_or_export(client):
    """Every parameterless GET of org B (lists, exports, search, notifications, metrics, data export)
    plus searches for A's words and B's own custom values, saved views and chat answers."""
    world = World()
    b = world.b
    # B gets data of its own so empty results cannot hide a leak
    b_project = b.get("/api/projects").json()[0]["id"]
    bug = b.post("/api/bugs", json={"project_id": b_project, "title": "B item", "description": "d"}).json()
    b.post("/api/projects/%d/custom-fields" % b_project, json={"name": "Region"})
    b.post("/api/saved-views", json={"name": "B view", "filters": {}, "shared_with_org": True})
    b.post("/api/invitations", json={"email": "pending@b.test"})
    # A reacts to its own bug so A has notifications and audit rows too
    world.a.put(f"/api/bugs/{world.bug_id}", json={"title": "Secret bug v2"})

    texts = []
    for method, template, url, query in _operations(world):
        if method != "GET" or "{" in template or template.startswith("/api/invitations/preview"):
            continue
        res = b.get(url, params=query)
        assert res.status_code < 500, (template, res.status_code)
        if res.headers.get("content-type", "").startswith(("application/json", "text/")):
            texts.append((template, res.text))
    assert len(texts) >= 30
    for q in ("Secret", "classified", "Colleague", "owner@a.test"):
        for path in ("/api/bugs", "/api/events", "/api/users", "/api/audit", "/api/saved-views"):
            texts.append((f"{path}?q={q}", b.get(path, params={"q": q, "search": q}).text))
    for message in ("search Secret", "who is Colleague", "show webhooks", "list custom fields"):
        texts.append((f"chat:{message}", b.post("/api/chat/ask", json={"message": message}).text))
    texts.append(("custom-values", b.get(f"/api/bugs/{bug['id']}/custom-values").text))
    for template, text in texts:
        for secret in SECRETS:
            assert secret not in text, f"{secret!r} leaked through {template}"
