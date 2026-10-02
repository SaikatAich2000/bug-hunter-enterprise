"""GitHub REST provider — branch creation only.

No local repository, no `git` command, no shell, no clone. Every call is a
bounded HTTPS request through the already-present httpx dependency.

Authentication is PAT-only, and the credential is passed IN explicitly: the
provider never reads a token from ``get_settings()`` itself, so a provider built
for Project A can never authenticate as Project B. Resolving which credential a
project uses (its own encrypted PAT, else the legacy global ``GITHUB_TOKEN``)
happens in ``app/git/credentials.py`` and ``app/routes/git.py::build_provider``.
The token is never persisted, never returned to a client and never logged.

TLS verification is always enforced through the operating system trust store
(``app/git/tls.py``), which is what makes this work behind corporate TLS
interception. Verification is never turned off, and no flag can disable
certificate checking.

No network call happens at import time or at application startup.
"""
from __future__ import annotations

import logging
import socket
import time
from urllib.parse import quote, urlparse

import httpx

from app.config import get_settings
from app.git.provider import (
    AUTH_FAILED,
    BASE_BRANCH_MISSING,
    BRANCH_EXISTS,
    DNS_ERROR,
    INVALID_BASE_BRANCH,
    INVALID_BASE_URL,
    INVALID_RESPONSE,
    NOT_CONFIGURED,
    NOT_FOUND,
    PERMISSION_DENIED,
    PROXY_ERROR,
    RATE_LIMITED,
    TIMEOUT,
    TLS_TRUST_ERROR,
    UNAVAILABLE,
    GitProviderError,
    ProviderRepository,
    redact_secrets,
    sanitize_provider_message,
)
from app.git.tls import (
    TLS_TRUST_MESSAGE,
    is_certificate_verification_error,
    system_trust_context,
)

logger = logging.getLogger("bug_hunter.git.github")

_DEFAULT_API = "https://api.github.com"
_DEFAULT_WEB = "https://github.com"
# github.com, its www alias and the API host itself all resolve to the public
# api.github.com REST root — never to an enterprise ``/api/v3`` prefix.
_GITHUB_COM_HOSTS = frozenset({"github.com", "www.github.com", "api.github.com"})

# Only http(s) may ever be used to reach a provider. Plain http is accepted by
# the parser only so the caller can reject non-loopback hosts with a safe
# configuration error; the provider never sends a PAT over plaintext.
_ALLOWED_SCHEMES = frozenset({"http", "https"})
# Loopback hosts where plain http stays usable for local smoke tests. Anything
# else must use https, otherwise a PAT would travel unencrypted.
_LOOPBACK_HOSTS = frozenset({"localhost", "127.0.0.1", "::1"})
# The one path an enterprise base URL may already carry, so re-normalizing a
# saved value can never produce ``/api/v3/api/v3``.
_API_SUFFIX = "/api/v3"

# Safe, non-echoing message for an unusable configured base URL. The offending
# value is deliberately never reflected back into a response or a log line.
INVALID_BASE_URL_MESSAGE = (
    "The configured Git base URL is not a usable provider URL; enter an https "
    "origin such as https://github.example.com"
)

# DNS resolution markers, checked against the exception chain.
_DNS_MARKERS = (
    "getaddrinfo failed",
    "name or service not known",
    "nodename nor servname",
    "temporary failure in name resolution",
    "no address associated with hostname",
)

# 5xx codes that are safe to replay on an idempotent GET. A failing POST is never
# replayed here: the caller re-checks the remote branch and reconciles instead.
_RETRYABLE_STATUS = frozenset({502, 503, 504})
_BACKOFF_SECONDS = 0.5


class GitBaseUrlError(ValueError):
    """A stored base URL that cannot be turned into a safe provider URL."""


def _is_dns_failure(exc: BaseException) -> bool:
    """True when the chain shows a name-resolution failure (not a TLS failure)."""
    current: BaseException | None = exc
    depth = 0
    while current is not None and depth < 12:
        if isinstance(current, socket.gaierror):
            return True
        text = str(current).lower()
        if any(marker in text for marker in _DNS_MARKERS):
            return True
        current = current.__cause__ or current.__context__
        depth += 1
    return False


