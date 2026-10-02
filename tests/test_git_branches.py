"""Focused tests for GitHub Enterprise feature branches (Story-only contract).

Every provider call is mocked: `app.routes.git.build_provider` is replaced with
an in-memory fake, so nothing here contacts a real organization, repository, or
GitHub Enterprise host and no live credentials are required.

Scope covered: branch naming, project Git configuration authorization, read-only
repository discovery, the removed legacy repository-management endpoints, strict
create input, the read-only preview, Story-only enforcement, the create/reconcile
flow, provider-id repository identity, idempotency, provider failure handling,
audit rows, and the exact tracked branch removal.
"""
from __future__ import annotations

import threading

import pytest
from sqlalchemy import select

from app.git.naming import (
    TITLE_SLUG_CHARS,
    UnsupportedItemType,
    build_branch_name,
    normalize_branch_title,
)
from app.git.provider import (
    AUTH_FAILED,
    NOT_FOUND,
    RATE_LIMITED,
    TIMEOUT,
    ProviderRepository,
)

ADMIN_EMAIL = "admin@test.local"
ADMIN_PASSWORD = "Admin1234"
PASSWORD = "Pass12345"

BASE_SHA = "b" * 40
REMOTE_SHA = "d" * 40


def _provider_error(code: str, message: str):
    """Build a GitProviderError of the CURRENTLY loaded app.git.provider."""
    from app.git.provider import GitProviderError

    return GitProviderError(code, message)

# ---------------------------------------------------------------------------
# Fake provider (never touches the network)
# ---------------------------------------------------------------------------

class FakeProvider:
    """In-memory `GitProvider`. Records every call for assertions."""

    def __init__(self) -> None:
        self.calls: list[str] = []
        self.repositories = [
            ProviderRepository(
                provider_repo_id="42",
                name="web-app",
                owner="acme",
                url="https://ghe.example.local/acme/web-app",
                default_branch="trunk",
            )
        ]
        # Base-branch tips; an empty dict simulates a missing base branch.
        self.base_shas = {"dev": BASE_SHA, "main": "c" * 40}
        # Remote branches that already exist, keyed by (owner, name, branch) so
        # two repositories never collide on the same branch name.
        self.remote_branches: dict[tuple[str, str, str], str] = {}
        # method name -> exception raised when that method is called
        self.fail_on: dict[str, Exception] = {}

    def _record(self, name: str) -> None:
        failure = self.fail_on.get(name)
        if failure is not None:
            raise failure
        self.calls.append(name)

    def _find(self, provider_repo_id: str) -> ProviderRepository:
        for repository in self.repositories:
            if repository.provider_repo_id == provider_repo_id:
                return repository
        raise _provider_error(NOT_FOUND, "Repository was not found")

    # --- GitProvider -----------------------------------------------------

    def validate_connection(self) -> str:
        self._record("validate_connection")
        return "acme"

    def list_repositories(self, *, limit: int = 100) -> list[ProviderRepository]:
        self._record("list_repositories")
        return list(self.repositories)[:limit]

    def get_branch_sha(self, owner: str, name: str, branch: str) -> str | None:
        self._record("get_branch_sha")
        if (owner, name, branch) in self.remote_branches:
            return self.remote_branches[(owner, name, branch)]
        return self.base_shas.get(branch)

    def branch_exists(self, owner: str, name: str, branch: str) -> bool:
        self._record("branch_exists")
        return (owner, name, branch) in self.remote_branches

    def create_branch(self, owner: str, name: str, branch: str, sha: str) -> str:
        self._record("create_branch")
        self.remote_branches[(owner, name, branch)] = sha
        return sha

    def delete_branch(self, owner: str, name: str, full_ref: str) -> bool:
        self._record("delete_branch")
        prefix = "refs/heads/"
        short_ref = full_ref[len(prefix):] if full_ref.startswith(prefix) else full_ref
        self.remote_branches.pop((owner, name, short_ref), None)
        return True

    def build_branch_url(self, owner: str, name: str, branch: str) -> str:
        return f"https://ghe.example.local/{owner}/{name}/tree/{branch}"

# ===========================================================================
# 1. Branch naming (pure, no client)
# ===========================================================================

@pytest.mark.parametrize(
    "item_type,item_id,title,expected",
    [
        ("Story", 125, "Login Page", "feature_125_login-page"),
        ("User Story", 125, "Login Page", "feature_125_login-page"),
    ],
)
def test_branch_name_for_user_stories(item_type, item_id, title, expected):
    assert build_branch_name(item_type, title, item_id=item_id) == expected


def test_branch_name_uses_the_stable_story_number_not_the_display_string():
    assert (
        build_branch_name("Story", "Stored id", item_id=7, display_id="USRSTR-7")
        == "feature_7_stored-id"
    )


def test_branch_name_strips_unsafe_characters():
    title = "Fix ../etc/passwd~^:?*[\\] @{x} \"quoted\""
    name = build_branch_name("Story", title, item_id=7)
    assert name.startswith("feature_7_")
    for char in "/\\~^:?*[ ]\"@{}":
        assert char not in name
    assert ".." not in name
    # Deterministic: the same work item and title always produce the same ref.
    assert name == build_branch_name("Story", title, item_id=7)


def test_branch_name_slug_uses_the_first_25_characters():
    assert TITLE_SLUG_CHARS == 25
    long_title = "abcdefghij" * 5  # 50 characters
    name = build_branch_name("Story", long_title, item_id=9)
    assert name == "feature_9_abcdefghijabcdefghijabcde"  # first 25 chars only
    # Exactly at the boundary the whole title is used.
    assert build_branch_name("Story", "a" * 25, item_id=9) == "feature_9_" + "a" * 25


