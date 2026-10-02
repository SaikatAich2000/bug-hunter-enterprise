"""Reports REST router: /types, /run, /export.xlsx — all manager-or-admin only.

The sidebar hiding is UI convenience; the role dependency is the access gate.
"""
from __future__ import annotations

import logging
import re
from datetime import datetime, timezone
from typing import Any, Optional

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import Response, StreamingResponse
from pydantic import BaseModel, Field, field_validator
from sqlalchemy.orm import Session

from app.access import accessible_project_ids
from app.api_docs import BAD_REQUEST_400, EXPORT_ERRORS, XLSX_FILE_200
from app.auth import require_manager_or_admin
from app.config import get_settings
from app.database import get_db
from app.models import User
from app.reports import (
    REPORT_CATALOG,
    REPORT_TYPES,
    Filters,
    UnknownReportError,
    build_workbook_bytes,
    run_report,
)
from app.reports.engine import (
    OPEN_STATUSES_BY_TYPE,
    RESOLVED_STATUSES_BY_TYPE,
)
from app.reports.xlsx import XlsxBuildError
from app.schemas import (
    ALLOWED_ENVIRONMENTS,
    ALLOWED_ITEM_TYPES,
    ALLOWED_PRIORITIES,
    ALLOWED_STATUSES,
)

logger = logging.getLogger("bug_hunter.reports")

router = APIRouter(prefix="/api/reports", tags=["reports"])


class FilterIn(BaseModel):
    """Inbound filter blob; omitted fields mean no filter."""
    date_from: Optional[str] = None
    date_to: Optional[str] = None
    item_types: list[str] = Field(default_factory=list)
    statuses: list[str] = Field(default_factory=list)
    priorities: list[str] = Field(default_factory=list)
    environments: list[str] = Field(default_factory=list)
    project_ids: list[int] = Field(default_factory=list, max_length=1000)
    assignee_ids: list[int] = Field(default_factory=list, max_length=1000)
    reporter_ids: list[int] = Field(default_factory=list, max_length=1000)
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


def _run_or_400(payload: ReportRunIn, db: Session, user: User):
    if payload.report_key not in REPORT_CATALOG:
        raise HTTPException(
            status_code=400,
            detail=(
                f"Unknown report '{payload.report_key}'. "
                f"Valid: {', '.join(sorted(REPORT_CATALOG.keys()))}"
            ),
        )
    filters = _build_filters(payload)
    # restrict_project_ids is route-set, never payload — a manager can't widen scope
    filters.restrict_project_ids = accessible_project_ids(db, user)
    try:
        return run_report(payload.report_key, filters, db), filters
    except UnknownReportError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.post("/run", responses=BAD_REQUEST_400)
def run(
    payload: ReportRunIn,
    db: Session = Depends(get_db),
    user: User = Depends(require_manager_or_admin),
) -> dict[str, Any]:
    """Run a report and return JSON rows, capped at 1000; XLSX export has full data."""
    result, _filters = _run_or_400(payload, db, user)
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


@router.post("/export.xlsx", response_class=Response, responses={**XLSX_FILE_200, **EXPORT_ERRORS})
def export_xlsx(
    payload: ReportRunIn,
    db: Session = Depends(get_db),
    user: User = Depends(require_manager_or_admin),
) -> StreamingResponse:
    """Run the report and stream the workbook; 413 past MAX_REPORT_ROWS (in-memory build)."""
    result, filters = _run_or_400(payload, db, user)
    # max() covers detail vs aggregate reports; truncated catches a capped scan
    # that would otherwise silently ship under-counted totals
    settings = get_settings()
    n_rows = max(result.total, len(result.detail_rows))
    if n_rows > settings.MAX_REPORT_ROWS or result.truncated:
        raise HTTPException(
            status_code=413,
            detail=(
                f"This export exceeds the {settings.MAX_REPORT_ROWS}-row limit. "
                "Narrow the date range or add filters and try again."
            ),
        )
    try:
        payload_bytes = build_workbook_bytes(result)
    except XlsxBuildError as exc:
        # log the real cause server-side; don't leak config state to the client
        logger.exception("XLSX build failed: %s", exc)
        raise HTTPException(
            status_code=500,
            detail="Could not build the report workbook. Please try again or contact an administrator.",
        ) from exc
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
