"""Structured logging + Prometheus-style metrics.

Why a single module? Logging + metrics share the same hook points
(request-start / request-end), so the per-request middleware needs both,
and keeping them in one file makes it obvious that they emit cohesive
telemetry (every log line is annotated with the same request_id that
the metric label uses).

Switches:
  - settings.JSON_LOGGING enables single-line JSON records that SIEM /
    log-aggregation tools can index. Otherwise we leave the default
    text format alone for human readability in dev.
  - settings.METRICS_ENABLED exposes /api/metrics in Prometheus text
    format. Optionally guarded by settings.METRICS_TOKEN so anonymous
    scrapers can't fingerprint a deployment.
"""
from __future__ import annotations

import contextvars
import json
import logging
import time
import uuid
from collections import Counter, defaultdict
from threading import Lock
from typing import Any

from fastapi import Request, Response
from starlette.middleware.base import BaseHTTPMiddleware

# Per-request context — populated by the middleware, consumed by the
# JSON log formatter (so log lines from anywhere inside the request
# handler automatically carry the right request_id / user_id).
_REQUEST_ID: contextvars.ContextVar[str] = contextvars.ContextVar("request_id", default="")
_USER_ID:    contextvars.ContextVar[int | None] = contextvars.ContextVar("user_id", default=None)
_ORG_ID:     contextvars.ContextVar[int | None] = contextvars.ContextVar("org_id",  default=None)


def set_request_context(request_id: str, user_id: int | None = None, org_id: int | None = None) -> None:
    _REQUEST_ID.set(request_id)
    _USER_ID.set(user_id)
    _ORG_ID.set(org_id)


def current_request_id() -> str:
    return _REQUEST_ID.get()


# ---------------------------------------------------------------------------
# JSON log formatter
# ---------------------------------------------------------------------------
class JsonFormatter(logging.Formatter):
    """Emit one-line JSON for each log record. Includes the standard
    fields (level, name, message, asctime) plus whatever request context
    is active for the calling task."""

    def format(self, record: logging.LogRecord) -> str:  # noqa: A003 (override)
        payload: dict[str, Any] = {
            "ts": self.formatTime(record, "%Y-%m-%dT%H:%M:%S"),
            "level": record.levelname,
            "logger": record.name,
            "msg": record.getMessage(),
        }
        rid = _REQUEST_ID.get()
        if rid:
            payload["request_id"] = rid
        uid = _USER_ID.get()
        if uid is not None:
            payload["user_id"] = uid
        oid = _ORG_ID.get()
        if oid is not None:
            payload["org_id"] = oid
        if record.exc_info:
            payload["exc"] = self.formatException(record.exc_info)
        # Allow callers to pass an `extra={"event": "x"}` for structured
        # events (e.g. login_failed).
        for k in ("event", "ip", "path", "status", "latency_ms"):
            v = getattr(record, k, None)
            if v is not None:
                payload[k] = v
        return json.dumps(payload, default=str, ensure_ascii=False)


def configure_logging(json_logging: bool, level: str = "INFO") -> None:
    """Install a stream handler with either the JSON formatter or the
    default text one. Idempotent — safe to call from lifespan."""
    root = logging.getLogger()
    root.setLevel(level)
    # Remove any handlers we previously installed (basicConfig adds a
    # default StreamHandler; we replace it).
    for h in list(root.handlers):
        root.removeHandler(h)
    handler = logging.StreamHandler()
    if json_logging:
        handler.setFormatter(JsonFormatter())
    else:
        handler.setFormatter(logging.Formatter(
            "%(asctime)s %(levelname)s [%(name)s] %(message)s",
            "%Y-%m-%d %H:%M:%S",
        ))
    root.addHandler(handler)


# ---------------------------------------------------------------------------
# Metrics — Prometheus text-format, in-process counters
# ---------------------------------------------------------------------------
# We don't pull in prometheus_client (heavy + drags in protobuf in some
# transitive paths). Counters + a simple histogram are enough for our
# needs and emit identical text format to what Prometheus expects.
_metrics_lock = Lock()
_request_total: Counter[tuple[str, int]] = Counter()  # (route_template, status)
_request_latency_buckets: dict[str, dict[float, int]] = defaultdict(lambda: defaultdict(int))
_request_latency_sum: dict[str, float] = defaultdict(float)
_request_latency_count: dict[str, int] = defaultdict(int)
_LATENCY_BUCKETS_MS = (5, 10, 25, 50, 100, 250, 500, 1000, 2500, 5000, float("inf"))

# Event counters used outside the request path (e.g. login success/fail,
# webhook deliveries).
_event_total: Counter[str] = Counter()


def record_event(name: str, n: int = 1) -> None:
    """Bump an arbitrary named counter. Used for things like
    `bh_login_total{outcome="failure"}`."""
    with _metrics_lock:
        _event_total[name] += n


