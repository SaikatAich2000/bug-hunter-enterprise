"""Telemetry wiring (app/telemetry.py): env-driven, off by default, testable seams.

Covers signal toggles, sampling, resource attributes, exporter options, console
formatting, redaction, export filters, trace/log correlation, FastAPI
instrumentation, shutdown flushing and the audit bridge.
"""

from __future__ import annotations

import io
import json
import logging
import os
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
# A syntactically valid endpoint used only as a *string*: every test below that
# sets OTEL_EXPORTER_OTLP_ENDPOINT also swaps the exporter factories for
# in-memory fakes, and the one genuine gRPC test spins up its own loopback
# server — so no test ever dials this address. Kept deliberately non-routable
# (TEST-NET-1, RFC 5737) so a missing fake fails fast with connection-refused
# instead of hanging behind a VPN/firewall the way the old 10.1.151.228:4327
# value could when CI has no route to it.
ENDPOINT = "http://192.0.2.1:4317"


class _FakeExporter:
    """In-memory exporter: records what the SDK would have sent to the collector."""

    def __init__(self) -> None:
        self.records: list = []  # spans / log records
        self.metrics: list = []  # MetricsData containers (not iterable)
        self.shutdown_called = False
        # PeriodicExportingMetricReader defers its configuration to the exporter
        # (empty mapping == "use the SDK default aggregation/temporality").
        self._preferred_temporality: dict = {}
        self._preferred_aggregation: dict = {}

    def export(self, batch, **kwargs) -> int:
        if isinstance(batch, (list, tuple)):
            self.records.extend(batch)
        else:  # metric readers hand over a single MetricsData object
            self.metrics.append(batch)
        return 0  # SUCCESS

    def metric_names(self) -> set[str]:
        return {
            metric.name
            for data in self.metrics
            for resource_metric in data.resource_metrics
            for scope in resource_metric.scope_metrics
            for metric in scope.metrics
        }

    def force_flush(self, timeout_millis: int = 30000) -> bool:
        return True

    def shutdown(self, timeout_millis: int = 30000, **kwargs) -> None:
        # The metric reader calls shutdown(timeout=...); span/log processors call it bare.
        self.shutdown_called = True


class _FakeInstrumentor:
    """Records instrument()/uninstrument()/instrument_app() calls."""

    def __init__(self) -> None:
        self.instrument_calls: list[dict] = []
        self.uninstrumented = 0
        self.app_calls: list[tuple] = []

    def instrument(self, **kwargs):
        self.instrument_calls.append(kwargs)
        return None

    def uninstrument(self, **kwargs):
        self.uninstrumented += 1

    def instrument_app(self, app, **kwargs):
        self.app_calls.append((app, kwargs))


class _FakeLoggingInstrumentor(_FakeInstrumentor):
    """Mirrors LoggingInstrumentor's class-level handler attribute."""

    _logging_handler = None


@pytest.fixture
def telemetry(monkeypatch):
    """Isolated app.telemetry; console handlers and record factory restored after."""
    from app import telemetry as t

    root = logging.getLogger()
    saved_handlers = list(root.handlers)
    saved_level = root.level
    saved_factory = logging.getLogRecordFactory()
    yield t
    t.shutdown_telemetry()
    root.handlers[:] = saved_handlers
    root.setLevel(saved_level)
    logging.setLogRecordFactory(saved_factory)


@pytest.fixture
def fake_signals(telemetry, monkeypatch):
    """Enable export with in-memory exporters and fake library instrumentors."""
    logging_handler = logging.NullHandler()
    _FakeLoggingInstrumentor._logging_handler = logging_handler
    signals = {
        "spans": _FakeExporter(),
        "metrics": _FakeExporter(),
        "logs": _FakeExporter(),
        "log_handler": logging_handler,
        "instrumentors": {
            "logging": _FakeLoggingInstrumentor(),
            "sqlalchemy": _FakeInstrumentor(),
            "httpx": _FakeInstrumentor(),
            "fastapi": _FakeInstrumentor(),
        },
    }
    monkeypatch.setenv("OTEL_EXPORTER_OTLP_ENDPOINT", ENDPOINT)
    monkeypatch.setattr(telemetry, "_make_span_exporter", lambda: signals["spans"])
    monkeypatch.setattr(telemetry, "_make_metric_exporter", lambda: signals["metrics"])
    monkeypatch.setattr(telemetry, "_make_log_exporter", lambda: signals["logs"])
    monkeypatch.setattr(telemetry, "_make_logging_instrumentor", lambda: signals["instrumentors"]["logging"])
    monkeypatch.setattr(
        telemetry, "_make_sqlalchemy_instrumentor", lambda: signals["instrumentors"]["sqlalchemy"]
    )
    monkeypatch.setattr(telemetry, "_make_httpx_instrumentor", lambda: signals["instrumentors"]["httpx"])
    monkeypatch.setattr(telemetry, "_make_fastapi_instrumentor", lambda: signals["instrumentors"]["fastapi"])
    return signals


