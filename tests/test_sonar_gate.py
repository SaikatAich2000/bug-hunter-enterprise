"""Contract for scripts/sonar_gate.py: the quality-gate change must keep the
zero-security bars (security rating A and zero vulnerabilities, overall and
on new code), pin the coverage floor at 80% (overall and on new code) and
remove the hotspot-review blocker.
"""
from __future__ import annotations

from scripts.sonar_gate import (
    DEFAULT_COVERAGE_THRESHOLD,
    find_builtin_default_gate,
    find_gate_by_name,
    plan_condition_changes,
    required_conditions,
    summarize_status,
)


def test_default_coverage_floor_is_eighty_percent():
    assert DEFAULT_COVERAGE_THRESHOLD == "80"


def test_pyproject_enforces_the_same_floor():
    """One project-wide number: pyproject's fail_under must match the gate."""
    import tomllib
    from pathlib import Path

    pyproject = Path(__file__).resolve().parents[1] / "pyproject.toml"
    with pyproject.open("rb") as handle:
        config = tomllib.load(handle)
    assert config["tool"]["coverage"]["report"]["fail_under"] == int(DEFAULT_COVERAGE_THRESHOLD)


def test_required_conditions_include_overall_and_new_security_and_coverage():
    assert required_conditions() == (
        ("security_rating", "GT", "1"),
        ("vulnerabilities", "GT", "0"),
        ("new_security_rating", "GT", "1"),
        ("new_vulnerabilities", "GT", "0"),
        ("coverage", "LT", "80"),
    )
    assert required_conditions("60")[-1] == ("coverage", "LT", "60")


def _condition(cid, metric, op="LT", error="0"):
    return {"id": cid, "metric": metric, "op": op, "error": error}


def _sonar_way_conditions():
    """Shape of the default 'Sonar way' conditions on a current SonarQube."""
    return [
        _condition("c1", "new_reliability_rating", "GT", "1"),
        _condition("c2", "new_security_rating", "GT", "1"),
        _condition("c3", "new_software_quality_maintainability_rating", "GT", "1"),
        _condition("c4", "new_coverage", "LT", "80"),
        _condition("c5", "new_duplicated_lines_density", "GREATER_THAN", "3"),
        _condition("c6", "new_security_hotspots_reviewed", "LESS_THAN", "100"),
    ]


def test_plan_keeps_zero_defect_bars_and_pins_coverage_floor():
    updates, drop, keep, missing = plan_condition_changes(
        _sonar_way_conditions())
    assert updates == [{"id": "c4", "metric": "new_coverage", "op": "LT", "error": "80"}]
    assert [c["id"] for c in drop] == ["c6"]
    kept = {c["metric"] for c in keep}
    assert kept == {
        "new_reliability_rating", "new_security_rating",
        "new_software_quality_maintainability_rating", "new_duplicated_lines_density",
    }
    # 'Sonar way' has no overall-coverage condition, so one is created.
    assert [m for m, _op, _err in missing] == [
        "security_rating", "vulnerabilities", "new_vulnerabilities", "coverage",
    ]


def test_plan_pins_both_overall_and_new_code_coverage():
    updates, _drop, _keep, missing = plan_condition_changes([
        _condition("o1", "coverage", "LT", "95"),
        _condition("n1", "new_coverage", "LT", "95"),
    ])
    assert [u["id"] for u in updates] == ["o1", "n1"]


def test_pinned_coverage_floor_is_eighty_percent():
    """The coverage floor for both overall and new code is 80 (single assert)."""
    updates, _drop, _keep, _missing = plan_condition_changes([
        _condition("o1", "coverage", "LT", "95"),
        _condition("n1", "new_coverage", "LT", "95"),
    ])
    assert {u["error"] for u in updates} == {"80"}


def test_plan_does_not_readd_present_conditions():
    conditions = _sonar_way_conditions() + [
        _condition("s1", "security_rating", "GT", "1"),
        _condition("s2", "vulnerabilities", "GREATER_THAN", "0"),
        _condition("s3", "new_security_rating", "GT", "1"),
        _condition("s4", "new_vulnerabilities", "GREATER_THAN", "0"),
        _condition("s5", "coverage", "LT", "80"),
    ]
    _, _, keep, missing = plan_condition_changes(conditions)
    assert missing == []
    kept = {c["metric"] for c in keep}
    assert {"security_rating", "vulnerabilities",
            "new_security_rating", "new_vulnerabilities"} <= kept


def test_plan_supports_alternative_hotspot_metric_spellings():
    for metric in ("security_review_rating", "new_security_review_rating",
                   "security_hotspots_reviewed"):
        updates, drop, _keep, _missing = plan_condition_changes(
            [_condition("c1", "new_coverage", "LT", "55"), _condition("c2", metric, "LT", "100")]
        )
        assert [c["id"] for c in drop] == ["c2"], metric
        assert updates[0]["error"] == "80"


def test_plan_handles_a_gate_without_coverage_condition():
    updates, drop, keep, missing = plan_condition_changes(
        [_condition("c1", "new_security_rating")])
    assert updates == []
    assert drop == []
    assert keep[0]["metric"] == "new_security_rating"
    added = [m for m, _op, _err in missing]
    assert "new_security_rating" not in added
    assert "security_rating" in added
    assert "coverage" in added


def test_custom_coverage_threshold_is_applied():
    updates, _, _, missing = plan_condition_changes(
        _sonar_way_conditions(), coverage_threshold="60")
    assert updates[0]["error"] == "60"
    assert ("coverage", "LT", "60") in missing


def test_find_gates():
    gates = {"qualitygates": [
        {"key": "AY0", "name": "Sonar way", "builtIn": True},
        {"key": "AY1", "name": "bug-hunter-gate", "builtIn": False},
    ]}
    assert find_builtin_default_gate(gates) == "Sonar way"
    assert find_gate_by_name(gates, "bug-hunter-gate") == "bug-hunter-gate"
    assert find_gate_by_name(gates, "missing") is None


def test_find_gates_on_current_sonarqube_payload():
    # SonarQube 10+: no gate key/id in the list, built-in flag is "isBuiltIn".
    gates = {"qualitygates": [
        {"name": "Sonar way", "isDefault": True, "isBuiltIn": True},
        {"name": "Custom", "isDefault": False, "isBuiltIn": False},
    ]}
    assert find_builtin_default_gate(gates) == "Sonar way"
    assert find_gate_by_name(gates, "Custom") == "Custom"


def test_status_summary_extracts_failed_conditions():
    status = {"projectStatus": {"status": "ERROR", "conditions": [
        {"metricKey": "new_coverage", "status": "ERROR", "actualValue": "55.6"},
        {"metricKey": "new_security_rating", "status": "OK", "actualValue": "A"},
    ]}}
    gate_status, failed = summarize_status(status)
    assert gate_status == "ERROR"
    assert [c["metricKey"] for c in failed] == ["new_coverage"]
    ok_status, none_failed = summarize_status({"projectStatus": {"status": "OK", "conditions": []}})
    assert ok_status == "OK"
    assert none_failed == []
