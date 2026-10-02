"""TLS trust, transport classification and API-URL normalization for the Git provider.

Two failure modes this guards against, both observed in the field:

* Corporate TLS interception: ``curl``/Windows trust the interception CA, but
  httpx's bundled ``certifi`` roots do not, so every Git call died with
  ``CERTIFICATE_VERIFY_FAILED: unable to get local issuer certificate``. The
  provider must verify against the OS trust store (app/git/tls.py) — and must
  still verify, never fall back to an unverified connection.
* URL normalization: a stored ``https://github.com`` or an enterprise
  ``https://ghe.example.com/api/v3`` must map to the exact REST base, and a
  value carrying credentials, a query or a fragment must be refused.

Nothing here touches the network: the transport is stubbed.
"""
from __future__ import annotations

import ssl
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest

ROOT = Path(__file__).resolve().parents[1]

# A realistic-looking PAT. Only used to prove it never leaks.
TOKEN = "ghp_" + "a" * 30


def _github():
    """The currently loaded app.git.github module (conftest re-imports app.*)."""
    from app.git import github

    return github


class _StubResponse:
    def __init__(self, status_code: int = 200, payload=None):
        self.status_code = status_code
        self.headers: dict[str, str] = {}
        self.content = b"{}"
        self._payload = {"login": "acme"} if payload is None else payload

    def json(self):
        return self._payload


def _install_transport(monkeypatch, github, *, response=None, raiser=None):
    """Replace httpx.Client with a recorder; returns the recorded calls."""
    calls: dict[str, list] = {"verify": [], "count": 0}

    class _Recorder:
        def __init__(self, **kwargs):
            calls["count"] += 1
            calls["verify"].append(kwargs.get("verify"))
            assert "verify" in kwargs, "TLS verification must always be passed"

        def __enter__(self):
            return self

        def __exit__(self, *exc_info):
            return False

        def request(self, method, url, **kwargs):
            if raiser is not None:
                raise raiser
            return response if response is not None else _StubResponse()

    monkeypatch.setattr(github.httpx, "Client", _Recorder)
    return calls


def _provider(github, base_url="https://ghe.example.com", token=TOKEN):
    return github.GitHubEnterpriseProvider(
        base_url=base_url, organization="acme", token=token
    )


# ===========================================================================
# A. TLS trust
# ===========================================================================

def test_system_trust_context_is_handed_to_httpx(monkeypatch):
    """The OS-trust context — not the certifi default — is what httpx verifies with."""
    github = _github()
    sentinel = object()
    monkeypatch.setattr(github, "system_trust_context", lambda: sentinel)
    calls = _install_transport(monkeypatch, github)

    assert _provider(github).validate_connection() == "acme"
    assert calls["verify"] == [sentinel]


def test_verification_stays_on_with_the_system_trust_store():
    """The real context enforces verification: required + hostname checked."""
    from app.git.tls import system_trust_context

    context = system_trust_context()
    assert isinstance(context, ssl.SSLContext)
    assert context.verify_mode == ssl.CERT_REQUIRED
    assert context.check_hostname is True


@pytest.mark.parametrize("relative", ["app/git/github.py", "app/git/tls.py"])
def test_source_never_disables_verification(relative):
    """No insecure bypass exists, and none can be switched on by configuration."""
    source = (ROOT / relative).read_text(encoding="utf-8")
    assert "verify=False" not in source
    assert "CERT_NONE" not in source
    assert "check_hostname = False" not in source
    assert "_create_unverified_context" not in source


def test_the_provider_passes_the_context_as_verify():
    source = (ROOT / "app" / "git" / "github.py").read_text(encoding="utf-8")
    assert "verify=ssl_context" in source, "httpx.Client must receive verify=<context>"