@pytest.fixture
def real_log_bridge(telemetry, monkeypatch):
    """Real logging bridge (no fake logging instrumentor) over in-memory exporters.

    Exercises the actual contrib handler, record factory and trace-context
    injection instead of a stand-in.
    """
    exports = {"logs": _FakeExporter(), "spans": _FakeExporter()}
    monkeypatch.setenv("OTEL_EXPORTER_OTLP_ENDPOINT", ENDPOINT)
    monkeypatch.setattr(telemetry, "_make_log_exporter", lambda: exports["logs"])
    monkeypatch.setattr(telemetry, "_make_span_exporter", lambda: exports["spans"])
    monkeypatch.setattr(telemetry, "_make_metric_exporter", lambda: _FakeExporter())
    monkeypatch.setattr(telemetry, "_make_sqlalchemy_instrumentor", lambda: _FakeInstrumentor())
    monkeypatch.setattr(telemetry, "_make_httpx_instrumentor", lambda: _FakeInstrumentor())
    assert telemetry.setup_telemetry() is True
    return exports


# ------------------------------------------------------------ default behaviour


def test_suite_env_disables_export(telemetry):
    """conftest hard-sets the endpoint empty: the suite must never dial out."""
    assert os.environ.get("OTEL_EXPORTER_OTLP_ENDPOINT") == ""
    assert telemetry.setup_telemetry() is False


def test_disabled_without_endpoint_keeps_console_untouched(telemetry, monkeypatch):
    monkeypatch.delenv("OTEL_EXPORTER_OTLP_ENDPOINT", raising=False)
    before = list(logging.getLogger().handlers)
    assert telemetry.setup_telemetry() is False
    assert list(logging.getLogger().handlers) == before


def test_all_signals_disabled_stays_console_only(telemetry, monkeypatch, caplog):
    monkeypatch.setenv("OTEL_EXPORTER_OTLP_ENDPOINT", ENDPOINT)
    for name in ("OTEL_TRACES_ENABLED", "OTEL_METRICS_ENABLED", "OTEL_LOGS_ENABLED"):
        monkeypatch.setenv(name, "false")
    with caplog.at_level(logging.WARNING, logger="bug_hunter.telemetry"):
        assert telemetry.setup_telemetry() is False
    assert any("every signal is disabled" in r.getMessage() for r in caplog.records)
    assert telemetry._tracer_provider is None


# ------------------------------------------------------------------ happy path


def test_setup_enables_all_signals_and_is_idempotent(telemetry, fake_signals):
    assert telemetry.setup_telemetry() is True
    assert telemetry._tracer_provider is not None
    assert telemetry._meter_provider is not None
    assert telemetry._logger_provider is not None
    resource = telemetry._tracer_provider.resource
    assert resource.attributes["service.name"] == "bug-hunter"

    handlers = len(logging.getLogger().handlers)
    assert telemetry.setup_telemetry() is True  # idempotent: no stacked providers
    assert len(logging.getLogger().handlers) == handlers


def test_signal_toggles_skip_disabled_signals(telemetry, fake_signals, monkeypatch):
    monkeypatch.setenv("OTEL_TRACES_ENABLED", "false")
    monkeypatch.setenv("OTEL_METRICS_ENABLED", "false")
    assert telemetry.setup_telemetry() is True
    assert telemetry._tracer_provider is None
    assert telemetry._meter_provider is None
    assert telemetry._logger_provider is not None