def parse_base_url(base_url: str) -> tuple[str, str, str]:
    """Validate one base URL and return ``(scheme, netloc, host)``.

    ``("", "", "")`` means "blank — use the deployment default".

    Rejected, always raising :class:`GitBaseUrlError`:

    * any scheme other than ``http``/``https`` (``file``, ``ftp``, ``data``,
      ``javascript``, ...),
    * credentials/userinfo (``https://user:pass@host``) — a base URL must never
      carry a secret, including percent-encoded ``%40``/``%3A`` bypasses,
    * a query string or a fragment,
    * a path that is anything other than an existing ``/api/v3`` suffix,
    * a missing host, or an out-of-range port.
    """
    raw = (base_url or "").strip()
    if not raw:
        return "", "", ""
    if "@" in raw.split("://", 1)[-1].split("/", 1)[0]:
        raise GitBaseUrlError("credentials in a base URL are not allowed")
    candidate = raw if "://" in raw else f"https://{raw}"
    parsed = urlparse(candidate)
    scheme = (parsed.scheme or "").lower()
    if scheme not in _ALLOWED_SCHEMES:
        raise GitBaseUrlError("unsupported scheme")
    if parsed.username or parsed.password:
        raise GitBaseUrlError("credentials in a base URL are not allowed")
    if parsed.query or parsed.fragment or parsed.params:
        raise GitBaseUrlError("query strings and fragments are not allowed")
    host = (parsed.hostname or "").strip().lower()
    if not host:
        raise GitBaseUrlError("missing host")
    # ``urlparse`` keeps a redundant ``:port`` in ``netloc`` for IPv6 loopback
    # (``[::1]:8080``); the validated ``hostname``/``port`` pair below is the
    # authority, so encoded userinfo cannot smuggle credentials past this point.
    try:
        port = parsed.port
    except ValueError:
        raise GitBaseUrlError("invalid port") from None
    path = (parsed.path or "").rstrip("/")
    if path and path != _API_SUFFIX:
        raise GitBaseUrlError("unexpected path")
    # ``parsed.hostname`` strips IPv6 brackets; put them back for a usable netloc.
    literal = f"[{host}]" if ":" in host else host
    netloc = literal if port is None else f"{literal}:{port}"
    return scheme, netloc, host


def require_https_provider_url(base_url: str) -> str:
    """Normalized API base that additionally enforces transport safety.

    ``http`` survives only for loopback test hosts; every other provider URL
    must be ``https``. Raises :class:`GitBaseUrlError` otherwise.
    """
    scheme, netloc, host = parse_base_url(base_url)
    if not host:
        return _DEFAULT_API
    if scheme == "http" and host not in _LOOPBACK_HOSTS:
        raise GitBaseUrlError("base URL must use https")
    if host in _GITHUB_COM_HOSTS:
        return _DEFAULT_API
    return f"{scheme}://{netloc}{_API_SUFFIX}"


def normalize_api_base(base_url: str) -> str:
    """The REST base URL for one configured value.

    Mapping (exactly):

    ===========================================  =====================================
    blank                                        ``https://api.github.com``
    ``https://github.com``                       ``https://api.github.com``
    ``https://www.github.com``                   ``https://api.github.com``
    ``https://api.github.com``                   unchanged
    ``https://api.github.com/``                  ``https://api.github.com``
    ``https://ghe.example.com``                  ``https://ghe.example.com/api/v3``
    ``https://ghe.example.com/api/v3``           unchanged
    ``https://ghe.example.com:8443``             ``https://ghe.example.com:8443/api/v3``
    ===========================================  =====================================

    ``/api/v3`` is derived from the origin, so it can never be appended twice. A
    blank value falls back to the deployment default; a saved non-empty value is
    never replaced by an environment value (that decision belongs to the caller).
    """
    scheme, netloc, host = parse_base_url(base_url)
    if not host:
        return _DEFAULT_API
    if host in _GITHUB_COM_HOSTS:
        return _DEFAULT_API
    return f"{scheme}://{netloc}{_API_SUFFIX}"


def normalize_web_base(base_url: str) -> str:
    """Human-facing base URL (never the ``/api/v3`` root) for branch links."""
    scheme, netloc, host = parse_base_url(base_url)
    if not host:
        return _DEFAULT_WEB
    if host in _GITHUB_COM_HOSTS:
        return _DEFAULT_WEB
    # Enterprise links always open on the host root, never under /api/v3.
    return f"{scheme}://{netloc}"


