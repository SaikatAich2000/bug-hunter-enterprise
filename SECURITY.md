# Security policy

## Supported versions

Only the latest minor release receives security fixes.

| Version | Supported |
|---------|-----------|
| 2.10.x  | ✅        |
| < 2.10  | ❌        |

## Reporting a vulnerability

**Do not open a public GitHub issue for security findings.**

Open a private **GitHub Security Advisory** on this repository
(*Security* tab → *Report a vulnerability*). That keeps the report
confidential until a fix ships.

Please include:

- Affected version (`GET /api/health` returns it, or check
  `app/__init__.py`).
- Reproduction steps — minimal proof of concept is fine.
- Impact assessment.
- Suggested fix (optional).

## Response

- Acknowledgement within **5 working days**.
- Triaged severity within **10 working days**.
- Patches on best-effort timeline; coordinated disclosure preferred.
- Credit on request once the fix is public.

## Existing security posture

See the **v2.10** entry in [CHANGELOG.md](CHANGELOG.md) and the
*Security & multi-tenant isolation* sections of [README.md](README.md)
for what's already in place — cookie auth with HttpOnly + SameSite +
signed token, CSRF double-submit middleware, per-IP rate limiting,
per-account login lockout, HaveIBeenPwned breach check on every
password set, optional TOTP 2FA, EXIF strip on uploads, attachment
content-type defenses, bcrypt password hashing, audit trail, and
strict per-organisation row-level isolation.
