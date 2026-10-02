"""Production telemetry for SigNoz: OTLP traces, metrics and structured logs.

Design rules
------------
* **Off unless configured.** Nothing is exported until
  ``OTEL_EXPORTER_OTLP_ENDPOINT`` is set, so tests and local runs stay
  wire-silent; a set endpoint turns on the signals enabled below.
* **Console is never sacrificed.** ``LOG_FORMAT=text`` (default) keeps the
  current ``INFO:logger:message`` console format; ``LOG_FORMAT=json`` switches
  the console to one JSON object per line (for log shippers). The OTLP handler
  is added *alongside* the console handler, never instead of it.
* **Correlation is real, not invented.** trace_id/span_id on log records come
  from the active span through the OTel context, so SigNoz can pivot
  log -> trace and show request spans with HTTP attributes.
* **Noise is filtered at the edge.** Framework/access chatter is excluded from
  export by default (``OTEL_LOG_EXCLUDE_LOGGERS``): requests arrive as traces,
  not as hundreds of identical access lines.
* **Secrets do not leave the process.** Attributes whose key looks sensitive are
  redacted before export (``OTEL_LOG_REDACT_SENSITIVE``).
* **The collector URL lives in .env / platform secrets, never in code.**

Environment (all optional; see README "Observability"):
  OTEL_EXPORTER_OTLP_ENDPOINT      collector URL (``http://`` = cleartext gRPC)
  OTEL_EXPORTER_OTLP_INSECURE      override TLS detection (default: not https)
  OTEL_EXPORTER_OTLP_HEADERS       ``k=v,k=v`` collector auth headers
  OTEL_SERVICE_NAME                default ``bug-hunter``
  OTEL_SERVICE_VERSION             default APP_VERSION
  OTEL_DEPLOYMENT_ENVIRONMENT      default APP_ENV
  OTEL_RESOURCE_ATTRIBUTES         extra ``k=v,k=v`` resource attributes
  OTEL_TRACES_ENABLED              default true
  OTEL_TRACES_SAMPLER              parentbased_always_on (default) | always_on |
                                   always_off | traceidratio | parentbased_traceidratio
  OTEL_TRACES_SAMPLER_ARG          ratio for the *ratio samplers (default 1.0)
  OTEL_TRACES_EXCLUDED_URLS        URL regexes kept out of server spans
  OTEL_METRICS_ENABLED             default true
  OTEL_METRICS_EXPORT_INTERVAL_MS  default 60000
  OTEL_LOGS_ENABLED                default true
  OTEL_LOG_MIN_LEVEL               lowest level exported (default INFO)
  OTEL_LOG_EXCLUDE_LOGGERS         csv of loggers kept out of export
  OTEL_LOG_REDACT_SENSITIVE        default true
  OTEL_INSTRUMENT_SQLALCHEMY       default true
  OTEL_INSTRUMENT_HTTPX            default true
  LOG_FORMAT                       ``text`` (default) | ``json``
"""

from __future__ import annotations

import json
import logging
import os
import threading
import weakref
from datetime import datetime, timezone
from typing import Any

from app.config import _env_bool, _env_float, _env_int

_logger = logging.getLogger("bug_hunter.telemetry")

DEFAULT_SERVICE_NAME = "bug-hunter"
DEFAULT_LOG_MIN_LEVEL = "INFO"
# Requests are exported as spans; their access lines stay on the console only.
DEFAULT_EXCLUDED_LOGGERS = ("uvicorn.access", "watchfiles")
DEFAULT_EXCLUDED_URLS = "/api/health,/static"
# Substring match, case-insensitive: a key containing any of these is redacted.
SENSITIVE_KEY_PARTS = (
    "password",
    "passwd",
    "secret",
    "token",
    "authorization",
    "cookie",
    "api_key",
    "apikey",
    "credential",
    "private_key",
    "jwt",
    "session",
)
REDACTED = "[REDACTED]"

_SEVERITY_NUMBERS = {
    "TRACE": 1,
    "DEBUG": 5,
    "INFO": 9,
    "WARN": 13,
    "WARNING": 13,
    "ERROR": 17,
    "CRITICAL": 21,
    "FATAL": 21,
}
_SEVERITY_TEXT = {"WARNING": "WARN", "CRITICAL": "FATAL"}

_SAMPLER_NAMES = (
    "parentbased_always_on",
    "parentbased_always_off",
    "parentbased_traceidratio",
    "always_on",
    "always_off",
    "traceidratio",
)

