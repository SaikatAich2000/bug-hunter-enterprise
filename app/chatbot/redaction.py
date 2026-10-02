"""Secret redaction: redact() before ANY outbound text to the cloud LLM.

Regexes scrub common secret shapes (keys, tokens, JWTs, PEM blocks, PII);
fails closed to [REDACTED].
"""
from __future__ import annotations

import re

_REDACTED = "[REDACTED]"

# repl None = keep label (group 1), scrub value; else replace whole match.
# Ordered most-specific first.
_PATTERNS: list[tuple[re.Pattern[str], str]] = [
    # PEM private key blocks (multi-line).
    (re.compile(
        r"-----BEGIN [A-Z ]*PRIVATE KEY-----.*?-----END [A-Z ]*PRIVATE KEY-----",
        re.DOTALL,
    ), _REDACTED),
    # JWTs: three base64url segments separated by dots.
    (re.compile(r"\beyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\b"),
     _REDACTED),
    # Auth tokens; separator group keeps `:` and space forms disjoint (ReDoS mitigation).
    (re.compile(
        r"(?i)\b(authorization|bearer|token)(\s*[:=]\s*|\s+)"
        r"(?:(?:bearer|token)\s+)?\S+"
    ), None),
    # key=value / "password": "x" / secret: x   (label kept, value scrubbed)
    (re.compile(
        r"(?i)\b(password|passwd|pwd|secret|api[_-]?key|access[_-]?token|"
        r"refresh[_-]?token|client[_-]?secret|private[_-]?key|session[_-]?secret)"
        r"(\s*[:=]\s*)(\"[^\"]+\"|'[^']+'|\S+)"
    ), None),
    # Provider key shapes: Google, OpenAI/OpenRouter, Stripe, GitHub.
    (re.compile(r"\bAIza[0-9A-Za-z_-]{30,}\b"), _REDACTED),
    (re.compile(r"\bsk-[A-Za-z0-9_-]{16,}\b"), _REDACTED),
    (re.compile(r"\b(?:sk|rk)_(?:live|test)_[A-Za-z0-9]{16,}\b"), _REDACTED),
    (re.compile(r"\b(ghp|gho|ghu|ghs|ghr)_[A-Za-z0-9]{20,}\b"), _REDACTED),
    # Fine-grained PATs have underscores, missed by the base64 rule below.
    (re.compile(r"\bgithub_pat_\w{20,}\b"), _REDACTED),
    # Other common token prefixes: npm, HuggingFace, DigitalOcean.
    (re.compile(r"\b(?:npm_|hf_|dop_v1_|doo_v1_)[A-Za-z0-9]{20,}\b"), _REDACTED),
    # Slack tokens and Google OAuth refresh tokens.
    (re.compile(r"\bxox[baprs]-[A-Za-z0-9-]{10,}\b"), _REDACTED),
    (re.compile(r"\b1//[A-Za-z0-9_-]{20,}\b"), _REDACTED),
    (re.compile(r"\bAKIA[0-9A-Z]{16}\b"), _REDACTED),
    # GitLab personal access tokens.
    (re.compile(r"\bglpat-[A-Za-z0-9_-]{20,}\b"), _REDACTED),
    # SendGrid API keys (SG.<id>.<secret>).
    (re.compile(r"\bSG\.[A-Za-z0-9_-]{16,}\.[A-Za-z0-9_-]{16,}\b"), _REDACTED),
    # Twilio SIDs (AC/SK + 32 hex); SK form isn't caught by the bare-hex rule.
    (re.compile(r"\b(?:AC|SK)[0-9a-fA-F]{32}\b"), _REDACTED),
    # Slack app-level tokens.
    (re.compile(r"\bxapp-[A-Za-z0-9-]{10,}\b"), _REDACTED),
    # Phone numbers (PII); specific enough to spare bare ids/counts/versions.
    (re.compile(r"\+\d[\d\s().-]{7,18}\d"), _REDACTED),
    (re.compile(r"\b\d{3}[-.\s]\d{3}[-.\s]\d{4}\b"), _REDACTED),
    # Email addresses (PII).
    (re.compile(r"\b[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}\b"), _REDACTED),
    # Long hex (>=32) / base64 (>=40) blobs are likely hashes or raw secrets.
    (re.compile(r"\b[0-9a-fA-F]{32,}\b"), _REDACTED),
    (re.compile(r"[A-Za-z0-9+/]{40,}={0,2}"), _REDACTED),
]


def redact(text: str) -> str:
    """Replace recognized secrets with [REDACTED]; any error fails closed to [REDACTED]."""
    if not text:
        return text or ""
    try:
        out = text
        for pat, repl in _PATTERNS:
            if repl is None:
                # Keep label + separator; scrub only the value.
                out = pat.sub(
                    lambda m: (m.group(1) or "")
                    + (m.group(2) if m.lastindex and m.lastindex >= 2 else "")
                    + " " + _REDACTED,
                    out,
                )
            else:
                out = pat.sub(repl, out)
        return out
    except Exception:  # noqa: BLE001 — redaction must never throw into the caller
        return _REDACTED  # fail closed


__all__ = ["redact"]
