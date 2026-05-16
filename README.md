# 🐞 Bug Hunter v4

A multi-tenant, self-hostable issue tracker. Built with FastAPI + SQLite or
PostgreSQL + a zero-framework JavaScript SPA. One Docker command to run, no
external auth, no external file storage — attachments live in the database
itself.

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
  viewable by admins and managers
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
own organization with themselves as the first admin. No bootstrap admin, no
env-vars-with-passwords — just go to the home page, click *Create an
organization*, and you're in.

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

Schema changes in v3.1 are **purely additive**:

- A new `sessions` table is created on first start (idempotent, only if
  it doesn't already exist). No existing table's columns or indexes change.
- Cookies issued by older builds (which don't carry a `jti`) are still
  accepted and treated as legacy sessions, so a redeploy doesn't kick
  every user out at once.

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