def test_instrumentation_wiring(fake_signals, telemetry):
    assert telemetry.setup_telemetry() is True
    logging_call = fake_signals["instrumentors"]["logging"].instrument_calls[0]
    assert logging_call["inject_trace_context"] is True
    assert logging_call["log_handler_level"] == logging.INFO
    assert logging_call["log_code_attributes"] is True
    assert "engine" in fake_signals["instrumentors"]["sqlalchemy"].instrument_calls[0]
    assert fake_signals["instrumentors"]["httpx"].instrument_calls == [{}]


def test_instrumentation_toggles(telemetry, fake_signals, monkeypatch):
    monkeypatch.setenv("OTEL_INSTRUMENT_SQLALCHEMY", "false")
    monkeypatch.setenv("OTEL_INSTRUMENT_HTTPX", "false")
    assert telemetry.setup_telemetry() is True
    assert fake_signals["instrumentors"]["sqlalchemy"].instrument_calls == []
    assert fake_signals["instrumentors"]["httpx"].instrument_calls == []


def test_failing_instrumentor_warns_but_setup_succeeds(telemetry, fake_signals, monkeypatch, caplog):
    class _Boom:
        def instrument(self, **kwargs):
            raise RuntimeError("no engine")

    monkeypatch.setattr(telemetry, "_make_sqlalchemy_instrumentor", lambda: _Boom())
    with caplog.at_level(logging.WARNING, logger="bug_hunter.telemetry"):
        assert telemetry.setup_telemetry() is True
    assert any("SQLAlchemy instrumentation unavailable" in r.getMessage() for r in caplog.records)


def test_export_filters_attached_to_log_handler(telemetry, fake_signals):
    assert telemetry.setup_telemetry() is True
    filters = fake_signals["log_handler"].filters
    exclude = next(f for f in filters if isinstance(f, telemetry.ExcludeLoggersFilter))
    assert any(isinstance(f, telemetry.SensitiveAttributeFilter) for f in filters)

    access = logging.LogRecord("uvicorn.access", logging.INFO, __file__, 1, "GET /", None, None)
    child = logging.LogRecord("uvicorn.access.proxy", logging.INFO, __file__, 1, "x", None, None)
    audit = logging.LogRecord("bug_hunter.audit", logging.INFO, __file__, 1, "audit", None, None)
    assert exclude.filter(access) is False
    assert exclude.filter(child) is False
    assert exclude.filter(audit) is True


def test_redaction_filter_scrubs_sensitive_attributes(telemetry, fake_signals):
    record = logging.LogRecord("bug_hunter", logging.INFO, __file__, 1, "login", None, None)
    record.attributes = {
        "email": "user@example.com",
        "password": "hunter2",
        "nested": {"api_token": "t", "cookie": "session=1"},
        "list": [{"secret": "s"}],
    }
    telemetry.SensitiveAttributeFilter().filter(record)
    assert record.attributes == {
        "email": "user@example.com",
        "password": telemetry.REDACTED,
        "nested": {"api_token": telemetry.REDACTED, "cookie": telemetry.REDACTED},
        "list": [{"secret": telemetry.REDACTED}],
    }


def test_redaction_can_be_disabled(telemetry, fake_signals, monkeypatch):
    monkeypatch.setenv("OTEL_LOG_REDACT_SENSITIVE", "false")
    assert telemetry.setup_telemetry() is True
    filters = fake_signals["log_handler"].filters
    assert not any(isinstance(f, telemetry.SensitiveAttributeFilter) for f in filters)


def test_excluded_loggers_can_be_configured(telemetry, fake_signals, monkeypatch):
    monkeypatch.setenv("OTEL_LOG_EXCLUDE_LOGGERS", "sqlalchemy.engine, httpx")
    assert telemetry.setup_telemetry() is True
    exclude = next(
        f for f in fake_signals["log_handler"].filters if isinstance(f, telemetry.ExcludeLoggersFilter)
    )
    engine = logging.LogRecord("sqlalchemy.engine", logging.INFO, __file__, 1, "sql", None, None)
    httpx = logging.LogRecord("httpx", logging.INFO, __file__, 1, "http", None, None)
    other = logging.LogRecord("uvicorn.access", logging.INFO, __file__, 1, "GET /", None, None)
    assert exclude.filter(engine) is False
    assert exclude.filter(httpx) is False
    assert exclude.filter(other) is True


