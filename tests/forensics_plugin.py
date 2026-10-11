"""CI forensics plugin, armed with CI_TEST_FORENSICS=1 and inert otherwise.

A killed runner never lets pytest flush junit.xml, so heartbeats, slow-test lines
and thread dumps are streamed to stdout (and a per-process log) instead.
"""
from __future__ import annotations

import faulthandler
import os
import signal
import sys
import threading
import time
from pathlib import Path

import pytest

_FORENSICS_ENABLED = os.environ.get("CI_TEST_FORENSICS", "") == "1"
_FORENSICS_LOG = Path(
    os.environ.get("CI_TEST_FORENSICS_LOG", "ci-forensics.log")
).resolve()
_FORENSICS_HEARTBEAT_SECONDS = max(
    15, int(os.environ.get("CI_TEST_FORENSICS_HEARTBEAT", "60") or 60)
)
_FORENSICS_SLOW_SECONDS = max(
    30, int(os.environ.get("CI_TEST_FORENSICS_SLOW", "30") or 30)
)
_forensics_lock = threading.Lock()
_forensics_current: dict[str, object] = {}
_forensics_heartbeat_stop = threading.Event()
_forensics_handle = None
_forensics_totals = {"tests": 0, "failed": 0, "slow": 0, "slowest": 0.0, "slowest_id": "-"}


def _forensics_log_line(line: str) -> None:
    """Append one line to the forensics log through a single reused handle."""
    global _forensics_handle
    try:
        if _forensics_handle is None or _forensics_handle.closed:
            _FORENSICS_LOG.parent.mkdir(parents=True, exist_ok=True)
            _forensics_handle = _FORENSICS_LOG.open("a", encoding="utf-8", buffering=1)
        _forensics_handle.write(line + "\n")
    except (OSError, ValueError):
        pass


def _forensics_close() -> None:
    """Flush and close the forensics log (called at session teardown)."""
    global _forensics_handle
    try:
        if _forensics_handle is not None and not _forensics_handle.closed:
            _forensics_handle.flush()
            _forensics_handle.close()
    except (OSError, ValueError):
        pass
    _forensics_handle = None


def _forensics_emit(kind: str, message: str, stream: bool = True) -> None:
    """Record one timestamped line; echo it to stdout only when stream=True.

    stream=False keeps per-test bookkeeping out of the job log while the full
    detail still lands in the artifact.
    """
    stamp = time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime())
    line = f"[{stamp}Z] [ci-forensics pid={os.getpid()}] {kind}: {message}"
    _forensics_log_line(line)
    if not stream:
        return
    try:
        print(line, flush=True)
    except OSError:
        pass


def _forensics_heartbeat() -> None:
    """Emit a line ONLY while a single test has outlived the heartbeat interval.

    A healthy suite never trips this (every test finishes inside the window),
    so the job log stays quiet; a wedged test produces one line per interval
    naming it and how long it has held the worker.
    """
    while not _forensics_heartbeat_stop.wait(_FORENSICS_HEARTBEAT_SECONDS):
        with _forensics_lock:
            test_id = _forensics_current.get("test")
            since = _forensics_current.get("since")
        if not test_id or since is None:
            continue
        held = time.monotonic() - float(since)
        if held < _FORENSICS_HEARTBEAT_SECONDS:
            continue
        _forensics_emit("heartbeat", f"test still running after {held:.0f}s: {test_id}")


def _forensics_on_signal(signum: int, _frame) -> None:  # pragma: no cover
    """Trap: dump every thread's stack, then let the default action proceed."""
    try:
        name = signal.Signals(signum).name
    except (AttributeError, ValueError):
        name = str(signum)
    _forensics_emit("signal", f"received {name}; dumping all thread stacks")
    try:
        faulthandler.dump_traceback(file=sys.stderr)
    except (OSError, ValueError):
        pass
    # Re-raise with the default disposition so the runner still sees the
    # real exit behaviour (143 on SIGTERM, etc.) instead of a silent swallow.
    signal.signal(signum, signal.SIG_DFL)
    try:
        os.kill(os.getpid(), signum)
    except OSError:
        pass