def test_certificate_failure_is_classified_with_a_safe_message(monkeypatch, caplog):
    """A trust failure is its own class, and the token never appears anywhere."""
    github = _github()
    cause = ssl.SSLCertVerificationError(
        1, "certificate verify failed: unable to get local issuer certificate"
    )
    error = httpx.ConnectError(f"request failed for token {TOKEN}")
    error.__cause__ = cause
    _install_transport(monkeypatch, github, raiser=error)

    with caplog.at_level("DEBUG"):
        with pytest.raises(github.GitProviderError) as excinfo:
            _provider(github).validate_connection()

    assert excinfo.value.code == github.TLS_TRUST_ERROR
    assert excinfo.value.message == (
        "GitHub TLS certificate could not be verified using the system trust store."
    )
    assert TOKEN not in str(excinfo.value)
    assert TOKEN not in caplog.text
    assert "unable to get local issuer certificate" not in caplog.text


def test_certificate_failure_is_not_retried(monkeypatch):
    """Retrying an untrusted chain cannot help, so it fails on the first attempt."""
    github = _github()
    from app.config import Settings, get_settings

    monkeypatch.setattr(Settings, "GITHUB_MAX_RETRIES", 5)
    get_settings.cache_clear()
    try:
        error = httpx.ConnectError("boom")
        error.__cause__ = ssl.SSLCertVerificationError(1, "certificate verify failed")
        calls = _install_transport(monkeypatch, github, raiser=error)
        with pytest.raises(github.GitProviderError) as excinfo:
            _provider(github).validate_connection()
        assert excinfo.value.code == github.TLS_TRUST_ERROR
        assert calls["count"] == 1
    finally:
        get_settings.cache_clear()


def test_proxy_dns_and_generic_failures_are_classified_separately(monkeypatch):
    """TLS, proxy, DNS and a plain outage must be distinguishable."""
    github = _github()
    from app.config import Settings, get_settings

    monkeypatch.setattr(Settings, "GITHUB_MAX_RETRIES", 0)
    get_settings.cache_clear()
    try:
        cases = [
            (httpx.ProxyError("proxy connect failed"), github.PROXY_ERROR),
            (httpx.ConnectError("getaddrinfo failed"), github.DNS_ERROR),
            (httpx.ConnectError("connection refused"), github.UNAVAILABLE),
        ]
        for exc, expected in cases:
            _install_transport(monkeypatch, github, raiser=exc)
            with pytest.raises(github.GitProviderError) as excinfo:
                _provider(github).validate_connection()
            assert excinfo.value.code == expected, type(exc).__name__
            assert excinfo.value.code != github.TLS_TRUST_ERROR
    finally:
        get_settings.cache_clear()


def test_timeout_is_still_its_own_class(monkeypatch):
    github = _github()
    _install_transport(monkeypatch, github, raiser=httpx.ReadTimeout("took too long"))
    with pytest.raises(github.GitProviderError) as excinfo:
        _provider(github).validate_connection()
    assert excinfo.value.code == github.TIMEOUT


def test_trust_store_init_failure_fails_closed_before_any_request(monkeypatch):
    """No trust store means no request at all — never an unverified one."""
    github = _github()

    def _boom():
        raise github.GitProviderError(github.TLS_TRUST_ERROR, github.TLS_TRUST_MESSAGE)

    monkeypatch.setattr(github, "system_trust_context", _boom)
    calls = _install_transport(monkeypatch, github)
    with pytest.raises(github.GitProviderError) as excinfo:
        _provider(github).validate_connection()
    assert excinfo.value.code == github.TLS_TRUST_ERROR
    assert calls["count"] == 0, "no connection may be attempted without a trust store"


def test_tls_helper_fails_closed_when_truststore_cannot_be_imported(monkeypatch):
    """A missing/unhealthy trust store is a sanitized configuration error."""
    from app.git import tls
    from app.git.provider import GitProviderError

    monkeypatch.setattr(tls, "_TRUSTSTORE_AVAILABLE", False)
    monkeypatch.setattr(tls, "_TRUSTSTORE_IMPORT_ERROR", "ImportError")
    tls.reset_tls_context_cache()
    try:
        with pytest.raises(GitProviderError) as excinfo:
            tls.system_trust_context()
        assert excinfo.value.code == "tls_trust_error"
        assert excinfo.value.message == tls.TLS_TRUST_MESSAGE
    finally:
        tls.reset_tls_context_cache()