def test_branch_name_is_unicode_safe():
    title = "\u00c4nderung \u00fcber Stra\u00dfe"  # non-ASCII title
    name = build_branch_name("Story", title, item_id=11)
    assert name == build_branch_name("Story", title, item_id=11)
    assert name.startswith("feature_11_")
    for char in name:
        assert char.isascii(), char


def test_branch_name_falls_back_when_the_title_normalizes_to_nothing():
    # The fallback slug keeps the ref readable even for a symbols-only title.
    assert normalize_branch_title("  ***  ") == "story"
    assert build_branch_name("Story", "  ***  ", item_id=9).endswith("_story")
    assert build_branch_name("Story", None, item_id=9).endswith("_story")
    assert build_branch_name("Story", "", item_id=9).endswith("_story")


def test_branch_name_rejects_unsupported_item_types():
    for item_type in (
        "Bug", "Requirement", "Task", "Sub-task", "Feature",
        "Epic", "Sprint", "Collection", "Project", None, "",
    ):
        with pytest.raises(UnsupportedItemType):
            build_branch_name(item_type, "anything", item_id=1)

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture
def provider() -> FakeProvider:
    return FakeProvider()


@pytest.fixture
def git_env(admin_client, monkeypatch, provider):
    """Turn branch actions on for this test and swap in the fake provider."""
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
    """Project with an enabled Git integration (no allow-list involved)."""
    project = _make_project(admin_client, "Branch Project")
    response = admin_client.put(
        f"/api/git/projects/{project['id']}/config",
        json={"enabled": True, "default_base_branch": "dev"},
    )
    assert response.status_code == 200, response.text
    return {"project": project}


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_project(client, name, color="#c9764f"):
    response = client.post(
        "/api/projects", json={"name": name, "description": "", "color": color}
    )
    assert response.status_code == 201, response.text
    return response.json()


def _make_user(client, name, email, role="user", password=PASSWORD, project_ids=None):
    payload = {"name": name, "email": email, "role": role, "password": password}
    if project_ids:
        payload["project_ids"] = list(project_ids)
    response = client.post("/api/users", json=payload)
    assert response.status_code == 201, response.text
    return response.json()


def _login(client, email, password=PASSWORD):
    response = client.post(
        "/api/auth/login", json={"email": email, "password": password}
    )
    assert response.status_code == 200, response.text
    return client


def _login_admin(client):
    return _login(client, ADMIN_EMAIL, ADMIN_PASSWORD)


def _make_bug(client, project_id, title, **extra):
    body = {
        "project_id": project_id,
        "title": title,
        "priority": "Medium",
        "environment": "DEV",
    }
    body.update(extra)
    response = client.post("/api/bugs", json=body)
    assert response.status_code == 201, response.text
    return response.json()


def _enable_agile(client, project_id):
    response = client.post(
        f"/api/agile/projects/{project_id}/enable", json={"feature_flags": {}}
    )
    assert response.status_code in (200, 201), response.text
    return response.json()


def _make_story(client, project_id, title, **extra):
    """A persisted User Story: the only branchable work-item type."""
    _enable_agile(client, project_id)
    assignees = extra.pop("assignee_ids", None)
    response = client.post(
        "/api/agile/work-items",
        json={"project_id": project_id, "title": title, "item_type": "story"},
    )
    assert response.status_code == 201, response.text
    story = response.json()
    if assignees:
        patched = client.put(
            f"/api/bugs/{story['id']}", json={"assignee_ids": assignees}
        )
        assert patched.status_code == 200, patched.text
        story = patched.json()
    return story


def _post_branch(client, work_item_id, provider_repo_id="42", **extra):
    """The only approved creation input: provider id + base branch."""
    payload = {"provider_repo_id": provider_repo_id, "base_branch": "dev"}
    payload.update(extra)
    return client.post(f"/api/git/work-items/{work_item_id}/branches", json=payload)


def _preview_branch(client, work_item_id, provider_repo_id="42", **params):
    query = f"provider_repo_id={provider_repo_id}&base_branch=dev"
    for key, value in params.items():
        query += f"&{key}={value}"
    return client.get(
        f"/api/git/work-items/{work_item_id}/branches/preview?{query}"
    )


def _audit_rows(action=None):
    """Read audit rows from the same temp database the app is using."""
    from app.database import SessionLocal
    from app.models import Activity

    with SessionLocal() as db:
        stmt = select(Activity)
        if action:
            stmt = stmt.where(Activity.action == action)
        return list(db.scalars(stmt).all())


def _table_count(model):
    from app.database import SessionLocal

    with SessionLocal() as db:
        return len(list(db.scalars(select(model)).all()))

# ===========================================================================
# 4. Strict request input and the read-only preview
# ===========================================================================

def test_create_rejects_the_legacy_repository_id_and_extra_fields(
    git_project, admin_client, git_env, provider
):
    project = git_project["project"]
    story = _make_story(admin_client, project["id"], "Strict input")
    response = admin_client.post(
        f"/api/git/work-items/{story['id']}/branches",
        json={"repository_id": 5, "provider_repo_id": "42", "base_branch": "dev"},
    )
    assert response.status_code == 422, response.text
    response = admin_client.post(
        f"/api/git/work-items/{story['id']}/branches",
        json={"provider_repo_id": "42", "base_branch": "dev", "branch_name": "evil"},
    )
    assert response.status_code == 422, response.text
    assert provider.calls == []


