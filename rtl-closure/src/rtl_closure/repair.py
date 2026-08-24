"""Counterexample-guided RTL repair with immutable gates and rollback."""

from __future__ import annotations

import json
import re
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

from .adaptive import (
    behavioral_coverage,
    build_suite_epoch,
    derive_counterexample_scenarios,
    suite_hash,
    verify_epoch_chain,
)
from .errors import NonRepairableInfrastructureError, RepairExhaustedError
from .contract import canonical_json, content_hash, verify_frozen
from .extraction import extract_verilog
from .feedback import (
    apply_block_edits,
    build_failure_packet,
    counterexample_from_packet,
    dependency_slice,
    extract_rtl_blocks,
)
from .formal import (
    merge_formal_result,
    merge_lint_result,
    run_bounded_trace_formal,
    run_verilator_lint,
)
from .oracle import revalidate_oracle_bundle_quorum
from .runtime import sandbox_image_ref
from .sandbox import (
    check_exact_interface,
    check_rtl_policy,
    normalize_top_module,
    run_architecture_audit,
    yosys_check,
)
from .spec import DesignSpec, render_interface
from .universal import merge_universal_result, run_universal_properties
from .verification import (
    derive_independent_stimuli,
    independent_input_widths,
    validate_independent_stimuli,
    validate_independent_transcript,
    verify_rtl,
)


ProgressCallback = Callable[[str, dict[str, Any]], None]


# This bounds only the counterexample evidence embedded in a model request. The
# complete verifier response remains in independent-verification.json.
INDEPENDENT_PROMPT_EVIDENCE_MAX_BYTES = 32 * 1024
INDEPENDENT_PROMPT_VALUE_MAX_BYTES = 4 * 1024


@dataclass(frozen=True)
class RepairOutcome:
    rtl: str
    verification: dict[str, Any]
    history: list[dict[str, Any]]
    counterexamples: list[dict[str, Any]]
    repaired: bool
    exhausted: bool


class VerificationInfrastructureError(RuntimeError):
    """A required verification provider could not reach a verdict."""

    failure_class = "verification_infrastructure"
    failure_gate = "verification_provider"

    def __init__(
        self, message: str, *, error: dict[str, Any] | None = None
    ) -> None:
        super().__init__(message)
        self.error = error or {
            "code": "verification_infrastructure_error",
            "category": "infrastructure",
            "repairable": False,
            "message": message,
            "context": {},
        }
        self.failure_code = str(
            self.error.get("code") or "verification_infrastructure_error"
        )


class NonRepairableVerificationError(VerificationInfrastructureError):
    """Verification evidence/configuration cannot be changed by editing RTL."""


def _nonrepairable_formal_error(result: dict[str, Any]) -> dict[str, Any] | None:
    formal = result.get("formal") or {}
    if (
        result.get("failure_class") == "formal_counterexample"
        and isinstance(formal, dict)
        and formal.get("counterexamples")
    ):
        # A concrete behavioral counterexample is actionable even when another
        # obligation was inconclusive. The next candidate still has to rerun the
        # complete required suite before release.
        return None
    error = result.get("verification_error") or formal.get("error")
    failure_class = str(result.get("failure_class") or "")
    if isinstance(error, dict) and error.get("repairable") is False:
        return dict(error)
    infrastructure = result.get("infrastructure_failure")
    if isinstance(infrastructure, dict):
        provider = str(infrastructure.get("provider") or "verification_provider")
        return {
            "code": f"{provider}_infrastructure_error",
            "category": "infrastructure",
            "domain": "verification",
            "repairable": False,
            "repairable_by_rtl": False,
            "retryable": True,
            "user_action_required": False,
            "provider": provider,
            "message": str(
                infrastructure.get("detail") or "verification provider was inconclusive"
            ),
            "context": {"status": infrastructure.get("status")},
            "evidence": [],
        }
    if failure_class in {
        "formal_incomplete",
        "formal_capacity",
        "formal_configuration",
    }:
        return {
            "code": failure_class,
            "category": "evidence" if failure_class == "formal_incomplete" else "configuration",
            "repairable": False,
            "repairable_by_rtl": False,
            "retryable": failure_class != "formal_configuration",
            "user_action_required": failure_class == "formal_configuration",
            "provider": "yosys_bounded_trace",
            "message": str(result.get("detail") or failure_class),
            "context": {},
        }
    return None


