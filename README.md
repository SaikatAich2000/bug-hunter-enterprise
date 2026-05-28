# 🐞 Bug Hunter v4

A multi-tenant, self-hostable issue tracker. Built with FastAPI + SQLite or
PostgreSQL + a zero-framework JavaScript SPA. One Docker command to run, no
external auth, no external file storage — attachments live in the database
itself.

Current version: **v2.4**.

## What's new in v2.4

- **First-run bootstrap admin** — set `BOOTSTRAP_ADMIN_EMAIL` and
  `BOOTSTRAP_ADMIN_PASSWORD` in `.env` and the app auto-creates one
  organization + one admin user on first boot. Lets a fresh Docker /
  Render deployment be logged-into immediately without going through
  the multi-tenant `/signup` flow or running raw SQL. The bootstrap is
  **strictly idempotent by default** — once that user exists, the env
  vars are ignored.
- **Locked-out recovery** — for the common deployment where the prod
  DB already has a user with the bootstrap email but the password is
  unknown (stale from a prior boot, manual signup, etc.), set
  `BOOTSTRAP_ADMIN_RESET_PASSWORD=true` and redeploy. On the next
  boot the app *resets* the user's password to the env-var value,
  re-promotes them to admin, re-activates them if disabled, and
  invalidates outstanding sessions. After logging in, change the
  password from the Account panel and flip the flag back off. See
  "[Locked out of the bootstrap admin?](#locked-out-of-the-bootstrap-admin)"
  below for the full procedure.
- **Audit history survives bug deletion**. Pre-v2.4, deleting a bug
  cascade-deleted every activity row it owned, so the trail kept only a
  single `bug_deleted` summary line. Now the delete handler detaches
  activity rows (`bug_id → NULL`) before issuing the DELETE, so the
  full original story — create / update / comment / assignment /
  delete — survives. Works on existing production databases without a
  DDL change (the application-level detach runs before the legacy
  `ON DELETE CASCADE` constraint fires; the constraint is also
  upgraded to `SET NULL` on fresh installs).
- **Audit search hits live bug titles** via a `LEFT JOIN` on bugs,
  so renaming a bug after the fact doesn't hide its history from a
  title search. The free-text search now ORs against the action,
  detail, actor name, entity type, the *live* bug title from the
  join, plus numeric `#id` matches against entity_id, bug_id, and a
  substring cast of entity_id (for partial-number searches).
- **Form-field visual refresh**. Every input, select, textarea, the
  top search bar, the audit filter strip and the multi-select filter
  buttons got a contrast pass — visible borders, hover lift to the
  accent colour, focus ring, and a *truly* disabled state (opacity +
  dashed border + not-allowed cursor) so the difference between "you
  can type here" and "you can't" finally reads at a glance.
- **Top search placeholder updated** to make it obvious that the box
  searches title, description and `#id` together.

Migrations remain **strictly additive** — existing production
databases are never altered or destroyed on deploy. See
"[Live-data safety](#live-data-safety)" below.

## Features

- **Multi-tenant from the ground up** — anyone can sign up and create their
  own organization. Strict per-org data isolation enforced at every route.
- **Email-based invitations** — admins/managers invite teammates via email
  links; recipients set their own password on accept. 7-day expiration.
- **Project memberships** with per-project leads — admins see everything,
  managers and members only see projects they're added to.
- **Jira-style project keys** — bugs display as `WEB-42`, `API-7` etc.
- **Login + role-based access** — admin, manager, member; bcrypt password hashing
- **Per-session tracking & admin revocation** (Keycloak-style) — admins can
  see every active session in their org and log a specific device out
  without affecting any other session for the same user
- **Bug tracking** with status, priority, environment (DEV / UAT / PROD)
- **Multi-assignee** support — many users per bug
- **Single-screen Jira-style bug detail** — title, description, metadata,
  comments and attachments are all on one wide screen; no separate edit modal,
  no pencil button to chase
- **Comments and attachments** (PDF, image, video) stored as BLOBs in the DB
- **Email notifications** on bug create / update / assignment / new comment
  (Gmail / Outlook / SMTP)
- **Forgot-password** flow via email reset link
- **Per-org audit trail** — every create / update / delete / login logged,
  viewable by admins and managers. **Audit history survives bug deletion**:
  deleting a bug doesn't shrink the trail. The delete handler detaches
  activity rows before issuing the DELETE, so the original create /
  update / comment events stay searchable alongside the new
  `bug_deleted` row.
- **Powerful audit search** — paste anything into the search box: bug
  number (`#42` / `42` / `bug 42`), assignee name, current or historical
  title, action keyword. The query OR's against action / detail / actor
  name / entity type / live bug title (via LEFT JOIN). Org-scoped — never
  leaks across tenants.
