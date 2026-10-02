# Bug Hunter Enterprise Edition

Bug Hunter Enterprise is a self-hosted, multi-tenant tracker for bugs, requirements, tasks and agile
delivery. It runs as a single Docker Compose stack: a FastAPI + PostgreSQL
backend and a React (JavaScript) frontend. It doesn't need an external login
provider, and attachments are stored in PostgreSQL, so one database backup
saves everything.

One installation hosts any number of **organizations**. Each has its own people,
projects, events, branding, invitations and webhooks, and nothing is visible across
organizations. Sign-in is by email, which also tells Bug Hunter which organization you
belong to.

On top of classic issue tracking it has opt-in, per-project **Sprints & Agile**
(boards, a ranked backlog, sprint planning, releases and reports), optional
**GitHub feature branches** for User Stories, **bulk import** from Excel/CSV,
**OpenTelemetry** observability, and an in-app assistant called **Sleuth**. The enterprise
layer adds two-factor sign-in, project leads, custom fields, saved views, signed webhooks,
per-organization branding, and data-subject tools (export and delete).

Upgrades only *add* tables and columns. Existing data is never removed or
changed (see [Live-data safety](#live-data-safety)). The running version is
whatever `APP_VERSION` in `.env` says, and `/api/health` reports it.

## Contents

- [Features](#features)
- [Organizations & access](#organizations--access)
- [Sprints & Agile](#sprints--agile)
- [Architecture](#architecture)
- [Quick start](#quick-start)
- [Configuration](#configuration)
- [Local development](#local-development)
- [Live-data safety](#live-data-safety)
- [Upgrading from earlier editions](#upgrading-from-earlier-editions)
- [Deployment](#deployment)
- [Sleuth](#sleuth)
- [Git integration (Story feature branches)](#git-integration-story-feature-branches)
- [Limitations](#limitations)
- [Troubleshooting](#troubleshooting)
- [Security, contributing, license](#security)

## Features

| Area | What it does |
|---|---|
| Work items | Bugs, requirements and tasks share one `#N` counter with the agile types (Epic, Story, Sub-task). A tab strip filters KPIs, columns and analytics by type. Each type has its own statuses; bugs also have a DEV/UAT/PROD environment. Items can be converted between types in place. |
| Sprints & Agile | Opt-in per project, modelled on Jira Scrum: ranked backlog and sprint planning, the active sprint board, Epic → issue → Sub-task hierarchy, board settings, and burndown/sprint/velocity/flow reports. See [Sprints & Agile](#sprints--agile). |
| Projects & events | Projects group your work. Events group items for a standup or review and have one or more managers. |
| Item links | Link items together: relates, blocks, or duplicate. |
| Comments & attachments | Rich-text comments (bold, italic, lists, code, quotes) and plain-text descriptions that keep line breaks. PDF, image and video files are stored in PostgreSQL. Pasted images become real attachments, and image metadata (EXIF) is stripped. |
| Bulk actions | Change status, priority or environment, or delete, across many items at once. |
| Bulk import | Download an Excel template, fill it in (or use CSV), and import. Every row is validated on its own and errors are reported per row. |
| Reports | A report builder (manager/admin) that exports a multi-sheet Excel file, plus agile reports (burndown, burnup, velocity, cumulative flow, control chart, workload, scope change, epic progress). |
| Notifications | In-app bell, email (per event or one daily digest), and optional browser/FCM push. Every notification links straight to its item or event, through the login page if needed. |
| Audit log | Every create, update, delete and login is recorded for admins and managers, who can filter it and export it as CSV. Entries stay even after an item is deleted (until `AUDIT_RETENTION_DAYS`). |
| Sessions | Admins see every active session (user, role, IP, browser, time) and can log out a single device. |
| Organizations | Public sign-up (switchable) creates an organization and its first admin; the bootstrap admin creates the first one. Every record belongs to one organization, and another organization's data answers 404. |
| Invitations | Admins and managers invite people by email with a role and optional projects (as member or lead). Links are single-use, expire after 7 days and are stored hashed. |
| Login | Local accounts with bcrypt-hashed passwords, three roles (admin / manager / user), email password reset, a verified email change, per-account lockout, and optional **two-factor sign-in** with an authenticator app and one-time recovery codes. |
| Project leads | Each project member is a *lead* or a *member*. Leads (and admins) manage the project's members and custom fields. |
| Custom fields | Per-project text, number, date or choice fields, optionally required, shown on every item form. |
| Saved views | Save the current filters under a name; admins and managers can share a view with the whole organization. |
| Webhooks | Admins send item and comment events to HTTPS endpoints, signed with HMAC-SHA256. Private networks are refused; a failing endpoint is suspended automatically. |
| Branding | Per organization: logo, accent colour and the From address of its emails. |
| Privacy | A public privacy notice and delete-account page, and, in Account settings, a JSON export of your data and self-service account deletion. Old audit rows are purged after `AUDIT_RETENTION_DAYS`. |
| Metrics | Optional Prometheus counters at `/api/metrics`. |
| Git integration | Optional: create a deterministic `feature_<id>_<slug>` branch in a GitHub repository for a User Story, and remove exactly that branch again. |
| Observability | Optional OpenTelemetry export (traces, metrics, logs) to SigNoz or any OTLP/gRPC collector. |
| API docs | Self-hosted Swagger UI (`/docs`) and ReDoc (`/redoc`) in development. Production hides them unless `ENABLE_API_DOCS=true`. |
| Sleuth assistant | Answers plain-English questions and runs actions (with confirmation). Runs locally by default; see [Sleuth](#sleuth). |
| UI | The Bug Hunter Steam-style dark theme and a light theme, responsive layout, auto-refresh. |

### Roles

Organization roles:

- **Admin** has full access in the organization, including user management, branding, webhooks and all deletes, and sees every project.
- **Manager** can edit any item or event they can see, enable Agile, manage sprints, boards, releases and labels, and invite people. Managers can't delete items, grant the admin role, or edit existing admins.
- **User** can create and edit Bugs, User Stories and Sub-tasks, move them on the board, and view and export agile reports. Tasks and Requirements are read-only to users, and sprint lifecycle and board/workflow/taxonomy configuration need a manager.

Managers and users see only the projects they belong to. Within a project, a member is a
**lead** or a plain **member**; leads manage that project's members and custom fields even
when their organization role is *user*.

## Organizations & access

**Tenancy.** Users, projects, events, audit entries, invitations, saved views and webhooks
carry an organization; everything else (items, comments, boards, sprints, …) is scoped
through its project. An id from another organization answers `404 Not Found`, the same as an
id that doesn't exist, on every endpoint, in bulk import and in Sleuth. Email addresses are
unique across the whole installation, so signing in needs no organization picker.

**Getting started.**

- *Sign-up:* with `ALLOW_PUBLIC_SIGNUP=true` (the default) anyone can create an organization at `/signup`: it starts with a *General* project and its creator is the admin. Turn it off for a closed installation.
- *Bootstrap:* `BOOTSTRAP_ADMIN_EMAIL` / `_PASSWORD` create `BOOTSTRAP_ORG_NAME` and its admin on first boot. Leave the email empty to start with no account and rely on sign-up.
- *Invitations:* **Organization → Invitations**. A manager can only attach projects they lead; only admins invite admins. An invitation for an address that already has an account anywhere is refused.

**Visibility.** Admins see every project of their organization. Everyone else sees the projects
they belong to, and events that belong to those projects. Events without a project are
visible to admins only.

**Two-factor sign-in.** *Account settings → Security → Enable 2FA* shows a QR code (and the
key) for any authenticator app, then confirms with a code and shows ten one-time recovery
codes once. A code works once: replaying it fails. Turning it off, or making new recovery
codes, needs the password. An admin can turn it off for someone who lost both their
authenticator and their recovery codes (open the person in the sidebar, **Turn off their 2FA**).
`TOTP_ENABLED=false` switches the feature off for the server.

**Webhooks.** *Organization → Webhooks* (admins). Events: `bug.created`, `bug.updated`,
`bug.deleted`, `comment.added`, `bugs.bulk_updated`, `bugs.bulk_deleted` and the test
`webhook.ping`; subscribe to `*`, a name, or a family such as `bug.*`. Each delivery is a
`POST` of

```json
{"delivery_id": "…", "event": "bug.created", "org_id": 1, "delivered_at": "…", "payload": {}}
```

with the headers `X-BugHunter-Event`, `X-BugHunter-Delivery` (same as `delivery_id`; use it to
deduplicate) and `X-BugHunter-Signature: sha256=<hex>`, the HMAC-SHA256 of the raw body with the
hook's secret. The secret is shown once, when the hook is created or rotated; with
`FIELD_ENCRYPTION_KEY` set it is encrypted in the database. Deliveries run right after the
request commits, are best-effort (no queue, redirects are not followed) and are lost if the
worker restarts mid-flight. Ten failures in a row suspend the hook until an admin resumes it.
Targets on loopback, private or link-local networks are refused when saved and again when
delivered; `WEBHOOK_ALLOW_PRIVATE_NETWORKS=true` allows them for listeners inside your own
network.

**Branding.** *Organization → Branding*: logo (PNG, JPEG, SVG, GIF or WebP up to 100 KB), accent
colour (applied with automatically readable text) and the From address of the organization's
emails (your mail server must be allowed to send as it).

**Privacy and data rights.** `/privacy` and `/delete-account` are public pages (the latter is
the deletion URL an app-store listing asks for). In *Account settings → Privacy* people download
a JSON file of what is stored about them or delete their account; items they reported stay,
without their name. The last admin of an organization can't delete the account until another
admin exists. `PRIVACY_CONTACT_EMAIL` is shown on the privacy page.

**Audit retention.** Audit rows older than `AUDIT_RETENTION_DAYS` (default 365, `0` keeps
everything) are deleted once a day. Reports built from history, such as status changes and time
to resolution, only cover that window.

**Metrics.** With `METRICS_ENABLED=true`, `GET /api/metrics` serves Prometheus counters
(request counts and latency per route, logins, webhook deliveries); set `METRICS_TOKEN` to
require `Authorization: Bearer <token>`. Counters are per worker process.

Deleting items, editing/deleting comments, and deleting attachments are
admin-only for every type. Everyone sees only the projects they are a member of,
except admins.

## Sprints & Agile

Sprints work the way Jira Software's Scrum boards do. Agile is **off for every
project** until a manager or admin opens **Sprints**, picks the project and
clicks **Set up Scrum board**. That creates the project's board (To Do, In
Progress, Testing, Done) and a backlog ranked from its open issues; nothing is
deleted or changed.

**Hierarchy.** There are three levels, as in Jira:

- **Epic**: a large body of work. Epics are never in a sprint.
- **Standard issues**: Story, Task, Bug and Requirement. Each can belong to one
  Epic and is planned into sprints.
- **Sub-task**: always has a standard issue as its parent, and always follows
  that parent's sprint and Epic.

The server enforces these rules on every write path (API, bulk edit, import,
Sleuth), and records each change to status, sprint, estimate, Epic, parent,
type and project in a change log the reports are built from.

| Tab | What it does |
|---|---|
| Backlog | Future and active sprints above the ranked backlog. Drag issues (or several ticked issues) to rank them or plan them into a sprint; the ⋯ menu and the keyboard do the same without dragging. Create, edit, start and complete sprints here. Starting needs at least one issue and opens the board. Completing keeps finished issues with the closed sprint and moves the rest to the backlog, a future sprint or a new sprint. |
| Active sprint | The board: columns map to statuses, and dragging a card moves it to that column's status. Column min/max (WIP) limits, swimlanes (stories, assignees, Epics, priority), quick filters and flags. An issue is done when its status is in the right-most column. |
| Epics | Each Epic with its issues and their sub-tasks, progress by estimate, and the issues not yet in an Epic. Link or unlink issues here. |
| Reports | Burndown, burnup, sprint report, velocity, cumulative flow diagram, control chart, Epic report, daily summary and workload, each with CSV export. |
| Releases & labels | Versions/releases, components and labels. |
| Capacity | Per-sprint capacity: each member's hours per day and days off against the committed estimate. |
| Board settings | Columns and their statuses, column limits, estimation statistic (story points, hours or issue count), working days and time zone (used by the burndown guideline), and quick filters. |

Reports replay the change log, so a sprint's commitment is what it held when
it started, scope added or removed afterwards is shown separately, and
re-estimates count as scope changes. Lifecycle and bulk endpoints accept an
`Idempotency-Key` header so retried requests are safe.

**Upgrading from the earlier Sprints preview.** The first boot converts the
retired Collection and Feature levels into labels on the issues they grouped
(a Story under a Feature joins that Feature's Epic), turns sub-tasks without a
valid parent into Tasks, takes Epics out of sprints and re-ranks every issue in
the list it sits in. The conversion runs once and is safe to repeat.

## Architecture

- **Backend:** FastAPI, SQLAlchemy 2.x and Pydantic 2. PostgreSQL 16 in production; SQLite for tests and quick local runs.
- **Frontend:** React 18 in plain JavaScript (JSX), built with Vite into `app/static`, which FastAPI serves directly. Tested with Vitest and linted with ESLint.
- **Packaging:** Docker Compose runs the app and its own PostgreSQL. The image is built on `python:3.12-slim`, and the frontend is compiled in a throwaway `node:20-slim` stage.
- **Sleuth:** pure-Python rules and a TF-IDF classifier, plus an optional local LLM and an optional cloud LLM with tool calling.

```
app/
├── config.py · database.py · main.py · models.py · schemas.py · telemetry.py
├── auth.py · access.py · tenancy.py · project_keys.py   # sessions · tenant scoping · org setup
├── totp.py · secrets_box.py · webhooks_delivery.py · metrics.py
├── email_service.py · notification_service.py · push_service.py · fcm_transport.py
├── bulk_import.py  # spreadsheet template + row-by-row validation
├── agile/       # boards · workflow · sprints · backlog/ranking · planning
│                # hierarchy · integrity · item types · releases · taxonomy
│                # reports · permissions · idempotency · upgrade
├── git/         # branches · naming · provider · github · credentials · tls
├── routes/      # auth · totp · dsar · organizations · invitations · users · projects
│                # memberships · custom_fields · saved_views · webhooks · devices
│                # bugs · events · stats · audit · sessions · reports
│                # notifications · push · git
│                # agile · agile_board · agile_planning · agile_reports · agile_taxonomy
├── chatbot/     # Sleuth: nlu · classifier · llm · cloud_llm · redaction
│                # rag · retrieval · verify · agent · evals · tools
│                # llm_tools_agent · executor · actions · memory · excel · router
├── jobs/        # email_digest · audit_retention
└── static/      # built React bundle + icon.png / favicon.png
frontend/        # React + Vite SPA source → builds into app/static
tests/           # SQLite-backed pytest suite (+ Playwright browser suites, marker `ui`)
scripts/         # load test, release packaging, SonarQube, RAG index builder
models/          # GGUF files for Sleuth's optional local LLM (gitignored)
```

There's no `migrations/` folder or Alembic. Schema changes are additive edits
in `app/models.py`, applied automatically and idempotently by `init_db()` on
every boot. On PostgreSQL, a database advisory lock serializes concurrent boots.

## Quick start

You need [Docker Desktop](https://www.docker.com/products/docker-desktop/) (Docker Engine + Compose v2).

```bash
git clone https://github.com/<your-org>/bug-hunter-enterprise.git
cd bug-hunter-enterprise
cp .env.example .env       # set APP_VERSION, POSTGRES_PASSWORD, BOOTSTRAP_ADMIN_* at minimum
./deploy.sh
```

`./deploy.sh` builds the image (including the frontend), starts PostgreSQL,
waits for it to be healthy, then starts the app. Open <http://localhost:8765>.

PostgreSQL runs in its own container, published only on `127.0.0.1:55432` so it
doesn't clash with a local PostgreSQL install. Data lives in the named volume
`bugtracker_pgdata`, which `./deploy.sh` and `./down.sh` never delete.

For a clean rebuild, run `BUILD_CLEAN=1 ./deploy.sh`. Behind a corporate proxy or
air gap, set `BASE_IMAGE` in `.env` to an internal mirror, or pre-load
`python:3.12-slim` and `node:20-slim` with `docker save | docker load`.

### First login

On first boot, Bug Hunter creates the organization `BOOTSTRAP_ORG_NAME` with an admin from
`BOOTSTRAP_ADMIN_EMAIL` / `BOOTSTRAP_ADMIN_PASSWORD` (Compose defaults the email
to `admin@bughunter.local`; the password has no default and must be set). Change
the password right away from the profile menu. Other organizations are created at `/signup`
or by that admin's invitations; turn sign-up off with `ALLOW_PUBLIC_SIGNUP=false`. A production deploy refuses to
start while the `.env.example` placeholder password is still set.

Set `AUTO_LOGIN_ENABLED=true` to skip the login screen entirely: every visitor
is signed in automatically as the bootstrap admin. Use it only on a trusted
local deployment. Production refuses to start with it on.

### Production checklist

```bash
APP_VERSION=<release tag>
SESSION_SECRET=$(openssl rand -hex 32)    # at least 32 characters, never the placeholder
COOKIE_SECURE=true                        # only when serving over HTTPS
BOOTSTRAP_ADMIN_EMAIL=you@example.com
BOOTSTRAP_ADMIN_PASSWORD=<a strong password>
APP_BASE_URL=https://bugs.example.com
CORS_ORIGINS=https://bugs.example.com
APP_ENV=production                        # strict checks: https URL, secure cookies, real email backend
ALLOW_PUBLIC_SIGNUP=false                 # unless anyone may create an organization on your host
FIELD_ENCRYPTION_KEY=<Fernet key>         # encrypts 2FA and webhook secrets (see .env.example)
PRIVACY_CONTACT_EMAIL=privacy@example.com
```

Then `./down.sh && ./deploy.sh`. If a requirement is unmet, the app logs every
problem at once and refuses to start.

### Stopping

```bash
./down.sh                  # stop containers; keep database volume and image
./down.sh --wipe-db        # also delete the database volume (asks for confirmation)
./down.sh --remove-images  # also remove the built image
./down.sh --full-clean     # both
```

## Configuration

All settings come from environment variables. Copy [`.env.example`](.env.example)
to `.env`. Every variable is explained inline there and read in
[`app/config.py`](app/config.py). Never commit `.env`. Secret values belong in
`.env` (local), a Compose `--env-file`, or a platform secret store, never in the
image or the repository. The ones that matter most:

| Variable | Default | Purpose |
|---|---|---|
| `APP_VERSION` | _(required)_ | Release shown in the UI, API docs and `/api/health`, and the image tag. The single source of truth; there's no hardcoded fallback. |
| `APP_NAME` | `Bug Hunter` | Product name in HTML titles, API docs, email subjects, reports and notifications. |
| `APP_ENV` | _(blank = development)_ | `production` turns on the strict start-up checks. |
| `SESSION_SECRET` | _(blank)_ | Signs session cookies. Blank makes a new secret every restart, which logs everyone out, and is refused in production. |
| `COOKIE_SECURE` | `false` | Set `true` only when serving over HTTPS. Also counts as a production deploy. |
| `APP_BASE_URL` | `http://localhost:8765` | Public URL used in email links. |
| `CORS_ORIGINS` | _(blank = same-origin)_ | Comma-separated list of allowed cross-origin clients. |
| `ENABLE_API_DOCS` | `false` | Serve `/docs`, `/redoc` and `/openapi.json` in production too. They are always on in development. |
| `BOOTSTRAP_ADMIN_EMAIL` / `_PASSWORD` / `_NAME` | `admin@bughunter.local` (Compose) / _(required)_ / `Admin` | First admin, created only when the database has no users. |
| `BOOTSTRAP_ORG_NAME` | `Default Organization` | Organization created together with the bootstrap admin. |
| `BOOTSTRAP_ADMIN_RESET_PASSWORD` | `false` | Recovery: every boot resets the bootstrap admin's password to `BOOTSTRAP_ADMIN_PASSWORD`. Turn it off again afterwards. |
| `ALLOW_PUBLIC_SIGNUP` | `true` | Anyone may create an organization at `/signup`. |
| `TOTP_ENABLED` | `true` | Two-factor sign-in with an authenticator app (each user opts in). |
| `BCRYPT_ROUNDS` | `12` | Cost of new password hashes (minimum 10). |
| `FIELD_ENCRYPTION_KEY` | _(blank)_ | Fernet key encrypting 2FA and webhook secrets at rest. Values written under a key can't be read without it. |
| `AUDIT_RETENTION_DAYS` | `365` | Audit rows older than this are deleted daily; `0` keeps everything. |
| `WEBHOOK_ALLOW_PRIVATE_NETWORKS` | `false` | Allow webhook targets on private networks. |
| `PRIVACY_CONTACT_EMAIL` | _(blank)_ | Contact shown on `/privacy`. |
| `METRICS_ENABLED` / `METRICS_TOKEN` | `false` / _(blank)_ | Prometheus counters at `/api/metrics`, optionally behind a bearer token. |
| `AUTO_LOGIN_ENABLED` | `false` | Sign every visitor in as the bootstrap admin. Trusted local use only. |
| `EMAIL_BACKEND` | `console` | `console` (log to stdout), `smtp`, or `disabled`. |
| `EMAIL_DIGEST_ENABLED` | `false` | Batch per-event emails into one daily digest. |
| `MAX_REPORT_ROWS` | `50000` | Max rows in one Reports Excel export (returns 413 above it). |
| `WEB_PUSH_ENABLED` | `false` | Master switch for browser push (FCM). |
| `GIT_BRANCH_CREATION_ENABLED` / `GIT_BRANCH_DELETION_ENABLED` | `false` / `false` | Story feature branches; see [Git integration](#git-integration-story-feature-branches). |
| `GIT_CREDENTIAL_ENCRYPTION_KEY` | _(blank)_ | Fernet key that encrypts per-project Git tokens. Required before a token can be saved. |
| `OTEL_EXPORTER_OTLP_ENDPOINT` | _(blank)_ | OTLP/**gRPC** collector, e.g. `http://otel-collector:4317`. Blank means console-only, with nothing exported. |
| `LOG_FORMAT` | `text` | `json` writes one JSON object per line for log shippers. |
| `SLEUTH_CLOUD_ENABLED` | `0` | Opt-in cloud LLM fallback for Sleuth. |

### Observability (OpenTelemetry / SigNoz)

Set `OTEL_EXPORTER_OTLP_ENDPOINT` and the app streams three signals to the
collector over OTLP/gRPC. Leave it blank and everything stays on the console.

| Signal | Details |
| --- | --- |
| **Traces** | One server span per HTTP request from FastAPI, DB spans from SQLAlchemy, and outbound spans from `httpx`. Health checks and `/static` are excluded by default (`OTEL_TRACES_EXCLUDED_URLS`). |
| **Metrics** | HTTP server and DB client metrics, exported every `OTEL_METRICS_EXPORT_INTERVAL_MS` (default 60 s). |
| **Logs** | The same `bug_hunter.*` records as the console, with `trace_id`/`span_id` so a log pivots to its request. Audit-trail entries arrive as `audit …` records. Sensitive attribute keys are redacted before export (`OTEL_LOG_REDACT_SENSITIVE`). |

The service name defaults to `bug-hunter` (`OTEL_SERVICE_NAME`), and the
deployment environment defaults to `APP_ENV` (`OTEL_DEPLOYMENT_ENVIRONMENT`).
Per-signal switches (`OTEL_TRACES_ENABLED`, `OTEL_METRICS_ENABLED`,
`OTEL_LOGS_ENABLED`), the sampler (`OTEL_TRACES_SAMPLER`), collector headers and
TLS options are all documented in `.env.example`.

**No traces?** Check, in order: the startup log says `OTLP export enabled -> …`,
`OTEL_TRACES_ENABLED` isn't `false`, the sampler isn't `always_off`, and your
collector's environment filter includes `OTEL_DEPLOYMENT_ENVIRONMENT`.

### Email (optional)

By default, `EMAIL_BACKEND=console` prints emails to the log. For real delivery,
set `EMAIL_BACKEND=smtp` and the `SMTP_*` variables (host, port, username,
password, TLS), then restart. Any standard SMTP provider works. For Gmail, use an
[App Password](https://myaccount.google.com/apppasswords), not the account
password.

Set `EMAIL_DIGEST_ENABLED=true` to batch each user's notifications into one
email per day. Password-reset and other security emails always send immediately.
With the digest on, immediate work-item emails are off, so make sure a scheduler
runs. Either run `python -m app.jobs.email_digest` from cron/Task Scheduler, or
set `EMAIL_DIGEST_CRON` (5-field cron) and `EMAIL_DIGEST_TIMEZONE` (IANA name) to
let the app run it itself (the log confirms with `Email-digest scheduler started`).
The job is idempotent, bounded by `EMAIL_DIGEST_LOOKBACK_HOURS` (default 50), and
retries failed sends on the next run.

### Web push (optional)

Browser push uses Firebase Cloud Messaging and is off by default. One-time setup:

1. Create or reuse a project at <https://console.firebase.google.com>.
2. Add a Web app and copy its config into `FIREBASE_API_KEY`, `FIREBASE_AUTH_DOMAIN`, `FIREBASE_PROJECT_ID`, `FIREBASE_MESSAGING_SENDER_ID` and `FIREBASE_APP_ID`.
3. Generate a Web Push key pair and put the public key in `FIREBASE_VAPID_KEY`.
4. Provide a service-account key in one of two ways:
   - **File mount** (default Compose setup): save it as `secrets/firebase-admin.json` (`FCM_CREDENTIALS_FILE`).
   - **Env var** (no mount, e.g. Azure Container Apps): put the JSON, raw or base64-encoded, in `FCM_CREDENTIALS_JSON` as a platform secret. It takes priority over the file.
5. Set `WEB_PUSH_ENABLED=true`, restart, and serve over HTTPS (`localhost` is exempt).

Each user then enables push once from the profile menu. The Firebase SDK is
self-hosted (no CDN). If a token fails to register, the client retries with
backoff and again on reconnect. Devices still need to reach Google's FCM
endpoints, and on networks that block them push never arrives. The in-app bell
doesn't depend on FCM.

## Local development

### One command

```powershell
.\scripts\run_local.ps1              # Windows; add -Reload to auto-restart on code changes
```

```bash
bash scripts/run_local.sh            # macOS / Linux / Git Bash; RELOAD=1 for auto-restart
```

The first run creates `.venv` (Python 3.12, via [uv](https://docs.astral.sh/uv/)
when installed) and a `.env` with generated secrets. Set `APP_VERSION` in `.env`,
then open <http://127.0.0.1:8000> and sign in with `BOOTSTRAP_ADMIN_EMAIL` /
`BOOTSTRAP_ADMIN_PASSWORD` from `.env`. The prebuilt frontend in `app/static` is
served as-is, so Node isn't needed just to run the app.

### Backend by hand

```bash
python -m venv .venv
source .venv/bin/activate            # Windows: .venv\Scripts\Activate.ps1
pip install -r requirements.txt -r requirements-dev.txt
python scripts/gen_local_env_secrets.py   # creates .env with generated secrets
python -m uvicorn app.main:app --reload --host 127.0.0.1 --port 8000
```

On Linux you can install the exact CI pins instead with
`pip install --require-hashes -r requirements-dev-lock.txt`. The lockfile is
compiled for Linux, so it doesn't install on Windows or macOS.

Without `DATABASE_URL`, the app uses SQLite (`bug_hunter.db`) and creates the
schema on first run. API docs are at <http://127.0.0.1:8000/docs> and `/redoc`.

To use PostgreSQL instead, set
`DATABASE_URL=postgresql+psycopg://user:password@localhost:5432/bughunter`.

### Frontend

```bash
cd frontend
npm ci               # lockfile-faithful install
npm test             # Vitest behaviour tests
npm run lint         # ESLint
npm run build        # writes the bundle into ../app/static
npm run dev          # optional Vite dev server on :5173, proxies /api (see vite.config.js)
```

Commit the rebuilt `app/static` together with frontend changes so a plain
`uvicorn` run serves the current UI. The Docker image rebuilds it anyway.

### Tests and checks

```bash
pytest -m "not ui"                           # backend suite (coverage gate: 80%)
python -m playwright install chromium firefox webkit   # one-time browser download
pytest -m ui                                 # browser suites (Chromium)
PW_BROWSERS=chromium,firefox,webkit pytest tests/test_e2e_browser.py -m ui

python -m compileall -q app tests scripts
ruff check app tests scripts
bandit -c pyproject.toml -r app -q
pip-audit -r requirements-lock.txt --strict
cd frontend && npm run lint && npm test && npm audit --omit=dev --audit-level=high
```

The browser suites start their own server on a throwaway database. Besides
the main workflows (login and deep links, every view, board drag-and-drop,
sprint configuration and reports, role gating, logout, phone width) and the
enterprise journeys (sign-up, two-factor sign-in, account settings, branding,
invitations and webhooks, project members and custom fields, saved views, the public
pages) they run
axe-core WCAG 2.1 AA checks in the dark and light themes, which needs
`npm ci` in `frontend/` first. Any uncaught page error, browser console error
or HTTP 5xx fails a test.

Load test a running instance (read-only and bounded; credentials come from
`--email/--password` or `BH_LOAD_EMAIL`/`BH_LOAD_PASSWORD`):

```bash
python scripts/load_test.py --base-url http://localhost:8765 --users 15 --duration 90 --docker-container bugtracker_app
```

SonarQube: `scripts/sonar-server-up.ps1` starts a local server,
`scripts/sonar-scan.ps1` / `sonar-scan.sh` scan, and
`python scripts/sonar_gate.py apply` installs the `bug-hunter-gate` quality gate
(80% coverage overall and on new code, security rating A, zero vulnerabilities).

The GitHub Actions pipeline (`.github/workflows/build-and-push.yml`) runs on
pushes and pull requests to `main`: compile, ruff, bandit, the backend suite
with the 80% coverage gate, ESLint, the frontend tests and build, `npm audit`
and `pip-audit`. All actions are pinned to commit SHAs. Three repository
variables control the rest:

| Variable | Effect |
| --- | --- |
| `SONAR_ENABLED=true` | Run the SonarQube scan and wait for its quality gate (needs `SONAR_TOKEN` and `SONAR_HOST_URL` secrets). |
| `IMAGE_PUSH_ENABLED=true` | Build and push the image on `main` after every check passes (needs the `ACR_*` secrets). |
| `RUN_TESTS=false` | Skip the backend and frontend test jobs and the SonarQube gate for an image-only run. Lint, build and dependency audits still gate the push. |

Browser suites are not run in CI.

## Live-data safety

- `./deploy.sh` rebuilds the image and restarts containers without touching the `bugtracker_pgdata` volume.
- `./down.sh` (no flags) stops containers and keeps the volume.
- Data is lost only through explicit commands: `./down.sh --wipe-db` (asks for confirmation) or `docker compose down -v`.
- `init_db()` only creates missing tables, columns and indexes. Existing rows are never deleted, and the only in-place writes are one-time backfills of new columns (for example display IDs and default workflow statuses).
- Because the schema is additive, an older release still runs against a newer database. Rolling back is still a manual step, so take a backup before upgrading.

**Backup & restore:**

```bash
docker exec -t bugtracker_db pg_dump -U bugtracker bugtracker > backup.sql
cat backup.sql | docker exec -i bugtracker_db psql -U bugtracker bugtracker
```

If you changed `POSTGRES_USER`/`POSTGRES_DB`, substitute those values.

**Rollback:** `./down.sh`, check out the previous tag (or `docker load` a saved
image and set `APP_VERSION` to it), then `./deploy.sh`.

## Upgrading from earlier editions

The schema upgrade is automatic and additive on first boot; take a backup first.

**From the earlier Bug Hunter Enterprise edition.** Organizations, users, projects and items keep
their data and ids. On boot, `project_memberships` are copied into the project roles
(lead / member), `device_tokens` into the push registrations, and the retired `member`
role becomes `user`; the old tables stay in place, unread. Check these before switching over:

- Events without a project are now visible to admins only (assign them a project to share them).
- Settings that were renamed or dropped: `FIREBASE_SA_JSON` is `FCM_CREDENTIALS_JSON` (or a mounted `FCM_CREDENTIALS_FILE`); `JSON_LOGGING=true` is `LOG_FORMAT=json`; `ALLOW_ACCOUNT_ENUMERATION=true` is `FORGOT_PASSWORD_ENUMERATION_SAFE=false`; `CSRF_PROTECTION` is gone (cross-site request protection is always on, based on the request's origin); `FIREBASE_HTTP_TIMEOUT_SECONDS` and `WEBHOOK_MAX_URL_LENGTH` are gone.
- Webhook secrets and 2FA secrets keep working. Set `FIELD_ENCRYPTION_KEY` to encrypt them from then on.
- `AUDIT_RETENTION_DAYS` defaults to 365: older audit rows are deleted on the first daily run. Set `0` first if you keep history for longer.
- Sign-up is on by default; set `ALLOW_PUBLIC_SIGNUP=false` for a closed installation.

**From the single-tenant Bug Hunter.** Every existing user, project, event and audit row joins
one organization (`BOOTSTRAP_ORG_NAME`, created if none exists), project keys are generated
from the project names, and everything else works as before.

## Deployment

To upgrade production: take a database backup, `git pull`, set `APP_VERSION`
in `.env` to the new release, and run `./deploy.sh`. There's no separate
migration step: `init_db()` adds missing tables, columns and indexes on boot and
never drops or rewrites existing rows (see *Live-data safety*), apart from
the one-time Sprints upgrade described under *Features*. `./down.sh`
keeps all data; only `./down.sh --wipe-db` deletes it. To roll back, `./down.sh`,
check out the previous tag and `./deploy.sh` again (an older app still runs
against a newer schema).

| Aspect | How it's handled |
| --- | --- |
| Services | The app container and a PostgreSQL container |
| Ports | App `8765 → 8000`; PostgreSQL only on `127.0.0.1:55432` |
| Resource limits | 0.5 vCPU / 512 MB for the whole stack (app 0.30 / 320 MB, database 0.20 / 192 MB) |
| Workers | One Uvicorn worker and a small SQLAlchemy pool (`DB_POOL_SIZE` + `DB_MAX_OVERFLOW`, 2 + 2 in Compose). Requests beyond what the pool can serve queue on the event loop instead of tying up worker threads, so bursts slow down rather than fail. The digest scheduler and rate limiters are per process, so scale out only after moving them out of the app. |
| Secrets | Injected from `.env` / `secrets/` by Compose, or by the platform; never baked into the image |
| Health | `curl -fsS http://localhost:8765/api/health` |

The app container runs only the app. It doesn't run Docker or Compose and
doesn't mount the Docker socket. Without Compose (for example on Azure Container
Apps), the platform must inject a `DATABASE_URL` that points at an external
PostgreSQL, never `localhost`.

## Sleuth

Sleuth is the in-app assistant, a floating widget on every page (open it with
`Ctrl + /` or `⌘ + /`).

**Ask questions** (answered by exact SQL handlers):

- *show open bugs assigned to alice*
- *how many critical bugs are in PROD?*
- *export all bugs in apollo to excel* (returns a real `.xlsx`)

**Run actions** (always confirmed before any change, always audited):

- *close bug 5* · *reopen #12* · *assign bug 3 to alice* · *set bug 9 priority to high*
- *comment on #5: looks fixed in v2.1*
- *create a bug titled "Login broken" in project Apollo*

With the cloud layer on, Sleuth can also call tools for agile work: create a
work item under a parent, move it through the workflow, and add it to or remove
it from a sprint. Every write still needs your confirmation and uses your own
permissions.

### How it works

Sleuth tries the cheapest layer first:

1. **Rules** (`app/chatbot/nlu.py`) is a regex parser over verbs, filters, names and IDs.
2. **Statistical classifier** (`app/chatbot/classifier.py`) uses TF-IDF and cosine similarity, with no external models.
3. **Local LLM** (`app/chatbot/llm.py`) is an optional `llama.cpp` backend for a GGUF model in `models/`. It stays dormant without a model file.
4. **Cloud LLM** (`app/chatbot/cloud_llm.py`, `llm_tools_agent.py`) is optional and off by default: Groq (primary) or OpenRouter (fallback), with tool calling.

With the defaults (`SLEUTH_CLOUD_ENABLED=0`, no model file), Sleuth is fully
local and makes no outbound HTTP calls. The cloud layer is the only path that
sends text off the box, and all text first passes through a secret-redaction
filter (`app/chatbot/redaction.py`). Optional read-only add-ons for the cloud
layer are `SLEUTH_RETRIEVAL_ENABLED` (keyword grounding),
`SLEUTH_AGENT_ENABLED` (multi-step lookups), `SLEUTH_VERIFY_ANSWERS` (citation
check) and `SLEUTH_EVAL_ENABLED` (LLM-as-judge note). `/api/chat` is limited to
30 messages per minute per user.

## Git integration (Story feature branches)

Bug Hunter supports exactly two optional remote Git operations, both disabled by
default:

1. create one feature branch for one **User Story** in one GitHub repository;
2. remove exactly that tracked branch again.

Pull requests, merges, tags, pipeline sync and bulk deletion are out of scope.
Bug Hunter never runs local `git`; every provider call is REST.

**Project settings → Git integration** holds the API base URL, organization,
default base branch and an encrypted project token (Fernet, via
`GIT_CREDENTIAL_ENCRYPTION_KEY`), and has **Test connection** and read-only
repository discovery (at most 200 repositories). There's no repository
allow-list: the repository is chosen when a branch is created.

**Creating.** `POST /api/git/work-items/{id}/branches` accepts only
`provider_repo_id` and `base_branch`. The name is deterministic:
`feature_<story-number>_<title-slug>` (slug from the first 25 characters of the
title). The server verifies the repository and base branch, reads the base SHA,
creates the branch, reconciles an uncertain success, and audits the outcome.
Repeated requests are idempotent. `GET .../branches/preview` validates the same
way without creating anything. Other item types are refused with
`Feature branches can be created only for User Stories.`

**Removing.** `DELETE /api/git/branches/{branch_record_id}` needs
`GIT_BRANCH_DELETION_ENABLED=true`. Any member of the Story's project may remove
the branch, and anyone outside the project gets a 404. The server deletes only
the exact `refs/heads/<stored-name>` of an `Active` record with a `feature_`
prefix, and it refuses protected names (`main`, `master`, `dev`, `develop`,
`release`) and the project's base branch. A provider 404 counts as already gone.
Auth, TLS, timeout, rate-limit and 5xx failures leave the record `Active` so the
removal can be retried. Removed branches stay visible as history.

**Credentials.** The project token takes precedence over the deployment-wide
`GITHUB_TOKEN`. A token is never accepted from API payloads, returned in a
response, or written to logs or audit rows. Use a fine-grained PAT with
`Metadata: Read` and `Contents: Read and Write`, and set `GIT_CA_BUNDLE_FILE`
behind a TLS-intercepting proxy.

## Limitations

- Webhook deliveries are in-process and best-effort: no queue, no retries, and a delivery in flight is lost if the worker restarts. Use `delivery_id` to deduplicate and treat webhooks as notifications, not a ledger. Sprint planning changes (ranking, moving between sprints) do not emit events; item creation, updates, board moves, deletion and comments do.
- Rate limits, account lockout and metrics are per worker process. Run one worker (the default) or move them out before scaling out.
- Invitation links are only sent by email; there is no copy-link option, and an address that already has an account in any organization can't be invited.
- There is no single sign-on (SAML/OIDC), no organization deletion, and no moving a person between organizations.
- Audit-based reports only cover `AUDIT_RETENTION_DAYS`.
- The data export covers the signed-in person's own records, not an organization-wide export; use a database backup for that.

## Troubleshooting

- **`APP_VERSION is required`:** add `APP_VERSION=<release>` to `.env`.
- **App refuses to start in production:** the log lists every unmet requirement (session secret, bootstrap password, auto-login, Git encryption key, and with `APP_ENV=production` also https URL, secure cookies and email backend).
- **Database connection refused:** check `docker compose ps`; the app waits for the database health check, so rerun `./deploy.sh` once it's healthy.
- **Port 8765 in use:** stop the other service or change the host port in `docker-compose.yml`.
- **Logged out after every restart:** set a fixed `SESSION_SECRET`.
- **Password rejected as too weak:** the default policy is at least `PASSWORD_MIN_LENGTH` (8) characters containing at least one letter and one number (`PASSWORD_REQUIRE_COMPLEXITY`); a few very common passwords are refused outright.
- **Emails not sent:** check `EMAIL_BACKEND=smtp` and the `SMTP_*` values, and set `LOG_LEVEL=DEBUG` for SMTP details.
- **Blank page or 404 on assets:** rebuild the frontend (`cd frontend && npm run build`) and restart the backend so it recalculates the asset version.
- **401s right after login:** `COOKIE_SECURE=true` needs https; use `false` for plain http.
- **Sprints shows "Scrum isn't set up":** Agile is off for that project. A manager or admin clicks **Set up Scrum board** on the Sprints page.
- **Sign-up says it's disabled / the login page has no sign-up link:** `ALLOW_PUBLIC_SIGNUP=false`; ask an admin for an invitation.
- **A record that exists answers 404:** it belongs to another organization or to a project you aren't a member of.
- **"Invitation expired" or "already used":** ask the inviter to send a new one; a new invitation replaces the earlier one.
- **Webhook shows "public host" errors or stays suspended:** the target resolves to a private network (`WEBHOOK_ALLOW_PRIVATE_NETWORKS`), or ten deliveries in a row failed; fix the endpoint and use **Resume**.
- **Logs:** `docker compose logs -f app`.

## Security

See [SECURITY.md](SECURITY.md) for supported versions, private vulnerability
reporting, and the security posture.

## Contributing

See [CONTRIBUTING.md](CONTRIBUTING.md). Run the test suites before opening a pull
request. Report vulnerabilities privately via [SECURITY.md](SECURITY.md), not as
public issues.

## License

[MIT](LICENSE.txt).