def test_create_trims_whitespace_and_validates_the_base_branch(
    git_project, admin_client, git_env
):
    project = git_project["project"]
    story = _make_story(admin_client, project["id"], "Trimmed input")
    ok = _post_branch(admin_client, story["id"], "42", base_branch=" dev ")
    assert ok.status_code == 201, ok.text
    assert ok.json()["base_branch"] == "dev"
    story2 = _make_story(admin_client, project["id"], "Bad base branch")
    bad = _post_branch(admin_client, story2["id"], "42", base_branch="../etc")
    assert bad.status_code == 422, bad.text


def test_preview_is_read_only_and_never_creates_state(
    git_project, admin_client, git_env
):
    from app.models import Activity, ProjectRepository, WorkItemBranch

    project = git_project["project"]
    story = _make_story(admin_client, project["id"], "Read-only preview")
    before_branches = _table_count(ProjectRepository)
    before_items = _table_count(WorkItemBranch)
    before_activity = _table_count(Activity)
    response = _preview_branch(admin_client, story["id"])
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["can_create"] is True
    assert body["repository_full_name"] == "acme/web-app"
    assert body["branch_name"].startswith("feature_")
    assert "repository_id" not in body
    assert _table_count(ProjectRepository) == before_branches
    assert _table_count(WorkItemBranch) == before_items
    assert _table_count(Activity) == before_activity


def test_preview_reports_an_existing_branch_without_creating_one(
    git_project, admin_client, git_env
):
    project = git_project["project"]
    story = _make_story(admin_client, project["id"], "Existing branch preview")
    created = _post_branch(admin_client, story["id"])
    assert created.status_code == 201, created.text
    response = _preview_branch(admin_client, story["id"])
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["can_create"] is False
    assert body["existing_branch"]["branch_name"] == created.json()["branch_name"]


# ===========================================================================
# 5. Story-only enforcement (preview and create, zero provider calls)
# ===========================================================================

STORY_ONLY_MESSAGE = "Feature branches can be created only for User Stories."


def test_every_non_story_type_is_rejected_before_any_provider_call(
    git_project, admin_client, git_env, provider
):
    project = git_project["project"]
    for item_type in ("Bug", "Requirement", "Task"):
        item = _make_bug(admin_client, project["id"], f"Not a story {item_type}",
                         **{"item_type": item_type})
        preview = _preview_branch(admin_client, item["id"])
        assert preview.status_code == 422, preview.text
        assert preview.json()["detail"] == STORY_ONLY_MESSAGE
        assert preview.headers.get("X-Error-Code") == "user_story_only"
        created = _post_branch(admin_client, item["id"])
        assert created.status_code == 422, created.text
        assert created.json()["detail"] == STORY_ONLY_MESSAGE
    assert provider.calls == []

# ===========================================================================
# 2. Project Git configuration authorization (unchanged surface)
# ===========================================================================

def test_git_config_requires_a_project_administrator(git_project, admin_client, git_env):
    project = git_project["project"]
    _make_user(admin_client, "Member", "member@git.local", project_ids=[project["id"]])
    _login(admin_client, "member@git.local")
    response = admin_client.get(f"/api/git/projects/{project['id']}/config")
    assert response.status_code == 403, response.text
    _login_admin(admin_client)
    assert admin_client.get(
        f"/api/git/projects/{project['id']}/config"
    ).status_code == 200


def test_git_config_hides_the_credential_and_reports_it_present(
    git_project, admin_client, git_env, monkeypatch
):
    import base64

    from app.config import Settings

    # A structurally valid Fernet key, so a project credential CAN be stored.
    monkeypatch.setattr(
        Settings,
        "GIT_CREDENTIAL_ENCRYPTION_KEY",
        base64.urlsafe_b64encode(b"k" * 32).decode(),
    )
    project = git_project["project"]
    response = admin_client.put(
        f"/api/git/projects/{project['id']}/config",
        json={"enabled": True, "credential": "ghp_supersecretvalue"},
    )
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["credentials_configured"] is True
    assert "credential" not in body
    assert "ghp_supersecretvalue" not in response.text


# ===========================================================================
# 3. Read-only repository discovery; legacy management endpoints are gone
# ===========================================================================

def test_discovery_lists_safe_provider_metadata_only(git_project, admin_client, git_env):
    project = git_project["project"]
    response = admin_client.get(
        f"/api/git/projects/{project['id']}/available-repositories"
    )
    assert response.status_code == 200, response.text
    assert response.json() == [{
        "provider_repo_id": "42",
        "name": "web-app",
        "owner": "acme",
        "full_name": "acme/web-app",
        "url": "https://ghe.example.local/acme/web-app",
        "default_branch": "trunk",
    }]


def test_discovery_requires_an_enabled_integration(git_project, admin_client, git_env):
    project = git_project["project"]
    assert admin_client.put(
        f"/api/git/projects/{project['id']}/config", json={"enabled": False}
    ).status_code == 200
    assert admin_client.get(
        f"/api/git/projects/{project['id']}/available-repositories"
    ).status_code == 409


def test_discovery_requires_project_membership(git_project, admin_client, git_env):
    project = git_project["project"]
    _make_user(admin_client, "Outsider", "outsider@git.local")
    _login(admin_client, "outsider@git.local")
    assert admin_client.get(
        f"/api/git/projects/{project['id']}/available-repositories"
    ).status_code == 404


