# Security model

What the app enforces, where, and where it stops. [SECURITY.md](../SECURITY.md) has the
reporting policy and the summary list. Each statement below was checked against the code at
the path shown.

## Trust boundaries

| Boundary | Enforced by |
|---|---|
| Browser to API | session cookie, Origin/Referer check on mutating `/api/` requests (`app/main.py`, `csrf_check`; requests with neither header, such as curl, pass because they are not a browser deputy), security headers, role dependencies |
| API to database | SQLAlchemy parameters, project-membership scoping (`app/access.py`), `CHECK` constraints (`app/models.py`, added to existing PostgreSQL databases by `app/database.py`) |
| App to Git server | HTTPS only, credentials sent only to their own server, internal addresses refused (`app/net_guard.py`, `app/git/`) |
| App to cloud LLM | off by default; text is redacted first (`app/chatbot/redaction.py`) |

## Sessions and cookies (`app/auth.py`)

- Token = signed (itsdangerous) then encrypted (Fernet, key derived from `SESSION_SECRET`),
  so the user id is not readable client-side.
- Cookie: HttpOnly, `SameSite=Lax`, `Path=/`, lifetime `SESSION_TTL_SECONDS` (1 day).
- Name: `__Host-bh_session` when the cookie is `Secure` (`COOKIE_SECURE=true` or an
  `https://` `APP_BASE_URL`); `bh_session` over plain HTTP, because browsers reject the
  `__Host-` prefix without `Secure`. Reads accept both names, and setting the new cookie
  deletes the old one.
- A token must carry a session id (`jti`) that exists in the sessions table. Admins can end
  any session; changing a password bumps the user's `session_version`.
- Blank `SESSION_SECRET` uses a per-process random secret: sessions die on restart. Production
  refuses to start that way.
- `AUTO_LOGIN_ENABLED` signs every visitor in as the bootstrap admin. Local use only;
  production refuses it.

## Login abuse controls

| Control | Limit | File |
|---|---|---|
| Per-IP window, `POST /api/auth/login` | 8 / 60 s | `app/main.py` `_RATE_RULES` |
| `forgot-password` | 3 / 60 s | same |
| `reset-password`, `change-password` | 5 / 60 s each | same |
| Lockout per email+IP | `LOGIN_FAIL_LIMIT` (10) failures in `LOGIN_FAIL_WINDOW_SECONDS` (900 s), locked `LOGIN_LOCKOUT_SECONDS` (900 s) | `app/account_lockout.py` |
| Lockout per email, any IP | `LOGIN_FAIL_EMAIL_LIMIT` (50), never below `LOGIN_FAIL_LIMIT` | same |
| Comments / uploads / chat | 30 / 20 / 30 per minute per user | `app/routes/bugs.py`, `app/chatbot/router.py` |

Setting `LOGIN_FAIL_LIMIT=0` disables lockout. The lockout check runs before bcrypt, and
unknown emails are counted so response differences do not reveal which accounts exist. All
counters are per process and reset on restart (see operations.md for scaling). The client IP
is the socket peer unless `TRUST_PROXY_FORWARDED_FOR=true`, which then takes the
`TRUST_PROXY_HOP_COUNT`-th `X-Forwarded-For` entry from the right.

Passwords: bcrypt (cost 12) over a SHA-256 pre-hash so long passphrases are not truncated.
Policy: `PASSWORD_MIN_LENGTH`, `PASSWORD_REQUIRE_COMPLEXITY`, and the breach check
(`PASSWORD_BREACH_CHECK_ENABLED`, fails open on outage). Reset tokens are single-use,
hashed and expire after 2 hours; `FORGOT_PASSWORD_ENUMERATION_SAFE` keeps the response
identical for unknown emails.

## Attachments (`app/upload_policy.py`, `app/image_strip.py`)