_lock = threading.Lock()
# Providers/handler kept so shutdown can flush queued data and detach handlers.
_tracer_provider = None
_meter_provider = None
_logger_provider = None
_log_handler: logging.Handler | None = None
_instrumentors: dict[str, Any] = {}
_enabled = False
_app_instrumented = False


# --------------------------------------------------------------------- env reading


def _endpoint() -> str:
    return os.getenv("OTEL_EXPORTER_OTLP_ENDPOINT", "").strip()


def _insecure() -> bool:
    """Cleartext gRPC unless the endpoint says https:// (or the flag overrides)."""
    if os.getenv("OTEL_EXPORTER_OTLP_INSECURE", "").strip():
        return _env_bool("OTEL_EXPORTER_OTLP_INSECURE", True)
    return not _endpoint().startswith("https://")


def _service_name() -> str:
    return os.getenv("OTEL_SERVICE_NAME", "").strip() or DEFAULT_SERVICE_NAME


def _traces_enabled() -> bool:
    return _env_bool("OTEL_TRACES_ENABLED", True)


def _metrics_enabled() -> bool:
    return _env_bool("OTEL_METRICS_ENABLED", True)


def _logs_enabled() -> bool:
    return _env_bool("OTEL_LOGS_ENABLED", True)


def _log_min_level() -> int:
    name = os.getenv("OTEL_LOG_MIN_LEVEL", "").strip().upper() or DEFAULT_LOG_MIN_LEVEL
    level = logging.getLevelName(name)
    if not isinstance(level, int):
        _logger.warning("Unknown OTEL_LOG_MIN_LEVEL=%r; using %s", name, DEFAULT_LOG_MIN_LEVEL)
        return logging.INFO
    return level


def _excluded_loggers() -> tuple[str, ...]:
    raw = os.getenv("OTEL_LOG_EXCLUDE_LOGGERS")
    if raw is None:
        return DEFAULT_EXCLUDED_LOGGERS
    return tuple(part.strip() for part in raw.split(",") if part.strip())


def _excluded_urls() -> str:
    raw = os.getenv("OTEL_TRACES_EXCLUDED_URLS")
    return DEFAULT_EXCLUDED_URLS if raw is None else raw.strip()


def _app_setting(name: str) -> str:
    """Read a product setting (APP_VERSION/APP_ENV) without ever raising."""
    try:
        from app.config import get_settings

        return str(getattr(get_settings(), name, "") or "").strip()
    except Exception:  # pragma: no cover - settings must never break telemetry
        return ""


# ------------------------------------------------------------- secrets + filters


def _is_sensitive_key(key: object) -> bool:
    lowered = str(key).lower()
    return any(part in lowered for part in SENSITIVE_KEY_PARTS)


def _redact(value: Any) -> Any:
    """Recursively replace sensitive mapping values with ``[REDACTED]``."""
    if isinstance(value, dict):
        return {k: (REDACTED if _is_sensitive_key(k) else _redact(v)) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_redact(item) for item in value]
    return value


class ExcludeLoggersFilter(logging.Filter):
    """Drop records from noisy loggers so they never reach the collector."""

    def __init__(self, prefixes: tuple[str, ...]) -> None:
        super().__init__()
        self._prefixes = tuple(prefixes)

    def filter(self, record: logging.LogRecord) -> bool:
        name = record.name or ""
        return not any(name == p or name.startswith(p + ".") for p in self._prefixes)


class SensitiveAttributeFilter(logging.Filter):
    """Redact structured ``extra={"attributes": {...}}`` before export."""

    def filter(self, record: logging.LogRecord) -> bool:
        attributes = getattr(record, "attributes", None)
        if isinstance(attributes, dict) and attributes:
            record.attributes = _redact(attributes)
        return True


# ----------------------------------------------------------------- console format


def _iso_utc(timestamp: float) -> str:
    return datetime.fromtimestamp(timestamp, tz=timezone.utc).isoformat().replace("+00:00", "Z")


class JsonLogFormatter(logging.Formatter):
    """One JSON object per line: machine-readable without extra collectors."""

    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, Any] = {
            "timestamp": _iso_utc(record.created),
            "severity": _SEVERITY_TEXT.get(record.levelname, record.levelname),
            "severity_number": _SEVERITY_NUMBERS.get(record.levelname.upper(), 0),
            "logger": record.name,
            "message": record.getMessage(),
            "code": {"file": record.pathname, "function": record.funcName, "line": record.lineno},
            "process": {"id": record.process, "name": record.processName},
            "thread": record.threadName,
            "service": _service_name(),
        }
        trace_id = getattr(record, "otelTraceID", "")
        span_id = getattr(record, "otelSpanID", "")
        if trace_id:
            payload["trace_id"] = trace_id
        if span_id:
            payload["span_id"] = span_id
        attributes = getattr(record, "attributes", None)
        if isinstance(attributes, dict) and attributes:
            payload["attributes"] = _redact(attributes)
        if record.exc_info:
            payload["exception"] = self.formatException(record.exc_info)
        if record.stack_info:
            payload["stack"] = self.formatStack(record.stack_info)
        return json.dumps(payload, default=str, ensure_ascii=False)


