"""Coverage-guided, declaration-only refinement of frozen oracle scenarios."""

from __future__ import annotations

import json
import math
import re
from pathlib import Path
from typing import Any, Callable

from .contract import canonical_json, content_hash
from .errors import NonRepairableInfrastructureError, PreRtlGateError
from .spec import DesignSpec, validate_spec
from .verification import generate_expected_trace


ProgressCallback = Callable[[str, dict[str, Any]], None]
ORACLE_COVERAGE_METRIC = "python_oracle_executable_line_proxy"
ORACLE_COVERAGE_LIMITATION = (
    "This measures executed lines in the Python oracle only. It is not RTL "
    "functional, assertion, toggle, branch, or code coverage."
)
SHA256_RE = re.compile(r"^[0-9a-f]{64}$")


def _evidence_error(detail: str) -> RuntimeError:
    return RuntimeError(f"oracle line-coverage evidence is invalid: {detail}")


def _sha256(value: Any, label: str) -> str:
    if not isinstance(value, str) or not SHA256_RE.fullmatch(value):
        raise _evidence_error(f"{label} must be a lowercase SHA-256 digest")
    return value


def _line_numbers(value: Any, label: str) -> list[int]:
    if (
        not isinstance(value, list)
        or any(isinstance(row, bool) or not isinstance(row, int) or row < 1 for row in value)
        or value != sorted(set(value))
    ):
        raise _evidence_error(f"{label} must be sorted unique positive line numbers")
    return value


