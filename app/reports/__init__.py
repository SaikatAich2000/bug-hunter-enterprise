"""Reporting engine: catalog (types), engine (Filters + run_report), xlsx (workbook writer).

Shared by the REST API and the Sleuth chatbot so both return the same numbers.
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
