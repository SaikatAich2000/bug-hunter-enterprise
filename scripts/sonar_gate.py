"""Manage the SonarQube quality gate for this project (requires SONAR_TOKEN).

The default "Sonar way" gate blocks this project's first analyses (it demands
80% coverage on the whole codebase *and* 100% of security hotspots reviewed on
every analysis). This tool creates a project gate that keeps the non-negotiable
bars and sets one realistic coverage floor:

  * Security rating (overall) = A AND vulnerabilities = 0 -> zero security
    issues anywhere in the codebase, always
  * Security rating on new code = A AND new vulnerabilities = 0 -> zero new
    security issues on every analysis, always
  * the rest of the copied "Sonar way" new-code bars, unchanged: on
    SonarQube 10+ "no new issues" (new_violations = 0) and duplicated lines
    on new code <= 3%; older servers carry new reliability/maintainability
    rating = A instead
  * Coverage (overall AND on new code) >= 80% (adjustable with
    --coverage-threshold); the overall `coverage` condition is created when
    the copied gate only carries `new_coverage`, so the bar applies to the
    whole codebase and not just to the diff
  * the "hotspots reviewed" condition is removed from the gate (hotspots are
    still reported in SonarQube; they just must not block every image build;
    real vulnerabilities are still blocked by the ratings above)

Usage (token stays on the machine that runs this):

    $env:SONAR_TOKEN = "<token>"          # or --token
    $env:SONAR_HOST_URL = "http://..."    # or --host-url

    python scripts/sonar_gate.py report   # show the conditions that failed
    python scripts/sonar_gate.py apply    # create/refresh + assign the gate

Idempotent: re-running `apply` updates the coverage floor and re-applies the
condition set instead of duplicating anything.
"""
from __future__ import annotations

import argparse
import base64
import json
import os
import sys
import urllib.error
import urllib.parse
import urllib.request

GATE_NAME = "bug-hunter-gate"
#: Coverage conditions (overall and on-new-code) are pinned to this floor.
COVERAGE_METRIC_MARKERS = ("coverage",)
#: Metrics whose conditions are removed from the gate: hotspot review is a
#: manual triage activity and must not block every image build (real
#: vulnerabilities are still blocked by the security ratings below).
DROP_METRIC_MARKERS = ("hotspot",)
DROP_METRIC_NAMES = frozenset({
    "security_review_rating", "new_security_review_rating",
    "security_hotspots_reviewed", "new_security_hotspots_reviewed",
})
#: Metrics that must exist on the gate (created if missing) so security
#: findings always fail the build: overall security rating, overall
#: vulnerability count, new-code security rating, new vulnerability count.
#: Rating thresholds are numeric in the Web API (1 = A ... 5 = E), so
#: "GT 1" reads "worse than A"; the letter is rejected as an invalid value.
RATING_A = "1"
SECURITY_METRIC_DEFAULTS = (
    ("security_rating", "GT", RATING_A),
    ("vulnerabilities", "GT", "0"),
    ("new_security_rating", "GT", RATING_A),
    ("new_vulnerabilities", "GT", "0"),
)
#: Single coverage floor for the whole project: overall and on-new-code.
DEFAULT_COVERAGE_THRESHOLD = "80"
#: SonarQube metric key for coverage of the *whole* codebase (as opposed to
#: `new_coverage`, which only looks at the changes of the latest analysis).
OVERALL_COVERAGE_METRIC = "coverage"


def required_conditions(coverage_threshold: str = DEFAULT_COVERAGE_THRESHOLD) -> tuple:
    """Conditions the gate must always carry, whatever base it was copied from."""
    return (*SECURITY_METRIC_DEFAULTS,
            (OVERALL_COVERAGE_METRIC, "LT", coverage_threshold))


def _auth_header(token: str) -> dict[str, str]:
    encoded = base64.b64encode(f"{token}:".encode("utf-8")).decode("ascii")
    return {"Authorization": f"Basic {encoded}"}


