"""Bug Hunter reporting engine.

Single source of truth for every report the system can produce. The same
engine powers:

  - The REST API at /api/reports/*  (driven by the Reports sidebar view).
  - The Sleuth chatbot's "report" intent (natural-language → same reports).

Why one engine in two callers? So a manager who clicks "Throughput,
last 7 days" in the UI gets exactly the same numbers as a manager who
asks Sleuth "who resolved how many bugs last week". No drift, one set
of tests.

What lives here:
  catalog.py  — the registry of report types + their default filters.
  engine.py   — Filters dataclass + run_report() dispatcher + every
                per-report query implementation.
  xlsx.py     — multi-sheet workbook writer shared by both callers.
"""
from __future__ import annotations

from app.reports.catalog import REPORT_CATALOG, REPORT_TYPES, get_report_meta
from app.reports.engine import (
    Filters,
    ReportColumn,
    ReportResult,
    UnknownReportError,
    run_report,
)
from app.reports.xlsx import build_workbook_bytes

__all__ = [
    "Filters",
    "ReportColumn",
    "ReportResult",
    "REPORT_CATALOG",
    "REPORT_TYPES",
    "UnknownReportError",
    "build_workbook_bytes",
    "get_report_meta",
    "run_report",
]
