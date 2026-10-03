"""Release-hardening focused tests for the Git feature (v1.3).

Covers the failure paths the release checklist calls out that are easiest to
get wrong under load or between two API calls: concurrent duplicate creation
across workers, a repository/config change between preview and create, project
scoping, and branch-history retention after an exact removal.

Every provider call is mocked - nothing contacts a real GitHub host.
"""
from __future__ import annotations

import threading

import pytest
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

from tests.test_git_branches import (
    ADMIN_EMAIL,
    BASE_SHA,
    FakeProvider,
    _login,
    _make_project,
    _make_story,
    _make_user,
    _post_branch,
    _preview_branch,
    _table_count,
)


@pytest.fixture
def provider() -> FakeProvider:
    return FakeProvider()


@pytest.fixture
def git_env(admin_client, monkeypatch, provider):
    from app.config import Settings, get_settings

    monkeypatch.setattr(Settings, "GIT_BRANCH_CREATION_ENABLED", True)
    monkeypatch.setattr(Settings, "GIT_BRANCH_DELETION_ENABLED", True)
    monkeypatch.setattr(Settings, "GITHUB_API_URL", "https://api.github.com")
    monkeypatch.setattr(Settings, "GITHUB_ORGANIZATION", "acme-org")
    monkeypatch.setattr(Settings, "GITHUB_TOKEN", "test-pat")

    get_settings.cache_clear()

    from app.routes import git as git_routes

    monkeypatch.setattr(
        git_routes,
        "build_provider",
        lambda config, base_url=None, organization=None, token=None: provider,
    )
    yield git_routes
    get_settings.cache_clear()


@pytest.fixture
def git_project(git_env, admin_client):
    project = _make_project(admin_client, "Branch Project")
    response = admin_client.put(
        f"/api/git/projects/{project['id']}/config",
        json={"enabled": True, "default_base_branch": "dev"},
    )
    assert response.status_code == 200, response.text
    return {"project": project}


# ===========================================================================
# Configuration change between preview and create
# ===========================================================================

def test_integration_disabled_between_preview_and_create(
    git_project, admin_client, git_env, provider
):
    project = git_project["project"]
    story = _make_story(admin_client, project["id"], "Integration off mid-flight")

    preview = _preview_branch(admin_client, story["id"])
    assert preview.status_code == 200, preview.text

    assert admin_client.put(
        f"/api/git/projects/{project['id']}/config", json={"enabled": False}
    ).status_code == 200

    response = _post_branch(admin_client, story["id"])
    assert response.status_code == 409, response.text
    assert provider.calls.count("create_branch") == 0
    assert admin_client.get(
        f"/api/git/work-items/{story['id']}/branches"
    ).json()["branches"] == []


def test_branch_creation_globally_disabled_between_preview_and_create(
    git_project, admin_client, git_env, provider, monkeypatch
):
    from app.config import Settings, get_settings

    project = git_project["project"]
    story = _make_story(admin_client, project["id"], "Global kill switch")

    preview = _preview_branch(admin_client, story["id"])
    assert preview.status_code == 200, preview.text

    monkeypatch.setattr(Settings, "GIT_BRANCH_CREATION_ENABLED", False)
    get_settings.cache_clear()
    response = _post_branch(admin_client, story["id"])
    assert response.status_code == 409, response.text
    assert provider.calls.count("create_branch") == 0


# ===========================================================================
# Concurrent duplicate creation across workers
# ===========================================================================