def test_discovery_performs_no_database_writes(git_project, admin_client, git_env):
    from app.models import ProjectRepository, WorkItemBranch

    project = git_project["project"]
    assert admin_client.get(
        f"/api/git/projects/{project['id']}/available-repositories"
    ).status_code == 200
    assert _table_count(ProjectRepository) == 0
    assert _table_count(WorkItemBranch) == 0
    actions = {row.action for row in _audit_rows()}
    assert "git_repositories_discovered" in actions
    assert "git_repository_identity_created" not in actions
    # No credential material ever reaches an audit row or a response.
    for row in _audit_rows():
        assert "test-pat" not in row.detail


def test_discovery_audits_safe_failures_without_exposing_the_provider_error(
    git_project, admin_client, git_env, provider
):
    project = git_project["project"]
    provider.fail_on["list_repositories"] = _provider_error(
        AUTH_FAILED, "Bad credentials token ghp_abcsecret1234"
    )
    response = admin_client.get(
        f"/api/git/projects/{project['id']}/available-repositories"
    )
    assert response.status_code == 502, response.text
    assert "ghp_abcsecret1234" not in response.text
    failed = _audit_rows("git_repositories_discovery_failed")
    assert len(failed) == 1
    assert "ghp_abcsecret1234" not in failed[0].detail


def test_legacy_repository_management_endpoints_are_gone(
    git_project, admin_client, git_env
):
    project = git_project["project"]
    base = f"/api/git/projects/{project['id']}"
    assert admin_client.get(f"{base}/repositories").status_code == 404
    assert admin_client.get(f"{base}/repositories/selectable").status_code == 404
    assert admin_client.post(
        f"{base}/repositories", json={"owner": "acme", "name": "web-app"}
    ).status_code == 404
    assert admin_client.put(
        f"{base}/repositories/1", json={"active": False}
    ).status_code == 404
    assert admin_client.post(f"{base}/repositories/1/test").status_code == 404
    assert admin_client.delete(f"{base}/repositories/1").status_code == 404

# ===========================================================================
# 6. Creation: idempotency, latest SHA, reconciliation, multiple repositories
# ===========================================================================

def test_story_can_create_one_branch_per_repository(git_project, admin_client, git_env, provider):
    project = git_project["project"]
    provider.repositories.append(
        ProviderRepository(
            provider_repo_id="43",
            name="mobile-app",
            owner="acme",
            url="https://ghe.example.local/acme/mobile-app",
            default_branch="trunk",
        )
    )
    story = _make_story(admin_client, project["id"], "Two repositories")
    first = _post_branch(admin_client, story["id"], "42")
    assert first.status_code == 201, first.text
    second = _post_branch(admin_client, story["id"], "43")
    assert second.status_code == 201, second.text
    rows = admin_client.get(f"/api/git/work-items/{story['id']}/branches").json()["branches"]
    assert len(rows) == 2
    assert {row["provider_repo_id"] for row in rows} == {"42", "43"}


def test_duplicate_create_is_idempotent_and_returns_the_existing_record(
    git_project, admin_client, git_env, provider
):
    project = git_project["project"]
    story = _make_story(admin_client, project["id"], "Idempotent create")
    first = _post_branch(admin_client, story["id"])
    assert first.status_code == 201, first.text
    second = _post_branch(admin_client, story["id"])
    assert second.status_code == 200, second.text
    assert second.json()["id"] == first.json()["id"]
    assert provider.calls.count("create_branch") == 1


def test_create_uses_the_latest_base_sha_immediately_before_creating(
    git_project, admin_client, git_env, provider
):
    project = git_project["project"]
    story = _make_story(admin_client, project["id"], "Latest SHA")
    # The base tip moves between discovery and creation.
    provider.base_shas["dev"] = "e" * 40
    response = _post_branch(admin_client, story["id"])
    assert response.status_code == 201, response.text
    assert response.json()["base_commit_sha"] == "e" * 40


def test_remote_existing_local_missing_is_adopted(git_project, admin_client, git_env, provider):
    from app.models import WorkItemBranch

    project = git_project["project"]
    story = _make_story(admin_client, project["id"], "Adopt remote")
    name = build_branch_name("Story", story["title"], item_id=story["id"])
    provider.remote_branches[("acme", "web-app", name)] = REMOTE_SHA
    response = _post_branch(admin_client, story["id"])
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["branch_name"] == name
    assert body["base_commit_sha"] == REMOTE_SHA
    assert provider.calls.count("create_branch") == 0
    assert _table_count(WorkItemBranch) == 1


def test_lost_provider_response_is_reconciled_not_duplicated(
    git_project, admin_client, git_env, provider
):
    class LostResponseProvider(FakeProvider):
        def create_branch(self, owner, name, branch, sha):
            self._record("create_branch")
            self.remote_branches[(owner, name, branch)] = sha  # remote committed
            raise _provider_error(TIMEOUT, "Request timed out after the ref was created")

    import app.routes.git as git_routes

    lost = LostResponseProvider()
    git_routes.build_provider = (  # type: ignore[method-assign]
        lambda config, base_url=None, organization=None, token=None: lost
    )
    project = git_project["project"]
    story = _make_story(admin_client, project["id"], "Lost response")
    response = _post_branch(admin_client, story["id"])
    assert response.status_code == 201, response.text
    assert lost.calls.count("create_branch") == 1
    assert lost.remote_branches  # the branch really exists remotely


