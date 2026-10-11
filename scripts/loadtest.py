#!/usr/bin/env python3
"""Closed-loop load test: N virtual users at 10/25/50/100/200 (or --levels), reads plus writes.

Signs in once (login is rate-limited to 8/min per IP) and shares that session across users.
Each user loops: 4 reads, then 1 write (creates a work item titled "loadtest-..."); every item
it made is deleted at the end, so use an admin account. Reports req/s, p50/p95/p99 and error
counts per read/write, and the server's RSS if you pass --pid (needs `pip install psutil`).

  python scripts/loadtest.py --base-url http://localhost:8765 --email a@b.c --password ... \
      --project-id 1 --pid 12345 --duration 30

Credentials may come from BH_LOAD_EMAIL / BH_LOAD_PASSWORD. Run it against a test instance,
not production: the write traffic is real.
"""
from __future__ import annotations

import argparse
import asyncio
import os
import statistics
import sys
import time

import httpx

READS = ("/api/auth/me", "/api/projects", "/api/bugs", "/api/stats")


def _pct(vals: list[float], p: float) -> float:
    if not vals:
        return 0.0
    s = sorted(vals)
    return s[min(len(s) - 1, int(round(p / 100 * (len(s) - 1))))]


class Stats:
    def __init__(self) -> None:
        self.lat: dict[str, list[float]] = {"read": [], "write": []}
        self.err: dict[str, int] = {"read": 0, "write": 0}
        self.created: list[int] = []


async def _user(client: httpx.AsyncClient, st: Stats, project_id: int, stop: float, uid: int) -> None:
    n = 0
    while time.monotonic() < stop:
        n += 1
        kind, call = ("write", None) if n % 5 == 0 else ("read", READS[n % len(READS)])
        t0 = time.perf_counter()
        try:
            if kind == "read":
                r = await client.get(call)
            else:
                r = await client.post("/api/bugs", json={
                    "project_id": project_id, "title": f"loadtest-{uid}-{n}", "item_type": "Bug"})
            ok = r.status_code < 400
            if ok and kind == "write":
                st.created.append(r.json()["id"])
        except httpx.HTTPError:
            ok = False
        st.lat[kind].append(time.perf_counter() - t0)
        if not ok:
            st.err[kind] += 1


async def _level(base: str, cookies: httpx.Cookies, users: int, duration: float, project_id: int, rss: list[float], proc):
    st = Stats()
    limits = httpx.Limits(max_connections=users, max_keepalive_connections=users)
    async with httpx.AsyncClient(base_url=base, cookies=cookies, limits=limits, timeout=60) as client:
        stop = time.monotonic() + duration
        t0 = time.monotonic()

        async def sample():
            while time.monotonic() < stop and proc:
                rss.append(proc.memory_info().rss / 2**20)
                await asyncio.sleep(1)

        await asyncio.gather(sample(), *(_user(client, st, project_id, stop, i) for i in range(users)))
        elapsed = time.monotonic() - t0
        for bug_id in st.created:  # best-effort cleanup
            try:
                await client.delete(f"/api/bugs/{bug_id}")
            except httpx.HTTPError:
                pass
    return st, elapsed


async def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--base-url", default="http://localhost:8765")
    ap.add_argument("--email", default=os.getenv("BH_LOAD_EMAIL", ""))
    ap.add_argument("--password", default=os.getenv("BH_LOAD_PASSWORD", ""))
    ap.add_argument("--project-id", type=int, required=True, help="project the test items are created in")
    ap.add_argument("--levels", default="10,25,50,100,200")
    ap.add_argument("--duration", type=float, default=30, help="seconds per level")
    ap.add_argument("--pid", type=int, help="server process id, to sample RSS (needs psutil)")
    a = ap.parse_args()
    if not (a.email and a.password):
        print("pass --email/--password or set BH_LOAD_EMAIL/BH_LOAD_PASSWORD", file=sys.stderr)
        return 2
    proc = None
    if a.pid:
        try:
            import psutil
            proc = psutil.Process(a.pid)
        except ImportError:
            print("psutil not installed; skipping RSS sampling", file=sys.stderr)

    async with httpx.AsyncClient(base_url=a.base_url, timeout=30) as c:
        r = await c.post("/api/auth/login", json={"email": a.email, "password": a.password})
        if r.status_code >= 400:
            print(f"login failed: {r.status_code} {r.text[:200]}", file=sys.stderr)
            return 1
        cookies = c.cookies

    print(f"{'users':>5} {'kind':<6} {'req/s':>8} {'p50 ms':>8} {'p95 ms':>8} {'p99 ms':>8} {'errors':>7}  rss MiB (max)")
    for users in (int(x) for x in a.levels.split(",")):
        rss: list[float] = []
        st, elapsed = await _level(a.base_url, cookies, users, a.duration, a.project_id, rss, proc)
        for kind in ("read", "write"):
            v = [x * 1000 for x in st.lat[kind]]
            extra = f"{max(rss):.0f}" if rss and kind == "read" else ""
            print(f"{users:>5} {kind:<6} {len(v) / elapsed:>8.1f} {statistics.median(v) if v else 0:>8.1f} "
                  f"{_pct(v, 95):>8.1f} {_pct(v, 99):>8.1f} {st.err[kind]:>7}  {extra}")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
