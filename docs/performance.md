# Performance

No numbers are published here yet. The ones below must come from running
`scripts/loadtest.py` on your hardware; anything else would be a guess.

## What is limited by design

- Compose caps the stack at 0.5 vCPU / 512 MB (app 0.30 / 320 MB, database 0.20 / 192 MB) and
  runs one Uvicorn worker.
- Postgres pool: `DB_POOL_SIZE` + `DB_MAX_OVERFLOW`; statements time out after
  `DB_STATEMENT_TIMEOUT_MS` (30 s) and lock waits after `DB_LOCK_TIMEOUT_MS` (10 s).
- Bodies: `MAX_REQUEST_BODY_BYTES` (60 MiB, uploads) and `MAX_JSON_BODY_BYTES` (2 MiB).
  Attachments are 50 MB each; Excel exports stop at `MAX_REPORT_ROWS` (50 000, then 413).
- Login is 8 requests/min/IP and writes are rate-limited per user (comments 30/min,
  uploads 20/min). The load script therefore signs in once and shares the session.

## Measuring

Use a throwaway instance with a user that can create and delete work items (an admin), and
a project id to create items in. The script creates items titled `loadtest-...` and deletes
them at the end of each level.

```bash
pip install psutil                      # optional, for server RSS
# Find the server pid: Docker: docker top bugtracker_app; local: the uvicorn process.
python scripts/loadtest.py --base-url http://localhost:8765 \
    --email admin@example.com --password '...' --project-id 1 \
    --levels 10,25,50,100,200 --duration 30 --pid <server pid>
```

Each virtual user loops: four reads (`/api/auth/me`, `/api/projects`, `/api/bugs`,
`/api/stats`), then one write (`POST /api/bugs`). The script prints, per level and for
reads and writes: requests per second, p50, p95, p99 (ms), error count (HTTP 4xx/5xx or
transport failures), and the peak server RSS. A 429 counts as an error, so a level that
trips a limiter shows up as errors rather than as a good number.

For the container's CPU and memory as Docker sees them (no pid needed), use the read-only
script: `python scripts/load_test.py --base-url ... --users 15 --duration 90 --docker-container bugtracker_app`.

## Results

To be filled by running `scripts/loadtest.py` (command above). Record the host, the date, the
`APP_VERSION`, whether PostgreSQL ran in Compose, the row counts (`/api/stats`), and the
output table verbatim.

| Users | Reads req/s | Reads p50 / p95 / p99 ms | Writes req/s | Writes p50 / p95 / p99 ms | Errors | Peak RSS MiB |
|---|---|---|---|---|---|---|
| 10 | | | | | | |
| 25 | | | | | | |
| 50 | | | | | | |
| 100 | | | | | | |
| 200 | | | | | | |

## Reading the result

- Errors above zero at a level means that level is past what the deployment sustains (or a
  limiter fired); do not quote its latency as capacity.
- Compare RSS with the 320 MB app limit in Compose. A container killed for memory restarts
  and the load script shows transport errors.
- Repeat on PostgreSQL with realistic data volume; SQLite results do not transfer.
