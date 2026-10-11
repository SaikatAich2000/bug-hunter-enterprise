# Operations

Everything here is for the Docker Compose deployment (`docker-compose.yml`, `deploy.sh`,
`down.sh`). Settings are environment variables; every one is listed in
[`.env.example`](../.env.example) and read in `app/config.py`.

## Deploy and upgrade

```bash
cp .env.example .env     # or: python scripts/gen_local_env_secrets.py (fills the secrets)
./deploy.sh              # http://localhost:8765
```

`deploy.sh` refuses to run while `POSTGRES_PASSWORD`, `SESSION_SECRET` or
`BOOTSTRAP_ADMIN_PASSWORD` is empty or a placeholder. If the database container is
already running it takes a backup first (`SKIP_BACKUP=1` skips that), then builds, starts
PostgreSQL, waits for it to be healthy, then starts the app. The `bugtracker_pgdata`
volume is never touched.

To upgrade: set the new `APP_VERSION` in `.env`, run `./deploy.sh`. There is no migration
step: `init_db()` adds missing tables, columns, indexes and `CHECK` constraints on boot and
never drops data. On PostgreSQL an advisory lock serializes concurrent boots. New CHECK
constraints are added `NOT VALID`, so existing rows are not rescanned or rejected.

## Production start-up checks

With `APP_ENV=production` (or `prod`/`staging`) the app logs every unmet requirement and
refuses to start:

- `SESSION_SECRET` at least 32 characters and not a placeholder;
- a bootstrap admin password that is not the placeholder and meets the password policy;
- `AUTO_LOGIN_ENABLED` off;
- a well-formed `GIT_CREDENTIAL_ENCRYPTION_KEY` if one is set;
- `COOKIE_SECURE=true`, an `https://` `APP_BASE_URL`, and `EMAIL_BACKEND` other than `console`.

`COOKIE_SECURE=true` alone also counts as production for the first four checks.

## Backup and restore

```bash
./scripts/backup.sh                          # backups/bughunter-<UTC>.dump
BACKUP_KEEP=30 BACKUP_DIR=/mnt/backups ./scripts/backup.sh
./scripts/restore.sh backups/bughunter-<UTC>.dump     # --force skips the prompt
```

- `backup.sh` runs `pg_dump -Fc` inside `bugtracker_db`, verifies the dump with
  `pg_restore --list`, writes it with mode 600 and keeps the newest `BACKUP_KEEP`
  (default 14). It exits non-zero when the database container is not running.
- `restore.sh` validates the archive, asks you to type `restore`, takes a safety dump of the
  current data, stops the app, runs `pg_restore --clean --if-exists --single-transaction`,
  and starts the app again. The safety dump's name is printed, so a restore can be undone.
- Attachments live in the database, so one dump covers everything. Copy dumps off the
  host; a backup on the same disk does not survive losing it. Restore into a scratch stack
  now and then to prove the dumps work.
- Sessions stay valid across a restore only if `SESSION_SECRET` is unchanged.

## Data retention

The app runs `app/jobs/retention.py` daily (first run a few minutes after start) unless
`RETENTION_JOB_ENABLED=false`. It deletes, in small batches:

| What | Rule |
|---|---|
| Notifications | older than `NOTIFICATION_RETENTION_DAYS` (default 180; 0 keeps) |
| Sleuth conversations | idle longer than `CHAT_RETENTION_DAYS` (default 180; 0 keeps) |
| Sessions, used or expired reset tokens, expired idempotency keys | always |

Audit rows and work items are never pruned. To use an external scheduler instead, disable
the built-in run and call `python -m app.jobs.retention`. It is safe on several replicas.

## Health, logs, tracing

- `GET /api/health` returns `{"status","database","version","asset_version"}`: 200 when the
  database answers, 503 (`"degraded"`) when it does not. The image's `HEALTHCHECK` uses it.
  Startup DB work is bounded by `DB_STARTUP_TIMEOUT_SECONDS`, so a stuck database yields a
  degraded app, not a hung container.
- Every response has an `X-Request-ID` (a safe client-supplied value of 8 to 64 characters
  is kept). It is on every log line the request causes. Ask users for it when reporting
  an error.
- `LOG_FORMAT=json` writes one JSON object per line. `LOG_LEVEL=DEBUG` adds SMTP detail.
- Logs: `docker compose logs -f app`.

### OpenTelemetry

Off until `OTEL_EXPORTER_OTLP_ENDPOINT` is set (OTLP/gRPC, e.g. `http://otel-collector:4317`;
`http://` means cleartext gRPC). Then traces (FastAPI, SQLAlchemy, httpx), metrics and logs
are exported. Variables, all read in `app/telemetry.py`:

| Variable | Default |
|---|---|
| `OTEL_EXPORTER_OTLP_ENDPOINT` | blank = off |
| `OTEL_EXPORTER_OTLP_INSECURE` | not `https://` |
| `OTEL_EXPORTER_OTLP_HEADERS` | none (`k=v,k=v`) |
| `OTEL_SERVICE_NAME` / `OTEL_SERVICE_VERSION` | `bug-hunter` / `APP_VERSION` |
| `OTEL_DEPLOYMENT_ENVIRONMENT` | `APP_ENV` |
| `OTEL_RESOURCE_ATTRIBUTES` | none |
| `OTEL_TRACES_ENABLED` / `OTEL_METRICS_ENABLED` / `OTEL_LOGS_ENABLED` | `true` |
| `OTEL_TRACES_SAMPLER` / `OTEL_TRACES_SAMPLER_ARG` | `parentbased_always_on` / `1.0` |
| `OTEL_TRACES_EXCLUDED_URLS` | health checks and `/static` |
| `OTEL_METRICS_EXPORT_INTERVAL_MS` | `60000` |
| `OTEL_LOG_MIN_LEVEL` / `OTEL_LOG_EXCLUDE_LOGGERS` | `INFO` / `uvicorn.access,watchfiles` |
| `OTEL_LOG_REDACT_SENSITIVE` | `true` |
| `OTEL_INSTRUMENT_SQLALCHEMY` / `OTEL_INSTRUMENT_HTTPX` | `true` |

No traces? Check the start-up log for `OTLP export enabled`, that the signal is not
disabled, that the sampler is not `always_off`, and that the collector filter includes your
`OTEL_DEPLOYMENT_ENVIRONMENT`.

## Scaling limits

- The stack runs one Uvicorn worker. Login lockout, the per-IP, per-user and chat rate
  limiters, and the in-app digest scheduler keep their state in process memory. With
  several workers or replicas each has its own counters, so put a limit in the proxy and
  run the digest from one place.
- Postgres pool: `DB_POOL_SIZE` + `DB_MAX_OVERFLOW` (Compose sets a small pool).
- Behind a reverse proxy set `TRUST_PROXY_FORWARDED_FOR=true` and `TRUST_PROXY_HOP_COUNT`
  to the number of trusted hops; otherwise the app sees only the proxy's address. Without
  it, per-IP limits and lockout apply to the proxy as a whole.

## Sleuth and the docs folder

When `SLEUTH_RAG_ENABLED=1`, the index includes every `.md` and `.txt` file under
`SLEUTH_DOCS_DIR` (default `docs/`), so these pages become assistant knowledge. Point it
elsewhere if that is not wanted.

## Troubleshooting

See the *Troubleshooting* section of the [README](../README.md#troubleshooting).
