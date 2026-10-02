"""In-process scheduler: the optional daily email digest and the daily audit-log sweep.

When ``EMAIL_DIGEST_CRON`` holds a standard 5-field cron expression the app
runs ``app.jobs.email_digest`` on that schedule with no host cron or Task
Scheduler needed. An empty expression (the default) leaves scheduling to an
external runner. Independently, with AUDIT_RETENTION_DAYS > 0 a daily task
deletes audit rows past the retention window (``app.jobs.audit_retention``).

No external dependencies: a small cron parser plus one asyncio task that
wakes at the top of every minute. Errors (bad expression, unknown timezone,
failed run) only disable or skip the scheduler; they never affect app startup
or the request path.
"""
from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timezone, tzinfo
from zoneinfo import ZoneInfo

from app.config import get_settings

logger = logging.getLogger("bug_hunter.scheduler")


def _parse_cron_part(part: str, lo: int, hi: int) -> range:
    """Parse one comma-separated element of a cron field into the range it covers.

    Handles ``*``, a single value, ``a-b`` ranges, and a trailing ``/n`` step.
    """
    part = part.strip()
    if not part:
        raise ValueError("empty element in cron field")
    step = 1
    has_step = "/" in part
    if has_step:
        base, step_s = part.split("/", 1)
        step = int(step_s)
        if step <= 0:
            raise ValueError("cron step must be a positive integer")
    else:
        base = part
    if base == "*":
        start, end = lo, hi
    elif "-" in base:
        a, b = base.split("-", 1)
        start, end = int(a), int(b)
    else:
        start = int(base)
        # "N/step" means from N to the field max (e.g. "5/15" → 5,20,35,50).
        # A bare "N" with no step is just that single value.
        end = hi if has_step else start
    if start < lo or end > hi or start > end:
        raise ValueError(f"cron value '{base}' out of range [{lo}, {hi}]")
    return range(start, end + 1, step)


def _parse_field(spec: str, lo: int, hi: int) -> set[int]:
    """Parse one cron field into the set of matching integers in ``[lo, hi]``.

    Supports ``*``, a single value, ``a-b`` ranges, ``*/n`` and ``a-b/n``
    steps, and comma-separated lists of any of those.
    """
    out: set[int] = set()
    for part in spec.split(","):
        out.update(_parse_cron_part(part, lo, hi))
    return out


class CronSchedule:
    """Parsed 5-field cron expression with a ``matches(dt)`` predicate.

    Fields: ``minute hour day-of-month month day-of-week`` (``0``/``7`` =
    Sunday). Follows the classic cron rule: when both day-of-month and
    day-of-week are restricted, a tick matches if either condition is true.
    """

    __slots__ = (
        "minute", "hour", "dom", "month", "dow",
        "_dom_restricted", "_dow_restricted",
    )

    def __init__(self, expr: str) -> None:
        fields = expr.split()
        if len(fields) != 5:
            raise ValueError(
                f"cron expression must have 5 fields, got {len(fields)}: {expr!r}"
            )
        self.minute = _parse_field(fields[0], 0, 59)
        self.hour = _parse_field(fields[1], 0, 23)
        self.dom = _parse_field(fields[2], 1, 31)
        self.month = _parse_field(fields[3], 1, 12)
        dow = _parse_field(fields[4], 0, 7)
        if 7 in dow:  # both 0 and 7 mean Sunday in standard cron
            dow.discard(7)
            dow.add(0)
        self.dow = dow
        self._dom_restricted = fields[2] != "*"
        self._dow_restricted = fields[4] != "*"

    def matches(self, dt: datetime) -> bool:
        if dt.minute not in self.minute:
            return False
        if dt.hour not in self.hour:
            return False
        if dt.month not in self.month:
            return False
        dom_ok = dt.day in self.dom
        # Python weekday() is Mon=0..Sun=6; cron dow is Sun=0..Sat=6.
        dow_ok = ((dt.weekday() + 1) % 7) in self.dow
        if self._dom_restricted and self._dow_restricted:
            return dom_ok or dow_ok
        if self._dom_restricted:
            return dom_ok
        if self._dow_restricted:
            return dow_ok
        return True


def _resolve_tz(name: str) -> tzinfo:
    """Return the IANA timezone for the cron expression, or UTC if unset/unknown."""
    if not name:
        return timezone.utc
    try:
        return ZoneInfo(name)
    except Exception:  # noqa: BLE001 — unknown tz must not break startup
        logger.warning(
            "Could not load EMAIL_DIGEST_TIMEZONE=%r; falling back to UTC, so the "
            "digest fires at the cron time in UTC rather than local time. This "
            "usually means the IANA tz database is missing from the image — "
            "ensure the 'tzdata' package is installed (it is in requirements.txt).",
            name,
        )
        return timezone.utc


def _run_digest_once() -> dict:
    """Open a DB session and run the digest. Blocking; call via a thread."""
    from app.database import SessionLocal
    from app.jobs.email_digest import run_digest

    db = SessionLocal()
    try:
        return run_digest(db)
    finally:
        db.close()


