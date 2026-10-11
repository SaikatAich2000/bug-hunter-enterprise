"""Release readiness, the readiness-gated release action, and release/roadmap
timeline data. Reuses the `Version`/`WorkItemVersion` models from
taxonomy; adds no new schema.
"""
from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.agile.taxonomy import is_done_status
from app.models import Bug, BugLink, User, Version, WorkItemVersion
from app.util import utcnow


class ReleaseError(ValueError):
    pass


def _version_items(db: Session, version_id: int) -> list[Bug]:
    item_ids = [
        r.work_item_id for r in
        db.scalars(select(WorkItemVersion).where(
            WorkItemVersion.version_id == version_id, WorkItemVersion.relation_type == "fix",
        )).all()
    ]
    if not item_ids:
        return []
    return list(db.scalars(select(Bug).where(Bug.id.in_(item_ids))).all())


def release_progress(db: Session, version: Version) -> dict:
    items = _version_items(db, version.id)
    total_estimate = 0.0
    completed_estimate = 0.0
    completed_count = 0
    unestimated_count = 0
    for item in items:
        pts = float(item.story_points) if item.story_points is not None else None
        if pts is None:
            unestimated_count += 1
        else:
            total_estimate += pts
        if is_done_status(db, item):
            completed_count += 1
            if pts is not None:
                completed_estimate += pts
    return {
        "item_count": len(items),
        "completed_count": completed_count,
        "total_estimate": total_estimate,
        "completed_estimate": completed_estimate,
        "unestimated_count": unestimated_count,
    }


def release_readiness(db: Session, version: Version) -> list[dict]:
    """unresolved blockers, open critical bugs, incomplete work,
    missing release date."""
    checks: list[dict] = []
    items = _version_items(db, version.id)
    item_ids = {i.id for i in items}

    incomplete = [i for i in items if not is_done_status(db, i)]
    checks.append({
        "key": "all_work_complete", "passed": len(incomplete) == 0,
        "message": (
            "All associated work is complete" if not incomplete
            else f"{len(incomplete)} associated item(s) are not yet complete"
        ),
        "severity": "error",
    })

    open_critical = [
        i for i in items
        if i.priority == "Critical" and not is_done_status(db, i)
    ]
    checks.append({
        "key": "no_open_critical_bugs", "passed": len(open_critical) == 0,
        "message": (
            "No open critical bugs" if not open_critical
            else f"{len(open_critical)} open critical bug(s) associated with this release"
        ),
        "severity": "error",
    })

    blocking_links = []
    if item_ids:
        blocking_links = list(db.scalars(
            select(BugLink).where(
                BugLink.link_type == "blocks",
                BugLink.target_bug_id.in_(item_ids),
            )
        ).all())
    unresolved_blockers = []
    for link in blocking_links:
        blocker = db.get(Bug, link.source_bug_id)
        if blocker is not None and not is_done_status(db, blocker):
            unresolved_blockers.append(blocker)
    checks.append({
        "key": "no_unresolved_blockers", "passed": len(unresolved_blockers) == 0,
        "message": (
            "No unresolved blocking dependencies" if not unresolved_blockers
            else f"{len(unresolved_blockers)} unresolved blocker(s) for items in this release"
        ),
        "severity": "error",
    })

    checks.append({
        "key": "has_release_date", "passed": bool(version.release_date),
        "message": "Release date is set" if version.release_date else "No release date set",
        "severity": "warning",
    })

    return checks


def mark_released(db: Session, version: Version, _actor: User) -> Version:
    """`_actor` is accepted for call-site symmetry with other mutating Agile
    service functions (and future audit-by hooks); Version has no per-action
    actor column today, so it isn't persisted yet."""
    if version.status == "released":
        raise ReleaseError("Version is already released")
    if version.archived:
        raise ReleaseError("Cannot release an archived version")
    checks = release_readiness(db, version)
    hard_errors = [c for c in checks if c["severity"] == "error" and not c["passed"]]
    if hard_errors:
        raise ReleaseError(
            "Release is not ready: " + "; ".join(c["message"] for c in hard_errors)
        )
    version.status = "released"
    version.released_at = utcnow()
    version.version += 1
    db.flush()
    return version