def test_db_failure_after_remote_create_recovers_on_retry(
    git_project, admin_client, git_env, provider, monkeypatch
):
    import app.git.branches as branch_svc

    project = git_project["project"]
    story = _make_story(admin_client, project["id"], "DB failure recovery")
    original_persist = branch_svc._persist
    state = {"calls": 0}

    def flaky_persist(*args, **kwargs):
        state["calls"] += 1
        if state["calls"] == 1:
            from sqlalchemy.exc import IntegrityError

            raise IntegrityError("stmt", {}, Exception("db gone"))
        return original_persist(*args, **kwargs)

    monkeypatch.setattr(branch_svc, "_persist", flaky_persist)
    first = _post_branch(admin_client, story["id"])
    assert first.status_code == 503, first.text
    # The remote branch exists; a retry adopts it instead of failing forever.
    second = _post_branch(admin_client, story["id"])
    assert second.status_code == 200, second.text
    assert provider.calls.count("create_branch") == 1


def test_provider_failure_is_translated_and_audited_without_a_row(
    git_project, admin_client, git_env, provider
):
    from app.models import WorkItemBranch

    project = git_project["project"]
    story = _make_story(admin_client, project["id"], "Provider down")
    provider.fail_on["get_branch_sha"] = _provider_error(
        RATE_LIMITED, "API rate limit exceeded token ghp_ratelimited1"
    )
    response = _post_branch(admin_client, story["id"])
    assert response.status_code == 503, response.text
    assert "ghp_ratelimited1" not in response.text
    assert _table_count(WorkItemBranch) == 0
    failed = _audit_rows("git_branch_creation_failed")
    assert len(failed) == 1
    assert "ghp_ratelimited1" not in failed[0].detail


def test_missing_base_branch_is_a_409_with_a_human_message(
    git_project, admin_client, git_env
):
    project = git_project["project"]
    story = _make_story(admin_client, project["id"], "Missing base")
    response = _post_branch(admin_client, story["id"], "42", base_branch="ghost")
    assert response.status_code == 409, response.text
    assert "ghost" in response.json()["detail"]


def test_unknown_provider_repository_is_a_404(git_project, admin_client, git_env, provider):
    project = git_project["project"]
    story = _make_story(admin_client, project["id"], "Unknown repo")
    response = _post_branch(admin_client, story["id"], "999")
    assert response.status_code == 404, response.text
    assert provider.calls.count("create_branch") == 0


def test_creation_records_a_safe_audit_trail(git_project, admin_client, git_env):
    project = git_project["project"]
    story = _make_story(admin_client, project["id"], "Audited create")
    response = _post_branch(admin_client, story["id"])
    assert response.status_code == 201, response.text
    rows = _audit_rows("git_branch_created")
    assert len(rows) == 1
    detail = rows[0].detail
    assert f"work_item=Story#{story['id']}" in detail
    assert "repository=acme/web-app" in detail

# ===========================================================================
# 7. Permissions and project isolation
# ===========================================================================

def test_assignee_can_create_a_branch(git_project, admin_client, git_env, provider):
    project = git_project["project"]
    alice = _make_user(admin_client, "Alice", "alice@git.local",
                       project_ids=[project["id"]])
    story = _make_story(admin_client, project["id"], "Assignee branch",
                        assignee_ids=[alice["id"]])
    _login(admin_client, "alice@git.local")
    response = _post_branch(admin_client, story["id"])
    assert response.status_code == 201, response.text
    body = response.json()
    assert body["branch_name"] == f"feature_{story['id']}_assignee-branch"
    assert body["display_id"] == f"USRSTR-{story['id']}"
    assert body["base_branch"] == "dev"
    assert body["base_commit_sha"] == BASE_SHA
    assert body["status"] == "Active"
    assert body["provider_branch_ref"] == f"refs/heads/{body['branch_name']}"
    assert body["created_by_name"] == "Alice"
    assert provider.calls.count("create_branch") == 1


def test_unassigned_project_member_is_rejected_and_admin_overrides(
    git_project, admin_client, git_env, provider
):
    project = git_project["project"]
    _make_user(admin_client, "Carol", "carol@git.local", project_ids=[project["id"]])
    story = _make_story(admin_client, project["id"], "Unassigned member")
    _login(admin_client, "carol@git.local")
    assert _post_branch(admin_client, story["id"]).status_code == 403
    _login_admin(admin_client)
    assert _post_branch(admin_client, story["id"]).status_code == 201


def test_work_item_without_project_access_is_invisible(git_project, admin_client, git_env):
    project = git_project["project"]
    story = _make_story(admin_client, project["id"], "Scoped story")
    _make_user(admin_client, "Outsider", "outsider@git.local")
    _login(admin_client, "outsider@git.local")
    assert admin_client.get(
        f"/api/git/work-items/{story['id']}/branches"
    ).status_code == 404
    assert _post_branch(admin_client, story["id"]).status_code == 404


# ===========================================================================
# 8. Provider-id repository identity (branch-history only)
# ===========================================================================

def _identity_rows(project_id):
    from app.database import SessionLocal
    from app.models import ProjectRepository

    with SessionLocal() as db:
        return list(db.scalars(
            select(ProjectRepository)
            .where(ProjectRepository.project_id == project_id)
            .order_by(ProjectRepository.id)
        ).all())


