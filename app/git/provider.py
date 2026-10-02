"""Provider-agnostic git branch service seam.

Only GitHub Enterprise is implemented today (app/git/github.py). The Protocol
and the error taxonomy below are the only things a second provider would need
to satisfy — GitLab/Bitbucket/Azure DevOps are intentionally NOT implemented.

Everything a provider reports passes through `sanitize_provider_message` before
it can reach a log line, an audit row or an API response, because provider
bodies can echo the Authorization header or a token.
"""
from __future__ import annotations

import re
from collections.abc import Iterable
from dataclasses import dataclass
from typing import Protocol

# --- Stable error classifiers -------------------------------------------------
AUTH_FAILED = "auth_failed"
PERMISSION_DENIED = "permission_denied"
NOT_FOUND = "not_found"
BASE_BRANCH_MISSING = "base_branch_missing"
BRANCH_EXISTS = "branch_exists"
RATE_LIMITED = "rate_limited"
TIMEOUT = "timeout"
UNAVAILABLE = "unavailable"
NOT_CONFIGURED = "not_configured"
INVALID_RESPONSE = "invalid_response"
# Transport failures are classified separately so an operator can tell a broken
# trust store from a proxy, a DNS problem or a plain network outage.
TLS_TRUST_ERROR = "tls_trust_error"
PROXY_ERROR = "proxy_error"
DNS_ERROR = "dns_error"
# Configuration failure: the stored base URL cannot become a provider URL.
INVALID_BASE_URL = "invalid_base_url"
# Local validation failure: the supplied base branch is not usable.
INVALID_BASE_BRANCH = "invalid_base_branch"

# Only these may be retried: everything else is either a definitive failure or
# could already have created the branch, which the caller reconciles instead.
# A TLS trust failure is deliberately NOT retryable — retrying an untrusted
# certificate chain cannot succeed and would only delay the diagnosis.
RETRYABLE_CODES = frozenset(
    {TIMEOUT, UNAVAILABLE, PROXY_ERROR, DNS_ERROR}
)

_SECRET_PATTERNS = (
    re.compile(r"gh[pousr]_\w{8,}"),
    re.compile(r"github_pat_\w{8,}"),
    re.compile(r"-----BEGIN[^-]{0,40}PRIVATE KEY-----[\s\S]*?-----END[^-]{0,40}PRIVATE KEY-----"),
    re.compile(r"(?i)\b(?:authorization|token|bearer|password)\b\s*[:=]\s*\S+"),
    re.compile(r"(?i)\bbearer\s+\S+"),
)

_MAX_MESSAGE_CHARS = 300

#: Single placeholder used everywhere a secret value is erased.
_REDACTED_PLACEHOLDER = "[redacted]"


def _configured_secret_values(extra: Iterable[str] = ()) -> tuple[str, ...]:
    """Secret literals that must never survive into a message.

    Only values long enough to be a real credential are collected, so a
    one-character environment value cannot turn every message into a redaction
    storm. The global token is read lazily to avoid an import cycle with
    app.config.
    """
    values: list[str] = []
    for candidate in extra:
        value = (candidate or "").strip()
        if len(value) >= 8:
            values.append(value)
    try:
        from app.config import get_settings

        global_token = (get_settings().GITHUB_TOKEN or "").strip()
    except Exception:  # pragma: no cover - settings always load in practice
        global_token = ""
    if len(global_token) >= 8 and global_token not in values:
        values.append(global_token)
    return tuple(values)


def redact_secrets(text: str, extra_secrets: Iterable[str] = ()) -> str:
    """Sanitize ``text`` and additionally erase literal secret values.

    Used for the one place raw exception text is touched (the transport-failure
    log line in app/git/github.py), where a provider or library could otherwise
    echo the very token the request carried.
    """
    safe = sanitize_provider_message(text, fallback="")
    for secret in _configured_secret_values(extra_secrets):
        if secret in safe:
            safe = safe.replace(secret, _REDACTED_PLACEHOLDER)
    return safe


def sanitize_provider_message(
    message: str | None, fallback: str = "Git provider request failed"
) -> str:
    """Collapse and redact an external message so it is safe to surface.

    Applied to every provider-derived string before it is logged, audited or
    returned, so a token or private key that a provider echoed back cannot leak.
    """
    text = (message or "").strip()
    if not text:
        return fallback
    text = " ".join(text.split())
    for pattern in _SECRET_PATTERNS:
        text = pattern.sub(_REDACTED_PLACEHOLDER, text)
    for secret in _configured_secret_values():
        if secret in text:
            text = text.replace(secret, _REDACTED_PLACEHOLDER)
    if len(text) > _MAX_MESSAGE_CHARS:
        text = text[:_MAX_MESSAGE_CHARS].rstrip() + "..."
    return text


class GitProviderError(Exception):
    """A provider failure whose ``message`` is always safe to expose."""

    def __init__(self, code: str, message: str, *, status_code: int | None = None):
        safe = sanitize_provider_message(message)
        super().__init__(safe)
        self.code = code
        self.message = safe
        # Provider HTTP status, when there was one (never returned to clients).
        self.status_code = status_code

    @property
    def retryable(self) -> bool:
        return self.code in RETRYABLE_CODES


@dataclass(frozen=True)
class ProviderRepository:
    """Non-secret repository metadata exactly as the provider reports it."""

    provider_repo_id: str
    name: str
    owner: str
    url: str
    default_branch: str = ""

    @property
    def full_name(self) -> str:
        return f"{self.owner}/{self.name}" if self.owner else self.name


class GitProvider(Protocol):
    """The seam a second provider would implement. Every call may raise
    GitProviderError; none of them may return credentials."""

    def validate_connection(self) -> str:
        """Return a short non-secret label describing the authenticated target."""
        ...

    def list_repositories(self, *, limit: int = 100) -> list[ProviderRepository]:
        """Repositories the installation may select from (the allow-list source)."""
        ...

    def get_branch_sha(self, owner: str, name: str, branch: str) -> str | None:
        """Tip SHA of ``branch``, or None when the branch does not exist."""
        ...

    def branch_exists(self, owner: str, name: str, branch: str) -> bool:
        """True when the remote branch already exists."""
        ...

    def create_branch(self, owner: str, name: str, branch: str, sha: str) -> str:
        """Create ``branch`` at ``sha``; returns the created ref SHA (may be "")."""
        ...

    def delete_branch(self, owner: str, name: str, full_ref: str) -> bool:
        """Delete one exact heads ref; False means that ref was absent."""
        ...

    def build_branch_url(self, owner: str, name: str, branch: str) -> str:
        """Human-facing URL that opens the branch in the provider UI."""
        ...