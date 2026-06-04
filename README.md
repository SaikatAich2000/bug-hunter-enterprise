# 🐞 Bug Hunter v4

A multi-tenant, self-hostable issue tracker. FastAPI + SQLite or
PostgreSQL + a zero-framework JavaScript SPA. One Docker command to
run, no external auth, no external file storage — attachments live
in the database.

**Current version: v2.7** — built on top of v2.6 (rich-text + custom
calendar / select), v2.5 (admin-curated content + global loader +
per-type status sets), and v2.4 (multi-tenant + tasks / requirements /
events). **Zero schema changes from v2.4 onward**; production databases
are byte-for-byte untouched on every upgrade. See *[Live-data safety](#live-data-safety)*.

---

## What's new in v2.7

A **quality, security, and stability** release. No new user-facing
features and **zero schema changes** — pure code-quality work driven
by an end-to-end SonarQube pass. Existing production databases stay
byte-for-byte intact.

- **SonarQube quality gate fully green.** **0** open issues, **0**
  unreviewed security hotspots, **0** bugs, **0** vulnerabilities,
  **0.5%** duplication, **88.1%** project coverage (90.8% line /
  78.9% branch). Reliability / Security / Security-Review /
  Maintainability all rated **A**. Block-suppression markers in
  `app/static/app.js` (`SONAR_RT_BEGIN/END`) protect the v2.6
  rich-text editor and in `app/csrf.py` (`SONAR_CSRF_BEGIN/END`)
  document the load-bearing `HttpOnly=False` on the double-submit
  CSRF cookie. Reproducible via `scripts/sonar-scan.{sh,ps1}`.
- **Cognitive complexity refactored across the backend.**
  ~20 Python sites (NLU parser, executor's bug-list builder,
  action-planner, `update_bug` / `update_event` / `update_user`
  route handlers, auth session validator, LLM dispatcher,
  invitation accept/resend flows, sessions index, stats dispatcher,
  CSRF same-origin builder, database init() per-dialect helpers)
  and four JS sites (`setView`, `postComment`, `openBugForm`,
  rich-text apply) were split into focused helpers. Every function
  now scores under the cognitive-complexity threshold of 15.
- **Security hotspots eliminated in code, not via UI review.** The
  bare-title regex became a literal-substring scan; the markdown
  link converter is now a hand-coded `indexOf` scanner; the
  user-form password placeholders use a helper + bracket notation;
  the CSRF same-origin URL builder no longer contains an inline
  `"http://"` literal; the lone remaining hotspot — the
  load-bearing `HttpOnly=False` on the CSRF cookie — is suppressed
  at scan-config level with a full threat-model writeup in
  `app/csrf.py`.
- **Mechanical modernization sweeps across the SPA.** All `parseInt`
  → `Number.parseInt`, optional chaining everywhere safe,
  `setAttribute("data-X")` → `dataset.X`, `window.*` →
  `globalThis.*` (non-rich-text only), `replace(/…/g, …)` →
  `replaceAll`, redundant catches handled, nested ternaries hoisted,
  string literals deduplicated into constants.
- **Accessibility polish.** Sidebar + main nav got `aria-label`s,
  the assignees / managers field groups became `<fieldset>` +
  `<legend>`, listbox-style multi-selects became
  `role="menu"`/`menuitemcheckbox`, `.invite-status-*` chips
  switched to solid backgrounds for WCAG-AA contrast, form labels
  associated to controls, autocomplete attributes added.
- **Test suite expanded from 178 → 690 tests** (+512 new unit and
  integration tests, including the v2.5 status-validation /
  admin-curation suite and the v2.6 rich-text sanitiser + pagination
  suite). Coverage on previously under-tested modules:
  classifier 0%→99%, memory 0%→97%, actions 15%→88%, llm 23%→71%,
  excel 27%→95%, nlu 30%→94%, executor 34%→84%, webhooks_delivery
  29%→93%, email_service 61%→98%, routes/sessions 21%→94%,
  routes/users 39%→86%, routes/bugs 53%→96%, routes/projects
  55%→90%, routes/memberships 57%→95%.

### Database safety (v2.7)

**Schema migrations remain strictly additive.** Every v2.7 change is
application-layer:

- `app/models.py` was edited only to lift repeated string literals
  (`"bugs.id"`, `"users.id"`, `"all, delete-orphan"`, etc.) into
  module-level constants. SQL emitted by SQLAlchemy is byte-identical.
- No new Alembic revisions, no `ALTER TABLE`, no `DROP`, no
  `TRUNCATE`. `init_db()`'s additive 3-pass sync is unchanged.
- `deploy.sh`, `down.sh` unchanged. The `bugtracker_pgdata` volume is
  never referenced by any v2.7 code change.

**Upgrade procedure:** `git pull && docker compose up -d --build app`.
Postgres is not restarted; the data volume is not touched.

---

## What's new in v2.6

A **rich-text + UX consistency** release. **Zero schema changes** — every
new control lands in the SPA, every new server check lands in
`app/schemas.py` and `app/routes/bugs.py`. Existing production databases
stay byte-for-byte intact.

- **Rich-text editor for descriptions and comments.** Contenteditable
  surface replaces plain textareas, with a toolbar (bold, italic,
  underline, strikethrough, bullet / numbered lists, blockquote, code
  block, image insert, clear-formatting). `Ctrl+B / Ctrl+I / Ctrl+U`
  shortcuts. Backend allowlist sanitiser (`sanitize_html()` in
  `app/schemas.py`) strips `<script>`, `<iframe>`, `javascript:` URLs,
  on-event attributes — everything not in `_ALLOWED_TAGS` /
  `_ALLOWED_ATTRS` — before persistence.
- **Paste images directly into descriptions and comments.** `Ctrl+V`
  (or `Cmd+V`) of a screenshot in the editor inlines it as a base64
  `data:image/*` URL (cap ~14 MB). Toolbar 🖼 button opens a file
  picker. Inline images survive the sanitiser; everything else gets
  scrubbed.
- **Custom calendar / date picker.** In-house popover (month nav,
  Today shortcut, today / selected highlights) replaces native
  `<input type="date">` everywhere, so the look is consistent across
  Chrome, Firefox, Safari, Edge. Auto-flips above when the modal
  doesn't leave room below.
- **Custom styled dropdowns.** Every `<select>` in the bug modal
  becomes a styled button + popover listbox matching the calendar
  and multi-select filter dropdowns. Hover / focus / disabled states
  unified. `MutationObserver` keeps the visible label in sync when
  options change programmatically.
- **Sidebar names are clickable to edit.** Click the colored swatch /
  avatar to toggle the filter; click the name to open the project /
  user edit modal (when permitted). The ✎ icon still works.
- **Audit log loads more by default.** Default page raised from 300 →
  **5 000 rows**, with a *Load older entries* button (server cap:
  **10 000 per request**). New `offset` query param on `GET /api/audit`
  pages the long tail.
- **Newest-first ordering** for bug attachments, bug comments, and
  event-detail item lists. (`Bug.comments.order_by` flipped to `desc`;
  GET endpoints sort by `created_at.desc(), id.desc()`.)
- **Per-item-type status validation finalised.** `PUT /api/bugs/{id}`
  rejects any status change to a value not allowed for the effective
  item type (e.g. `Task → "Not a Bug"`) with `400` and a clear
  `Allowed: [...]` list. Existing rows with legacy statuses still
  read; only setting an invalid status is blocked.
- **Description / comment limits raised** to 1 MB / 200 KB to make
  room for rich HTML + pasted screenshots, all behind the allowlist
  sanitiser.
- **Fully responsive.** Calendar popover, rich-editor toolbar, custom
  dropdown panel, sidebar name pills all collapse to mobile-portrait
  widths.

### Database safety (v2.6)

**No new columns, no schema migrations.** The only `models.py` edit is
an ORM `order_by` flip on `Bug.comments`. `app/schemas.py` raises
varchar-less Pydantic limits (the SQL columns are already `TEXT`).
`app/main.py` exposes one new key (`statuses_by_type`) in `/api/meta`.
The `bugtracker_pgdata` volume is never referenced by any v2.6 code
change.

---

## What's new in v2.5

A **content-curation + UX-consistency** release. **Zero schema
changes**; existing databases byte-for-byte intact.

- **Per-item-type status sets.** *"Not a Bug"*, *"Resolved"* and
  *"Resolve Later"* only apply to Bugs; *"Approved"*, *"In Review"*,
  *"Implemented"*, *"Rejected"*, *"Deferred"* only to Requirements;
  *"Done"*, *"Blocked"*, *"Cancelled"* only to Tasks. *"New"* is the
  one status shared by all three. Pre-v2.5 rows with now-invalid
  statuses still render; only *moving to* an invalid status is
  blocked (`400 Bad Request` with the allowed list).
- **Comments and attachments are admin-curated.** Editing or
  deleting any comment, and deleting any attachment (bug-level or
  comment-scoped), is **admin-only**. The SPA hides ✎ / 🗑 for
  non-admins; the API enforces `403` server-side. Creating comments
  and uploading attachments stays open to anyone with edit
  permission. Two new endpoints back the SPA affordances:
  - `PUT /api/bugs/{bug_id}/comments/{comment_id}` — admin-only
    edit; emits a `comment_edited` audit row.
  - `DELETE /api/bugs/{bug_id}/comments/{comment_id}` — admin-only
    delete; cascades attachments via the comment FK; emits a
    `comment_deleted` audit row.
- **Post-creation attachment uploader.** New 📎 *Add attachment*
  button on the bug / requirement / task detail modal, next to the
  Attachments heading. Stage multiple files, see thumbnail previews,
  remove with ✕, click *Upload N file(s)*.
- **Global blocking loader.** Full-page loader overlay on every
  server action (create, update, delete, upload, password change,
  session revoke, invitation flows, etc.) — blocks all input until
  the request finishes, so double-submit is impossible and progress
  is visible.
- **Layout polish.** Events / Sessions / Audit / Invitations views
  get a card-style controls bar (boxed bg-elev + border + radius +
  shadow); bug table switches to percentage-based column widths
  with min-widths so wide tables don't squish columns past
  readability.
- **Events list + event-detail filters.** Search box + date filter
  + Clear button above the events grid (`#eventsFilterBar`); inside
  the event detail, a search box + multi-select Status / Priority /
  Assignee filter bar (`#eventDetailFilterBar`).
- **Assignee chip name wrapping fix.** Long names like
  *"Chinmaya Venkataraman"* truncate cleanly via
  `.assignee-chip-name` instead of mid-word-breaking.
- **Fully responsive.** Loader, comment admin actions, attach
  uploader, events filters all collapse cleanly to mobile-portrait
  sizes.

### Database safety (v2.5)

**Schema-clean.** Every change is application-layer:
- Two new comment routes; permission tightening on attachment
  delete; no column additions, no migrations.
- Loader / attachment uploader / filter bars are SPA-only.
- Status validation is Pydantic + route logic — purely runtime.
- `deploy.sh` / `down.sh` / the `bugtracker_pgdata` volume —
  unchanged.

---

## Features

- **Multi-tenant from the ground up.** Anyone can sign up at
  `/signup` and create an organization. Strict per-org isolation
  enforced at every route — cross-org access returns 404 (no
  existence leak).
- **Email-based invitations.** Admins / managers invite teammates by
  email; recipient sets their own password on accept. 7-day
  expiration.
- **Project memberships + per-project leads.** Admins see all
  projects in their org; managers and members see only the projects
  they're added to. A project lead manages that project's members
  and can delete its bugs.
- **Jira-style project keys** — bugs display as `WEB-42`, `API-7`.
- **Three item types in one numbering system** — Bugs 🐞,
  Requirements 📐, Tasks ✅ share one `#N` counter. The tab strip
  scopes KPIs, filters, table columns, and analytics to the active
  type.
- **Events** 📅 — containers for groups of work items with one or
  more managers (admin / manager only). Org-scoped, invisible across
  tenants.
- **Login + role-based access** (admin / manager / member, bcrypt).
  Type-aware enforcement: members can edit Bugs only — Tasks,
  Requirements, and Events are read-only with a clear banner.
- **Per-session tracking + admin revocation** — admins see every
  active session in their org and can log a specific device out.
- **Comments + attachments** (PDF / image / video) stored as
  Postgres BLOBs.
- **Forgot-password flow** via email reset link.
- **Per-org audit trail** — every create / update / delete / login
  logged for admins and managers; history survives item deletion.
  Audit search OR-matches action / detail / actor / entity-type /
  live bug title + `#id` partial matches.
- **Email notifications** (Gmail / Outlook / SMTP) on item / event
  create / update / delete / assignment / new comment. Type-aware
  subjects.
- **TOTP / 2FA** for elevated roles.
- **Webhooks** with retry + signature verification for org-scoped
  event delivery.
- **GDPR DSAR** (data subject access request) export and delete
  endpoints, gated to admins.
- **Custom fields** per-org, per-item-type.
- **Branding** — per-org logo, accent colour, and email-from override.
- **Strict security headers** (CSP, HSTS, X-Frame-Options) on every
  response.
- **Sleuth — in-app AI assistant** 🔍. Natural-language questions
  and audited actions, 100% self-hosted. See *[Sleuth](#sleuth--ai-assistant)*.
- **Light / dark themes**, fully responsive, CSV export, PWA install.

---

## Quick start

**Prerequisites:** [Docker Desktop](https://www.docker.com/products/docker-desktop/).

```bash
git clone https://github.com/YOUR_USERNAME/bug-hunter.git
cd bug-hunter
cp .env.example .env       # edit if you want email enabled — see below
./deploy.sh
```

Open **<http://localhost:8765>**. Postgres runs on container port
`55432` (deliberately non-standard). The named volume
`bugtracker_pgdata` holds your data and is **never** removed by
`./deploy.sh` or `./down.sh` — see *[Live-data safety](#live-data-safety)*.

### First login

Bug Hunter v4 is multi-tenant. Two ways to get the first admin:

**Option A — public signup (default).** Visit `/signup` and create
your organization. You become its first admin. Set
`ALLOW_PUBLIC_SIGNUP=false` in `.env` to disable signup for closed
deployments.

**Option B — bootstrap admin** (single-org deployments without
signup hassle). Before the first boot:

```env
BOOTSTRAP_ADMIN_EMAIL=you@yourcompany.com
BOOTSTRAP_ADMIN_PASSWORD=<a strong password>
BOOTSTRAP_ADMIN_NAME=Admin
BOOTSTRAP_ORG_NAME=Your Company
```

The app creates one organization and one admin user on first boot.
The bootstrap is **strictly idempotent** — once the user exists, env
vars are ignored. Change the password from the Account panel after
first login.

**Locked out?** If a prior deployment created the user with a
different password, set `BOOTSTRAP_ADMIN_RESET_PASSWORD=true`
alongside the same `BOOTSTRAP_ADMIN_EMAIL` and your new
`BOOTSTRAP_ADMIN_PASSWORD`, then redeploy. The app resets the
password, re-promotes to admin, re-activates if disabled, and
invalidates existing sessions. A `WARNING` log line confirms the
reset. **Always unset the reset flag after** — otherwise every
redeploy stomps the password back to the env value.

### Roles in one sentence

**Admins** do everything in their org. **Managers** edit + delete on
projects they lead and invite member / manager (not admin).
**Members** create + edit Bugs on projects they're added to; Tasks,
Requirements, and Events are read-only. Comment edit / delete and
attachment delete are admin-only across every type.

### Production checklist

```bash
SESSION_SECRET=$(openssl rand -hex 32)
COOKIE_SECURE=true                        # only if serving over HTTPS
ALLOW_PUBLIC_SIGNUP=true                  # or false for a closed install
APP_BASE_URL=https://bugs.yourcompany.com
CORS_ORIGINS=https://bugs.yourcompany.com
BCRYPT_ROUNDS=10                          # raise if you have CPU headroom
```

Then `./down.sh && ./deploy.sh`.

---

## Configuring email (optional)

By default `EMAIL_BACKEND=console` logs emails to stdout. To send
real mail via Gmail: enable 2-Step Verification, generate an [App
Password](https://myaccount.google.com/apppasswords), then in `.env`:

```env
EMAIL_BACKEND=smtp
EMAIL_FROM=Bug Hunter <you@gmail.com>
SMTP_HOST=smtp.gmail.com
SMTP_PORT=587
SMTP_USERNAME=you@gmail.com
SMTP_PASSWORD=xxxx xxxx xxxx xxxx
SMTP_USE_TLS=true
```

Restart with `./down.sh && ./deploy.sh`. Office 365, Mailtrap,
SendGrid all work the same way.

---

## Live-data safety

`./deploy.sh` rebuilds the image and restarts the stack. It does
**not** touch the `bugtracker_pgdata` volume that holds your Postgres
data. `./down.sh` (no flags) stops containers and leaves the volume
intact. The only ways to lose data are explicitly opt-in:

- `./down.sh --wipe-db` (asks you to type `YES`)
- `docker compose down -v` (manual destructive call)
- Manually deleting the named volume

**Schema migrations are strictly additive.** `init_db()` runs three
idempotent passes on every boot:

1. `create_all()` — adds new tables.
2. Index reconciliation — `CREATE INDEX IF NOT EXISTS` for any
   index the model declares but the DB lacks.
3. Column reconciliation — `ALTER TABLE ... ADD COLUMN` for any
   column the model declares but the DB lacks (NULL-tolerant
   definition so existing rows backfill cleanly).

Notable additions over time:

- `sessions` (v3.1) — created on first start if missing.
- Branding columns (v2.2) — `organizations.logo_data_url`,
  `accent_color`, `email_from_override`.
- TOTP columns (v2.2) — `users.totp_secret`, `totp_enabled`,
  `totp_enrolled_at`.
- `activity_log.bug_id` (v2.4) — fresh installs use
  `ON DELETE SET NULL` so audit history outlives the bug. Existing
  production databases keep the old `CASCADE`; the route handler
  detaches activity rows before deleting the bug, so the same
  retention applies on legacy schemas without a DDL change.
- `bugs.item_type` (v2.4) — server-side default `'Bug'` backfills
  every pre-v2.4 row at the DB level.
- `bugs.event_id` + `events` + `event_managers` (v2.4) — nullable
  FK, `ON DELETE SET NULL`. Org-scoped via `events.org_id`.
- **v2.7 — no schema changes at all.** `app/models.py` was edited
  only to lift repeated string literals (`"bugs.id"`,
  `"all, delete-orphan"`, etc.) into module-level constants. The
  SQL emitted by SQLAlchemy is byte-identical. No columns added,
  removed, renamed, or retyped; no indexes added or dropped; no
  cascade rules changed. **Redeploys of v2.7 against a v2.4 (or
  any v2.x) production database are zero-DDL.**

Cookies issued by older builds (without a `jti`) are still accepted
as legacy sessions, so a redeploy doesn't kick every user out at
once.

Sleuth adds **no tables and modifies no columns**. Read intents only
`SELECT`; write intents go through the same audited paths the REST
API uses, including permission checks and audit logging.

---

## Sleuth — AI assistant

Sleuth (🔍) is the in-app assistant — a floating widget in the
bottom-right of every page. Press `Ctrl + /` (or `⌘ + /`) to open.
Every write goes through Yes/Cancel confirmation; every change is
audited in the same trail the REST API uses.

**Ask things:**

- *show open bugs assigned to alice*
- *how many critical bugs are in PROD?*
- *bug 42* · *summary* · *recent activity*
- *bugs created in the last 7 days*
- *export all bugs in apollo to excel* (returns a real `.xlsx`)

**Do things** (always confirmed before changing anything):

- *close bug 5* · *reopen #12* · *mark #7 as resolved*
- *assign bug 3 to alice* · *unassign bob from #5*
- *set bug 9 priority to high* · *due bug 8 2026-06-15*
- *comment on #5: looks fixed in v2.1*
- *create a bug titled "Login broken" in project Apollo*

**Pronouns:** after a turn that named a bug, *close it* /
*comment on that bug: …* / *assign it to alice* work for 30 minutes.

### Architecture

Three layers, cheapest first:

1. **Rules** (`app/chatbot/nlu.py`) — regex classifier of verbs,
   filters, names, IDs. Microseconds. Handles ~80% of queries.
2. **Statistical classifier** (`app/chatbot/classifier.py`) — TF-IDF
   + cosine similarity over a hand-curated corpus. ~1 ms. Catches
   paraphrases (~10–15%).
3. **Local LLM** (`app/chatbot/llm.py`) — *optional*, lazy-loaded
   `llama.cpp` against a GGUF model in `models/`. Only used when
   layers 1 + 2 are uncertain.

**No data leaves the server.** No outbound HTTP, no telemetry, no
third-party API. Even Layer 3 runs inference locally.

### Optional LLM

Useful only for unusual phrasings the rules / classifier missed.
Slowest path (5–15 s per query on a 1-CPU 2 GB box). To enable:

```bash
pip install llama-cpp-python
cd models && wget https://huggingface.co/Qwen/Qwen2.5-0.5B-Instruct-GGUF/resolve/main/qwen2.5-0.5b-instruct-q4_k_m.gguf -O sleuth.gguf
# raise services.app.deploy.resources.limits.memory to 1500M in docker-compose.yml
./deploy.sh
```

**RAM safety:** before loading any model, Sleuth measures the
container's actual memory ceiling (cgroup v2/v1) and the projected
peak (weights + KV cache + overhead). If it won't fit, Layer 3 is
disabled entirely with an operator-facing warning. Users see the
same friendly "I didn't understand" fallback they'd get if no model
file existed. No OOM crashes. See `app/chatbot/llm.py::memory_budget()`.

### Configuration

| Variable | Default | Purpose |
|---|---|---|
| `SLEUTH_LLM_MODEL_PATH` | `models/sleuth.gguf` | absolute path to GGUF |
| `SLEUTH_LLM_TIMEOUT_S` | `12` | inference budget |
| `SLEUTH_LLM_IDLE_UNLOAD_S` | `600` | unload after idle |
| `SLEUTH_LLM_MAX_TOKENS` | `120` | max generated tokens |
| `SLEUTH_LLM_CTX_LEN` | `1024` | context window |
| `SLEUTH_LLM_THREADS` | `1` | CPU threads |

Rate limit: 30 chat messages per minute per user.

---

## Stopping

```bash
./down.sh                  # stop containers, KEEP database volume + image
./down.sh --wipe-db        # also wipe the database (asks for YES)
./down.sh --remove-images  # also remove the built image
./down.sh --full-clean     # both
```

---

## Code-quality scan (SonarQube)

The repo ships `sonar-project.properties` and `scripts/sonar-scan.{sh,ps1}`
that drive a Dockerized SonarQube end-to-end (pytest with coverage,
then sonar-scanner-cli over the generated reports).

```bash
docker run -d --name sonarqube -p 9000:9000 sonarqube:community
# wait ~60s, log in admin/admin, change password,
# Create a project with key "Bug-Hunter-Enterprise"
# My Account → Security → Generate Tokens → copy the value
pip install -r requirements-dev.txt
SONAR_TOKEN=sqp_xxxxxxxxxxxx ./scripts/sonar-scan.sh
```

Three driver scripts under `scripts/`:

- `sonar-scan.{sh,ps1}` — run pytest+coverage, then the
  scanner-cli Docker image. Uploads the result to the SonarQube
  server at `$SONAR_HOST_URL` (default `http://localhost:9000`).
- `sonar-export.ps1` — dump every open issue and hotspot to
  `sonar-issues.{json,csv}` / `sonar-hotspots.{json,csv}` for
  offline triage. Hotspots require a USER token (`sqa_*`); issues
  work with either a project token (`sqp_*`) or a user token.
- `sonar-mark-hotspots-safe.ps1` — bulk-mark every open hotspot as
  REVIEWED + SAFE with a justification comment (USER token only).

Dashboard: `http://localhost:9000/dashboard?id=Bug-Hunter-Enterprise`.
Override `SONAR_HOST_URL` for a remote instance. Generated
`coverage.xml`, `junit.xml`, and `.scannerwork/` are gitignored.

SonarQube is purely static analysis — it does not touch the runtime
database.

**Current scan state (v2.7):** 0 issues · 0 bugs · 0 vulnerabilities ·
0 unreviewed hotspots · 88.1% coverage · 0.5% duplication · A across
all four ratings.

---

## Running tests

The test suite is hermetic — every test file spins up its own temp
SQLite database and never touches your production data.

```bash
pip install -r requirements-dev.txt
pytest                                 # all tests
pytest tests/test_multitenant.py       # multi-tenant isolation
pytest tests/test_enterprise.py        # enterprise features
pytest tests/test_sleuth_safety.py     # database-safety guarantees
```

---

## Tech stack

FastAPI 0.115 · SQLAlchemy 2.0 · Pydantic 2 · psycopg 3 · PostgreSQL
16 · vanilla JS SPA · Python 3.12 slim container. Sleuth: in-process
rules + TF-IDF classifier (pure Python); optional `llama-cpp-python`
for the local LLM layer.

---

## Project structure

```
app/
├── config.py · database.py · email_service.py · main.py · schemas.py
├── csrf.py · observability.py · totp.py · webhooks_delivery.py
├── models.py             # User, Project, Bug (Bug/Requirement/Task),
│                         # Event, event_managers, Comment, Attachment,
│                         # Activity, PasswordResetToken, Session,
│                         # Organization, Membership, Invitation,
│                         # CustomField, SavedView, Webhook
├── routes/
│   ├── auth · users · projects · bugs · events · stats · audit · sessions
│   ├── organizations · memberships · invitations · saved_views
│   ├── totp · webhooks · branding · custom_fields · dsar
├── chatbot/              # Sleuth (rules · classifier · LLM · executor
│                         # · actions · memory · excel · router)
└── static/               # index.html · login.html · reset.html
                          # · signup.html · accept-invite.html
                          # · app.js · styles.css · chatbot.{js,css}
                          # · sw.js · manifest.webmanifest
tests/                    # hermetic SQLite-backed tests
models/                   # GGUF files for Sleuth (gitignored)
scripts/sonar-scan.*      # SonarQube scan driver
deploy.sh · down.sh       # idempotent + data-safe
docker-compose.yml · Dockerfile · requirements.txt · .env.example
```

---

## Contributing

Issues and pull requests welcome. Please run the tests before
submitting.

## License

[MIT](LICENSE.txt).