def request(base_url: str, token: str, path: str, params: dict | None = None,
            method: str = "GET") -> dict | list:
    """Call the SonarQube web API. Never prints the token."""
    url = f"{base_url.rstrip('/')}{path}"
    data = None
    if params:
        if method == "GET":
            url += "?" + urllib.parse.urlencode(params)
        else:
            data = urllib.parse.urlencode(params).encode("utf-8")
    req = urllib.request.Request(url, data=data, method=method,
                                 headers=_auth_header(token))
    try:
        with urllib.request.urlopen(req, timeout=60) as resp:
            body = resp.read().decode("utf-8")
            return json.loads(body) if body else {}
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")[:300]
        if exc.code == 401:
            raise SystemExit(
                "SonarQube rejected the token (401). Check SONAR_TOKEN / "
                "SONAR_HOST_URL."
            ) from exc
        raise SystemExit(f"SonarQube API {exc.code} on {path}: {detail}") from exc
    except urllib.error.URLError as exc:
        raise SystemExit(
            f"Cannot reach SonarQube at {base_url.rstrip('/')}: {exc.reason}"
        ) from exc


# Gates are addressed by NAME everywhere below: the name parameters exist since
# SonarQube 8.4, and from 10.0 on they are the only ones (the list payload no
# longer carries a gate id/key, and the built-in flag is spelled "isBuiltIn").

def find_builtin_default_gate(gates: dict) -> str:
    """The name of the built-in 'Sonar way' gate, which we copy as our base."""
    for gate in gates.get("qualitygates", []):
        built_in = gate.get("isBuiltIn", gate.get("builtIn"))
        if built_in and "Sonar way" in gate.get("name", ""):
            return gate["name"]
    raise SystemExit("Built-in 'Sonar way' gate not found on this server.")


def find_gate_by_name(gates: dict, name: str) -> str | None:
    for gate in gates.get("qualitygates", []):
        if gate.get("name") == name:
            return gate["name"]
    return None


def plan_condition_changes(
    conditions: list[dict], coverage_threshold: str = DEFAULT_COVERAGE_THRESHOLD
) -> tuple:
    """Decide what to change in a copied gate. Pure function (unit-tested).

    Returns (coverage_updates, drop_conditions, keep_conditions, to_add) where
    ``coverage_updates`` is a list of ``{id, metric, op, error}`` (one per
    coverage condition present: overall and/or new code) and ``to_add`` lists
    the ``(metric, op, error)`` triples the gate is still missing.
    """
    updates: list[dict] = []
    drop: list[dict] = []
    keep: list[dict] = []
    for cond in conditions:
        metric = cond.get("metric", "")
        lowered = metric.lower()
        if any(marker in lowered for marker in COVERAGE_METRIC_MARKERS):
            updates.append({
                "id": cond["id"], "metric": metric,
                "op": cond.get("op", "LT"), "error": coverage_threshold,
            })
        elif (any(marker in lowered for marker in DROP_METRIC_MARKERS)
              or metric in DROP_METRIC_NAMES):
            drop.append(cond)
        else:
            keep.append(cond)
    present_metrics = {c.get("metric") for c in conditions}
    missing = [s for s in required_conditions(coverage_threshold)
               if s[0] not in present_metrics]
    return updates, drop, keep, missing


def summarize_status(status: dict) -> tuple[str, list[dict]]:
    """(gate status, failed conditions) from a project_status payload."""
    project = status.get("projectStatus", {})
    failed = [c for c in project.get("conditions", []) if c.get("status") == "ERROR"]
    return project.get("status", "UNKNOWN"), failed


def _print_conditions(title: str, conditions: list[dict]) -> None:
    print(title)
    for cond in conditions:
        print(
            f"  {cond.get('metric', '?'):<44}"
            f"{cond.get('op', ''):<5}{cond.get('error', ''):<8}"
            f"actual={cond.get('actualValue', cond.get('value', '?'))} "
            f"status={cond.get('status', '')}"
        )


def cmd_report(base_url: str, token: str, project_key: str) -> int:
    status = request(base_url, token, "/api/qualitygates/project_status",
                     {"projectKey": project_key})
    gate_status, failed = summarize_status(status)
    print(f"Quality gate status for {project_key}: {gate_status}")
    if failed:
        _print_conditions("FAILED conditions:", failed)
    else:
        print("No failing conditions.")
    return 0 if gate_status == "OK" else 1


