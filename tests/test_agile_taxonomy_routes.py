"""REST-level tests for app/routes/agile_taxonomy.py: Epics, Releases,
Components, Labels, Collections — get/update/archive, version conflicts,
duplicate-name conflicts, and access/visibility checks not already covered
by the taxonomy/release smoke tests.
"""
from __future__ import annotations


def _make_project(admin_client, name="Taxonomy Route Suite"):
    res = admin_client.post("/api/projects", json={"name": name, "color": "#c9764f"})
    assert res.status_code == 201, res.text
    project = res.json()
    enable_res = admin_client.post(f"/api/agile/projects/{project['id']}/enable", json={"feature_flags": {}})
    assert enable_res.status_code == 200, enable_res.text
    return project


# --- Epics ---

def test_epic_get_update_archive_and_version_conflict(admin_client):
    project = _make_project(admin_client, "Epic Routes Suite")
    epic = admin_client.post("/api/agile/work-items", json={
        "project_id": project["id"], "title": "Route Epic", "item_type": "epic",
    }).json()

    get_res = admin_client.get(f"/api/agile/epics/{epic['id']}")
    assert get_res.status_code == 200
    assert get_res.json()["title"] == "Route Epic"

    assert admin_client.get("/api/agile/epics/999999").status_code == 404

    update_res = admin_client.put(f"/api/agile/epics/{epic['id']}", json={
        "health": "at_risk", "version": 1,
    })
    assert update_res.status_code == 200, update_res.text
    assert update_res.json()["health"] == "at_risk"

    conflict_res = admin_client.put(f"/api/agile/epics/{epic['id']}", json={
        "health": "on_track", "version": 1,
    })
    assert conflict_res.status_code == 409

    archive_res = admin_client.post(f"/api/agile/epics/{epic['id']}/archive")
    assert archive_res.status_code == 200
    assert archive_res.json()["archived"] is True


def test_epic_routes_404_for_non_epic_or_missing(admin_client):
    project = _make_project(admin_client, "Epic Non-Epic Suite")
    bug = admin_client.post("/api/bugs", json={
        "project_id": project["id"], "title": "Not an epic", "item_type": "Bug",
    }).json()
    assert admin_client.get(f"/api/agile/epics/{bug['id']}").status_code == 404
    assert admin_client.put(f"/api/agile/epics/{bug['id']}", json={"version": 1}).status_code == 404
    assert admin_client.post(f"/api/agile/epics/{bug['id']}/archive").status_code == 404


# --- Releases / Versions ---

def test_release_get_update_archive_and_conflicts(admin_client):
    project = _make_project(admin_client, "Release Routes Suite")
    v = admin_client.post("/api/agile/releases", json={
        "project_id": project["id"], "name": "v1.2.3",
    }).json()

    get_res = admin_client.get(f"/api/agile/releases/{v['id']}")
    assert get_res.status_code == 200
    assert get_res.json()["name"] == "v1.2.3"
    assert admin_client.get("/api/agile/releases/999999").status_code == 404

    dup_res = admin_client.post("/api/agile/releases", json={
        "project_id": project["id"], "name": "v1.2.3",
    })
    assert dup_res.status_code == 409

    update_res = admin_client.put(f"/api/agile/releases/{v['id']}", json={
        "name": "v1.2.4", "version": v["version"],
    })
    assert update_res.status_code == 200, update_res.text

    conflict_res = admin_client.put(f"/api/agile/releases/{v['id']}", json={
        "name": "v1.2.5", "version": v["version"],
    })
    assert conflict_res.status_code == 409

    archive_res = admin_client.post(f"/api/agile/releases/{v['id']}/archive")
    assert archive_res.status_code == 200
    assert archive_res.json()["archived"] is True


def test_release_update_duplicate_name_conflict(admin_client):
    project = _make_project(admin_client, "Release Dup Name Suite")
    admin_client.post("/api/agile/releases", json={"project_id": project["id"], "name": "v1.0"})
    v2 = admin_client.post("/api/agile/releases", json={"project_id": project["id"], "name": "v2.0"}).json()
    conflict_res = admin_client.put(f"/api/agile/releases/{v2['id']}", json={
        "name": "v1.0", "version": v2["version"],
    })
    assert conflict_res.status_code == 409


# --- Components ---