def test_import_error_degrades_to_console(telemetry, monkeypatch, caplog):
    monkeypatch.setenv("OTEL_EXPORTER_OTLP_ENDPOINT", ENDPOINT)
    monkeypatch.setattr(telemetry, "_make_span_exporter", lambda: _FakeExporter())
    monkeypatch.setattr(telemetry, "_make_metric_exporter", lambda: _FakeExporter())
    monkeypatch.setattr(telemetry, "_make_logging_instrumentor", lambda: _FakeLoggingInstrumentor())
    monkeypatch.setattr(telemetry, "_make_sqlalchemy_instrumentor", lambda: _FakeInstrumentor())
    monkeypatch.setattr(telemetry, "_make_httpx_instrumentor", lambda: _FakeInstrumentor())
    monkeypatch.setitem(sys.modules, "opentelemetry.sdk._logs", None)  # SDK import blows up
    with caplog.at_level(logging.ERROR, logger="bug_hunter.telemetry"):
        assert telemetry.setup_telemetry() is False
    assert any("Telemetry setup failed" in r.getMessage() for r in caplog.records)
    assert telemetry._logger_provider is None


def test_shutdown_flushes_and_detaches(telemetry, fake_signals):
    assert telemetry.setup_telemetry() is True
    handler = fake_signals["log_handler"]
    logging.getLogger().addHandler(handler)  # the fake instrumentor does not add it
    assert handler in logging.getLogger().handlers

    telemetry.shutdown_telemetry()
    assert handler not in logging.getLogger().handlers
    # Shutdown flushes: every batched exporter is drained and closed.
    assert fake_signals["logs"].shutdown_called is True
    assert fake_signals["metrics"].shutdown_called is True
    assert fake_signals["spans"].shutdown_called is True
    assert fake_signals["instrumentors"]["httpx"].uninstrumented == 1
    assert telemetry._logger_provider is None
    assert telemetry._tracer_provider is None
    assert telemetry._enabled is False

    telemetry.shutdown_telemetry()  # second call is a safe no-op


# ------------------------------------------- real logging bridge (no stand-ins)


def test_exported_records_carry_trace_context(real_log_bridge, telemetry):
    """Log records must link to their request trace (this is what SigNoz pivots on)."""
    tracer = telemetry._tracer_provider.get_tracer("test")
    probe = logging.getLogger("bug_hunter.test")
    probe.setLevel(logging.INFO)
    with tracer.start_as_current_span("unit-span") as span:
        probe.info("hello from a span")
        expected_trace_id = span.get_span_context().trace_id

    assert telemetry._logger_provider.force_flush(5000)
    records = [r.log_record for r in real_log_bridge["logs"].records]
    exported = next(r for r in records if str(r.body) == "hello from a span")
    assert exported.trace_id == expected_trace_id
    assert exported.severity_text == "INFO"
    # inject_trace_context also surfaces the hex ids as attributes.
    assert exported.attributes["otelTraceID"] == format(expected_trace_id, "032x")
    # log_code_attributes=True puts the call site on the record.
    assert exported.attributes["code.function.name"] == "test_exported_records_carry_trace_context"

    # Shutting the app down drains the exporter (last records are never lost).
    telemetry.shutdown_telemetry()
    assert real_log_bridge["logs"].shutdown_called is True
    assert real_log_bridge["spans"].shutdown_called is True


def test_access_logs_excluded_from_export(real_log_bridge, telemetry):
    access = logging.getLogger("uvicorn.access")
    access.setLevel(logging.INFO)
    probe = logging.getLogger("bug_hunter.test")
    probe.setLevel(logging.INFO)

    access.info("GET /api/health 200")
    probe.info("kept in signoz")
    assert telemetry._logger_provider.force_flush(5000)

    bodies = [str(r.log_record.body) for r in real_log_bridge["logs"].records]
    assert "kept in signoz" in bodies
    assert "GET /api/health 200" not in bodies