def test_same_name_different_owner_creates_distinct_identity_rows(
    git_project, admin_client, git_env, provider
):

    project = git_project["project"]
    provider.repositories = [
        ProviderRepository(
            provider_repo_id="42", name="web-app", owner="acme",
            url="https://ghe.example.local/acme/web-app", default_branch="trunk",
        ),
        ProviderRepository(
            provider_repo_id="43", name="web-app", owner="other",
            url="https://ghe.example.local/other/web-app", default_branch="trunk",
        ),
    ]
    story = _make_story(admin_client, project["id"], "Owner namespacing")
    assert _post_branch(admin_client, story["id"], "42").status_code == 201
    assert _post_branch(admin_client, story["id"], "43").status_code == 201
    rows = _identity_rows(project["id"])
    assert len(rows) == 2
    assert {row.provider_repo_id for row in rows} == {"42", "43"}
    assert {(row.owner, row.name) for row in rows} == {("acme", "web-app"), ("other", "web-app")}


def test_rename_keeps_the_identity_row_and_branch_history(
    git_project, admin_client, git_env, provider
):
    from app.models import WorkItemBranch

    project = git_project["project"]
    story = _make_story(admin_client, project["id"], "Rename repo")
    created = _post_branch(admin_client, story["id"])
    assert created.status_code == 201, created.text
    branch_row_id = created.json()["id"]
    original_identity_id = _identity_rows(project["id"])[0].id

    provider.repositories[0] = ProviderRepository(
        provider_repo_id="42", name="renamed-app", owner="acme",
        url="https://ghe.example.local/acme/renamed-app", default_branch="trunk",
    )
    story2 = _make_story(admin_client, project["id"], "After rename")
    assert _post_branch(admin_client, story2["id"]).status_code == 201
    rows = _identity_rows(project["id"])
    assert len(rows) == 1
    assert rows[0].id == original_identity_id
    assert rows[0].name == "renamed-app"
    with_git = admin_client.get(
        f"/api/git/work-items/{story['id']}/branches"
    ).json()["branches"]
    assert with_git[0]["id"] == branch_row_id
    assert with_git[0]["repository_full_name"] == "acme/renamed-app"
    assert _table_count(WorkItemBranch) == 2


def test_legacy_blank_provider_id_row_is_reconciled(
    git_project, admin_client, git_env, provider
):
    from app.database import SessionLocal
    from app.models import ProjectRepository

    project = git_project["project"]
    with SessionLocal() as db:
        legacy = ProjectRepository(
            project_id=project["id"], provider="github_enterprise",
            provider_repo_id="", name="web-app", owner="acme",
            url="", default_base_branch="dev", active=True,
            branch_creation_enabled=True,
        )
        db.add(legacy)
        db.commit()
        legacy_id = legacy.id
    story = _make_story(admin_client, project["id"], "Legacy reconcile")
    response = _post_branch(admin_client, story["id"])
    assert response.status_code == 201, response.text
    rows = _identity_rows(project["id"])
    assert len(rows) == 1
    assert rows[0].id == legacy_id
    assert rows[0].provider_repo_id == "42"
    assert rows[0].url == "https://ghe.example.local/acme/web-app"


def test_concurrent_identity_creation_creates_exactly_one_row(
    git_project, admin_client, git_env, provider
):
    from app.database import SessionLocal
    from app.models import ProjectRepository
    from app.routes.git import _repository_identity_from_provider

    project = git_project["project"]
    metadata = provider.repositories[0]
    config = None
    results: list[int] = []
    barrier = threading.Barrier(2)

    def worker():
        barrier.wait()
        with SessionLocal() as db:
            row = _repository_identity_from_provider(
                db, project_id=project["id"], metadata=metadata, config=config
            )
            db.commit()
            results.append(row.id)

    threads = [threading.Thread(target=worker) for _ in range(2)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=30)
    assert len(set(results)) == 1
    assert _table_count(ProjectRepository) == 1


def test_confirmed_creation_creates_the_identity_row_preview_does_not(
    git_project, admin_client, git_env
):
    from app.models import ProjectRepository

    project = git_project["project"]
    story = _make_story(admin_client, project["id"], "Identity lifecycle")
    assert _preview_branch(admin_client, story["id"]).status_code == 200
    assert _table_count(ProjectRepository) == 0
    assert _post_branch(admin_client, story["id"]).status_code == 201
    assert _table_count(ProjectRepository) == 1

# ===========================================================================
# 9. Exact feature-branch removal
# ===========================================================================

def _branch_record_id(client, work_item_id):
    rows = client.get(f"/api/git/work-items/{work_item_id}/branches").json()["branches"]
    return rows[0]["id"], rows[0]


def test_removal_deletes_exactly_the_stored_remote_branch(
    git_project, admin_client, git_env, provider
):
    from app.database import SessionLocal
    from app.models import WorkItemBranch

    project = git_project["project"]
    story = _make_story(admin_client, project["id"], "Removable branch")
    created = _post_branch(admin_client, story["id"])
    assert created.status_code == 201, created.text
    branch_id, record = _branch_record_id(admin_client, story["id"])

    response = admin_client.delete(
        f"/api/git/branches/{branch_id}?expected_version={record['version']}"
    )
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["status"] == "Deleted"
    assert body["removed_by_name"] == "Test Admin"
    assert body["removal_reason"]
    assert body["removed_at"]
    assert body["version"] == record["version"] + 1
    assert body["created_by_name"] == "Test Admin"
    assert provider.remote_branches == {}

    with SessionLocal() as db:
        row = db.get(WorkItemBranch, branch_id)
        assert row.status == "Deleted"
        assert row.removed_at is not None
    # Deleted history remains visible.
    rows = admin_client.get(
        f"/api/git/work-items/{story['id']}/branches"
    ).json()["branches"]
    assert [r["status"] for r in rows] == ["Deleted"]