def _raise_if_nonrepairable_verification(
    result: dict[str, Any], *, artifact: Path
) -> None:
    error = _nonrepairable_formal_error(result)
    if error is None:
        return
    error.setdefault("context", {})
    error["context"] = {**error["context"], "artifact": str(artifact)}
    raise NonRepairableVerificationError(
        "non-repairable formal verification failure: "
        f"code={error.get('code')}, category={error.get('category')}, "
        f"detail={error.get('message')}, artifact={artifact}",
        error=error,
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
    raise ValueError("repair response did not contain a JSON object")


def verification_score(result: dict[str, Any]) -> tuple[int, int, int]:
    if result.get("passed"):
        return (2, int(result.get("total_checks") or 0) + 1, 0)
    failure_class = str(result.get("failure_class") or "")
    if failure_class in {
        "compile_error",
        "interface_error",
        "policy_error",
        "synthesis_error",
        "simulation_error",
        "simulation_timeout",
        "result_parse_error",
        "formal_unknown",
        "formal_error",
    }:
        return (0, 0, -int(result.get("mismatched") or 1))
    first = result.get("first_failure_ordinal")
    return (
        1,
        int(first) if first is not None else 0,
        -int(result.get("mismatched") or 0),
    )


def candidate_improves(
    champion: dict[str, Any], candidate: dict[str, Any]
) -> tuple[bool, str]:
    champion_suite = champion.get("suite_sha256")
    candidate_suite = candidate.get("suite_sha256")
    if not champion_suite or not candidate_suite:
        return False, "candidate or champion is missing the required suite hash"
    if champion_suite != candidate_suite:
        return False, "candidate and champion were evaluated on different suite hashes"
    champion_passes = set(champion.get("passed_check_ids") or [])
    candidate_passes = set(candidate.get("passed_check_ids") or [])
    regressed = champion_passes - candidate_passes
    if regressed:
        sample = sorted(regressed)[:5]
        return False, f"historical regression in {len(regressed)} checks: {sample}"
    old_score = verification_score(champion)
    new_score = verification_score(candidate)
    if new_score <= old_score:
        return False, f"score did not strictly improve: {old_score} -> {new_score}"
    return True, "strict improvement without historical regression"


def _failure_result(failure_class: str, detail: str) -> dict[str, Any]:
    return {
        "passed": False,
        "failure_class": failure_class,
        "detail": detail[-4000:],
        "total_checks": 0,
        "mismatched": 1,
        "first_failure_ordinal": 0,
        "mismatches": [],
        "passed_check_ids": [],
        "actual_trace": [],
    }


def verify_frozen_assets(
    *,
    contract: dict[str, Any],
    plan: dict[str, Any],
    expected_trace: dict[str, Any],
    contract_path: Path,
    plan_path: Path,
    oracle_path: Path,
    expected_path: Path,
    manifest_path: Path,
    frozen_manifest: dict[str, Any],
) -> None:
    """Fail closed if any immutable verification input changed."""
    required = {
        "schema_version",
        "contract_sha256",
        "testplan_sha256",
        "oracle_sha256",
        "expected_trace_sha256",
        "bundle_sha256",
    }
    if set(frozen_manifest) != required or frozen_manifest.get("schema_version") != 1:
        raise RuntimeError("frozen manifest has an invalid schema")
    bundle = {key: frozen_manifest[key] for key in required - {"bundle_sha256"}}
    if content_hash(bundle) != frozen_manifest["bundle_sha256"]:
        raise RuntimeError("frozen manifest bundle hash is invalid")
    expected_hashes = {
        "contract_sha256": content_hash(contract),
        "testplan_sha256": content_hash(plan),
        "expected_trace_sha256": content_hash(expected_trace),
    }
    for key, actual in expected_hashes.items():
        if actual != frozen_manifest[key]:
            raise RuntimeError(
                f"in-memory frozen asset disagrees with manifest: {key}; "
                f"expected {frozen_manifest[key]}, got {actual}"
            )
    verify_frozen(contract_path, frozen_manifest["contract_sha256"])
    verify_frozen(plan_path, frozen_manifest["testplan_sha256"])
    verify_frozen(expected_path, frozen_manifest["expected_trace_sha256"])
    verify_frozen(manifest_path, content_hash(frozen_manifest))
    try:
        oracle_content = oracle_path.read_text(encoding="utf-8")
    except OSError as exc:
        raise RuntimeError(f"frozen Python oracle is unreadable: {oracle_path}: {exc}") from exc
    actual_oracle_hash = content_hash(oracle_content)
    if actual_oracle_hash != frozen_manifest["oracle_sha256"]:
        raise RuntimeError(
            "frozen Python oracle changed during repair; "
            f"expected {frozen_manifest['oracle_sha256']}, got {actual_oracle_hash}"
        )


def _evaluate(
    spec: DesignSpec,
    rtl: str,
    contract: dict[str, Any],
    plan: dict[str, Any],
    expected_trace: dict[str, Any],
    attempt_dir: Path,
    *,
    contract_path: Path,
    plan_path: Path,
    oracle_path: Path,
    expected_path: Path,
    manifest_path: Path,
    frozen_manifest: dict[str, Any],
    evaluation_plan: dict[str, Any] | None = None,
    evaluation_expected_trace: dict[str, Any] | None = None,
    progress: ProgressCallback | None = None,
) -> tuple[str, dict[str, Any]]:
    attempt_dir.mkdir(parents=True, exist_ok=True)
    (attempt_dir / "candidate.sv").write_text(rtl + "\n", encoding="utf-8")
    verify_frozen_assets(
        contract=contract,
        plan=plan,
        expected_trace=expected_trace,
        contract_path=contract_path,
        plan_path=plan_path,
        oracle_path=oracle_path,
        expected_path=expected_path,
        manifest_path=manifest_path,
        frozen_manifest=frozen_manifest,
    )
    active_plan = evaluation_plan or plan
    active_expected = evaluation_expected_trace or expected_trace
    active_suite_hash = suite_hash(active_plan, active_expected)

    def annotate(
        result: dict[str, Any], evaluated_rtl: str | None = None
    ) -> dict[str, Any]:
        result.setdefault("rtl_sha256", content_hash(evaluated_rtl or rtl))
        result.setdefault("suite_sha256", active_suite_hash)
        result["coverage"] = behavioral_coverage(contract, active_plan, result)
        return result

    try:
        normalized = normalize_top_module(rtl, spec.name)
        check_rtl_policy(normalized)
    except Exception as exc:
        result = annotate(_failure_result("policy_error", str(exc)))
        (attempt_dir / "verification-result.json").write_text(
            json.dumps(result, indent=2) + "\n", encoding="utf-8"
        )
        return rtl, result
    try:
        check_exact_interface(normalized, spec.ports, spec.name)
    except Exception as exc:
        result = annotate(_failure_result("interface_error", str(exc)), normalized)
        (attempt_dir / "verification-result.json").write_text(
            json.dumps(result, indent=2) + "\n", encoding="utf-8"
        )
        return normalized, result
    try:
        yosys_log = yosys_check(normalized, spec.name)
        (attempt_dir / "yosys.log").write_text(yosys_log + "\n", encoding="utf-8")
    except NonRepairableInfrastructureError:
        raise
    except Exception as exc:
        result = annotate(_failure_result("synthesis_error", str(exc)), normalized)
        (attempt_dir / "verification-result.json").write_text(
            json.dumps(result, indent=2) + "\n", encoding="utf-8"
        )
        return normalized, result
    architecture_report: dict[str, Any] | None = None
    architecture_pass_ids: list[str] = []
    if spec.architecture:
        if progress:
            progress(
                "architecture_audit_running",
                {
                    "action": "audit_architecture",
                    "latest_attempt": str(attempt_dir),
                    "result": None,
                },
            )
        architecture_report = run_architecture_audit(
            normalized, spec.name, spec.architecture
        )
        architecture_path = attempt_dir / "architecture-audit.json"
        architecture_path.write_text(
            json.dumps(architecture_report, indent=2, ensure_ascii=False) + "\n",
            encoding="utf-8",
        )
        failed_architecture = [
            row
            for row in architecture_report.get("checks", [])
            if row.get("status") == "fail"
        ]
        architecture_pass_ids = [
            f"architecture:{row['id']}"
            for row in architecture_report.get("checks", [])
            if row.get("status") == "pass"
        ]
        if progress:
            progress(
                "architecture_audit_completed",
                {
                    "action": "audit_architecture",
                    "latest_attempt": str(attempt_dir),
                    "result": "failed" if failed_architecture else "passed",
                    "failure_class": (
                        "architecture_constraint" if failed_architecture else None
                    ),
                    "failed_checks": [row["id"] for row in failed_architecture],
                    "architecture_artifact": str(architecture_path),
                },
            )
        if failed_architecture:
            all_checks = architecture_report.get("checks", [])
            failed_ids = {row["id"] for row in failed_architecture}
            mismatches = [
                {
                    "check_id": f"architecture:{row['id']}",
                    "ordinal": index,
                    "scenario_index": None,
                    "scenario_id": None,
                    "requirement_ids": [],
                    "cycle": None,
                    "phase": "static_architecture",
                    "signal": row["id"],
                    "expected": row.get("expected"),
                    "actual": row.get("actual"),
                    "inputs": {},
                    "failure_class": "architecture_constraint",
                }
                for index, row in enumerate(all_checks)
                if row["id"] in failed_ids
            ]
            result = {
                "passed": False,
                "failure_class": "architecture_constraint",
                "detail": "architecture constraints failed: "
                + ", ".join(row["id"] for row in failed_architecture),
                "total_checks": len(all_checks),
                "mismatched": len(failed_architecture),
                "first_failure_ordinal": mismatches[0]["ordinal"],
                "mismatches": mismatches,
                "passed_check_ids": architecture_pass_ids,
                "actual_trace": [],
                "architecture_audit": architecture_report,
            }
            result = annotate(result, normalized)
            (attempt_dir / "verification-result.json").write_text(
                json.dumps(result, indent=2, sort_keys=True, ensure_ascii=False)
                + "\n",
                encoding="utf-8",
            )
            return normalized, result
    port_names = {port.name for port in spec.ports}
    internal_signals = sorted(
        {
            signal
            for block in extract_rtl_blocks(normalized)
            for signal in block.writes
            if signal not in port_names
        }
    )[: spec.verification["internal_trace_signals"]]
    result = verify_rtl(
        spec,
        normalized,
        active_plan,
        active_expected,
        attempt_dir,
        debug_signals=internal_signals,
    )
    if architecture_report is not None:
        result["architecture_audit"] = architecture_report
        result["total_checks"] = int(result.get("total_checks") or 0) + len(
            architecture_report.get("checks", [])
        )
        passed_check_ids = list(result.get("passed_check_ids") or [])
        passed_check_ids.extend(
            check_id
            for check_id in architecture_pass_ids
            if check_id not in passed_check_ids
        )
        result["passed_check_ids"] = passed_check_ids
    if result.get("passed"):
        formal_mode = spec.verification["formal_mode"]
        formal_enabled = formal_mode != "off"
        lint_enabled = spec.verification["verilator_lint"]
        providers_enabled = formal_enabled or lint_enabled
        formal_dir = attempt_dir / "formal-evidence"
        if progress and providers_enabled:
            progress(
                "formal_running",
                {
                    "action": "verification_providers",
                    "latest_attempt": str(attempt_dir),
                    "formal_mode": formal_mode,
                    "formal_status": "running" if formal_enabled else "not_run",
                    "verilator_status": "running" if lint_enabled else "not_run",
                    "result": None,
                },
            )
        lint_report = None
        if lint_enabled:
            lint_report = run_verilator_lint(
                spec,
                normalized,
                formal_dir,
                timeout=spec.verification["formal_timeout_seconds"],
            )
            if lint_report.get("status") in {"unknown", "error"}:
                (formal_dir / "verilator-lint-attempt-01.json").write_text(
                    json.dumps(lint_report, indent=2, ensure_ascii=False) + "\n",
                    encoding="utf-8",
                )
                if progress:
                    progress(
                        "formal_retrying",
                        {
                            "action": "retry_verilator",
                            "latest_attempt": str(attempt_dir),
                            "formal_mode": formal_mode,
                            "formal_status": "running" if formal_enabled else "not_run",
                            "verilator_status": lint_report.get("status"),
                            "formal_detail": lint_report.get("detail"),
                            "result": "retrying",
                        },
                    )
                lint_report = run_verilator_lint(
                    spec,
                    normalized,
                    formal_dir,
                    timeout=spec.verification["formal_timeout_seconds"],
                )
        formal_report = None
        if formal_enabled:
            formal_started = time.monotonic()
            formal_total_budget = spec.verification["formal_total_timeout_seconds"]
            formal_report = run_bounded_trace_formal(
                spec,
                normalized,
                active_plan,
                active_expected,
                formal_dir,
                depth=spec.verification["formal_depth"],
                timeout=spec.verification["formal_timeout_seconds"],
                total_timeout=formal_total_budget,
            )
            if formal_report.get("status") in {"unknown", "error"}:
                (formal_dir / "formal-report-attempt-01.json").write_text(
                    json.dumps(formal_report, indent=2, ensure_ascii=False) + "\n",
                    encoding="utf-8",
                )
                if progress:
                    progress(
                        "formal_retrying",
                        {
                            "action": "retry_formal",
                            "latest_attempt": str(attempt_dir),
                            "formal_mode": formal_mode,
                            "formal_status": formal_report.get("status"),
                            "verilator_status": (lint_report or {}).get("status"),
                            "formal_detail": formal_report.get("detail"),
                            "result": "retrying",
                        },
                    )
                remaining_total = int(
                    formal_total_budget - (time.monotonic() - formal_started)
                )
                if remaining_total > 0:
                    formal_report = run_bounded_trace_formal(
                        spec,
                        normalized,
                        active_plan,
                        active_expected,
                        formal_dir,
                        depth=spec.verification["formal_depth"],
                        timeout=spec.verification["formal_timeout_seconds"],
                        total_timeout=remaining_total,
                    )
            result = merge_formal_result(
                result,
                formal_report,
                contract,
                active_plan,
                required=formal_mode == "required",
            )
        if lint_report is not None:
            result = merge_lint_result(
                result,
                lint_report,
                contract,
                active_plan,
                # Enabling lint makes its verdict an independent release gate;
                # formal advisory/required mode does not weaken that contract.
                required=True,
            )
        (attempt_dir / "verification-result.json").write_text(
            json.dumps(result, indent=2, sort_keys=True, ensure_ascii=False) + "\n",
            encoding="utf-8",
        )
        if progress and providers_enabled:
            formal_status = (result.get("formal") or {}).get("status", "not_run")
            lint_status = (result.get("verilator_lint") or {}).get(
                "status", "not_run"
            )
            inconclusive = (
                formal_mode == "required"
                and formal_status in {"unknown", "error", "unsupported"}
            ) or (
                lint_enabled and lint_status in {"unknown", "error"}
            )
            event_detail = (
                (result.get("verilator_lint") or {}).get("detail")
                if lint_status in {"unknown", "error"}
                else (result.get("formal") or {}).get("detail")
                or (lint_report or {}).get("detail")
            )
            progress(
                "formal_completed",
                {
                    "action": "verification_providers_complete",
                    "latest_attempt": str(attempt_dir),
                    "formal_mode": spec.verification["formal_mode"],
                    "formal_status": formal_status,
                    "verilator_status": lint_status,
                    "formal_detail": event_detail,
                    "formal_artifact": (result.get("formal") or {}).get("artifact"),
                    "result": (
                        "inconclusive"
                        if inconclusive
                        else "passed"
                        if result.get("passed")
                        else "failed"
                    ),
                },
            )
        provider_status = (result.get("formal") or {}).get("status")
        lint_status = (result.get("verilator_lint") or {}).get("status")
        lint_inconclusive = lint_enabled and lint_status in {"unknown", "error"}
        formal_inconclusive = (
            formal_mode == "required"
            and provider_status in {"unknown", "error", "unsupported"}
        )
        if lint_inconclusive or formal_inconclusive:
            provider = (
                "verilator_lint"
                if lint_inconclusive
                else (result.get("formal") or {}).get("provider")
            )
            status = lint_status if lint_inconclusive else provider_status
            detail = (
                (result.get("verilator_lint") or {}).get("detail")
                if lint_inconclusive
                else (result.get("formal") or {}).get("detail")
            )
            structured_error = (
                {
                    "code": f"verilator_lint_{status}",
                    "category": "infrastructure",
                    "domain": "verification",
                    "repairable": False,
                    "repairable_by_rtl": False,
                    "retryable": True,
                    "provider": "verilator_lint",
                    "message": str(detail),
                    "context": {"artifact": str(attempt_dir)},
                    "evidence": [],
                }
                if lint_inconclusive
                else (result.get("formal") or {}).get("error")
                or {
                    "code": f"formal_provider_{status}",
                    "category": "infrastructure",
                    "repairable": False,
                    "provider": provider,
                    "message": str(detail),
                    "context": {"artifact": str(attempt_dir)},
                }
            )
            raise VerificationInfrastructureError(
                "required verification provider remained inconclusive after retry: "
                f"provider={provider}, status={status}, "
                f"detail={str(detail)[:2000]}, artifact={attempt_dir}",
                error=structured_error,
            )
        _raise_if_nonrepairable_verification(result, artifact=attempt_dir)
        universal_mode = spec.verification["universal_mode"]
        universal_properties = spec.verification["universal_properties"]
        universal_dir = attempt_dir / "universal-evidence"
        if progress and universal_mode != "off":
            progress(
                "universal_formal_running",
                {
                    "action": "prove_universal_properties",
                    "latest_attempt": str(attempt_dir),
                    "universal_mode": universal_mode,
                    "universal_status": "running",
                    "property_count": len(universal_properties),
                    "result": None,
                },
            )
        universal_report = run_universal_properties(
            normalized,
            spec.name,
            spec.ports,
            spec.clock_port,
            spec.reset,
            universal_properties if universal_mode != "off" else [],
            universal_dir,
            timeout=spec.verification["universal_timeout_seconds"],
            induction_depth=spec.verification["universal_induction_depth"],
        )
        if universal_report.get("status") in {"unknown", "error"}:
            first_report = universal_dir / "universal-properties.json"
            if first_report.is_file():
                (universal_dir / "universal-properties-attempt-01.json").write_text(
                    first_report.read_text(encoding="utf-8"),
                    encoding="utf-8",
                )
            if progress:
                progress(
                    "universal_formal_retrying",
                    {
                        "action": "retry_universal_properties",
                        "latest_attempt": str(attempt_dir),
                        "universal_mode": universal_mode,
                        "universal_status": universal_report.get("status"),
                        "universal_detail": universal_report.get("detail"),
                        "result": "retrying",
                    },
                )
            universal_report = run_universal_properties(
                normalized,
                spec.name,
                spec.ports,
                spec.clock_port,
                spec.reset,
                universal_properties,
                universal_dir,
                timeout=spec.verification["universal_timeout_seconds"],
                induction_depth=spec.verification["universal_induction_depth"],
            )
        result = merge_universal_result(
            result,
            universal_report,
            required=universal_mode == "required",
        )
        if progress and universal_mode != "off":
            progress(
                "universal_formal_completed",
                {
                    "action": "prove_universal_properties",
                    "latest_attempt": str(attempt_dir),
                    "universal_mode": universal_mode,
                    "universal_status": universal_report.get("status"),
                    "universal_proof": universal_report.get("universal_proof"),
                    "passed_property_count": len(
                        universal_report.get("passed_property_ids") or []
                    ),
                    "failed_property_ids": universal_report.get(
                        "failed_property_ids"
                    ),
                    "universal_artifact": str(
                        universal_dir / "universal-properties.json"
                    ),
                    "result": (
                        "passed"
                        if universal_report.get("status") == "pass"
                        else "failed"
                        if universal_report.get("status") == "fail"
                        else "inconclusive"
                    ),
                },
            )
        if (
            universal_mode == "required"
            and universal_report.get("status") in {"unknown", "error", "not_requested"}
            and not universal_report.get("failed_property_ids")
        ):
            (attempt_dir / "verification-result.json").write_text(
                json.dumps(result, indent=2, sort_keys=True, ensure_ascii=False)
                + "\n",
                encoding="utf-8",
            )
            raise VerificationInfrastructureError(
                "required universal property provider remained inconclusive after retry: "
                f"status={universal_report.get('status')}, "
                f"detail={str(universal_report.get('detail'))[:2000]}, "
                f"artifact={universal_dir / 'universal-properties.json'}"
            )
    (attempt_dir / "verification-result.json").write_text(
        json.dumps(result, indent=2, sort_keys=True, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )
    verify_frozen_assets(
        contract=contract,
        plan=plan,
        expected_trace=expected_trace,
        contract_path=contract_path,
        plan_path=plan_path,
        oracle_path=oracle_path,
        expected_path=expected_path,
        manifest_path=manifest_path,
        frozen_manifest=frozen_manifest,
    )
    return normalized, annotate(result)


def _local_prompt(
    contract: dict[str, Any],
    packet: dict[str, Any],
    rtl: str,
    history: list[dict[str, Any]],
    counterexamples: list[dict[str, Any]],
) -> str:
    compact_history = [
        {
            "round": item.get("round"),
            "candidate": item.get("candidate"),
            "accepted": item.get("accepted"),
            "reason": item.get("reason"),
            "diagnosis": item.get("diagnosis"),
        }
        for item in history[-8:]
    ]
    compact_counterexamples = [
        {
            key: item.get(key)
            for key in (
                "fingerprint",
                "source",
                "failure_class",
                "scenario_id",
                "requirement_ids",
                "first_failure_cycle",
                "signal",
                "expected",
                "actual",
                "inputs",
                "internal_state_delta",
                "mismatch_cluster",
            )
        }
        for item in counterexamples[-12:]
    ]
    return f"""Repair the RTL against the immutable contract and failure packet.
Return one JSON object and no Markdown or prose outside it:
{{
  "diagnosis": "specific root cause tied to the trace and RTL",
  "confidence": 0.0,
  "edits": [{{"block_id": "B001", "replacement": "complete always/assign block"}}]
}}

Hard rules:
- Only block_id values present in suspect_blocks are authorized.
- replacement contains exactly one complete block of the same kind.
- Do not add ports, modules, declarations, initial blocks, delays, system tasks,
  vendor primitives, or writes to signals outside the original block write-set.
- Preserve all behavior unrelated to the failing requirement.
- A latency hint is evidence, not permission to change the interface or contract.

IMMUTABLE CONTRACT:
{canonical_json(contract)}

FAILURE PACKET:
{canonical_json(packet)}

PREVIOUS REJECTED/ACCEPTED ATTEMPTS:
{canonical_json(compact_history)}

ACCUMULATED CHAMPION COUNTEREXAMPLES (oldest behavior remains a regression obligation):
{canonical_json(compact_counterexamples)}

CURRENT RTL:
```systemverilog
{rtl}
```
"""


def _regeneration_prompt(
    spec: DesignSpec,
    contract: dict[str, Any],
    packet: dict[str, Any],
    rtl: str,
    counterexamples: list[dict[str, Any]],
) -> str:
    return f"""Regenerate a corrected synthesizable SystemVerilog implementation.
The contract, interface, Python oracle, and test plan are immutable. Return the
complete RTL inside <answer> and a systemverilog code fence.

Exact interface:
{render_interface(spec)}

Machine-checked architecture constraints:
{canonical_json(spec.architecture)}

Contract:
{canonical_json(contract)}

Latest failure packet:
{canonical_json(packet)}

Accumulated champion counterexamples:
{canonical_json(counterexamples[-12:])}

Current failing RTL (use only as diagnostic context):
```systemverilog
{rtl}
```

Do not use initial blocks, delays, system tasks, file I/O, DPI, force/release,
or vendor primitives. Preserve exact top-level port names, order, directions,
widths, and signedness.
"""


def persist_verification_evidence(
    result: dict[str, Any], run_dir: Path
) -> None:
    """Promote the accepted candidate's provider evidence to the run root."""
    formal = result.get("formal")
    if not isinstance(formal, dict):
        formal = None
    if formal is not None:
        (run_dir / "formal-report.json").write_text(
            json.dumps(formal, indent=2, ensure_ascii=False) + "\n",
            encoding="utf-8",
        )
        artifact = formal.get("artifact")
        if isinstance(artifact, str):
            evidence_root = Path(artifact).parent
            for name in (
                "formal-properties.json",
                "formal-manifest.json",
                "formal-checkpoint.json",
                "formal-execution-evidence.json",
            ):
                source = evidence_root / name
                if source.is_file():
                    (run_dir / name).write_text(
                        source.read_text(encoding="utf-8"), encoding="utf-8"
                    )
    universal = result.get("universal_properties")
    if isinstance(universal, dict):
        (run_dir / "universal-properties.json").write_text(
            json.dumps(universal, indent=2, ensure_ascii=False) + "\n",
            encoding="utf-8",
        )
    lint = result.get("verilator_lint") or (
        formal.get("verilator_lint") if formal is not None else None
    )
    if isinstance(lint, dict):
        (run_dir / "verilator-lint.json").write_text(
            json.dumps(lint, indent=2, ensure_ascii=False) + "\n",
            encoding="utf-8",
        )


def repair_loop(
    spec: DesignSpec,
    backend,
    initial_rtl: str,
    contract: dict[str, Any],
    plan: dict[str, Any],
    expected_trace: dict[str, Any],
    run_dir: Path,
    *,
    contract_path: Path,
    plan_path: Path,
    oracle_path: Path,
    expected_path: Path,
    manifest_path: Path,
    frozen_manifest: dict[str, Any],
    origin_bundle: dict[str, Any],
    progress: ProgressCallback | None = None,
) -> RepairOutcome:
    attempts_root = run_dir / "attempts"
    attempts_root.mkdir(parents=True, exist_ok=True)
    verify_frozen_assets(
        contract=contract,
        plan=plan,
        expected_trace=expected_trace,
        contract_path=contract_path,
        plan_path=plan_path,
        oracle_path=oracle_path,
        expected_path=expected_path,
        manifest_path=manifest_path,
        frozen_manifest=frozen_manifest,
    )
    selected_oracle = oracle_path.read_text(encoding="utf-8")

    def notify(phase: str, values: dict[str, Any]) -> None:
        if progress:
            progress(phase, values)

    notify(
        "directed_verification",
        {
            "round": 0,
            "latest_attempt": str(attempts_root / "000"),
            "action": "verify",
            "strategy": None,
            "result": None,
            "reason": None,
            "eligible": None,
        },
    )
    champion_rtl, champion_result = _evaluate(
        spec,
        initial_rtl,
        contract,
        plan,
        expected_trace,
        attempts_root / "000",
        contract_path=contract_path,
        plan_path=plan_path,
        oracle_path=oracle_path,
        expected_path=expected_path,
        manifest_path=manifest_path,
        frozen_manifest=frozen_manifest,
        progress=notify,
    )
    _raise_if_nonrepairable_verification(
        champion_result, artifact=attempts_root / "000"
    )
    history: list[dict[str, Any]] = []
    counterexamples: list[dict[str, Any]] = []
    active_plan = json.loads(json.dumps(plan))
    active_expected_trace = json.loads(json.dumps(expected_trace))
    base_suite_sha256 = suite_hash(active_plan, active_expected_trace)
    adaptive_epochs: list[dict[str, Any]] = []
    refined_fingerprints: set[str] = set()
    adaptive_path = run_dir / "adaptive-corpus.json"

    def persist_adaptive_corpus() -> None:
        verified_active_suite = verify_epoch_chain(
            adaptive_epochs,
            plan,
            expected_trace,
            epoch_artifact_root=run_dir / "adaptive",
            spec=spec,
            oracle_sha256=frozen_manifest["oracle_sha256"],
            selected_oracle=selected_oracle,
            origin_bundle=origin_bundle,
        )
        current_active_suite = suite_hash(active_plan, active_expected_trace)
        if verified_active_suite != current_active_suite:
            raise RuntimeError(
                "adaptive suite chain does not resolve to the active verification suite"
            )
        adaptive_path.write_text(
            json.dumps(
                {
                    "schema_version": 2,
                    "base_suite_sha256": base_suite_sha256,
                    "active_suite_sha256": verified_active_suite,
                    "chain_head_sha256": (
                        adaptive_epochs[-1]["epoch_sha256"]
                        if adaptive_epochs
                        else None
                    ),
                    "epochs": adaptive_epochs,
                },
                indent=2,
                ensure_ascii=False,
            )
            + "\n",
            encoding="utf-8",
        )

    persist_adaptive_corpus()
    if champion_result.get("passed"):
        (run_dir / "repair-history.json").write_text("[]\n", encoding="utf-8")
        (run_dir / "counterexamples.json").write_text("[]\n", encoding="utf-8")
        persist_verification_evidence(champion_result, run_dir)
        return RepairOutcome(champion_rtl, champion_result, history, counterexamples, False, False)

    packet = build_failure_packet(
        champion_result,
        champion_rtl,
        trace_window_cycles=spec.verification["trace_window_cycles"],
    )
    (attempts_root / "000" / "failure-packet.json").write_text(
        json.dumps(packet, indent=2, ensure_ascii=False) + "\n", encoding="utf-8"
    )
    counterexamples.append(counterexample_from_packet(packet))
    stagnation = 0
    repaired = False
    max_rounds = spec.verification["max_repair_rounds"]
    for round_index in range(1, max_rounds + 1):
        packet_fingerprint = str(packet.get("fingerprint") or "")
        if packet_fingerprint not in refined_fingerprints:
            refined_fingerprints.add(packet_fingerprint)
            additions = derive_counterexample_scenarios(
                spec,
                active_plan,
                packet,
                round_index=round_index,
            )
            if additions:
                notify(
                    "repair_refining_tests",
                    {
                        "round": round_index,
                        "action": "extend_suite",
                        "strategy": "counterexample_mutation",
                        "added_scenario_count": len(additions),
                        "parent_suite_sha256": suite_hash(
                            active_plan, active_expected_trace
                        ),
                        "result": None,
                    },
                )
                epoch_dir = run_dir / "adaptive" / f"epoch-{len(adaptive_epochs) + 1:02d}"
                epoch_dir.mkdir(parents=True, exist_ok=True)
                try:
                    verify_frozen_assets(
                        contract=contract,
                        plan=plan,
                        expected_trace=expected_trace,
                        contract_path=contract_path,
                        plan_path=plan_path,
                        oracle_path=oracle_path,
                        expected_path=expected_path,
                        manifest_path=manifest_path,
                        frozen_manifest=frozen_manifest,
                    )
                    candidate_plan = json.loads(json.dumps(active_plan))
                    candidate_plan["scenarios"].extend(additions)
                    candidate_expected, epoch_quorum = revalidate_oracle_bundle_quorum(
                        spec,
                        candidate_plan,
                        selected_oracle,
                        origin_bundle,
                        epoch_dir / "quorum-replay",
                    )
                    parent_epoch_sha256 = (
                        adaptive_epochs[-1]["epoch_sha256"]
                        if adaptive_epochs
                        else None
                    )
                    parent_suite_sha256 = suite_hash(
                        active_plan, active_expected_trace
                    )
                    epoch = build_suite_epoch(
                        epoch=len(adaptive_epochs) + 1,
                        parent_epoch_sha256=parent_epoch_sha256,
                        parent_suite_sha256=parent_suite_sha256,
                        additions=additions,
                        plan=candidate_plan,
                        expected_trace=candidate_expected,
                        oracle_quorum=epoch_quorum,
                    )
                    (epoch_dir / "testplan.json").write_text(
                        json.dumps(candidate_plan, indent=2, ensure_ascii=False) + "\n",
                        encoding="utf-8",
                    )
                    (epoch_dir / "expected-trace.json").write_text(
                        json.dumps(candidate_expected, indent=2, ensure_ascii=False)
                        + "\n",
                        encoding="utf-8",
                    )
                    (epoch_dir / "oracle-quorum.json").write_text(
                        json.dumps(epoch_quorum, indent=2, ensure_ascii=False) + "\n",
                        encoding="utf-8",
                    )
                    (epoch_dir / "epoch.json").write_text(
                        json.dumps(epoch, indent=2, ensure_ascii=False) + "\n",
                        encoding="utf-8",
                    )
                    for path in (
                        epoch_dir / "testplan.json",
                        epoch_dir / "expected-trace.json",
                        epoch_dir / "oracle-quorum.json",
                        epoch_dir / "epoch.json",
                    ):
                        path.chmod(0o400)
                    active_plan = candidate_plan
                    active_expected_trace = candidate_expected
                    adaptive_epochs.append(epoch)
                    persist_adaptive_corpus()
                    baseline_attempt = (
                        attempts_root
                        / f"{round_index:03d}-suite-{len(adaptive_epochs):02d}-champion"
                    )
                    champion_rtl, champion_result = _evaluate(
                        spec,
                        champion_rtl,
                        contract,
                        plan,
                        expected_trace,
                        baseline_attempt,
                        contract_path=contract_path,
                        plan_path=plan_path,
                        oracle_path=oracle_path,
                        expected_path=expected_path,
                        manifest_path=manifest_path,
                        frozen_manifest=frozen_manifest,
                        evaluation_plan=active_plan,
                        evaluation_expected_trace=active_expected_trace,
                        progress=notify,
                    )
                    _raise_if_nonrepairable_verification(
                        champion_result, artifact=baseline_attempt
                    )
                    packet = build_failure_packet(
                        champion_result,
                        champion_rtl,
                        trace_window_cycles=spec.verification[
                            "trace_window_cycles"
                        ],
                    )
                    (baseline_attempt / "failure-packet.json").write_text(
                        json.dumps(packet, indent=2, ensure_ascii=False) + "\n",
                        encoding="utf-8",
                    )
                    if not any(
                        item["fingerprint"] == packet["fingerprint"]
                        for item in counterexamples
                    ):
                        counterexamples.append(counterexample_from_packet(packet))
                        counterexamples = counterexamples[
                            -spec.verification["max_counterexamples"] :
                        ]
                    notify(
                        "repair_suite_extended",
                        {
                            "round": round_index,
                            "action": "champion_reverified",
                            "strategy": "counterexample_mutation",
                            "result": "passed"
                            if champion_result.get("passed")
                            else "failed",
                            "suite_sha256": epoch["suite_sha256"],
                            "epoch": epoch["epoch"],
                            "total_checks": champion_result.get("total_checks"),
                            "mismatched": champion_result.get("mismatched"),
                        },
                    )
                except VerificationInfrastructureError:
                    # Provider retry/progress events already identify the real
                    # failure. Do not relabel it as an adaptive-suite error.
                    raise
                except Exception as exc:
                    notify(
                        "repair_suite_refinement_error",
                        {
                            "round": round_index,
                            "action": "abort",
                            "strategy": "counterexample_mutation",
                            "result": "error",
                            "reason": str(exc)[:2000],
                        },
                    )
                    raise
        blocks = extract_rtl_blocks(champion_rtl)
        authorized = dependency_slice(blocks, packet.get("signal"))
        use_regeneration = not authorized or stagnation >= spec.verification["stagnation_limit"]
        candidate_payloads: list[tuple[str, str, dict[str, Any]]] = []
        if use_regeneration:
            notify(
                "repair_regenerating",
                {
                    "round": round_index,
                    "max_rounds": max_rounds,
                    "failure_class": packet["failure_class"],
                    "first_failure_cycle": packet.get("first_failure_cycle"),
                    "action": "regenerate",
                    "strategy": "regeneration",
                    "result": None,
                    "reason": None,
                    "eligible": None,
                },
            )
            responses = backend.generate(
                _regeneration_prompt(
                    spec, contract, packet, champion_rtl, counterexamples
                ),
                n=spec.verification["repair_candidates"],
                temperature=spec.generation["temperature"],
                max_tokens=12000,
                system_prompt=(
                    "You repair synthesizable RTL. The verification contract and tests "
                    "are immutable; output only the requested complete RTL."
                ),
            )
            for response in responses:
                code = extract_verilog(response)
                candidate_payloads.append(("regeneration", code, {"diagnosis": "full regeneration"}))
            stagnation = 0
        else:
            notify(
                "repair_diagnosing",
                {
                    "round": round_index,
                    "max_rounds": max_rounds,
                    "failure_class": packet["failure_class"],
                    "requirement_id": (packet.get("requirement_ids") or [None])[0],
                    "first_failure_cycle": packet.get("first_failure_cycle"),
                    "counterexample_count": len(counterexamples),
                    "action": "diagnose",
                    "strategy": "local",
                    "result": None,
                    "reason": None,
                    "eligible": None,
                },
            )
            responses = backend.generate(
                _local_prompt(
                    contract,
                    packet,
                    champion_rtl,
                    history,
                    counterexamples,
                ),
                n=spec.verification["repair_candidates"],
                temperature=spec.generation["temperature"],
                max_tokens=8000,
                system_prompt=(
                    "You are a conservative RTL repair engine. Return strict JSON with "
                    "only authorized complete-block replacements."
                ),
            )
            for response in responses:
                try:
                    proposal = _extract_json(response)
                    code = apply_block_edits(champion_rtl, proposal.get("edits"), authorized)
                    candidate_payloads.append(("local", code, proposal))
                except Exception as exc:
                    candidate_payloads.append(
                        ("invalid_proposal", "", {"diagnosis": "proposal rejected", "error": str(exc)})
                    )

        accepted_this_round = False
        evaluated: list[dict[str, Any]] = []
        for candidate_index, (strategy, candidate_rtl, proposal) in enumerate(candidate_payloads, 1):
            attempt = attempts_root / f"{round_index:03d}-candidate-{candidate_index:02d}"
            verify_frozen_assets(
                contract=contract,
                plan=plan,
                expected_trace=expected_trace,
                contract_path=contract_path,
                plan_path=plan_path,
                oracle_path=oracle_path,
                expected_path=expected_path,
                manifest_path=manifest_path,
                frozen_manifest=frozen_manifest,
            )
            notify(
                "repair_evaluating",
                {
                    "round": round_index,
                    "max_rounds": max_rounds,
                    "candidate": candidate_index,
                    "candidate_count": len(candidate_payloads),
                    "strategy": strategy,
                    "latest_attempt": str(attempt),
                    "action": "evaluate",
                    "result": None,
                    "reason": None,
                    "eligible": None,
                },
            )
            (attempt / "proposal.json").parent.mkdir(parents=True, exist_ok=True)
            (attempt / "proposal.json").write_text(
                json.dumps(proposal, indent=2, ensure_ascii=False) + "\n",
                encoding="utf-8",
            )
            if not candidate_rtl:
                candidate_result = _failure_result(
                    "policy_error", proposal.get("error", "model returned no usable RTL")
                )
                candidate_result["suite_sha256"] = suite_hash(
                    active_plan, active_expected_trace
                )
                candidate_result["coverage"] = behavioral_coverage(
                    contract, active_plan, candidate_result
                )
                (attempt / "verification-result.json").write_text(
                    json.dumps(candidate_result, indent=2) + "\n", encoding="utf-8"
                )
            else:
                candidate_rtl, candidate_result = _evaluate(
                    spec,
                    candidate_rtl,
                    contract,
                    plan,
                    expected_trace,
                    attempt,
                    contract_path=contract_path,
                    plan_path=plan_path,
                    oracle_path=oracle_path,
                    expected_path=expected_path,
                    manifest_path=manifest_path,
                    frozen_manifest=frozen_manifest,
                    evaluation_plan=active_plan,
                    evaluation_expected_trace=active_expected_trace,
                    progress=notify,
                )
                _raise_if_nonrepairable_verification(
                    candidate_result, artifact=attempt
                )
            candidate_packet = None
            if not candidate_result.get("passed"):
                candidate_packet = build_failure_packet(
                    candidate_result,
                    candidate_rtl or champion_rtl,
                    trace_window_cycles=spec.verification["trace_window_cycles"],
                )
                (attempt / "failure-packet.json").write_text(
                    json.dumps(candidate_packet, indent=2, ensure_ascii=False) + "\n",
                    encoding="utf-8",
                )
            eligible, reason = candidate_improves(champion_result, candidate_result)
            row = {
                "round": round_index,
                "candidate": candidate_index,
                "strategy": strategy,
                "accepted": False,
                "eligible": eligible,
                "reason": reason,
                "diagnosis": proposal.get("diagnosis"),
                "score": verification_score(candidate_result),
                "attempt": str(attempt),
                "failure_class": candidate_result.get("failure_class"),
                "first_failure_ordinal": candidate_result.get("first_failure_ordinal"),
                "mismatched": candidate_result.get("mismatched"),
                "suite_sha256": candidate_result.get("suite_sha256"),
            }
            evaluated.append(
                {
                    "row": row,
                    "rtl": candidate_rtl,
                    "result": candidate_result,
                    "attempt": attempt,
                    "candidate": candidate_index,
                    "packet": candidate_packet,
                }
            )
            notify(
                "repair_candidate_completed",
                {
                    "round": round_index,
                    "max_rounds": max_rounds,
                    "candidate": candidate_index,
                    "candidate_count": len(candidate_payloads),
                    "strategy": strategy,
                    "result": "passed" if candidate_result.get("passed") else "failed",
                    "eligible": eligible,
                    "reason": reason,
                    "score": list(verification_score(candidate_result)),
                    "attempt": str(attempt),
                    "failure_class": candidate_result.get("failure_class"),
                    "requirement_id": (
                        (candidate_packet or {}).get("requirement_ids") or [None]
                    )[0],
                    "first_failure_cycle": (candidate_packet or {}).get(
                        "first_failure_cycle"
                    ),
                    "formal_status": (candidate_result.get("formal") or {}).get(
                        "status", "not_run"
                    ),
                    "verilator_status": (
                        candidate_result.get("verilator_lint")
                        or (candidate_result.get("formal") or {}).get("verilator_lint")
                        or {}
                    ).get("status", "not_run"),
                    "action": "candidate_complete",
                },
            )

        eligible_rows = [item for item in evaluated if item["row"]["eligible"]]
        selected = (
            max(
                eligible_rows,
                key=lambda item: (
                    verification_score(item["result"]),
                    -int(item["candidate"]),
                ),
            )
            if eligible_rows
            else None
        )
        for item in evaluated:
            if item is selected:
                item["row"]["accepted"] = True
            elif item["row"]["eligible"]:
                item["row"]["reason"] = (
                    "strictly improved but was not the strongest candidate in this round"
                )
            history.append(item["row"])

        if selected is not None:
            champion_rtl = selected["rtl"]
            champion_result = selected["result"]
            accepted_this_round = True
            repaired = True
            notify(
                "repair_accepted",
                {
                    "round": round_index,
                    "candidate": selected["candidate"],
                    "score": verification_score(champion_result),
                    "passed": bool(champion_result.get("passed")),
                    "latest_attempt": str(selected["attempt"]),
                    "strategy": selected["row"]["strategy"],
                    "result": "accepted",
                    "reason": selected["row"]["reason"],
                    "eligible": True,
                    "action": "release" if champion_result.get("passed") else "continue",
                    "failure_class": (
                        None
                        if champion_result.get("passed")
                        else (selected.get("packet") or {}).get("failure_class")
                    ),
                    "requirement_id": (
                        None
                        if champion_result.get("passed")
                        else ((selected.get("packet") or {}).get("requirement_ids") or [None])[0]
                    ),
                    "first_failure_cycle": (
                        None
                        if champion_result.get("passed")
                        else (selected.get("packet") or {}).get("first_failure_cycle")
                    ),
                    "formal_mode": spec.verification["formal_mode"],
                    "formal_status": (champion_result.get("formal") or {}).get(
                        "status", "not_run"
                    ),
                    "verilator_status": (
                        champion_result.get("verilator_lint")
                        or (champion_result.get("formal") or {}).get("verilator_lint")
                        or {}
                    ).get("status", "not_run"),
                },
            )
            if champion_result.get("passed"):
                (run_dir / "repair-history.json").write_text(
                    json.dumps(history, indent=2, ensure_ascii=False) + "\n",
                    encoding="utf-8",
                )
                (run_dir / "counterexamples.json").write_text(
                    json.dumps(counterexamples, indent=2, ensure_ascii=False) + "\n",
                    encoding="utf-8",
                )
                persist_verification_evidence(champion_result, run_dir)
                return RepairOutcome(
                    champion_rtl,
                    champion_result,
                    history,
                    counterexamples,
                    repaired,
                    False,
                )
            packet = selected["packet"]
            if not any(item["fingerprint"] == packet["fingerprint"] for item in counterexamples):
                counterexamples.append(counterexample_from_packet(packet))
                counterexamples = counterexamples[-spec.verification["max_counterexamples"] :]
        stagnation = 0 if accepted_this_round else stagnation + 1
        if not accepted_this_round:
            notify(
                "repair_rollback",
                {
                    "round": round_index,
                    "stagnation": stagnation,
                    "failure_class": packet["failure_class"],
                    "first_failure_cycle": packet.get("first_failure_cycle"),
                    "result": "rolled_back",
                    "reason": "no candidate strictly improved the champion",
                    "action": "regenerate" if stagnation >= spec.verification["stagnation_limit"] else "retry",
                    "strategy": None,
                    "eligible": None,
                },
            )

    (run_dir / "repair-history.json").write_text(
        json.dumps(history, indent=2, ensure_ascii=False) + "\n", encoding="utf-8"
    )
    (run_dir / "counterexamples.json").write_text(
        json.dumps(counterexamples, indent=2, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )
    persist_verification_evidence(champion_result, run_dir)
    return RepairOutcome(
        champion_rtl,
        champion_result,
        history,
        counterexamples,
        repaired,
        True,
    )


def _independent_result_error(
    result: dict[str, Any],
    *,
    expected_stimuli: list[dict[str, int]],
    expected_inputs: dict[str, int],
    expected_outputs: dict[str, int],
) -> str | None:
    """Validate the complete successful differential-verification transcript."""
    return validate_independent_transcript(
        result,
        expected_stimuli=expected_stimuli,
        expected_inputs=expected_inputs,
        expected_outputs=expected_outputs,
    )


def _independent_result_passed(
    result: dict[str, Any],
    *,
    expected_stimuli: list[dict[str, int]],
    expected_inputs: dict[str, int],
    expected_outputs: dict[str, int],
) -> bool:
    return (
        result.get("match_rate") == 1.0
        and result.get("mismatched") == 0
        and result.get("sv_error") is None
        and result.get("py_error") is None
        and result.get("meta_error") is None
        and _independent_result_error(
            result,
            expected_stimuli=expected_stimuli,
            expected_inputs=expected_inputs,
            expected_outputs=expected_outputs,
        )
        is None
    )


def _independent_timeout_error(
    *, sv_error: Any, py_error: Any
) -> tuple[str, str, str] | None:
    """Type timeout strings at the current verifier/result boundary."""
    for provider, value in (
        ("systemverilog", sv_error),
        ("python_oracle", py_error),
    ):
        if not isinstance(value, str):
            continue
        code = value.partition(":")[0].strip().lower()
        if re.fullmatch(r"[a-z0-9]+(?:_[a-z0-9]+)*_timeout", code):
            return provider, code, value
    return None


def _json_clone(value: Any) -> Any:
    return json.loads(json.dumps(value))


def _independent_failure_fingerprint(
    *, seed: int, verification: dict[str, Any]
) -> str:
    """Identify the same independent failure across rolled-back candidates."""
    mismatches = verification.get("mismatches")
    earliest_test: int | None = None
    earliest_rows: list[dict[str, Any]] = []
    if isinstance(mismatches, list):
        test_indexes = [
            row.get("test")
            for row in mismatches
            if isinstance(row, dict)
            and isinstance(row.get("test"), int)
            and not isinstance(row.get("test"), bool)
            and row.get("test") >= 0
        ]
        if test_indexes:
            earliest_test = min(test_indexes)
            matching_rows = [
                _json_clone(row)
                for row in mismatches
                if isinstance(row, dict) and row.get("test") == earliest_test
            ]
            earliest_rows = sorted(matching_rows, key=canonical_json)[:1]
    return content_hash(
        {
            "seed": seed,
            "first_failure_test": earliest_test,
            "mismatches": earliest_rows,
            "sv_error": verification.get("sv_error"),
            "py_error": verification.get("py_error"),
            "meta_error": verification.get("meta_error"),
        }
    )


def _bounded_prompt_value(value: Any) -> Any:
    encoded = canonical_json(value).encode("utf-8")
    if len(encoded) <= INDEPENDENT_PROMPT_VALUE_MAX_BYTES:
        return _json_clone(value)
    return {
        "omitted_for_prompt_budget": True,
        "full_value_sha256": content_hash(value),
        "full_value_bytes": len(encoded),
    }


def _minimal_independent_replay_slice(evidence: dict[str, Any]) -> dict[str, Any]:
    """Return the first failing sample and its exact aligned verifier rows."""
    verification = evidence.get("verification")
    if not isinstance(verification, dict):
        verification = {}
    mismatches = verification.get("mismatches")
    if not isinstance(mismatches, list):
        mismatches = []
    indexes = [
        row.get("test")
        for row in mismatches
        if isinstance(row, dict)
        and isinstance(row.get("test"), int)
        and not isinstance(row.get("test"), bool)
        and row.get("test") >= 0
    ]
    first_test = min(indexes) if indexes else None

    def aligned_row(name: str) -> Any:
        rows = verification.get(name)
        if (
            first_test is None
            or not isinstance(rows, list)
            or first_test >= len(rows)
        ):
            return None
        return _bounded_prompt_value(rows[first_test])

    matching_mismatches = sorted(
        (
            row
            for row in mismatches
            if isinstance(row, dict) and row.get("test") == first_test
        ),
        key=canonical_json,
    )
    first_mismatches = [
        _bounded_prompt_value(row) for row in matching_mismatches[:1]
    ]
    return {
        "source": evidence.get("source"),
        "fingerprint": evidence.get("fingerprint"),
        "seed": evidence.get("seed"),
        "num_tests": verification.get("num_tests"),
        "first_failure_test": first_test,
        "stimulus": aligned_row("stimuli"),
        "verilog_trace": aligned_row("verilog_trace"),
        "python_trace": aligned_row("python_trace"),
        "mismatches": first_mismatches,
        "mismatch_count_at_test": len(matching_mismatches),
        "sv_error": _bounded_prompt_value(verification.get("sv_error")),
        "full_verification_sha256": content_hash(verification),
    }


def _independent_prompt_evidence(
    counterexamples: list[dict[str, Any]],
) -> dict[str, Any]:
    """Build a deterministic, deduplicated, byte-bounded model context."""
    unique: list[dict[str, Any]] = []
    seen: set[str] = set()
    duplicate_count = 0
    for evidence in counterexamples:
        if not isinstance(evidence, dict):
            continue
        fingerprint = evidence.get("fingerprint")
        if not isinstance(fingerprint, str):
            verification = evidence.get("verification")
            if not isinstance(verification, dict):
                continue
            seed = evidence.get("seed")
            if not isinstance(seed, int) or isinstance(seed, bool):
                continue
            fingerprint = _independent_failure_fingerprint(
                seed=seed, verification=verification
            )
        if fingerprint in seen:
            duplicate_count += 1
            continue
        seen.add(fingerprint)
        bound_evidence = {**evidence, "fingerprint": fingerprint}
        unique.append(_minimal_independent_replay_slice(bound_evidence))

    envelope: dict[str, Any] = {
        "schema_version": 1,
        "byte_budget": INDEPENDENT_PROMPT_EVIDENCE_MAX_BYTES,
        "full_evidence_location": "independent-verification.json",
        "unique_counterexample_count": len(unique),
        "duplicate_counterexample_count": duplicate_count,
        "included_counterexample_count": 0,
        "omitted_counterexample_count": len(unique),
        "replay_slices": [],
    }
    for replay_slice in unique:
        candidate = {
            **envelope,
            "replay_slices": [*envelope["replay_slices"], replay_slice],
        }
        candidate["included_counterexample_count"] = len(candidate["replay_slices"])
        candidate["omitted_counterexample_count"] = len(unique) - len(
            candidate["replay_slices"]
        )
        if len(canonical_json(candidate).encode("utf-8")) > INDEPENDENT_PROMPT_EVIDENCE_MAX_BYTES:
            continue
        envelope = candidate
    if len(canonical_json(envelope).encode("utf-8")) > INDEPENDENT_PROMPT_EVIDENCE_MAX_BYTES:
        raise RuntimeError("independent prompt evidence envelope exceeded its byte budget")
    return envelope


def _independent_regeneration_prompt(
    spec: DesignSpec,
    contract: dict[str, Any],
    interface: str,
    rtl: str,
    counterexamples: list[dict[str, Any]],
) -> str:
    prompt_evidence = _independent_prompt_evidence(counterexamples)
    return f"""Regenerate a corrected synthesizable SystemVerilog implementation.
The immutable directed suite has already passed, but independent differential
verification found the deterministic replay slices below. The complete evidence
is persisted separately and the slices are deduplicated and byte-bounded. Return
only the complete RTL inside <answer> and a systemverilog code fence.

Exact interface:
{interface}

Machine-checked architecture constraints:
{canonical_json(spec.architecture)}

Immutable contract:
{canonical_json(contract)}

Independent counterexample replay slices:
{canonical_json(prompt_evidence)}

Current RTL (diagnostic context only):
```systemverilog
{rtl}
```

Preserve the exact interface and every specified behavior. Do not use initial
blocks, delays, system tasks, file I/O, DPI, force/release, or vendor primitives.
"""


def close_independent_verification(
    spec: DesignSpec,
    backend,
    outcome: RepairOutcome,
    contract: dict[str, Any],
    base_plan: dict[str, Any],
    base_expected_trace: dict[str, Any],
    active_plan: dict[str, Any],
    active_expected_trace: dict[str, Any],
    run_dir: Path,
    *,
    interface: str,
    python_model: str,
    verifier: Callable[..., dict[str, Any]],
    contract_path: Path,
    plan_path: Path,
    oracle_path: Path,
    expected_path: Path,
    manifest_path: Path,
    frozen_manifest: dict[str, Any],
    progress: ProgressCallback | None = None,
) -> tuple[RepairOutcome, dict[str, Any]]:
    """Close independent differential failures without weakening prior gates."""
    if not outcome.verification.get("passed") or outcome.exhausted:
        raise RuntimeError(
            "independent closure requires a candidate that passed the active suite"
        )

    report_path = run_dir / "independent-verification.json"
    attempts_root = run_dir / "independent-repair"
    attempts_root.mkdir(parents=True, exist_ok=True)
    max_rounds = int(spec.verification["max_repair_rounds"])
    seeds = [spec.generation["seed"] + offset for offset in (1009, 2027)]
    expected_num_tests = spec.generation["independent_verify_tests"]
    expected_inputs = independent_input_widths(spec)
    expected_outputs = {
        port.name: port.width
        for port in spec.ports
        if port.direction == "output"
    }
    expected_stimuli = {
        seed: derive_independent_stimuli(
            spec, num_tests=expected_num_tests, seed=seed
        )
        for seed in seeds
    }
    attempts: list[dict[str, Any]] = []
    repair_history = [dict(row) for row in outcome.history]
    counterexamples = [dict(row) for row in outcome.counterexamples]
    independent_counterexamples: list[dict[str, Any]] = []
    final_runs: list[dict[str, Any]] = []

    def persist(status: str, rtl: str) -> dict[str, Any]:
        report = {
            "schema_version": 3,
            "status": status,
            "sandbox_image": sandbox_image_ref(),
            "rtl_sha256": content_hash(rtl),
            "oracle_sha256": content_hash(python_model),
            "active_suite_sha256": suite_hash(
                active_plan, active_expected_trace
            ),
            "max_repair_rounds": max_rounds,
            "runs": final_runs,
            "attempts": attempts,
            "counterexamples": independent_counterexamples,
            "repair_history": [
                row
                for row in repair_history
                if row.get("stage") == "independent_closure"
            ],
        }
        report_path.write_text(
            json.dumps(report, indent=2, ensure_ascii=False) + "\n",
            encoding="utf-8",
        )
        (run_dir / "repair-history.json").write_text(
            json.dumps(repair_history, indent=2, ensure_ascii=False) + "\n",
            encoding="utf-8",
        )
        (run_dir / "counterexamples.json").write_text(
            json.dumps(counterexamples, indent=2, ensure_ascii=False) + "\n",
            encoding="utf-8",
        )
        return report

    def run_pair(
        rtl: str, *, round_index: int, candidate_index: int | None
    ) -> tuple[bool, list[dict[str, Any]]]:
        pair: list[dict[str, Any]] = []
        for run_index, seed in enumerate(seeds, 1):
            if progress:
                progress(
                    "independent_verification",
                    {
                        "round": round_index,
                        "max_rounds": max_rounds,
                        "candidate": candidate_index,
                        "run": run_index,
                        "run_count": len(seeds),
                        "seed": seed,
                        "status": "running",
                    },
                )
            try:
                raw = verifier(
                    interface,
                    rtl,
                    python_model,
                    num_tests=expected_num_tests,
                    seed=seed,
                )
            except NonRepairableInfrastructureError:
                persist("failed", rtl)
                raise
            if not isinstance(raw, dict):
                persist("failed", rtl)
                raise NonRepairableVerificationError(
                    "independent verifier returned a non-object result",
                    error={
                        "code": "independent_contract_execution_error",
                        "category": "evidence",
                        "domain": "independent_verification",
                        "repairable": False,
                        "repairable_by_rtl": False,
                        "retryable": False,
                        "provider": "independent_contract",
                        "message": (
                            "independent verifier returned a non-object result"
                        ),
                        "context": {
                            "artifact": str(report_path),
                            "round": round_index,
                            "candidate": candidate_index,
                            "run": run_index,
                            "seed": seed,
                        },
                        "evidence": [str(report_path)],
                    },
                )
            passed = _independent_result_passed(
                raw,
                expected_stimuli=expected_stimuli[seed],
                expected_inputs=expected_inputs,
                expected_outputs=expected_outputs,
            )
            raw_evidence = _json_clone(raw)
            row = {
                "run": run_index,
                "seed": seed,
                "num_tests": expected_num_tests,
                "passed": passed,
                "verification": raw_evidence,
            }
            pair.append(row)
            sv_error = raw.get("sv_error")
            py_error = raw.get("py_error")
            meta_error = raw.get("meta_error")
            error_field_error = next(
                (
                    f"verifier {name} field is not a string or null"
                    for name, value in (
                        ("sv_error", sv_error),
                        ("py_error", py_error),
                        ("meta_error", meta_error),
                    )
                    if value is not None and not isinstance(value, str)
                ),
                None,
            )
            timeout_error = _independent_timeout_error(
                sv_error=sv_error, py_error=py_error
            )
            if timeout_error is not None:
                provider, timeout_code, timeout_detail = timeout_error
                attempts.append(
                    {
                        "round": round_index,
                        "candidate": candidate_index,
                        "rtl_sha256": content_hash(rtl),
                        "passed": False,
                        "runs": pair,
                    }
                )
                persist("failed", rtl)
                raise NonRepairableVerificationError(
                    "independent verification provider timed out: "
                    f"{timeout_detail}",
                    error={
                        "code": timeout_code,
                        "category": "infrastructure",
                        "domain": "independent_verification",
                        "repairable": False,
                        "repairable_by_rtl": False,
                        "retryable": True,
                        "provider": provider,
                        "message": timeout_detail,
                        "context": {
                            "artifact": str(report_path),
                            "round": round_index,
                            "candidate": candidate_index,
                            "run": run_index,
                            "seed": seed,
                        },
                        "evidence": [str(report_path)],
                    },
                )
            transcript_error = error_field_error
            if transcript_error is None:
                transcript_error = (
                    "verifier num_tests does not match the frozen request"
                    if raw.get("num_tests") != expected_num_tests
                    else validate_independent_stimuli(
                        raw.get("stimuli"),
                        expected_stimuli=expected_stimuli[seed],
                        expected_inputs=expected_inputs,
                    )
                )
            if (
                transcript_error is None
                and sv_error is None
                and py_error is None
                and meta_error is None
            ):
                transcript_error = _independent_result_error(
                    raw,
                    expected_stimuli=expected_stimuli[seed],
                    expected_inputs=expected_inputs,
                    expected_outputs=expected_outputs,
                )
            nonrepairable_error = (
                py_error
                if py_error is not None
                else meta_error
                if meta_error is not None
                else transcript_error
            )
            if nonrepairable_error is not None:
                attempts.append(
                    {
                        "round": round_index,
                        "candidate": candidate_index,
                        "rtl_sha256": content_hash(rtl),
                        "passed": False,
                        "runs": pair,
                    }
                )
                persist("failed", rtl)
                provider = (
                    "python_oracle"
                    if py_error is not None else "independent_contract"
                )
                raise NonRepairableVerificationError(
                    "independent verification could not execute its frozen "
                    f"{provider} evidence: {nonrepairable_error}",
                    error={
                        "code": f"{provider}_execution_error",
                        "category": (
                            "verification"
                            if provider == "python_oracle"
                            else "evidence"
                        ),
                        "domain": "independent_verification",
                        "repairable": False,
                        "repairable_by_rtl": False,
                        "retryable": False,
                        "provider": provider,
                        "message": str(nonrepairable_error),
                        "context": {
                            "artifact": str(report_path),
                            "round": round_index,
                            "candidate": candidate_index,
                            "run": run_index,
                            "seed": seed,
                        },
                        "evidence": [str(report_path)],
                    },
                )
            if not passed:
                evidence = {
                    "source": "independent_cross_verification",
                    "round": round_index,
                    "candidate": candidate_index,
                    "run": run_index,
                    "seed": seed,
                    "num_tests": expected_num_tests,
                    "rtl_sha256": content_hash(rtl),
                    "verification": raw_evidence,
                }
                evidence["fingerprint"] = _independent_failure_fingerprint(
                    seed=seed, verification=raw_evidence
                )
                independent_counterexamples.append(evidence)
                counterexamples.append(evidence)
            if progress:
                progress(
                    "independent_verification",
                    {
                        "round": round_index,
                        "max_rounds": max_rounds,
                        "candidate": candidate_index,
                        "run": run_index,
                        "run_count": len(seeds),
                        "seed": seed,
                        "status": "passed" if passed else "failed",
                        "match_rate": raw.get("match_rate"),
                        "mismatched": raw.get("mismatched"),
                    },
                )
        attempts.append(
            {
                "round": round_index,
                "candidate": candidate_index,
                "rtl_sha256": content_hash(rtl),
                "passed": all(row["passed"] for row in pair),
                "runs": pair,
            }
        )
        return all(row["passed"] for row in pair), pair

    champion_rtl = outcome.rtl
    initial_passed, initial_pair = run_pair(
        champion_rtl, round_index=0, candidate_index=None
    )
    if initial_passed:
        final_runs.extend(initial_pair)
        return outcome, persist("pass", champion_rtl)
    persist("repairing", champion_rtl)

    for round_index in range(1, max_rounds + 1):
        if progress:
            progress(
                "independent_repair_generating",
                {
                    "round": round_index,
                    "max_rounds": max_rounds,
                    "candidate_count": spec.verification["repair_candidates"],
                    "counterexample_count": len(independent_counterexamples),
                    "status": "running",
                },
            )
        responses = backend.generate(
            _independent_regeneration_prompt(
                spec,
                contract,
                interface,
                champion_rtl,
                independent_counterexamples,
            ),
            n=spec.verification["repair_candidates"],
            temperature=spec.generation["temperature"],
            max_tokens=12000,
            system_prompt=(
                "You repair synthesizable RTL from complete differential "
                "counterexamples. Return only the requested complete RTL."
            ),
        )
        for candidate_index, response in enumerate(responses, 1):
            attempt_dir = (
                attempts_root
                / f"round-{round_index:02d}-candidate-{candidate_index:02d}"
            )
            attempt_dir.mkdir(parents=True, exist_ok=True)
            (attempt_dir / "response.txt").write_text(
                response, encoding="utf-8"
            )
            candidate = extract_verilog(response) or ""
            if not candidate.strip():
                repair_history.append(
                    {
                        "stage": "independent_closure",
                        "round": round_index,
                        "candidate": candidate_index,
                        "accepted": False,
                        "reason": "model response did not contain complete RTL",
                    }
                )
                persist("repairing", champion_rtl)
                continue
            normalized, verification = _evaluate(
                spec,
                candidate,
                contract,
                base_plan,
                base_expected_trace,
                attempt_dir,
                contract_path=contract_path,
                plan_path=plan_path,
                oracle_path=oracle_path,
                expected_path=expected_path,
                manifest_path=manifest_path,
                frozen_manifest=frozen_manifest,
                evaluation_plan=active_plan,
                evaluation_expected_trace=active_expected_trace,
                progress=progress,
            )
            if not verification.get("passed"):
                repair_history.append(
                    {
                        "stage": "independent_closure",
                        "round": round_index,
                        "candidate": candidate_index,
                        "rtl_sha256": content_hash(normalized),
                        "accepted": False,
                        "reason": "candidate regressed the active verification suite",
                        "failure_class": verification.get("failure_class"),
                    }
                )
                persist("repairing", champion_rtl)
                continue
            independent_passed, candidate_pair = run_pair(
                normalized,
                round_index=round_index,
                candidate_index=candidate_index,
            )
            repair_history.append(
                {
                    "stage": "independent_closure",
                    "round": round_index,
                    "candidate": candidate_index,
                    "rtl_sha256": content_hash(normalized),
                    "accepted": independent_passed,
                    "reason": (
                        "active suite and both independent runs passed"
                        if independent_passed
                        else "independent counterexample remained; champion rolled back"
                    ),
                }
            )
            if not independent_passed:
                persist("repairing", champion_rtl)
                continue
            final_runs.extend(candidate_pair)
            persist_verification_evidence(verification, run_dir)
            accepted_outcome = RepairOutcome(
                normalized,
                verification,
                repair_history,
                counterexamples,
                True,
                False,
            )
            return accepted_outcome, persist("pass", normalized)

    persist("failed", champion_rtl)
    raise RepairExhaustedError(
        "independent differential verification did not close within "
        f"{max_rounds} repair rounds; artifact={report_path}",
        failure_class="independent_cross_verification",
    )