def _is_otel_handler(handler: logging.Handler) -> bool:
    return type(handler).__module__.startswith("opentelemetry.")


# Original (basicConfig) formatters, so switching LOG_FORMAT back to text restores
# the console exactly as it was — telemetry must never rewrite console output.
_console_formatters: weakref.WeakKeyDictionary = weakref.WeakKeyDictionary()


def configure_console_logging() -> str:
    """Apply LOG_FORMAT to the console handlers; returns the format in force.

    Independent of OTLP export: JSON console output is useful on its own for
    production log shippers. ``text`` restores the exact formatter that
    ``logging.basicConfig`` installed, so the default console output is
    byte-identical to a build without telemetry.
    """
    fmt = os.getenv("LOG_FORMAT", "text").strip().lower() or "text"
    if fmt not in ("text", "json"):
        _logger.warning("Unknown LOG_FORMAT=%r; using text", fmt)
        fmt = "text"
    for handler in logging.getLogger().handlers:
        if _is_otel_handler(handler):
            continue
        if not isinstance(handler, logging.StreamHandler):
            continue
        if handler not in _console_formatters:
            _console_formatters[handler] = handler.formatter
        handler.setFormatter(JsonLogFormatter() if fmt == "json" else _console_formatters[handler])
    return fmt


# ------------------------------------------------------- exporters + resource


def _exporter_kwargs() -> dict[str, Any]:
    """Common OTLP/gRPC exporter options derived from the environment."""
    endpoint = _endpoint()
    kwargs: dict[str, Any] = {
        # "http://host:4327" -> "host:4327"; the scheme is notation, not REST.
        "endpoint": endpoint.split("://", 1)[-1],
        "insecure": _insecure(),
    }
    headers = os.getenv("OTEL_EXPORTER_OTLP_HEADERS", "").strip()
    if headers:
        parsed = [
            (key.strip(), value.strip())
            for key, sep, value in (item.partition("=") for item in headers.split(","))
            if sep and key.strip()
        ]
        if parsed:
            kwargs["headers"] = tuple(parsed)
    return kwargs


# These three are the test seam: every exporter is built here, so tests inject
# in-memory exporters instead of dialling a collector.
def _make_span_exporter():
    from opentelemetry.exporter.otlp.proto.grpc.trace_exporter import OTLPSpanExporter

    return OTLPSpanExporter(**_exporter_kwargs())


def _make_metric_exporter():
    from opentelemetry.exporter.otlp.proto.grpc.metric_exporter import OTLPMetricExporter

    return OTLPMetricExporter(**_exporter_kwargs())


def _make_log_exporter():
    from opentelemetry.exporter.otlp.proto.grpc._log_exporter import OTLPLogExporter

    return OTLPLogExporter(**_exporter_kwargs())


def _parse_resource_attributes(raw: str) -> dict[str, str]:
    attributes: dict[str, str] = {}
    for item in raw.split(","):
        key, sep, value = item.partition("=")
        if sep and key.strip():
            attributes[key.strip()] = value.strip()
    return attributes


def _resource():
    """service.name/version + deployment.environment so SigNoz can filter."""
    from opentelemetry.sdk.resources import Resource

    attributes: dict[str, str] = {"service.name": _service_name()}
    version = os.getenv("OTEL_SERVICE_VERSION", "").strip() or _app_setting("APP_VERSION")
    environment = os.getenv("OTEL_DEPLOYMENT_ENVIRONMENT", "").strip() or _app_setting("APP_ENV")
    if version:
        attributes["service.version"] = version
    if environment:
        attributes["deployment.environment"] = environment
    attributes.update(_parse_resource_attributes(os.getenv("OTEL_RESOURCE_ATTRIBUTES", "")))
    return Resource.create(attributes)


