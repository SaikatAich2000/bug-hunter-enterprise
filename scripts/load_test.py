#!/usr/bin/env python3
"""
Bounded, non-destructive load test for Bug Hunter, sized for the
resource-constrained deployment target (0.5 vCPU / 512 MB RAM / HDD-class
storage container).

The whole run is read-only. It exercises only these endpoints:

    GET  /api/health                     health
    POST /api/auth/login                 session (once, to authenticate)
    GET  /api/auth/me                    login/session read
    GET  /api/projects                   projects
    GET  /api/bugs?item_type=...         work-item list + filter
    GET  /api/bugs/{id}                  work-item (Story) detail
    GET  /api/git/work-items/{id}/branches  branch history read
    GET  /api/stats                      dashboard/stats
    GET  /api/reports/types              report read

It never creates, updates or deletes anything, never touches a branch
create/remove operation, and never writes to the database. When the instance
has no Story to read, the Story-detail and branch-history scenarios are simply
skipped (reported as such).

Optionally samples the target container's CPU/memory via `docker stats` while
the run is in progress, so you can see whether it stays within the 0.5 vCPU /
512 MB envelope under load.

Usage:
    python scripts/load_test.py --base-url http://localhost:8765 \\
        --users 10 --duration 60

    # Also sample container resource usage (requires the container name and
    # a working local Docker):
    python scripts/load_test.py --docker-container bugtracker_app

No extra dependencies beyond what's already in requirements.txt (httpx).
"""
from __future__ import annotations

import argparse
import asyncio
import os
import random
import statistics
import subprocess
import sys
import time
from dataclasses import dataclass, field

import httpx

# Credentials come from --email/--password or BH_LOAD_EMAIL/BH_LOAD_PASSWORD.
# There is no password fallback: the bootstrap password is generated per
# install (scripts/gen_local_env_secrets.py), so no default could work.
DEFAULT_ADMIN_EMAIL = os.getenv("BH_LOAD_EMAIL", "admin@bughunter.local")
DEFAULT_ADMIN_PASSWORD = os.getenv("BH_LOAD_PASSWORD", "")


@dataclass
class ScenarioResult:
    name: str
    latencies_ms: list[float] = field(default_factory=list)
    errors: int = 0
    status_counts: dict[int, int] = field(default_factory=dict)


@dataclass
class LoadTestReport:
    scenarios: dict[str, ScenarioResult] = field(default_factory=dict)
    started_at: float = 0.0
    ended_at: float = 0.0

    def record(self, name: str, latency_ms: float, status: int) -> None:
        s = self.scenarios.setdefault(name, ScenarioResult(name))
        s.latencies_ms.append(latency_ms)
        s.status_counts[status] = s.status_counts.get(status, 0) + 1
        if status >= 400:
            s.errors += 1

    def total_requests(self) -> int:
        return sum(len(s.latencies_ms) for s in self.scenarios.values())

    def total_errors(self) -> int:
        return sum(s.errors for s in self.scenarios.values())


def _percentile(values: list[float], pct: float) -> float:
    if not values:
        return 0.0
    values = sorted(values)
    k = (len(values) - 1) * (pct / 100)
    f, c = int(k), min(int(k) + 1, len(values) - 1)
    if f == c:
        return values[f]
    return values[f] + (values[c] - values[f]) * (k - f)


async def _docker_stats_sampler(container: str, samples: list[dict], stop: asyncio.Event) -> None:
    """Poll `docker stats --no-stream` every 2s for CPU%/memory usage."""
    while not stop.is_set():
        try:
            proc = await asyncio.create_subprocess_exec(
                "docker", "stats", "--no-stream", "--format",
                "{{.CPUPerc}}\t{{.MemUsage}}", container,
                stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
            )
            out, _ = await proc.communicate()
            line = out.decode().strip()
            if line:
                cpu_str, mem_str = line.split("\t", 1)
                samples.append({"cpu": cpu_str.strip(), "mem": mem_str.strip(), "t": time.time()})
        except (OSError, ValueError):
            pass
        try:
            await asyncio.wait_for(stop.wait(), timeout=2.0)
        except asyncio.TimeoutError:
            pass