def test_min_level_keeps_debug_out_of_export(telemetry, monkeypatch):
    exports = {"logs": _FakeExporter()}
    monkeypatch.setenv("OTEL_EXPORTER_OTLP_ENDPOINT", ENDPOINT)
    monkeypatch.setenv("OTEL_LOG_MIN_LEVEL", "WARNING")
    monkeypatch.setattr(telemetry, "_make_log_exporter", lambda: exports["logs"])
    monkeypatch.setattr(telemetry, "_make_span_exporter", lambda: _FakeExporter())
    monkeypatch.setattr(telemetry, "_make_metric_exporter", lambda: _FakeExporter())
    monkeypatch.setattr(telemetry, "_make_sqlalchemy_instrumentor", lambda: _FakeInstrumentor())
    monkeypatch.setattr(telemetry, "_make_httpx_instrumentor", lambda: _FakeInstrumentor())
    assert telemetry.setup_telemetry() is True

    probe = logging.getLogger("bug_hunter.test")
    probe.setLevel(logging.DEBUG)
    probe.debug("too low")
    probe.info("also too low")
    probe.warning("exported")
    assert telemetry._logger_provider.force_flush(5000)

    bodies = [str(r.log_record.body) for r in exports["logs"].records]
    assert bodies == ["exported"]


# ----------------------------------------------------------------- console format


def test_json_console_format_and_reset(telemetry, monkeypatch):
    """LOG_FORMAT=json wins; LOG_FORMAT=text restores the basicConfig format exactly."""
    root = logging.getLogger()
    # Stand-in for the handler app/main.py installs via logging.basicConfig().
    handler = logging.StreamHandler(io.StringIO())
    handler.setFormatter(logging.Formatter("%(levelname)s:%(name)s:%(message)s"))
    root.addHandler(handler)
    try:
        monkeypatch.setenv("LOG_FORMAT", "json")
        assert telemetry.configure_console_logging() == "json"
        assert isinstance(handler.formatter, telemetry.JsonLogFormatter)

        record = logging.LogRecord(
            "bug_hunter.test", logging.WARNING, __file__, 7, "hello %s", ("world",), None
        )
        record.otelTraceID = "a" * 32
        record.otelSpanID = "b" * 16
        record.attributes = {"password": "p", "user": "u"}
        payload = json.loads(handler.formatter.format(record))
        assert payload["severity"] == "WARN"
        assert payload["severity_number"] == 13
        assert payload["message"] == "hello world"
        assert payload["logger"] == "bug_hunter.test"
        assert payload["trace_id"] == "a" * 32
        assert payload["span_id"] == "b" * 16
        assert payload["code"]["line"] == 7
        assert payload["attributes"] == {"password": telemetry.REDACTED, "user": "u"}
        assert payload["service"] == "bug-hunter"

        monkeypatch.setenv("LOG_FORMAT", "text")
        assert telemetry.configure_console_logging() == "text"
        # The console keeps the exact format it had before telemetry touched it.
        assert handler.formatter._fmt == "%(levelname)s:%(name)s:%(message)s"
    finally:
        root.removeHandler(handler)


def test_unknown_log_format_warns_and_falls_back(telemetry, monkeypatch, caplog):
    monkeypatch.setenv("LOG_FORMAT", "logfmt")
    with caplog.at_level(logging.WARNING, logger="bug_hunter.telemetry"):
        assert telemetry.configure_console_logging() == "text"
    assert any("Unknown LOG_FORMAT" in r.getMessage() for r in caplog.records)


# ------------------------------------------------- resource, sampler, exporters


def test_resource_attributes_and_overrides(telemetry, monkeypatch):
    monkeypatch.setenv("OTEL_SERVICE_NAME", "bug-hunter-prod")
    monkeypatch.setenv("OTEL_SERVICE_VERSION", "9.9")
    monkeypatch.setenv("OTEL_DEPLOYMENT_ENVIRONMENT", "production")
    monkeypatch.setenv("OTEL_RESOURCE_ATTRIBUTES", "host.region=eu-west,team=ops")
    attributes = telemetry._resource().attributes
    assert attributes["service.name"] == "bug-hunter-prod"
    assert attributes["service.version"] == "9.9"
    assert attributes["deployment.environment"] == "production"
    assert attributes["host.region"] == "eu-west"
    assert attributes["team"] == "ops"


