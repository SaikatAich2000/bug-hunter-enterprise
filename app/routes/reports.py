"""Reports REST router.

Three endpoints, all manager-or-admin only:

  GET  /api/reports/types         — catalog of report types + filter schema
                                    for the frontend dropdown.
  POST /api/reports/run           — run a report and return JSON for the
                                    on-screen table.
  POST /api/reports/export.xlsx   — run a report and stream the workbook
                                    as a download.

A regular user hitting any of these gets a 403; the sidebar entry is also
hidden for them via data-needs-role="manager", so this is defense in
depth, not the primary boundary.
"""
from __future__ import annotations

import logging
import re
from datetime import datetime, timezone
from typing import Any, Optional

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field, field_validator
from sqlalchemy.orm import Session

from app.auth import require_manager_or_admin
from app.database import get_db
from app.models import User
from app.reports import (
    Filters,
    REPORT_CATALOG,
    REPORT_TYPES,
    UnknownReportError,
    build_workbook_bytes,
    run_report,
)
from app.reports.xlsx import XlsxBuildError
from app.schemas import (
    ALLOWED_ENVIRONMENTS,
    ALLOWED_ITEM_TYPES,
    ALLOWED_PRIORITIES,
    ALLOWED_STATUSES,
)
from app.reports.engine import (
    OPEN_STATUSES_BY_TYPE,
    RESOLVED_STATUSES_BY_TYPE,
)

logger = logging.getLogger("bug_hunter.reports")

router = APIRouter(prefix="/api/reports", tags=["reports"])


# ---------------------------------------------------------------------------
# I/O models
# ---------------------------------------------------------------------------
class FilterIn(BaseModel):
    """Inbound filter blob. Every field is optional; missing → no filter."""
    date_from: Optional[str] = None
    date_to: Optional[str] = None
    item_types: list[str] = Field(default_factory=list)
    statuses: list[str] = Field(default_factory=list)
    priorities: list[str] = Field(default_factory=list)
    environments: list[str] = Field(default_factory=list)
    project_ids: list[int] = Field(default_factory=list)
    assignee_ids: list[int] = Field(default_factory=list)
    reporter_ids: list[int] = Field(default_factory=list)
    event_id: Optional[int] = None
    include_not_a_bug: bool = False
    text_search: Optional[str] = Field(default=None, max_length=400)
    label: Optional[str] = Field(default=None, max_length=120)

    @field_validator("item_types")
    @classmethod
    def _valid_item_types(cls, v: list[str]) -> list[str]:
        return [t for t in v if t in ALLOWED_ITEM_TYPES]

    @field_validator("statuses")
    @classmethod
    def _valid_statuses(cls, v: list[str]) -> list[str]:
        return [s for s in v if s in ALLOWED_STATUSES]

    @field_validator("priorities")
    @classmethod
    def _valid_priorities(cls, v: list[str]) -> list[str]:
        return [p for p in v if p in ALLOWED_PRIORITIES]

    @field_validator("environments")
    @classmethod
    def _valid_environments(cls, v: list[str]) -> list[str]:
        return [e for e in v if e in ALLOWED_ENVIRONMENTS]


class ReportRunIn(BaseModel):
    report_key: str = Field(min_length=1, max_length=60)
    filters: FilterIn = Field(default_factory=FilterIn)


# ---------------------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------------------
@router.get("/types")
def list_report_types(
    _user: User = Depends(require_manager_or_admin),
) -> dict[str, Any]:
    """Catalog + filter vocab so the frontend can build the picker UI."""
    return {
        "types": REPORT_TYPES,
        "vocab": {
            "item_types": ALLOWED_ITEM_TYPES,
            "statuses": ALLOWED_STATUSES,
            "priorities": ALLOWED_PRIORITIES,
            "environments": ALLOWED_ENVIRONMENTS,
            "open_statuses_by_type": OPEN_STATUSES_BY_TYPE,
            "resolved_statuses_by_type": RESOLVED_STATUSES_BY_TYPE,
        },
    }


def _build_filters(payload: ReportRunIn) -> Filters:
    return Filters.from_dict(payload.filters.model_dump())


def _run_or_400(payload: ReportRunIn, db: Session, actor: User):
    if payload.report_key not in REPORT_CATALOG:
        raise HTTPException(
            status_code=400,
            detail=(
                f"Unknown report '{payload.report_key}'. "
                f"Valid: {', '.join(sorted(REPORT_CATALOG.keys()))}"
            ),
        )
    filters = _build_filters(payload)
    try:
        # Enterprise multi-tenancy: ALWAYS pass actor.org_id so the engine
        # can't accidentally return cross-org rows.
        return run_report(payload.report_key, filters, db, org_id=actor.org_id), filters
    except UnknownReportError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.post("/run")
def run(
    payload: ReportRunIn,
    db: Session = Depends(get_db),
    actor: User = Depends(require_manager_or_admin),
) -> dict[str, Any]:
    """Run a report and return JSON. For driving the on-screen table.

    Inline rendering is capped at 1000 rows to keep payloads sensible —
    the XLSX export endpoint has no cap (see below).
    """
    result, _filters = _run_or_400(payload, db, actor)
    api = result.to_api()
    rendered_cap = 1000
    if len(api["rows"]) > rendered_cap:
        api["rows"] = api["rows"][:rendered_cap]
        api["truncated"] = True
        api["truncated_cap"] = rendered_cap
    else:
        api["truncated"] = False
    return api


_FILENAME_SAFE_RE = re.compile(r"[^A-Za-z0-9_\-]+")


def _safe_filename(report_key: str, label: str) -> str:
    base = report_key
    if label:
        suffix = _FILENAME_SAFE_RE.sub("_", label.strip()).strip("_")[:40]
        if suffix:
            base = f"{base}_{suffix}"
    stamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
    return f"bug-hunter-report-{base}-{stamp}.xlsx"


@router.post("/export.xlsx")
def export_xlsx(
    payload: ReportRunIn,
    db: Session = Depends(get_db),
    actor: User = Depends(require_manager_or_admin),
) -> StreamingResponse:
    """Run the report and stream the resulting workbook.

    NO row cap here — Reports exports are an audit / compliance surface,
    so the caller decides how big a slice they want. The route layer
    bounds total cost via request size and timeouts; very-large reports
    will time out, which is the right pressure relief.
    """
    result, filters = _run_or_400(payload, db, actor)
    try:
        payload_bytes = build_workbook_bytes(result)
    except XlsxBuildError as exc:
        logger.exception("XLSX build failed: %s", exc)
        raise HTTPException(status_code=500, detail=str(exc)) from exc
    filename = _safe_filename(payload.report_key, filters.label)
    safe_filename = filename.replace('"', "_").replace("\r", "").replace("\n", "")
    return StreamingResponse(
        iter([payload_bytes]),
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        headers={
            "Content-Disposition": f'attachment; filename="{safe_filename}"',
            "Content-Length": str(len(payload_bytes)),
            "Cache-Control": "private, no-store, max-age=0",
        },
    )


__all__ = ["router"]