def _build_sampler():
    from opentelemetry.sdk.trace.sampling import (
        ALWAYS_OFF,
        ALWAYS_ON,
        ParentBased,
        TraceIdRatioBased,
    )

    ratio = min(_env_float("OTEL_TRACES_SAMPLER_ARG", 1.0, minimum=0.0), 1.0)
    factories = {
        "always_on": lambda: ALWAYS_ON,
        "always_off": lambda: ALWAYS_OFF,
        "traceidratio": lambda: TraceIdRatioBased(ratio),
        "parentbased_always_on": lambda: ParentBased(ALWAYS_ON),
        "parentbased_always_off": lambda: ParentBased(ALWAYS_OFF),
        "parentbased_traceidratio": lambda: ParentBased(TraceIdRatioBased(ratio)),
    }
    name = os.getenv("OTEL_TRACES_SAMPLER", "").strip().lower() or "parentbased_always_on"
    if name not in factories:
        _logger.warning(
            "Unknown OTEL_TRACES_SAMPLER=%r; expected one of %s. Using parentbased_always_on.",
            name,
            ", ".join(_SAMPLER_NAMES),
        )
        name = "parentbased_always_on"
    return factories[name]()


def _setup_tracing(resource):
    from opentelemetry import trace
    from opentelemetry.sdk.trace import TracerProvider
    from opentelemetry.sdk.trace.export import BatchSpanProcessor

    provider = TracerProvider(resource=resource, sampler=_build_sampler())
    provider.add_span_processor(BatchSpanProcessor(_make_span_exporter()))
    trace.set_tracer_provider(provider)
    return provider


def _setup_metrics(resource):
    from opentelemetry import metrics
    from opentelemetry.sdk.metrics import MeterProvider
    from opentelemetry.sdk.metrics.export import PeriodicExportingMetricReader

    interval_ms = _env_int("OTEL_METRICS_EXPORT_INTERVAL_MS", 60000, minimum=1000)
    reader = PeriodicExportingMetricReader(_make_metric_exporter(), export_interval_millis=interval_ms)
    provider = MeterProvider(resource=resource, metric_readers=[reader])
    metrics.set_meter_provider(provider)
    return provider


def _setup_logs(resource):
    from opentelemetry import _logs as logs_api
    from opentelemetry.sdk._logs import LoggerProvider
    from opentelemetry.sdk._logs.export import BatchLogRecordProcessor

    provider = LoggerProvider(resource=resource)
    provider.add_log_record_processor(BatchLogRecordProcessor(_make_log_exporter()))
    logs_api.set_logger_provider(provider)
    return provider


# ------------------------------------------------------------- instrumentation

# Header names never captured onto spans (session cookies / API keys).
_SANITIZED_HEADERS = ("authorization", "cookie", "set-cookie", "x-api-key", "proxy-authorization")


# Instrumentor factories: the seam tests replace to avoid touching real libraries.
def _make_logging_instrumentor():
    from opentelemetry.instrumentation.logging import LoggingInstrumentor

    return LoggingInstrumentor()


def _make_sqlalchemy_instrumentor():
    from opentelemetry.instrumentation.sqlalchemy import SQLAlchemyInstrumentor

    return SQLAlchemyInstrumentor()


def _make_httpx_instrumentor():
    from opentelemetry.instrumentation.httpx import HTTPXClientInstrumentor

    return HTTPXClientInstrumentor()


def _make_fastapi_instrumentor():
    from opentelemetry.instrumentation.fastapi import FastAPIInstrumentor

    return FastAPIInstrumentor()


def _instrument_libraries() -> None:
    """Attach the stdlib-logging bridge, DB spans and outbound-HTTP spans.

    Every step is independently guarded: a missing optional dependency turns one
    signal off and logs why, instead of aborting startup.
    """
    if _logger_provider is not None:
        _instrument_logging_bridge()
    if _env_bool("OTEL_INSTRUMENT_SQLALCHEMY", True):
        _instrument_sqlalchemy()
    if _env_bool("OTEL_INSTRUMENT_HTTPX", True):
        _instrument_httpx()


def _instrument_logging_bridge() -> None:
    """Wire stdlib logging records into *our* logger provider."""
    global _log_handler

    instrumentor = _make_logging_instrumentor()
    # inject_trace_context adds otelTraceID/otelSpanID/otelServiceName to every
    # record; log_handler_level is the export floor (console keeps its level).
    instrumentor.instrument(
        inject_trace_context=True,
        log_handler_level=_log_min_level(),
        log_code_attributes=True,
    )
    _instrumentors["logging"] = instrumentor

    handler = getattr(type(instrumentor), "_logging_handler", None)
    _log_handler = handler
    if handler is None:
        return
    if getattr(handler, "_logger_provider", None) is not _logger_provider:
        # The instrumentation resolves the process-global logger provider,
        # which can be stale when a second setup happens in one process
        # (tests, reloads). The bridge must always feed *our* provider.
        handler._logger_provider = _logger_provider  # type: ignore[attr-defined]
    excluded = _excluded_loggers()
    if excluded:
        handler.addFilter(ExcludeLoggersFilter(excluded))
    if _env_bool("OTEL_LOG_REDACT_SENSITIVE", True):
        handler.addFilter(SensitiveAttributeFilter())