def test_resource_falls_back_to_app_settings(telemetry, monkeypatch):
    monkeypatch.delenv("OTEL_SERVICE_VERSION", raising=False)
    monkeypatch.delenv("OTEL_DEPLOYMENT_ENVIRONMENT", raising=False)
    monkeypatch.setattr(
        telemetry,
        "_app_setting",
        lambda name: {"APP_VERSION": "3.1", "APP_ENV": "staging"}.get(name, ""),
    )
    attributes = telemetry._resource().attributes
    assert attributes["service.version"] == "3.1"
    assert attributes["deployment.environment"] == "staging"


@pytest.mark.parametrize(
    ("name", "expected"),
    [
        ("always_on", "StaticSampler"),
        ("always_off", "StaticSampler"),
        ("traceidratio", "TraceIdRatioBased"),
        ("parentbased_always_on", "ParentBased"),
        ("parentbased_always_off", "ParentBased"),
        ("parentbased_traceidratio", "ParentBased"),
    ],
)
def test_sampler_selection(telemetry, monkeypatch, name, expected):
    monkeypatch.setenv("OTEL_TRACES_SAMPLER", name)
    sampler = telemetry._build_sampler()
    assert type(sampler).__name__ == expected
    if expected == "StaticSampler":  # ALWAYS_ON/ALWAYS_OFF are static sampler instances
        assert sampler.get_description() in ("AlwaysOnSampler", "AlwaysOffSampler")


def test_sampler_default_and_invalid_fallback(telemetry, monkeypatch, caplog):
    monkeypatch.delenv("OTEL_TRACES_SAMPLER", raising=False)
    assert type(telemetry._build_sampler()).__name__ == "ParentBased"
    monkeypatch.setenv("OTEL_TRACES_SAMPLER", "nonsense")
    with caplog.at_level(logging.WARNING, logger="bug_hunter.telemetry"):
        assert type(telemetry._build_sampler()).__name__ == "ParentBased"
    assert any("Unknown OTEL_TRACES_SAMPLER" in r.getMessage() for r in caplog.records)


def test_sampler_ratio_is_clamped(telemetry, monkeypatch):
    monkeypatch.setenv("OTEL_TRACES_SAMPLER", "traceidratio")
    monkeypatch.setenv("OTEL_TRACES_SAMPLER_ARG", "5")
    assert type(telemetry._build_sampler()).__name__ == "TraceIdRatioBased"


def test_exporter_kwargs_endpoint_headers_and_tls(telemetry, monkeypatch):
    monkeypatch.setenv("OTEL_EXPORTER_OTLP_ENDPOINT", "http://192.0.2.1:4317")
    monkeypatch.setenv("OTEL_EXPORTER_OTLP_HEADERS", "signoz-access-token=abc, x-tenant=one")
    kwargs = telemetry._exporter_kwargs()
    assert kwargs["endpoint"] == "192.0.2.1:4317"
    assert kwargs["insecure"] is True
    assert kwargs["headers"] == (("signoz-access-token", "abc"), ("x-tenant", "one"))

    monkeypatch.setenv("OTEL_EXPORTER_OTLP_ENDPOINT", "https://collector.example.com:4317")
    monkeypatch.delenv("OTEL_EXPORTER_OTLP_HEADERS", raising=False)
    kwargs = telemetry._exporter_kwargs()
    assert kwargs["insecure"] is False
    assert "headers" not in kwargs

    monkeypatch.setenv("OTEL_EXPORTER_OTLP_INSECURE", "true")
    assert telemetry._exporter_kwargs()["insecure"] is True


def test_real_exporter_factories_construct_offline(telemetry, monkeypatch):
    """Constructing an OTLP exporter must not dial out (channels connect lazily)."""
    monkeypatch.setenv("OTEL_EXPORTER_OTLP_ENDPOINT", "http://127.0.0.1:4317")
    assert type(telemetry._make_span_exporter()).__name__ == "OTLPSpanExporter"
    assert type(telemetry._make_metric_exporter()).__name__ == "OTLPMetricExporter"
    assert type(telemetry._make_log_exporter()).__name__ == "OTLPLogExporter"


