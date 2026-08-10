"""Persistent RTL-Closure generation and LibreLane orchestration pipeline."""

from __future__ import annotations

import hashlib
import fcntl
import json
import math
import os
import re
import secrets
import shutil
import signal
import stat
import subprocess
import sys
import tempfile
import threading
import time
from datetime import datetime
from pathlib import Path
from typing import Any

import yaml
from contextlib import contextmanager

from .backend import StaticBackend, make_backends
from .adaptive import suite_hash, verify_epoch_chain
from .contract import canonical_json, content_hash, plan_contract
from .coverage_refinement import (
    enforce_oracle_line_coverage,
    refine_oracle_coverage,
    validate_oracle_line_coverage_evidence,
)
from .errors import LaneLaunchError, LaneReconciliationError, RepairExhaustedError
from .formal import (
    FORMAL_BATCH_SIZE,
    FORMAL_MIN_BATCH_SIZE,
    FORMAL_SUCCESS_PATTERN,
    build_formal_evidence_contract,
    render_formal_batch_script,
)
from .feedback import build_failure_packet
from .inference import run_problem
from .oracle import (
    backend_provenance,
    build_oracle_origin_bundle,
    enforce_oracle_diversity,
    generate_frozen_oracle,
    revalidate_oracle_bundle_quorum,
    revalidate_oracle_quorum,
    validate_oracle_origin_bundle,
    validate_oracle_quorum_evidence,
)
from .repair import (
    close_independent_verification,
    repair_loop,
    verify_frozen_assets,
)
from .sandbox import (
    check_exact_interface,
    check_rtl_policy,
    make_contract_verifier,
    normalize_top_module,
    run_architecture_audit,
    run_synthesis_equivalence,
    yosys_check,
)
from .runtime import (
    bind_sandbox_image,
    bind_verify_image,
    resolve_sandbox_image,
    resolve_verify_image,
    sandbox_image_ref,
    validate_sandbox_image_identity,
    validate_verify_image_identity,
    verify_image_ref,
)
from .universal import (
    bind_universal_requirement_coverage,
    classify_universal_execution_log,
    render_universal_execution_inputs,
    universal_proof_scope,
    universal_requirement_coverage,
)
from .verification import (
    derive_independent_stimuli,
    independent_input_widths,
    validate_independent_stimuli,
    validate_independent_transcript,
)
from .spec import (
    DesignSpec,
    load_spec,
    render_config,
    render_eqy_gate_tcl,
    render_eqy_script,
    render_eqy_script_for_name,
    render_interface,
    render_pin_order,
    render_question,
    render_sdc,
)


STATE_ROOT = Path(
    os.environ.get("RTL_CLOSURE_STATE_DIR", "~/.local/state/rtl-closure")
).expanduser().resolve()
DESIGN_ROOT = Path(
    os.environ.get(
        "RTL_CLOSURE_DESIGN_ROOT", str(STATE_ROOT / "published-designs")
    )
).expanduser().resolve()
CURRENT = STATE_ROOT / "current"
RUN_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,254}$")
ACTIVE_STATUSES = frozenset({"created", "queued", "running"})
TERMINAL_STATUSES = frozenset({"completed", "failed", "incomplete", "stopped"})
MAPPED_EQUIVALENCE_SCOPE = (
    "RTL versus synthesized PDK-mapped netlist equivalence only; "
    "this is not post-layout LEC"
)
MAPPED_EQUIVALENCE_PROVIDER = "LibreLane Classic / Yosys.EQY"
MAPPED_EQUIVALENCE_PROVIDER_VERSION = "LibreLane 3.0.5 / EQY 0.62"
MAX_EVENT_JOURNAL_BYTES = 64 * 1024 * 1024
WORKER_LAUNCH_TOKEN_ENV = "RTL_CLOSURE_WORKER_LAUNCH_TOKEN"


class WorkerFenceError(RuntimeError):
    """A worker no longer owns the durable launch generation for its run."""


_ACTIVE_WORKER_FENCE: tuple[str, int, str] | None = None

BASE_RELEASE_ARTIFACTS = (
    "contract.json",
    "testplan.json",
    "expected-trace.json",
    "frozen-manifest.json",
    "oracle-origins.json",
    "oracle-quorum.json",
    "final-suite-quorum.json",
    "verification-result.json",
    "repair-history.json",
    "counterexamples.json",
    "coverage-refinement.json",
    "adaptive-corpus.json",
    "independent-verification.json",
    "yosys-check.log",
    "architecture-audit.json",
    "universal-properties.json",
    "universal-execution-evidence.json",
    "synthesis-equivalence.json",
    "synthesis-equivalence.log",
)


class LaneInputIdentityError(ValueError):
    """Raised when LibreLane did not consume the attested publication bundle."""


def _distinct_oracle_backends(*backends: Any) -> list[Any]:
    """Keep one backend per declared provider/model source in stable order."""
    selected: list[Any] = []
    seen: set[str] = set()
    for backend in backends:
        source_id = str(backend_provenance(backend)["source_id"])
        if source_id in seen:
            continue
        seen.add(source_id)
        selected.append(backend)
    return selected


def _required_release_artifacts(spec: DesignSpec) -> tuple[str, ...]:
    required = list(BASE_RELEASE_ARTIFACTS)
    if spec.verification["formal_mode"] != "off":
        required.extend(
            (
                "formal-report.json",
                "formal-properties.json",
                "formal-execution-evidence.json",
            )
        )
    if spec.verification["verilator_lint"]:
        required.append("verilator-lint.json")
    return tuple(required)


def _merge_repair_progress(
    previous: dict[str, Any], phase: str, summary: dict[str, Any]
) -> dict[str, Any]:
    """Keep durable context while replacing candidate-specific observations."""
    current = dict(previous)
    transient = {
        "failure_class",
        "requirement_id",
        "first_failure_cycle",
        "result",
        "eligible",
        "reason",
        "score",
        "passed",
        "formal_status",
        "verilator_status",
        "formal_detail",
        "formal_artifact",
        "universal_status",
        "universal_proof",
        "universal_detail",
        "universal_artifact",
        "property_count",
        "passed_property_count",
        "failed_property_ids",
        "failed_checks",
        "architecture_artifact",
    }
    for key in transient:
        current.pop(key, None)
    if phase in {
        "directed_verification",
        "repair_refining_tests",
        "repair_regenerating",
        "repair_diagnosing",
        "repair_rollback",
    }:
        for key in ("candidate", "candidate_count", "latest_attempt"):
            current.pop(key, None)
    current.update(summary)
    return current


def _merge_model_progress(
    active: dict[int, dict[str, Any]], event: dict[str, Any]
) -> dict[str, Any]:
    """Update in-flight request snapshots without retaining model content."""
    request_id = int(event["request_id"])
    event_name = str(event["event"])
    previous = active.get(request_id, {})
    combined = {**previous, **event}
    if event_name in {
        "request_started",
        "request_waiting",
        "stream_connected",
        "streaming",
        "request_retrying",
    }:
        active[request_id] = combined
    elif event_name in {"request_completed", "request_failed"}:
        active.pop(request_id, None)
    summary = dict(combined)
    summary["active_request_count"] = len(active)
    summary["active_requests"] = [dict(active[key]) for key in sorted(active)]
    return summary


def now_iso() -> str:
    return datetime.now().astimezone().isoformat(timespec="seconds")


