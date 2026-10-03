# Contributing

Thanks for your interest. Bug Hunter Enterprise runs on a small server with no external
dependencies — keep changes small and self-contained and they will merge faster.

## Setup

The quickest path is the launcher, which creates `.venv` and `.env` on first run:

```bash
.\scripts\run_local.ps1 -Reload            # Windows
RELOAD=1 bash scripts/run_local.sh         # macOS / Linux / Git Bash
```

Or step by step:

```bash
python -m venv .venv
# Windows:  .venv\Scripts\Activate.ps1
# macOS/Linux:  source .venv/bin/activate
pip install -r requirements.txt -r requirements-dev.txt
# On Linux (as in CI) you can use the exact hashed pins instead:
#   pip install --require-hashes -r requirements-dev-lock.txt
python scripts/gen_local_env_secrets.py    # creates .env with generated secrets
python -m uvicorn app.main:app --reload    # http://127.0.0.1:8000
```

`requirements.txt` / `requirements-dev.txt` stay the human-maintained source of
truth. After any dependency change, regenerate both Linux locks — the dev lock
first, then the runtime lock constrained to it, so the Docker image ships exactly
the versions CI tested (`tests/test_release_hygiene.py` fails on any drift):

```bash
uv pip compile --python-version 3.12 --python-platform linux --generate-hashes -o requirements-dev-lock.txt requirements.txt requirements-dev.txt
uv pip compile --python-version 3.12 --python-platform linux --generate-hashes -c requirements-dev-lock.txt -o requirements-lock.txt requirements.txt
```

Or via Docker (the canonical run path):

```bash
./deploy.sh                                 # http://localhost:8765
```

### Frontend

The SPA source lives in `frontend/` (React + JavaScript + Vite — no
TypeScript anywhere in this repo). The build writes the static bundle to
`app/static/`, which FastAPI serves. After any frontend change, rebuild:

```bash
cd frontend
npm ci           # lockfile-faithful install (npm install only when changing the lock)
npm test         # Vitest behavioral tests (project state, branch actions, loader, conversion)
npm run build
```

### Observability

Traces, metrics and logs stay console-only unless `OTEL_EXPORTER_OTLP_ENDPOINT` is
set in `.env` (OTLP/gRPC; full flag list in the *Observability (SigNoz)* section of
[README.md](README.md)). Tests force the endpoint off in `tests/conftest.py`, so the
suite never talks to a collector — `tests/test_telemetry.py` covers export behaviour
with fakes plus an in-process OTLP/gRPC receiver.

## Tests

All tests must pass on every pull request. Coverage is enforced project-wide at
**80% and above** (`fail_under = 80` in `pyproject.toml`, same floor in the SonarQube gate
created by `scripts/sonar_gate.py`).
`addopts` does not include `--cov`, so pass it manually:

```bash
python -m pytest -m "not ui" --cov=app      # backend suite + coverage gate
```

### Resetting the local database

Local development uses SQLite (`bug_hunter.db` in the repo root). To reset
it back to the first-launch state, delete the database files (including the
write-ahead-log sidecars) and restart the app — the schema and default data
are recreated on boot:

```bash
# Windows PowerShell — stop the server first, then:
Remove-Item -Force bug_hunter.db, bug_hunter.db-wal, bug_hunter.db-shm
python -m uvicorn app.main:app --reload
```

```bash
# Docker Compose (Postgres in the `bugtracker_pgdata` volume):
./down.sh --wipe-db     # drops the volume (ALL DATA LOST), app re-seeds on next boot
./deploy.sh
```

For a safer reset that keeps the bootstrap admin, the default project and
workflow baseline, prefer `./down.sh --clean-db` (dry-run first, asks for
confirmation) for the Docker stack, or `python scripts/clean_db.py` (dry run)
followed by `python scripts/clean_db.py --yes` against `DATABASE_URL`.

Frontend behavior is covered by Vitest (`npm test` in `frontend/`); the
Playwright UI suite covers the browser flows:

```bash
cd frontend && npm ci && npm test && npm run build && cd ..
python -m playwright install chromium firefox webkit
python -m pytest -m ui                                   # Chromium
PW_BROWSERS=chromium,firefox,webkit python -m pytest tests/test_e2e_browser.py tests/test_e2e_enterprise.py -m ui
```

`tests/test_e2e_browser.py` (and `tests/test_e2e_enterprise.py`, which covers sign-up, two-factor
sign-in, account settings, the Organization screens and the public pages) boot their own server,
seed data through the API
and fails on any page error, browser console error or HTTP 5xx, not only on
its explicit assertions. Its accessibility tests inject axe-core from
`frontend/node_modules`, so run `npm ci` first. New UI work should come with a
workflow test there, and new views should be added to its axe checks.

`tests/fixtures/legacy_enterprise.sql` is a database written by the earlier enterprise edition's
own code; `tests/test_upgrade_paths.py` boots the current app on it, so keep it unchanged.

### Lint & security

Run these before opening a PR — the CI pipeline runs the same checks:

```bash
python -m compileall -q app tests scripts    # syntax gate
ruff check app tests scripts                 # Python lint
bandit -c pyproject.toml -r app -q           # Python security scan
pip-audit -r requirements-lock.txt --strict  # dependency audit (blocking in CI)
cd frontend && npm run lint && npm test && npm audit --omit=dev --audit-level=high && cd ..
```

## Code style

- Match the surrounding code.
- Keep comments concise: one short line stating a non-obvious *why* — a hidden
  constraint, a subtle rule, or a workaround for a specific bug. No essay
  blocks; docstrings are one line (two for genuinely complex APIs).
- New routes need tests; new schemas need validators. Every operation must be listed in
  `tests/test_openapi_ownership.py` with the test file that covers it.
- Tenant data is always scoped: take project ids from `access.accessible_project_ids`, load
  projects and users with `get_org_project_or_404` / `get_org_user`, and answer another
  organization's id with `404`. `tests/test_cross_tenant.py` probes every operation that takes an
  id with a second organization's session, so a new endpoint is checked automatically; add a case
  there when it takes a body that references other records.
- Database changes must only *add* — see *Live-data safety* in
  [README.md](README.md). No destructive migrations.
- The live FastAPI app is the source of truth for the API: browse `/docs` on a
  running instance.

## Version bumps

`APP_VERSION` in `.env` is the **single source of truth** for the product
version. There is deliberately no hardcoded copy anywhere else — not in
`app/config.py`, `docker-compose.yml`, `frontend/package.json`,
`sonar-project.properties`, CI, or the release scripts. A second literal is
exactly how a release ends up labelled with the previous version.

To cut a release:

1. Set `APP_VERSION` in `.env` (the only edit a release should need).
2. Mirror it in the `APP_VERSION` repository variable
   (Settings → Secrets and variables → Actions → Variables). The backend test
   job reads it from there (and falls back to a sentinel when it is missing).
3. Leave `APP_VERSION` blank in `.env.example`; it is a template, not a value.

Everything else follows automatically: `/api/health` and the UI version string,
the Docker image tag (`docker-compose.yml` uses `${APP_VERSION:?...}`, so a
missing value fails loudly instead of tagging a stale image), the local
SonarQube scan's project version, and the default release-archive filename.

`tests/test_release_hygiene.py` enforces this: it reads the live `.env` value
and fails if that value appears as a literal in any other file.

## Pull requests

- One concern per pull request.
- Write a short description of the change and list any DB or config implications.
- Tests must pass and coverage must stay at or above the 80% gate.
- Reference the related issue if one exists.

## Security

Do not open a public issue for vulnerabilities. See [SECURITY.md](SECURITY.md)
for the private disclosure path.

## License

By submitting a contribution you agree it will be licensed under the project's
[MIT license](LICENSE.txt).