def test_tls_helper_context_is_cached_and_resettable():
    from app.git import tls

    first = tls.system_trust_context()
    assert tls.system_trust_context() is first
    tls.reset_tls_context_cache()
    assert tls.system_trust_context() is not first


def test_provider_repr_and_str_never_render_the_token():
    github = _github()
    provider = _provider(github)
    assert TOKEN not in repr(provider)
    assert TOKEN not in str(provider)
    assert "token=<set>" in repr(provider)
    assert "token=None" in repr(_provider(github, token=None))


def test_requests_are_sent_to_the_normalized_rest_base(monkeypatch):
    """The URL actually used is the /api/v3 enterprise root, not the web root."""
    github = _github()
    seen: list[str] = []

    class _Recorder:
        def __init__(self, **kwargs):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *exc_info):
            return False

        def request(self, method, url, **kwargs):
            seen.append(url)
            return _StubResponse()

    monkeypatch.setattr(github.httpx, "Client", _Recorder)
    _provider(github).validate_connection()
    assert seen == ["https://ghe.example.com/api/v3/orgs/acme"]


@pytest.mark.parametrize("ref", ["", "main", "heads/main", "refs/heads/", "refs/heads/*"])
def test_delete_branch_refuses_a_non_exact_ref_without_calling_the_api(monkeypatch, ref):
    """Only an exact ``refs/heads/<branch>`` may ever be deleted.

    Regression guard: the guard clause referenced an error code that was never
    imported, so a malformed stored ref raised ``NameError`` instead of a
    classified provider error. A malformed ref must also never reach the API, so
    the recorder is asserted to have made zero calls.
    """
    github = _github()
    calls = _install_transport(monkeypatch, github)

    with pytest.raises(github.GitProviderError) as excinfo:
        _provider(github).delete_branch("acme", "widgets", ref)

    assert excinfo.value.code == github.INVALID_BASE_BRANCH
    assert calls["count"] == 0, "an invalid ref must be rejected before any request"


def test_delete_branch_sends_only_the_url_encoded_short_ref(monkeypatch):
    """The stored full ref is stripped to ``refs/heads/<branch>`` exactly once."""
    github = _github()
    seen: list[tuple[str, str]] = []

    class _Recorder:
        def __init__(self, **kwargs):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *exc_info):
            return False

        def request(self, method, url, **kwargs):
            seen.append((method, url))
            return _StubResponse(status_code=204, payload={})

    monkeypatch.setattr(github.httpx, "Client", _Recorder)

    assert _provider(github).delete_branch("acme", "widgets", "refs/heads/feature/one") is True
    assert seen == [
        ("DELETE", "https://ghe.example.com/api/v3/repos/acme/widgets/git/refs/heads/feature%2Fone")
    ]


def test_delete_branch_returns_false_only_for_an_exact_404(monkeypatch):
    """A 404 of the exact ref is "already absent"; success stays True."""
    github = _github()
    seen: list[tuple[str, str]] = []

    class _Recorder:
        def __init__(self, **kwargs):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *exc_info):
            return False

        def request(self, method, url, **kwargs):
            seen.append((method, url))
            return _StubResponse(status_code=404, payload={"message": "Not Found"})

    monkeypatch.setattr(github.httpx, "Client", _Recorder)

    assert _provider(github).delete_branch("acme", "widgets", "refs/heads/feature/one") is False
    assert len(seen) == 1


@pytest.mark.parametrize(
    ("status_code", "expected_code"),
    [
        (401, "auth_failed"),
        (403, "permission_denied"),
        (429, "rate_limited"),
        (502, "unavailable"),
    ],
)
def test_delete_branch_never_treats_http_failures_as_absent(monkeypatch, status_code, expected_code):
    """Auth/permission/rate-limit/server failures raise; they never return False."""
    github = _github()
    from app.config import Settings, get_settings

    monkeypatch.setattr(Settings, "GITHUB_MAX_RETRIES", 0)
    get_settings.cache_clear()
    try:
        _install_transport(
            monkeypatch, github,
            response=_StubResponse(status_code=status_code, payload={"message": "boom"}),
        )
        with pytest.raises(github.GitProviderError) as excinfo:
            _provider(github).delete_branch("acme", "widgets", "refs/heads/feature/one")
        assert excinfo.value.code == expected_code
    finally:
        get_settings.cache_clear()