class GitHubEnterpriseProvider:
    """`GitProvider` implementation over the GitHub REST API."""

    def __init__(
        self,
        *,
        base_url: str = "",
        organization: str = "",
        token: str | None = None,
    ) -> None:
        self.base_url = (base_url or "").strip()
        self.organization = (organization or "").strip()
        # The credential is supplied by the caller for THIS project only. It is
        # never read from the environment here, so two providers can never share
        # or cross over a token.
        self._token = (token or "").strip()

    def __repr__(self) -> str:
        """Never render the token — this object may end up in a traceback."""
        return (
            f"{type(self).__name__}(base_url={self.base_url!r}, "
            f"organization={self.organization!r}, token=<set>)"
            if self._token
            else (
                f"{type(self).__name__}(base_url={self.base_url!r}, "
                f"organization={self.organization!r}, token=None)"
            )
        )

    __str__ = __repr__

    # --- internal helpers -------------------------------------------------

    @property
    def api_base(self) -> str:
        """Normalized REST base; a bad configured value is a safe 409-class error.

        Transport safety is enforced here (not only at save time): plain http
        survives solely for loopback test hosts, so a legacy row can never turn
        a PAT-bearing request into plaintext on the wire.
        """
        try:
            return require_https_provider_url(self.base_url)
        except GitBaseUrlError:
            raise GitProviderError(INVALID_BASE_URL, INVALID_BASE_URL_MESSAGE) from None

    def _effective_organization(self) -> str:
        """Only this provider's own organization — never an environment fallback.

        The environment default is applied once, by ``build_provider``, only for
        legacy rows whose organization field is blank.
        """
        return self.organization

    def _auth_token(self) -> str:
        """The token this project resolved to; never logged or returned."""
        if not self._token:
            raise GitProviderError(
                NOT_CONFIGURED, "GitHub authentication is not configured on the server"
            )
        return self._token

    def _timeouts(self) -> httpx.Timeout:
        settings = get_settings()
        connect = settings.GITHUB_CONNECT_TIMEOUT_SECONDS
        return httpx.Timeout(
            connect=connect,
            read=settings.GITHUB_READ_TIMEOUT_SECONDS,
            write=connect,
            pool=connect,
        )

    def _reason_phrase(self, response: httpx.Response) -> str:
        """Provider-supplied `message` field, sanitized and length-capped."""
        try:
            body = response.json()
        except ValueError:
            return f"HTTP {response.status_code}"
        if isinstance(body, dict):
            return sanitize_provider_message(
                str(body.get("message") or ""),
                fallback=f"HTTP {response.status_code}",
            )
        return f"HTTP {response.status_code}"

    def _error_for(self, response: httpx.Response) -> GitProviderError:
        status = response.status_code
        reason = self._reason_phrase(response)
        if status == 401:
            return GitProviderError(
                AUTH_FAILED,
                f"GitHub rejected the credentials ({reason})",
                status_code=status,
            )
        if status == 429 or (
            status == 403 and response.headers.get("x-ratelimit-remaining") == "0"
        ):
            return GitProviderError(
                RATE_LIMITED,
                f"GitHub rate limit reached ({reason})",
                status_code=status,
            )
        if status == 403:
            return GitProviderError(
                PERMISSION_DENIED,
                f"GitHub denied access ({reason})",
                status_code=status,
            )
        if status == 404:
            return GitProviderError(
                NOT_FOUND,
                f"GitHub resource not found ({reason})",
                status_code=status,
            )
        if status in _RETRYABLE_STATUS:
            return GitProviderError(
                UNAVAILABLE,
                f"GitHub is temporarily unavailable ({status})",
                status_code=status,
            )
        return GitProviderError(
            INVALID_RESPONSE,
            f"Unexpected GitHub response ({status} {reason})",
            status_code=status,
        )

    def _transport_error(
        self, exc: BaseException, auth: str | None
    ) -> GitProviderError:
        """Classify a transport failure without ever echoing raw exception text.

        TLS trust failures are their own class: retrying an untrusted chain
        cannot succeed, and an operator needs to see "the trust store" rather
        than "the network". Proxy, DNS and generic connection failures stay
        distinguishable from each other for the same reason. Only the exception
        class name and a redacted reason reach the log.
        """
        if is_certificate_verification_error(exc):
            logger.warning(
                "git.tls.verification_failed exception=%s",
                type(exc).__name__,
            )
            return GitProviderError(TLS_TRUST_ERROR, TLS_TRUST_MESSAGE)
        reason = redact_secrets(str(exc), extra_secrets=(auth or "",))[:200]
        if isinstance(exc, httpx.ProxyError):
            logger.warning(
                "git.transport.proxy_failure exception=%s reason=%s",
                type(exc).__name__, reason,
            )
            return GitProviderError(
                PROXY_ERROR, "GitHub is unreachable through the configured proxy"
            )
        if _is_dns_failure(exc):
            logger.warning(
                "git.transport.dns_failure exception=%s reason=%s",
                type(exc).__name__, reason,
            )
            return GitProviderError(
                DNS_ERROR, "GitHub host name could not be resolved"
            )
        logger.warning(
            "git.transport.failure exception=%s reason=%s",
            type(exc).__name__, reason,
        )
        return GitProviderError(UNAVAILABLE, "GitHub is unreachable")

    def _request(
        self,
        method: str,
        path: str,
        *,
        token: str | None = None,
        params: dict | None = None,
        json_body: dict | None = None,
        not_found_ok: bool = False,
    ):
        """One bounded request, with bounded retries for safe transient failures.

        Authentication/validation errors are never retried; connection and 5xx
        failures are, at most GITHUB_MAX_RETRIES times with backoff.
        """
        settings = get_settings()
        auth = token if token is not None else self._auth_token()
        url = f"{self.api_base}{path}"
        headers = {
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
            "User-Agent": "Bug-Hunter",
        }
        if auth:
            headers["Authorization"] = f"Bearer {auth}"

        # Fail closed before any network attempt. Verification is always on, via
        # the operating system trust store (corporate TLS interception included).
        ssl_context = system_trust_context()

        attempts = max(settings.GITHUB_MAX_RETRIES, 0) + 1
        last_error: GitProviderError | None = None
        for attempt in range(attempts):
            try:
                with httpx.Client(
                    timeout=self._timeouts(), verify=ssl_context
                ) as client:
                    response = client.request(
                        method, url, headers=headers, params=params, json=json_body
                    )
            except httpx.TimeoutException:
                last_error = GitProviderError(
                    TIMEOUT, "GitHub did not respond in time"
                )
            except (httpx.HTTPError, OSError) as exc:
                # Raw exception text can embed the URL or the Authorization
                # header, so it is classified and never handed back verbatim: a
                # TLS failure, a proxy, a DNS problem and a plain outage each get
                # their own stable, secret-free message.
                last_error = self._transport_error(exc, auth)
                if not last_error.retryable:
                    raise last_error from None
            else:
                if response.status_code == 404 and not_found_ok:
                    return None
                if response.status_code < 400:
                    if response.status_code == 204 or not response.content:
                        return None
                    try:
                        return response.json()
                    except ValueError as exc:
                        raise GitProviderError(
                            INVALID_RESPONSE,
                            "GitHub returned a non-JSON response",
                        ) from exc
                error = self._error_for(response)
                if not error.retryable:
                    raise error
                last_error = error
            if attempt + 1 < attempts:
                time.sleep(_BACKOFF_SECONDS * (attempt + 1))
        raise last_error or GitProviderError(
            UNAVAILABLE, "GitHub request failed"
        )

    # --- GitProvider ------------------------------------------------------

    def validate_connection(self) -> str:
        """Cheap authenticated call; returns a short non-secret label."""
        org = self._effective_organization()
        if org:
            payload = self._request("GET", f"/orgs/{quote(org, safe='')}")
            if isinstance(payload, dict) and payload.get("login"):
                return str(payload["login"])
            return org
        payload = self._request("GET", "/user")
        if isinstance(payload, dict) and payload.get("login"):
            return str(payload["login"])
        return self.api_base

    def list_repositories(self, *, limit: int = 100) -> list[ProviderRepository]:
        """Repositories the PAT may pick from (the allow-list source)."""
        capped = max(1, min(limit, 200))
        org = self._effective_organization()
        path = f"/orgs/{quote(org, safe='')}/repos" if org else "/user/repos"
        rows: list[dict] = []
        page, per_page = 1, min(100, capped)
        while len(rows) < capped:
            params = {"per_page": per_page, "page": page}
            if org:
                params.update({"type": "all", "sort": "full_name"})
            payload = self._request("GET", path, params=params)
            batch = payload if isinstance(payload, list) else []
            rows.extend(batch[: capped - len(rows)])
            if len(batch) < per_page:
                break
            page += 1
        return [
            repository
            for repository in (
                self._to_provider_repository(row) for row in rows
            )
            if repository is not None
        ]

    def get_branch_sha(self, owner: str, name: str, branch: str) -> str | None:
        payload = self._request(
            "GET",
            f"/repos/{quote(owner, safe='')}/{quote(name, safe='')}"
            f"/branches/{quote(branch, safe='')}",
            not_found_ok=True,
        )
        if not isinstance(payload, dict):
            return None
        commit = payload.get("commit")
        sha = ""
        if isinstance(commit, dict):
            sha = str(commit.get("sha") or "")
        if sha:
            return sha
        raise GitProviderError(
            BASE_BRANCH_MISSING, f"Branch '{branch}' has no resolvable commit SHA"
        )

    def branch_exists(self, owner: str, name: str, branch: str) -> bool:
        payload = self._request(
            "GET",
            f"/repos/{quote(owner, safe='')}/{quote(name, safe='')}"
            f"/branches/{quote(branch, safe='')}",
            not_found_ok=True,
        )
        return isinstance(payload, dict)

    def create_branch(self, owner: str, name: str, branch: str, sha: str) -> str:
        payload = self._request(
            "POST",
            f"/repos/{quote(owner, safe='')}/{quote(name, safe='')}/git/refs",
            json_body={"ref": f"refs/heads/{branch}", "sha": sha},
        )
        if not isinstance(payload, dict):
            return ""
        obj = payload.get("object")
        return str(obj.get("sha") or "") if isinstance(obj, dict) else ""

    def delete_branch(self, owner: str, name: str, full_ref: str) -> bool:
        prefix = "refs/heads/"
        if not full_ref.startswith(prefix) or "*" in full_ref:
            raise GitProviderError(INVALID_BASE_BRANCH, "Ref is not an exact branch ref")
        short_ref = full_ref[len(prefix):]
        if not short_ref:
            raise GitProviderError(INVALID_BASE_BRANCH, "Ref is not an exact branch ref")
        # not_found_ok is deliberately NOT used: a 204 and a swallowed 404 both
        # return None, so it could not tell "deleted" from "already absent".
        # Instead the classified 404 (NOT_FOUND, never retried) distinguishes the
        # absent ref, while auth/rate-limit/TLS/timeout/5xx keep raising.
        try:
            self._request(
                "DELETE",
                f"/repos/{quote(owner, safe='')}/{quote(name, safe='')}"
                f"/git/refs/heads/{quote(short_ref, safe='')}",
            )
        except GitProviderError as exc:
            if exc.code != NOT_FOUND:
                raise
            return False  # the exact expected ref was already absent
        return True

    def build_branch_url(self, owner: str, name: str, branch: str) -> str:
        try:
            web_base = normalize_web_base(self.base_url)
        except GitBaseUrlError:
            raise GitProviderError(
                INVALID_BASE_URL, INVALID_BASE_URL_MESSAGE
            ) from None
        return (
            f"{web_base}/{quote(owner, safe='')}"
            f"/{quote(name, safe='')}/tree/{quote(branch, safe='/')}"
        )

    # --- mapping ----------------------------------------------------------

    @staticmethod
    def _to_provider_repository(row) -> ProviderRepository | None:
        """Map one API payload to safe metadata; None when it has no name."""
        if not isinstance(row, dict):
            return None
        name = str(row.get("name") or "").strip()
        if not name:
            return None
        owner = ""
        owner_payload = row.get("owner")
        if isinstance(owner_payload, dict):
            owner = str(owner_payload.get("login") or "").strip()
        if not owner:
            full_name = str(row.get("full_name") or "")
            owner = full_name.split("/", 1)[0] if "/" in full_name else ""
        return ProviderRepository(
            provider_repo_id=str(row.get("id") or ""),
            name=name,
            owner=owner,
            url=str(row.get("html_url") or "").strip(),
            default_branch=str(row.get("default_branch") or "").strip(),
        )


def reset_token_cache() -> None:
    """Compatibility shim (PAT auth keeps no token cache). No-op."""
    return None


__all__ = [
    "BRANCH_EXISTS",
    "GitBaseUrlError",
    "GitHubEnterpriseProvider",
    "INVALID_BASE_URL_MESSAGE",
    "normalize_api_base",
    "normalize_web_base",
    "parse_base_url",
    "reset_token_cache",
]