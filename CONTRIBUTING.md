# Contributing

This is the multi-tenant Bug Hunter build. PRs that keep the shape
intact (one Docker stack, no external auth provider, ~512 MB RAM
budget) land easiest.

## Setup

```bash
python -m venv .venv
# Windows:  .venv\Scripts\Activate.ps1
# macOS:    source .venv/bin/activate
pip install -r requirements.txt -r requirements-dev.txt
cp .env.example .env       # edit values you care about
```

Run the app locally:

```bash
python -m uvicorn app.main:app --reload
# browse http://127.0.0.1:8000
```

Or via Docker:

```bash
docker compose up -d
# browse http://localhost:8765
```

## Tests

The full suite must stay green for every PR.

```bash
python -m pytest -q
```

Multi-tenant isolation is enforced by `tests/test_multitenant.py`; new
endpoints should add an entry there if they touch user-owned rows.

## Code style

- Match the surrounding code.
- Default to **no comments** unless the *why* is non-obvious (a hidden
  constraint, a subtle invariant, a workaround for a specific bug).
- Every query that reads or writes user data goes through the org-scope
  helpers in `app/auth.py` (`accessible_project_ids`, etc.) — never
  query `Bug`/`User`/`Project` without an org filter.
- Database changes must be **strictly additive** — see *Live-data
  safety* in [README.md](README.md). No destructive migrations.
- If you add or change an API route, regenerate the docs:
  ```bash
  python scripts/gen-api-docs.py
  ```
  (the artifacts under `docs/api/` are gitignored — the live FastAPI
  app is the source of truth.)

## Pull requests

- One concern per PR.
- Describe the change in one paragraph; list any DB, config, or
  cross-tenant implications.
- CI must pass; SonarQube quality gate must stay green.
- Reference the related issue if there is one.

## Security

Please **don't open a public issue for vulnerabilities** — see
[SECURITY.md](SECURITY.md) for the disclosure path.

## License

By submitting a contribution you agree it will be licensed under the
project's [LICENSE](LICENSE.txt).