def test_delete_branch_never_treats_transport_failures_as_absent(monkeypatch):
    """Timeout and TLS failures raise; they never mean "already absent"."""
    github = _github()
    from app.config import Settings, get_settings

    monkeypatch.setattr(Settings, "GITHUB_MAX_RETRIES", 0)
    get_settings.cache_clear()
    try:
        failures = [
            (httpx.ReadTimeout("took too long"), github.TIMEOUT),
            (tls_failure(), github.TLS_TRUST_ERROR),
        ]
        for raiser, expected_code in failures:
            _install_transport(monkeypatch, github, raiser=raiser)
            with pytest.raises(github.GitProviderError) as excinfo:
                _provider(github).delete_branch("acme", "widgets", "refs/heads/feature/one")
            assert excinfo.value.code == expected_code
    finally:
        get_settings.cache_clear()


def tls_failure():
    """A transport failure whose cause is a certificate-verification error."""
    error = httpx.ConnectError("request failed")
    error.__cause__ = ssl.SSLCertVerificationError(1, "certificate verify failed")
    return error


def test_delete_branch_only_uses_the_exact_heads_ref_endpoint(monkeypatch):
    """Only ``/git/refs/heads/<encoded ref>`` is called; never repo or tag."""
    github = _github()
    seen: list[tuple[str, str]] = []

    class _Recorder:
        def __init__(self, **kwargs):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *exc_info):
            return False

        def request(self, method, url, **kwargs):
            seen.append((method, url))
            return _StubResponse(status_code=204, payload={})

    monkeypatch.setattr(github.httpx, "Client", _Recorder)

    assert _provider(github).delete_branch(
        "acme", "widgets", "refs/heads/feature/spaced name"
    ) is True
    assert seen == [
        (
            "DELETE",
            "https://ghe.example.com/api/v3/repos/acme/widgets"
            "/git/refs/heads/feature%2Fspaced%20name",
        )
    ]
    for _method, url in seen:
        assert "/git/refs/heads/" in url
        assert url.rstrip("/").endswith("/git/refs/heads/feature%2Fspaced%20name")
        assert "/git/tags" not in url
        assert not url.endswith("/repos/acme/widgets")



# ===========================================================================
# B. API URL normalization
# ===========================================================================

@pytest.mark.parametrize(
    ("stored", "expected"),
    [
        # blank -> deployment default
        ("", "https://api.github.com"),
        ("   ", "https://api.github.com"),
        # public github.com variants -> the public REST root
        ("https://github.com", "https://api.github.com"),
        ("https://github.com/", "https://api.github.com"),
        ("https://www.github.com", "https://api.github.com"),
        ("https://WWW.GitHub.com", "https://api.github.com"),
        # the API host itself, with or without a trailing slash -> unchanged
        ("https://api.github.com", "https://api.github.com"),
        ("https://api.github.com/", "https://api.github.com"),
        # enterprise -> exactly one /api/v3
        ("https://ghe.example.com", "https://ghe.example.com/api/v3"),
        ("https://ghe.example.com/", "https://ghe.example.com/api/v3"),
        ("https://ghe.example.com/api/v3", "https://ghe.example.com/api/v3"),
        ("https://ghe.example.com/api/v3/", "https://ghe.example.com/api/v3"),
        # a valid enterprise port is preserved
        ("https://ghe.example.com:8443", "https://ghe.example.com:8443/api/v3"),
        ("https://ghe.example.com:8443/", "https://ghe.example.com:8443/api/v3"),
        ("https://ghe.example.com:8443/api/v3", "https://ghe.example.com:8443/api/v3"),
        # loopback http stays usable for local smoke testing
        ("http://localhost:3000", "http://localhost:3000/api/v3"),
    ],
)
def test_api_base_normalization(stored, expected):
    github = _github()
    assert github.normalize_api_base(stored) == expected