async def _tick(schedule: CronSchedule, tz: tzinfo, now: datetime | None = None) -> dict | None:
    """Fire the digest if ``now`` matches the schedule.

    Returns the run stats dict, or ``None`` if the schedule didn't match or
    the run failed. Never raises.
    """
    now = now or datetime.now(tz)
    if not schedule.matches(now):
        return None
    try:
        stats = await asyncio.to_thread(_run_digest_once)
        logger.info(
            "Scheduled digest ran: %s email(s), %s operation(s), %s user(s).",
            stats["emails_sent"], stats["operations"], stats["users"],
        )
        return stats
    except Exception:  # noqa: BLE001 — a failed run must not kill the loop
        logger.exception("Scheduled digest run failed; will retry next tick.")
        return None


async def _loop(schedule: CronSchedule, tz: tzinfo) -> None:
    last_fired_minute: datetime | None = None
    while True:
        now = datetime.now(tz)
        # Sleep until the top of the next minute, then check the schedule.
        await asyncio.sleep(max(1.0, 60 - now.second - now.microsecond / 1_000_000))
        now = datetime.now(tz)
        minute = now.replace(second=0, microsecond=0)
        # Guard against wake-time drift landing us in the same minute twice,
        # which would double-run the digest.
        if minute == last_fired_minute:
            continue
        if schedule.matches(now):
            last_fired_minute = minute
            await _tick(schedule, tz, now)


_task: asyncio.Task | None = None
_retention_task: asyncio.Task | None = None
_RETENTION_INTERVAL_SECONDS = 24 * 3600


def _in_event_loop() -> bool:
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return False
    return True


def _running_here(task: asyncio.Task | None) -> bool:
    """True for a live task of the current event loop (a task left over from another, closed
    loop counts as gone)."""
    return task is not None and not task.done() and task.get_loop() is asyncio.get_running_loop()


def _purge_audit_once() -> int:
    """Open a DB session and purge expired audit rows. Blocking; call via a thread."""
    from app.database import SessionLocal
    from app.jobs.audit_retention import purge_expired

    db = SessionLocal()
    try:
        return purge_expired(db, get_settings().AUDIT_RETENTION_DAYS)
    finally:
        db.close()


async def _retention_loop() -> None:
    """Sweep at startup, then every 24 hours; a failed sweep only skips that round."""
    while True:
        try:
            await asyncio.to_thread(_purge_audit_once)
        except Exception:  # noqa: BLE001 - keep the loop alive
            logger.exception("Audit retention sweep failed; will retry next round.")
        await asyncio.sleep(_RETENTION_INTERVAL_SECONDS)


def start() -> None:
    """Start the in-app digest scheduler when ``EMAIL_DIGEST_CRON`` is set.

    Safe to call unconditionally: does nothing when the expression is empty,
    when digest mode is off, or when the expression is invalid. Must be called
    from a running event loop (e.g. inside the FastAPI lifespan).
    """
    global _task, _retention_task
    if _task is not None and not _task.done():
        # A second start() call (e.g. accidental double lifespan) must not
        # orphan the first task: stop() only cancels whatever is in _task, so
        # overwriting it here would leak the original loop forever.
        logger.debug("Scheduler already running; start() is a no-op.")
        return
    settings = get_settings()
    if settings.AUDIT_RETENTION_DAYS > 0 and _in_event_loop() and not _running_here(_retention_task):
        _retention_task = asyncio.create_task(_retention_loop())
        logger.info("Audit retention sweep started (keeping %d days).", settings.AUDIT_RETENTION_DAYS)
    expr = settings.EMAIL_DIGEST_CRON
    if not expr:
        if settings.EMAIL_DIGEST_ENABLED:
            # Digest mode suppresses the immediate per-operation emails, so with
            # no cron here AND no external runner nothing is ever delivered.
            # Surface that silent gap loudly instead of losing a team's email.
            logger.warning(
                "EMAIL_DIGEST_ENABLED is true but EMAIL_DIGEST_CRON is empty: the "
                "in-app scheduler will NOT run and immediate emails are "
                "suppressed, so no work-item email will be sent. Set "
                "EMAIL_DIGEST_CRON (e.g. '0 8 * * *') and restart, or run "
                "'python -m app.jobs.email_digest' from an external scheduler."
            )
        return  # scheduling left to an external runner
    if not settings.EMAIL_DIGEST_ENABLED:
        logger.warning(
            "EMAIL_DIGEST_CRON is set but EMAIL_DIGEST_ENABLED is false — "
            "work-item emails send immediately, so the in-app scheduler "
            "stays idle."
        )
        return
    try:
        schedule = CronSchedule(expr)
    except (ValueError, TypeError):
        logger.exception(
            "Invalid EMAIL_DIGEST_CRON=%r — in-app scheduler disabled.", expr
        )
        return
    tz = _resolve_tz(settings.EMAIL_DIGEST_TIMEZONE)
    _task = asyncio.create_task(_loop(schedule, tz))
    logger.info("Email-digest scheduler started (cron=%r, tz=%s).", expr, tz)


async def stop() -> None:
    """Cancel the scheduler task on shutdown. Safe when never started."""
    global _task, _retention_task
    tasks = [t for t in (_task, _retention_task) if t is not None]
    for task in tasks:
        task.cancel()
    # A task of another, already closed loop cannot be awaited from here; cancelling suffices.
    tasks = [t for t in tasks if t.get_loop() is asyncio.get_running_loop()]
    # return_exceptions=True lets gather capture the task's CancelledError as
    # a result rather than swallowing it, so a cancellation targeting stop()
    # itself still propagates normally.
    await asyncio.gather(*tasks, return_exceptions=True)
    _task = _retention_task = None
