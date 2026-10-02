# Security Policy

## Supported versions

Only the latest release receives security fixes. Check the running version with
`GET /api/health` (or read `APP_VERSION` in `.env`); anything older is out of
support.

## Reporting a vulnerability

Do not open a public GitHub issue for security problems.

Open a private **GitHub Security Advisory** instead (*Security* tab → *Report a vulnerability*). This keeps the report confidential until a fix ships.

Include:

- The affected version (`GET /api/health` returns it, or see `APP_VERSION` in `.env`).
- Steps to reproduce — a minimal proof of concept is fine.
- Impact.
- A suggested fix, if you have one.

## Response

- We acknowledge within 5 working days.
- We assess severity within 10 working days.
- Patches are best-effort; we prefer coordinated disclosure.
- We credit you on request once the fix is public.

## Security posture

- **Login** — passwords are hashed with bcrypt. Session cookies are signed, HttpOnly, and SameSite (`Secure` over HTTPS). Sessions are stored server-side so admins can revoke any device.
- **Account protection** — accounts lock after repeated failed logins. Login timing is normalized so attackers cannot enumerate emails. An optional HaveIBeenPwned check runs whenever a password is set.
- **Password reset** — reset does not reveal whether an email exists. Tokens are single-use, hashed, and expire.
- **Email** — outbound headers are stripped of line breaks to block header injection. The console backend never logs message bodies (including live reset links).
- **CSRF** — state-changing requests get an Origin/Referer check on top of SameSite cookies.
- **Rate limiting** — per-IP and per-account limits on sensitive endpoints (login, reset, change-password, commenting, chat).
- **HTTP headers** — Content-Security-Policy (`script-src 'self'`, no CDN), plus `X-Content-Type-Options`, `X-Frame-Options`, `Referrer-Policy`, `Permissions-Policy`, cross-origin isolation headers, and HSTS over HTTPS. The same headers apply to error responses (`429`, `403`, and a generic `500`).
- **API surface** — interactive API docs (`/docs`, `/redoc`, `/openapi.json`) are served in development. A production deploy (`APP_ENV=production` or `COOKIE_SECURE=true`) disables them unless `ENABLE_API_DOCS=true`; the pages are self-hosted and CSP-safe.
- **Request limits** — a global cap on request body size, and row limits on list and report endpoints.
- **Authorization** — role checks are enforced the same way on both the REST and chat write paths, with a guard against mass-assignment when an item's type changes.
- **Tenant isolation** — every user, project, event, audit entry, invitation, saved view and webhook belongs to one organization, and everything else is scoped through its project. The scope is applied in every route, in bulk import, and in the assistant's retrieval and actions; another organization's id answers `404`, indistinguishable from a missing one. Email addresses are unique across organizations, so sign-in cannot reveal which organization an address belongs to.
- **Two-factor sign-in** — optional TOTP (RFC 6238). A correct password only yields a short-lived signed pending token; the session starts after a valid code. Each code is accepted once (a conditional update closes the race), the second step counts toward the per-account lockout, recovery codes are stored hashed and burn on use, and turning 2FA off or regenerating codes needs the password. Authenticator secrets are encrypted at rest when `FIELD_ENCRYPTION_KEY` is set.
- **Invitations** — tokens are random, stored hashed, single-use, expire after 7 days, and a new invitation voids the pending one. Managers can only attach projects they lead; only admins can invite admins.
- **Webhooks** — payloads are signed with HMAC-SHA256 (`X-BugHunter-Signature`); the secret is shown once and encrypted at rest with `FIELD_ENCRYPTION_KEY`. Targets on loopback, private, link-local and metadata addresses (including integer and IPv6 forms) are refused when saved and re-checked against the resolved addresses at delivery; redirects are never followed and the timeout is bounded.
- **Privacy** — people can export their data and delete their account; the last admin of an organization cannot. Audit rows older than `AUDIT_RETENTION_DAYS` are purged daily. `/api/metrics` is off by default and can require a bearer token.
- **Concurrent edits** — bug updates use optimistic concurrency. A stale save returns `409` instead of silently overwriting another user's change.
- **Uploads** — EXIF metadata is stripped on upload. Oversized or decompression-bomb images are rejected by a pixel budget. Content-type checks apply to all attachments.
- **Output** — stored rich text is sanitized server-side against an allowlist, then again in the browser with DOMPurify before rendering.
- **Data export** — the Excel writer defends against spreadsheet formula injection.
- **Assistant egress** — the optional cloud LLM is off by default. When enabled, all outbound text passes through a secret-redaction filter first. Sleuth never writes data through the model: even the multi-step agent uses only read-only tools, which re-check through the same write firewall as the REST API.
- **Audit trail** — every create, update, delete, and login is logged. The log survives item deletion.
- **Telemetry egress** — OTLP export to SigNoz stays off until `OTEL_EXPORTER_OTLP_ENDPOINT` is set; the collector URL and any auth headers live in `.env`/platform secrets (never in code or the image), and exported attributes matching password/token/secret/cookie patterns are stripped first (`OTEL_LOG_REDACT_SENSITIVE=true`).