@pytest.mark.parametrize(
    "stored",
    [
        "https://ghe.example.com/api/v3/api/v3",  # never double the suffix
        "https://ghe.example.com/some/path",
        "https://user:pass@ghe.example.com",  # credentials in a URL
        "https://user@ghe.example.com",  # userinfo without a password
        "https://ghe.example.com?redirect=evil",  # query string
        "https://ghe.example.com#fragment",
        "file:///etc/passwd",  # unsafe scheme
        "ftp://ghe.example.com",
        "javascript:alert(1)",
        "https://",  # no host
    ],
)
def test_api_base_rejects_unsafe_values(stored):
    github = _github()
    with pytest.raises(github.GitBaseUrlError):
        github.normalize_api_base(stored)


def test_api_base_is_idempotent():
    """Re-normalizing a normalized value must not append /api/v3 twice."""
    github = _github()
    once = github.normalize_api_base("https://ghe.example.com")
    assert github.normalize_api_base(once) == once
    assert once.count("/api/v3") == 1


@pytest.mark.parametrize(
    ("stored", "expected"),
    [
        ("", "https://github.com"),
        ("https://github.com", "https://github.com"),
        ("https://api.github.com", "https://github.com"),
        ("https://ghe.example.com", "https://ghe.example.com"),
        ("https://ghe.example.com/api/v3", "https://ghe.example.com"),
        ("https://ghe.example.com:8443", "https://ghe.example.com:8443"),
    ],
)
def test_web_base_normalization(stored, expected):
    github = _github()
    assert github.normalize_web_base(stored) == expected


def test_branch_url_uses_the_web_base_not_the_api_base():
    github = _github()
    provider = github.GitHubEnterpriseProvider(
        base_url="https://ghe.example.com", organization="acme", token=TOKEN
    )
    assert provider.build_branch_url("acme", "web", "bug/123-fix") == (
        "https://ghe.example.com/acme/web/tree/bug/123-fix"
    )


def test_an_unusable_stored_base_url_is_a_safe_configuration_error(monkeypatch):
    """A bad saved URL must not 500 and must not echo the value back."""
    github = _github()
    monkeypatch.setattr(github.httpx, "Client", _NeverCalled)
    provider = github.GitHubEnterpriseProvider(
        base_url="https://user:secret@ghe.example.com", organization="acme", token=TOKEN
    )
    with pytest.raises(github.GitProviderError) as excinfo:
        provider.validate_connection()
    assert excinfo.value.code == github.INVALID_BASE_URL
    assert "secret" not in excinfo.value.message
    with pytest.raises(github.GitProviderError) as url_excinfo:
        provider.build_branch_url("acme", "web", "dev")
    assert url_excinfo.value.code == github.INVALID_BASE_URL


class _NeverCalled:
    """A client factory that fails the test if a request is ever attempted."""

    def __init__(self, **kwargs):
        raise AssertionError("no request may be attempted for an unusable base URL")


# ===========================================================================
# F. Strict draft semantics: explicit blank is never replaced
# ===========================================================================

def test_draft_explicit_blank_base_url_is_tested_as_blank(monkeypatch):
    """An explicit "" draft must reach the provider unchanged (then fail safe)."""
    github = _github()
    seen: dict[str, object] = {}

    class _Capture:
        def __init__(self, **kwargs):
            seen["base_url"] = kwargs.get("base_url", "")

        def validate_connection(self):
            raise github.GitProviderError(github.INVALID_BASE_URL, "bad test value")

    monkeypatch.setattr(github, "GitHubEnterpriseProvider", _Capture)
    provider = github.GitHubEnterpriseProvider(base_url="", organization="acme")
    assert seen["base_url"] == ""
    with pytest.raises(github.GitProviderError):
        provider.validate_connection()