def atomic_write(path: Path, content: str, mode: int = 0o600) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{path.name}.tmp.", dir=path.parent
    )
    temporary = Path(temporary_name)
    try:
        os.fchmod(descriptor, mode)
        owned_descriptor = descriptor
        descriptor = -1
        with os.fdopen(owned_descriptor, "w", encoding="utf-8") as handle:
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
        directory = os.open(path.parent, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
    finally:
        if descriptor >= 0:
            os.close(descriptor)
        temporary.unlink(missing_ok=True)


def _artifact_hash(path: Path) -> str:
    content = path.read_text(encoding="utf-8")
    return _artifact_content_hash(path.name, content)


def _artifact_content_hash(name: str, content: str) -> str:
    if name.endswith(".json"):
        return content_hash(json.loads(content))
    return content_hash(content)


def _parse_json_object(name: str, content: str) -> dict[str, Any]:
    try:
        value = json.loads(content)
    except json.JSONDecodeError as exc:
        raise RuntimeError(f"release evidence is unreadable: {name}: {exc}") from exc
    if not isinstance(value, dict):
        raise RuntimeError(f"release evidence must be a JSON object: {name}")
    return value


def _read_json_object(path: Path) -> dict[str, Any]:
    try:
        content = path.read_text(encoding="utf-8")
    except OSError as exc:
        raise RuntimeError(f"release evidence is unreadable: {path.name}: {exc}") from exc
    return _parse_json_object(path.name, content)


def _trusted_run_text_file(
    raw_path: Any,
    run_dir: Path,
    *,
    label: str,
    maximum_bytes: int = 2 * 1024 * 1024,
) -> tuple[Path, str]:
    if not isinstance(raw_path, str) or not raw_path:
        raise RuntimeError(f"release evidence is missing {label}")
    candidate = Path(raw_path)
    if not candidate.is_absolute():
        raise RuntimeError(f"release evidence {label} path must be absolute")
    try:
        root = run_dir.resolve(strict=True)
        resolved = candidate.resolve(strict=True)
        resolved.relative_to(root)
    except (OSError, RuntimeError, ValueError) as exc:
        raise RuntimeError(
            f"release evidence {label} must be a canonical file inside the run directory"
        ) from exc
    if candidate != resolved or candidate.is_symlink() or not resolved.is_file():
        raise RuntimeError(
            f"release evidence {label} must be a canonical regular non-symlink file"
        )
    size = resolved.stat().st_size
    if size <= 0 or size > maximum_bytes:
        raise RuntimeError(
            f"release evidence {label} is empty or exceeds {maximum_bytes} bytes"
        )
    try:
        return resolved, resolved.read_text(encoding="utf-8")
    except (OSError, UnicodeError) as exc:
        raise RuntimeError(f"release evidence {label} is not readable UTF-8 text") from exc


def _build_universal_execution_evidence(
    spec: DesignSpec,
    rtl: str,
    report: dict[str, Any],
    run_dir: Path,
) -> dict[str, Any]:
    requested = spec.verification["universal_properties"]
    rows = report.get("properties")
    if not isinstance(rows, list) or len(rows) != len(requested):
        raise RuntimeError(
            "release evidence universal execution rows do not match the specification"
        )
    entries: list[dict[str, Any]] = []
    total_bytes = 0
    expected_dut = rtl + "\n"
    requirement_ids = [row["id"] for row in spec.verification["requirements"]]
    for index, (property_value, observed) in enumerate(zip(requested, rows), 1):
        if not isinstance(observed, dict):
            raise RuntimeError("release evidence universal property row is invalid")
        executed = observed.get("executed")
        if not isinstance(executed, bool):
            raise RuntimeError(
                f"release evidence for universal property {property_value['id']} has no execution disposition"
            )
        artifacts = observed.get("artifacts")
        if not isinstance(artifacts, dict):
            raise RuntimeError(
                f"release evidence for universal property {property_value['id']} has no execution artifacts"
            )
        expected = render_universal_execution_inputs(
            top=spec.name,
            ports=spec.ports,
            clock_port=spec.clock_port,
            reset=spec.reset,
            property_value=property_value,
            property_index=index,
            induction_depth=spec.verification["universal_induction_depth"],
            timeout=spec.verification["universal_timeout_seconds"],
            requirement_ids=requirement_ids,
        )
        _, dut = _trusted_run_text_file(
            artifacts.get("dut"), run_dir, label=f"{property_value['id']} DUT"
        )
        _, harness = _trusted_run_text_file(
            artifacts.get("harness"), run_dir, label=f"{property_value['id']} harness"
        )
        _, script = _trusted_run_text_file(
            artifacts.get("script"), run_dir, label=f"{property_value['id']} script"
        )
        _, log = _trusted_run_text_file(
            artifacts.get("log"), run_dir, label=f"{property_value['id']} log"
        )
        if (
            dut != expected_dut
            or harness != expected["harness"]
            or script != expected["script"]
        ):
            raise RuntimeError(
                f"release evidence for universal property {property_value['id']} "
                "does not match the deterministic harness or script"
            )
        returncode = observed.get("returncode")
        if isinstance(returncode, bool) or (
            returncode is not None and not isinstance(returncode, int)
        ):
            raise RuntimeError(
                f"release evidence for universal property {property_value['id']} has an invalid return code"
            )
        attested_status = classify_universal_execution_log(returncode, log)
        if observed.get("status") != attested_status:
            raise RuntimeError(
                f"release evidence for universal property {property_value['id']} "
                "disagrees with its Yosys transcript"
            )
        total_bytes += len(harness.encode()) + len(script.encode()) + len(log.encode())
        if total_bytes > 16 * 1024 * 1024:
            raise RuntimeError("universal execution evidence exceeds the 16 MiB release limit")
        entries.append(
            {
                "property_index": index,
                "id": property_value["id"],
                "property_sha256": content_hash(property_value),
                "status": observed.get("status"),
                "executed": executed,
                "returncode": returncode,
                "harness_sha256": content_hash(harness),
                "script_sha256": content_hash(script),
                "log_sha256": content_hash(log),
                "harness": harness,
                "script": script,
                "log": log,
            }
        )
    evidence = {
        "schema_version": 1,
        "provider": "yosys_sat_universal_properties",
        "verify_image": verify_image_ref(),
        "top": spec.name,
        "rtl_sha256": content_hash(rtl),
        "dut": expected_dut,
        "dut_sha256": hashlib.sha256(expected_dut.encode()).hexdigest(),
        "properties_sha256": content_hash(requested),
        "property_count": len(requested),
        "entries": entries,
    }
    evidence["evidence_sha256"] = content_hash(evidence)
    return evidence


def _validate_universal_execution_evidence(
    spec: DesignSpec,
    rtl: str,
    report: dict[str, Any],
    evidence: dict[str, Any],
) -> None:
    payload = dict(evidence)
    digest = payload.pop("evidence_sha256", None)
    requested = spec.verification["universal_properties"]
    rows = report.get("properties")
    entries = payload.get("entries")
    if (
        digest != content_hash(payload)
        or payload.get("schema_version") != 1
        or payload.get("provider") != "yosys_sat_universal_properties"
        or payload.get("verify_image") != verify_image_ref()
        or payload.get("top") != spec.name
        or payload.get("rtl_sha256") != content_hash(rtl)
        or payload.get("dut") != rtl + "\n"
        or payload.get("dut_sha256")
        != hashlib.sha256((rtl + "\n").encode()).hexdigest()
        or payload.get("properties_sha256") != content_hash(requested)
        or payload.get("property_count") != len(requested)
        or not isinstance(rows, list)
        or not isinstance(entries, list)
        or len(rows) != len(requested)
        or len(entries) != len(requested)
    ):
        raise RuntimeError("publication denied: universal execution evidence is invalid")
    requirement_ids = [row["id"] for row in spec.verification["requirements"]]
    for index, (property_value, observed, entry) in enumerate(
        zip(requested, rows, entries), 1
    ):
        if not isinstance(observed, dict) or not isinstance(entry, dict):
            raise RuntimeError("publication denied: universal execution evidence is invalid")
        expected = render_universal_execution_inputs(
            top=spec.name,
            ports=spec.ports,
            clock_port=spec.clock_port,
            reset=spec.reset,
            property_value=property_value,
            property_index=index,
            induction_depth=spec.verification["universal_induction_depth"],
            timeout=spec.verification["universal_timeout_seconds"],
            requirement_ids=requirement_ids,
        )
        harness = entry.get("harness")
        script = entry.get("script")
        log = entry.get("log")
        returncode = entry.get("returncode")
        executed = entry.get("executed")
        if (
            entry.get("property_index") != index
            or entry.get("id") != property_value["id"]
            or entry.get("property_sha256") != content_hash(property_value)
            or entry.get("status") != observed.get("status")
            or not isinstance(executed, bool)
            or executed is not observed.get("executed")
            or harness != expected["harness"]
            or script != expected["script"]
            or not isinstance(log, str)
            or not log
            or entry.get("harness_sha256") != content_hash(harness)
            or entry.get("script_sha256") != content_hash(script)
            or entry.get("log_sha256") != content_hash(log)
            or isinstance(returncode, bool)
            or (returncode is not None and not isinstance(returncode, int))
            or classify_universal_execution_log(returncode, log)
            != observed.get("status")
        ):
            raise RuntimeError("publication denied: universal execution evidence is invalid")


def _validate_formal_release_evidence(
    spec: DesignSpec,
    rtl: str,
    plan: dict[str, Any],
    expected_trace: dict[str, Any],
    property_map: Any,
    report: dict[str, Any],
    verification_report: Any,
    release_report: Any,
    execution_evidence: dict[str, Any],
) -> None:
    depth = spec.verification["formal_depth"]
    expected_properties, expected_map, expected_selection = build_formal_evidence_contract(
        spec, plan, expected_trace, depth
    )
    if (
        property_map != expected_map
        or content_hash(verification_report) != content_hash(report)
        or content_hash(release_report) != content_hash(report)
        or report.get("provider") != "yosys_bounded_trace"
        or report.get("authoritative") is not True
        or report.get("verification_method") != "bounded_trace_checking"
        or report.get("proof_scope")
        != "supported_unique_frozen_input_trace_obligations"
        or report.get("universal_proof") is not False
        or report.get("engine") != "yosys_internal_sat"
        or report.get("verify_image") != verify_image_ref()
        or report.get("rtl_sha256") != content_hash(rtl)
        or report.get("suite_sha256") != suite_hash(plan, expected_trace)
        or report.get("property_map_sha256") != content_hash(expected_map)
        or report.get("property_selection") != expected_selection
        or report.get("depth") != depth
        or report.get("property_count") != len(expected_map)
        or report.get("eligible_property_count") != expected_selection["eligible"]
        or report.get("selected_property_count") != expected_selection["selected"]
        or report.get("excluded_property_count") != expected_selection["excluded"]
        or report.get("properties_truncated") is not expected_selection["truncated"]
        or report.get("adaptive_selected_property_count")
        != expected_selection["adaptive_selected"]
        or report.get("batch_size") != FORMAL_BATCH_SIZE
        or report.get("minimum_batch_size") != FORMAL_MIN_BATCH_SIZE
    ):
        raise RuntimeError("publication denied: formal evidence contract is invalid")

    batches = report.get("batches")
    errors = report.get("errors")
    passed_ids = report.get("passed_property_ids")
    counterexamples = report.get("counterexamples")
    expected_batch_count = report.get("batch_count")
    reschedules = report.get("reschedules")
    if (
        isinstance(expected_batch_count, bool)
        or not isinstance(expected_batch_count, int)
        or expected_batch_count < 0
        or not isinstance(batches, list)
        or len(batches) != expected_batch_count
        or not isinstance(errors, list)
        or not isinstance(passed_ids, list)
        or not isinstance(counterexamples, list)
        or len(set(passed_ids)) != len(passed_ids)
        or not isinstance(reschedules, list)
        or report.get("reschedule_count") != len(reschedules)
    ):
        raise RuntimeError("publication denied: formal batch evidence is invalid")

    valid_ids = [row["property_id"] for row in expected_map]
    if any(value not in valid_ids for value in passed_ids):
        raise RuntimeError("publication denied: formal passed-property list is invalid")
    batch_statuses: list[str] = []
    infrastructure_statuses: list[str] = []
    completed_batch_count = 0
    next_property_index = 1
    for index, batch in enumerate(batches, 1):
        if not isinstance(batch, dict):
            raise RuntimeError("publication denied: formal batch evidence is invalid")
        start = batch.get("property_start_index")
        end = batch.get("property_end_index")
        count = batch.get("property_count")
        status = batch.get("status")
        infrastructure_status = batch.get("infrastructure_status")
        returncode = batch.get("returncode")
        success_verdict_count = batch.get("success_verdict_count")
        if (
            status not in {"pass", "fail", "unknown", "error", "not_run"}
            or batch.get("batch_index") != index
            or isinstance(start, bool)
            or not isinstance(start, int)
            or isinstance(end, bool)
            or not isinstance(end, int)
            or isinstance(count, bool)
            or not isinstance(count, int)
            or start != next_property_index
            or end != start + count - 1
            or count <= 0
            or count > FORMAL_BATCH_SIZE
            or (count < FORMAL_MIN_BATCH_SIZE and end != len(expected_map))
            or end > len(expected_map)
            or (
                infrastructure_status is not None
                and infrastructure_status not in {"unknown", "error", "not_run"}
            )
            or (status == "pass" and infrastructure_status is not None)
            or (status == "unknown" and infrastructure_status != "unknown")
            or (status == "error" and infrastructure_status != "error")
            or (status == "not_run" and infrastructure_status != "not_run")
            or (
                infrastructure_status is not None
                and not isinstance(batch.get("error"), dict)
            )
            or (
                isinstance(batch.get("error"), dict)
                and batch.get("error") not in errors
            )
            or isinstance(returncode, bool)
            or (returncode is not None and not isinstance(returncode, int))
            or isinstance(success_verdict_count, bool)
            or not isinstance(success_verdict_count, int)
            or success_verdict_count < 0
            or (
                status == "pass"
                and (returncode != 0 or success_verdict_count != count)
            )
        ):
            raise RuntimeError("publication denied: formal batch evidence is invalid")
        next_property_index = end + 1
        batch_statuses.append(status)
        if infrastructure_status is not None:
            infrastructure_statuses.append(infrastructure_status)
        if status in {"pass", "fail"} and infrastructure_status is None:
            completed_batch_count += 1

    if next_property_index != len(expected_map) + 1:
        raise RuntimeError("publication denied: formal batch evidence omits obligations")
    for row in reschedules:
        if not isinstance(row, dict):
            raise RuntimeError("publication denied: formal reschedule evidence is invalid")
        count = row.get("property_count")
        children = row.get("child_property_counts")
        detail = row.get("detail")
        returncode = row.get("returncode")
        resource_marker = isinstance(detail, str) and any(
            marker in detail.casefold()
            for marker in (
                "out of memory",
                "oom-kill",
                "oom killed",
                "cannot allocate memory",
                "memory cgroup out of memory",
                "killed process",
            )
        )
        if (
            row.get("reason") != "resource_pressure"
            or isinstance(count, bool)
            or not isinstance(count, int)
            or count < FORMAL_MIN_BATCH_SIZE * 2
            or not isinstance(children, list)
            or len(children) != 2
            or any(
                isinstance(value, bool)
                or not isinstance(value, int)
                or value < FORMAL_MIN_BATCH_SIZE
                for value in children
            )
            or sum(children) != count
            or (returncode not in {137, -9} and not resource_marker)
            or not isinstance(detail, str)
            or row.get("log_sha256")
            != hashlib.sha256(detail.encode()).hexdigest()
        ):
            raise RuntimeError("publication denied: formal reschedule evidence is invalid")

    if "fail" in batch_statuses:
        derived_status = "fail"
    elif "error" in batch_statuses:
        derived_status = "error"
    elif set(batch_statuses) & {"unknown", "not_run"}:
        derived_status = "unknown"
    elif batch_statuses:
        derived_status = "pass"
    elif expected_selection["unsupported_scenario_count"]:
        derived_status = "pass"
    else:
        derived_status = "unknown"
    selection_complete = bool(
        not expected_selection["truncated"]
        and expected_selection["excluded"] == 0
        and expected_selection["scenario_fairness_complete"]
        and expected_selection["requirement_fairness_complete"]
    )
    execution_complete = bool(
        len(batches) == expected_batch_count
        and all(
            row["status"] in {"pass", "fail"}
            and row.get("infrastructure_status") is None
            for row in batches
        )
    )
    evidence_complete = selection_complete and execution_complete
    completeness = (
        "complete_for_all_unique_bounded_trace_obligations"
        if evidence_complete
        else "incomplete_execution"
        if selection_complete
        else "incomplete_selection"
    )
    expected_infrastructure = sorted(set(infrastructure_statuses))
    if (
        report.get("status") != derived_status
        or report.get("selection_complete") is not selection_complete
        or report.get("execution_complete") is not execution_complete
        or report.get("evidence_complete") is not evidence_complete
        or report.get("completeness") != completeness
        or report.get("completed_batch_count") != completed_batch_count
        or report.get("infrastructure_errors_present") is not bool(errors)
        or report.get("infrastructure_error_count") != len(errors)
        or report.get("infrastructure_statuses") != expected_infrastructure
        or report.get("error") != (errors[0] if errors else None)
        or not isinstance(report.get("detail"), str)
        or not report["detail"]
    ):
        raise RuntimeError("publication denied: formal aggregate evidence is invalid")
    if derived_status == "pass" and passed_ids != valid_ids:
        raise RuntimeError("publication denied: formal pass omits obligations")
    if derived_status == "pass" and errors:
        raise RuntimeError("publication denied: formal pass contains infrastructure errors")
    if derived_status == "fail":
        if not counterexamples:
            raise RuntimeError("publication denied: formal failure has no counterexample")
    elif counterexamples:
        raise RuntimeError("publication denied: formal counterexample contradicts its status")
    for counterexample in counterexamples:
        if not isinstance(counterexample, dict):
            raise RuntimeError("publication denied: formal counterexample is invalid")
        property_index = counterexample.get("property_index")
        if (
            isinstance(property_index, bool)
            or not isinstance(property_index, int)
            or not 1 <= property_index <= len(expected_map)
        ):
            raise RuntimeError("publication denied: formal counterexample is invalid")
        expected = expected_map[property_index - 1]
        for key in (
            "property_id",
            "canonical_sha256",
            "aliases",
            "provenance",
            "scenario_index",
            "scenario_id",
            "requirement_ids",
            "cycle",
            "phase",
            "signal",
            "expected",
        ):
            if counterexample.get(key) != expected.get(key):
                raise RuntimeError("publication denied: formal counterexample is invalid")
    if spec.verification["formal_mode"] == "required" and (
        derived_status != "pass" or not evidence_complete
    ):
        raise RuntimeError("publication denied: required formal evidence is incomplete")

    execution_payload = dict(execution_evidence)
    execution_digest = execution_payload.pop("evidence_sha256", None)
    entries = execution_payload.get("entries")
    if (
        execution_digest != content_hash(execution_payload)
        or report.get("execution_evidence_sha256") != execution_digest
        or execution_payload.get("schema_version") != 2
        or execution_payload.get("provider") != "yosys_bounded_trace_execution"
        or execution_payload.get("engine") != "yosys_internal_sat"
        or execution_payload.get("verify_image") != verify_image_ref()
        or execution_payload.get("rtl_sha256") != content_hash(rtl)
        or execution_payload.get("suite_sha256") != suite_hash(plan, expected_trace)
        or execution_payload.get("property_map_sha256") != content_hash(expected_map)
        or execution_payload.get("batch_size") != FORMAL_BATCH_SIZE
        or execution_payload.get("minimum_batch_size") != FORMAL_MIN_BATCH_SIZE
        or execution_payload.get("batch_count") != expected_batch_count
        or execution_payload.get("reschedule_count")
        != report.get("reschedule_count")
        or execution_payload.get("reschedules") != report.get("reschedules")
        or not isinstance(entries, list)
        or len(entries) != expected_batch_count
    ):
        raise RuntimeError("publication denied: formal execution evidence is invalid")
    expected_dut_sha256 = hashlib.sha256((rtl + "\n").encode()).hexdigest()
    total_execution_bytes = 0
    for index, (batch, entry) in enumerate(zip(batches, entries), 1):
        if not isinstance(entry, dict):
            raise RuntimeError("publication denied: formal execution evidence is invalid")
        start = int(batch["property_start_index"]) - 1
        end = int(batch["property_end_index"])
        batch_properties = expected_properties[start:end]
        executed = batch.get("artifact") is not None
        if (
            entry.get("batch_index") != index
            or entry.get("status") != batch.get("status")
            or entry.get("executed") is not executed
            or entry.get("returncode") != batch.get("returncode")
            or entry.get("property_start_index") != batch.get("property_start_index")
            or entry.get("property_end_index") != batch.get("property_end_index")
            or entry.get("property_count") != batch.get("property_count")
            or entry.get("success_verdict_count")
            != batch.get("success_verdict_count")
        ):
            raise RuntimeError("publication denied: formal execution evidence is invalid")
        if not executed:
            if any(
                entry.get(key) is not None
                for key in (
                    "dut_sha256",
                    "script_sha256",
                    "log_sha256",
                    "script",
                    "log",
                )
            ):
                raise RuntimeError("publication denied: formal execution evidence is invalid")
            continue
        script = entry.get("script")
        log = entry.get("log")
        if not isinstance(script, str) or not isinstance(log, str) or not log:
            raise RuntimeError("publication denied: formal execution evidence is invalid")
        total_execution_bytes += len(script.encode()) + len(log.encode())
        success_count = len(FORMAL_SUCCESS_PATTERN.findall(log))
        if (
            total_execution_bytes > 64 * 1024 * 1024
            or script != render_formal_batch_script(spec, batch_properties)
            or entry.get("dut_sha256") != expected_dut_sha256
            or entry.get("script_sha256")
            != hashlib.sha256(script.encode()).hexdigest()
            or entry.get("log_sha256") != hashlib.sha256(log.encode()).hexdigest()
            or entry.get("success_verdict_count") != success_count
            or (
                batch.get("status") == "pass"
                and (
                    entry.get("returncode") != 0
                    or success_count != len(batch_properties)
                )
            )
        ):
            raise RuntimeError("publication denied: formal execution evidence is invalid")


def _independent_failure_fingerprint(
    *, seed: int, verification: dict[str, Any]
) -> str:
    mismatches = verification.get("mismatches")
    earliest_test: int | None = None
    earliest_rows: list[dict[str, Any]] = []
    if isinstance(mismatches, list):
        indexes = [
            row.get("test")
            for row in mismatches
            if isinstance(row, dict)
            and isinstance(row.get("test"), int)
            and not isinstance(row.get("test"), bool)
            and row.get("test") >= 0
        ]
        if indexes:
            earliest_test = min(indexes)
            matching_rows = [
                row
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


def _validate_independent_run(
    row: Any,
    *,
    expected_run: int,
    expected_seed: int,
    expected_num_tests: int,
    expected_stimuli: list[dict[str, int]],
    expected_inputs: dict[str, int],
    expected_outputs: dict[str, int],
    require_pass: bool,
) -> dict[str, Any]:
    envelope_keys = {"run", "seed", "num_tests", "passed", "verification"}
    if (
        not isinstance(row, dict)
        or set(row) != envelope_keys
        or row.get("run") != expected_run
        or row.get("seed") != expected_seed
        or row.get("num_tests") != expected_num_tests
        or not isinstance(row.get("passed"), bool)
        or not isinstance(row.get("verification"), dict)
    ):
        raise RuntimeError("publication denied: independent run envelope is invalid")
    verification = row["verification"]
    errors = [
        verification.get(name) for name in ("sv_error", "py_error", "meta_error")
    ]
    if any(value is not None and not isinstance(value, str) for value in errors):
        raise RuntimeError(
            "publication denied: independent verifier error fields are invalid"
        )
    stimulus_error = validate_independent_stimuli(
        verification.get("stimuli"),
        expected_stimuli=expected_stimuli,
        expected_inputs=expected_inputs,
    )
    if verification.get("num_tests") != expected_num_tests or stimulus_error:
        detail = stimulus_error or "verifier test count drifted"
        raise RuntimeError(
            f"publication denied: independent evidence is invalid: {detail}"
        )
    if any(value is not None for value in errors):
        if row["passed"] is not False or require_pass:
            raise RuntimeError(
                "publication denied: independent execution error contradicts pass"
            )
        return verification
    transcript_error = validate_independent_transcript(
        verification,
        expected_stimuli=expected_stimuli,
        expected_inputs=expected_inputs,
        expected_outputs=expected_outputs,
    )
    if transcript_error:
        raise RuntimeError(
            "publication denied: independent verifier transcript is invalid: "
            + transcript_error
        )
    derived_passed = verification["mismatched"] == 0
    if row["passed"] is not derived_passed or (require_pass and not derived_passed):
        raise RuntimeError(
            "publication denied: independent pass disposition is invalid"
        )
    return verification


@contextmanager
def _state_lock(state_file: Path):
    lock_path = state_file.with_name("state.lock")
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    with lock_path.open("a+", encoding="utf-8") as handle:
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


@contextmanager
def _operation_lock(state_file: Path):
    lock_path = state_file.with_name("operation.lock")
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    with lock_path.open("a+", encoding="utf-8") as handle:
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


def _prepare_state_locked(
    state: dict[str, Any], *, timestamp: str | None = None
) -> None:
    state["updated_at"] = timestamp or now_iso()
    state["revision"] = int(state.get("revision", 0)) + 1


def _persist_state_locked(state: dict[str, Any], state_file: Path) -> None:
    atomic_write(state_file, json.dumps(state, indent=2, sort_keys=True) + "\n")


def _write_state_locked(state: dict[str, Any], state_file: Path) -> None:
    _prepare_state_locked(state)
    _persist_state_locked(state, state_file)


def _launch_token_sha256(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def _launch_fence_matches(
    state: dict[str, Any], *, generation: int, token_sha256: str
) -> bool:
    stored_generation = state.get("launch_generation")
    stored_token_sha256 = state.get("launch_token_sha256")
    return bool(
        not isinstance(stored_generation, bool)
        and isinstance(stored_generation, int)
        and stored_generation == generation
        and isinstance(stored_token_sha256, str)
        and secrets.compare_digest(stored_token_sha256, token_sha256)
    )


def _assert_active_worker_fence(state: dict[str, Any]) -> None:
    fence = _ACTIVE_WORKER_FENCE
    if fence is None:
        return
    run_id, generation, token_sha256 = fence
    if state.get("run_id") != run_id or not _launch_fence_matches(
        state,
        generation=generation,
        token_sha256=token_sha256,
    ):
        raise WorkerFenceError(
            "pipeline worker launch generation was superseded"
        )


def _public_failure(value: object) -> dict[str, Any] | None:
    if not isinstance(value, dict):
        return None
    result = {
        key: value.get(key)
        for key in (
            "code",
            "category",
            "domain",
            "repairable",
            "repairable_by_rtl",
            "retryable",
            "user_action_required",
            "provider",
        )
        if isinstance(value.get(key), (str, bool, int, float, type(None)))
    }
    if value.get("message") is not None:
        result["message"] = str(value["message"])[:4096]
    evidence = value.get("evidence")
    if isinstance(evidence, list):
        result["evidence"] = [str(item)[:4096] for item in evidence[:16]]
    return result


def _build_event_locked(
    state: dict[str, Any], *, previous_event_sha256: str | None
) -> dict[str, Any]:
    model_request = state.get("model_request")
    if isinstance(model_request, dict):
        model_request = {
            key: model_request.get(key)
            for key in (
                "event",
                "request_id",
                "candidate_index",
                "candidate_count",
                "attempt",
                "max_attempts",
                "active_request_count",
                "elapsed_seconds",
                "first_event_seconds",
                "chunk_count",
                "http_status",
                "error",
            )
            if key in model_request
        }
    repair = state.get("repair")
    if isinstance(repair, dict):
        repair = {
            key: repair.get(key)
            for key in (
                "round",
                "max_rounds",
                "candidate",
                "candidate_count",
                "action",
                "result",
                "failure_class",
                "reason",
                "stagnation",
            )
            if key in repair
        }
    coverage = state.get("coverage")
    if isinstance(coverage, dict):
        coverage = {
            key: coverage.get(key)
            for key in (
                "status",
                "mode",
                "metric",
                "round",
                "max_rounds",
                "coverage_percent",
                "coverage_target_pct",
                "target_met",
                "remaining_cycle_budget",
                "report",
                "error",
            )
            if key in coverage
        }
    verification = state.get("verification")
    if isinstance(verification, dict):
        verification = {
            "passed": verification.get("passed"),
            "suite_sha256": verification.get("suite_sha256"),
            "rtl_sha256": verification.get("rtl_sha256"),
            "formal_status": (verification.get("formal") or {}).get("status")
            if isinstance(verification.get("formal"), dict)
            else None,
            "universal_status": (
                verification.get("universal_properties") or {}
            ).get("status")
            if isinstance(verification.get("universal_properties"), dict)
            else None,
            "verilator_status": (verification.get("verilator") or {}).get("status")
            if isinstance(verification.get("verilator"), dict)
            else None,
        }
    architecture = state.get("architecture_audit")
    if isinstance(architecture, dict):
        architecture = {
            "status": architecture.get("status"),
            "failed_checks": architecture.get("failed_checks") or [],
            "artifact": architecture.get("artifact"),
        }
    equivalence = state.get("synthesis_equivalence")
    if isinstance(equivalence, dict):
        equivalence = {
            "status": equivalence.get("status"),
            "proven_cells": equivalence.get("proven_cells"),
            "equivalence_cells": equivalence.get("equivalence_cells"),
            "artifact": equivalence.get("artifact"),
        }
    event = {
        "schema_version": 3,
        "event_id": f"{state.get('run_id', 'unknown')}:{state['event_sequence']}",
        "run_id": state.get("run_id"),
        "workflow_id": (state.get("origin") or {}).get("workflow_id"),
        "lane_run_id": state.get("lane_run_id"),
        "lane_attempt_id": state.get("active_lane_attempt_id"),
        "sequence": state["event_sequence"],
        "state_revision": state.get("revision"),
        "timestamp": state.get("last_event_timestamp") or state.get("updated_at"),
        "previous_event_sha256": previous_event_sha256,
        "type": (
            "run_terminal"
            if state.get("status") in {"completed", "failed", "incomplete", "stopped"}
            else "state_transition"
        ),
        "terminal": state.get("status")
        in {"completed", "failed", "incomplete", "stopped"},
        "status": state["status"],
        "phase": state["phase"],
        "model_request": model_request,
        "repair": repair,
        "coverage": coverage,
        "verification": verification,
        "architecture_audit": architecture,
        "synthesis_equivalence": equivalence,
        "evidence": {
            key: state.get(key)
            for key in (
                "publication_manifest",
                "publication_bundle_sha256",
                "lane_input_bundle_sha256",
            )
            if state.get(key) is not None
        },
        "error": state.get("error"),
        "failure": _public_failure(state.get("failure")),
        "failure_class": state.get("failure_class"),
        "failure_gate": state.get("failure_gate"),
        "failure_code": state.get("failure_code"),
    }
    encoded = json.dumps(event, sort_keys=True, separators=(",", ":")) + "\n"
    if len(encoded.encode("utf-8")) > 63 * 1024:
        event.update(
            {
                "payload_truncated": True,
                "model_request": None,
                "repair": None,
                "coverage": None,
                "verification": None,
                "architecture_audit": None,
                "synthesis_equivalence": None,
                "error": str(state.get("error") or "")[:4096] or None,
            }
        )
    event["event_sha256"] = content_hash(event)
    if len(
        (json.dumps(event, sort_keys=True, separators=(",", ":")) + "\n").encode(
            "utf-8"
        )
    ) > 64 * 1024:
        raise RuntimeError("compacted event exceeds the 64 KiB journal limit")
    return event


def _append_event_locked(
    state: dict[str, Any], event: dict[str, Any], *, state_file: Path
) -> None:
    event_file = state_file.with_name("events.jsonl")
    event_file.parent.mkdir(parents=True, exist_ok=True)
    encoded = json.dumps(event, sort_keys=True, separators=(",", ":")) + "\n"
    descriptor = os.open(
        event_file,
        os.O_RDWR
        | os.O_APPEND
        | os.O_CREAT
        | getattr(os, "O_CLOEXEC", 0)
        | getattr(os, "O_NOFOLLOW", 0),
        0o600,
    )
    try:
        if not stat.S_ISREG(os.fstat(descriptor).st_mode):
            raise RuntimeError("event journal is not a regular file")
        os.fchmod(descriptor, 0o600)
        end = os.lseek(descriptor, 0, os.SEEK_END)
        if end > 0 and os.pread(descriptor, 1, end - 1) != b"\n":
            os.write(descriptor, b"\n")
        remaining = memoryview(encoded.encode("utf-8"))
        while remaining:
            written = os.write(descriptor, remaining)
            if written <= 0:
                raise OSError("could not append the RTL-Closure event journal")
            remaining = remaining[written:]
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _event_digest(
    event: Any, *, expected_run_id: str, expected_sequence: int | None = None
) -> str:
    if not isinstance(event, dict):
        raise RuntimeError("event journal contains a non-object record")
    sequence = event.get("sequence")
    if (
        isinstance(sequence, bool)
        or not isinstance(sequence, int)
        or sequence < 1
        or (expected_sequence is not None and sequence != expected_sequence)
        or event.get("run_id") != expected_run_id
        or event.get("event_id") != f"{expected_run_id}:{sequence}"
    ):
        raise RuntimeError("event journal record identity or sequence is invalid")
    if event.get("schema_version") != 3:
        raise RuntimeError("event journal record schema is unsupported")
    payload = dict(event)
    claimed = payload.pop("event_sha256", None)
    previous = payload.get("previous_event_sha256")
    if previous is not None and (
        not isinstance(previous, str) or not re.fullmatch(r"[0-9a-f]{64}", previous)
    ):
        raise RuntimeError("event journal previous hash is invalid")
    actual = content_hash(payload)
    if claimed != actual:
        raise RuntimeError("event journal record hash is invalid")
    return actual


def _last_event_locked(state_file: Path, *, run_id: str) -> tuple[dict[str, Any], str] | None:
    event_file = state_file.with_name("events.jsonl")
    if not event_file.exists():
        return None
    if event_file.is_symlink():
        raise RuntimeError("event journal path identity is invalid")
    descriptor = os.open(
        event_file,
        os.O_RDONLY
        | getattr(os, "O_CLOEXEC", 0)
        | getattr(os, "O_NOFOLLOW", 0)
        | getattr(os, "O_NONBLOCK", 0),
    )
    try:
        metadata = os.fstat(descriptor)
        if not stat.S_ISREG(metadata.st_mode):
            raise RuntimeError("event journal is not a regular file")
        size = metadata.st_size
        if size == 0:
            return None
        if size > MAX_EVENT_JOURNAL_BYTES:
            raise RuntimeError("event journal exceeds the 64 MiB verification limit")
        if os.pread(descriptor, 1, size - 1) != b"\n":
            raise RuntimeError("event journal has an incomplete trailing record")
        raw = os.pread(descriptor, size, 0)
        if len(raw) != size:
            raise RuntimeError("event journal could not be read completely")
        previous_digest: str | None = None
        last_event: dict[str, Any] | None = None
        try:
            rows = raw.decode("utf-8").splitlines()
        except UnicodeDecodeError as exc:
            raise RuntimeError("event journal contains invalid UTF-8") from exc
        for sequence, encoded_event in enumerate(rows, 1):
            if not encoded_event:
                raise RuntimeError("event journal contains an empty record")
            try:
                event = json.loads(encoded_event)
            except json.JSONDecodeError as exc:
                raise RuntimeError("event journal contains invalid JSON") from exc
            digest = _event_digest(
                event, expected_run_id=run_id, expected_sequence=sequence
            )
            if event.get("previous_event_sha256") != previous_digest:
                raise RuntimeError("event journal hash chain is invalid")
            previous_digest = digest
            last_event = event
        if last_event is None:
            return None
        return last_event, previous_digest
    finally:
        os.close(descriptor)


def _pending_event_path(state_file: Path) -> Path:
    return state_file.with_name("event-pending.json")


def _sync_directory(path: Path) -> None:
    descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_CLOEXEC", 0))
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _write_pending_event_locked(state_file: Path, pending: dict[str, Any]) -> None:
    path = _pending_event_path(state_file)
    atomic_write(path, json.dumps(pending, indent=2, sort_keys=True) + "\n", 0o600)
    descriptor = os.open(
        path,
        os.O_RDONLY
        | getattr(os, "O_CLOEXEC", 0)
        | getattr(os, "O_NOFOLLOW", 0)
        | getattr(os, "O_NONBLOCK", 0),
    )
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)
    _sync_directory(path.parent)


def _clear_pending_event_locked(state_file: Path) -> None:
    path = _pending_event_path(state_file)
    try:
        path.unlink()
    except FileNotFoundError:
        return
    _sync_directory(path.parent)


def _read_pending_event_locked(state_file: Path) -> dict[str, Any] | None:
    path = _pending_event_path(state_file)
    if not path.exists():
        return None
    if path.is_symlink():
        raise RuntimeError("pending event journal path identity is invalid")
    descriptor = os.open(
        path,
        os.O_RDONLY
        | getattr(os, "O_CLOEXEC", 0)
        | getattr(os, "O_NOFOLLOW", 0)
        | getattr(os, "O_NONBLOCK", 0),
    )
    try:
        metadata = os.fstat(descriptor)
        if not stat.S_ISREG(metadata.st_mode) or metadata.st_size > 128 * 1024:
            raise RuntimeError("pending event journal is invalid or too large")
        raw = os.read(descriptor, metadata.st_size + 1)
    finally:
        os.close(descriptor)
    try:
        pending = json.loads(raw)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise RuntimeError("pending event journal is invalid JSON") from exc
    if not isinstance(pending, dict):
        raise RuntimeError("pending event journal must be an object")
    payload = dict(pending)
    claimed = payload.pop("pending_sha256", None)
    if pending.get("schema_version") != 1 or claimed != content_hash(payload):
        raise RuntimeError("pending event journal hash is invalid")
    return pending


def _journal_position_locked(
    state: dict[str, Any], state_file: Path
) -> tuple[int, str | None]:
    run_id = state.get("run_id")
    sequence = state.get("event_sequence", 0)
    if (
        not isinstance(run_id, str)
        or isinstance(sequence, bool)
        or not isinstance(sequence, int)
        or sequence < 0
    ):
        raise RuntimeError("state event journal identity is invalid")
    last = _last_event_locked(state_file, run_id=run_id)
    if last is None:
        if sequence != 0:
            raise RuntimeError("event journal has a sequence gap")
        if state.get("last_event_sha256") is not None:
            raise RuntimeError("state names an event hash but the journal is empty")
        return 0, None
    event, digest = last
    journal_sequence = event["sequence"]
    if journal_sequence > sequence:
        raise RuntimeError("event journal is ahead of state")
    if journal_sequence < sequence:
        raise RuntimeError("event journal has a sequence gap")
    state_digest = state.get("last_event_sha256")
    if state_digest is not None and state_digest != digest:
        raise RuntimeError("state and event journal hashes disagree")
    if any(
        (
            event.get("status") != state.get("status"),
            event.get("phase") != state.get("phase"),
            event.get("failure_class") != state.get("failure_class"),
            event.get("failure_gate") != state.get("failure_gate"),
            event.get("failure_code") != state.get("failure_code"),
        )
    ):
        raise RuntimeError("state projection disagrees with the latest event")
    return journal_sequence, digest


def _recover_event_journal_locked(state: dict[str, Any], state_file: Path) -> str | None:
    """Finish or roll back one WAL record using the durable state as authority."""
    pending = _read_pending_event_locked(state_file)
    if pending is None:
        return _journal_position_locked(state, state_file)[1]
    event = pending.get("event")
    run_id = state.get("run_id")
    sequence = state.get("event_sequence")
    revision = state.get("revision")
    if (
        not isinstance(run_id, str)
        or isinstance(sequence, bool)
        or not isinstance(sequence, int)
        or sequence < 0
        or isinstance(revision, bool)
        or not isinstance(revision, int)
        or revision < 0
    ):
        raise RuntimeError("pending event journal has no matching state identity")
    pending_sequence = pending.get("sequence")
    if isinstance(pending_sequence, bool) or not isinstance(pending_sequence, int):
        raise RuntimeError("pending event journal sequence is invalid")

    # WAL was durable but the atomic state replacement did not happen. The
    # current state and journal still agree, so the uncommitted intent can be
    # discarded and the caller may retry the mutation.
    if pending_sequence == sequence + 1:
        journal_sequence, journal_digest = _journal_position_locked(state, state_file)
        event_digest = _event_digest(
            event, expected_run_id=run_id, expected_sequence=pending_sequence
        )
        if (
            journal_sequence != sequence
            or pending.get("run_id") != run_id
            or pending.get("state_revision") != revision + 1
            or pending.get("event_sha256") != event_digest
            or event.get("previous_event_sha256") != journal_digest
        ):
            raise RuntimeError("uncommitted pending event does not match durable state")
        _clear_pending_event_locked(state_file)
        return journal_digest

    if pending_sequence != sequence or sequence < 1 or revision < 1:
        raise RuntimeError("pending event journal has no matching state identity")
    event_digest = _event_digest(
        event, expected_run_id=run_id, expected_sequence=sequence
    )
    if (
        pending.get("run_id") != run_id
        or pending.get("sequence") != sequence
        or pending.get("state_revision") != revision
        or pending.get("state_sha256") != content_hash(state)
        or pending.get("event_sha256") != event_digest
        or state.get("last_event_sha256") != event_digest
    ):
        raise RuntimeError("pending event journal does not uniquely match current state")
    expected_event = _build_event_locked(
        state, previous_event_sha256=event.get("previous_event_sha256")
    )
    if event != expected_event:
        raise RuntimeError("pending event journal is not uniquely derivable from state")
    last = _last_event_locked(state_file, run_id=run_id)
    if last is None:
        journal_sequence, journal_digest = 0, None
    else:
        journal_sequence, journal_digest = last[0]["sequence"], last[1]
    if journal_sequence > sequence:
        raise RuntimeError("event journal is ahead of state")
    if journal_sequence < sequence - 1:
        raise RuntimeError("event journal has a sequence gap")
    if journal_sequence == sequence:
        if journal_digest != event_digest:
            raise RuntimeError("pending event conflicts with the durable event journal")
        _clear_pending_event_locked(state_file)
        return event_digest
    previous = event.get("previous_event_sha256")
    if journal_sequence != sequence - 1 or journal_digest != previous:
        raise RuntimeError("pending event predecessor does not match the journal")
    _append_event_locked(state, event, state_file=state_file)
    _clear_pending_event_locked(state_file)
    return event_digest


def save_state(state: dict[str, Any]) -> None:
    state_file = Path(state["state_file"])
    with _state_lock(state_file):
        if state_file.is_file():
            try:
                current = json.loads(state_file.read_text(encoding="utf-8"))
                if not isinstance(current, dict) or current.get("run_id") != state.get(
                    "run_id"
                ):
                    raise RuntimeError("state identity changed while saving")
                _recover_event_journal_locked(current, state_file)
                state["revision"] = max(
                    int(state.get("revision", 0)), int(current.get("revision", 0))
                )
                state["event_sequence"] = current.get("event_sequence", 0)
                for key in ("last_event_sha256", "last_event_timestamp"):
                    if key in current:
                        state[key] = current[key]
            except (OSError, json.JSONDecodeError) as exc:
                raise RuntimeError("existing RTL-Closure state cannot be read safely") from exc
        else:
            if state.get("event_sequence", 0) != 0:
                raise RuntimeError("new state cannot begin with a nonzero event sequence")
            if _pending_event_path(state_file).exists() or state_file.with_name(
                "events.jsonl"
            ).exists():
                raise RuntimeError("new state collides with an existing event journal")
        _write_state_locked(state, state_file)


def load_state(run_id: str | None = None) -> dict[str, Any]:
    if run_id is None:
        if not CURRENT.is_file():
            raise RuntimeError("no RTL-Closure run exists")
        run_id = CURRENT.read_text(encoding="utf-8").strip()
    if not RUN_ID_RE.fullmatch(run_id) or run_id in {".", ".."}:
        raise RuntimeError("invalid RTL-Closure run id")
    root = STATE_ROOT.resolve()
    run_dir = STATE_ROOT / run_id
    if run_dir.is_symlink() or run_dir.resolve().parent != root:
        raise RuntimeError("RTL-Closure run directory identity is invalid")
    path = run_dir / "state.json"
    if path.is_symlink():
        raise RuntimeError("RTL-Closure state path identity is invalid")
    if not path.is_file():
        raise RuntimeError(f"run does not exist: {run_id}")
    with _state_lock(path):
        state = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(state, dict) or state.get("run_id") != run_id:
            raise RuntimeError("RTL-Closure state identity is invalid")
        if state.get("schema_version") != 2:
            raise RuntimeError("RTL-Closure state schema is unsupported")
        _assert_active_worker_fence(state)
        _recover_event_journal_locked(state, path)

    # Runtime control paths are derived from the selected run, never trusted from
    # mutable JSON. Historical artifact paths remain untouched for replay.
    state.update(
        {
            "run_dir": str(run_dir),
            "state_file": str(path),
            "pipeline_log": str(run_dir / "pipeline.log"),
            "events_file": str(run_dir / "events.jsonl"),
            "spec_file": str(run_dir / "spec.json"),
        }
    )
    return state


def _load_frozen_spec(state: dict[str, Any]) -> DesignSpec:
    run_dir = Path(str(state["run_dir"])).resolve()
    spec_path = Path(str(state["spec_file"]))
    if (
        spec_path.is_symlink()
        or not spec_path.is_file()
        or spec_path.resolve().parent != run_dir
    ):
        raise RuntimeError("frozen specification path identity is invalid")
    spec = load_spec(spec_path)
    expected_sha256 = (state.get("origin") or {}).get("spec_sha256")
    if expected_sha256 is not None and content_hash(spec.raw) != expected_sha256:
        raise RuntimeError("frozen specification content changed after launch")
    return spec


def _state_started_epoch(state: dict[str, Any], path: Path) -> float:
    """Return a stable creation-order key without changing persisted state."""
    try:
        started_epoch = float(state.get("started_epoch"))
        if math.isfinite(started_epoch):
            return started_epoch
    except (TypeError, ValueError):
        pass

    started_at = state.get("started_at")
    if isinstance(started_at, str):
        try:
            return datetime.fromisoformat(started_at.replace("Z", "+00:00")).timestamp()
        except ValueError:
            pass

    try:
        return path.stat().st_mtime
    except OSError:
        return 0.0


def list_states(*, active_only: bool = False, limit: int = 100) -> list[dict[str, Any]]:
    """List compact persisted-run snapshots without reconciling or mutating them."""
    if not 1 <= limit <= 1000:
        raise ValueError("limit must be from 1 to 1000")
    if not STATE_ROOT.is_dir():
        return []

    states: list[tuple[float, str, dict[str, Any]]] = []
    for path in STATE_ROOT.glob("*/state.json"):
        try:
            state = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, json.JSONDecodeError):
            continue
        if not isinstance(state, dict) or state.get("schema_version") != 2:
            continue
        run_id = state.get("run_id")
        status = state.get("status")
        if (
            not isinstance(run_id, str)
            or not RUN_ID_RE.fullmatch(run_id)
            or run_id != path.parent.name
            or not isinstance(status, str)
        ):
            continue
        if active_only and status not in ACTIVE_STATUSES:
            continue
        origin = state.get("origin")
        workflow_id = origin.get("workflow_id") if isinstance(origin, dict) else None
        summary = {
            "schema_version": 1,
            "run_id": run_id,
            "status": status,
            "phase": state.get("phase"),
            "started_at": state.get("started_at"),
            "started_epoch": state.get("started_epoch"),
            "updated_at": state.get("updated_at"),
            "design_name": state.get("design_name"),
            "design_slug": state.get("design_slug"),
            "pdk": state.get("pdk"),
            "workflow_id": workflow_id,
            "lane_run_id": state.get("lane_run_id"),
            "state_file": str(path),
        }
        states.append((_state_started_epoch(state, path), run_id, summary))

    states.sort(key=lambda item: (item[0], item[1]), reverse=True)
    return [state for _, _, state in states[:limit]]


def _read_process_identity(pid: int | None) -> dict[str, Any] | None:
    """Read enough immutable /proc identity to distinguish PID reuse."""
    if not pid:
        return None
    try:
        stat_text = Path(f"/proc/{pid}/stat").read_text(encoding="utf-8")
        closing_paren = stat_text.rfind(")")
        if closing_paren < 0:
            return None
        fields = stat_text[closing_paren + 2 :].split()
        if len(fields) <= 19 or fields[0] == "Z":
            return None
        cmdline = Path(f"/proc/{pid}/cmdline").read_bytes()
        if not cmdline:
            return None
        os.kill(pid, 0)
        return {
            "pid": int(pid),
            "start_time_ticks": int(fields[19]),
            "cmdline_sha256": hashlib.sha256(cmdline).hexdigest(),
        }
    except (OSError, ValueError):
        return None


def _process_identity_matches(
    pid: int | None, expected: dict[str, Any] | None
) -> bool:
    if not isinstance(expected, dict):
        return False
    observed = _read_process_identity(pid)
    return bool(
        observed
        and expected.get("pid") == observed["pid"]
        and expected.get("start_time_ticks") == observed["start_time_ticks"]
        and expected.get("cmdline_sha256") == observed["cmdline_sha256"]
    )


def pid_alive(pid: int | None, identity: dict[str, Any] | None = None) -> bool:
    if identity is not None:
        return _process_identity_matches(pid, identity)
    return _read_process_identity(pid) is not None


def _worker_alive(state: dict[str, Any], *, require_identity: bool = False) -> bool:
    identity = state.get("worker_process")
    if isinstance(identity, dict):
        return pid_alive(state.get("pid"), identity)
    if require_identity:
        return False
    return pid_alive(state.get("pid"))


def _terminate_spawned_process(process: subprocess.Popen[Any]) -> None:
    """Best-effort cleanup for a child whose durable registration failed."""
    if not hasattr(process, "terminate"):
        return
    try:
        process.terminate()
        process.wait(timeout=2)
    except (OSError, ProcessLookupError, subprocess.TimeoutExpired):
        try:
            process.kill()
            process.wait(timeout=2)
        except (OSError, ProcessLookupError, subprocess.TimeoutExpired):
            pass


def make_run_id(name: str) -> str:
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    base = f"{stamp}-{name.lower()}"
    candidate = base
    index = 2
    while (STATE_ROOT / candidate).exists():
        candidate = f"{base}-{index}"
        index += 1
    return candidate


def _launch_pipeline_worker(state: dict[str, Any]) -> dict[str, Any]:
    """Complete the durable worker hand-off for a newly created run."""
    run_id = str(state["run_id"])
    current = load_state(run_id)
    state.clear()
    state.update(current)
    if state.get("status") != "created" or state.get("phase") != "worker_launching":
        return state
    previous_generation = state.get("launch_generation", 0)
    if isinstance(previous_generation, bool) or not isinstance(
        previous_generation, int
    ) or previous_generation < 0:
        raise RuntimeError("pipeline worker launch generation is invalid")
    launch_generation = previous_generation + 1
    launch_token = secrets.token_urlsafe(32)
    launch_token_sha256 = _launch_token_sha256(launch_token)
    state = _update(
        state,
        phase="worker_launching",
        status="created",
        pid=None,
        worker_process=None,
        launch_generation=launch_generation,
        launch_token_sha256=launch_token_sha256,
        announce=False,
        record_event=False,
    )
    try:
        output = Path(state["pipeline_log"]).open("ab", buffering=0)
        try:
            worker_environment = os.environ.copy()
            worker_environment[WORKER_LAUNCH_TOKEN_ENV] = launch_token
            process = subprocess.Popen(
                [
                    sys.executable,
                    "-m",
                    "rtl_closure.cli",
                    "_worker",
                    "--run-id",
                    run_id,
                    "--launch-generation",
                    str(launch_generation),
                ],
                stdin=subprocess.DEVNULL,
                stdout=output,
                stderr=subprocess.STDOUT,
                start_new_session=True,
                env=worker_environment,
            )
        finally:
            output.close()
    except OSError as exc:
        state = _update(
            state,
            phase="failed",
            status="failed",
            error=f"pipeline worker could not be started: {exc}",
            failure_class="infrastructure",
            failure_gate="worker_launch",
            failure_code="infrastructure/worker_launch",
            elapsed_seconds=max(
                0, round(time.time() - float(state["started_epoch"]))
            ),
        )
        atomic_write(CURRENT, run_id + "\n")
        raise RuntimeError(state["error"]) from exc

    try:
        identity = _read_process_identity(process.pid)
        if identity is None:
            raise RuntimeError("pipeline worker identity could not be attested")
        state = _register_pipeline_worker_unlocked(
            state,
            launch_generation=launch_generation,
            launch_token_sha256=launch_token_sha256,
            pid=process.pid,
            identity=identity,
        )
        return state
    except Exception as exc:
        _terminate_spawned_process(process)
        try:
            _update(
                state,
                phase="failed",
                status="failed",
                error=f"pipeline worker registration failed: {exc}",
                failure_class="infrastructure",
                failure_gate="worker_launch",
                failure_code="infrastructure/worker_registration",
                elapsed_seconds=max(
                    0, round(time.time() - float(state["started_epoch"]))
                ),
            )
        except Exception:
            pass
        raise


def _register_pipeline_worker_unlocked(
    state: dict[str, Any],
    *,
    launch_generation: int,
    launch_token_sha256: str,
    pid: int,
    identity: dict[str, Any],
) -> dict[str, Any]:
    """Atomically register one worker while the run operation lock is held."""
    current = load_state(str(state["run_id"]))
    if not _launch_fence_matches(
        current,
        generation=launch_generation,
        token_sha256=launch_token_sha256,
    ):
        raise WorkerFenceError("pipeline worker launch generation was superseded")
    if (
        current.get("status") == "running"
        and current.get("phase") == "queued"
        and current.get("pid") == pid
        and current.get("worker_process") == identity
    ):
        atomic_write(CURRENT, str(current["run_id"]) + "\n")
        state.clear()
        state.update(current)
        return state
    if (
        current.get("status") != "created"
        or current.get("phase") != "worker_launching"
        or current.get("pid") is not None
        or current.get("worker_process") is not None
    ):
        raise WorkerFenceError("pipeline worker launch is no longer claimable")
    state.clear()
    state.update(current)
    state = _update(
        state,
        phase="queued",
        status="running",
        pid=pid,
        worker_process=identity,
        announce=False,
        record_event=False,
    )
    atomic_write(CURRENT, str(state["run_id"]) + "\n")
    return state


def _recover_created_worker(state: dict[str, Any]) -> dict[str, Any]:
    """Resume the only recoverable start boundary for the current state schema."""
    if state.get("schema_version") != 2:
        raise RuntimeError("RTL-Closure state schema is unsupported")
    if state.get("status") != "created" or state.get("phase") != "worker_launching":
        return state
    return _launch_pipeline_worker(state)


def start_pipeline(
    spec_path: Path,
    *,
    generate_only: bool = False,
    workflow_id: str | None = None,
    idempotency_key: str | None = None,
) -> dict[str, Any]:
    spec = load_spec(spec_path.resolve())
    for label, value in (
        ("workflow_id", workflow_id),
        ("idempotency_key", idempotency_key),
    ):
        if value is not None and (
            not isinstance(value, str)
            or not re.fullmatch(r"[A-Za-z0-9_.:@/-]{1,256}", value)
        ):
            raise ValueError(f"{label} contains unsupported characters")
    if generate_only and spec.flow["mapped_equivalence_mode"] == "required":
        raise ValueError(
            "--generate-only cannot satisfy required mapped equivalence; "
            "run the full LibreLane flow or set mapped_equivalence_mode=off"
        )
    STATE_ROOT.mkdir(parents=True, exist_ok=True, mode=0o700)
    spec_sha256 = content_hash(spec.raw)
    idempotency_scope = workflow_id or "rtl-closure-cli"
    with _operation_lock(STATE_ROOT / "start.json"):
        if idempotency_key is not None:
            for candidate in sorted(STATE_ROOT.glob("*/state.json")):
                try:
                    existing = json.loads(candidate.read_text(encoding="utf-8"))
                except (OSError, json.JSONDecodeError):
                    continue
                origin = existing.get("origin") or {}
                if origin.get("idempotency_key") != idempotency_key:
                    continue
                existing_scope = origin.get("idempotency_scope")
                if existing_scope != idempotency_scope:
                    continue
                if (
                    origin.get("spec_sha256") != spec_sha256
                    or bool(existing.get("generate_only")) != generate_only
                ):
                    raise ValueError(
                        "idempotency key is already bound to a different request"
                    )
                existing = load_state(candidate.parent.name)
                with _operation_lock(Path(existing["state_file"])):
                    existing = load_state(candidate.parent.name)
                    return _recover_created_worker(existing)

        run_id = make_run_id(spec.name)
        run_dir = STATE_ROOT / run_id
        run_dir.mkdir(mode=0o700)
        normalized_spec = run_dir / "spec.json"
        atomic_write(
            normalized_spec,
            json.dumps(spec.raw, indent=2, ensure_ascii=False) + "\n",
        )
        state: dict[str, Any] = {
            "schema_version": 2,
            "run_id": run_id,
            "run_dir": str(run_dir),
            "state_file": str(run_dir / "state.json"),
            "pipeline_log": str(run_dir / "pipeline.log"),
            "events_file": str(run_dir / "events.jsonl"),
            "event_sequence": 0,
            "spec_file": str(normalized_spec),
            "design_name": spec.name,
            "pdk": spec.pdk,
            "mapped_equivalence_mode": spec.flow["mapped_equivalence_mode"],
            "status": "created",
            "phase": "worker_launching",
            "generate_only": generate_only,
            "pid": None,
            "worker_process": None,
            "launch_generation": 0,
            "launch_token_sha256": None,
            "lane_run_id": None,
            "lane_attempts": [],
            "active_lane_attempt_id": None,
            "started_at": now_iso(),
            "started_epoch": time.time(),
            "origin": {
                "source": "pi-lane" if workflow_id else "rtl-closure-cli",
                "workflow_id": workflow_id or run_id,
                "idempotency_key": idempotency_key,
                "idempotency_scope": idempotency_scope,
                "spec_sha256": spec_sha256,
            },
        }
        save_state(state)
        # The worker waits for this durable hand-off before doing any work. The
        # caller owns the CLI output contract, so start --json emits one object.
        with _operation_lock(Path(state["state_file"])):
            return _launch_pipeline_worker(state)


def _update(
    state: dict[str, Any],
    *,
    phase: str | None = None,
    status: str | None = None,
    announce: bool = True,
    record_event: bool = True,
    allow_terminal_transition: bool = False,
    **values: Any,
) -> dict[str, Any]:
    state_file = Path(state["state_file"])
    with _state_lock(state_file):
        current = dict(state)
        if state_file.is_file():
            try:
                current = json.loads(state_file.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError) as exc:
                raise RuntimeError("existing RTL-Closure state cannot be read safely") from exc
            if not isinstance(current, dict) or current.get("run_id") != state.get("run_id"):
                raise RuntimeError("RTL-Closure state identity changed before update")
        _assert_active_worker_fence(current)
        previous_event_sha256 = _recover_event_journal_locked(current, state_file)
        if (
            current.get("status") in TERMINAL_STATUSES
            and not allow_terminal_transition
        ):
            state.clear()
            state.update(current)
            return state
        if phase is not None:
            current["phase"] = phase
        if status is not None:
            current["status"] = status
        current.update(values)
        if record_event:
            current["events_file"] = str(state_file.with_name("events.jsonl"))
            current["event_sequence"] = int(current.get("event_sequence", 0)) + 1
            timestamp = now_iso()
            current["last_event_timestamp"] = timestamp
            _prepare_state_locked(current, timestamp=timestamp)
            event = _build_event_locked(
                current, previous_event_sha256=previous_event_sha256
            )
            current["last_event_sha256"] = event["event_sha256"]
            pending: dict[str, Any] = {
                "schema_version": 1,
                "run_id": current.get("run_id"),
                "sequence": current["event_sequence"],
                "state_revision": current["revision"],
                "state_sha256": content_hash(current),
                "event_sha256": event["event_sha256"],
                "event": event,
            }
            pending["pending_sha256"] = content_hash(pending)
            _write_pending_event_locked(state_file, pending)
            _persist_state_locked(current, state_file)
            _append_event_locked(current, event, state_file=state_file)
            _clear_pending_event_locked(state_file)
        else:
            _write_state_locked(current, state_file)
    state.clear()
    state.update(current)
    if announce:
        print(
            f"[{now_iso()}] phase={current['phase']} status={current['status']}",
            flush=True,
        )
    return state


def _record_repair_outcome(
    spec: DesignSpec,
    outcome: Any,
    rtl: str,
    run_dir: Path,
    adaptive_corpus: dict[str, Any],
    state: dict[str, Any],
) -> tuple[dict[str, Any], dict[str, Any]]:
    """Persist the final repair verdict and stop failures before release work."""

    raw_universal = outcome.verification.get("universal_properties") or {}
    final_universal = bind_universal_requirement_coverage(
        raw_universal,
        [row["id"] for row in spec.verification["requirements"]],
    )
    if not final_universal.get("status"):
        universal_requested = bool(
            spec.verification["universal_mode"] != "off"
            and spec.verification["universal_properties"]
        )
        final_universal.update(
            {
                "status": "not_run" if universal_requested else "not_requested",
                "property_count": 0,
                "requested_property_count": len(
                    spec.verification["universal_properties"]
                ),
                "passed_property_count": 0,
                "failed_property_ids": [],
                "universal_proof": False,
            }
        )
        if universal_requested and not outcome.verification.get("passed"):
            final_universal["blocked_by"] = outcome.verification.get(
                "failure_class"
            ) or "prior_verification_gate"
    outcome.verification["universal_properties"] = final_universal
    atomic_write(run_dir / f"{spec.name}.sv", rtl + "\n")
    atomic_write(
        run_dir / "universal-properties.json",
        json.dumps(final_universal, indent=2, ensure_ascii=False) + "\n",
    )
    atomic_write(
        run_dir / "verification-result.json",
        json.dumps(outcome.verification, indent=2, ensure_ascii=False) + "\n",
    )
    final_formal = outcome.verification.get("formal") or {}
    final_lint = outcome.verification.get("verilator_lint") or final_formal.get(
        "verilator_lint"
    ) or {}
    final_packet = (
        build_failure_packet(
            outcome.verification,
            outcome.rtl,
            trace_window_cycles=spec.verification["trace_window_cycles"],
        )
        if not outcome.verification.get("passed")
        else None
    )
    repair_summary = {
        "attempt_count": len(outcome.history),
        "counterexample_count": len(outcome.counterexamples),
        "repaired": outcome.repaired,
        "exhausted": outcome.exhausted,
        "formal_mode": spec.verification["formal_mode"],
        "formal_status": final_formal.get("status", "not_run"),
        "verilator_status": final_lint.get("status", "not_run"),
        "universal_mode": spec.verification["universal_mode"],
        "universal_status": final_universal["status"],
        "universal_proof": final_universal.get("universal_proof", False),
        "property_count": final_universal.get("property_count", 0),
        "requested_property_count": final_universal.get(
            "requested_property_count",
            len(spec.verification["universal_properties"]),
        ),
        "passed_property_count": final_universal.get("passed_property_count", 0),
        "failed_property_ids": final_universal.get("failed_property_ids", []),
        "universal_scope": (final_universal.get("scope") or {}).get("claim"),
        "universal_blocked_by": final_universal.get("blocked_by"),
        "covered_requirement_ids": (
            final_universal.get("requirement_coverage") or {}
        ).get("covered_requirement_ids", []),
        "uncovered_requirement_ids": (
            final_universal.get("requirement_coverage") or {}
        ).get("uncovered_requirement_ids", []),
        "universal_artifact": str(run_dir / "universal-properties.json"),
        "epoch": len(adaptive_corpus.get("epochs", [])),
        "suite_sha256": outcome.verification.get("suite_sha256"),
        "failure_class": outcome.verification.get("failure_class"),
        "requirement_id": (
            ((final_packet or {}).get("requirement_ids") or [None])[0]
        ),
        "first_failure_cycle": (final_packet or {}).get("first_failure_cycle"),
    }
    verification_summary = {
        key: outcome.verification.get(key)
        for key in (
            "passed",
            "failure_class",
            "total_checks",
            "mismatched",
            "first_failure_ordinal",
            "suite_sha256",
            "coverage",
        )
    }
    verification_summary["formal"] = outcome.verification.get("formal")
    verification_summary["universal_properties"] = final_universal
    verification_summary["artifact"] = str(run_dir / "verification-result.json")
    failed = not outcome.verification.get("passed")
    failure_class = (
        str(outcome.verification.get("failure_class") or "verification_failure")
        if failed
        else None
    )
    state = _update(
        state,
        phase="repair_exhausted" if failed else "regression",
        repair=repair_summary,
        verification=verification_summary,
        failure_class=failure_class,
        failure_gate="rtl_repair" if failed else None,
        failure_code=f"{failure_class}/repair_exhausted" if failed else None,
    )
    # Earlier gates intentionally short-circuit formal/universal execution.
    # Preserve that primary failure before constructing release-only evidence.
    if failed:
        assert final_packet is not None
        atomic_write(
            run_dir / "last-failure.json",
            json.dumps(final_packet, indent=2, ensure_ascii=False) + "\n",
        )
        raise RepairExhaustedError(
            "RTL repair exhausted without passing the frozen verification suite; "
            f"failure={final_packet['failure_class']}, "
            f"scenario={final_packet.get('scenario_id')}, "
            f"cycle={final_packet.get('first_failure_cycle')}, "
            f"artifact={run_dir / 'last-failure.json'}",
            failure_class=str(final_packet["failure_class"]),
        )
    return state, final_universal


def _published_file_record(root: Path, relative: str) -> dict[str, Any]:
    path = root / relative
    if path.is_symlink() or not path.is_file():
        raise RuntimeError(f"publication file identity is invalid: {relative}")
    try:
        resolved = path.resolve(strict=True)
        resolved.relative_to(root.resolve(strict=True))
        content = path.read_bytes()
    except (OSError, ValueError) as exc:
        raise RuntimeError(f"publication file is unreadable: {relative}: {exc}") from exc
    return {
        "path": relative,
        "size_bytes": len(content),
        "sha256": hashlib.sha256(content).hexdigest(),
    }


def _lane_input_manifest(
    records: list[dict[str, Any]], lane_input_paths: list[str]
) -> dict[str, Any]:
    by_path = {row["path"]: row for row in records}
    if set(lane_input_paths) - set(by_path):
        raise RuntimeError("publication manifest names an absent Lane input")
    files = [dict(by_path[path]) for path in sorted(lane_input_paths)]
    encoded = json.dumps(
        files, sort_keys=True, separators=(",", ":"), ensure_ascii=True
    ).encode("ascii")
    return {
        "schema_version": 2,
        "files": files,
        "bundle_sha256": hashlib.sha256(encoded).hexdigest(),
    }


def _write_publication_manifest(
    root: Path, spec: DesignSpec, state: dict[str, Any]
) -> dict[str, Any]:
    paths = [
        "config.yaml",
        f"src/{spec.name}.sv",
        "constraints.sdc",
        "pin_order.cfg",
        "spec.json",
        "reference_model.py",
        "rtl_closure-provenance.json",
    ]
    verification_root = root / "verification"
    if verification_root.is_symlink() or not verification_root.is_dir():
        raise RuntimeError("publication verification evidence directory is invalid")
    for candidate in sorted(verification_root.rglob("*")):
        if candidate.is_symlink():
            raise RuntimeError("publication verification evidence contains a symlink")
        if candidate.is_file():
            paths.append(candidate.relative_to(root).as_posix())
    lane_input_paths = [
        "config.yaml",
        f"src/{spec.name}.sv",
        "constraints.sdc",
        "pin_order.cfg",
    ]
    if spec.flow["mapped_equivalence_mode"] == "required":
        paths.extend((f"eqy/{spec.name}.eqy", f"eqy/{spec.name}-gate.tcl"))
        lane_input_paths.extend(
            (f"eqy/{spec.name}.eqy", f"eqy/{spec.name}-gate.tcl")
        )
    records = [_published_file_record(root, path) for path in sorted(paths)]
    lane_manifest = _lane_input_manifest(records, lane_input_paths)
    payload: dict[str, Any] = {
        "schema_version": 2,
        "run_id": state["run_id"],
        "design_name": spec.name,
        "files": records,
        "lane_input_paths": sorted(lane_input_paths),
        "lane_input_manifest": lane_manifest,
        "lane_input_bundle_sha256": lane_manifest["bundle_sha256"],
    }
    payload["publication_bundle_sha256"] = content_hash(payload)
    atomic_write(
        root / "publication-manifest.json",
        json.dumps(payload, indent=2, sort_keys=True) + "\n",
        0o444,
    )
    return payload


def _verify_publication(state: dict[str, Any]) -> dict[str, Any] | None:
    config_value = state.get("design_config")
    if not config_value:
        return None
    config_path = Path(str(config_value))
    design_dir = config_path.parent
    manifest_path = design_dir / "publication-manifest.json"
    if (
        config_path.is_symlink()
        or manifest_path.is_symlink()
        or not config_path.is_file()
        or not manifest_path.is_file()
    ):
        raise RuntimeError("published design or publication manifest identity is invalid")
    try:
        raw = manifest_path.read_bytes()
    except OSError as exc:
        raise RuntimeError(f"publication manifest is unreadable: {exc}") from exc
    if len(raw) > 1024 * 1024:
        raise RuntimeError("publication manifest exceeds the 1 MiB limit")
    try:
        manifest = json.loads(raw)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise RuntimeError("publication manifest is invalid JSON") from exc
    if not isinstance(manifest, dict):
        raise RuntimeError("publication manifest must be an object")
    payload = dict(manifest)
    bundle = payload.pop("publication_bundle_sha256", None)
    records = payload.get("files")
    lane_paths = payload.get("lane_input_paths")
    schema_version = payload.get("schema_version")
    if (
        schema_version != 2
        or payload.get("run_id") != state.get("run_id")
        or payload.get("design_name") != state.get("design_name")
        or not isinstance(records, list)
        or not records
        or not isinstance(lane_paths, list)
        or any(not isinstance(value, str) for value in lane_paths)
        or bundle != content_hash(payload)
    ):
        raise RuntimeError("publication manifest identity or bundle hash is invalid")
    expected_paths: set[str] = set()
    verified_records: list[dict[str, Any]] = []
    for row in records:
        if not isinstance(row, dict) or set(row) != {"path", "size_bytes", "sha256"}:
            raise RuntimeError("publication manifest contains an invalid file record")
        relative = row.get("path")
        if (
            not isinstance(relative, str)
            or not relative
            or relative in expected_paths
            or Path(relative).is_absolute()
            or ".." in Path(relative).parts
        ):
            raise RuntimeError("publication manifest contains an unsafe file path")
        expected_paths.add(relative)
        actual = _published_file_record(design_dir, relative)
        if actual != row:
            raise RuntimeError(f"published design changed after release: {relative}")
        verified_records.append(actual)
    verification_root = design_dir / "verification"
    if verification_root.is_symlink() or not verification_root.is_dir():
        raise RuntimeError("published verification evidence directory is invalid")
    protected_paths = {"reference_model.py"}
    for candidate in verification_root.rglob("*"):
        if candidate.is_symlink():
            raise RuntimeError("published verification evidence contains a symlink")
        if candidate.is_file():
            protected_paths.add(candidate.relative_to(design_dir).as_posix())
    if not protected_paths.issubset(expected_paths):
        raise RuntimeError(
            "publication manifest omits reference model or verification evidence"
        )
    lane_manifest = _lane_input_manifest(verified_records, lane_paths)
    if (
        payload.get("lane_input_manifest") != lane_manifest
        or payload.get("lane_input_bundle_sha256") != lane_manifest["bundle_sha256"]
        or state.get("publication_bundle_sha256") not in {None, bundle}
        or state.get("lane_input_bundle_sha256")
        not in {None, lane_manifest["bundle_sha256"]}
    ):
        raise RuntimeError("publication manifest Lane input binding is invalid")
    if state.get("lane_input_manifest") is not None and state.get(
        "lane_input_manifest"
    ) != lane_manifest:
        raise RuntimeError("persisted Lane input manifest changed after publication")
    expected_manifest_path = state.get("publication_manifest")
    if expected_manifest_path is not None and Path(str(expected_manifest_path)) != manifest_path:
        raise RuntimeError("publication manifest path changed after release")
    return manifest


def _validate_verification_image_identity(state: dict[str, Any]) -> None:
    identity = state.get("verify_image_identity")
    try:
        immutable = validate_verify_image_identity(identity)
    except RuntimeError as exc:
        raise RuntimeError(
            "publication denied: verification image identity is invalid"
        ) from exc
    if immutable != verify_image_ref():
        raise RuntimeError("publication denied: verification image identity is invalid")


def _validate_sandbox_image_identity(state: dict[str, Any]) -> None:
    identity = state.get("sandbox_image_identity")
    try:
        immutable = validate_sandbox_image_identity(identity)
    except RuntimeError as exc:
        raise RuntimeError("publication denied: sandbox image identity is invalid") from exc
    if immutable != sandbox_image_ref():
        raise RuntimeError("publication denied: sandbox image identity is invalid")


def _publish_design(
    spec: DesignSpec,
    state: dict[str, Any],
    rtl: str,
    python_model: str,
    result_data: dict[str, Any],
    created_staging: list[Path],
) -> tuple[str, Path]:
    verification = state.get("verification") or {}
    release_gate = state.get("release_gate") or {}
    if verification.get("passed") is not True:
        raise RuntimeError("publication denied: frozen RTL verification has not passed")
    if release_gate.get("passed") is not True:
        raise RuntimeError("publication denied: release validation gate has not passed")
    if release_gate.get("verify_image_identity") != state.get("verify_image_identity"):
        raise RuntimeError(
            "publication denied: release gate references another verification image"
        )
    if release_gate.get("sandbox_image_identity") != state.get("sandbox_image_identity"):
        raise RuntimeError(
            "publication denied: release gate references another sandbox image"
        )
    if release_gate.get("rtl_sha256") != content_hash(rtl):
        raise RuntimeError("publication denied: RTL changed after release validation")
    if release_gate.get("oracle_sha256") != content_hash(python_model):
        raise RuntimeError("publication denied: Python oracle changed after freezing")
    formal = release_gate.get("formal") or {}
    if formal.get("status") == "fail":
        raise RuntimeError("publication denied: bounded formal found a counterexample")
    if spec.verification["formal_mode"] == "required" and formal.get("status") != "pass":
        raise RuntimeError(
            "publication denied: required formal verification did not pass"
        )
    _validate_verification_image_identity(state)
    _validate_sandbox_image_identity(state)
    run_dir = Path(state.get("run_dir", ""))
    required_artifacts = _required_release_artifacts(spec)
    if tuple(release_gate.get("required_artifacts") or ()) != required_artifacts:
        raise RuntimeError(
            "publication denied: release evidence manifest is absent or incomplete"
        )
    artifact_hashes = release_gate.get("artifact_hashes") or {}
    if set(artifact_hashes) != set(required_artifacts):
        raise RuntimeError(
            "publication denied: release evidence hash set does not match the required manifest"
        )

    stamp = re.sub(r"[^0-9]", "", state["run_id"].split("-" + spec.name.lower(), 1)[0])
    suffix = f"{stamp}_{hashlib.sha256(state['run_id'].encode()).hexdigest()[:8]}"
    design_slug = f"{spec.design_slug_prefix}_{suffix}"
    DESIGN_ROOT.mkdir(parents=True, exist_ok=True, mode=0o700)
    os.chmod(DESIGN_ROOT, 0o700)
    target = DESIGN_ROOT / design_slug
    staging = DESIGN_ROOT / f".{design_slug}.staging-{os.getpid()}"
    if target.exists() or staging.exists():
        raise RuntimeError(f"generated design path already exists: {design_slug}")
    (staging / "src").mkdir(parents=True, mode=0o700)
    (staging / "runs").mkdir(mode=0o700)
    staged_verification = staging / "verification"
    staged_verification.mkdir()
    created_staging.append(staging)

    artifact_contents: dict[str, str] = {}
    for name in required_artifacts:
        expected_hash = artifact_hashes[name]
        path = run_dir / name
        try:
            content = path.read_text(encoding="utf-8")
            actual_hash = _artifact_content_hash(name, content)
        except (OSError, json.JSONDecodeError) as exc:
            raise RuntimeError(
                f"publication denied: release evidence is unreadable: {name}: {exc}"
            ) from exc
        if actual_hash != expected_hash:
            raise RuntimeError(
                f"publication denied: release evidence changed or is missing: {name}"
            )
        artifact_contents[name] = content
        atomic_write(staged_verification / name, content, 0o444)
    manifest = _parse_json_object(
        "frozen-manifest.json", artifact_contents["frozen-manifest.json"]
    )
    contract = _parse_json_object("contract.json", artifact_contents["contract.json"])
    plan = _parse_json_object("testplan.json", artifact_contents["testplan.json"])
    expected_trace = _parse_json_object(
        "expected-trace.json", artifact_contents["expected-trace.json"]
    )
    manifest_payload = dict(manifest)
    bundle_sha256 = manifest_payload.pop("bundle_sha256", None)
    if bundle_sha256 != content_hash(manifest_payload):
        raise RuntimeError("publication denied: frozen manifest bundle hash is invalid")
    frozen_checks = {
        "contract_sha256": content_hash(contract),
        "testplan_sha256": content_hash(plan),
        "oracle_sha256": content_hash(python_model),
        "expected_trace_sha256": content_hash(expected_trace),
    }
    if any(manifest.get(key) != value for key, value in frozen_checks.items()):
        raise RuntimeError("publication denied: frozen evidence disagrees with its manifest")
    frozen_intent = contract.get("intent_closure")
    if frozen_intent is not None and frozen_intent != spec.intent_closure:
        raise RuntimeError(
            "publication denied: frozen intent closure disagrees with the specification"
        )
    if spec.intent_closure["mode"] == "required" and (
        frozen_intent != spec.intent_closure
        or spec.intent_closure["closure_satisfied"] is not True
        or contract.get("requirements") != spec.verification["requirements"]
    ):
        raise RuntimeError(
            "publication denied: required user-confirmed intent closure is absent or invalid"
        )
    if release_gate.get("frozen_bundle_sha256") != bundle_sha256:
        raise RuntimeError("publication denied: release gate references another frozen bundle")
    verification_evidence = _parse_json_object(
        "verification-result.json", artifact_contents["verification-result.json"]
    )
    active_suite_sha256 = release_gate.get("active_suite_sha256")
    if (
        verification_evidence.get("passed") is not True
        or verification_evidence.get("verify_image") != verify_image_ref()
        or verification_evidence.get("rtl_sha256") != content_hash(rtl)
        or verification_evidence.get("suite_sha256") != active_suite_sha256
        or verification.get("suite_sha256") != active_suite_sha256
    ):
        raise RuntimeError("publication denied: final verification evidence is inconsistent")
    origin_bundle = _parse_json_object(
        "oracle-origins.json", artifact_contents["oracle-origins.json"]
    )
    validate_oracle_origin_bundle(origin_bundle)
    quorum = _parse_json_object(
        "oracle-quorum.json", artifact_contents["oracle-quorum.json"]
    )
    validate_oracle_quorum_evidence(
        quorum,
        plan,
        python_model,
        expected_trace,
        origin_bundle,
    )
    enforce_oracle_diversity(
        spec,
        origin_bundle["origins"],
        quorum_origin_ids=quorum["quorum_origin_ids"],
    )
    coverage_evidence = _parse_json_object(
        "coverage-refinement.json", artifact_contents["coverage-refinement.json"]
    )
    validate_oracle_line_coverage_evidence(
        spec,
        coverage_evidence,
        final_plan=plan,
        final_expected_trace=expected_trace,
        final_quorum=quorum,
    )
    enforce_oracle_line_coverage(
        spec,
        coverage_evidence,
        final_plan=plan,
        final_expected_trace=expected_trace,
        final_quorum=quorum,
    )
    adaptive = _parse_json_object(
        "adaptive-corpus.json", artifact_contents["adaptive-corpus.json"]
    )
    if (
        adaptive.get("schema_version") != 2
        or adaptive.get("base_suite_sha256") != suite_hash(plan, expected_trace)
    ):
        raise RuntimeError("publication denied: adaptive corpus has an invalid trust root")
    staged_adaptive = staged_verification / "adaptive"
    if adaptive.get("epochs"):
        for index in range(1, len(adaptive["epochs"]) + 1):
            source_epoch = run_dir / "adaptive" / f"epoch-{index:02d}"
            target_epoch = staged_adaptive / f"epoch-{index:02d}"
            target_epoch.mkdir(parents=True)
            for name in (
                "testplan.json",
                "expected-trace.json",
                "oracle-quorum.json",
                "epoch.json",
            ):
                try:
                    content = (source_epoch / name).read_text(encoding="utf-8")
                except OSError as exc:
                    raise RuntimeError(
                        "publication denied: adaptive epoch evidence is unreadable: "
                        f"epoch-{index:02d}/{name}: {exc}"
                    ) from exc
                atomic_write(target_epoch / name, content, 0o444)
    verified_active_suite = verify_epoch_chain(
        adaptive.get("epochs", []),
        plan,
        expected_trace,
        epoch_artifact_root=staged_adaptive,
        spec=spec,
        oracle_sha256=content_hash(python_model),
        selected_oracle=python_model,
        origin_bundle=origin_bundle,
    )
    expected_chain_head = (
        adaptive["epochs"][-1]["epoch_sha256"] if adaptive.get("epochs") else None
    )
    if (
        adaptive.get("active_suite_sha256") != active_suite_sha256
        or verified_active_suite != active_suite_sha256
        or adaptive.get("chain_head_sha256") != expected_chain_head
        or release_gate.get("adaptive_chain_head_sha256") != expected_chain_head
    ):
        raise RuntimeError("publication denied: adaptive corpus names another active suite")
    if adaptive.get("epochs"):
        final_epoch = staged_adaptive / f"epoch-{len(adaptive['epochs']):02d}"
        active_plan = _read_json_object(final_epoch / "testplan.json")
        active_expected_trace = _read_json_object(
            final_epoch / "expected-trace.json"
        )
    else:
        active_plan = plan
        active_expected_trace = expected_trace
    final_suite_quorum = _parse_json_object(
        "final-suite-quorum.json", artifact_contents["final-suite-quorum.json"]
    )
    validate_oracle_quorum_evidence(
        final_suite_quorum,
        active_plan,
        python_model,
        active_expected_trace,
        origin_bundle,
    )
    with tempfile.TemporaryDirectory(prefix="rtl-closure-release-quorum-") as replay:
        replayed_trace, replayed_quorum = revalidate_oracle_bundle_quorum(
            spec,
            active_plan,
            python_model,
            origin_bundle,
            Path(replay),
        )
    if (
        content_hash(replayed_trace) != content_hash(active_expected_trace)
        or content_hash(replayed_quorum) != content_hash(final_suite_quorum)
    ):
        raise RuntimeError(
            "publication denied: final active suite oracle quorum replay changed"
        )
    independent = _parse_json_object(
        "independent-verification.json",
        artifact_contents["independent-verification.json"],
    )
    independent_runs = independent.get("runs")
    independent_attempts = independent.get("attempts")
    independent_counterexamples = independent.get("counterexamples")
    independent_history = independent.get("repair_history")
    expected_independent_seeds = [
        spec.generation["seed"] + offset for offset in (1009, 2027)
    ]
    expected_independent_tests = spec.generation["independent_verify_tests"]
    expected_max_rounds = spec.verification["max_repair_rounds"]
    expected_repair_candidates = spec.verification["repair_candidates"]
    expected_independent_inputs = independent_input_widths(spec)
    expected_independent_outputs = {
        port.name: port.width for port in spec.ports if port.direction == "output"
    }
    expected_independent_stimuli = {
        seed: derive_independent_stimuli(
            spec, num_tests=expected_independent_tests, seed=seed
        )
        for seed in expected_independent_seeds
    }
    if (
        independent.get("schema_version") != 3
        or independent.get("status") != "pass"
        or independent.get("rtl_sha256") != content_hash(rtl)
        or independent.get("oracle_sha256") != content_hash(python_model)
        or independent.get("sandbox_image") != sandbox_image_ref()
        or independent.get("active_suite_sha256") != suite_hash(
            active_plan, active_expected_trace
        )
        or independent.get("max_repair_rounds") != expected_max_rounds
        or len(set(expected_independent_seeds)) != 2
        or not isinstance(independent_runs, list)
        or len(independent_runs) != 2
        or not isinstance(independent_attempts, list)
        or not independent_attempts
        or len(independent_attempts)
        > 1 + expected_max_rounds * expected_repair_candidates
        or not isinstance(independent_counterexamples, list)
        or not isinstance(independent_history, list)
    ):
        raise RuntimeError("publication denied: independent verification evidence is invalid")
    for run_index, (row, seed) in enumerate(
        zip(independent_runs, expected_independent_seeds), 1
    ):
        _validate_independent_run(
            row,
            expected_run=run_index,
            expected_seed=seed,
            expected_num_tests=expected_independent_tests,
            expected_stimuli=expected_independent_stimuli[seed],
            expected_inputs=expected_independent_inputs,
            expected_outputs=expected_independent_outputs,
            require_pass=True,
        )
    accepted_independent_attempts = [
        row
        for row in independent_attempts
        if isinstance(row, dict)
        and row.get("passed") is True
        and row.get("rtl_sha256") == content_hash(rtl)
        and row.get("runs") == independent_runs
    ]
    if len(accepted_independent_attempts) != 1:
        raise RuntimeError("publication denied: independent verification evidence is invalid")
    seen_attempts: set[tuple[int, int | None]] = set()
    failed_run_count = 0
    for attempt in independent_attempts:
        if not isinstance(attempt, dict) or set(attempt) != {
            "round",
            "candidate",
            "rtl_sha256",
            "passed",
            "runs",
        }:
            raise RuntimeError("publication denied: independent verification evidence is invalid")
        round_index = attempt.get("round")
        candidate_index = attempt.get("candidate")
        if (
            isinstance(round_index, bool)
            or not isinstance(round_index, int)
            or not 0 <= round_index <= expected_max_rounds
            or (round_index == 0 and candidate_index is not None)
            or (
                round_index > 0
                and (
                    isinstance(candidate_index, bool)
                    or not isinstance(candidate_index, int)
                    or not 1 <= candidate_index <= expected_repair_candidates
                )
            )
            or not isinstance(attempt.get("rtl_sha256"), str)
            or re.fullmatch(r"[0-9a-f]{64}", attempt["rtl_sha256"]) is None
            or not isinstance(attempt.get("passed"), bool)
            or not isinstance(attempt.get("runs"), list)
            or len(attempt["runs"]) != 2
        ):
            raise RuntimeError("publication denied: independent attempt is invalid")
        attempt_identity = (round_index, candidate_index)
        if attempt_identity in seen_attempts:
            raise RuntimeError("publication denied: independent attempt is duplicated")
        seen_attempts.add(attempt_identity)
        derived_attempt_passed = True
        for run_index, (row, seed) in enumerate(
            zip(attempt["runs"], expected_independent_seeds), 1
        ):
            _validate_independent_run(
                row,
                expected_run=run_index,
                expected_seed=seed,
                expected_num_tests=expected_independent_tests,
                expected_stimuli=expected_independent_stimuli[seed],
                expected_inputs=expected_independent_inputs,
                expected_outputs=expected_independent_outputs,
                require_pass=False,
            )
            if row["passed"] is not True:
                derived_attempt_passed = False
            if row["passed"] is not False:
                continue
            failed_run_count += 1
            matching = [
                evidence
                for evidence in independent_counterexamples
                if isinstance(evidence, dict)
                and set(evidence)
                == {
                    "source",
                    "round",
                    "candidate",
                    "run",
                    "seed",
                    "num_tests",
                    "rtl_sha256",
                    "verification",
                    "fingerprint",
                }
                and evidence.get("source") == "independent_cross_verification"
                and evidence.get("round") == attempt.get("round")
                and evidence.get("candidate") == attempt.get("candidate")
                and evidence.get("run") == row.get("run")
                and evidence.get("seed") == row.get("seed")
                and evidence.get("num_tests") == expected_independent_tests
                and evidence.get("rtl_sha256") == attempt.get("rtl_sha256")
                and evidence.get("verification") == row.get("verification")
                and evidence.get("fingerprint")
                == _independent_failure_fingerprint(
                    seed=seed, verification=row["verification"]
                )
            ]
            if len(matching) != 1:
                raise RuntimeError(
                    "publication denied: independent counterexample evidence is incomplete"
                )
        if attempt["passed"] is not derived_attempt_passed:
            raise RuntimeError(
                "publication denied: independent attempt disposition is invalid"
            )
    if (0, None) not in seen_attempts or len(independent_counterexamples) != failed_run_count:
        raise RuntimeError(
            "publication denied: independent counterexample set is not exhaustive"
        )
    synthesis_equivalence = _parse_json_object(
        "synthesis-equivalence.json",
        artifact_contents["synthesis-equivalence.json"],
    )
    equivalence_log = artifact_contents["synthesis-equivalence.log"]
    equivalence_cells = synthesis_equivalence.get("equivalence_cells")
    proven_cells = synthesis_equivalence.get("proven_cells")
    equivalence_base = synthesis_equivalence.get("reset_base_case") or {}
    equivalence_semantics = synthesis_equivalence.get("comparison_semantics") or {}
    expected_output_ports = sorted(
        port.name for port in spec.ports if port.direction == "output"
    )
    expected_output_bits = sum(
        port.width for port in spec.ports if port.direction == "output"
    )
    supporting_internal_names = equivalence_semantics.get(
        "supporting_internal_match_names"
    )
    expected_equivalence_scope = (
        "post_reset_observable_outputs_rtl_vs_generic_synthesized_netlist"
        if spec.reset is not None
        else "observable_outputs_rtl_vs_generic_synthesized_netlist"
    )
    reset_base_valid = (
        equivalence_base.get("status") == "pass"
        and equivalence_base.get("asserted_samples") == 2
        and equivalence_base.get("post_reset_samples") == 8
        and equivalence_base.get("sampling") == "rising_clock_edge"
        and bool(
            re.search(
                r"SAT proof finished\s*-\s*no model found:\s*SUCCESS",
                equivalence_log,
                re.I,
            )
        )
        if spec.reset is not None
        else equivalence_base.get("status") == "not_requested"
        and equivalence_base.get("asserted_samples") == 0
        and equivalence_base.get("post_reset_samples") == 0
    )
    if (
        synthesis_equivalence.get("schema_version") != 3
        or synthesis_equivalence.get("status") != "pass"
        or synthesis_equivalence.get("provider")
        != "yosys_generic_synthesis_equivalence"
        or synthesis_equivalence.get("sandbox_image") != sandbox_image_ref()
        or synthesis_equivalence.get("rtl_sha256") != content_hash(rtl)
        or synthesis_equivalence.get("top") != spec.name
        or synthesis_equivalence.get("clock_port") != spec.clock_port
        or synthesis_equivalence.get("reset") != spec.reset
        or synthesis_equivalence.get("scope") != expected_equivalence_scope
        or synthesis_equivalence.get("sequential_induction_depth") != 8
        or equivalence_semantics.get("points") != "top_level_output_bits_only"
        or equivalence_semantics.get("observable_output_bits")
        != expected_output_bits
        or equivalence_semantics.get("output_ports") != expected_output_ports
        or isinstance(supporting_internal_names, bool)
        or not isinstance(supporting_internal_names, int)
        or supporting_internal_names < 0
        or equivalence_semantics.get("internal_state")
        != "supporting_inductive_relation_not_a_release_obligation"
        or equivalence_semantics.get("reachable_state_base_proven")
        is not (spec.reset is not None)
        or not reset_base_valid
        or not re.search(r"Equivalence successfully proven", equivalence_log, re.I)
        or isinstance(equivalence_cells, bool)
        or not isinstance(equivalence_cells, int)
        or equivalence_cells <= 0
        or proven_cells != equivalence_cells
        or content_hash(release_gate.get("synthesis_equivalence"))
        != content_hash(synthesis_equivalence)
    ):
        raise RuntimeError(
            "publication denied: synthesis equivalence evidence is invalid"
        )
    architecture_audit = _parse_json_object(
        "architecture-audit.json",
        artifact_contents["architecture-audit.json"],
    )
    expected_architecture_status = "pass" if spec.architecture else "not_requested"
    expected_architecture_checks = {
        *(f"required_module:{name}" for name in spec.architecture.get("required_modules", [])),
        *(
            f"forbidden_operator:{name}"
            for name in spec.architecture.get("forbidden_operators", [])
        ),
        *(
            name
            for name in (
                "min_sequential_bits",
                "max_sequential_bits",
                "max_synth_cells",
            )
            if name in spec.architecture
        ),
    }
    architecture_checks = architecture_audit.get("checks")
    architecture_checks_valid = isinstance(architecture_checks, list) and (
        not spec.architecture
        and architecture_checks == []
        or bool(spec.architecture)
        and len(architecture_checks) == len(expected_architecture_checks)
        and {
            row.get("id")
            for row in architecture_checks
            if isinstance(row, dict)
        }
        == expected_architecture_checks
        and all(
            isinstance(row, dict) and row.get("status") == "pass"
            for row in architecture_checks
        )
    )
    if (
        architecture_audit.get("status") != expected_architecture_status
        or architecture_audit.get("provider")
        != "yosys_structural_architecture_audit"
        or architecture_audit.get("sandbox_image") != sandbox_image_ref()
        or architecture_audit.get("top") != spec.name
        or architecture_audit.get("rtl_sha256") != content_hash(rtl)
        or architecture_audit.get("constraints_sha256")
        != content_hash(spec.architecture)
        or architecture_audit.get("constraints") != spec.architecture
        or not architecture_checks_valid
        or content_hash(release_gate.get("architecture_audit"))
        != content_hash(architecture_audit)
    ):
        raise RuntimeError("publication denied: architecture audit evidence is invalid")
    universal_properties = _parse_json_object(
        "universal-properties.json",
        artifact_contents["universal-properties.json"],
    )
    universal_mode = spec.verification["universal_mode"]
    universal_status = universal_properties.get("status")
    requested_properties = spec.verification["universal_properties"]
    property_count = len(requested_properties)
    universal_status_valid = (
        universal_status == "not_requested"
        if universal_mode == "off"
        else universal_status == "pass"
        if universal_mode == "required"
        else universal_status in (
            {"pass", "unknown", "error"} if property_count else {"not_requested"}
        )
    )
    universal_rows = universal_properties.get("properties")
    universal_rows_valid = isinstance(universal_rows, list) and len(
        universal_rows
    ) == property_count
    if universal_rows_valid:
        for requested, observed in zip(requested_properties, universal_rows):
            if (
                not isinstance(observed, dict)
                or observed.get("id") != requested["id"]
                or observed.get("kind") != requested["kind"]
                or observed.get("requirement_ids") != requested["requirement_ids"]
                or observed.get("rtl_sha256") != content_hash(rtl)
                or observed.get("property_sha256") != content_hash(requested)
                or observed.get("status") not in {"pass", "fail", "unknown", "error"}
                or observed.get("universal_proof")
                is not (observed.get("status") == "pass")
            ):
                universal_rows_valid = False
                break
    by_universal_status = {
        status: [
            row["id"]
            for row in universal_rows or []
            if isinstance(row, dict) and row.get("status") == status
        ]
        for status in ("pass", "fail", "unknown", "error")
    }
    expected_universal_status = (
        "not_requested"
        if property_count == 0
        else "fail"
        if by_universal_status["fail"]
        else "error"
        if by_universal_status["error"]
        else "unknown"
        if by_universal_status["unknown"]
        else "pass"
    )
    expected_universal_scope = universal_proof_scope(
        induction_depth=spec.verification["universal_induction_depth"],
        reset=spec.reset,
    )
    expected_requirement_coverage = universal_requirement_coverage(
        universal_rows if isinstance(universal_rows, list) else [],
        [row["id"] for row in spec.verification["requirements"]],
    )
    if (
        not universal_status_valid
        or universal_status != expected_universal_status
        or universal_properties.get("schema_version") != 2
        or universal_properties.get("provider")
        != "yosys_sat_universal_properties"
        or universal_properties.get("verify_image") != verify_image_ref()
        or universal_properties.get("authoritative") is not True
        or universal_properties.get("authoritative_for")
        != "listed_port_level_safety_properties_only"
        or universal_properties.get("complete_behavioral_proof") is not False
        or universal_properties.get("complete_requirement_proof") is not False
        or universal_properties.get("top") != spec.name
        or universal_properties.get("clock_port") != spec.clock_port
        or universal_properties.get("scope") != expected_universal_scope
        or universal_properties.get("requirement_coverage")
        != expected_requirement_coverage
        or universal_properties.get("rtl_sha256") != content_hash(rtl)
        or universal_properties.get("properties_sha256")
        != content_hash(requested_properties)
        or universal_properties.get("property_count")
        != property_count
        or not universal_rows_valid
        or universal_properties.get("universal_proof")
        is not (universal_status == "pass")
        or universal_properties.get("passed_property_count")
        != len(by_universal_status["pass"])
        or universal_properties.get("passed_property_ids")
        != by_universal_status["pass"]
        or universal_properties.get("failed_property_ids")
        != by_universal_status["fail"]
        or universal_properties.get("unknown_property_ids")
        != by_universal_status["unknown"]
        or universal_properties.get("error_property_ids")
        != by_universal_status["error"]
        or content_hash(verification_evidence.get("universal_properties"))
        != content_hash(universal_properties)
        or content_hash(release_gate.get("universal_properties"))
        != content_hash(universal_properties)
    ):
        raise RuntimeError(
            "publication denied: universal property evidence is invalid"
        )
    universal_execution_evidence = _parse_json_object(
        "universal-execution-evidence.json",
        artifact_contents["universal-execution-evidence.json"],
    )
    _validate_universal_execution_evidence(
        spec,
        rtl,
        universal_properties,
        universal_execution_evidence,
    )
    if spec.verification["formal_mode"] != "off":
        formal_evidence = _parse_json_object(
            "formal-report.json", artifact_contents["formal-report.json"]
        )
        formal_properties = json.loads(artifact_contents["formal-properties.json"])
        formal_execution_evidence = _parse_json_object(
            "formal-execution-evidence.json",
            artifact_contents["formal-execution-evidence.json"],
        )
        _validate_formal_release_evidence(
            spec,
            rtl,
            active_plan,
            active_expected_trace,
            formal_properties,
            formal_evidence,
            verification_evidence.get("formal"),
            formal,
            formal_execution_evidence,
        )
    if spec.verification["verilator_lint"]:
        lint_evidence = _parse_json_object(
            "verilator-lint.json", artifact_contents["verilator-lint.json"]
        )
        if lint_evidence.get("rtl_sha256") != content_hash(rtl):
            raise RuntimeError(
                "publication denied: Verilator lint evidence references another RTL"
            )
        quality_gate = lint_evidence.get("quality_gate")
        if (
            lint_evidence.get("status") != "pass"
            or not isinstance(quality_gate, dict)
            or quality_gate.get("status") != "pass"
            or quality_gate.get("blocking_warning_codes") != []
        ):
            raise RuntimeError("publication denied: Verilator lint did not pass")
    provenance = {
        "generator": "rtl-closure",
        "run_id": state["run_id"],
        "model": state["model"],
        "matched": result_data["matched"],
        "best_match_rate": result_data["best_match_rate"],
        "closed_loop_passed": bool(
            (state.get("verification") or {}).get("passed")
        ),
        "contract": state.get("contract"),
        "oracle": state.get("oracle"),
        "oracle_source_configuration": state.get("oracle_source_configuration"),
        "coverage": state.get("coverage"),
        "repair": state.get("repair"),
        "independent_verification": state.get("independent_verification"),
        "release_gate": release_gate,
        "created_at": now_iso(),
    }
    atomic_write(staging / "src" / f"{spec.name}.sv", rtl + "\n", 0o444)
    atomic_write(staging / "reference_model.py", python_model, 0o400)
    atomic_write(
        staging / "spec.json",
        json.dumps(spec.raw, indent=2, ensure_ascii=False) + "\n",
        0o444,
    )
    atomic_write(staging / "config.yaml", render_config(spec), 0o444)
    if spec.flow["mapped_equivalence_mode"] == "required":
        atomic_write(
            staging / "eqy" / f"{spec.name}.eqy",
            render_eqy_script(spec),
            0o444,
        )
        atomic_write(
            staging / "eqy" / f"{spec.name}-gate.tcl",
            render_eqy_gate_tcl(),
            0o444,
        )
    atomic_write(staging / "constraints.sdc", render_sdc(spec), 0o444)
    atomic_write(staging / "pin_order.cfg", render_pin_order(spec), 0o444)
    atomic_write(
        staging / "rtl_closure-provenance.json",
        json.dumps(provenance, indent=2) + "\n",
        0o444,
    )
    publication_manifest = _write_publication_manifest(staging, spec, state)
    for name in required_artifacts:
        if _artifact_hash(staged_verification / name) != artifact_hashes[name]:
            raise RuntimeError(
                f"publication denied: staged release evidence changed: {name}"
            )
    if adaptive.get("epochs"):
        staged_active_suite = verify_epoch_chain(
            adaptive["epochs"],
            plan,
            expected_trace,
            epoch_artifact_root=staged_adaptive,
            spec=spec,
            oracle_sha256=content_hash(python_model),
            selected_oracle=python_model,
            origin_bundle=origin_bundle,
        )
        if staged_active_suite != active_suite_sha256:
            raise RuntimeError(
                "publication denied: staged adaptive evidence changed during copy"
            )
    for directory in sorted(
        (
            path
            for path in staging.rglob("*")
            if path.is_dir() and path != staging / "runs"
        ),
        key=lambda path: len(path.parts),
        reverse=True,
    ):
        os.chmod(directory, 0o555)
    os.chmod(staging / "runs", 0o700)
    os.chmod(staging, 0o555)
    os.replace(staging, target)
    created_staging.append(target)
    if _verify_publication(
        {
            **state,
            "design_name": spec.name,
            "design_config": str(target / "config.yaml"),
            "publication_manifest": str(target / "publication-manifest.json"),
            "publication_bundle_sha256": publication_manifest[
                "publication_bundle_sha256"
            ],
            "lane_input_bundle_sha256": publication_manifest[
                "lane_input_bundle_sha256"
            ],
        }
    ) is None:
        raise RuntimeError("publication manifest verification unexpectedly failed")
    created_staging.remove(target)
    return design_slug, target / "config.yaml"


def publish_design(
    spec: DesignSpec,
    state: dict[str, Any],
    rtl: str,
    python_model: str,
    result_data: dict[str, Any],
) -> tuple[str, Path]:
    created_staging: list[Path] = []
    try:
        return _publish_design(
            spec,
            state,
            rtl,
            python_model,
            result_data,
            created_staging,
        )
    except BaseException:
        for staging in created_staging:
            if staging.is_dir() and not staging.is_symlink():
                try:
                    os.chmod(staging, 0o700)
                    for directory in staging.rglob("*"):
                        if directory.is_dir() and not directory.is_symlink():
                            os.chmod(directory, 0o700)
                except OSError:
                    pass
                shutil.rmtree(staging, ignore_errors=True)
        raise


def _validate_lane_state_identity(
    value: Any,
    *,
    expected_run_id: str | None,
    expected_workflow_id: str | None,
    expected_attempt_id: str | None,
    expected_idempotency_key: str | None = None,
    expected_input_manifest: dict[str, Any] | None = None,
    expected_input_bundle_sha256: str | None = None,
    expected_integrity_audit_id: str | None = None,
) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise ValueError("lane-agent returned a non-object response")
    if value.get("schema_version") != 6:
        raise ValueError("lane-agent response schema is unsupported; expected 6")
    run_id = value.get("run_id")
    if (
        not isinstance(run_id, str)
        or not RUN_ID_RE.fullmatch(run_id)
        or run_id in {".", ".."}
    ):
        raise ValueError("lane-agent response has an invalid run_id")
    if expected_run_id is not None and run_id != expected_run_id:
        raise ValueError("lane-agent response belongs to another run")
    for field, expected in (
        ("workflow_id", expected_workflow_id),
        ("attempt_id", expected_attempt_id),
        ("idempotency_key", expected_idempotency_key),
    ):
        if expected is not None and value.get(field) != expected:
            raise ValueError(f"lane-agent response has a mismatched {field}")
    if value.get("status") not in {"running", *TERMINAL_STATUSES}:
        raise ValueError("lane-agent response has an invalid status")
    if expected_input_manifest is not None and value.get(
        "input_manifest"
    ) != expected_input_manifest:
        raise LaneInputIdentityError(
            "lane-agent response consumed a different immutable input manifest"
        )
    if expected_input_bundle_sha256 is not None and value.get(
        "input_bundle_sha256"
    ) != expected_input_bundle_sha256:
        raise LaneInputIdentityError(
            "lane-agent response consumed a different immutable input bundle"
        )
    release_integrity = value.get("release_integrity")
    delivery_attestation = value.get("delivery_attestation")
    if release_integrity is not None or delivery_attestation is not None:
        if (
            not isinstance(release_integrity, dict)
            or release_integrity.get("schema_version") != 1
        ):
            raise ValueError("lane-agent delivery integrity evidence is malformed")
        integrity_status = release_integrity.get("status")
        integrity_errors = release_integrity.get("errors")
        if (
            not isinstance(release_integrity.get("audit_id"), str)
            or not re.fullmatch(
                r"[A-Za-z0-9][A-Za-z0-9_.:/-]{0,254}",
                str(release_integrity["audit_id"]),
            )
            or not isinstance(integrity_errors, list)
            or len(integrity_errors) > 16
            or any(
                not isinstance(error, str) or not error.strip()
                for error in integrity_errors
            )
            or integrity_status not in {"verified", "unknown", "degraded"}
            or (integrity_status == "verified" and integrity_errors != [])
            or (
                integrity_status in {"unknown", "degraded"}
                and not integrity_errors
            )
        ):
            raise ValueError("lane-agent delivery attestation binding is invalid")
        unattested_degraded_delivery = (
            value.get("status") == "incomplete"
            and integrity_status == "degraded"
            and bool(integrity_errors)
            and release_integrity.get("attestation_sha256") is None
        )
        if not unattested_degraded_delivery:
            if (
                not isinstance(delivery_attestation, dict)
                or delivery_attestation.get("schema_version") != 1
            ):
                raise ValueError(
                    "lane-agent delivery integrity evidence is malformed"
                )
            attestation_payload = dict(delivery_attestation)
            attestation_sha256 = attestation_payload.pop(
                "attestation_sha256", None
            )
            if (
                not isinstance(attestation_sha256, str)
                or not re.fullmatch(r"[0-9a-f]{64}", attestation_sha256)
                or attestation_sha256 != content_hash(attestation_payload)
                or release_integrity.get("attestation_sha256")
                != attestation_sha256
            ):
                raise ValueError(
                    "lane-agent delivery attestation binding is invalid"
                )
    if expected_integrity_audit_id is not None and (
        not isinstance(release_integrity, dict)
        or release_integrity.get("audit_id") != expected_integrity_audit_id
    ):
        raise ValueError("lane-agent returned a stale release integrity audit")
    if value.get("status") == "completed":
        if (
            not isinstance(release_integrity, dict)
            or release_integrity.get("status") != "verified"
            or release_integrity.get("errors") != []
        ):
            raise ValueError(
                "completed Lane delivery has no fresh verified release integrity audit"
            )
    return value


def _active_lane_launch_key(state: dict[str, Any]) -> str | None:
    active_attempt_id = state.get("active_lane_attempt_id")
    if not isinstance(active_attempt_id, str) or not active_attempt_id:
        return None
    for attempt in reversed(state.get("lane_attempts") or []):
        if not isinstance(attempt, dict):
            continue
        if attempt.get("attempt_id") == active_attempt_id:
            launch_key = attempt.get("launch_key")
            return launch_key if isinstance(launch_key, str) and launch_key else None
    return None


def _start_lane_unlocked(spec: DesignSpec, state: dict[str, Any]) -> dict[str, Any]:
    if state.get("status") in TERMINAL_STATUSES:
        raise RuntimeError(
            "terminal RTL-Closure state must be transitioned by retry-lane first"
        )
    publication = (
        _verify_publication(state)
        if state.get("publication_bundle_sha256") is not None
        else None
    )
    attempts = [
        dict(row) for row in state.get("lane_attempts", []) if isinstance(row, dict)
    ]
    if state.get("lane_run_id") and not any(
        row.get("lane_run_id") == state["lane_run_id"] for row in attempts
    ):
        raise RuntimeError("persisted Lane run has no matching attempt lineage")
    retry_unknown_launch = bool(
        attempts
        and attempts[-1].get("lane_run_id") is None
        and attempts[-1].get("status") in {"launching", "launch_unknown"}
        and attempts[-1].get("launch_key")
    )
    if retry_unknown_launch:
        attempt = attempts[-1]
        attempt_id = str(attempt["attempt_id"])
        launch_key = str(attempt["launch_key"])
        attempt.update(
            {
                "status": "launching",
                "completed_at": None,
                "error": None,
                "launch_retries": int(attempt.get("launch_retries", 0)) + 1,
            }
        )
    else:
        attempt_id = f"lane-attempt-{len(attempts) + 1:03d}"
        launch_key = content_hash(
            {
                "rtl_closure_run_id": state.get("run_id"),
                "attempt_id": attempt_id,
                "design_config": state.get("design_config"),
                "publication_bundle_sha256": (
                    publication["publication_bundle_sha256"]
                    if publication is not None
                    else None
                ),
                "lane_input_bundle_sha256": (
                    publication["lane_input_bundle_sha256"]
                    if publication is not None
                    else None
                ),
                "pdk": spec.pdk,
                "flow": spec.flow["name"],
                "to": spec.flow["to"],
            }
        )
        attempt = {
            "attempt_id": attempt_id,
            "lane_run_id": None,
            "status": "launching",
            "started_at": now_iso(),
            "completed_at": None,
            "supersedes": attempts[-1].get("attempt_id") if attempts else None,
            "launch_key": launch_key,
            "launch_retries": 0,
            "error": None,
        }
        attempts.append(attempt)
    _update(
        state,
        phase="starting_lane",
        status="running",
        lane_attempts=attempts,
        active_lane_attempt_id=attempt_id,
    )
    command = [
        "lane-agent",
        "start",
        "--config",
        state["design_config"],
        "--pdk",
        spec.pdk,
        "--flow",
        spec.flow["name"],
        "--workflow-id",
        str((state.get("origin") or {}).get("workflow_id") or state.get("run_id")),
        "--attempt-id",
        attempt_id,
        "--idempotency-key",
        launch_key,
        "--origin",
        "rtl-closure",
        "--json",
    ]
    if spec.flow["to"]:
        command.extend(["--to", spec.flow["to"]])
    if spec.flow["jobs"]:
        command.extend(["--jobs", str(spec.flow["jobs"])])
    try:
        started = subprocess.run(
            command,
            cwd=Path(state["design_config"]).parent,
            capture_output=True,
            text=True,
            timeout=60,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        attempts[-1].update(
            {"status": "launch_unknown", "completed_at": now_iso(), "error": str(exc)}
        )
        _update(state, lane_attempts=attempts, announce=False)
        raise LaneLaunchError(f"could not start LibreLane: {exc}") from exc
    if started.returncode != 0:
        message = (started.stderr or started.stdout).strip()
        attempts[-1].update(
            {"status": "launch_unknown", "completed_at": now_iso(), "error": message}
        )
        _update(state, lane_attempts=attempts, announce=False)
        raise LaneLaunchError(f"could not start LibreLane: {message}")
    expected_workflow_id = str(
        (state.get("origin") or {}).get("workflow_id") or state.get("run_id")
    )
    try:
        lane_state = _validate_lane_state_identity(
            json.loads(started.stdout),
            expected_run_id=None,
            expected_workflow_id=expected_workflow_id,
            expected_attempt_id=attempt_id,
            expected_idempotency_key=launch_key,
            expected_input_manifest=(
                publication["lane_input_manifest"]
                if publication is not None
                else None
            ),
            expected_input_bundle_sha256=(
                publication["lane_input_bundle_sha256"]
                if publication is not None
                else None
            ),
        )
    except (json.JSONDecodeError, ValueError) as exc:
        attempts[-1].update(
            {
                "status": "launch_unknown",
                "completed_at": now_iso(),
                "error": f"lane-agent returned an invalid start response: {exc}",
            }
        )
        _update(state, lane_attempts=attempts, announce=False)
        raise LaneLaunchError(
            f"lane-agent returned an invalid start response: {exc}"
        ) from exc
    attempts[-1].update(
        {"lane_run_id": lane_state["run_id"], "status": lane_state.get("status", "running")}
    )
    return _update(
        state,
        phase="lane_running",
        status="running",
        lane_run_id=lane_state["run_id"],
        lane_state=lane_state,
        lane_attempts=attempts,
        active_lane_attempt_id=attempt_id,
        error=None,
        failure=None,
        lane_status_error=None,
        failure_class=None,
        failure_gate=None,
        failure_code=None,
        stop_requested=False,
    )


def _lane_is_active_or_unknown(state: dict[str, Any]) -> bool:
    if not state.get("lane_run_id"):
        return False
    child_status = (state.get("lane_state") or {}).get("status")
    return child_status not in {"completed", "failed", "incomplete", "stopped"}


def start_lane(spec: DesignSpec, state: dict[str, Any]) -> dict[str, Any]:
    state_file = Path(state["state_file"])
    with _operation_lock(state_file):
        current = load_state(state["run_id"])
        if _lane_is_active_or_unknown(current):
            raise RuntimeError("a child LibreLane flow is already active or its state is unknown")
        state.clear()
        state.update(current)
        return _start_lane_unlocked(spec, state)


def worker(run_id: str, launch_generation: int, launch_token: str) -> int:
    global _ACTIVE_WORKER_FENCE
    token_sha256 = _launch_token_sha256(launch_token)
    _ACTIVE_WORKER_FENCE = (run_id, launch_generation, token_sha256)
    state: dict[str, Any] | None = None
    try:
        initial = load_state(run_id)
        with _operation_lock(Path(initial["state_file"])):
            state = load_state(run_id)
            identity = _read_process_identity(os.getpid())
            if identity is None:
                raise RuntimeError("pipeline worker identity could not be attested")
            state = _register_pipeline_worker_unlocked(
                state,
                launch_generation=launch_generation,
                launch_token_sha256=token_sha256,
                pid=os.getpid(),
                identity=identity,
            )
        spec = _load_frozen_spec(state)
        sandbox_image_identity = resolve_sandbox_image()
        verify_image_identity = resolve_verify_image()
        bind_sandbox_image(sandbox_image_identity)
        bind_verify_image(verify_image_identity)
        state = _update(
            state,
            sandbox_image_identity=sandbox_image_identity,
            verify_image_identity=verify_image_identity,
            announce=False,
            record_event=False,
        )
        cross_verify = make_contract_verifier(
            clock_port=spec.clock_port,
            reset=spec.reset,
        )
        state = _update(state, phase="model_initializing")
        model_progress_lock = threading.Lock()
        active_model_requests: dict[int, dict[str, Any]] = {}

        def record_model_progress(event: dict[str, Any]) -> None:
            nonlocal state
            with model_progress_lock:
                summary = _merge_model_progress(active_model_requests, event)
                state = _update(
                    state,
                    announce=False,
                    model_request=summary,
                )

        v_backend, p_backend, public_model = make_backends(
            spec.generation,
            progress=record_model_progress,
            request_scope=str(state["run_id"]),
        )
        state = _update(state, model=public_model, record_event=False)
        oracle_backends = _distinct_oracle_backends(v_backend, p_backend)
        state = _update(
            state,
            oracle_source_configuration={
                "sources": [backend_provenance(item) for item in oracle_backends],
                "distinct_source_count": len(oracle_backends),
            },
            announce=False,
            record_event=False,
        )

        run_dir = Path(state["run_dir"])
        state = _update(state, phase="contracting")
        contract, testplan, contract_metadata = plan_contract(spec, v_backend)
        contract_path = run_dir / "contract.json"
        plan_draft_path = run_dir / "testplan-draft.json"
        plan_path = run_dir / "testplan.json"
        atomic_write(
            contract_path,
            json.dumps(contract, indent=2, ensure_ascii=False) + "\n",
            0o400,
        )
        atomic_write(
            plan_draft_path,
            json.dumps(testplan, indent=2, ensure_ascii=False) + "\n",
            0o600,
        )
        state = _update(
            state,
            phase="contract_frozen",
            contract=contract_metadata,
            artifacts={
                "contract": str(contract_path),
                "testplan_draft": str(plan_draft_path),
            },
        )

        state = _update(state, phase="oracle_generating")
        python_model, expected_trace, oracle_metadata = generate_frozen_oracle(
            spec,
            oracle_backends,
            contract,
            testplan,
            run_dir / "oracle-candidates",
        )
        origin_bundle = build_oracle_origin_bundle(
            oracle_metadata, run_dir / "oracle-candidates"
        )
        origin_bundle_path = run_dir / "oracle-origins.json"
        atomic_write(
            origin_bundle_path,
            json.dumps(origin_bundle, indent=2, ensure_ascii=False) + "\n",
            0o400,
        )
        coverage_dir = run_dir / "coverage-refinement"

        def coverage_progress(phase: str, summary: dict[str, Any]) -> None:
            nonlocal state
            state = _update(state, phase=phase, coverage=summary)

        testplan, expected_trace, coverage_report = refine_oracle_coverage(
            spec,
            v_backend,
            contract,
            testplan,
            python_model,
            expected_trace,
            coverage_dir,
            progress=coverage_progress,
        )
        coverage_report_path = run_dir / "coverage-refinement.json"
        atomic_write(
            coverage_report_path,
            json.dumps(coverage_report, indent=2, ensure_ascii=False) + "\n",
            0o400,
        )
        state = _update(
            state,
            phase="coverage_gate_check",
            coverage={
                "status": coverage_report["status"],
                "mode": coverage_report["mode"],
                "metric": coverage_report["metric"],
                "rtl_functional_coverage": False,
                "coverage_percent": (coverage_report.get("final") or {}).get(
                    "percent"
                ),
                "coverage_target_pct": coverage_report["target_percent"],
                "target_met": coverage_report["target_met"],
                "warning": coverage_report.get("warning"),
                "error": coverage_report.get("error"),
                "report": str(coverage_report_path),
            },
        )
        enforce_oracle_line_coverage(spec, coverage_report)
        state = _update(state, phase="oracle_quorum_revalidating")
        refined_trace_sha256 = content_hash(expected_trace)
        expected_trace, final_quorum = revalidate_oracle_quorum(
            spec,
            testplan,
            python_model,
            oracle_metadata,
            run_dir / "oracle-candidates",
            run_dir / "oracle-final-quorum",
            origin_bundle=origin_bundle,
        )
        if content_hash(expected_trace) != refined_trace_sha256:
            raise RuntimeError(
                "selected Python oracle was nondeterministic when replayed on the final plan"
            )
        coverage_report["final_expected_trace_sha256"] = content_hash(expected_trace)
        coverage_report["final_oracle_quorum_sha256"] = content_hash(final_quorum)
        enforce_oracle_line_coverage(
            spec,
            coverage_report,
            final_plan=testplan,
            final_expected_trace=expected_trace,
            final_quorum=final_quorum,
        )
        oracle_metadata["final_plan_quorum"] = final_quorum
        quorum_path = run_dir / "oracle-quorum.json"
        atomic_write(
            quorum_path,
            json.dumps(final_quorum, indent=2, ensure_ascii=False) + "\n",
            0o400,
        )
        atomic_write(
            coverage_report_path,
            json.dumps(coverage_report, indent=2, ensure_ascii=False) + "\n",
            0o400,
        )
        atomic_write(
            plan_path,
            json.dumps(testplan, indent=2, ensure_ascii=False) + "\n",
            0o400,
        )
        plan_draft_path.chmod(0o400)
        contract_metadata.update(
            {
                "testplan_sha256": content_hash(testplan),
                "total_scenario_count": len(testplan["scenarios"]),
                "coverage_added_scenario_count": coverage_report[
                    "added_scenario_count"
                ],
                "oracle_line_coverage": coverage_report["final"],
            }
        )
        oracle_metadata["coverage_refinement"] = coverage_report
        coverage_summary = {
            "status": coverage_report["status"],
            "mode": coverage_report["mode"],
            "metric": coverage_report["metric"],
            "rtl_functional_coverage": coverage_report["rtl_functional_coverage"],
            "limitation": coverage_report["limitation"],
            "round": len(coverage_report["rounds"]),
            "max_rounds": spec.verification["oracle_coverage_rounds"],
            "coverage_percent": (coverage_report.get("final") or {}).get(
                "percent"
            ),
            "coverage_target_pct": coverage_report["target_percent"],
            "target_met": coverage_report["target_met"],
            "remaining_cycle_budget": coverage_report[
                "remaining_cycle_budget"
            ],
            "added_scenario_count": coverage_report["added_scenario_count"],
            "stop_reason": coverage_report["stop_reason"],
            "warning": coverage_report.get("warning"),
            "error": coverage_report.get("error"),
            "report": str(coverage_report_path),
        }
        oracle_path = run_dir / "reference_model.py"
        expected_path = run_dir / "expected-trace.json"
        atomic_write(oracle_path, python_model, 0o400)
        atomic_write(
            expected_path,
            json.dumps(expected_trace, indent=2, ensure_ascii=False) + "\n",
            0o400,
        )
        manifest_path = run_dir / "frozen-manifest.json"
        manifest = {
            "schema_version": 1,
            "contract_sha256": content_hash(contract),
            "testplan_sha256": content_hash(testplan),
            "oracle_sha256": content_hash(python_model),
            "expected_trace_sha256": content_hash(expected_trace),
        }
        manifest["bundle_sha256"] = content_hash(manifest)
        atomic_write(
            manifest_path,
            json.dumps(manifest, indent=2) + "\n",
            0o400,
        )
        state = _update(
            state,
            phase="oracle_frozen",
            oracle=oracle_metadata,
            artifacts={
                **(state.get("artifacts") or {}),
                "oracle": str(oracle_path),
                "testplan": str(plan_path),
                "expected_trace": str(expected_path),
                "frozen_manifest": str(manifest_path),
                "coverage_refinement": str(coverage_report_path),
                "oracle_quorum": str(quorum_path),
                "oracle_origins": str(origin_bundle_path),
            },
            contract=contract_metadata,
            coverage=coverage_summary,
        )

        state = _update(state, phase="generating")
        question = render_question(spec)
        interface = render_interface(spec)
        atomic_write(run_dir / "question.txt", question + "\n")
        atomic_write(run_dir / "interface.sv", interface)
        result = run_problem(
            task_id=run_id,
            question=question,
            ref_sv=interface,
            v_backend=v_backend,
            p_backend=StaticBackend(python_model, language="python"),
            verifier=cross_verify,
            n=spec.generation["n"],
            max_turns=spec.generation["max_turns"],
            num_verify_tests=spec.generation["num_verify_tests"],
            temperature=spec.generation["temperature"],
            seed=spec.generation["seed"],
        )
        result_data = result.to_dict()
        atomic_write(
            run_dir / "rtl_closure-result.json",
            json.dumps(result_data, indent=2) + "\n",
        )
        state = _update(
            state,
            phase="initial_candidate_ready",
            rtl_closure={
                "matched": result.matched,
                "best_match_rate": result.best_match_rate,
                "best_turn": result.best_turn,
                "turns_used": result.turns_used,
            },
        )
        if not result.verilog.strip():
            raise RuntimeError("RTL-Closure returned no initial Verilog candidate")
        rtl = normalize_top_module(result.verilog, spec.name)

        def repair_progress(phase: str, summary: dict[str, Any]) -> None:
            nonlocal state
            current = _merge_repair_progress(
                dict(state.get("repair") or {}), phase, summary
            )
            state = _update(state, phase=phase, repair=current)

        outcome = repair_loop(
            spec,
            v_backend,
            rtl,
            contract,
            testplan,
            expected_trace,
            run_dir,
            contract_path=contract_path,
            plan_path=plan_path,
            oracle_path=oracle_path,
            expected_path=expected_path,
            manifest_path=manifest_path,
            frozen_manifest=manifest,
            origin_bundle=origin_bundle,
            progress=repair_progress,
        )
        rtl = outcome.rtl
        adaptive_corpus_path = run_dir / "adaptive-corpus.json"
        adaptive_corpus = json.loads(
            adaptive_corpus_path.read_text(encoding="utf-8")
        )
        if adaptive_corpus.get("schema_version") != 2:
            raise RuntimeError("adaptive suite evidence has an invalid schema")
        trusted_base_suite = suite_hash(testplan, expected_trace)
        if adaptive_corpus.get("base_suite_sha256") != trusted_base_suite:
            raise RuntimeError("adaptive suite evidence has an untrusted base suite")
        verified_active_suite = verify_epoch_chain(
            adaptive_corpus.get("epochs", []),
            testplan,
            expected_trace,
            epoch_artifact_root=run_dir / "adaptive",
            spec=spec,
            oracle_sha256=manifest["oracle_sha256"],
            selected_oracle=python_model,
            origin_bundle=origin_bundle,
        )
        if adaptive_corpus.get("active_suite_sha256") != verified_active_suite:
            raise RuntimeError("adaptive suite evidence has an invalid active suite")
        expected_chain_head = (
            adaptive_corpus["epochs"][-1]["epoch_sha256"]
            if adaptive_corpus.get("epochs")
            else None
        )
        if adaptive_corpus.get("chain_head_sha256") != expected_chain_head:
            raise RuntimeError("adaptive suite evidence has an invalid chain head")
        if adaptive_corpus.get("epochs"):
            final_epoch_dir = (
                run_dir
                / "adaptive"
                / f"epoch-{len(adaptive_corpus['epochs']):02d}"
            )
            active_plan = _read_json_object(final_epoch_dir / "testplan.json")
            active_expected_trace = _read_json_object(
                final_epoch_dir / "expected-trace.json"
            )
        else:
            active_plan = testplan
            active_expected_trace = expected_trace
        outcome, independent_report = close_independent_verification(
            spec,
            v_backend,
            outcome,
            contract,
            testplan,
            expected_trace,
            active_plan,
            active_expected_trace,
            run_dir,
            interface=interface,
            python_model=python_model,
            verifier=cross_verify,
            contract_path=contract_path,
            plan_path=plan_path,
            oracle_path=oracle_path,
            expected_path=expected_path,
            manifest_path=manifest_path,
            frozen_manifest=manifest,
            progress=repair_progress,
        )
        rtl = outcome.rtl
        if adaptive_corpus.get("active_suite_sha256") != outcome.verification.get(
            "suite_sha256"
        ):
            raise RuntimeError(
                "adaptive suite evidence does not match the final verification result"
            )
        formal_evidence = outcome.verification.get("formal") or {}
        if formal_evidence and formal_evidence.get(
            "suite_sha256"
        ) != outcome.verification.get("suite_sha256"):
            raise RuntimeError(
                "formal evidence was produced for a different verification suite"
            )
        state, final_universal = _record_repair_outcome(
            spec, outcome, rtl, run_dir, adaptive_corpus, state
        )
        replayed_trace, final_suite_quorum = revalidate_oracle_bundle_quorum(
            spec,
            active_plan,
            python_model,
            origin_bundle,
            run_dir / "oracle-final-suite-quorum",
        )
        if content_hash(replayed_trace) != content_hash(active_expected_trace):
            raise RuntimeError(
                "final active suite changed when replayed through oracle quorum"
            )
        final_suite_quorum_path = run_dir / "final-suite-quorum.json"
        atomic_write(
            final_suite_quorum_path,
            json.dumps(final_suite_quorum, indent=2, ensure_ascii=False) + "\n",
            0o400,
        )
        adaptive_corpus_path.chmod(0o400)
        for evidence_name in (
            "formal-report.json",
            "formal-properties.json",
            "formal-manifest.json",
            "formal-checkpoint.json",
            "formal-execution-evidence.json",
        ):
            evidence_path = run_dir / evidence_name
            if evidence_path.is_file():
                evidence_path.chmod(0o400)
        universal_execution_evidence = _build_universal_execution_evidence(
            spec, rtl, final_universal, run_dir
        )
        atomic_write(
            run_dir / "universal-execution-evidence.json",
            json.dumps(
                universal_execution_evidence, indent=2, ensure_ascii=False
            )
            + "\n",
        )
        state = _update(state, phase="release_validating")
        check_rtl_policy(rtl)
        check_exact_interface(rtl, spec.ports, spec.name)
        independent = independent_report["runs"]
        state = _update(state, phase="release_yosys_check")
        yosys_tail = yosys_check(rtl, spec.name)
        yosys_path = run_dir / "yosys-check.log"
        atomic_write(yosys_path, yosys_tail + "\n")
        state = _update(state, phase="release_architecture_audit")
        architecture_audit = run_architecture_audit(
            rtl, spec.name, spec.architecture
        )
        architecture_path = run_dir / "architecture-audit.json"
        atomic_write(
            architecture_path,
            json.dumps(architecture_audit, indent=2, ensure_ascii=False) + "\n",
        )
        failed_checks = [
            row["id"]
            for row in architecture_audit.get("checks", [])
            if row.get("status") == "fail"
        ]
        state = _update(
            state,
            phase="release_architecture_audit",
            architecture_audit={
                "status": architecture_audit["status"],
                "failed_checks": failed_checks,
                "artifact": str(architecture_path),
                "observed": architecture_audit.get("observed"),
            },
        )
        if failed_checks:
            raise RuntimeError(
                "architecture constraints failed: " + ", ".join(failed_checks)
            )
        state = _update(state, phase="release_synthesis_equivalence")
        equivalence, equivalence_log = run_synthesis_equivalence(
            rtl,
            spec.name,
            clock_port=spec.clock_port,
            reset=spec.reset,
        )
        equivalence_path = run_dir / "synthesis-equivalence.json"
        equivalence_log_path = run_dir / "synthesis-equivalence.log"
        atomic_write(
            equivalence_path,
            json.dumps(equivalence, indent=2, ensure_ascii=False) + "\n",
        )
        atomic_write(equivalence_log_path, equivalence_log + "\n")
        state = _update(
            state,
            phase="release_synthesis_equivalence",
            synthesis_equivalence={
                **equivalence,
                "artifact": str(equivalence_path),
                "log": str(equivalence_log_path),
            },
        )
        verify_frozen_assets(
            contract=contract,
            plan=testplan,
            expected_trace=expected_trace,
            contract_path=contract_path,
            plan_path=plan_path,
            oracle_path=oracle_path,
            expected_path=expected_path,
            manifest_path=manifest_path,
            frozen_manifest=manifest,
        )
        evidence_names = _required_release_artifacts(spec)
        for name in evidence_names:
            path = run_dir / name
            if not path.is_file():
                raise RuntimeError(f"release evidence is missing: {name}")
            path.chmod(0o400)
        artifact_hashes = {name: _artifact_hash(run_dir / name) for name in evidence_names}
        release_gate = {
            "passed": True,
            "sandbox_image_identity": state.get("sandbox_image_identity"),
            "verify_image_identity": state.get("verify_image_identity"),
            "rtl_sha256": content_hash(rtl),
            "oracle_sha256": content_hash(python_model),
            "frozen_bundle_sha256": manifest["bundle_sha256"],
            "independent_verification": independent,
            "yosys_log": str(yosys_path),
            "architecture_audit": architecture_audit,
            "universal_properties": outcome.verification.get(
                "universal_properties"
            ),
            "synthesis_equivalence": equivalence,
            "synthesis_equivalence_log": str(equivalence_log_path),
            "formal": outcome.verification.get("formal"),
            "formal_mode": spec.verification["formal_mode"],
            "active_suite_sha256": outcome.verification.get("suite_sha256"),
            "adaptive_chain_head_sha256": (
                adaptive_corpus["epochs"][-1]["epoch_sha256"]
                if adaptive_corpus.get("epochs")
                else None
            ),
            "required_artifacts": list(evidence_names),
            "artifact_hashes": artifact_hashes,
        }
        state = _update(
            state,
            phase="publishing",
            independent_verification=independent,
            release_gate=release_gate,
        )
        design_slug, config_path = publish_design(
            spec, state, rtl, python_model, result_data
        )
        publication_manifest = _read_json_object(
            config_path.parent / "publication-manifest.json"
        )
        state = _update(
            state,
            design_slug=design_slug,
            design_dir=str(config_path.parent),
            design_config=str(config_path),
            publication_manifest=str(
                config_path.parent / "publication-manifest.json"
            ),
            publication_bundle_sha256=publication_manifest[
                "publication_bundle_sha256"
            ],
            lane_input_manifest=publication_manifest["lane_input_manifest"],
            lane_input_bundle_sha256=publication_manifest[
                "lane_input_bundle_sha256"
            ],
            announce=False,
            record_event=False,
        )
        if state["generate_only"]:
            _update(
                state,
                phase="completed",
                status="completed",
                elapsed_seconds=max(
                    0, round(time.time() - float(state["started_epoch"]))
                ),
            )
            return 0
        state = _update(state, phase="starting_lane")
        start_lane(spec, state)
        return 0
    except WorkerFenceError as exc:
        print(f"rtl-closure worker fenced: {exc}", file=sys.stderr, flush=True)
        return 75
    except Exception as exc:
        structured_failure = getattr(exc, "error", None)
        if not isinstance(structured_failure, dict):
            structured_failure = None
        try:
            state = load_state(run_id)
            _update(
                state,
                phase="failed",
                status="failed",
                error=str(exc),
                failure=structured_failure,
                failure_class=getattr(exc, "failure_class", None),
                failure_gate=getattr(exc, "failure_gate", None),
                failure_code=getattr(exc, "failure_code", None),
                elapsed_seconds=max(
                    0, round(time.time() - float(state["started_epoch"]))
                ),
            )
        except WorkerFenceError as fence_exc:
            print(
                f"rtl-closure worker fenced: {fence_exc}",
                file=sys.stderr,
                flush=True,
            )
            return 75
        print(f"rtl-closure worker failed: {exc}", file=sys.stderr, flush=True)
        return 1
    finally:
        _ACTIVE_WORKER_FENCE = None


def _mapped_equivalence_evidence_errors(
    child: dict[str, Any],
    *,
    expected_run_id: str,
    expected_pdk: str,
    expected_design_config: str | Path | None,
) -> list[str]:
    evidence = child.get("mapped_equivalence")
    if not isinstance(evidence, dict):
        return ["required mapped-equivalence evidence is missing"]

    errors: list[str] = []
    if (
        evidence.get("schema_version") != 2
        or evidence.get("requested") is not True
        or evidence.get("status") != "pass"
        or evidence.get("gate") != "pass"
        or evidence.get("provider") != MAPPED_EQUIVALENCE_PROVIDER
        or evidence.get("provider_version") != MAPPED_EQUIVALENCE_PROVIDER_VERSION
        or evidence.get("scope") != MAPPED_EQUIVALENCE_SCOPE
        or evidence.get("reasons") != []
    ):
        errors.append("mapped-equivalence protocol identity or verdict is invalid")
    if (
        child.get("run_id") != expected_run_id
        or evidence.get("run_id") != expected_run_id
    ):
        errors.append("mapped-equivalence evidence belongs to another run")
    assurance = child.get("assurance")
    if (
        not isinstance(assurance, dict)
        or assurance.get("pdk") != expected_pdk
        or assurance.get("open_pdk") is not True
        or assurance.get("foundry_signoff") is not False
        or assurance.get("tapeout_guarantee") is not False
        or not isinstance(assurance.get("limitations"), list)
        or not assurance["limitations"]
    ):
        errors.append("child open-PDK assurance boundary is invalid")

    expected_config_path: Path | None = None
    expected_design_dir: Path | None = None
    expected_run_dir: Path | None = None
    expected_eqy_script_path: Path | None = None
    expected_eqy_script_sha256: str | None = None
    expected_eqy_gate_tcl_path: Path | None = None
    expected_eqy_gate_tcl_sha256: str | None = None
    if not isinstance(expected_design_config, (str, Path)) or not str(
        expected_design_config
    ):
        errors.append("parent published design config identity is missing")
    else:
        config_candidate = Path(expected_design_config)
        try:
            config_resolved = config_candidate.resolve(strict=True)
        except (OSError, RuntimeError, ValueError):
            config_resolved = None
        if (
            config_resolved is None
            or not config_candidate.is_absolute()
            or config_candidate != config_resolved
            or config_candidate.is_symlink()
            or not config_resolved.is_file()
        ):
            errors.append("parent published design config identity is invalid")
        else:
            expected_config_path = config_resolved
            expected_design_dir = config_resolved.parent
            runs_dir = expected_design_dir / "runs"
            run_candidate = runs_dir / f"PI_LANE_{expected_run_id}"
            try:
                run_resolved = run_candidate.resolve(strict=True)
            except (OSError, RuntimeError, ValueError):
                run_resolved = None
            if (
                run_resolved is None
                or runs_dir.is_symlink()
                or not runs_dir.is_dir()
                or run_candidate != run_resolved
                or run_candidate.is_symlink()
                or not run_resolved.is_dir()
            ):
                errors.append("published design run directory identity is invalid")
            else:
                expected_run_dir = run_resolved
            try:
                expected_config_sha256 = hashlib.sha256(
                    config_resolved.read_bytes()
                ).hexdigest()
            except OSError:
                expected_config_sha256 = None
            if (
                child.get("design_dir") != str(expected_design_dir)
                or child.get("config_path") != str(expected_config_path)
                or expected_config_sha256 is None
                or child.get("config_sha256") != expected_config_sha256
            ):
                errors.append("child design/config identity does not match publication")
            try:
                published_config = yaml.safe_load(
                    config_resolved.read_text(encoding="utf-8")
                )
            except (OSError, UnicodeError, yaml.YAMLError):
                published_config = None
            script_reference = (
                published_config.get("EQY_SCRIPT")
                if isinstance(published_config, dict)
                else None
            )
            design_name = (
                published_config.get("DESIGN_NAME")
                if isinstance(published_config, dict)
                else None
            )
            if (
                not isinstance(published_config, dict)
                or published_config.get("PDK") != expected_pdk
                or published_config.get("RUN_EQY") is not True
                or not isinstance(design_name, str)
                or not isinstance(script_reference, str)
                or not script_reference.startswith("dir::")
            ):
                errors.append(
                    "published design config does not bind PDK/RUN_EQY/EQY_SCRIPT"
                )
            else:
                expected_eqy_script_sha256 = hashlib.sha256(
                    render_eqy_script_for_name(design_name).encode("utf-8")
                ).hexdigest()
                expected_eqy_gate_tcl_sha256 = hashlib.sha256(
                    render_eqy_gate_tcl().encode("utf-8")
                ).hexdigest()
                script_candidate = expected_design_dir / script_reference.removeprefix(
                    "dir::"
                )
                try:
                    script_resolved = script_candidate.resolve(strict=True)
                    script_resolved.relative_to(expected_design_dir)
                except (OSError, RuntimeError, ValueError):
                    script_resolved = None
                if (
                    script_resolved is None
                    or script_candidate != script_resolved
                    or script_candidate.is_symlink()
                    or not script_resolved.is_file()
                ):
                    errors.append("published EQY script identity is invalid")
                else:
                    expected_eqy_script_path = script_resolved
                    gate_tcl_candidate = script_resolved.with_name(
                        f"{design_name}-gate.tcl"
                    )
                    try:
                        gate_tcl_resolved = gate_tcl_candidate.resolve(strict=True)
                    except (OSError, RuntimeError, ValueError):
                        gate_tcl_resolved = None
                    if (
                        gate_tcl_resolved is None
                        or gate_tcl_candidate != gate_tcl_resolved
                        or gate_tcl_candidate.is_symlink()
                        or not gate_tcl_resolved.is_file()
                    ):
                        errors.append("published EQY gate Tcl identity is invalid")
                    else:
                        expected_eqy_gate_tcl_path = gate_tcl_resolved

    raw_run_dir = evidence.get("run_dir")
    run_dir: Path | None = None
    if not isinstance(raw_run_dir, str) or not raw_run_dir:
        errors.append("mapped-equivalence run directory is missing")
    else:
        candidate = Path(raw_run_dir)
        try:
            resolved = candidate.resolve(strict=True)
        except (OSError, RuntimeError, ValueError):
            resolved = None
        if (
            resolved is None
            or not candidate.is_absolute()
            or candidate != resolved
            or candidate.is_symlink()
            or not resolved.is_dir()
            or resolved.name != f"PI_LANE_{expected_run_id}"
            or expected_run_dir is None
            or resolved != expected_run_dir
            or child.get("librelane_run_dir") != str(resolved)
        ):
            errors.append("mapped-equivalence run directory identity is invalid")
        else:
            run_dir = resolved

    step = evidence.get("step")
    step_dir: Path | None = None
    ordinal: int | None = None
    if not isinstance(step, dict):
        errors.append("mapped-equivalence Step identity is missing")
    else:
        ordinal_value = step.get("ordinal")
        raw_step_dir = step.get("directory")
        if (
            isinstance(ordinal_value, bool)
            or not isinstance(ordinal_value, int)
            or ordinal_value < 1
            or step.get("id") != "Yosys.EQY"
            or not isinstance(raw_step_dir, str)
        ):
            errors.append("mapped-equivalence Step identity is invalid")
        else:
            ordinal = ordinal_value
            candidate = Path(raw_step_dir)
            try:
                resolved = candidate.resolve(strict=True)
            except (OSError, RuntimeError, ValueError):
                resolved = None
            if (
                resolved is None
                or not candidate.is_absolute()
                or candidate != resolved
                or candidate.is_symlink()
                or not resolved.is_dir()
                or run_dir is None
                or resolved.parent != run_dir
                or not resolved.name.startswith(f"{ordinal}-")
            ):
                errors.append("mapped-equivalence Step directory is invalid")
            else:
                step_dir = resolved

    history = child.get("step_history")
    matching_history = (
        [
            row
            for row in history
            if isinstance(row, dict) and row.get("id") == "Yosys.EQY"
        ]
        if isinstance(history, list)
        else []
    )
    if (
        len(matching_history) != 1
        or ordinal is None
        or step_dir is None
        or matching_history[0].get("ordinal") != ordinal
        or matching_history[0].get("directory") != str(step_dir)
        or matching_history[0].get("status") != "completed"
    ):
        errors.append("mapped-equivalence evidence does not match unique Step history")

    def attest_file(
        value: Any,
        *,
        label: str,
        expected_parent: Path | None = None,
        expected_name: str | None = None,
        maximum_bytes: int = 256 * 1024 * 1024,
        allowed_root: Path | None = None,
    ) -> Path | None:
        if not isinstance(value, dict):
            errors.append(f"mapped-equivalence {label} evidence is missing")
            return None
        raw_path = value.get("path")
        size = value.get("size_bytes")
        digest = value.get("sha256")
        if (
            not isinstance(raw_path, str)
            or not raw_path
            or isinstance(size, bool)
            or not isinstance(size, int)
            or size <= 0
            or size > maximum_bytes
            or not isinstance(digest, str)
            or not re.fullmatch(r"[0-9a-f]{64}", digest)
        ):
            errors.append(f"mapped-equivalence {label} metadata is invalid")
            return None
        candidate = Path(raw_path)
        try:
            resolved = candidate.resolve(strict=True)
            root = allowed_root if allowed_root is not None else run_dir
            if root is None:
                raise ValueError("trusted artifact root unavailable")
            resolved.relative_to(root)
        except (OSError, RuntimeError, ValueError):
            errors.append(f"mapped-equivalence {label} path is outside the run")
            return None
        if (
            not candidate.is_absolute()
            or candidate != resolved
            or candidate.is_symlink()
            or not resolved.is_file()
            or (expected_parent is not None and resolved.parent != expected_parent)
            or (expected_name is not None and resolved.name != expected_name)
        ):
            errors.append(f"mapped-equivalence {label} path identity is invalid")
            return None
        try:
            actual_size = resolved.stat().st_size
            actual_digest = hashlib.sha256(resolved.read_bytes()).hexdigest()
        except OSError:
            errors.append(f"mapped-equivalence {label} file is unreadable")
            return None
        if actual_size != size or actual_digest != digest:
            errors.append(f"mapped-equivalence {label} hash or size changed")
            return None
        return resolved

    bundle = evidence.get("evidence")
    bundle = bundle if isinstance(bundle, dict) else {}
    config_path = attest_file(
        bundle.get("config"),
        label="config",
        expected_parent=step_dir,
        expected_name="config.json",
    )
    eqy_script_path = attest_file(
        bundle.get("eqy_script"),
        label="EQY script",
        expected_parent=(
            expected_eqy_script_path.parent
            if expected_eqy_script_path is not None
            else None
        ),
        expected_name=(
            expected_eqy_script_path.name
            if expected_eqy_script_path is not None
            else None
        ),
        maximum_bytes=1024 * 1024,
        allowed_root=expected_design_dir,
    )
    if (
        eqy_script_path is not None
        and (
            eqy_script_path != expected_eqy_script_path
            or hashlib.sha256(eqy_script_path.read_bytes()).hexdigest()
            != expected_eqy_script_sha256
        )
    ):
        errors.append("mapped-equivalence EQY script hash does not match publication")
    eqy_gate_tcl_path = attest_file(
        bundle.get("eqy_gate_tcl"),
        label="EQY gate Tcl",
        expected_parent=(
            expected_eqy_gate_tcl_path.parent
            if expected_eqy_gate_tcl_path is not None
            else None
        ),
        expected_name=(
            expected_eqy_gate_tcl_path.name
            if expected_eqy_gate_tcl_path is not None
            else None
        ),
        maximum_bytes=1024 * 1024,
        allowed_root=expected_design_dir,
    )
    if (
        eqy_gate_tcl_path is not None
        and (
            eqy_gate_tcl_path != expected_eqy_gate_tcl_path
            or hashlib.sha256(eqy_gate_tcl_path.read_bytes()).hexdigest()
            != expected_eqy_gate_tcl_sha256
        )
    ):
        errors.append("mapped-equivalence EQY gate Tcl hash does not match publication")
    state_in_path = attest_file(
        bundle.get("state_in"),
        label="state_in",
        expected_parent=step_dir,
        expected_name="state_in.json",
    )
    state_out_path = attest_file(
        bundle.get("state_out"),
        label="state_out",
        expected_parent=step_dir,
        expected_name="state_out.json",
    )
    status_parent = step_dir / "scratch" if step_dir is not None else None
    status_path = attest_file(
        bundle.get("status"),
        label="PASS status",
        expected_parent=status_parent,
        expected_name="PASS",
    )
    if status_parent is not None and any(
        (status_parent / name).exists()
        for name in ("FAIL", "UNKNOWN", "ERROR", "TIMEOUT")
    ):
        errors.append("mapped-equivalence scratch contains a failure status")
    raw_logs = bundle.get("logs")
    log_paths: list[Path] = []
    if not isinstance(raw_logs, list) or not raw_logs:
        errors.append("mapped-equivalence log evidence is missing")
    else:
        for index, row in enumerate(raw_logs, 1):
            path = attest_file(
                row,
                label=f"log {index}",
                expected_parent=step_dir,
            )
            if path is not None:
                if path.suffix != ".log" or path in log_paths:
                    errors.append("mapped-equivalence log identity is invalid")
                else:
                    log_paths.append(path)
    if config_path is not None:
        try:
            config = json.loads(config_path.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, json.JSONDecodeError):
            config = None
        if (
            not isinstance(config, dict)
            or not isinstance(config.get("meta"), dict)
            or config["meta"].get("step") != "Yosys.EQY"
            or config.get("PDK") != expected_pdk
            or config.get("EQY_SCRIPT")
            != (str(eqy_script_path) if eqy_script_path is not None else None)
        ):
            errors.append("mapped-equivalence Step config does not bind Step/PDK/EQY_SCRIPT")
    for label, path in (("state_in", state_in_path), ("state_out", state_out_path)):
        if path is None:
            continue
        try:
            value = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, json.JSONDecodeError):
            value = None
        if not isinstance(value, dict) or not value:
            errors.append(f"mapped-equivalence {label} is not a non-empty JSON object")

    top_logs = evidence.get("logs")
    if top_logs != [str(path) for path in log_paths]:
        errors.append("mapped-equivalence top-level logs disagree with evidence")
    if evidence.get("artifact") != (
        str(state_out_path) if state_out_path is not None else None
    ):
        errors.append("mapped-equivalence artifact disagrees with state_out")
    if matching_history and (
        matching_history[0].get("state_out")
        != (str(state_out_path) if state_out_path is not None else None)
        or matching_history[0].get("logs") != [str(path) for path in log_paths]
    ):
        errors.append("mapped-equivalence files disagree with Step history")

    verdict = evidence.get("verdict")
    status_record = bundle.get("status") if isinstance(bundle, dict) else None
    verdict_log: Path | None = None
    if isinstance(verdict, dict) and isinstance(verdict.get("log_path"), str):
        candidate = Path(verdict["log_path"])
        if candidate in log_paths:
            verdict_log = candidate
    try:
        verdict_log_sha256 = (
            hashlib.sha256(verdict_log.read_bytes()).hexdigest()
            if verdict_log is not None
            else None
        )
    except OSError:
        verdict_log_sha256 = None
    if (
        not isinstance(verdict, dict)
        or verdict.get("status") != "PASS"
        or verdict.get("source") != "eqy_status_file_and_step_log"
        or verdict.get("status_marker") != "PASS status file"
        or verdict.get("full_design_marker")
        != "Successfully proved designs equivalent"
        or verdict.get("log_marker") != "DONE (PASS, rc=0)"
        or verdict.get("status_path")
        != (str(status_path) if status_path is not None else None)
        or verdict.get("status_sha256")
        != (status_record or {}).get("sha256")
        or verdict_log is None
        or verdict.get("log_sha256") != verdict_log_sha256
    ):
        errors.append("mapped-equivalence machine verdict is invalid")
    else:
        try:
            status_text = status_path.read_text(encoding="utf-8") if status_path else ""
            log_text = verdict_log.read_text(encoding="utf-8")
            all_log_text = [path.read_text(encoding="utf-8") for path in log_paths]
        except (OSError, UnicodeError):
            status_text = ""
            log_text = ""
            all_log_text = []
        if (
            not status_text.strip()
            or verdict["full_design_marker"] not in log_text
            or verdict["log_marker"] not in log_text
            or sum(
                verdict["full_design_marker"] in text
                and verdict["log_marker"] in text
                for text in all_log_text
            )
            != 1
        ):
            errors.append("mapped-equivalence PASS markers are absent or ambiguous")
    return errors


def _mapped_equivalence_mode(state: dict[str, Any]) -> str:
    mode = state.get("mapped_equivalence_mode")
    if mode in {"off", "required"}:
        return str(mode)
    return "invalid"


def _degrade_completed_publication(
    state: dict[str, Any], error: str
) -> dict[str, Any]:
    lane_state_value = state.get("lane_state")
    lane_state = dict(lane_state_value) if isinstance(lane_state_value, dict) else None
    if lane_state is not None:
        reasons = list(lane_state.get("delivery_gate_reasons") or [])
        reason = f"publication_manifest: {error}"
        if reason not in reasons:
            reasons.append(reason)
        lane_state["delivery_gate"] = "incomplete"
        lane_state["delivery_gate_reasons"] = reasons
    return _update(
        state,
        status="incomplete",
        phase="release_integrity_incomplete",
        allow_terminal_transition=True,
        lane_state=lane_state,
        error="published release evidence changed after completion: " + error,
        failure_class="release_integrity",
        failure_gate="publication_manifest",
        failure_code="release_integrity/publication_invalid",
    )


def _refresh_state_unlocked(
    state: dict[str, Any], *, integrity_audit_id: str | None = None
) -> dict[str, Any]:
    if (
        state.get("status") in TERMINAL_STATUSES
        and state.get("failure_code") == "release_integrity/publication_invalid"
    ):
        return state
    try:
        publication = (
            _verify_publication(state)
            if state.get("publication_bundle_sha256") is not None
            else None
        )
    except RuntimeError as exc:
        if state.get("status") == "completed":
            return _degrade_completed_publication(state, str(exc)[:4096])
        raise
    reconcile_completed = bool(
        state.get("status") == "completed" and state.get("lane_run_id")
    )
    if state.get("status") in TERMINAL_STATUSES:
        lane_state_value = state.get("lane_state")
        if publication is not None and isinstance(lane_state_value, dict):
            origin = state.get("origin") or {}
            lane_input_mismatch = (
                state.get("failure_code")
                == "release_integrity/lane_input_mismatch"
            )
            _validate_lane_state_identity(
                lane_state_value,
                expected_run_id=(
                    str(state["lane_run_id"])
                    if state.get("lane_run_id") is not None
                    else None
                ),
                expected_workflow_id=(
                    str(origin["workflow_id"])
                    if origin.get("workflow_id") is not None
                    else None
                ),
                expected_attempt_id=(
                    str(state["active_lane_attempt_id"])
                    if state.get("active_lane_attempt_id") is not None
                    else None
                ),
                expected_idempotency_key=_active_lane_launch_key(state),
                expected_input_manifest=(
                    None
                    if lane_input_mismatch
                    else publication["lane_input_manifest"]
                ),
                expected_input_bundle_sha256=(
                    None
                    if lane_input_mismatch
                    else publication["lane_input_bundle_sha256"]
                ),
            )
        if (
            isinstance(lane_state_value, dict)
            and state.get("status") == "completed"
            and _mapped_equivalence_mode(state) == "required"
        ):
            equivalence_errors = _mapped_equivalence_evidence_errors(
                lane_state_value,
                expected_run_id=str(state.get("lane_run_id") or ""),
                expected_pdk=str(state.get("pdk") or ""),
                expected_design_config=state.get("design_config"),
            )
            if equivalence_errors:
                lane_state = dict(lane_state_value)
                reasons = list(lane_state.get("delivery_gate_reasons") or [])
                reasons.extend(
                    f"mapped_equivalence: {error}"
                    for error in equivalence_errors
                    if f"mapped_equivalence: {error}" not in reasons
                )
                lane_state["delivery_gate"] = "incomplete"
                lane_state["delivery_gate_reasons"] = reasons
                return _update(
                    state,
                    status="incomplete",
                    phase="lane_delivery_incomplete",
                    allow_terminal_transition=True,
                    lane_state=lane_state,
                    error=(
                        "mapped-equivalence release evidence changed after "
                        "completion: " + "; ".join(equivalence_errors[:8])
                    ),
                    failure_class="release_integrity",
                    failure_gate="mapped_equivalence",
                    failure_code="release_integrity/mapped_equivalence_evidence",
                )
        if not reconcile_completed:
            return state
    changes: dict[str, Any] = {}
    phase: str | None = None
    status: str | None = None
    record_event = False
    completed_parent = state.get("status") == "completed"
    reconcile_succeeded = False
    lane_run_id = state.get("lane_run_id")
    if lane_run_id:
        lane_command = [
            "lane-agent",
            "reconcile",
            "--run-id",
            str(lane_run_id),
            "--json",
        ]
        if integrity_audit_id is not None:
            lane_command.extend(["--integrity-audit-id", integrity_audit_id])
        try:
            completed = subprocess.run(
                lane_command,
                capture_output=True,
                text=True,
                timeout=20,
                check=False,
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            changes["lane_status_error"] = str(exc)
            record_event = state.get("lane_status_error") != str(exc)
            phase = "lane_status_unknown"
            completed = None
        if completed is not None and completed.returncode != 0:
            changes["lane_status_error"] = (
                completed.stderr or completed.stdout or "lane-agent status failed"
            ).strip()
            record_event = state.get("lane_status_error") != changes[
                "lane_status_error"
            ]
            phase = "lane_status_unknown"
        elif completed is not None and completed.stdout.strip():
            lane_payload: Any = None
            try:
                origin = state.get("origin") or {}
                lane_payload = json.loads(completed.stdout)
                lane_state = _validate_lane_state_identity(
                    lane_payload,
                    expected_run_id=str(lane_run_id),
                    expected_workflow_id=(
                        str(origin["workflow_id"])
                        if origin.get("workflow_id") is not None
                        else None
                    ),
                    expected_attempt_id=(
                        str(state["active_lane_attempt_id"])
                        if state.get("active_lane_attempt_id") is not None
                        else None
                    ),
                    expected_idempotency_key=_active_lane_launch_key(state),
                    expected_input_manifest=(
                        publication["lane_input_manifest"]
                        if publication is not None
                        else None
                    ),
                    expected_input_bundle_sha256=(
                        publication["lane_input_bundle_sha256"]
                        if publication is not None
                        else None
                    ),
                    expected_integrity_audit_id=integrity_audit_id,
                )
                reconcile_succeeded = True
                changes["lane_state"] = lane_state
                release_integrity = lane_state.get("release_integrity")
                if isinstance(release_integrity, dict):
                    changes["last_integrity_audit_id"] = release_integrity.get(
                        "audit_id"
                    )
                changes["lane_status_error"] = None
                record_event = bool(state.get("lane_status_error"))
                phase = "lane_" + str(
                    lane_state.get("active_step") or lane_state["status"]
                )
                attempts = [
                    dict(row)
                    for row in state.get("lane_attempts", [])
                    if isinstance(row, dict)
                ]
                matching_attempt = next(
                    (
                        row
                        for row in reversed(attempts)
                        if row.get("lane_run_id") == lane_run_id
                    ),
                    None,
                )
                if matching_attempt is None:
                    raise ValueError(
                        "persisted Lane run has no matching attempt lineage"
                    )
                previous_child_status = (state.get("lane_state") or {}).get("status")
                matching_attempt["status"] = lane_state["status"]
                if lane_state["status"] in TERMINAL_STATUSES:
                    matching_attempt["completed_at"] = (
                        matching_attempt.get("completed_at") or now_iso()
                    )
                changes["lane_attempts"] = attempts
                changes["active_lane_attempt_id"] = matching_attempt["attempt_id"]
                if lane_state["status"] in TERMINAL_STATUSES:
                    record_event = previous_child_status not in TERMINAL_STATUSES
                    child_status = lane_state["status"]
                    flow_scope = lane_state.get("flow_scope")
                    delivery_gate = lane_state.get("delivery_gate")
                    release_degraded = (
                        isinstance(release_integrity, dict)
                        and release_integrity.get("status") == "degraded"
                    )
                    equivalence_errors: list[str] = []
                    equivalence_mode = _mapped_equivalence_mode(state)
                    if (
                        equivalence_mode in {"off", "required"}
                        and state.get("mapped_equivalence_mode") != equivalence_mode
                    ):
                        changes["mapped_equivalence_mode"] = equivalence_mode
                    if (
                        equivalence_mode == "required"
                        and child_status in {"completed", "incomplete"}
                    ):
                        equivalence_errors = _mapped_equivalence_evidence_errors(
                            lane_state,
                            expected_run_id=str(lane_run_id),
                            expected_pdk=str(state.get("pdk") or ""),
                            expected_design_config=state.get("design_config"),
                        )
                    elif equivalence_mode not in {"off", "required"}:
                        equivalence_errors = [
                            "mapped-equivalence mode cannot be validated from the frozen spec"
                        ]
                    if equivalence_errors:
                        lane_state["delivery_gate"] = "incomplete"
                        raw_reasons = lane_state.get("delivery_gate_reasons") or []
                        reasons = (
                            list(raw_reasons)
                            if isinstance(raw_reasons, list)
                            else [str(raw_reasons)]
                        )
                        reasons.extend(
                            f"mapped_equivalence: {reason}"
                            for reason in equivalence_errors
                            if f"mapped_equivalence: {reason}" not in reasons
                        )
                        lane_state["delivery_gate_reasons"] = reasons
                        delivery_gate = "incomplete"
                    if release_degraded:
                        integrity_errors = [
                            str(value)
                            for value in release_integrity.get("errors") or []
                        ]
                        status = "incomplete"
                        phase = "lane_release_integrity_incomplete"
                        record_event = True
                        changes.update(
                            {
                                "error": (
                                    "sealed Lane delivery evidence changed after "
                                    "completion: "
                                    + "; ".join(integrity_errors[:8])
                                ),
                                "failure_class": "release_integrity",
                                "failure_gate": "lane_delivery_attestation",
                                "failure_code": (
                                    "release_integrity/lane_artifact_changed"
                                ),
                            }
                        )
                    elif child_status == "failed":
                        status = "failed"
                        phase = "failed"
                        changes["error"] = (
                            "LibreLane flow failed; inspect child state and logs"
                        )
                    elif child_status == "stopped":
                        status = "stopped"
                        phase = "stopped"
                    elif (
                        child_status == "completed"
                        and flow_scope == "full"
                        and delivery_gate == "pass"
                    ):
                        status = "completed"
                        phase = "completed"
                        changes["error"] = None
                    else:
                        status = "incomplete"
                        phase = (
                            "lane_partial_completed"
                            if flow_scope == "partial"
                            else "lane_delivery_incomplete"
                        )
                        changes["error"] = None
                elif completed_parent:
                    status = "incomplete"
                    phase = "lane_delivery_incomplete"
                    record_event = True
                    changes.update(
                        {
                            "error": "completed child delivery became non-terminal during re-attestation",
                            "failure_class": "release_integrity",
                            "failure_gate": "lane_delivery",
                            "failure_code": "release_integrity/lane_delivery_changed",
                        }
                    )
            except LaneInputIdentityError as exc:
                status = "failed"
                phase = "failed"
                record_event = True
                changes.update(
                    {
                        "error": str(exc),
                        "lane_status_error": str(exc),
                        "failure_class": "release_integrity",
                        "failure_gate": "lane_input_binding",
                        "failure_code": "release_integrity/lane_input_mismatch",
                        # Basic child identity is validated before input binding.
                        # Preserve the observed state so an active mismatched child
                        # can be stopped without weakening the release gate.
                        "lane_state": lane_payload,
                    }
                )
            except (json.JSONDecodeError, ValueError) as exc:
                changes["lane_status_error"] = f"lane-agent returned invalid status JSON: {exc}"
                record_event = state.get("lane_status_error") != changes[
                    "lane_status_error"
                ]
                phase = "lane_status_unknown"
        elif completed is not None:
            changes["lane_status_error"] = "lane-agent returned an empty status response"
            record_event = state.get("lane_status_error") != changes[
                "lane_status_error"
            ]
            phase = "lane_status_unknown"
    elif state["status"] == "running" and not _worker_alive(state):
        status = "failed"
        phase = "failed"
        record_event = True
        changes["error"] = state.get("error") or (
            "pipeline worker exited without starting LibreLane"
        )
    if completed_parent and not reconcile_succeeded and status is None:
        # Preserve the previously attested snapshot, but make the failed audit
        # visible to the caller so an upper layer can retry it explicitly.
        detail = str(
            changes.get("lane_status_error")
            or "lane-agent did not return an authenticated child state"
        )
        raise LaneReconciliationError(
            f"completed Lane delivery could not be re-attested: {detail}"
        )
    effective_status = status or state["status"]
    if effective_status == "running" or "elapsed_seconds" not in state:
        changes["elapsed_seconds"] = max(
            0, round(time.time() - float(state["started_epoch"]))
        )
    changes = {key: value for key, value in changes.items() if state.get(key) != value}
    if phase == state.get("phase"):
        phase = None
    if status == state.get("status"):
        status = None
    elif status is not None:
        record_event = True
    if phase is None and status is None and not changes:
        return state
    return _update(
        state,
        phase=phase,
        status=status,
        announce=False,
        record_event=record_event,
        allow_terminal_transition=completed_parent,
        **changes,
    )


def refresh_state(
    state: dict[str, Any], *, integrity_audit_id: str | None = None
) -> dict[str, Any]:
    """Reconcile one run under its operation lock."""
    if not state.get("state_file"):
        return _refresh_state_unlocked(
            state, integrity_audit_id=integrity_audit_id
        )
    state_file = Path(state["state_file"])
    with _operation_lock(state_file):
        try:
            current = json.loads(state_file.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            current = dict(state)
        state.clear()
        state.update(current)
        if state.get("status") == "created" and state.get("phase") == "worker_launching":
            return _recover_created_worker(state)
        return _refresh_state_unlocked(
            state, integrity_audit_id=integrity_audit_id
        )


def retry_lane(
    run_id: str | None,
    *,
    idempotency_key: str | None = None,
) -> dict[str, Any]:
    if idempotency_key is not None and (
        not isinstance(idempotency_key, str)
        or not re.fullmatch(r"[A-Za-z0-9_.:@/-]{1,256}", idempotency_key)
    ):
        raise ValueError("idempotency_key contains unsupported characters")
    initial = load_state(run_id)
    with _operation_lock(Path(initial["state_file"])):
        state = _refresh_state_unlocked(load_state(initial["run_id"]))
        requests = [
            dict(row)
            for row in state.get("lane_retry_requests", [])
            if isinstance(row, dict)
        ]
        existing_request = next(
            (
                row
                for row in reversed(requests)
                if idempotency_key is not None
                and row.get("idempotency_key") == idempotency_key
            ),
            None,
        )
        if existing_request is not None:
            request_status = existing_request.get("status")
            if request_status in {"completed", "failed"}:
                return state
            if _lane_is_active_or_unknown(state):
                existing_request.update(
                    {
                        "status": "completed",
                        "completed_at": now_iso(),
                        "attempt_id": state.get("active_lane_attempt_id"),
                        "lane_run_id": state.get("lane_run_id"),
                    }
                )
                return _update(
                    state,
                    lane_retry_requests=requests,
                    announce=False,
                    record_event=False,
                )
        if state.get("status") == "completed":
            raise RuntimeError("a completed RTL-Closure run is immutable")
        if state.get("status") == "running" and _worker_alive(state):
            raise RuntimeError("the RTL-Closure pipeline worker is still running")
        if existing_request is None and _lane_is_active_or_unknown(state):
            raise RuntimeError("the child LibreLane flow is still active or its state is unknown")
        if not Path(state.get("design_config", "")).is_file():
            raise RuntimeError("this run has no validated, published LibreLane design")
        spec = _load_frozen_spec(state)
        if existing_request is None and idempotency_key is not None:
            existing_request = {
                "idempotency_key": idempotency_key,
                "status": "launching",
                "requested_at": now_iso(),
                "completed_at": None,
                "attempt_id": None,
                "lane_run_id": None,
                "error": None,
            }
            requests.append(existing_request)
            requests = requests[-32:]
        elif existing_request is not None:
            existing_request.update(
                {"status": "launching", "completed_at": None, "error": None}
            )
        state = _update(
            state,
            phase="restarting_lane",
            status="running",
            allow_terminal_transition=True,
            lane_retry_requests=requests,
            active_lane_retry_key=idempotency_key,
            error=None,
            failure_class=None,
            failure_gate=None,
            failure_code=None,
        )
        try:
            state = _start_lane_unlocked(spec, state)
            if existing_request is None:
                return state
            existing_request.update(
                {
                    "status": "completed",
                    "completed_at": now_iso(),
                    "attempt_id": state.get("active_lane_attempt_id"),
                    "lane_run_id": state.get("lane_run_id"),
                }
            )
            return _update(
                state,
                lane_retry_requests=requests,
                announce=False,
                record_event=False,
            )
        except Exception as exc:
            if existing_request is not None:
                attempts = state.get("lane_attempts") or []
                launch_unknown = bool(
                    attempts
                    and isinstance(attempts[-1], dict)
                    and attempts[-1].get("status") == "launch_unknown"
                )
                existing_request.update(
                    {
                        "status": "launch_unknown" if launch_unknown else "failed",
                        "completed_at": now_iso(),
                        "attempt_id": state.get("active_lane_attempt_id"),
                        "lane_run_id": state.get("lane_run_id"),
                        "error": str(exc),
                    }
                )
            _update(
                state,
                phase="failed",
                status="failed",
                error=str(exc),
                lane_retry_requests=requests,
                failure_class=getattr(exc, "failure_class", "infrastructure"),
                failure_gate=getattr(exc, "failure_gate", "lane_launch"),
                failure_code=getattr(
                    exc, "failure_code", "infrastructure/lane_launch"
                ),
                elapsed_seconds=max(
                    0, round(time.time() - float(state["started_epoch"]))
                ),
            )
            raise


def stop_pipeline(run_id: str | None) -> dict[str, Any]:
    initial = load_state(run_id)
    with _operation_lock(Path(initial["state_file"])):
        state = load_state(initial["run_id"])
        publication: dict[str, Any] | None = None
        publication_error: str | None = None
        if state.get("publication_bundle_sha256") is not None:
            try:
                publication = _verify_publication(state)
            except RuntimeError as exc:
                # Publication integrity remains a release failure, but it must
                # not prevent resource cleanup of an already identified child.
                publication_error = str(exc)[:4096]
        if publication_error is None:
            state = _refresh_state_unlocked(state)
        terminal_input_mismatch = bool(
            state.get("status") in TERMINAL_STATUSES
            and state.get("failure_code")
            == "release_integrity/lane_input_mismatch"
            and _lane_is_active_or_unknown(state)
        )
        publication_cleanup = bool(
            publication_error is not None and _lane_is_active_or_unknown(state)
        )
        preserve_integrity_failure = terminal_input_mismatch or publication_cleanup
        if state.get("status") in TERMINAL_STATUSES and not preserve_integrity_failure:
            if publication_error is not None and state.get("status") == "completed":
                return _degrade_completed_publication(state, publication_error)
            return state

        lane_run_id = state.get("lane_run_id")
        if lane_run_id:
            try:
                stopped = subprocess.run(
                    [
                        "lane-agent",
                        "stop",
                        "--run-id",
                        str(lane_run_id),
                        "--json",
                    ],
                    capture_output=True,
                    text=True,
                    timeout=20,
                    check=False,
                )
            except (OSError, subprocess.TimeoutExpired) as exc:
                raise RuntimeError(f"could not stop LibreLane: {exc}") from exc
            if stopped.returncode != 0:
                message = (
                    stopped.stderr or stopped.stdout or "lane-agent stop failed"
                ).strip()
                raise RuntimeError(f"could not stop LibreLane: {message}")
            origin = state.get("origin") or {}
            validate_expected_inputs = not preserve_integrity_failure
            try:
                child = _validate_lane_state_identity(
                    json.loads(stopped.stdout),
                    expected_run_id=str(lane_run_id),
                    expected_workflow_id=(
                        str(origin["workflow_id"])
                        if origin.get("workflow_id") is not None
                        else None
                    ),
                    expected_attempt_id=(
                        str(state["active_lane_attempt_id"])
                        if state.get("active_lane_attempt_id") is not None
                        else None
                    ),
                    expected_idempotency_key=_active_lane_launch_key(state),
                    expected_input_manifest=(
                        publication["lane_input_manifest"]
                        if publication is not None and validate_expected_inputs
                        else None
                    ),
                    expected_input_bundle_sha256=(
                        publication["lane_input_bundle_sha256"]
                        if publication is not None and validate_expected_inputs
                        else None
                    ),
                )
            except (json.JSONDecodeError, ValueError) as exc:
                raise RuntimeError(
                    f"lane-agent returned an invalid stop response: {exc}"
                ) from exc
            if child["status"] != "stopped":
                if child["status"] == "running":
                    raise RuntimeError(
                        "LibreLane stop was not confirmed by the child controller"
                    )
                # A terminal result can win the race with stop. Persist the
                # observation, then let the normal delivery reconciler classify it.
                integrity_values: dict[str, Any] = {}
                if publication_error is not None:
                    integrity_values = {
                        "status": "incomplete",
                        "phase": "release_integrity_incomplete",
                        "error": "published release evidence changed: "
                        + publication_error,
                        "failure_class": "release_integrity",
                        "failure_gate": "publication_manifest",
                        "failure_code": "release_integrity/publication_invalid",
                    }
                observed = _update(
                    state,
                    lane_state=child,
                    announce=False,
                    record_event=publication_error is not None,
                    allow_terminal_transition=(
                        preserve_integrity_failure
                        and state.get("status") in TERMINAL_STATUSES
                    ),
                    **integrity_values,
                )
                return (
                    observed
                    if preserve_integrity_failure
                    else _refresh_state_unlocked(observed)
                )

            attempts = [
                dict(row)
                for row in state.get("lane_attempts", [])
                if isinstance(row, dict)
            ]
            for attempt in reversed(attempts):
                if attempt.get("lane_run_id") == lane_run_id:
                    attempt["status"] = "stopped"
                    attempt["completed_at"] = (
                        attempt.get("completed_at") or now_iso()
                    )
                    break
            integrity_values = {}
            if publication_error is not None:
                integrity_values = {
                    "status": "incomplete",
                    "phase": "release_integrity_incomplete",
                    "error": "published release evidence changed: "
                    + publication_error,
                    "failure_class": "release_integrity",
                    "failure_gate": "publication_manifest",
                    "failure_code": "release_integrity/publication_invalid",
                }
            elif terminal_input_mismatch:
                integrity_values = {
                    "status": state["status"],
                    "phase": state["phase"],
                }
            else:
                integrity_values = {"status": "stopped", "phase": "stopped"}
            return _update(
                state,
                lane_state=child,
                lane_attempts=attempts,
                stop_requested=True,
                allow_terminal_transition=(
                    preserve_integrity_failure
                    and state.get("status") in TERMINAL_STATUSES
                ),
                announce=not preserve_integrity_failure,
                record_event=(
                    not preserve_integrity_failure or publication_error is not None
                ),
                elapsed_seconds=max(
                    0, round(time.time() - float(state["started_epoch"]))
                ),
                **integrity_values,
            )

        identity = state.get("worker_process")
        if not isinstance(identity, dict) or not _worker_alive(
            state, require_identity=True
        ):
            raise RuntimeError(
                "pipeline worker cannot be stopped safely because its process "
                "identity is unavailable"
            )
        try:
            process_group = os.getpgid(int(state["pid"]))
        except OSError as exc:
            raise RuntimeError("pipeline worker process group is unavailable") from exc
        if process_group != int(state["pid"]):
            raise RuntimeError("pipeline worker is not the expected session leader")
        try:
            os.killpg(int(state["pid"]), signal.SIGTERM)
        except ProcessLookupError:
            pass
        deadline = time.monotonic() + 2
        while time.monotonic() < deadline and _worker_alive(
            state, require_identity=True
        ):
            time.sleep(0.05)
        if _worker_alive(state, require_identity=True):
            return _update(
                state,
                phase="stopping",
                stop_requested=True,
                error="pipeline worker did not stop before the confirmation deadline",
            )
        return _update(
            state,
            status="stopped",
            phase="stopped",
            stop_requested=True,
            elapsed_seconds=max(
                0, round(time.time() - float(state["started_epoch"]))
            ),
        )


def _tail_lines(path: Path, count: int, *, max_bytes: int) -> list[str]:
    if count < 1 or max_bytes < 1 or not path.is_file():
        return []
    try:
        with path.open("rb") as handle:
            handle.seek(0, os.SEEK_END)
            end = handle.tell()
            start = max(0, end - max_bytes)
            handle.seek(start)
            data = handle.read(max_bytes)
    except OSError:
        return []
    if start > 0:
        newline = data.find(b"\n")
        data = data[newline + 1 :] if newline >= 0 else b""
    return data.decode("utf-8", errors="replace").splitlines()[-count:]


def tail(path: Path, count: int) -> str:
    if not 1 <= count <= 10_000:
        raise ValueError("line count must be from 1 to 10000")
    return "\n".join(_tail_lines(path, count, max_bytes=1024 * 1024))


def _recent_events(path: Path, count: int = 256) -> list[dict[str, Any]]:
    if not 1 <= count <= 256:
        raise ValueError("event count must be from 1 to 256")
    rows: list[dict[str, Any]] = []
    for line in _tail_lines(path, count, max_bytes=2 * 1024 * 1024):
        try:
            value = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(value, dict):
            rows.append(value)
    return rows


def build_report(
    state: dict[str, Any], *, log_lines: int = 20, compact: bool = False
) -> dict[str, Any]:
    if not 1 <= log_lines <= 200:
        raise ValueError("log_lines must be from 1 to 200")
    child = state.get("lane_state") or {}
    errors: list[str] = []
    for value in (state.get("error"), state.get("lane_status_error")):
        if value:
            errors.append(str(value))
    child_errors = child.get("errors") or []
    if not isinstance(child_errors, list):
        child_errors = [child_errors]
    for value in child_errors:
        rendered = value if isinstance(value, str) else json.dumps(value, sort_keys=True)
        if rendered and rendered not in errors:
            errors.append(rendered)

    failed = state.get("status") == "failed" or bool(errors)
    diagnostic_tail = (
        tail(Path(state["pipeline_log"]), log_lines).splitlines() if failed else []
    )
    current_step = child.get("active_step")
    last_completed_step = child.get("last_completed_step")
    component = "librelane" if state.get("lane_run_id") else "rtl_closure"
    local_artifacts = dict(state.get("artifacts") or {})
    local_artifacts.update(child.get("artifacts") or {})
    verification = state.get("verification")
    repair = state.get("repair")
    contract = state.get("contract")
    oracle = state.get("oracle")
    model_request = state.get("model_request")
    coverage = state.get("coverage")
    architecture_audit = state.get("architecture_audit")
    synthesis_equivalence = state.get("synthesis_equivalence")
    mapped_equivalence = child.get("mapped_equivalence")
    release_integrity = child.get("release_integrity")
    delivery_attestation = child.get("delivery_attestation")
    delivery_attestation_sha256 = (
        delivery_attestation.get("attestation_sha256")
        if isinstance(delivery_attestation, dict)
        else None
    )
    assurance = child.get("assurance")
    delivery_gate_reasons = child.get("delivery_gate_reasons") or []
    qor = child.get("qor")
    lane_attempts = state.get("lane_attempts") or []
    event_count = 16 if compact else 256
    if compact:
        def selected(value: Any, keys: tuple[str, ...]) -> Any:
            if not isinstance(value, dict):
                return value
            return {key: value.get(key) for key in keys if key in value}

        model_request = selected(
            model_request,
            (
                "event",
                "request_id",
                "candidate_index",
                "candidate_count",
                "attempt",
                "max_attempts",
                "active_request_count",
                "elapsed_seconds",
                "first_event_seconds",
                "chunk_count",
                "http_status",
                "ray_id",
                "error",
            ),
        )
        coverage = selected(
            coverage,
            (
                "status",
                "mode",
                "metric",
                "round",
                "max_rounds",
                "coverage_percent",
                "coverage_target_pct",
                "target_met",
                "remaining_cycle_budget",
                "report",
                "error",
            ),
        )
        architecture_audit = selected(
            architecture_audit, ("status", "failed_checks", "artifact")
        )
        synthesis_equivalence = selected(
            synthesis_equivalence,
            ("status", "equivalence_cells", "proven_cells", "artifact"),
        )
        mapped_equivalence = selected(
            mapped_equivalence,
            ("requested", "status", "gate", "scope", "artifact", "reasons"),
        )
        assurance = selected(
            assurance,
            (
                "pdk",
                "open_pdk",
                "foundry_signoff",
                "tapeout_guarantee",
                "limitations",
            ),
        )
        release_integrity = selected(
            release_integrity,
            (
                "schema_version",
                "audit_id",
                "status",
                "checked_at",
                "attestation_sha256",
                "errors",
            ),
        )
        if isinstance(release_integrity, dict):
            release_integrity["errors"] = [
                str(value)[:2048]
                for value in (release_integrity.get("errors") or [])[:16]
            ]
        if isinstance(qor, dict):
            qor = {
                key: value
                for key, value in list(qor.items())[:128]
                if isinstance(key, str)
                and isinstance(value, (str, int, float, bool, type(None)))
            }
        if isinstance(local_artifacts, dict):
            local_artifacts = {
                str(key): str(value)[:4096]
                for key, value in list(local_artifacts.items())[:128]
            }
        errors = [str(value)[:4096] for value in errors[:16]]
        diagnostic_tail = [str(value)[:4096] for value in diagnostic_tail[-20:]]
        delivery_gate_reasons = [
            str(value)[:2048] for value in delivery_gate_reasons[:32]
        ]
        if isinstance(verification, dict):
            verification = {
                "passed": verification.get("passed"),
                "rtl_sha256": verification.get("rtl_sha256"),
                "suite_sha256": verification.get("suite_sha256"),
                "formal": {
                    "status": (verification.get("formal") or {}).get("status"),
                    "property_count": (verification.get("formal") or {}).get(
                        "property_count"
                    ),
                    "evidence_complete": (verification.get("formal") or {}).get(
                        "evidence_complete"
                    ),
                },
                "universal_properties": {
                    "status": (
                        verification.get("universal_properties") or {}
                    ).get("status"),
                    "property_count": (
                        verification.get("universal_properties") or {}
                    ).get("property_count"),
                    "passed_property_count": (
                        verification.get("universal_properties") or {}
                    ).get("passed_property_count"),
                },
            }
        if isinstance(repair, dict):
            repair = {
                key: repair.get(key)
                for key in (
                    "round",
                    "max_rounds",
                    "candidate",
                    "candidate_count",
                    "action",
                    "result",
                    "failure_class",
                    "reason",
                    "stagnation",
                )
                if key in repair
            }
        contract = None
        oracle = None
        lane_attempts = (
            [
                selected(
                    row,
                    (
                        "attempt_id",
                        "lane_run_id",
                        "status",
                        "started_at",
                        "completed_at",
                        "supersedes",
                        "launch_key",
                        "error",
                    ),
                )
                for row in lane_attempts[-8:]
            ]
            if isinstance(lane_attempts, list)
            else []
        )
    events = _recent_events(Path(state.get("events_file", "")), count=event_count)
    if compact:
        compact_events: list[dict[str, Any]] = []
        for row in events:
            summary = selected(
                row,
                (
                    "schema_version",
                    "event_id",
                    "run_id",
                    "workflow_id",
                    "lane_run_id",
                    "lane_attempt_id",
                    "sequence",
                    "state_revision",
                    "timestamp",
                    "type",
                    "terminal",
                    "status",
                    "phase",
                    "model_request",
                    "repair",
                    "coverage",
                    "verification",
                    "architecture_audit",
                    "synthesis_equivalence",
                    "evidence",
                    "error",
                    "failure",
                    "failure_class",
                    "failure_gate",
                    "failure_code",
                ),
            )
            if not isinstance(summary, dict):
                continue
            summary["model_request"] = selected(
                summary.get("model_request"),
                (
                    "event",
                    "request_id",
                    "candidate_index",
                    "candidate_count",
                    "attempt",
                    "active_request_count",
                    "elapsed_seconds",
                    "http_status",
                    "error",
                ),
            )
            summary["repair"] = selected(
                summary.get("repair"),
                (
                    "round",
                    "max_rounds",
                    "candidate",
                    "candidate_count",
                    "action",
                    "result",
                    "failure_class",
                    "reason",
                    "stagnation",
                ),
            )
            summary["coverage"] = selected(
                summary.get("coverage"),
                (
                    "status",
                    "round",
                    "max_rounds",
                    "coverage_percent",
                    "coverage_target_pct",
                    "target_met",
                    "remaining_cycle_budget",
                    "error",
                ),
            )
            summary["verification"] = selected(
                summary.get("verification"),
                (
                    "passed",
                    "suite_sha256",
                    "rtl_sha256",
                    "formal_status",
                    "universal_status",
                    "verilator_status",
                ),
            )
            summary["architecture_audit"] = selected(
                summary.get("architecture_audit"),
                ("status", "failed_checks", "artifact"),
            )
            summary["synthesis_equivalence"] = selected(
                summary.get("synthesis_equivalence"),
                ("status", "equivalence_cells", "proven_cells", "artifact"),
            )
            if summary.get("error") is not None:
                summary["error"] = str(summary["error"])[:4096]
            compact_events.append(summary)
        events = compact_events
    return {
        "schema_version": 1,
        "compact": compact,
        "run_id": state["run_id"],
        "state_file": state.get("state_file"),
        "design": state.get("design_slug") or state["design_name"],
        "pdk": state["pdk"],
        "status": state["status"],
        "component": component,
        "phase": state["phase"],
        "current_step": current_step or (state["phase"] if component == "rtl_closure" else None),
        "last_completed_step": last_completed_step,
        "completed_step_count": child.get("completed_step_count", 0),
        "started_step_count": child.get("started_step_count", 0),
        "elapsed_seconds": state.get("elapsed_seconds", 0),
        "model_request": model_request,
        "rtl_closure": state.get("rtl_closure"),
        "contract": contract,
        "oracle": oracle,
        "oracle_source_configuration": state.get("oracle_source_configuration"),
        "coverage": coverage,
        "verification": verification,
        "repair": repair,
        "architecture_audit": architecture_audit,
        "synthesis_equivalence": synthesis_equivalence,
        "mapped_equivalence_mode": _mapped_equivalence_mode(state),
        "mapped_equivalence": mapped_equivalence,
        "release_integrity": release_integrity,
        "delivery_attestation_sha256": delivery_attestation_sha256,
        "last_integrity_audit_id": state.get("last_integrity_audit_id"),
        "assurance": assurance,
        "lane_run_id": state.get("lane_run_id"),
        "active_lane_attempt_id": state.get("active_lane_attempt_id"),
        "lane_attempts": lane_attempts,
        "origin": state.get("origin"),
        "process_status": child.get("process_status"),
        "flow_scope": child.get("flow_scope"),
        "delivery_gate": child.get("delivery_gate"),
        "delivery_gate_reasons": delivery_gate_reasons,
        "expected_final_step": child.get("expected_final_step"),
        "endpoint_reached": child.get("endpoint_reached"),
        "status_detail": child.get("status_detail"),
        "metrics_path": child.get("metrics_path"),
        "qor": qor,
        "artifacts": local_artifacts,
        "design_config": state.get("design_config"),
        "publication_manifest": state.get("publication_manifest"),
        "publication_bundle_sha256": state.get("publication_bundle_sha256"),
        "lane_input_bundle_sha256": state.get("lane_input_bundle_sha256"),
        "sandbox_image_identity": state.get("sandbox_image_identity"),
        "verify_image_identity": state.get("verify_image_identity"),
        "failure_class": state.get("failure_class"),
        "failure_gate": state.get("failure_gate"),
        "failure_code": state.get("failure_code"),
        "failure": _public_failure(state.get("failure")),
        "errors": errors,
        "diagnostic_log_tail": diagnostic_tail,
        "pipeline_log": state["pipeline_log"],
        "events_file": state.get("events_file"),
        "events": events,
    }


def report_state(
    run_id: str | None = None, *, log_lines: int = 20, compact: bool = False
) -> dict[str, Any]:
    return build_report(load_state(run_id), log_lines=log_lines, compact=compact)


def reconcile_state(
    run_id: str | None = None, *, integrity_audit_id: str | None = None
) -> dict[str, Any]:
    """Advance child-derived state explicitly; status/report remain pure reads."""
    if integrity_audit_id is not None and (
        not isinstance(integrity_audit_id, str)
        or not re.fullmatch(
            r"[A-Za-z0-9][A-Za-z0-9_.:/-]{0,254}", integrity_audit_id
        )
    ):
        raise ValueError("integrity audit id is malformed")
    return refresh_state(
        load_state(run_id), integrity_audit_id=integrity_audit_id
    )


def format_report(report: dict[str, Any]) -> str:
    lines = [
        f"run_id: {report['run_id']}",
        f"state_file: {report.get('state_file') or '-'}",
        f"design: {report['pdk']}/{report['design']}",
        f"status: {report['status']}",
        f"process_status: {report.get('process_status') or '-'}",
        f"flow_scope: {report.get('flow_scope') or '-'}",
        f"delivery_gate: {report.get('delivery_gate') or '-'}",
        f"mapped_equivalence_mode: {report.get('mapped_equivalence_mode', 'off')}",
        f"component: {report['component']}",
        f"phase: {report['phase']}",
        f"current_step: {report.get('current_step') or '-'}",
        f"last_completed_step: {report.get('last_completed_step') or '-'}",
        f"completed_steps: {report['completed_step_count']}",
        f"elapsed_seconds: {report['elapsed_seconds']}",
    ]
    if report.get("failure_class"):
        lines.append(f"failure_class: {report['failure_class']}")
    if report.get("failure_gate"):
        lines.append(f"failure_gate: {report['failure_gate']}")
    if report.get("failure_code"):
        lines.append(f"failure_code: {report['failure_code']}")
    release_integrity = report.get("release_integrity")
    if isinstance(release_integrity, dict):
        lines.append(
            "release_integrity: "
            f"{release_integrity.get('status', 'unknown')} "
            f"(audit={release_integrity.get('audit_id') or '-'})"
        )
    if report.get("delivery_attestation_sha256"):
        lines.append(
            "delivery_attestation_sha256: "
            + str(report["delivery_attestation_sha256"])
        )
    failure = report.get("failure")
    if isinstance(failure, dict):
        if isinstance(failure.get("retryable"), bool):
            lines.append(f"failure_retryable: {failure['retryable']}")
        if isinstance(failure.get("repairable_by_rtl"), bool):
            lines.append(
                f"failure_repairable_by_rtl: {failure['repairable_by_rtl']}"
            )
    for reason in report.get("delivery_gate_reasons") or []:
        lines.append(f"delivery_reason: {reason}")
    model_request = report.get("model_request") or {}
    if model_request:
        lines.append(
            "model_request: "
            f"event={model_request.get('event', 'unknown')} "
            f"candidate={model_request.get('candidate_index', '?')}/"
            f"{model_request.get('candidate_count', '?')} "
            f"attempt={model_request.get('attempt', '?')}/"
            f"{model_request.get('max_attempts', '?')} "
            f"active={model_request.get('active_request_count', 0)}"
        )
        if model_request.get("elapsed_seconds") is not None:
            lines.append(
                f"model_request_elapsed_seconds: {model_request['elapsed_seconds']}"
            )
        if model_request.get("http_status") is not None:
            lines.append(f"model_request_http_status: {model_request['http_status']}")
        if model_request.get("ray_id"):
            lines.append(f"model_request_ray_id: {model_request['ray_id']}")
    qor = report.get("qor") or {}
    if qor:
        lines.append(f"qor_gate: {qor.get('gate', 'unknown')}")
    oracle = report.get("oracle") or {}
    source_configuration = report.get("oracle_source_configuration") or {}
    if source_configuration:
        lines.append(
            "oracle_configured_sources: "
            f"{source_configuration.get('distinct_source_count', 0)}"
        )
    diversity = oracle.get("diversity") or {}
    if diversity:
        lines.append(
            "oracle_diversity: "
            f"mode={diversity.get('mode', 'advisory')} "
            f"sources={diversity.get('distinct_source_count', 0)}/"
            f"{diversity.get('minimum_distinct_sources', '?')} "
            f"satisfied={diversity.get('satisfied')}"
        )
        if diversity.get("warning"):
            lines.append(f"oracle_diversity_warning: {diversity['warning']}")
    contract = report.get("contract") or {}
    intent_closure = contract.get("intent_closure") or {}
    if intent_closure:
        lines.append(
            "intent_closure: "
            f"mode={intent_closure.get('mode', 'advisory')} "
            f"confirmed={intent_closure.get('user_confirmed')} "
            f"satisfied={intent_closure.get('closure_satisfied')} "
            f"unresolved={len(intent_closure.get('unresolved_questions') or [])}"
        )
    verification = report.get("verification") or {}
    if verification:
        lines.append(f"verification_passed: {verification.get('passed')}")
        lines.append(f"verification_checks: {verification.get('total_checks', 0)}")
        lines.append(f"verification_mismatches: {verification.get('mismatched', 0)}")
        formal = verification.get("formal") or {}
        if formal:
            lines.append(f"formal_status: {formal.get('status', 'unknown')}")
            lines.append(f"formal_properties: {formal.get('property_count', 0)}")
        universal = verification.get("universal_properties") or {}
        if universal:
            lines.append(
                "universal_properties: "
                f"{universal.get('status', 'unknown')} "
                f"({universal.get('passed_property_count', 0)}/"
                f"{universal.get('property_count', 0)} proven)"
            )
            lines.append(
                "universal_complete_behavioral_proof: "
                f"{universal.get('complete_behavioral_proof', False)}"
            )
            if universal.get("failed_property_ids"):
                lines.append(
                    "universal_failed_properties: "
                    + ", ".join(universal["failed_property_ids"])
                )
            proof_scope = universal.get("scope") or {}
            capabilities = proof_scope.get("capabilities") or {}
            if proof_scope:
                lines.append(
                    f"universal_scope: {proof_scope.get('claim', 'unknown')}"
                )
                lines.append(
                    "universal_supports_liveness: "
                    f"{capabilities.get('liveness_properties', False)}"
                )
                lines.append(
                    "universal_supports_internal_state: "
                    f"{capabilities.get('internal_state_references', False)}"
                )
                limitation_codes = [
                    row.get("code")
                    for row in proof_scope.get("limitations") or []
                    if isinstance(row, dict) and row.get("code")
                ]
                if limitation_codes:
                    lines.append(
                        "universal_limitations: " + ", ".join(limitation_codes)
                    )
            requirement_coverage = universal.get("requirement_coverage") or {}
            if requirement_coverage:
                lines.append(
                    "universal_requirement_mapping: "
                    f"{len(requirement_coverage.get('covered_requirement_ids') or [])}/"
                    f"{len(requirement_coverage.get('declared_requirement_ids') or [])}"
                )
                lines.append(
                    "universal_semantic_completeness_claimed: "
                    f"{requirement_coverage.get('semantic_completeness_claimed', False)}"
                )
                if requirement_coverage.get("uncovered_requirement_ids"):
                    lines.append(
                        "universal_uncovered_requirements: "
                        + ", ".join(
                            requirement_coverage["uncovered_requirement_ids"]
                        )
                    )
    coverage = report.get("coverage") or {}
    if coverage:
        lines.append(f"oracle_coverage_mode: {coverage.get('mode', 'advisory')}")
        lines.append(
            f"oracle_coverage_metric: {coverage.get('metric', 'python_oracle_executable_line_proxy')}"
        )
        lines.append(
            "oracle_coverage_is_rtl_functional: "
            f"{coverage.get('rtl_functional_coverage', False)}"
        )
        lines.append(
            f"oracle_coverage: {coverage.get('coverage_percent', '?')}%/"
            f"{coverage.get('coverage_target_pct', '?')}%"
        )
        lines.append(f"oracle_coverage_status: {coverage.get('status', 'unknown')}")
        lines.append(f"oracle_coverage_target_met: {coverage.get('target_met')}")
        lines.append(
            "oracle_coverage_remaining_cycle_budget: "
            f"{coverage.get('remaining_cycle_budget', '?')}"
        )
        if coverage.get("warning"):
            lines.append(f"oracle_coverage_warning: {coverage['warning']}")
        if coverage.get("error"):
            lines.append(f"oracle_coverage_error: {coverage['error']}")
    repair = report.get("repair") or {}
    if repair:
        lines.append(
            "repair_progress: "
            f"round={repair.get('round', 0)}/{repair.get('max_rounds', 0)} "
            f"candidate={repair.get('candidate', 0)}/{repair.get('candidate_count', 0)}"
        )
        if repair.get("failure_class"):
            lines.append(f"repair_failure_class: {repair['failure_class']}")
        if repair.get("first_failure_cycle") is not None:
            lines.append(f"repair_first_failure_cycle: {repair['first_failure_cycle']}")
    architecture_audit = report.get("architecture_audit") or {}
    if architecture_audit:
        lines.append(
            f"architecture_audit_status: {architecture_audit.get('status', 'unknown')}"
        )
        if architecture_audit.get("failed_checks"):
            lines.append(
                "architecture_failed_checks: "
                + ", ".join(architecture_audit["failed_checks"])
            )
    synthesis_equivalence = report.get("synthesis_equivalence") or {}
    if synthesis_equivalence:
        lines.append(
            "synthesis_equivalence: "
            f"{synthesis_equivalence.get('status', 'unknown')} "
            f"({synthesis_equivalence.get('proven_cells', '?')}/"
            f"{synthesis_equivalence.get('equivalence_cells', '?')} proven)"
        )
    mapped_equivalence = report.get("mapped_equivalence") or {}
    if mapped_equivalence:
        lines.append(
            "mapped_equivalence: "
            f"{mapped_equivalence.get('status', 'unknown')} "
            f"(gate={mapped_equivalence.get('gate', 'unknown')})"
        )
        lines.append(
            f"mapped_equivalence_scope: {mapped_equivalence.get('scope', 'unknown')}"
        )
    assurance = report.get("assurance") or {}
    if assurance:
        lines.append(f"open_pdk: {assurance.get('open_pdk', False)}")
        lines.append(f"foundry_signoff: {assurance.get('foundry_signoff', False)}")
        lines.append(
            f"tapeout_guarantee: {assurance.get('tapeout_guarantee', False)}"
        )
        for limitation in assurance.get("limitations") or []:
            lines.append(f"assurance_limitation: {limitation}")
    errors = report.get("errors") or []
    if errors:
        lines.append("errors:")
        lines.extend(f"- {error}" for error in errors)
    artifacts = report.get("artifacts") or {}
    if artifacts:
        lines.append(f"artifacts: {json.dumps(artifacts, sort_keys=True)}")
    return "\n".join(lines)


def format_state(state: dict[str, Any]) -> str:
    lines = [
        f"run_id: {state['run_id']}",
        f"design: {state['pdk']}/{state.get('design_slug') or state['design_name']}",
        f"status: {state['status']}",
        f"phase: {state['phase']}",
        f"mapped_equivalence_mode: {_mapped_equivalence_mode(state)}",
        f"elapsed_seconds: {state.get('elapsed_seconds', 0)}",
        f"pipeline_log: {state['pipeline_log']}",
    ]
    model_request = state.get("model_request") or {}
    if model_request:
        lines.append(
            "model_request: "
            f"event={model_request.get('event', 'unknown')} "
            f"candidate={model_request.get('candidate_index', '?')}/"
            f"{model_request.get('candidate_count', '?')} "
            f"attempt={model_request.get('attempt', '?')}/"
            f"{model_request.get('max_attempts', '?')} "
            f"active={model_request.get('active_request_count', 0)}"
        )
    if state.get("rtl_closure"):
        lines.append(f"rtl_closure_match_rate: {state['rtl_closure']['best_match_rate']}")
    repair = state.get("repair") or {}
    if repair:
        lines.append(
            "repair: "
            f"round={repair.get('round', 0)}/{repair.get('max_rounds', 0)} "
            f"candidate={repair.get('candidate', 0)}/{repair.get('candidate_count', 0)}"
        )
    verification = state.get("verification") or {}
    if verification:
        lines.append(f"verification_passed: {verification.get('passed')}")
        universal = verification.get("universal_properties") or {}
        if universal:
            lines.append(
                "universal_properties: "
                f"{universal.get('status', 'unknown')} "
                f"({universal.get('passed_property_count', 0)}/"
                f"{universal.get('property_count', 0)} proven)"
            )
            lines.append(
                "universal_complete_behavioral_proof: "
                f"{universal.get('complete_behavioral_proof', False)}"
            )
            proof_scope = universal.get("scope") or {}
            capabilities = proof_scope.get("capabilities") or {}
            if proof_scope:
                lines.append(
                    f"universal_scope: {proof_scope.get('claim', 'unknown')}"
                )
                lines.append(
                    "universal_supports_liveness: "
                    f"{capabilities.get('liveness_properties', False)}"
                )
                lines.append(
                    "universal_supports_internal_state: "
                    f"{capabilities.get('internal_state_references', False)}"
                )
            requirement_coverage = universal.get("requirement_coverage") or {}
            if requirement_coverage:
                lines.append(
                    "universal_requirement_mapping: "
                    f"{len(requirement_coverage.get('covered_requirement_ids') or [])}/"
                    f"{len(requirement_coverage.get('declared_requirement_ids') or [])}"
                )
                if requirement_coverage.get("uncovered_requirement_ids"):
                    lines.append(
                        "universal_uncovered_requirements: "
                        + ", ".join(
                            requirement_coverage["uncovered_requirement_ids"]
                        )
                    )
    architecture_audit = state.get("architecture_audit") or {}
    if architecture_audit:
        lines.append(
            f"architecture_audit: {architecture_audit.get('status', 'unknown')}"
        )
    synthesis_equivalence = state.get("synthesis_equivalence") or {}
    if synthesis_equivalence:
        lines.append(
            "synthesis_equivalence: "
            f"{synthesis_equivalence.get('status', 'unknown')} "
            f"({synthesis_equivalence.get('proven_cells', '?')}/"
            f"{synthesis_equivalence.get('equivalence_cells', '?')} proven)"
        )
    if state.get("design_config"):
        lines.append(f"design_config: {state['design_config']}")
    if state.get("error"):
        if state.get("failure_class"):
            lines.append(f"failure_class: {state['failure_class']}")
        if state.get("failure_gate"):
            lines.append(f"failure_gate: {state['failure_gate']}")
        if state.get("failure_code"):
            lines.append(f"failure_code: {state['failure_code']}")
        lines.append(f"error: {state['error']}")
    child = state.get("lane_state") or {}
    if child:
        lines.extend(
            [
                f"lane_run_id: {child.get('run_id')}",
                f"lane_step: {child.get('active_step') or '-'}",
                f"lane_completed_steps: {child.get('completed_step_count', 0)}",
                f"qor_gate: {(child.get('qor') or {}).get('gate', 'unknown')}",
            ]
        )
        mapped_equivalence = child.get("mapped_equivalence") or {}
        if mapped_equivalence:
            lines.append(
                "mapped_equivalence: "
                f"{mapped_equivalence.get('status', 'unknown')} "
                f"(gate={mapped_equivalence.get('gate', 'unknown')})"
            )
        assurance = child.get("assurance") or {}
        if assurance:
            lines.append(f"open_pdk: {assurance.get('open_pdk', False)}")
            lines.append(
                f"foundry_signoff: {assurance.get('foundry_signoff', False)}"
            )
            lines.append(
                f"tapeout_guarantee: {assurance.get('tapeout_guarantee', False)}"
            )
            for limitation in assurance.get("limitations") or []:
                lines.append(f"assurance_limitation: {limitation}")
    return "\n".join(lines)