def test_removal_treats_exact_404_as_already_absent(
    git_project, admin_client, git_env, provider
):
    class GoneProvider(FakeProvider):
        def delete_branch(self, owner, name, full_ref):
            self._record("delete_branch")
            return False

    import app.routes.git as git_routes

    git_routes.build_provider = (  # type: ignore[method-assign]
        lambda config, base_url=None, organization=None, token=None: GoneProvider()
    )
    project = git_project["project"]
    story = _make_story(admin_client, project["id"], "Gone remote")
    # The remote branch is gone out-of-band; the local record still says Active.
    from app.database import SessionLocal
    from app.models import ProjectRepository, WorkItemBranch

    with SessionLocal() as db:
        repo = ProjectRepository(
            project_id=project["id"], provider="github_enterprise",
            provider_repo_id="42", name="web-app", owner="acme",
            url="https://ghe.example.local/acme/web-app", default_base_branch="dev",
            active=True, branch_creation_enabled=True,
        )
        db.add(repo)
        db.flush()
        row = WorkItemBranch(
            work_item_type="Story", work_item_id=story["id"], project_id=project["id"],
            repository_id=repo.id, provider_repo_id="42",
            branch_name=f"feature_{story['id']}_gone-remote", base_branch="dev",
            base_commit_sha=BASE_SHA, branch_url="https://ghe.example.local/x",
            provider_branch_ref=f"refs/heads/feature_{story['id']}_gone-remote",
            status="Active", created_by_name="Test Admin",
        )
        db.add(row)
        db.commit()
        branch_id = row.id
    response = admin_client.delete(f"/api/git/branches/{branch_id}")
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["status"] == "Deleted"
    # Already-absent is recorded as a reconciliation, never as a fresh delete.
    assert body["removal_reason"] == "removed_by_user_reconciled_absent"


def test_removal_reconciles_when_the_provider_raises_not_found(
    git_project, admin_client, git_env, provider
):
    """A provider that raises NOT_FOUND is also "already absent" — never a failure."""

    class RaisingGoneProvider(FakeProvider):
        def delete_branch(self, owner, name, full_ref):
            self._record("delete_branch")
            raise _provider_error(NOT_FOUND, "Not Found")

    import app.routes.git as git_routes

    git_routes.build_provider = (  # type: ignore[method-assign]
        lambda config, base_url=None, organization=None, token=None: RaisingGoneProvider()
    )
    project = git_project["project"]
    story = _make_story(admin_client, project["id"], "Raised gone remote")
    from app.database import SessionLocal
    from app.models import ProjectRepository, WorkItemBranch

    with SessionLocal() as db:
        repo = ProjectRepository(
            project_id=project["id"], provider="github_enterprise",
            provider_repo_id="42", name="web-app", owner="acme",
            url="https://ghe.example.local/acme/web-app", default_base_branch="dev",
            active=True, branch_creation_enabled=True,
        )
        db.add(repo)
        db.flush()
        row = WorkItemBranch(
            work_item_type="Story", work_item_id=story["id"], project_id=project["id"],
            repository_id=repo.id, provider_repo_id="42",
            branch_name=f"feature_{story['id']}_raised-gone", base_branch="dev",
            base_commit_sha=BASE_SHA, branch_url="https://ghe.example.local/x",
            provider_branch_ref=f"refs/heads/feature_{story['id']}_raised-gone",
            status="Active", created_by_name="Test Admin",
        )
        db.add(row)
        db.commit()
        branch_id = row.id
    response = admin_client.delete(f"/api/git/branches/{branch_id}")
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["status"] == "Deleted"
    assert body["removal_reason"] == "removed_by_user_reconciled_absent"


def test_removal_provider_failure_keeps_the_record_active(
    git_project, admin_client, git_env, provider
):
    project = git_project["project"]
    story = _make_story(admin_client, project["id"], "Failed removal")
    assert _post_branch(admin_client, story["id"]).status_code == 201
    branch_id, record = _branch_record_id(admin_client, story["id"])
    provider.fail_on["delete_branch"] = _provider_error(
        AUTH_FAILED, "Bad credentials token ghp_deletefail1"
    )
    response = admin_client.delete(
        f"/api/git/branches/{branch_id}?expected_version={record['version']}"
    )
    assert response.status_code == 502, response.text
    assert "ghp_deletefail1" not in response.text
    rows = admin_client.get(
        f"/api/git/work-items/{story['id']}/branches"
    ).json()["branches"]
    assert rows[0]["status"] == "Active"
    assert rows[0]["removed_at"] is None


def test_removal_is_available_to_any_project_member(
    git_project, admin_client, git_env, provider
):
    """A user-role member (even unassigned) may remove the story's branch."""
    project = git_project["project"]
    alice = _make_user(admin_client, "Alice", "alice2@git.local",
                       project_ids=[project["id"]])
    story = _make_story(admin_client, project["id"], "Member can remove",
                        assignee_ids=[alice["id"]])
    _login(admin_client, "alice2@git.local")
    created = _post_branch(admin_client, story["id"])
    assert created.status_code == 201, created.text
    branch_id, record = _branch_record_id(admin_client, story["id"])
    response = admin_client.delete(
        f"/api/git/branches/{branch_id}?expected_version={record['version']}"
    )
    # Removal is a project-level action: any project member (user role) may do it.
    assert response.status_code == 200, response.text
    assert response.json()["status"] == "Deleted"
    assert provider.calls.count("delete_branch") == 1