- **Strict security headers** (CSP, HSTS, X-Frame-Options) on every response
- **Light / dark themes**, fully responsive (mobile, tablet, desktop)
- **CSV export** of all bugs
- **Sleuth — built-in AI assistant** 🔍 that answers natural-language questions
  about your bugs and *executes* tasks on demand (assign, close, comment,
  create). 100 % self-hosted: rules + a small statistical classifier handle
  most queries; an *optional* local LLM (llama.cpp, no external API key, no
  GPU required) catches the rest. See "[Sleuth](#sleuth--ai-assistant)" below.

## Quick start

### Prerequisites

- [Docker Desktop](https://www.docker.com/products/docker-desktop/) installed and running

### Run it

```bash
git clone https://github.com/YOUR_USERNAME/bug-hunter.git
cd bug-hunter
cp .env.example .env       # edit if you want email enabled — see below
./deploy.sh
```

Open **http://localhost:8765** in your browser.

That's it. Postgres runs in its own isolated Docker container on port `55432`
(intentionally non-standard so it won't collide with anything you have on
`5432`). The app listens on port `8765`. The named volume `bugtracker_pgdata`
holds your live data and is **never** removed by `./deploy.sh` or by a plain
`./down.sh` — see "Live-data safety" below.

### First login

Bug Hunter v4 is **multi-tenant**: anyone can visit `/signup` and create their
own organization with themselves as the first admin.

For a single-org deployment (Render, internal Docker host) where signup
hassle is overkill, set these in `.env` before the first boot:

```env
BOOTSTRAP_ADMIN_EMAIL=you@yourcompany.com
BOOTSTRAP_ADMIN_PASSWORD=<a strong password>
BOOTSTRAP_ADMIN_NAME=Admin
BOOTSTRAP_ORG_NAME=Your Company
```

On the first boot the app will create one organization and one admin
user with that email/password. Go straight to `/login.html`, sign in,
and **change the password from the Account panel** — leaving the
bootstrap password in your env is a credential-in-disk risk. The
bootstrap is **strictly idempotent**: once the user exists, the env
vars are ignored. Re-running with a different password will not
modify the live user.

Leave `BOOTSTRAP_ADMIN_EMAIL` empty to disable the bootstrap entirely
and rely on `/signup` + invitations.

#### Locked out of the bootstrap admin?

If a previous deployment created a user with `BOOTSTRAP_ADMIN_EMAIL`
but a different password, the bootstrap leaves that user untouched on
later boots — so re-deploying with a new `BOOTSTRAP_ADMIN_PASSWORD`
won't help on its own. To recover:

1. Set `BOOTSTRAP_ADMIN_RESET_PASSWORD=true` alongside the same
   `BOOTSTRAP_ADMIN_EMAIL` and your *new* `BOOTSTRAP_ADMIN_PASSWORD`.
2. Redeploy. On boot the app resets the existing user's password to
   the new env value, re-promotes them to admin, re-activates them if
   disabled, and invalidates existing sessions.
3. Log in with the new password.
4. **Change the password from the Account panel.**
5. Set `BOOTSTRAP_ADMIN_RESET_PASSWORD=false` (or remove it) and
   redeploy once more. Otherwise every redeploy stomps the password
   back to whatever's in the env var.

Watch the boot logs to confirm — a successful reset emits a `WARNING`
line like:

```
Bootstrap: RESET password for existing admin you@example.com
(BOOTSTRAP_ADMIN_RESET_PASSWORD=true). Log in with the env-var
password, change it, then unset the reset flag.
```

If you instead see:

```
Bootstrap: user you@example.com already exists; leaving untouched.
Set BOOTSTRAP_ADMIN_RESET_PASSWORD=true and redeploy if you need to
reset the password.
```

…then the reset flag isn't picked up — double-check the env var.

```
http://localhost:8765/signup
```

If you want to lock that down (for a private install), set
`ALLOW_PUBLIC_SIGNUP=false` in your `.env` and the signup page returns a 403.
You can still invite the first admin out-of-band by inserting a row in the
`users` table, or temporarily flip the flag.

After signing up:
1. You're logged in as the first admin of your new organization.
2. Open the **Invitations** panel (sidebar) and send invites to teammates.
3. Each teammate gets an email with a link to set their password and join.

Roles within an organization:

| Role     | Bugs                          | Projects                                | Users / Invites                          | Audit | Sessions      |
|----------|-------------------------------|-----------------------------------------|------------------------------------------|-------|---------------|
| admin    | Create, edit, **delete any**  | Create, edit any, **delete**            | Create, edit, delete, invite as anything | ✓     | list + revoke |
| manager  | Create, edit, delete on projects they lead | Create; edit projects they lead | Invite (member/manager); no admin invites| ✓     | —             |
| member   | Create, edit, no delete       | Visible only if they're a member        | —                                        | —     | —             |

**Project membership** is independent of org role:

- **Admins** implicitly see every project in their org.
- **Managers and members** see only projects they're added to.
- A **project lead** (per-project role) can manage that project's members and
  delete its bugs. Org admins are always treated as leads.

**Tenant isolation** is enforced at the route layer: every read and write
scopes to `actor.org_id`. Cross-org access returns 404 (not 403) — we don't
leak the existence of other orgs' data.

### Production checklist

Before exposing this to a real network, set these in `.env`:

```bash
SESSION_SECRET=$(openssl rand -hex 32)   # generate a long random secret
COOKIE_SECURE=true                        # only if serving over HTTPS
ALLOW_PUBLIC_SIGNUP=true                  # or false for a closed install
APP_BASE_URL=https://bugs.yourcompany.com
CORS_ORIGINS=https://bugs.yourcompany.com
BCRYPT_ROUNDS=10                          # 10 is fine on 0.1-vCPU; raise if you have headroom
```

Then `./down.sh && ./deploy.sh` to apply.

## Configuring email (optional)

By default `EMAIL_BACKEND=console`, which just logs emails to the app log
instead of sending them — perfect for trying things out.

To send real notifications via Gmail:

1. Enable **2-Step Verification** on your Google account.
2. Generate an [App Password](https://myaccount.google.com/apppasswords) (16 characters).
3. Edit your `.env`:
   ```env
   EMAIL_BACKEND=smtp
   EMAIL_FROM=Bug Hunter <you@gmail.com>
   SMTP_HOST=smtp.gmail.com
   SMTP_PORT=587
   SMTP_USERNAME=you@gmail.com
   SMTP_PASSWORD=xxxx xxxx xxxx xxxx
   SMTP_USE_TLS=true
   ```
4. Restart: `./down.sh && ./deploy.sh`

Other providers (Office 365, Mailtrap, SendGrid, etc.) work the same way —
just point at their SMTP host and credentials.

## Live-data safety

`./deploy.sh` rebuilds the application image and restarts the stack. It does
**not** touch the `bugtracker_pgdata` volume that holds your Postgres data.
`./down.sh` (no flags) stops the containers and leaves the volume intact.
The only ways to lose data are:

- `./down.sh --wipe-db` — explicitly asks you to type `YES` first
- `docker compose down -v` — manual destructive call
- Manually deleting the named volume

Schema changes are **purely additive** across every release. `init_db()`
does three passes on every boot — all idempotent, none destructive:

  1. `create_all()` — adds new tables.
  2. Index reconciliation — `CREATE INDEX IF NOT EXISTS` for any index
     the model declares but the DB lacks.
  3. Column reconciliation — `ALTER TABLE ... ADD COLUMN` for any
     column the model declares but the DB lacks (with a NULL-tolerant
     definition so existing rows backfill cleanly).

Notable additions over time:

- `sessions` (v3.1) — created on first start if missing.
- Branding columns (v2.2) — `organizations.logo_data_url`,
  `accent_color`, `email_from_override`.
- TOTP columns (v2.2) — `users.totp_secret`, `totp_enabled`,
  `totp_enrolled_at`.
- `activity_log.bug_id` (v2.4) — for fresh installs the FK changes
  from `ON DELETE CASCADE` to `ON DELETE SET NULL` so audit history
  outlives the bug it describes. **Existing production databases are
  not touched** — the legacy `CASCADE` constraint stays in place, and
  the route handler detaches activity rows (`UPDATE activity_log SET
  bug_id = NULL`) before issuing the bug delete, so the same retention
  behaviour applies on legacy schemas without a DDL change.
- Cookies issued by older builds (which don't carry a `jti`) are still
  accepted and treated as legacy sessions, so a redeploy doesn't kick
  every user out at once.

### Roadmap items deferred from v2.4

The OSS / internal edition of Bug Hunter ships three features the
enterprise edition does NOT yet have, because adding them touches the
multi-tenant scoping logic, custom fields and webhook contracts and
deserves its own release:

- **Item types** (Bug / Requirement / Task) sharing one numbering
  system. Requires a new `bugs.item_type` column, type-aware emails,
  per-type permissions and per-type analytics.
- **Events** — containers for groups of work items with per-event
  manager email notifications. Requires `events` + `event_managers`
  tables and a new `/api/events` router scoped to the org.
- **Type tabs in the SPA** (All / Bug / Requirement / Task) with
  per-tab KPIs, per-tab filters, per-tab table columns and tab-aware
  analytics. Depends on the two items above.

Sleuth (the AI assistant) **adds no new tables and modifies no existing
columns**. It uses the same `bugs`, `comments`, `users`, `projects` and
`activity_log` tables the REST API uses. Read intents only `SELECT`;
write intents go through the same paths the REST API uses, including
permission checks and audit logging.

## Sleuth — AI assistant

Sleuth (🔍) is the in-app assistant. It lives as a floating widget in
the bottom-right of every page and lets users ask questions in plain
English ("show me critical bugs in PROD") *and* run actions ("assign
bug 5 to alice", "close #12", "comment on #7: works fine"). Every
write goes through an explicit Yes/Cancel confirmation prompt, and
every change is recorded in the same audit log the REST API uses.

### Examples

**Ask things:**
- *show open bugs assigned to alice*
- *how many critical bugs are in PROD?*
- *list managers* / *list projects*
- *bug 42* &middot; *summary* &middot; *recent activity*
- *export all bugs in apollo to excel* (downloads a real `.xlsx`)
- *bugs created in the last 7 days*

**Do things** (Sleuth always asks before changing anything):
- *close bug 5* &middot; *reopen #12* &middot; *mark #7 as resolved*
- *assign bug 3 to alice* &middot; *unassign bob from #5*
- *set bug 9 priority to high* &middot; *make #3 critical*
- *comment on #5: looks fixed in v2.1*
- *due bug 8 2026-06-15*
- *create a bug titled "Login broken" in project Apollo*
- *create project Mercury* (admin / manager only)

**Pronouns:**
After viewing or filtering a bug, Sleuth remembers it for 30 minutes.
*close it*, *comment on that bug: ...* and *assign it to alice* all
work after a previous turn established the context.

### Architecture

Sleuth runs in three layers, ordered by cost:

1. **Rules** (`app/chatbot/nlu.py`) — regex-driven classification of
   verbs, filters, names, and IDs. Microseconds. Handles ~80 % of
   typical queries on its own.
2. **Statistical classifier** (`app/chatbot/classifier.py`) — pure
   Python TF-IDF + cosine similarity over a hand-curated corpus.
   No external models, no GPU. ~1 ms. Catches paraphrases the rules
   miss (~10–15 % of queries).
3. **Local LLM** (`app/chatbot/llm.py`) — *optional*, lazy-loaded
   `llama.cpp` (`llama-cpp-python`) backed by a GGUF model file you
   drop into `models/`. Used only when layers 1 and 2 are uncertain.
   No external API calls, no API keys — the inference runs entirely
   on this server.

If you don't enable the LLM, Sleuth still works through layers 1 and 2.
The LLM is purely a fallback for unusual phrasing.

### Privacy

**No data leaves the server.** Sleuth makes no outbound HTTP calls,
sends no telemetry, and doesn't depend on any third-party API. Layers
1 and 2 run inside the Python process. Layer 3 (if enabled) runs
inference locally via llama.cpp.

### Enabling the optional LLM

This is **optional** and only useful for unusual phrasings that the
rules + classifier didn't match. On a 1-CPU 2 GB box, this layer is the
slowest path (5–15 s per query). For most teams, the answer is "leave
it disabled". To turn it on:

```bash
# 1. Install the inference library (CPU build, no CUDA):
pip install llama-cpp-python

# 2. Drop a small GGUF model in place:
cd models
wget https://huggingface.co/Qwen/Qwen2.5-0.5B-Instruct-GGUF/resolve/main/qwen2.5-0.5b-instruct-q4_k_m.gguf -O sleuth.gguf

# 3. Restart the app.
```

`models/README.md` has a full sizing table and alternative model
recommendations. **Do not commit the GGUF file to git** — it's
large (300+ MB) and `.gitignore` already excludes it.

### RAM safety: Sleuth refuses to run an LLM that won't fit

Before loading any model, Sleuth measures the actual memory ceiling of
the running container (cgroup v2 / v1) and the GGUF file size. If the
projected peak — model weights + KV cache + overhead — exceeds what's
available, Sleuth **disables Layer 3 entirely** and logs a single
operator-facing warning with the exact numbers and the recommended
docker-compose memory value. End users never see technical details:
the chat just falls back to the same friendly "I didn't understand —
try `help`" reply they'd get if no model file were installed at all.
Layers 1 and 2 keep running. There is no out-of-memory crash, no
silent degradation. See `app/chatbot/llm.py::memory_budget()` and
`tests/test_sleuth_classifier.py` for the verified behaviour.

The default `docker-compose.yml` caps the app at **512 MB** — perfect
for layers 1+2, too small for Layer 3 with any current GGUF model. To
enable Layer 3, raise `services.app.deploy.resources.limits.memory` to
at least `1500M` (for a 0.5 B Q4 model) and rerun `./deploy.sh`.

### Configuration

Sleuth honours these environment variables (all optional, see
`.env.example`):

| Variable | Default | Purpose |
|---|---|---|
| `SLEUTH_LLM_MODEL_PATH` | `models/sleuth.gguf` | absolute path to GGUF |
| `SLEUTH_LLM_TIMEOUT_S` | `12` | inference budget |
| `SLEUTH_LLM_IDLE_UNLOAD_S` | `600` | unload model after idle |
| `SLEUTH_LLM_MAX_TOKENS` | `120` | max generated tokens |
| `SLEUTH_LLM_CTX_LEN` | `1024` | context window |
| `SLEUTH_LLM_THREADS` | `1` | CPU threads for inference |

Rate limit: 30 chat messages per minute per user (built into the
`/api/chat` router; not configurable).

### Keyboard shortcut

Press `Ctrl + /` (or `⌘ + /` on macOS) on any page to open the Sleuth
panel. `Esc` closes it.

## Stopping

```bash
./down.sh                  # stop containers, KEEP database volume + image
./down.sh --wipe-db        # also wipe the database (asks for YES)
./down.sh --remove-images  # also remove the built image
./down.sh --full-clean     # both
```

## Tech stack

- **Backend:** FastAPI 0.115, SQLAlchemy 2.0, Pydantic 2, psycopg 3
- **Database:** PostgreSQL 16
- **Frontend:** Vanilla JavaScript (no framework), CSS variables for theming
- **Container:** Python 3.12 slim image, multi-service Docker Compose
- **Sleuth assistant:** in-process rules + TF-IDF classifier (pure Python);
  optional `llama-cpp-python` for the local LLM layer

## Project structure

```
.
├── app/
│   ├── config.py          # env-driven settings
│   ├── database.py        # SQLAlchemy setup
│   ├── email_service.py   # SMTP / console email backends
│   ├── main.py            # FastAPI entry point
│   ├── models.py          # User, Project, Bug, Comment, Attachment,
│   │                      # Activity, PasswordResetToken, Session
│   ├── routes/            # auth, users, projects, bugs, stats, audit, sessions
│   ├── schemas.py         # Pydantic DTOs
│   ├── chatbot/           # Sleuth — the in-app AI assistant
│   │   ├── nlu.py         #   Layer 1: rule-based parser
│   │   ├── classifier.py  #   Layer 2: TF-IDF intent classifier
│   │   ├── llm.py         #   Layer 3: optional local LLM (llama.cpp)
│   │   ├── executor.py    #   read intents → DB queries → blocks
│   │   ├── actions.py     #   write intents → ActionPlan → audited mutation
│   │   ├── memory.py      #   per-user conversation context (TTL'd)
│   │   ├── excel.py       #   in-memory xlsx export (openpyxl)
│   │   └── router.py      #   FastAPI endpoints under /api/chat
│   └── static/            # index.html + login.html + reset.html
│                          # + app.js + styles.css + chatbot.{js,css} + favicons
├── tests/                 # Sleuth tests — 300 checks, hermetic SQLite
│   ├── test_sleuth_parser.py
│   ├── test_sleuth_actions.py
│   ├── test_sleuth_classifier.py
│   ├── test_sleuth_safety.py
│   ├── test_sleuth_comprehensive.py
│   └── run_all.py         # one-command runner
├── models/                # GGUF model files for Sleuth (gitignored)
│   └── README.md          # how to download an LLM if you want one
├── docker-compose.yml
├── Dockerfile
├── deploy.sh              # build + start (idempotent, safe on re-run)
├── down.sh                # stop (data-safe by default)
├── requirements.txt
└── .env.example           # copy to .env and edit
```

## Running tests

The Sleuth test suite is hermetic — every test file spins up its own
temp SQLite database and never touches your production data:

```bash
pip install -r requirements.txt
python3 tests/run_all.py        # 300 checks, ~10 s
```

You can also run an individual file:

```bash
python3 tests/test_sleuth_actions.py
python3 tests/test_sleuth_safety.py     # database-safety guarantees
```

## Contributing

Issues and pull requests welcome. Please run the tests before submitting.

## License

Released under the [MIT License](LICENSE.txt). See the LICENSE file for details.