@pytest.mark.parametrize("forced_overlap", [False, True], ids=["free-race", "forced-overlap"])
def test_concurrent_duplicate_creation_creates_one_remote_branch(
    git_project, admin_client, git_env, provider, monkeypatch, forced_overlap
):
    """Two workers race through the service layer; exactly one record wins and
    only one remote branch exists.

    In a free race the scheduler decides which correct path the loser takes:
    it either reads the winner's committed row (idempotent reuse, created=False)
    or passes the existence check first and is stopped by the partial unique
    index; the winner may in turn adopt a remote branch the loser just created.
    The forced-overlap run holds both workers after the local-record check, so
    the unique-index path is exercised on every run.
    """
    from app.database import SessionLocal
    from app.git import branches as branches_module
    from app.git.branches import BranchTarget, create_branch
    from app.models import ProjectRepository, User
    from app.routes.git import _repository_identity_from_provider

    project = git_project["project"]
    story = _make_story(admin_client, project["id"], "Racy story")
    metadata = provider.repositories[0]
    # The direct service call uses the repository's own default base branch,
    # which is the provider's "trunk" here.
    provider.base_shas["trunk"] = BASE_SHA

    with SessionLocal() as db:
        user = db.scalars(select(User).where(User.email == ADMIN_EMAIL)).first()
        repo = _repository_identity_from_provider(
            db, project_id=project["id"], metadata=metadata, config=None
        )
        db.commit()
        repo_id, user_id = repo.id, user.id

    target = BranchTarget(
        item_type="Story",
        item_id=story["id"],
        project_id=project["id"],
        title=story["title"],
        display_id=f"USRSTR-{story['id']}",
        assignee_ids=frozenset(),
    )

    results = []
    barrier = threading.Barrier(2)
    if forced_overlap:
        overlap = threading.Barrier(2, timeout=10)
        original_find = branches_module.find_existing_branch

        def find_then_wait(*args, **kwargs):
            found = original_find(*args, **kwargs)
            overlap.wait()
            return found

        monkeypatch.setattr(branches_module, "find_existing_branch", find_then_wait)

    def worker():
        barrier.wait()
        with SessionLocal() as db:
            repo = db.get(ProjectRepository, repo_id)
            user = db.get(User, user_id)
            try:
                _row, created = create_branch(
                    db, provider, target=target, repository=repo, actor=user
                )
                db.commit()
                results.append("created" if created else "reused")
            except IntegrityError:
                db.rollback()
                results.append("lost-race")

    threads = [threading.Thread(target=worker) for _ in range(2)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=30)
    if forced_overlap:
        # Only the two workers meet at the overlap barrier; the follow-up
        # route call below must see the real function again.
        monkeypatch.setattr(branches_module, "find_existing_branch", original_find)

    # Whichever order the scheduler picks, exactly one insert loses to the unique
    # index or reuses the winner's row. The winner either created the remote branch
    # itself or adopted the one the loser had just created (the remote-existence
    # reconciliation), so "lost-race" + "reused" is as valid as the other two.
    assert sorted(results) in (
        ["created", "lost-race"],
        ["created", "reused"],
        ["lost-race", "reused"],
    )
    assert [
        branch for (_, _, branch) in provider.remote_branches
        if branch.startswith(f"feature_{story['id']}_")
    ] != []
    assert len(provider.remote_branches) == 1

    # The route now sees exactly one existing branch and returns it (200).
    follow_up = _post_branch(admin_client, story["id"])
    assert follow_up.status_code == 200, follow_up.text
    rows = admin_client.get(
        f"/api/git/work-items/{story['id']}/branches"
    ).json()["branches"]
    assert len(rows) == 1


# ===========================================================================
# Project scoping and isolation
# ===========================================================================

def test_branch_project_follows_the_story_not_the_caller(
    git_project, admin_client, git_env
):
    other = _make_project(admin_client, "Other Project")
    assert admin_client.put(
        f"/api/git/projects/{other['id']}/config",
        json={"enabled": True, "base_url": "https://api.github.com",
              "organization": "acme-org", "default_base_branch": "dev"},
    ).status_code == 200
    story = _make_story(admin_client, other["id"], "Other project story")

    response = _post_branch(admin_client, story["id"])
    assert response.status_code == 201, response.text
    rows = admin_client.get(
        f"/api/git/work-items/{story['id']}/branches"
    ).json()["branches"]
    assert rows[0]["project_id"] == other["id"]


def test_story_of_project_without_access_is_invisible(
    git_project, admin_client, git_env
):
    project = git_project["project"]
    story = _make_story(admin_client, project["id"], "Scoped story")
    _make_user(admin_client, "Outsider", "outsider@hard.local")
    _login(admin_client, "outsider@hard.local")
    assert admin_client.get(
        f"/api/git/work-items/{story['id']}/branches"
    ).status_code == 404
    assert _post_branch(admin_client, story["id"]).status_code == 404


def test_removed_branch_history_is_never_deleted(
    git_project, admin_client, git_env, provider
):
    from app.database import SessionLocal
    from app.models import WorkItemBranch

    project = git_project["project"]
    story = _make_story(admin_client, project["id"], "History kept")
    created = _post_branch(admin_client, story["id"])
    assert created.status_code == 201, created.text
    rows = admin_client.get(
        f"/api/git/work-items/{story['id']}/branches"
    ).json()["branches"]
    branch_id, version = rows[0]["id"], rows[0]["version"]

    response = admin_client.delete(
        f"/api/git/branches/{branch_id}?expected_version={version}"
    )
    assert response.status_code == 200, response.text

    with SessionLocal() as db:
        row = db.get(WorkItemBranch, branch_id)
        assert row is not None  # retained, never hard-deleted
        assert row.status == "Deleted"
        assert row.branch_name == created.json()["branch_name"]
        assert row.created_at is not None  # creation history preserved
    assert _table_count(WorkItemBranch) == 1