def pytest_configure(config):
    if not _FORENSICS_ENABLED:
        return
    for signum in (signal.SIGTERM, signal.SIGABRT):
        try:
            # faulthandler first: it needs the raw frames at kill time.
            # (Guarded with getattr: python -X faulthandler pre-imports the
            # module early, which can surface a partially-initialized copy
            # missing 'register' on some interpreters.)
            faulthandler_register = getattr(faulthandler, "register", None)
            if callable(faulthandler_register):
                faulthandler_register(signum, file=sys.stderr, all_threads=True)
            previous = signal.getsignal(signum)
            if callable(previous):

                def _chained(signum_inner, frame, _prev=previous):
                    _forensics_on_signal(signum_inner, frame)
                    _prev(signum_inner, frame)

                signal.signal(signum, _chained)
            else:
                signal.signal(signum, _forensics_on_signal)
        except (OSError, ValueError, RuntimeError):
            pass
    _forensics_heartbeat_stop.clear()
    worker = threading.Thread(
        target=_forensics_heartbeat, name="ci-forensics-heartbeat", daemon=True
    )
    worker.start()
    # stream=False: with one pytest process per test file this line would
    # otherwise repeat ~105 times. The artifact keeps the process boundaries.
    _forensics_emit(
        "session-start",
        f"pid={os.getpid()} heartbeat={_FORENSICS_HEARTBEAT_SECONDS}s "
        f"slow-threshold={_FORENSICS_SLOW_SECONDS}s log={_FORENSICS_LOG}",
        stream=False,
    )


def pytest_unconfigure(config):
    if not _FORENSICS_ENABLED:
        return
    _forensics_heartbeat_stop.set()
    _forensics_emit("session-finish", "pytest session teardown reached", stream=False)
    totals = _forensics_totals
    # The one line that replaces ~4,800 per-test stdout lines.
    _forensics_emit(
        "session-summary",
        f"tests={totals['tests']} failed={totals['failed']} "
        f"slow(>={_FORENSICS_SLOW_SECONDS}s)={totals['slow']} "
        f"slowest={totals['slowest_id']} ({totals['slowest']:.1f}s)",
    )
    _forensics_close()


@pytest.hookimpl(hookwrapper=True)
def pytest_runtest_protocol(item, nextitem):
    if not _FORENSICS_ENABLED:
        yield
        return
    test_id = getattr(item, "nodeid", str(item))
    started = time.monotonic()
    with _forensics_lock:
        _forensics_current["test"] = test_id
        _forensics_current["since"] = started
    # Artifact only: ~2,200 of these per run is pure noise in the job log.
    _forensics_emit("test-start", test_id, stream=False)
    try:
        yield
    finally:
        # Phase outcomes recorded by pytest_runtest_logreport below.
        failed = [
            phase
            for phase in ("setup", "call", "teardown")
            if _forensics_reports.pop((test_id, phase), False)
        ]
        elapsed = time.monotonic() - started
        slow = elapsed >= _FORENSICS_SLOW_SECONDS
        with _forensics_lock:
            _forensics_current.pop("test", None)
            _forensics_current.pop("since", None)
            _forensics_totals["tests"] += 1
            _forensics_totals["slow"] += 1 if slow else 0
            _forensics_totals["failed"] += 1 if failed else 0
            if elapsed > _forensics_totals["slowest"]:
                _forensics_totals["slowest"] = elapsed
                _forensics_totals["slowest_id"] = test_id
        _forensics_emit(
            f"test-end {'FAILED:' + ','.join(failed) if failed else 'ok'} "
            f"{elapsed:.1f}s{' SLOW' if slow else ''}",
            test_id,
            # Only the interesting tests reach stdout.
            stream=bool(failed) or slow,
        )


_forensics_reports: dict[tuple[str, str], bool] = {}


@pytest.hookimpl
def pytest_runtest_logreport(report) -> None:
    """Record each phase outcome; pytest_runtest_protocol reads it on test-end.

    (A plain hook, not a wrapper: it fires exactly when the setup/call/
    teardown report object exists, so no pluggy ordering pitfalls.)
    """
    if not _FORENSICS_ENABLED:
        return
    try:
        _forensics_reports[(report.nodeid, report.when)] = bool(
            getattr(report, "failed", False)
        )
    except Exception:  # never break reporting because of forensics
        pass

