"""Git credentials only go to the server they belong to, and only to hosts the server may reach.

* The global GITHUB_TOKEN is sent only to GITHUB_API_URL.
* A project's saved token is sent only to the server the project is saved with; a connection
  test against another server, or saving another server, needs the token re-entered.
* The Git server may not resolve to a link-local/metadata address, nor (unless the deployment
  opts in with GIT_ALLOW_PRIVATE_HOSTS) to a private or loopback one. The check runs on the
  address actually connected to, so DNS rebinding cannot bypass it.

Nothing here contacts a real Git host.
"""
from __future__ import annotations

import base64
import ssl
from types import SimpleNamespace

import httpx
import pytest

KEY = base64.urlsafe_b64encode(b"k" * 32).decode()
SAVED_PAT = "saved-project-token-1234"
GLOBAL_PAT = "global-deployment-token-99"


@pytest.fixture
def git(admin_client, monkeypatch):
    from app.config import Settings, get_settings

    monkeypatch.setattr(Settings, "GITHUB_API_URL", "https://api.github.com")
    monkeypatch.setattr(Settings, "GITHUB_ORGANIZATION", "acme")
    monkeypatch.setattr(Settings, "GITHUB_TOKEN", GLOBAL_PAT)
    monkeypatch.setattr(Settings, "GIT_CREDENTIAL_ENCRYPTION_KEY", KEY)
    get_settings.cache_clear()

    from app.git import github

    seen: list[tuple[str, str]] = []

    def _validate(self):
        seen.append((self.api_base, self._token))
        return "acme"

    monkeypatch.setattr(github.GitHubEnterpriseProvider, "validate_connection", _validate)
    project = admin_client.post("/api/projects", json={"name": "Git Bound"}).json()
    yield SimpleNamespace(project=project, seen=seen, url=f"/api/git/projects/{project['id']}/config")
    get_settings.cache_clear()


def _save(client, url, **body):
    return client.put(url, json={"enabled": True, "default_base_branch": "dev", **body})


# --- credentials.resolve_token ------------------------------------------------

def test_global_token_only_goes_to_the_configured_api(git):
    from app.git import credentials

    blank = SimpleNamespace(base_url="", credential_encrypted="")
    assert credentials.resolve_token(blank) == GLOBAL_PAT
    assert credentials.resolve_token(blank, "https://github.com") == GLOBAL_PAT
    assert credentials.resolve_token(blank, "https://attacker.example") is None
    other = SimpleNamespace(base_url="https://ghe.example.com", credential_encrypted="")
    assert credentials.resolve_token(other) is None
    assert credentials.credentials_configured(other) is False


def test_project_token_only_goes_to_its_own_server(git):
    from app.git import credentials

    config = SimpleNamespace(
        base_url="https://ghe.example.com",
        credential_encrypted=credentials.encrypt_credential(SAVED_PAT),
    )
    assert credentials.resolve_token(config) == SAVED_PAT
    assert credentials.resolve_token(config, "https://ghe.example.com/api/v3") == SAVED_PAT
    assert credentials.resolve_token(config, "https://attacker.example") is None
    # A project with its own token never falls back to the global one.
    assert credentials.resolve_token(config, "https://api.github.com") is None


# --- connection test -----------------------------------------------------------

def test_draft_test_against_another_server_never_carries_a_saved_token(git, admin_client):
    assert _save(admin_client, git.url, base_url="https://ghe.example.com",
                 credential=SAVED_PAT).status_code == 200

    res = admin_client.post(f"{git.url}/test", json={"base_url": "https://attacker.example"})
    body = res.json()
    assert res.status_code == 200 and body["ok"] is False
    assert "saved token is only sent to the server it was saved for" in body["message"]
    assert git.seen == []

    # The same draft with a token typed in is tested with that token only.
    res = admin_client.post(f"{git.url}/test", json={
        "base_url": "https://attacker.example", "credential": "typed-for-test-123",
    })
    assert res.json()["ok"] is True
    assert git.seen == [("https://attacker.example/api/v3", "typed-for-test-123")]


def test_draft_test_without_a_project_token_never_carries_the_global_token(git, admin_client):
    assert _save(admin_client, git.url).status_code == 200
    res = admin_client.post(f"{git.url}/test", json={"base_url": "https://attacker.example"})
    assert res.json()["ok"] is False
    assert git.seen == []
    # Against the configured API the global token is used as before.
    res = admin_client.post(f"{git.url}/test", json={"base_url": "https://api.github.com"})
    assert res.json()["ok"] is True
    assert git.seen == [("https://api.github.com", GLOBAL_PAT)]


def test_saved_test_uses_the_saved_token_for_the_saved_server(git, admin_client):
    assert _save(admin_client, git.url, base_url="https://ghe.example.com",
                 credential=SAVED_PAT).status_code == 200
    assert admin_client.post(f"{git.url}/test").json()["ok"] is True
    assert git.seen == [("https://ghe.example.com/api/v3", SAVED_PAT)]