async def _login(client: httpx.AsyncClient, email: str, password: str) -> bool:
    r = await client.post("/api/auth/login", json={"email": email, "password": password})
    return r.status_code == 200


async def _timed(report: LoadTestReport, name: str, coro) -> None:
    start = time.perf_counter()
    try:
        resp = await coro
        status = resp.status_code
    except httpx.HTTPError:
        status = 599
    latency_ms = (time.perf_counter() - start) * 1000
    report.record(name, latency_ms, status)


# Weighted, strictly read-only scenario mix. Every entry is a GET: the load
# test never creates, updates or deletes anything, and never exercises branch
# create/remove (those hit the provider API and are covered by tests instead).
_SCENARIOS: list[tuple[str, int]] = [
    ("health", 5),
    ("session_me", 10),
    ("projects", 10),
    ("list_bugs", 25),
    ("list_bugs_filtered", 10),
    ("story_detail", 10),
    ("story_branches", 10),
    ("stats", 15),
    ("report_types", 5),
]


async def _user_session(
    client: httpx.AsyncClient, story_id: int | None,
    report: LoadTestReport, stop: asyncio.Event,
) -> None:
    """One simulated user's request loop, sharing one already-authenticated
    client (see main_async) — many real users hitting the app concurrently is
    the thing under test here, not the per-IP login rate limiter, which has
    its own dedicated tests in tests/test_security.py."""
    names = [name for name, _ in _SCENARIOS]
    weights = [weight for _, weight in _SCENARIOS]
    while not stop.is_set():
        action = random.choices(names, weights=weights)[0]

        if action == "health":
            await _timed(report, "health", client.get("/api/health"))
        elif action == "session_me":
            await _timed(report, "session_me", client.get("/api/auth/me"))
        elif action == "projects":
            await _timed(report, "projects", client.get("/api/projects"))
        elif action == "list_bugs_filtered":
            await _timed(report, "list_bugs_filtered", client.get(
                "/api/bugs", params={"item_type": "Bug", "page": 1, "page_size": 20},
            ))
        elif action == "story_detail" and story_id:
            await _timed(report, "story_detail", client.get(f"/api/bugs/{story_id}"))
        elif action == "story_branches" and story_id:
            await _timed(report, "story_branches", client.get(
                f"/api/git/work-items/{story_id}/branches",
            ))
        elif action == "stats":
            await _timed(report, "stats", client.get("/api/stats"))
        elif action == "report_types":
            await _timed(report, "report_types", client.get("/api/reports/types"))
        else:
            await _timed(report, "list_bugs", client.get(
                "/api/bugs", params={"page": 1, "page_size": 20},
            ))

        await asyncio.sleep(random.uniform(0.2, 1.0))  # think time


async def _read_targets(client: httpx.AsyncClient) -> tuple[int | None, int | None]:
    """Read-only target discovery on the already-authenticated `client`.

    Returns (project_id, story_id). Nothing is created or modified: when the
    instance has no accessible project (or no Story) the dependent scenarios
    are simply skipped and reported as such.
    """
    project_id: int | None = None
    story_id: int | None = None
    r = await client.get("/api/projects")
    if r.status_code == 200 and r.json():
        project_id = r.json()[0]["id"]
    r = await client.get("/api/bugs", params={"item_type": "Story", "page": 1, "page_size": 1})
    if r.status_code == 200:
        items = r.json().get("items") or []
        if items:
            story_id = items[0]["id"]
    return project_id, story_id