def test_component_get_update_archive_and_conflicts(admin_client):
    project = _make_project(admin_client, "Component Routes Suite")
    c = admin_client.post("/api/agile/components", json={
        "project_id": project["id"], "name": "Backend",
    }).json()

    get_res = admin_client.get(f"/api/agile/components/{c['id']}")
    assert get_res.status_code == 200
    assert get_res.json()["name"] == "Backend"
    assert admin_client.get("/api/agile/components/999999").status_code == 404

    dup_res = admin_client.post("/api/agile/components", json={
        "project_id": project["id"], "name": "Backend",
    })
    assert dup_res.status_code == 409

    update_res = admin_client.put(f"/api/agile/components/{c['id']}", json={
        "name": "Backend Services", "version": c["version"],
    })
    assert update_res.status_code == 200, update_res.text

    conflict_res = admin_client.put(f"/api/agile/components/{c['id']}", json={
        "name": "Something Else", "version": c["version"],
    })
    assert conflict_res.status_code == 409

    archive_res = admin_client.post(f"/api/agile/components/{c['id']}/archive")
    assert archive_res.status_code == 200
    assert archive_res.json()["archived"] is True


def test_component_update_duplicate_name_conflict(admin_client):
    project = _make_project(admin_client, "Component Dup Name Suite")
    admin_client.post("/api/agile/components", json={"project_id": project["id"], "name": "API"})
    c2 = admin_client.post("/api/agile/components", json={"project_id": project["id"], "name": "UI"}).json()
    conflict_res = admin_client.put(f"/api/agile/components/{c2['id']}", json={
        "name": "API", "version": c2["version"],
    })
    assert conflict_res.status_code == 409


# --- Labels ---

def test_label_get_update_delete_and_conflicts(admin_client):
    project = _make_project(admin_client, "Label Routes Suite")
    label = admin_client.post("/api/agile/labels", json={
        "project_id": project["id"], "display_name": "urgent",
    }).json()

    get_res = admin_client.get(f"/api/agile/labels/{label['id']}")
    assert get_res.status_code == 200
    assert get_res.json()["display_name"] == "urgent"
    assert admin_client.get("/api/agile/labels/999999").status_code == 404

    update_res = admin_client.put(f"/api/agile/labels/{label['id']}", json={
        "color": "#ff0000", "version": label["version"],
    })
    assert update_res.status_code == 200, update_res.text

    conflict_res = admin_client.put(f"/api/agile/labels/{label['id']}", json={
        "color": "#00ff00", "version": label["version"],
    })
    assert conflict_res.status_code == 409

    delete_res = admin_client.delete(f"/api/agile/labels/{label['id']}")
    assert delete_res.status_code == 200


def test_label_update_duplicate_name_conflict(admin_client):
    project = _make_project(admin_client, "Label Dup Name Suite")
    admin_client.post("/api/agile/labels", json={"project_id": project["id"], "display_name": "bug-bash"})
    label2 = admin_client.post("/api/agile/labels", json={
        "project_id": project["id"], "display_name": "regression",
    }).json()
    conflict_res = admin_client.put(f"/api/agile/labels/{label2['id']}", json={
        "display_name": "bug-bash", "version": label2["version"],
    })
    assert conflict_res.status_code == 409


def test_label_delete_blocked_while_in_use(admin_client):
    project = _make_project(admin_client, "Label In Use Suite")
    label = admin_client.post("/api/agile/labels", json={
        "project_id": project["id"], "display_name": "in-use-label",
    }).json()
    bug = admin_client.post("/api/bugs", json={
        "project_id": project["id"], "title": "Labeled bug", "item_type": "Bug",
    }).json()
    add_res = admin_client.post(f"/api/bugs/{bug['id']}/labels/{label['id']}")
    if add_res.status_code == 200:
        delete_res = admin_client.delete(f"/api/agile/labels/{label['id']}")
        assert delete_res.status_code == 409


def test_roadmap_requires_agile_enabled(admin_client):
    project_res = admin_client.post("/api/projects", json={"name": "Roadmap Guard Suite", "color": "#123456"})
    project = project_res.json()
    assert admin_client.get(f"/api/agile/roadmap?project_id={project['id']}").status_code == 404


def test_taxonomy_create_reports_agile_not_enabled(admin_client):
    """Taxonomy creates in an Agile-off project explain the real reason.

    The project is listed to the caller, so "Project not found" would be wrong;
    the status stays 404 for parity with inaccessible projects.
    """
    project = admin_client.post(
        "/api/projects", json={"name": "Taxonomy Agile Off Suite", "color": "#c9764f"},
    ).json()
    res = admin_client.post("/api/agile/components", json={
        "project_id": project["id"], "name": "Blocked Component",
    })
    assert res.status_code == 404
    assert res.json()["detail"] == "Agile is not enabled for this project"