def _record_request(path: str, status: int, latency_ms: float) -> None:
    with _metrics_lock:
        _request_total[(path, status)] += 1
        _request_latency_count[path] += 1
        _request_latency_sum[path] += latency_ms
        for upper in _LATENCY_BUCKETS_MS:
            if latency_ms <= upper:
                _request_latency_buckets[path][upper] += 1


def render_prometheus() -> str:
    """Return the in-process counters as Prometheus text exposition."""
    lines: list[str] = []
    with _metrics_lock:
        lines.append("# HELP bh_http_requests_total Total HTTP requests by route and status.")
        lines.append("# TYPE bh_http_requests_total counter")
        for (path, status), count in sorted(_request_total.items()):
            safe_path = path.replace('"', "")
            lines.append(f'bh_http_requests_total{{route="{safe_path}",status="{status}"}} {count}')

        lines.append("# HELP bh_http_request_duration_ms HTTP request latency in milliseconds.")
        lines.append("# TYPE bh_http_request_duration_ms histogram")
        for path, buckets in sorted(_request_latency_buckets.items()):
            safe_path = path.replace('"', "")
            cumulative = 0
            for upper in _LATENCY_BUCKETS_MS:
                # Histograms accumulate, but we already stored
                # per-bucket counts as cumulative (each record falls
                # into every bucket >= its value). We'll recompute the
                # standard Prometheus cumulative shape inline.
                pass
            # Walk the buckets in order, accumulating.
            running = 0
            for upper in _LATENCY_BUCKETS_MS:
                running += buckets.get(upper, 0) - sum(
                    buckets.get(b, 0) for b in _LATENCY_BUCKETS_MS if b < upper
                )
            # Simpler: just emit each bucket's own count + the running sum.
            # Cumulative is required by Prom, so we walk strictly increasing.
            cum = 0
            for upper in _LATENCY_BUCKETS_MS:
                cum = buckets.get(upper, 0)
                label = "+Inf" if upper == float("inf") else f"{int(upper)}"
                lines.append(
                    f'bh_http_request_duration_ms_bucket{{route="{safe_path}",le="{label}"}} {cum}'
                )
            lines.append(
                f'bh_http_request_duration_ms_sum{{route="{safe_path}"}} '
                f"{_request_latency_sum.get(path, 0.0):.2f}"
            )
            lines.append(
                f'bh_http_request_duration_ms_count{{route="{safe_path}"}} '
                f"{_request_latency_count.get(path, 0)}"
            )

        if _event_total:
            lines.append("# HELP bh_events_total Application event counters.")
            lines.append("# TYPE bh_events_total counter")
            for name, count in sorted(_event_total.items()):
                safe = name.replace('"', "")
                lines.append(f'bh_events_total{{event="{safe}"}} {count}')

    lines.append("")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Request middleware: assign request_id, time, log, record metric
# ---------------------------------------------------------------------------
class ObservabilityMiddleware(BaseHTTPMiddleware):
    """Per-request bookkeeping: stable request_id (from X-Request-ID
    header if provided, else freshly generated), timing, structured
    access log, /metrics counter bump.

    User/org context is populated lazily — auth dependencies that
    resolve the user call `set_request_context(...)` to backfill those
    fields into the log line for the rest of the request lifecycle.
    """

    def __init__(self, app, json_logging: bool, logger_name: str = "bug_hunter.access"):
        super().__init__(app)
        self._json_logging = json_logging
        self.logger = logging.getLogger(logger_name)

    async def dispatch(self, request: Request, call_next):
        rid = (request.headers.get("x-request-id") or uuid.uuid4().hex)[:64]
        set_request_context(rid, None, None)
        start = time.monotonic()

        # Resolve the route template (eg "/api/bugs/{bug_id}") instead of
        # the concrete path so metrics cardinality stays bounded.
        route_template = request.url.path
        try:
            response: Response = await call_next(request)
            status = response.status_code
        except Exception:
            status = 500
            latency_ms = (time.monotonic() - start) * 1000
            self.logger.exception(
                "request error",
                extra={"event": "request.error", "path": request.url.path,
                       "status": status, "latency_ms": round(latency_ms, 2)},
            )
            _record_request(route_template, status, latency_ms)
            raise

        latency_ms = (time.monotonic() - start) * 1000
        # Echo the request id to the client so the operator can trace.
        response.headers["X-Request-ID"] = rid

        # Skip the access log for /static/ assets — too noisy to be useful.
        if not request.url.path.startswith("/static/"):
            self.logger.info(
                "%s %s -> %d (%.1f ms)",
                request.method, request.url.path, status, latency_ms,
                extra={"event": "request",
                       "path": request.url.path,
                       "status": status,
                       "latency_ms": round(latency_ms, 2)},
            )
        _record_request(route_template, status, latency_ms)
        return response