- Allowlist, not denylist: the extension must be listed and the file's leading bytes must
  match it. Listed: png, jpg/jpeg, gif, webp, bmp, tif/tiff, heic, heif, avif, pdf, mp4, m4v,
  mov, 3gp, webm, mkv, avi, ogv, mpg/mpeg, wmv, doc, xls, ppt, docx, xlsx, pptx, odt, ods,
  odp, rtf, txt, log, csv, zip. Everything else, including SVG and HTML, is refused with 422.
- The stored content type comes from the extension table, never from the client.
- Trailing dots and spaces are dropped before the extension is read (`evil.exe.` is `exe`).
- 50 MB per file, 20 uploads per minute per user, request body ceiling `MAX_REQUEST_BODY_BYTES`.
- Downloads of anything other than images, video, audio, PDF and plain text/CSV are forced to
  `Content-Disposition: attachment`.
- JPEG, PNG and WebP lose EXIF, XMP, IPTC and comment data without being decoded; a JPEG's
  orientation tag is kept. GIF, BMP and TIFF are re-encoded within a 64 MiB decoded-pixel
  budget (16 megapixels at 4 bytes) and refused above it. Unreadable image bytes pass
  through unchanged, so cleanup never blocks a legitimate upload.
- ZIP-based formats (docx, xlsx, zip, ...) are checked by magic bytes only; their contents
  are not scanned. Run antivirus on the database host or at the proxy if that matters to you.

## Request handling

- Body limits: `MAX_REQUEST_BODY_BYTES` for uploads, `MAX_JSON_BODY_BYTES` for the rest;
  over-limit requests get 413 before the body is read.
- Every `422` is `{"detail": [{"type", "loc", "msg"}]}`, built in `app/main.py`; the
  submitted value is dropped so a rejected password is not echoed. Hand-raised 422s and
  out-of-range ids use the same shape.
- Rich text is sanitized server-side against an allowlist, then with DOMPurify in the browser.
- Excel exports neutralize formula injection; row ceiling `MAX_REPORT_ROWS`.
- GET requests do not write (hardening commit `8af878f`).
- CSP: `script-src 'self'`, `object-src 'none'`, `frame-ancestors 'none'`;
  `style-src` allows `'unsafe-inline'` because the SPA sets inline styles. HSTS is sent only
  when `COOKIE_SECURE=true`. The same headers are on 429, 403 and 500 responses.

## Onboarding preferences

`GET`/`PUT /api/me/preferences` (`app/routes/preferences.py`) store the welcome and tour state
of the signed-in user only. There is no user id in the URL, unknown fields are rejected, and
`tour_step` is bounded to 0-50. The first write is an atomic insert-if-missing, so two
simultaneous first requests cannot collide.

## Audit

Recorded: work-item, project, user, sprint, board, release, label and component changes;
sign-in, failed sign-in, lockout, logout, password change, reset request and reset. Rows
survive deletion of the item they describe. Not recorded: reads and most settings views.

## Known limits

- Rate limits, lockout and the digest scheduler are per process.
- Console email (`EMAIL_BACKEND=console`) is for development; production refuses it.
- Container image and dependency scanning run in CI (Trivy, pip-audit, npm audit, bandit,
  gitleaks); they find known issues only.
- No webhook, SSO or MFA support exists. Do not assume them.

## Hardening history

Commits on `main` that tightened this model, newest first:

| Commit | Change |
|---|---|
| `c497d85` | one shape for every 422; every status code routes raise is documented |
| `25ef54d` | lockout scoped to email+IP, higher per-email ceiling |
| `60a7a84` | upload extension denylist replaced by an allowlist checked against magic bytes |
| `ab2e909` | `__Host-` session cookie under HTTPS, encrypted payload |
| `ab9c14c` | database CHECK constraints, backup/restore scripts, safe deploy |
| `6c5cb21` | session cookies without a session id are refused |
| `8af878f` | input hardening, retention job, request IDs, read-only GETs |
| `f5cc7c6` | lossless image metadata stripping, off the event loop |
| `a275063` | Git credentials sent only to their own server, never to internal hosts |
| `daecf20` | password-policy exemption for legacy passwords removed |
| `03e9536` | people who cannot see a project cannot be attached to it |