# ---------------------------------------------------------------- FastAPI app


def test_instrument_app_passes_providers_and_exclusions(telemetry, fake_signals):
    assert telemetry.setup_telemetry() is True
    app = object()
    assert telemetry.instrument_app(app) is True
    calls = fake_signals["instrumentors"]["fastapi"].app_calls
    assert calls[0][0] is app
    assert calls[0][1]["tracer_provider"] is telemetry._tracer_provider
    assert calls[0][1]["meter_provider"] is telemetry._meter_provider
    assert calls[0][1]["excluded_urls"] == "/api/health,/static"
    assert "authorization" in calls[0][1]["http_capture_headers_sanitize_fields"]
    assert telemetry.instrument_app(app) is False  # idempotent


def test_instrument_app_respects_excluded_urls_env(telemetry, fake_signals, monkeypatch):
    monkeypatch.setenv("OTEL_TRACES_EXCLUDED_URLS", "/api/health")
    assert telemetry.setup_telemetry() is True
    assert telemetry.instrument_app(object()) is True
    assert fake_signals["instrumentors"]["fastapi"].app_calls[0][1]["excluded_urls"] == "/api/health"


def test_instrument_app_is_noop_when_export_disabled(telemetry):
    assert telemetry.instrument_app(object()) is False


def test_otlp_grpc_transport_end_to_end(telemetry, monkeypatch):
    """Real gRPC + real protobuf: logs, spans and metrics must hit a collector.

    Runs an in-process OTLP/gRPC receiver and points the app at it, so this
    exercises the exact wire path used against SigNoz (no fakes, no mocks).
    """
    from concurrent import futures

    import grpc
    from opentelemetry.proto.collector.logs.v1 import logs_service_pb2, logs_service_pb2_grpc
    from opentelemetry.proto.collector.metrics.v1 import metrics_service_pb2, metrics_service_pb2_grpc
    from opentelemetry.proto.collector.trace.v1 import trace_service_pb2, trace_service_pb2_grpc

    received: dict[str, list] = {"logs": [], "traces": [], "metrics": []}

    class _Logs(logs_service_pb2_grpc.LogsServiceServicer):
        def Export(self, request, context):
            received["logs"].append(request)
            return logs_service_pb2.ExportLogsServiceResponse()

    class _Traces(trace_service_pb2_grpc.TraceServiceServicer):
        def Export(self, request, context):
            received["traces"].append(request)
            return trace_service_pb2.ExportTraceServiceResponse()

    class _Metrics(metrics_service_pb2_grpc.MetricsServiceServicer):
        def Export(self, request, context):
            received["metrics"].append(request)
            return metrics_service_pb2.ExportMetricsServiceResponse()

    server = grpc.server(futures.ThreadPoolExecutor(max_workers=4))
    logs_service_pb2_grpc.add_LogsServiceServicer_to_server(_Logs(), server)
    trace_service_pb2_grpc.add_TraceServiceServicer_to_server(_Traces(), server)
    metrics_service_pb2_grpc.add_MetricsServiceServicer_to_server(_Metrics(), server)
    port = server.add_insecure_port("127.0.0.1:0")
    server.start()
    try:
        monkeypatch.setenv("OTEL_EXPORTER_OTLP_ENDPOINT", f"http://127.0.0.1:{port}")
        monkeypatch.setattr(telemetry, "_make_sqlalchemy_instrumentor", lambda: _FakeInstrumentor())
        monkeypatch.setattr(telemetry, "_make_httpx_instrumentor", lambda: _FakeInstrumentor())
        assert telemetry.setup_telemetry() is True

        probe = logging.getLogger("bug_hunter.transport")
        probe.setLevel(logging.INFO)
        with telemetry._tracer_provider.get_tracer("transport").start_as_current_span("e2e-span"):
            probe.info("e2e log line")
        telemetry._meter_provider.get_meter("transport").create_counter("e2e.counter").add(1)

        assert telemetry._tracer_provider.force_flush(5000)
        assert telemetry._logger_provider.force_flush(5000)
        assert telemetry._meter_provider.force_flush(5000)

        log_bodies = [
            record.body.string_value  # OTLP AnyValue: our message bodies are strings
            for request in received["logs"]
            for resource_logs in request.resource_logs
            for scope_logs in resource_logs.scope_logs
            for record in scope_logs.log_records
        ]
        span_names = [
            span.name
            for request in received["traces"]
            for resource_spans in request.resource_spans
            for scope_spans in resource_spans.scope_spans
            for span in scope_spans.spans
        ]
        metric_names = [
            metric.name
            for request in received["metrics"]
            for resource_metrics in request.resource_metrics
            for scope_metrics in resource_metrics.scope_metrics
            for metric in scope_metrics.metrics
        ]
        assert "e2e log line" in log_bodies, log_bodies
        assert "e2e-span" in span_names, span_names
        assert "e2e.counter" in metric_names, metric_names
        # Resource identity travels with the payload (SigNoz groups by these).
        resource = received["logs"][0].resource_logs[0].resource
        attributes = {kv.key: kv.value.string_value for kv in resource.attributes}
        assert attributes["service.name"] == "bug-hunter"
    finally:
        # Shut the telemetry down first: the metric reader's background thread
        # must not retry against a receiver we have already stopped.
        telemetry.shutdown_telemetry()
        server.stop(0)