def test_removal_hint_lists_project_member_but_not_outsiders(
    git_project, admin_client, git_env
):
    """can_remove_branch follows project membership, not the role."""
    project = git_project["project"]
    story = _make_story(admin_client, project["id"], "Hint story")
    _make_user(admin_client, "Member", "remover@git.local",
               project_ids=[project["id"]])
    _make_user(admin_client, "Outsider", "outsider-rm@git.local")
    _login(admin_client, "remover@git.local")
    body = admin_client.get(
        f"/api/git/work-items/{story['id']}/branches"
    ).json()
    assert body["can_remove_branch"] is True
    _login(admin_client, "outsider-rm@git.local")
    # Outsiders cannot even see the story: the list is a scoped 404.
    response = admin_client.get(f"/api/git/work-items/{story['id']}/branches")
    assert response.status_code == 404, response.text


def test_removal_version_conflict_is_rejected(
    git_project, admin_client, git_env
):
    project = git_project["project"]
    story = _make_story(admin_client, project["id"], "Version conflict")
    _post_branch(admin_client, story["id"])
    branch_id, _record = _branch_record_id(admin_client, story["id"])
    response = admin_client.delete(f"/api/git/branches/{branch_id}?expected_version=99")
    assert response.status_code == 409, response.text
    assert response.headers.get("X-Error-Code") == "version_conflict"


def test_removal_rejects_protected_base_and_non_feature_records(
    git_project, admin_client, git_env
):
    from app.database import SessionLocal
    from app.models import ProjectRepository, WorkItemBranch

    project = git_project["project"]
    bug = _make_bug(admin_client, project["id"], "Protected record")
    with SessionLocal() as db:
        repo = ProjectRepository(
            project_id=project["id"], provider="github_enterprise",
            provider_repo_id="42", name="web-app", owner="acme",
            url="https://ghe.example.local/acme/web-app", default_base_branch="dev",
            active=True, branch_creation_enabled=True,
        )
        db.add(repo)
        db.flush()
        row = WorkItemBranch(
            work_item_type="Story", work_item_id=bug["id"], project_id=project["id"],
            repository_id=repo.id, provider_repo_id="42",
            branch_name="main", base_branch="dev", base_commit_sha=BASE_SHA,
            branch_url="https://ghe.example.local/x",
            provider_branch_ref="refs/heads/main", status="Active",
        )
        db.add(row)
        db.commit()
        branch_id = row.id
    response = admin_client.delete(f"/api/git/branches/{branch_id}")
    assert response.status_code == 409, response.text


def test_removal_is_story_only(git_project, admin_client, git_env):
    from app.database import SessionLocal
    from app.models import ProjectRepository, WorkItemBranch

    project = git_project["project"]
    bug = _make_bug(admin_client, project["id"], "Bug branch record")
    with SessionLocal() as db:
        repo = ProjectRepository(
            project_id=project["id"], provider="github_enterprise",
            provider_repo_id="42", name="web-app", owner="acme",
            url="https://ghe.example.local/acme/web-app", default_base_branch="dev",
            active=True, branch_creation_enabled=True,
        )
        db.add(repo)
        db.flush()
        row = WorkItemBranch(
            work_item_type="Bug", work_item_id=bug["id"], project_id=project["id"],
            repository_id=repo.id, provider_repo_id="42",
            branch_name="legacy_1", base_branch="dev", base_commit_sha=BASE_SHA,
            branch_url="https://ghe.example.local/x",
            provider_branch_ref="refs/heads/legacy_1", status="Active",
        )
        db.add(row)
        db.commit()
        branch_id = row.id
    response = admin_client.delete(f"/api/git/branches/{branch_id}")
    assert response.status_code == 422, response.text
    assert response.json()["detail"] == STORY_ONLY_MESSAGE


def test_removal_of_a_missing_record_is_a_scoped_404(git_project, admin_client, git_env):
    response = admin_client.delete("/api/git/branches/424242")
    assert response.status_code == 404, response.text
def test_duplicate_removal_deletes_once_and_leaves_other_branches_alone(
    git_project, admin_client, git_env, provider
):
    """A repeated removal is a stable conflict and never re-hits the provider.

    Concurrency guard for the exact-removal contract: the second call must not
    issue a second remote DELETE, and another Story's Active branch in the same
    project must be left completely untouched.
    """
    project = git_project["project"]
    first = _make_story(admin_client, project["id"], "First removable branch")
    second = _make_story(admin_client, project["id"], "Second kept branch")
    assert _post_branch(admin_client, first["id"]).status_code == 201
    assert _post_branch(admin_client, second["id"]).status_code == 201
    branch_id, record = _branch_record_id(admin_client, first["id"])

    removed = admin_client.delete(
        f"/api/git/branches/{branch_id}?expected_version={record['version']}"
    )
    assert removed.status_code == 200, removed.text
    assert provider.calls.count("delete_branch") == 1

    repeat = admin_client.delete(
        f"/api/git/branches/{branch_id}?expected_version={removed.json()['version']}"
    )
    assert repeat.status_code == 409, repeat.text
    assert provider.calls.count("delete_branch") == 1, "a repeat must not re-delete"

    _other_id, other = _branch_record_id(admin_client, second["id"])
    assert other["status"] == "Active"
    assert ("acme", "web-app", other["branch_name"]) in provider.remote_branches