def _print_report(report: LoadTestReport, duration_s: float, docker_samples: list[dict]) -> bool:
    print("\n" + "=" * 78)
    print(f"LOAD TEST REPORT — {duration_s:.1f}s run")
    print("=" * 78)
    print(f"{'Scenario':<18}{'Count':>8}{'Errors':>8}{'p50 ms':>10}{'p95 ms':>10}{'p99 ms':>10}{'max ms':>10}")
    print("-" * 78)
    for name, s in sorted(report.scenarios.items()):
        lat = s.latencies_ms
        print(f"{name:<18}{len(lat):>8}{s.errors:>8}"
              f"{_percentile(lat, 50):>10.1f}{_percentile(lat, 95):>10.1f}"
              f"{_percentile(lat, 99):>10.1f}{(max(lat) if lat else 0):>10.1f}")
    print("-" * 78)
    total = report.total_requests()
    errors = report.total_errors()
    rps = total / duration_s if duration_s else 0
    print(f"Total requests: {total} | Errors: {errors} ({errors / total * 100 if total else 0:.1f}%) "
          f"| Throughput: {rps:.2f} req/s")

    if docker_samples:
        cpu_vals = []
        for s in docker_samples:
            try:
                cpu_vals.append(float(s["cpu"].rstrip("%")))
            except ValueError:
                pass
        if cpu_vals:
            print(f"\nContainer CPU%: avg={statistics.mean(cpu_vals):.1f}% "
                  f"max={max(cpu_vals):.1f}% (limit: 50% = 0.5 vCPU)")
        if docker_samples:
            print(f"Container mem (last sample): {docker_samples[-1]['mem']}")
    print("=" * 78)

    # Pass/fail verdict against the resource-constrained target.
    verdict_ok = True
    error_rate = (errors / total) if total else 0.0
    if error_rate > 0.01:
        verdict_ok = False
        print("[FAIL] Error rate exceeds 1%.")
    p95_list = _percentile(report.scenarios.get("list_bugs", ScenarioResult("list_bugs")).latencies_ms, 95)
    if p95_list and p95_list > 2000:
        verdict_ok = False
        print(f"[FAIL] list_bugs p95 latency {p95_list:.0f}ms exceeds 2000ms budget.")
    if docker_samples and cpu_vals and max(cpu_vals) > 55:
        print(f"[WARN] Peak CPU {max(cpu_vals):.1f}% is above the 50% (0.5 vCPU) limit "
              f"— check for missing indexes or N+1 queries under load.")
    if verdict_ok:
        print("[OK] Within resource-constrained targets.")
    return verdict_ok


async def main_async(args: argparse.Namespace) -> int:
    report = LoadTestReport()
    stop = asyncio.Event()
    docker_samples: list[dict] = []

    async with httpx.AsyncClient(base_url=args.base_url, timeout=15.0) as client:
        if not args.password:
            print("[FAIL] No password: pass --password or set BH_LOAD_PASSWORD.")
            return 2
        if not await _login(client, args.email, args.password):
            print("[FAIL] Could not log in with the given credentials.")
            return 1
        report.record("login", 0.0, 200)

        project_id, story_id = await _read_targets(client)
        if story_id is None:
            print("[INFO] No Story found — story_detail/story_branches scenarios skipped.")
        if project_id is None:
            print("[INFO] No accessible project found on this instance.")

        tasks = [
            asyncio.create_task(_user_session(client, story_id, report, stop))
            for _ in range(args.users)
        ]
        sampler_task = None
        if args.docker_container:
            sampler_task = asyncio.create_task(
                _docker_stats_sampler(args.docker_container, docker_samples, stop)
            )

        report.started_at = time.time()
        print(f"Running {args.users} concurrent user(s) for {args.duration}s against {args.base_url} ...")
        await asyncio.sleep(args.duration)
        stop.set()
        report.ended_at = time.time()

        await asyncio.gather(*tasks, return_exceptions=True)
        if sampler_task:
            await sampler_task

    ok = _print_report(report, report.ended_at - report.started_at, docker_samples)
    return 0 if ok else 1


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--base-url", default="http://localhost:8765")
    parser.add_argument("--users", type=int, default=10, help="Concurrent simulated users")
    parser.add_argument("--duration", type=int, default=60, help="Run duration in seconds")
    parser.add_argument("--email", default=DEFAULT_ADMIN_EMAIL)
    parser.add_argument("--password", default=DEFAULT_ADMIN_PASSWORD)
    parser.add_argument("--no-writes", action="store_true",
                        help="Retained for compatibility; the run is always read-only")
    parser.add_argument("--docker-container", default=None,
                         help="Container name to sample CPU/mem from via `docker stats` (e.g. bugtracker_app)")
    args = parser.parse_args()

    try:
        return asyncio.run(main_async(args))
    except KeyboardInterrupt:
        return 130


if __name__ == "__main__":
    sys.exit(main())