## Git integration (branch creation and removal)

Creating one branch for a persisted User Story, and removing that exact branch,
are the only Git automations in the product. Project Git Settings configures the
integration only (API URL, organization, default base branch, credential,
connection test, read-only repository discovery) — it never selects, allow-lists
or activates individual repositories. Repository choice happens in the Story's
branch dialog, where the browser picks a provider repository id returned by the
read-only discovery endpoint.

- **Token handling.** A project PAT is entered in Project Git Settings as a
  write-only password input, sent only in the save/test HTTPS request, and
  encrypted immediately with the server-only `GIT_CREDENTIAL_ENCRYPTION_KEY`
  (Fernet; never derived from `SESSION_SECRET`). It is never returned by any
  API (responses expose only `credentials_configured`), never stored in
  plaintext, never placed in a query string, `localStorage` or
  `sessionStorage`, never prefetched into the input, and never written to
  logs or audit rows. A blank PAT entry preserves the stored credential;
  `Clear credential` removes only that project's PAT. `GITHUB_TOKEN` is read
  from server-side settings only and used solely when a project has no PAT.
- **Precedence.** Test Connection with a body tests exactly the visible draft
  (`None` = no override, `""` = explicit blank, never replaced) and persists
  nothing. Saved operations use the saved project value first and the
  environment only for a blank legacy field. Credentials: draft PAT for one
  request, then project PAT, then global fallback, then not-configured.
- **TLS.** Outbound Git calls verify with the OS trust store via `truststore`
  (Windows SChannel included). An optional approved PEM bundle
  (`GIT_CA_BUNDLE_FILE`) covers Linux containers behind interception; a
  missing/invalid bundle fails closed. Verification can never be disabled.
- **Minimum permissions.** A fine-grained personal access token with repository
  *Metadata: read* and *Contents: read and write*, scoped to the organisation
  and the specific repositories that may receive branches. No other scope is
  needed; the integration uses no other GitHub capability.
- **Story-only actions.** Preview, create and remove are refused with HTTP 422
  and code `user_story_only` for every non-Story item, before any provider call
  is built. One Story may hold one active branch per provider repository, and a
  Story may use several repositories.
- **Server-derived values.** The browser sends only `provider_repo_id`,
  `base_branch` and (for removal) the branch record id plus the expected
  version. Branch names, base SHAs, owners, URLs and repository-history ids are
  derived server-side; a client attempt to send them is rejected.
- **Least privilege.** Leave `GIT_BRANCH_CREATION_ENABLED=false` unless the
  feature is in use, and leave `GIT_BRANCH_DELETION_ENABLED=false` unless branch
  removal is required. A branch can only ever be created in a repository that
  the read-only discovery call just returned for that project, and only at the
  exact base branch the server validates against the provider.
