"""Unit tests for the reports-engine helpers extracted during the Sonar
S3776 cognitive-complexity refactor.

These exercise the new helper functions directly (no DB round-trip) so the
new-code coverage stays above the quality gate, and they pin the behaviour
the refactor must preserve:

  * _days_open_value      — open-duration in whole days, tz-coercion
  * _bug_scalar_fields    — plain bug columns with defaults
  * _bug_relation_fields  — related-object columns with None handling
  * _ttr_row              — one Time-to-Resolution row from a query tuple
  * _ttr_summary          — avg / median / p95 / fastest / slowest
  * _fold_throughput_row  — per-user bucket accumulation
  * _percentile           — NIST linear interpolation
  * filter-blob parsers   — _parse_date / _parse_int / _str_list / _int_list

The throughput query tuple shape (see _build_throughput_query) is:
  (bug_id, actor_user_id, actor_name, created_at, detail,
   item_type, title, priority, current_status, project_name)

`_bug_scalar_fields` / `_bug_relation_fields` are typed to accept a Bug, so
the tests build real (transient, never-flushed) Bug/Project/User/Event
instances rather than duck-typed stand-ins — column defaults only apply at
flush, so unset attributes read back as None / [].
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest


def _utc(y, m, d, hh=0, mm=0):
    return datetime(y, m, d, hh, mm, tzinfo=timezone.utc)


def _raw(detail, *, bug_id=5, actor_id=9, actor_name="Alice",
         created_at=None, item_type="Bug", title="Login broken",
         priority="High", current_status="Resolved", project_name="Apollo"):
    """Build a throughput-query result tuple for the helper under test."""
    return (
        bug_id, actor_id, actor_name,
        created_at if created_at is not None else _utc(2026, 1, 2),
        detail, item_type, title, priority, current_status, project_name,
    )


# ---------------------------------------------------------------------------
# _days_open_value
# ---------------------------------------------------------------------------
def test_days_open_value_resolved_span():
    from app.reports.engine import _days_open_value
    assert _days_open_value(_utc(2026, 1, 1), _utc(2026, 1, 6)) == 5


def test_days_open_value_none_creation_returns_none():
    from app.reports.engine import _days_open_value
    assert _days_open_value(None, _utc(2026, 1, 6)) is None


def test_days_open_value_still_open_measures_to_now():
    from app.reports.engine import _days_open_value
    created = datetime.now(timezone.utc) - timedelta(days=3, hours=1)
    assert _days_open_value(created, None) >= 3


def test_days_open_value_never_negative():
    from app.reports.engine import _days_open_value
    # resolved before created (clock skew / bad data) clamps to 0.
    assert _days_open_value(_utc(2026, 1, 10), _utc(2026, 1, 1)) == 0


def test_days_open_value_coerces_naive_datetimes():
    from app.reports.engine import _days_open_value
    created = datetime(2026, 1, 1)        # naive
    resolved = datetime(2026, 1, 4)       # naive
    assert _days_open_value(created, resolved) == 3


# ---------------------------------------------------------------------------
# _bug_scalar_fields / _bug_relation_fields  (real transient Bug instances)
# ---------------------------------------------------------------------------
def test_bug_scalar_fields_applies_defaults():
    from app.models import Bug
    from app.reports.engine import _bug_scalar_fields
    b = Bug(id=7)  # every other column unset → reads back as None
    f = _bug_scalar_fields(b)
    assert f["id"] == 7
    assert f["item_type"] == "Bug"      # None → default
    assert f["title"] == ""
    assert f["status"] == ""
    assert f["created_at"] == ""        # _fmt_dt(None) → ""


def test_bug_scalar_fields_caps_description():
    from app.models import Bug
    from app.reports.engine import _bug_scalar_fields
    b = Bug(id=1, title="t", description="x" * 40000, status="New",
            priority="Low", environment="DEV")
    assert len(_bug_scalar_fields(b)["description"]) == 32000


def test_bug_relation_fields_resolves_related_objects():
    from app.models import Bug, Event, Project, User
    from app.reports.engine import _bug_relation_fields
    b = Bug(id=1, title="t")
    b.project = Project(name="Apollo")
    b.event = Event(name="Launch")
    b.reporter = User(name="Alice", email="alice@x.io")
    b.assignees = [User(name="Bob"), User(name="Carol")]
    f = _bug_relation_fields(b)
    assert f["project"] == "Apollo"
    assert f["event"] == "Launch"
    assert f["reporter_name"] == "Alice"
    assert f["reporter_email"] == "alice@x.io"
    assert f["assignees"] == "Bob, Carol"


def test_bug_relation_fields_handles_all_none():
    from app.models import Bug
    from app.reports.engine import _bug_relation_fields
    # No project/event/reporter, no assignees → every field falls back to "".
    b = Bug(id=2, title="t")
    f = _bug_relation_fields(b)
    assert f["project"] == ""
    assert f["event"] == ""
    assert f["reporter_name"] == ""
    assert f["reporter_email"] == ""
    assert f["assignees"] == ""


# ---------------------------------------------------------------------------
# _ttr_row
# ---------------------------------------------------------------------------
def test_ttr_row_builds_resolved_row():
    from app.reports.engine import _ttr_row
    raw = _raw("status: 'New' → 'Resolved'", created_at=_utc(2026, 1, 2))
    row = _ttr_row(raw, _utc(2026, 1, 1))  # 24h before resolution
    assert row["bug_id"] == 5
    assert row["item_type"] == "Bug"
    assert row["resolved_by"] == "Alice"
    assert row["project"] == "Apollo"
    assert row["hours_to_resolve"] == pytest.approx(24.0)
    assert row["days_to_resolve"] == pytest.approx(1.0)


def test_ttr_row_skips_non_resolved_transition():
    from app.reports.engine import _ttr_row
    raw = _raw("status: 'New' → 'In Progress'", current_status="In Progress")
    assert _ttr_row(raw, _utc(2026, 1, 1)) is None


def test_ttr_row_skips_when_no_creation_time():
    from app.reports.engine import _ttr_row
    raw = _raw("status: 'New' → 'Resolved'")
    assert _ttr_row(raw, None) is None


def test_ttr_row_clamps_and_coerces_naive():
    from app.reports.engine import _ttr_row
    # naive resolution 12h after naive creation, status Closed (resolved for Bug).
    raw = _raw("status: 'New' → 'Closed'",
               created_at=datetime(2026, 1, 1, 12, 0), current_status="Closed")
    row = _ttr_row(raw, datetime(2026, 1, 1, 0, 0))
    assert row["hours_to_resolve"] == pytest.approx(12.0)


# ---------------------------------------------------------------------------
# _ttr_summary
# ---------------------------------------------------------------------------
def test_ttr_summary_empty_is_all_zero():
    from app.reports.engine import _ttr_summary
    s = _ttr_summary([])
    assert s == {"count": 0, "average_hours": 0, "median_hours": 0,
                 "p95_hours": 0, "fastest_hours": 0, "slowest_hours": 0}


def test_ttr_summary_aggregates_sorted_rows():
    from app.reports.engine import _ttr_summary
    rows = [{"hours_to_resolve": h} for h in (2.0, 4.0, 6.0)]
    s = _ttr_summary(rows)
    assert s["count"] == 3
    assert s["average_hours"] == pytest.approx(4.0)
    assert s["median_hours"] == pytest.approx(4.0)
    assert s["fastest_hours"] == pytest.approx(2.0)     # rows[0]
    assert s["slowest_hours"] == pytest.approx(6.0)     # rows[-1]
    assert s["p95_hours"] >= 5.0


# ---------------------------------------------------------------------------
# _fold_throughput_row
# ---------------------------------------------------------------------------
def test_fold_throughput_row_counts_resolution():
    from app.reports.engine import _fold_throughput_row
    per_user, details = {}, []
    _fold_throughput_row(_raw("status: 'New' → 'Resolved'"), per_user, details)
    assert per_user[9]["resolved_count"] == 1
    assert per_user[9]["by_type"]["Bug"] == 1
    assert per_user[9]["by_status"]["Resolved"] == 1
    assert per_user[9]["user_name"] == "Alice"
    assert len(details) == 1
    assert details[0]["new_status"] == "Resolved"
    assert details[0]["bug_id"] == 5


def test_fold_throughput_row_deleted_actor_uses_sentinel():
    from app.reports.engine import _fold_throughput_row
    per_user, details = {}, []
    raw = _raw("status: 'New' → 'Closed'", actor_id=None, actor_name=None,
               current_status="Closed")
    _fold_throughput_row(raw, per_user, details)
    assert -1 in per_user
    assert per_user[-1]["user_id"] is None
    assert per_user[-1]["user_name"] == "(deleted user)"
    assert details[0]["user_name"] == "(deleted user)"


def test_fold_throughput_row_ignores_non_resolution():
    from app.reports.engine import _fold_throughput_row
    per_user, details = {}, []
    raw = _raw("status: 'New' → 'In Progress'", current_status="In Progress")
    _fold_throughput_row(raw, per_user, details)
    assert per_user == {}
    assert details == []


def test_fold_throughput_row_accumulates_multiple_for_one_user():
    from app.reports.engine import _fold_throughput_row
    per_user, details = {}, []
    _fold_throughput_row(_raw("status: 'New' → 'Resolved'", bug_id=1),
                         per_user, details)
    _fold_throughput_row(_raw("status: 'New' → 'Closed'", bug_id=2,
                              current_status="Closed"), per_user, details)
    assert per_user[9]["resolved_count"] == 2
    assert per_user[9]["by_status"] == {"Resolved": 1, "Closed": 1}
    assert len(details) == 2


# ---------------------------------------------------------------------------
# _percentile (NIST linear interpolation)
# ---------------------------------------------------------------------------
def test_percentile_empty_is_zero():
    from app.reports.engine import _percentile
    assert _percentile([], 95) == pytest.approx(0.0)


def test_percentile_exact_rank():
    from app.reports.engine import _percentile
    # 0th and 100th land exactly on the endpoints (f == c branch).
    assert _percentile([10.0, 20.0, 30.0], 0) == pytest.approx(10.0)
    assert _percentile([10.0, 20.0, 30.0], 100) == pytest.approx(30.0)


def test_percentile_interpolates_between_ranks():
    from app.reports.engine import _percentile
    # 95th of 0..100 (step 10) → between the 9th (90) and 10th (100) ranks.
    values = [float(x) for x in range(0, 101, 10)]
    assert _percentile(values, 95) == pytest.approx(95.0)


# ---------------------------------------------------------------------------
# Filter-blob parsing helpers
# ---------------------------------------------------------------------------
def test_parse_date_variants():
    from datetime import date
    from app.reports.engine import _parse_date
    assert _parse_date("2026-06-11") == date(2026, 6, 11)
    assert _parse_date(date(2026, 1, 2)) == date(2026, 1, 2)
    assert _parse_date(datetime(2026, 1, 2, 9, 0)) == date(2026, 1, 2)
    assert _parse_date("") is None
    assert _parse_date(None) is None
    assert _parse_date("not-a-date") is None
    assert _parse_date(12345) is None


def test_parse_int_variants():
    from app.reports.engine import _parse_int
    assert _parse_int("7") == 7
    assert _parse_int(7) == 7
    assert _parse_int("") is None
    assert _parse_int(None) is None
    assert _parse_int("abc") is None


def test_str_list_variants():
    from app.reports.engine import _str_list
    assert _str_list(None) == []
    assert _str_list("  hi ") == ["hi"]
    assert _str_list("   ") == []
    # Dedupe + strip + drop non-strings / empties, order preserved.
    assert _str_list(["a", " a ", "b", "", 5]) == ["a", "b"]
    assert _str_list(42) == []


def test_int_list_variants():
    from app.reports.engine import _int_list
    assert _int_list(None) == []
    assert _int_list([1, "2", 2, "x", None]) == [1, 2]
    assert _int_list(9) == [9]
    assert _int_list("nope") == []


# ---------------------------------------------------------------------------
# _age_bucket — boundary buckets
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("days,bucket", [
    (0, "0-7 days"),
    (7, "0-7 days"),
    (8, "8-30 days"),
    (30, "8-30 days"),
    (31, "31-60 days"),
    (60, "31-60 days"),
    (61, "61-90 days"),
    (90, "61-90 days"),
    (91, "90+ days"),
    (365, "90+ days"),
])
def test_age_bucket_boundaries(days, bucket):
    from app.reports.engine import _age_bucket
    assert _age_bucket(days) == bucket
