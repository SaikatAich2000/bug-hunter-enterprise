"""Regressions found by property-based API fuzzing (Schemathesis).

Integers wider than the database column used to escape as HTTP 500
(SQLite raises OverflowError while binding; PostgreSQL raises DataError).
They are client input errors and must come back as 422.
"""
import pytest

HUGE = 10**25          # past any 64-bit column
NEG_HUGE = -(10**20)


@pytest.mark.parametrize(
    "method,path",
    [
        ("get", f"/api/bugs/{HUGE}"),
        ("get", f"/api/agile/boards?project_id={NEG_HUGE}"),
        ("get", f"/api/agile/boards/{NEG_HUGE}/planning"),
        ("get", f"/api/agile/reports/burnup?sprint_id={NEG_HUGE}"),
        ("get", f"/api/agile/reports/workload?sprint_id={HUGE}"),
        ("post", f"/api/agile/components/{HUGE}/archive"),
        ("post", f"/api/agile/projects/{NEG_HUGE}/disable"),
    ],
)
def test_out_of_range_ids_are_rejected_not_crashed(admin_client, method, path):
    r = getattr(admin_client, method)(path)
    assert r.status_code == 422, (r.status_code, r.text)
    detail = r.json()["detail"]
    assert isinstance(detail, list) and detail[0]["msg"] == "A value in the request is out of range."


def test_out_of_range_id_in_json_body_is_rejected(admin_client):
    r = admin_client.post("/api/bugs", json={
        "title": "Overflow probe", "project_id": HUGE, "item_type": "Bug",
    })
    assert r.status_code in (400, 404, 422), (r.status_code, r.text)


def test_in_range_missing_id_is_still_404(admin_client):
    assert admin_client.get("/api/bugs/987654").status_code == 404


def test_timestamps_carry_a_utc_offset(admin_client):
    """SQLite drops the offset; the API must still emit RFC 3339 date-times.

    Browsers parse an offset-less ISO string as *local* time, which shifted
    every displayed timestamp by the viewer's UTC offset.
    """
    project = admin_client.post("/api/projects", json={"name": "TZ probe"}).json()
    for stamp in (project["created_at"], admin_client.get("/api/projects").json()[0]["created_at"]):
        assert stamp.endswith(("+00:00", "Z")), stamp


def _agile_project(client, name):
    pid = client.post("/api/projects", json={"name": name}).json()["id"]
    board = client.post(f"/api/agile/projects/{pid}/enable", json={}).json()["board_id"]
    return pid, board


@pytest.mark.parametrize("kind", ["sprint", "release", "component"])
def test_duplicate_names_are_409_on_create_and_rename(admin_client, kind):
    """Unique (scope, name) indexes must surface as 409, never as a 500."""
    pid, board = _agile_project(admin_client, f"Dup {kind}")
    if kind == "sprint":
        create = lambda n: admin_client.post(f"/api/agile/sprints?board_id={board}", json={"name": n})  # noqa: E731
        update = lambda obj, n: admin_client.put(  # noqa: E731
            f"/api/agile/sprints/{obj['id']}", json={"name": n, "version": obj["version"]})
    else:
        base = {"release": "/api/agile/releases", "component": "/api/agile/components"}[kind]
        create = lambda n: admin_client.post(base, json={"project_id": pid, "name": n})  # noqa: E731
        update = lambda obj, n: admin_client.put(  # noqa: E731
            f"{base}/{obj['id']}", json={"name": n, "version": obj["version"]})
    first = create("Alpha")
    assert first.status_code == 201, first.text
    assert create("alpha ").status_code == 409  # normalised duplicate
    second = create("Beta")
    assert second.status_code == 201, second.text
    renamed = update(second.json(), "ALPHA")
    assert renamed.status_code == 409, (renamed.status_code, renamed.text)


DAILY_BAD_DATES = ["0000-00-00", "2026-02-30", "2026-13-01"]


@pytest.mark.parametrize("bad", DAILY_BAD_DATES)
def test_daily_report_rejects_impossible_dates_cleanly(admin_client, bad):
    pid, board = _agile_project(admin_client, f"Daily {bad}")
    sprint = admin_client.post(f"/api/agile/sprints?board_id={board}", json={"name": "Sprint A"}).json()
    r = admin_client.get(f"/api/agile/reports/daily?sprint_id={sprint['id']}&report_date={bad}")
    assert r.status_code == 422
    assert "strptime" not in r.text and "does not match format" not in r.text