def test_real_fastapi_instrumentation_emits_request_spans(telemetry, monkeypatch):
    """End-to-end: an app instrumented before serving must emit server spans."""
    from fastapi import FastAPI
    from starlette.testclient import TestClient

    spans = _FakeExporter()
    metrics = _FakeExporter()
    monkeypatch.setenv("OTEL_EXPORTER_OTLP_ENDPOINT", ENDPOINT)
    monkeypatch.setattr(telemetry, "_make_span_exporter", lambda: spans)
    monkeypatch.setattr(telemetry, "_make_metric_exporter", lambda: metrics)
    monkeypatch.setattr(telemetry, "_make_log_exporter", lambda: _FakeExporter())
    monkeypatch.setattr(telemetry, "_make_sqlalchemy_instrumentor", lambda: _FakeInstrumentor())
    monkeypatch.setattr(telemetry, "_make_httpx_instrumentor", lambda: _FakeInstrumentor())

    app = FastAPI()

    @app.get("/ping")
    def ping():
        return {"ok": True}

    assert telemetry.setup_telemetry() is True
    # Same order as app/main.py: instrument right after app creation, before serving.
    assert telemetry.instrument_app(app) is True
    with TestClient(app) as client:
        assert client.get("/ping").status_code == 200
    assert telemetry._tracer_provider.force_flush(5000)
    assert telemetry._meter_provider.force_flush(5000)

    request_span = next((s for s in spans.records if s.name == "GET /ping"), None)
    assert request_span is not None, [s.name for s in spans.records]
    assert request_span.attributes["http.status_code"] == 200
    assert request_span.attributes["http.method"] == "GET"
    assert request_span.attributes["http.route"] == "/ping"
    assert "http.server.duration" in metrics.metric_names()


def test_get_tracer_is_safe_without_export(telemetry):
    tracer = telemetry.get_tracer("manual")
    with tracer.start_as_current_span("noop-span"):
        pass


# ------------------------------------------------------ wiring + audit trail


def test_main_wires_setup_before_app_and_instruments_it():
    """Guards the startup order: providers first, then the FastAPI app."""
    source = (ROOT / "app" / "main.py").read_text(encoding="utf-8")
    assert source.index("setup_telemetry()") < source.index("app = FastAPI(")
    assert source.index("instrument_app(app)") > source.index("app = FastAPI(")
    assert "shutdown_telemetry()" in source
    assert "setup_log_export" not in source  # renamed API is fully adopted


def test_audit_rows_mirrored_to_logs(admin_client, caplog):
    """Activity rows (the audit trail) must surface on the log stream for SigNoz."""
    caplog.set_level(logging.INFO, logger="bug_hunter.audit")
    res = admin_client.post(
        "/api/projects", json={"name": "Audit Bridge", "description": "", "color": "#c9764f"}
    )
    assert res.status_code == 201, res.text
    messages = [r.getMessage() for r in caplog.records if r.name == "bug_hunter.audit"]
    assert any(m.startswith("audit project_created project#") for m in messages), messages