def _instrument_sqlalchemy() -> None:
    """Attach DB spans. A failure here only turns DB spans off."""
    try:
        from app.database import engine

        instrumentor = _make_sqlalchemy_instrumentor()
        instrumentor.instrument(engine=engine)
        _instrumentors["sqlalchemy"] = instrumentor
    except Exception:
        _logger.warning("SQLAlchemy instrumentation unavailable; DB spans are off", exc_info=True)


def _instrument_httpx() -> None:
    """Attach outbound-HTTP spans. A failure here only turns those spans off."""
    try:
        instrumentor = _make_httpx_instrumentor()
        instrumentor.instrument()
        _instrumentors["httpx"] = instrumentor
    except Exception:
        _logger.warning("httpx instrumentation unavailable; outbound HTTP spans are off", exc_info=True)


# ------------------------------------------------------------------ public API


def setup_telemetry() -> bool:
    """Wire OTLP traces/metrics/logs plus instrumentation. Idempotent.

    Returns True when export is active; False when the endpoint is blank (the
    default for local dev, CI and the test suite) or setup degraded.
    """
    global _enabled, _tracer_provider, _meter_provider, _logger_provider

    configure_console_logging()
    with _lock:
        if _enabled:
            return True
        if not _endpoint():
            return False
        if not (_traces_enabled() or _metrics_enabled() or _logs_enabled()):
            _logger.warning(
                "OTEL_EXPORTER_OTLP_ENDPOINT is set but every signal is disabled "
                "(OTEL_TRACES_ENABLED/OTEL_METRICS_ENABLED/OTEL_LOGS_ENABLED); console only"
            )
            return False
        try:
            resource = _resource()
            if _traces_enabled():
                _tracer_provider = _setup_tracing(resource)
            if _metrics_enabled():
                _meter_provider = _setup_metrics(resource)
            if _logs_enabled():
                _logger_provider = _setup_logs(resource)
            _instrument_libraries()
        except Exception:
            # Observability must never take the application down with it.
            _logger.exception("Telemetry setup failed; continuing with console logging only")
            _enabled = False
            return False
        _enabled = True
        _logger.info(
            "OTLP export enabled -> %s (service.name=%s, traces=%s, metrics=%s, logs=%s)",
            _endpoint(),
            _service_name(),
            _traces_enabled(),
            _metrics_enabled(),
            _logs_enabled(),
        )
        return True


def instrument_app(app) -> bool:
    """Attach server spans (+ HTTP metrics) to a FastAPI app. Idempotent."""
    global _app_instrumented

    if not _enabled or _app_instrumented:
        return False
    if _tracer_provider is None and _meter_provider is None:
        return False
    _make_fastapi_instrumentor().instrument_app(
        app,
        tracer_provider=_tracer_provider,
        meter_provider=_meter_provider,
        excluded_urls=_excluded_urls(),
        http_capture_headers_sanitize_fields=list(_SANITIZED_HEADERS),
    )
    _app_instrumented = True
    return True


def shutdown_telemetry() -> None:
    """Flush queued telemetry and detach instrumentation. Safe to call repeatedly."""
    global _enabled, _app_instrumented, _tracer_provider, _meter_provider
    global _logger_provider, _log_handler

    with _lock:
        # tuple() snapshots the mapping: uninstrument() may mutate _instrumentors.
        for name, instrumentor in tuple(_instrumentors.items()):
            try:
                instrumentor.uninstrument()
            except Exception:  # pragma: no cover - defensive
                _logger.debug("Could not uninstrument %s", name, exc_info=True)
        _instrumentors.clear()

        handler, _log_handler = _log_handler, None
        if handler is not None:
            logging.getLogger().removeHandler(handler)

        for provider in (_logger_provider, _meter_provider, _tracer_provider):
            if provider is None:
                continue
            try:
                provider.force_flush()
                provider.shutdown()
            except Exception:  # pragma: no cover - defensive
                _logger.debug("Telemetry provider shutdown failed", exc_info=True)

        _logger_provider = _meter_provider = _tracer_provider = None
        _enabled = False
        _app_instrumented = False


def get_tracer(name: str = DEFAULT_SERVICE_NAME):
    """Tracer for manual spans; a no-op tracer when export is off."""
    from opentelemetry import trace

    return trace.get_tracer(name)