def cmd_apply(base_url: str, token: str, project_key: str,
              coverage_threshold: str) -> int:
    gates = request(base_url, token, "/api/qualitygates/list")
    if find_gate_by_name(gates, GATE_NAME) is None:
        base_name = find_builtin_default_gate(gates)
        request(base_url, token, "/api/qualitygates/copy",
                {"sourceName": base_name, "name": GATE_NAME}, method="POST")
        print(f"Created gate '{GATE_NAME}' as a copy of '{base_name}'.")
    else:
        print(f"Reusing existing gate '{GATE_NAME}'.")

    detail = request(base_url, token, "/api/qualitygates/show", {"name": GATE_NAME})
    conditions = detail.get("conditions", [])
    updates, drop, keep, required_missing = plan_condition_changes(
        conditions, coverage_threshold)

    for update in updates:
        request(base_url, token, "/api/qualitygates/update_condition",
                {"id": update["id"], "metric": update["metric"],
                 "op": update["op"], "error": update["error"]}, method="POST")
        print(f"Coverage floor set to >= {coverage_threshold}% "
              f"({update['metric']}).")
    if not updates:
        print("No coverage condition found in the gate; nothing to pin.")

    for cond in drop:
        request(base_url, token, "/api/qualitygates/delete_condition",
                {"id": cond["id"]}, method="POST")
        print(f"Removed condition {cond.get('metric')} (hotspot review no longer "
              "blocks builds; findings stay visible in SonarQube).")
    if not drop:
        print("No hotspot-review conditions present.")

    for metric, op, error in required_missing:
        created_cond = request(
            base_url, token, "/api/qualitygates/create_condition",
            {"gateName": GATE_NAME, "metric": metric, "op": op,
             "error": error}, method="POST")
        print(f"Added required condition {metric} {op} {error} "
              f"(id {created_cond.get('id') or created_cond.get('condition', {}).get('id', '?')}).")
    if not required_missing:
        print("Required conditions already present "
              "(security_rating, vulnerabilities, new_security_rating, "
              "new_vulnerabilities, coverage).")

    print("Kept conditions (unchanged): "
          + ", ".join(sorted(c.get("metric", "?") for c in keep)))
    for cond in keep:
        print(f"  {cond.get('metric')}: {cond.get('op')} {cond.get('error')}")

    request(base_url, token, "/api/qualitygates/select",
            {"gateName": GATE_NAME, "projectKey": project_key}, method="POST")
    print(f"Gate '{GATE_NAME}' assigned to project '{project_key}'.")

    status = request(base_url, token, "/api/qualitygates/project_status",
                     {"projectKey": project_key})
    gate_status, failed = summarize_status(status)
    print(f"Current gate status for {project_key}: {gate_status}")
    if failed:
        _print_conditions("Still failing (real findings to address):", failed)
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Manage this project's SonarQube quality gate.")
    parser.add_argument("mode", choices=("report", "apply"),
                        help="report = show failing conditions; apply = create/refresh the gate")
    parser.add_argument("--host-url", default=os.getenv("SONAR_HOST_URL", ""),
                        help="SonarQube base URL (default: $SONAR_HOST_URL)")
    parser.add_argument("--token", default=os.getenv("SONAR_TOKEN", ""),
                        help="SonarQube token (default: $SONAR_TOKEN); never printed")
    parser.add_argument("--project-key", default=os.getenv("SONAR_PROJECT_KEY", "Bug-Hunter-Enterprise"))
    parser.add_argument("--coverage-threshold", default=DEFAULT_COVERAGE_THRESHOLD,
                        help="Coverage floor (percent) to pin on the gate")
    args = parser.parse_args(argv)

    if not args.host_url or not args.token:
        print("SONAR_HOST_URL and SONAR_TOKEN are required "
              "(arguments or environment variables).", file=sys.stderr)
        return 2

    if args.mode == "report":
        return cmd_report(args.host_url, args.token, args.project_key)
    return cmd_apply(args.host_url, args.token, args.project_key,
                     args.coverage_threshold)


if __name__ == "__main__":  # pragma: no cover - CLI entry point
    raise SystemExit(main())
