"""In-process Prometheus counters for GET /api/metrics (off unless METRICS_ENABLED).

No client library: request counts and a latency histogram per route template, plus named
event counters (logins, webhook deliveries), rendered in the Prometheus text format.
Counters are per worker process, like the rate limiter.
"""
from __future__ import annotations

import time
from collections import Counter, defaultdict
from threading import Lock

from fastapi import Request, Response
from starlette.middleware.base import BaseHTTPMiddleware

_LATENCY_BUCKETS_MS = (5, 10, 25, 50, 100, 250, 500, 1000, 2500, 5000)

_lock = Lock()
_requests: Counter[tuple[str, int]] = Counter()
_latency_buckets: dict[str, Counter[float]] = defaultdict(Counter)
_latency_sum: dict[str, float] = defaultdict(float)
_latency_count: Counter[str] = Counter()
_events: Counter[str] = Counter()


def record_event(name: str, n: int = 1) -> None:
    """Bump a named application counter, e.g. ``login_failure``."""
    with _lock:
        _events[name] += n


def _record_request(route: str, status: int, latency_ms: float) -> None:
    with _lock:
        _requests[(route, status)] += 1
        _latency_count[route] += 1
        _latency_sum[route] += latency_ms
        for upper in _LATENCY_BUCKETS_MS:
            if latency_ms <= upper:
                _latency_buckets[route][upper] += 1


def _label(value: str) -> str:
    return value.replace("\\", "").replace('"', "").replace("\n", "")


def render() -> str:
    """The counters in Prometheus text exposition format."""
    lines = [
        "# HELP bh_http_requests_total Requests by route template and status.",
        "# TYPE bh_http_requests_total counter",
    ]
    with _lock:
        for (route, status), count in sorted(_requests.items()):
            lines.append(f'bh_http_requests_total{{route="{_label(route)}",status="{status}"}} {count}')
        lines += [
            "# HELP bh_http_request_duration_ms Request latency in milliseconds.",
            "# TYPE bh_http_request_duration_ms histogram",
        ]
        for route in sorted(_latency_count):
            safe = _label(route)
            for upper in _LATENCY_BUCKETS_MS:
                lines.append(
                    f'bh_http_request_duration_ms_bucket{{route="{safe}",le="{upper}"}} '
                    f"{_latency_buckets[route][upper]}"
                )
            lines.append(
                f'bh_http_request_duration_ms_bucket{{route="{safe}",le="+Inf"}} {_latency_count[route]}'
            )
            lines.append(f'bh_http_request_duration_ms_sum{{route="{safe}"}} {_latency_sum[route]:.2f}')
            lines.append(f'bh_http_request_duration_ms_count{{route="{safe}"}} {_latency_count[route]}')
        if _events:
            lines += ["# HELP bh_events_total Application events.", "# TYPE bh_events_total counter"]
            for name, count in sorted(_events.items()):
                lines.append(f'bh_events_total{{event="{_label(name)}"}} {count}')
    return "\n".join(lines) + "\n"


class MetricsMiddleware(BaseHTTPMiddleware):
    """Counts every API request under its route template (bounded label cardinality)."""

    async def dispatch(self, request: Request, call_next):
        start = time.monotonic()
        status = 500
        try:
            response: Response = await call_next(request)
            status = response.status_code
            return response
        finally:
            route = request.scope.get("route")
            template = getattr(route, "path", None)
            if template and template.startswith("/api/"):
                _record_request(template, status, (time.monotonic() - start) * 1000)