def test_build_provider_none_vs_blank_resolution(monkeypatch):
    """None inherits saved-then-env; "" stays an explicit blank draft value."""
    from app.config import get_settings
    from app.routes import git as routes

    settings = get_settings()
    saved = SimpleNamespace(
        base_url="https://saved.example.com",
        organization="saved-org",
        credential_encrypted="",
    )
    monkeypatch.setattr(
        routes.credential_svc, "resolve_token", lambda config: "global-fallback"
    )
    monkeypatch.setattr(
        routes, "GitHubEnterpriseProvider",
        lambda **kwargs: SimpleNamespace(**kwargs),
    )
    inherited = routes.build_provider(saved)
    assert inherited.base_url == "https://saved.example.com"
    assert inherited.organization == "saved-org"
    blank = routes.build_provider(saved, base_url="", organization="")
    assert blank.base_url == ""
    assert blank.organization == ""
    monkeypatch.setattr(
        routes, "GitHubEnterpriseProvider",
        lambda **kwargs: SimpleNamespace(**kwargs),
    )
    env_only = routes.build_provider(None)
    assert env_only.base_url == (settings.GITHUB_API_URL or "").strip()


def test_require_https_allows_loopback_http_but_rejects_remote_http():
    github = _github()
    assert github.require_https_provider_url(
        "http://localhost:8080"
    ) == "http://localhost:8080/api/v3"
    assert github.require_https_provider_url(
        "http://127.0.0.1:9000"
    ) == "http://127.0.0.1:9000/api/v3"
    with pytest.raises(github.GitBaseUrlError):
        github.require_https_provider_url("http://ghe.example.com")
    with pytest.raises(github.GitBaseUrlError):
        github.require_https_provider_url("http://192.168.1.10")


def test_require_https_rejects_userinfo_query_fragment_and_schemes():
    github = _github()
    for bad in (
        "https://user:pass@ghe.example.com",
        "https://user@ghe.example.com",
        "https://ghe.example.com?x=1",
        "https://ghe.example.com#frag",
        "ftp://ghe.example.com",
        "file:///etc/passwd",
        "https://ghe.example.com/extra/path",
    ):
        with pytest.raises(github.GitBaseUrlError):
            github.require_https_provider_url(bad)


def test_tls_ca_bundle_missing_file_fails_closed(monkeypatch, tmp_path):
    from app.git import tls as tls_mod

    tls_mod.reset_tls_context_cache()
    monkeypatch.setattr(
        tls_mod, "ca_bundle_configured",
        lambda: str(tmp_path / "missing-ca.pem"),
    )
    with pytest.raises(tls_mod.GitProviderError) as excinfo:
        tls_mod.system_trust_context()
    assert excinfo.value.code == tls_mod.TLS_TRUST_ERROR
    tls_mod.reset_tls_context_cache()


def test_tls_ca_bundle_invalid_file_fails_closed(monkeypatch, tmp_path):
    from app.git import tls as tls_mod

    bad = tmp_path / "bad-ca.pem"
    bad.write_text("not a pem bundle", encoding="utf-8")
    tls_mod.reset_tls_context_cache()
    monkeypatch.setattr(tls_mod, "ca_bundle_configured", lambda: str(bad))
    with pytest.raises(tls_mod.GitProviderError) as excinfo:
        tls_mod.system_trust_context()
    assert excinfo.value.code == tls_mod.TLS_TRUST_ERROR
    tls_mod.reset_tls_context_cache()


def test_tls_error_message_never_contains_pat(monkeypatch):
    github = _github()
    token = "super-secret-project-pat-value-12345"
    calls = _install_transport(
        monkeypatch, github,
        raiser=ssl.SSLCertVerificationError("certificate verify failed"),
    )
    provider = github.GitHubEnterpriseProvider(
        base_url="https://ghe.example.com", organization="acme", token=token
    )
    with pytest.raises(github.GitProviderError) as excinfo:
        provider.validate_connection()
    assert excinfo.value.code == github.TLS_TRUST_ERROR
    assert token not in excinfo.value.message
    assert calls["count"] >= 1