def _coverage_snapshot(value: Any, label: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise _evidence_error(f"{label} must be an object")
    if (
        value.get("metric") != "oracle_line_coverage"
        or value.get("coverage_type") != "python_source_line"
        or value.get("scope") != "TopModule executable statement lines"
    ):
        raise _evidence_error(f"{label} has an unsupported metric or scope")
    executable = _line_numbers(value.get("executable_lines"), f"{label}.executable_lines")
    executed = _line_numbers(value.get("executed_lines"), f"{label}.executed_lines")
    missing = _line_numbers(value.get("missing_lines"), f"{label}.missing_lines")
    executable_set = set(executable)
    executed_set = set(executed)
    if not executed_set <= executable_set:
        raise _evidence_error(f"{label}.executed_lines is not a subset of executable_lines")
    if missing != sorted(executable_set - executed_set):
        raise _evidence_error(f"{label}.missing_lines does not match the uncovered lines")
    covered = value.get("covered")
    total = value.get("total")
    if (
        isinstance(covered, bool)
        or not isinstance(covered, int)
        or covered != len(executed)
        or isinstance(total, bool)
        or not isinstance(total, int)
        or total != len(executable)
    ):
        raise _evidence_error(f"{label} covered/total counts disagree with its line sets")
    percent_value = value.get("percent")
    if isinstance(percent_value, bool) or not isinstance(percent_value, (int, float)):
        raise _evidence_error(f"{label}.percent must be numeric")
    percent = float(percent_value)
    expected_percent = round(100.0 * covered / total, 3) if total else 100.0
    if not math.isfinite(percent) or percent != expected_percent:
        raise _evidence_error(
            f"{label}.percent disagrees with covered/total: expected {expected_percent:g}"
        )
    return {
        "executable_lines": executable,
        "executed_lines": executed,
        "missing_lines": missing,
        "covered": covered,
        "total": total,
        "percent": percent,
    }


def validate_oracle_line_coverage_evidence(
    spec: DesignSpec,
    report: dict[str, Any],
    *,
    final_plan: dict[str, Any] | None = None,
    final_expected_trace: dict[str, Any] | None = None,
    final_quorum: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Strictly recompute proxy coverage and optionally bind frozen artifacts.

    This validator never accepts ``target_met`` as an authority. Publication can
    supply the final plan, trace, and quorum so the report is tied to the exact
    immutable artifacts being released.
    """
    if not isinstance(report, dict) or report.get("schema_version") != 2:
        raise _evidence_error("schema_version must be 2")
    mode = spec.verification["oracle_line_coverage_mode"]
    target = spec.verification["oracle_coverage_target_pct"]
    if report.get("mode") != mode:
        raise _evidence_error("mode disagrees with the validated specification")
    if (
        report.get("metric") != ORACLE_COVERAGE_METRIC
        or report.get("rtl_functional_coverage") is not False
        or report.get("limitation") != ORACLE_COVERAGE_LIMITATION
    ):
        raise _evidence_error("metric, scope limitation, or RTL-coverage disclaimer is invalid")
    if report.get("target_percent") != target:
        raise _evidence_error("target_percent disagrees with the validated specification")

    snapshot = _coverage_snapshot(report.get("final"), "final")
    target_met = snapshot["percent"] >= float(target)
    if not isinstance(report.get("target_met"), bool) or report["target_met"] != target_met:
        raise _evidence_error("target_met disagrees with the recomputed numeric result")

    error = report.get("error")
    warning = report.get("warning")
    if error is not None and (not isinstance(error, str) or not error):
        raise _evidence_error("error must be null or a non-empty string")
    if warning is not None and (not isinstance(warning, str) or not warning):
        raise _evidence_error("warning must be null or a non-empty string")
    expected_status = (
        "not_requested"
        if mode == "off"
        else "error"
        if error is not None
        else "passed"
        if target_met
        else "warning"
    )
    if report.get("status") != expected_status:
        raise _evidence_error("status disagrees with mode, error state, and numeric result")
    stop_reason = report.get("stop_reason")
    allowed_stop_reasons = {
        "disabled",
        "target_met",
        "rounds_exhausted",
        "cycle_budget_exhausted",
        "no_improvement",
        "error",
    }
    if stop_reason not in allowed_stop_reasons:
        raise _evidence_error("stop_reason is unsupported")
    if mode == "off" and stop_reason != "disabled":
        raise _evidence_error("off mode must use the disabled stop reason")
    if mode == "off" and (warning is not None or error is not None):
        raise _evidence_error("off mode cannot contain a warning or error")
    if mode != "off" and stop_reason == "disabled":
        raise _evidence_error("an enabled mode cannot use the disabled stop reason")
    if mode != "off" and target_met and stop_reason != "target_met":
        raise _evidence_error("a met target must use the target_met stop reason")
    if not target_met and stop_reason == "target_met":
        raise _evidence_error("an unmet target cannot use the target_met stop reason")
    if error is not None and stop_reason != "error":
        raise _evidence_error("an error report must use the error stop reason")
    if error is None and stop_reason == "error":
        raise _evidence_error("the error stop reason requires an error")
    if expected_status == "passed" and (warning is not None or error is not None):
        raise _evidence_error("a passed report cannot contain a warning or error")
    if expected_status == "warning" and (warning is None or error is not None):
        raise _evidence_error("a warning report must contain only a warning")

    rounds = report.get("rounds")
    remaining_cycles = report.get("remaining_cycle_budget")
    added_scenarios = report.get("added_scenario_count")
    if not isinstance(rounds, list) or len(rounds) > spec.verification["oracle_coverage_rounds"]:
        raise _evidence_error("round history exceeds the validated refinement limit")
    if (
        isinstance(remaining_cycles, bool)
        or not isinstance(remaining_cycles, int)
        or not 0 <= remaining_cycles <= spec.verification["oracle_coverage_cycle_budget"]
    ):
        raise _evidence_error("remaining_cycle_budget is outside the configured budget")
    if (
        isinstance(added_scenarios, bool)
        or not isinstance(added_scenarios, int)
        or added_scenarios < 0
    ):
        raise _evidence_error("added_scenario_count must be a non-negative integer")

    plan_sha256 = _sha256(report.get("final_testplan_sha256"), "final_testplan_sha256")
    trace_sha256 = _sha256(
        report.get("final_expected_trace_sha256"), "final_expected_trace_sha256"
    )
    oracle_sha256 = _sha256(report.get("final_oracle_sha256"), "final_oracle_sha256")
    bindings = report.get("evidence_bindings")
    expected_bindings = {
        "testplan_sha256": plan_sha256,
        "expected_trace_sha256": trace_sha256,
        "oracle_sha256": oracle_sha256,
    }
    if bindings != expected_bindings:
        raise _evidence_error("evidence_bindings disagrees with the report hashes")

    if final_plan is not None and content_hash(final_plan) != plan_sha256:
        raise _evidence_error("final_testplan_sha256 does not bind the supplied final plan")
    if final_expected_trace is not None:
        if content_hash(final_expected_trace) != trace_sha256:
            raise _evidence_error(
                "final_expected_trace_sha256 does not bind the supplied final trace"
            )
        if final_expected_trace.get("testplan_sha256") != plan_sha256:
            raise _evidence_error("the supplied final trace references another test plan")
        if final_expected_trace.get("oracle_sha256") != oracle_sha256:
            raise _evidence_error("the supplied final trace references another oracle")
        trace_coverage = final_expected_trace.get("oracle_line_coverage")
        if (
            _coverage_snapshot(
                trace_coverage, "final_expected_trace.oracle_line_coverage"
            )
            != snapshot
        ):
            raise _evidence_error("final coverage disagrees with the supplied final trace")
    if final_quorum is not None:
        quorum_sha256 = _sha256(
            report.get("final_oracle_quorum_sha256"),
            "final_oracle_quorum_sha256",
        )
        if content_hash(final_quorum) != quorum_sha256:
            raise _evidence_error(
                "final_oracle_quorum_sha256 does not bind the supplied final quorum"
            )
        if final_quorum.get("final_testplan_sha256") != plan_sha256:
            raise _evidence_error("the supplied final quorum references another test plan")
        if final_quorum.get("selected_expected_trace_sha256") != trace_sha256:
            raise _evidence_error("the supplied final quorum references another expected trace")
        if final_quorum.get("selected_oracle_sha256") != oracle_sha256:
            raise _evidence_error("the supplied final quorum references another oracle")
    return {
        "target_met": target_met,
        "percent": snapshot["percent"],
        "target_percent": target,
        "testplan_sha256": plan_sha256,
        "expected_trace_sha256": trace_sha256,
        "oracle_sha256": oracle_sha256,
    }


def enforce_oracle_line_coverage(
    spec: DesignSpec,
    report: dict[str, Any],
    *,
    final_plan: dict[str, Any] | None = None,
    final_expected_trace: dict[str, Any] | None = None,
    final_quorum: dict[str, Any] | None = None,
) -> None:
    """Reject an incomplete required proxy-coverage gate before RTL generation."""
    if spec.verification["oracle_line_coverage_mode"] != "required":
        return
    try:
        validated = validate_oracle_line_coverage_evidence(
            spec,
            report,
            final_plan=final_plan,
            final_expected_trace=final_expected_trace,
            final_quorum=final_quorum,
        )
    except RuntimeError as exc:
        raise PreRtlGateError(
            "oracle_line_coverage", str(exc), category="coverage"
        ) from exc
    if not validated["target_met"]:
        detail = report.get("error") or report.get("warning") or (
            "required Python oracle executable-line proxy coverage was not met"
        )
        raise PreRtlGateError(
            "oracle_line_coverage", str(detail), category="coverage"
        )


def _extract_json(response: str) -> dict[str, Any]:
    decoder = json.JSONDecoder()
    for index, character in enumerate(response):
        if character != "{":
            continue
        try:
            value, _ = decoder.raw_decode(response[index:])
        except json.JSONDecodeError:
            continue
        if isinstance(value, dict):
            return value
    raise ValueError("coverage refinement response did not contain a JSON object")


def _expanded_cycles(scenario: dict[str, Any]) -> int:
    return sum(int(cycle.get("repeat", 1)) for cycle in scenario.get("cycles", []))


def _scenario_fingerprint(scenario: dict[str, Any]) -> str:
    return content_hash(
        {
            "reset_before": scenario.get("reset_before", True),
            "requirement_ids": sorted(scenario.get("requirement_ids", [])),
            "cycles": scenario.get("cycles", []),
        }
    )


def _validate_additions(
    spec: DesignSpec,
    additions: Any,
    existing_ids: set[str],
    requirements: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    if not isinstance(additions, list) or not 1 <= len(additions) <= 16:
        raise ValueError("scenarios must contain 1 to 16 declaration-only tests")
    raw = json.loads(json.dumps(spec.raw))
    verification = dict(raw["verification"])
    requirement_map = {row["id"]: row for row in requirements}
    referenced = {
        requirement_id
        for scenario in additions
        if isinstance(scenario, dict)
        for requirement_id in scenario.get("requirement_ids", [])
        if isinstance(requirement_id, str)
    }
    unknown = sorted(referenced - set(requirement_map))
    if unknown:
        raise ValueError(
            "coverage scenarios reference unknown requirements: " + ", ".join(unknown)
        )
    verification["requirements"] = [
        requirement_map[requirement_id] for requirement_id in sorted(referenced)
    ]
    verification["directed_tests"] = additions
    expanded = sum(
        _expanded_cycles(scenario) for scenario in verification["directed_tests"]
    )
    verification["directed_test_budget"] = min(256, max(1, expanded))
    raw["verification"] = verification
    normalized = validate_spec(raw).verification["directed_tests"]
    validated = normalized
    for scenario in validated:
        if scenario["id"] in existing_ids:
            raise ValueError(f"duplicate scenario id: {scenario['id']}")
        existing_ids.add(scenario["id"])
    return validated


def _prompt(
    spec: DesignSpec,
    contract: dict[str, Any],
    plan: dict[str, Any],
    oracle: str,
    coverage: dict[str, Any],
    cycle_budget: int,
) -> str:
    numbered = "\n".join(
        f"{index:04d}: {line}"
        for index, line in enumerate(oracle.splitlines(), 1)
        if index in set(coverage.get("missing_lines", []))
    )
    existing = [
        {
            "id": scenario.get("id"),
            "requirement_ids": scenario.get("requirement_ids", []),
            "cycles": scenario.get("cycles", []),
        }
        for scenario in plan.get("scenarios", [])
    ]
    return f"""Add input-only scenarios that exercise currently uncovered behavior
in a frozen Python hardware oracle. Return one JSON object and no prose:
{{"scenarios":[{{"id":"COV_NAME","description":"...","requirement_ids":["REQ_ID"],"reset_before":true,"cycles":[{{"drive":{{"input":0}},"repeat":1}}]}}]}}

Hard rules:
- Scenarios may contain only declared non-clock inputs. Never provide expected
  outputs, Python, RTL, assertions, expressions, or executable text.
- Values must fit port widths. Every scenario must reference existing requirement IDs.
- Use at most {cycle_budget} expanded cycles in total and at most 16 scenarios.
- Exercise semantic branches, boundaries, priorities, latency, repeated holds,
  and back-to-back operations implicated by the missing oracle lines.
- Do not duplicate an existing input sequence.

CONTRACT:
{canonical_json(contract)}

PORTS:
{canonical_json([{"name": port.name, "direction": port.direction, "width": port.width} for port in spec.ports])}

CURRENT COVERAGE:
{canonical_json(coverage)}

UNCOVERED ORACLE SOURCE LINES (diagnostic only):
{numbered or "none"}

EXISTING SCENARIOS:
{canonical_json(existing)}
"""


def refine_oracle_coverage(
    spec: DesignSpec,
    backend,
    contract: dict[str, Any],
    plan: dict[str, Any],
    python_model: str,
    expected_trace: dict[str, Any],
    artifact_dir: Path,
    *,
    progress: ProgressCallback | None = None,
) -> tuple[dict[str, Any], dict[str, Any], dict[str, Any]]:
    """Improve oracle execution coverage before the test bundle is frozen.

    Model output is data only. The controller validates every input value and
    regenerates all expected values with the selected Python oracle.
    """
    artifact_dir.mkdir(parents=True, exist_ok=True)
    active_plan = json.loads(json.dumps(plan))
    active_trace = json.loads(json.dumps(expected_trace))
    target = spec.verification["oracle_coverage_target_pct"]
    mode = spec.verification["oracle_line_coverage_mode"]
    configured_rounds = spec.verification["oracle_coverage_rounds"]
    max_rounds = 0 if mode == "off" else configured_rounds
    remaining_cycles = spec.verification["oracle_coverage_cycle_budget"]
    existing_ids = {
        str(scenario.get("id")) for scenario in active_plan.get("scenarios", [])
    }
    fingerprints = {
        _scenario_fingerprint(scenario)
        for scenario in active_plan.get("scenarios", [])
    }
    history: list[dict[str, Any]] = []
    refinement_error: str | None = None
    stop_reason = "disabled" if mode == "off" else "rounds_exhausted"

    for round_index in range(1, max_rounds + 1):
        coverage = active_trace.get("oracle_line_coverage") or {}
        if float(coverage.get("percent", 0.0)) >= target:
            stop_reason = "target_met"
            break
        if remaining_cycles <= 0:
            stop_reason = "cycle_budget_exhausted"
            break
        if progress:
            progress(
                "coverage_refining",
                {
                    "status": "running",
                    "round": round_index,
                    "max_rounds": max_rounds,
                    "coverage_percent": coverage.get("percent"),
                    "coverage_target_pct": target,
                    "target_met": False,
                    "remaining_cycle_budget": remaining_cycles,
                },
            )
        row: dict[str, Any] = {
            "round": round_index,
            "before": coverage,
            "accepted_scenarios": [],
        }
        try:
            response = backend.generate(
                _prompt(
                    spec,
                    contract,
                    active_plan,
                    python_model,
                    coverage,
                    remaining_cycles,
                ),
                n=1,
                temperature=0.2,
                max_tokens=6000,
                system_prompt=(
                    "You are a coverage-guided hardware test planner. Return only "
                    "declaration-only JSON input scenarios; never return expected outputs or code."
                ),
            )[0]
            (artifact_dir / f"round-{round_index:02d}-response.txt").write_text(
                response, encoding="utf-8"
            )
            proposed = _extract_json(response).get("scenarios")
            candidate_existing_ids = set(existing_ids)
            additions = _validate_additions(
                spec,
                proposed,
                candidate_existing_ids,
                contract.get("requirements", []),
            )
            accepted: list[dict[str, Any]] = []
            candidate_remaining_cycles = remaining_cycles
            candidate_fingerprints = set(fingerprints)
            for scenario in additions:
                cycles = _expanded_cycles(scenario)
                fingerprint = _scenario_fingerprint(scenario)
                if (
                    fingerprint in candidate_fingerprints
                    or cycles > candidate_remaining_cycles
                ):
                    continue
                scenario = dict(scenario)
                scenario["source"] = "oracle_coverage_refinement"
                accepted.append(scenario)
                candidate_fingerprints.add(fingerprint)
                candidate_remaining_cycles -= cycles
            if not accepted:
                raise ValueError("no unique scenarios fit the remaining cycle budget")
            candidate_plan = json.loads(json.dumps(active_plan))
            candidate_plan["scenarios"].extend(accepted)
            candidate_trace = generate_expected_trace(
                spec,
                python_model,
                candidate_plan,
                artifact_dir / f"round-{round_index:02d}-oracle",
            )
            before = float(coverage.get("percent", 0.0))
            after = float(
                (candidate_trace.get("oracle_line_coverage") or {}).get(
                    "percent", 0.0
                )
            )
            row["candidate_scenarios"] = [scenario["id"] for scenario in accepted]
            row["after"] = candidate_trace.get("oracle_line_coverage")
            row["improved"] = after > before
            if after <= before:
                history.append(row)
                stop_reason = "no_improvement"
                break
            row["accepted_scenarios"] = list(row["candidate_scenarios"])
            active_plan = candidate_plan
            active_trace = candidate_trace
            remaining_cycles = candidate_remaining_cycles
            existing_ids = candidate_existing_ids
            fingerprints = candidate_fingerprints
            history.append(row)
        except NonRepairableInfrastructureError:
            raise
        except Exception as exc:
            refinement_error = str(exc)[:2000]
            row["error"] = refinement_error
            history.append(row)
            stop_reason = "error"
            break

    final_coverage = active_trace.get("oracle_line_coverage") or {}
    final_percent = float(final_coverage.get("percent", 0.0))
    target_met = final_percent >= target
    if mode != "off" and target_met:
        stop_reason = "target_met"
    warning = None
    if mode != "off" and not target_met:
        warning = (
            f"oracle executable-line coverage target was not met: "
            f"{final_percent:g}% < {target:g}% "
            f"(remaining cycle budget: {remaining_cycles})"
        )
    status = (
        "not_requested"
        if mode == "off"
        else "error"
        if refinement_error
        else "passed"
        if target_met
        else "warning"
    )
    report = {
        "schema_version": 2,
        "status": status,
        "mode": mode,
        "metric": ORACLE_COVERAGE_METRIC,
        "rtl_functional_coverage": False,
        "limitation": ORACLE_COVERAGE_LIMITATION,
        "target_met": target_met,
        "stop_reason": stop_reason,
        "warning": warning,
        "error": refinement_error,
        "target_percent": target,
        "initial": expected_trace.get("oracle_line_coverage"),
        "final": final_coverage,
        "rounds": history,
        "remaining_cycle_budget": remaining_cycles,
        "added_scenario_count": len(active_plan.get("scenarios", []))
        - len(plan.get("scenarios", [])),
        "final_testplan_sha256": content_hash(active_plan),
        "final_expected_trace_sha256": content_hash(active_trace),
        "final_oracle_sha256": content_hash(python_model),
        "evidence_bindings": {
            "testplan_sha256": content_hash(active_plan),
            "expected_trace_sha256": content_hash(active_trace),
            "oracle_sha256": content_hash(python_model),
        },
    }
    report_path = artifact_dir / "coverage-refinement.json"
    report_path.write_text(
        json.dumps(report, indent=2, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )
    if progress:
        progress(
            (
                "coverage_refinement_error"
                if refinement_error
                else "coverage_refinement_disabled"
                if mode == "off"
                else "coverage_refinement_completed"
                if target_met
                else "coverage_refinement_warning"
            ),
            {
                "status": status,
                "round": len(history),
                "max_rounds": max_rounds,
                "coverage_percent": final_percent,
                "coverage_target_pct": target,
                "target_met": target_met,
                "remaining_cycle_budget": remaining_cycles,
                "added_scenario_count": report["added_scenario_count"],
                "stop_reason": stop_reason,
                "warning": warning,
                "error": refinement_error,
                "report": str(report_path),
            },
        )
    return active_plan, active_trace, report
