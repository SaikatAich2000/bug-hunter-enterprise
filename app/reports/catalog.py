"""Report catalog: the list of report types the engine knows how to run.

Kept separate from engine.py so the catalog can be served to the frontend
(/api/reports/types) without dragging in SQLAlchemy or the runner.
"""
from __future__ import annotations

# Ordering here is the order the dropdown / Sleuth lists them.
REPORT_TYPES: list[dict] = [
    {
        "key": "item_detail",
        "label": "Item Detail Export",
        "icon": "📋",
        "description": (
            "Full list of bugs / requirements / tasks matching your "
            "filters — every column, like running a SQL query. Best for "
            "sharing raw data with anyone who asks for one"
        ),
        "result_shape": "detail",
        "default_window": "all_time",
    },
    {
        "key": "throughput",
        "label": "Resolution Throughput",
        "icon": "✅",
        "description": (
            "Per-user count of items moved into a resolved state in the "
            "time window. Bug → Resolved/Closed; Requirement → "
            "Implemented; Task → Done. Answers \"who solved how many last "
            "week\""
        ),
        "result_shape": "aggregate",
        "default_window": "last_7_days",
    },
    {
        "key": "pending_snapshot",
        "label": "Pending Items Snapshot",
        "icon": "⏳",
        "description": (
            "Detailed list of items still open right now (New / In "
            "Progress / Reopened / In Review / Approved / Blocked). "
            "Answers \"what's still pending\""
        ),
        "result_shape": "detail",
        "default_window": "all_time",
    },
    {
        "key": "status_distribution",
        "label": "Status Distribution",
        "icon": "📊",
        "description": "Counts grouped by status. Quick health check.",
        "result_shape": "aggregate",
        "default_window": "last_30_days",
    },
    {
        "key": "priority_distribution",
        "label": "Priority Distribution",
        "icon": "🚦",
        "description": (
            "Counts grouped by priority. How much critical work is in "
            "flight?"
        ),
        "result_shape": "aggregate",
        "default_window": "last_30_days",
    },
    {
        "key": "project_breakdown",
        "label": "Project Breakdown",
        "icon": "📁",
        "description": (
            "Per-project counts of created / still-open / resolved. "
            "Which projects are dominating the queue?"
        ),
        "result_shape": "aggregate",
        "default_window": "last_30_days",
    },
    {
        "key": "aging",
        "label": "Aging Report",
        "icon": "⏰",
        "description": (
            "Items still open, sorted oldest first. Includes the days-"
            "open count and an age bucket (0-7 / 8-30 / 31-60 / 61-90 / "
            "90+)"
        ),
        "result_shape": "detail",
        "default_window": "all_time",
    },
    {
        "key": "timeline",
        "label": "Created vs Resolved Timeline",
        "icon": "📈",
        "description": (
            "Per-day created and resolved counts over the time window. "
            "Are we keeping up with incoming work?"
        ),
        "result_shape": "aggregate",
        "default_window": "last_30_days",
    },
    {
        "key": "time_to_resolution",
        "label": "Time to Resolution",
        "icon": "⌛",
        "description": (
            "Per-item time from creation to resolution, with summary "
            "average / median / p95 across all resolved items in the "
            "window"
        ),
        "result_shape": "aggregate",
        "default_window": "last_30_days",
    },
]


REPORT_CATALOG: dict[str, dict] = {r["key"]: r for r in REPORT_TYPES}


def get_report_meta(key: str) -> dict | None:
    """Return the catalog entry for a report key, or None if unknown."""
    return REPORT_CATALOG.get(key)