# --- saving the configuration --------------------------------------------------

def test_changing_the_server_requires_the_token_again(git, admin_client):
    first = _save(admin_client, git.url, base_url="https://ghe.example.com", credential=SAVED_PAT)
    assert first.status_code == 200
    moved = _save(admin_client, git.url, base_url="https://attacker.example")
    assert moved.status_code == 422
    assert "Re-enter the access token" in moved.json()["detail"]
    # Re-entering the token (or clearing it) makes the change explicit and allowed.
    assert _save(admin_client, git.url, base_url="https://attacker.example",
                 credential="new-server-token-123").status_code == 200
    assert _save(admin_client, git.url, base_url="https://other.example",
                 clear_credential=True).status_code == 200


@pytest.mark.parametrize("base_url", ["https://127.0.0.1", "https://10.1.2.3", "https://169.254.169.254"])
def test_internal_servers_are_refused(git, admin_client, base_url):
    res = _save(admin_client, git.url, base_url=base_url)
    assert res.status_code == 422, res.text
    assert "GIT_ALLOW_PRIVATE_HOSTS" in res.json()["detail"]


def test_private_servers_are_allowed_when_the_deployment_opts_in(git, admin_client, monkeypatch):
    from app.config import Settings, get_settings

    monkeypatch.setattr(Settings, "GIT_ALLOW_PRIVATE_HOSTS", True)
    get_settings.cache_clear()
    assert _save(admin_client, git.url, base_url="https://10.1.2.3").status_code == 200
    # Metadata endpoints stay unreachable whatever the deployment allows.
    assert _save(admin_client, git.url, base_url="https://169.254.169.254").status_code == 422


def test_a_non_ascii_token_is_refused(git, admin_client):
    res = _save(admin_client, git.url, credential="tökén-1234567")
    assert res.status_code == 422
    assert "ASCII" in res.text


# --- a self-service organization admin (HIGH in the multi-tenant edition) --------

def test_org_admin_of_another_tenant_cannot_exfiltrate_the_deployment_token(git):
    """Anyone can sign up and become admin of their own organization, so the connection test
    is reachable by strangers: the deployment's GITHUB_TOKEN must go only to GITHUB_API_URL
    and never to internal addresses, whatever server such an admin names."""
    from tests.conftest import sign_up

    tenant, _me = sign_up("Org Evil", "evil@evil.test")
    project_id = tenant.get("/api/projects").json()[0]["id"]
    url = f"/api/git/projects/{project_id}/config"
    assert _save(tenant, url).status_code == 200

    res = tenant.post(f"{url}/test", json={"base_url": "https://attacker.example"})
    assert res.status_code == 200 and res.json()["ok"] is False
    assert git.seen == []

    for internal in ("https://169.254.169.254", "https://127.0.0.1", "https://10.1.2.3"):
        assert _save(tenant, url, base_url=internal).status_code == 422
    assert git.seen == []

    # A token the admin types in is theirs to send anywhere; a saved one stays with its server.
    assert _save(tenant, url, base_url="https://ghe.evil.test", credential=SAVED_PAT).status_code == 200
    moved = _save(tenant, url, base_url="https://attacker.example")
    assert moved.status_code == 422 and "Re-enter the access token" in moved.json()["detail"]
    res = tenant.post(f"{url}/test", json={"base_url": "https://attacker.example"})
    assert res.json()["ok"] is False
    assert git.seen == []


# --- the connection itself ------------------------------------------------------

@pytest.mark.parametrize("url", [
    "http://127.0.0.1:9/", "http://169.254.169.254/latest/meta-data", "http://[::ffff:169.254.169.254]/",
])
def test_the_transport_refuses_internal_addresses_at_connect_time(url):
    from app import net_guard

    transport = net_guard.guarded_transport(verify=ssl.create_default_context(), allow_private=False)
    with httpx.Client(transport=transport, trust_env=False, timeout=2) as client:
        with pytest.raises(net_guard.HostNotAllowed):
            client.get(url)


def test_the_provider_reports_a_refused_host_safely(monkeypatch):
    from app import net_guard
    from app.git import github

    monkeypatch.setattr(net_guard, "precheck", lambda url, allow_private: None)

    def _refuse(self, *args, **kwargs):
        raise net_guard.HostNotAllowed("internal")

    monkeypatch.setattr(httpx.Client, "request", _refuse)
    provider = github.GitHubEnterpriseProvider(base_url="https://ghe.example.com", token="tok-x1234567")
    with pytest.raises(github.GitProviderError) as excinfo:
        provider.validate_connection()
    assert excinfo.value.code == github.INVALID_BASE_URL
    assert "GIT_ALLOW_PRIVATE_HOSTS" in excinfo.value.message
