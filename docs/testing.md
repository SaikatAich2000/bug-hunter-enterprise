# Testing

Python 3.12 and Node 22 (`.nvmrc`). Install the dev dependencies first:
`pip install -r requirements.txt -r requirements-dev.txt` (on Linux, CI uses
`pip install --require-hashes -r requirements-dev-lock.txt`), then `cd frontend && npm ci`.

## Layers

| Layer | Where | Command |
|---|---|---|
| Backend (API, rules, jobs) | `tests/`, SQLite by default | `pytest -m "not ui" -n auto` |
| Backend on PostgreSQL 16 | same files | `BH_TEST_DB=postgres BH_TEST_POSTGRES_URL=postgresql+psycopg://user:pw@host:5432/postgres pytest -m "not ui" -n auto` |
| Browser end to end (`ui` marker) | `tests/test_e2e_browser.py`, `tests/test_playwright_*.py`, `tests/test_ui_smoke.py` | `python -m playwright install chromium`, then `pytest -m ui` |
| Frontend units | `frontend/src/**/*.test.jsx` (Vitest) | `cd frontend && npm test` (`npm run test:coverage` for coverage) |
| Static checks | whole repo | see below |

Coverage: `pytest -m "not ui" -n auto --cov=app` fails under 80% (`fail_under` in
`pyproject.toml`). `--cov` is not in `addopts`; pass it.

## Backend harness (`tests/conftest.py`)

- The environment is pinned before `app.*` is imported (SQLite file in a temp directory,
  console and cloud features off, bcrypt cost 4 via `BCRYPT_TEST_ROUNDS`, no OTLP endpoint,
  no breach-check calls). Your `.env` cannot change test behaviour.
- The app is imported once per process. `_fresh_db` truncates every table before each test
  and `client` / `db_session` re-run normal startup, so each test sees a fresh install.
- `_isolate` restores module state (rate limiters, caches, `Settings`, `sys.modules`) after
  each test, so a test may reload `app.*` without leaking into the next.
- With `-n auto` (pytest-xdist) each worker loads the file and gets its own database.
  Under PostgreSQL each process creates and drops a throwaway database.
- Test client: Starlette `TestClient` on `httpx2`.
- Set `CI_TEST_FORENSICS=1` (and `CI_TEST_FORENSICS_LOG=path`) to stream heartbeats, slow-test
  lines and thread dumps; CI does this so a killed runner still leaves evidence.

## Browser suites

They start their own uvicorn on a throwaway database, seed through the API, and fail on
any uncaught page error, browser console error or HTTP 5xx. They also run axe-core WCAG 2.1 AA
checks in both themes (needs `npm ci` in `frontend/`, since axe is read from `node_modules`).

| Variable | Effect |
|---|---|
| `PW_BROWSERS` | comma list, default `chromium`; e.g. `chromium,firefox,webkit` (install them first) |
| `PW_EXECUTABLE_PATH` | use an installed browser binary instead of Playwright's download |
| `PW_HEADED=1` | show the browser window |
| `BH_TEST_POSTGRES_URL` | admin URL; each server then gets a temporary PostgreSQL database |

The onboarding flows (welcome, resumable tour, help center) are in
`tests/test_playwright_onboarding.py`. Run the frontend build first
(`cd frontend && npm run build`) if you changed UI code, since the app serves `app/static`.

## Static checks

```bash
python -m compileall -q app tests scripts
ruff check app tests scripts
bandit -c pyproject.toml -r app -q
pip-audit -r requirements-lock.txt --strict
cd frontend && npm run lint && npm test && npm audit --omit=dev --audit-level=high
```

## CI

Workflows in `.github/workflows/`; every action is pinned to a commit SHA, and
Dependabot opens weekly update PRs for pip, npm, Actions and Docker.

| Workflow / job | What it runs |
|---|---|
| `build-and-push.yml` `policy` | reads the `RUN_TESTS` repository variable; `false`/`0`/`no`/`off` skips test jobs |
| `lint` | compileall, ruff, ESLint |
| `test` | backend suite on SQLite, `-n auto`, coverage XML and JUnit uploaded |
| `test-postgres` | backend suite on a PostgreSQL 16 service (`BH_TEST_DB=postgres`) |
| `playwright` | `pytest -m ui` on Chromium |
| `frontend-build` | `npm ci`, `npm test`, `npm run test:coverage` |
| `dependency-audit` | `pip-audit -r requirements-lock.txt --strict` |
| `sonar-scan`, `build-and-push` | optional SonarQube gate (`SONAR_ENABLED`) and image push (`IMAGE_PUSH_ENABLED`) |
| `security.yml` | conflict markers, gitleaks, Trivy filesystem scan, bandit, hadolint, ShellCheck |
| `docker.yml` (pull requests) | image build and Trivy image scan |

Every job except Sonar and the push runs with plain commands and no secrets, so you can reproduce it locally.

## Adding tests

- New route: a test in the matching `tests/test_*.py`; new schema: a validator and a test.
- New UI view or flow: a Playwright test, and add the view to its axe checks.
- Never relax a constraint or skip a test to get green; fix the cause.