def release_360(db: Session, version: Version) -> dict:
    return {
        "id": version.id,
        "name": version.name,
        "status": version.status,
        "start_date": version.start_date,
        "release_date": version.release_date,
        "released_at": version.released_at,
        "version": version.version,
        "progress": release_progress(db, version),
        "readiness": release_readiness(db, version),
    }


def _release_progress_many(db: Session, versions: list[Version]) -> dict[int, dict]:
    """release_progress for many versions in three queries instead of three per version."""
    from app.agile.taxonomy import category_of, status_categories

    ids = [v.id for v in versions]
    out = {vid: {"item_count": 0, "completed_count": 0, "total_estimate": 0.0,
                 "completed_estimate": 0.0, "unestimated_count": 0} for vid in ids}
    if not ids:
        return out
    links = db.execute(select(WorkItemVersion.version_id, WorkItemVersion.work_item_id).where(
        WorkItemVersion.version_id.in_(ids), WorkItemVersion.relation_type == "fix",
    )).all()
    items = {b.id: b for b in db.scalars(
        select(Bug).where(Bug.id.in_({item_id for _, item_id in links}))
    ).all()} if links else {}
    categories = status_categories(db)
    for version_id, item_id in links:
        item = items.get(item_id)
        if item is None:
            continue
        entry = out[version_id]
        entry["item_count"] += 1
        pts = float(item.story_points) if item.story_points is not None else None
        if pts is None:
            entry["unestimated_count"] += 1
        else:
            entry["total_estimate"] += pts
        if category_of(categories, item) == "done":
            entry["completed_count"] += 1
            if pts is not None:
                entry["completed_estimate"] += pts
    return out


def roadmap_data(db: Session, project_id: int) -> dict:
    """Timeline rows for Epics and Releases in one project, in a fixed number of queries.
    Read-only: an epic without a details row reads as having no dates."""
    from app.agile.taxonomy import epic_progress_many
    from app.models import EpicDetail

    epics = list(db.scalars(
        select(Bug).where(Bug.project_id == project_id, Bug.item_type == "Epic")
    ).all())
    epic_ids = [e.id for e in epics]
    details = {d.epic_id: d for d in db.scalars(
        select(EpicDetail).where(EpicDetail.epic_id.in_(epic_ids))
    ).all()} if epic_ids else {}
    progress = epic_progress_many(db, epic_ids)
    epic_rows = []
    for epic in epics:
        detail = details.get(epic.id)
        start, target = (detail.start_date, detail.target_date) if detail else (None, None)
        epic_rows.append({
            "kind": "epic", "id": epic.id, "title": epic.title,
            "start_date": start, "target_date": target,
            "health": detail.health if detail else "unknown",
            "scheduled": bool(start or target),
            "progress": progress[epic.id],
        })

    versions = list(db.scalars(select(Version).where(Version.project_id == project_id)).all())
    release_progress_by_id = _release_progress_many(db, versions)
    version_rows = []
    for v in versions:
        version_rows.append({
            "kind": "release", "id": v.id, "title": v.name,
            "start_date": v.start_date, "target_date": v.release_date,
            "status": v.status, "scheduled": bool(v.start_date or v.release_date),
            "progress": release_progress_by_id[v.id],
        })

    warnings = []
    for row in epic_rows + version_rows:
        if not row["scheduled"]:
            warnings.append(f"{row['kind'].title()} '{row['title']}' has no dates set")

    return {
        "project_id": project_id,
        "epics": [r for r in epic_rows if r["scheduled"]],
        "releases": [r for r in version_rows if r["scheduled"]],
        "unscheduled_epics": [r for r in epic_rows if not r["scheduled"]],
        "unscheduled_releases": [r for r in version_rows if not r["scheduled"]],
        "warnings": warnings,
    }