- **Exact branch deletion.** Removal targets exactly one stored record and the
  exact `refs/heads/<stored-name>` it created. The server refuses wildcards,
  prefixes, bulk operations, protected names (`main`, `master`, `dev`,
  `develop`, `release`), any branch that is a project's base/default branch,
  records with no provider identity, non-Story records and non-Active records.
  Removal is a project-level cleanup action: any member of the project may
  remove a tracked branch, but only when the deployment opts in with
  `GIT_BRANCH_DELETION_ENABLED=true`, and only a `feature_` branch the app
  itself created. A provider 404 is treated
  as already absent; a timeout, TLS, auth, rate-limit or 5xx failure leaves the
  record Active so the action can be retried safely.
- **Rotation and revocation.** Supply `GITHUB_TOKEN` and
  `GIT_CREDENTIAL_ENCRYPTION_KEY` as server-side secrets (for example Azure
  Container App secrets) - never in the image, and never in the repository.
  Back up the Fernet key: losing or rotating it makes stored project PATs
  unreadable until re-entered. Rotate tokens on your normal schedule and
  immediately after any suspected exposure, and revoke replaced tokens.
- **Log redaction.** Provider errors are sanitized before they reach a client or
  a log; stored failure text is truncated and stripped of anything resembling a
  credential. Audit entries record safe identifiers (work item, repository,
  branch name) only - never the token and never a raw provider response.
- **Outbound requests.** Provider base URLs must be HTTPS (loopback HTTP only
  for local tests), are validated with no userinfo/query/fragment/path, and
  requests are bounded by a timeout with limited retries so a slow or hostile
  endpoint cannot hang the worker.
- **File hygiene.** `.env` (and `.env.*` except `.env.example`),
  `bug_hunter.db` / `*.db`, `secrets/`, corporate CA bundles, `.venv/` and
  `node_modules/` are excluded from version control (`.gitignore`) and from the
  Docker build context (`.dockerignore`). The local SQLite database file, CA
  files and credentials are therefore never baked into the runtime image.
  Platform deployments inject `DATABASE_URL`, `APP_BASE_URL` and secrets
  (`SESSION_SECRET`, `GIT_CREDENTIAL_ENCRYPTION_KEY`, `POSTGRES_PASSWORD`, …)
  from the platform secret store; an optional corporate CA is mounted read-only
  via `GIT_CA_BUNDLE_FILE` only where TLS interception requires it, and a blank
  value keeps OS-trust verification.

- **Fail-closed configuration.** `docker-compose.yml` has no default database
  password: `${POSTGRES_PASSWORD:?...}` makes Compose refuse to start when the
  value is unset, so a deployment can never boot with a guessable credential.
  The application's own `DATABASE_URL` is likewise platform-injected. Direct
  local development keeps its documented SQLite fallback and needs no secret.
- **Static-analysis policy.** SonarQube is a gate, not a cosmetic one: Bugs,
  Vulnerabilities and Security Hotspots are fixed in code, every hotspot is
  reviewed individually with its own justification, and no script or API call
  bulk-marks findings or hotspots as false positive / SAFE. The only
  configuration-level narrowing is for documented FastAPI idiom rules
  (style and documentation), never for a bug or security rule.
- **Coverage floor.** Releases are expected to remain at or above 80% coverage overall and on new code (see `README.md` and `sonar-project.properties`). Coverage never waives Bugs, Vulnerabilities, or Security Hotspots; those are still fixed or reviewed individually.

## Secret-history remediation

Deleting a secret file from the working tree does not remove it from git
history, and it does not revoke the secret. If a populated `.env` or a
service-account file was ever committed:

1. Treat the credential as compromised and rotate/revoke it at the source:
   rotate the Firebase service-account key in the Firebase console, and rotate
   every GitHub token, database password, session secret and SMTP credential
   that was in the file.
2. Review and purge the history before publishing the repository (for example
   `git filter-repo` or the BFG repo cleaner), then force-push the rewritten
   history.
3. Tell anyone who already cloned the repository so they can refresh their copy.

Keep `.env.example` placeholder-only, and never commit a real `.env`.

See also the *Live-data safety* and *Production checklist* sections of [README.md](README.md).
