"""Evidence-only assurance statements for LibreLane runs.

This module does not start a flow or infer success from a process exit code.  It
only evaluates already-discovered, filesystem-backed evidence.
"""

from __future__ import annotations

import json
import re
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any, Final

from .integrity import (
    canonical_directory as canonical_fs_directory,
    canonical_regular_file,
    file_sha256,
)


EQY_STEP_ID: Final[str] = "Yosys.EQY"
EQY_PROVIDER: Final[str] = "LibreLane Classic / Yosys.EQY"
EQY_PROVIDER_VERSION: Final[str] = "LibreLane 3.0.5 / EQY 0.62"
EQY_SCOPE: Final[str] = (
    "RTL versus synthesized PDK-mapped netlist equivalence only; "
    "this is not post-layout LEC"
)
OPEN_PDKS: Final[frozenset[str]] = frozenset(
    {"sky130A", "gf180mcuD", "ihp-sg13g2"}
)
OPEN_PDK_LIMITATIONS: Final[tuple[str, ...]] = (
    "Open-source PDK checks are not commercial foundry signoff.",
    "Open-PDK rule decks do not include every foundry-private manufacturing rule.",
    "Passing automated checks does not guarantee tapeout acceptance, "
    "manufacturability, yield, or silicon functionality.",
)

_STEP_DIRECTORY_RE: Final[re.Pattern[str]] = re.compile(r"^(\d+)-.+$")


class _EvidenceError(ValueError):
    """An expected evidence item is absent, malformed, or untrusted."""


def _result(
    *,
    requested: bool,
    status: str,
    gate: str,
    artifact: str | None = None,
    logs: Sequence[str] = (),
    reasons: Sequence[str] = (),
    run_id: str | None = None,
    run_dir: str | None = None,
    step: Mapping[str, Any] | None = None,
    evidence: Mapping[str, Any] | None = None,
    verdict: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    return {
        "schema_version": 2,
        "requested": requested,
        "status": status,
        "gate": gate,
        "provider": EQY_PROVIDER,
        "provider_version": EQY_PROVIDER_VERSION,
        "scope": EQY_SCOPE,
        "run_id": run_id,
        "run_dir": run_dir,
        "step": dict(step) if step is not None else None,
        "evidence": dict(evidence) if evidence is not None else None,
        "verdict": dict(verdict) if verdict is not None else None,
        "artifact": artifact,
        "logs": list(logs),
        "reasons": list(reasons),
    }


def _canonical_directory(raw: object) -> Path:
    if not isinstance(raw, str) or not raw:
        raise _EvidenceError("Yosys.EQY directory is missing or is not a string")
    candidate = Path(raw)
    if not candidate.is_absolute():
        raise _EvidenceError("Yosys.EQY directory must be an absolute path")
    try:
        resolved = candidate.resolve(strict=True)
    except (OSError, RuntimeError, ValueError) as exc:
        raise _EvidenceError(f"Yosys.EQY directory is unavailable: {exc}") from exc
    if candidate != resolved or candidate.is_symlink():
        raise _EvidenceError("Yosys.EQY directory must be canonical and contain no symlinks")
    if not resolved.is_dir():
        raise _EvidenceError("Yosys.EQY directory is not a directory")
    try:
        next(resolved.iterdir(), None)
    except OSError as exc:
        raise _EvidenceError(f"Yosys.EQY directory is not readable: {exc}") from exc
    return resolved


def _canonical_child(
    raw: object,
    directory: Path,
    *,
    label: str,
    expected_name: str | None = None,
) -> Path:
    if not isinstance(raw, str) or not raw:
        raise _EvidenceError(f"{label} path is missing or is not a string")
    candidate = Path(raw)
    if not candidate.is_absolute():
        raise _EvidenceError(f"{label} must be an absolute path")
    try:
        resolved = candidate.resolve(strict=True)
    except (OSError, RuntimeError, ValueError) as exc:
        raise _EvidenceError(f"{label} is unavailable: {exc}") from exc
    if candidate != resolved or candidate.is_symlink():
        raise _EvidenceError(f"{label} must be canonical and contain no symlinks")
    if resolved.parent != directory:
        raise _EvidenceError(f"{label} must be a direct child of the Yosys.EQY directory")
    if expected_name is not None and resolved.name != expected_name:
        raise _EvidenceError(f"{label} must be named {expected_name}")
    if not resolved.is_file():
        raise _EvidenceError(f"{label} is not a regular file")
    return resolved


def _read_json_object(path: Path, *, label: str) -> Mapping[str, Any]:
    try:
        text = path.read_text(encoding="utf-8")
    except (OSError, UnicodeError) as exc:
        raise _EvidenceError(f"{label} is not readable UTF-8 JSON: {exc}") from exc
    if not text.strip():
        raise _EvidenceError(f"{label} is empty")
    try:
        value = json.loads(text)
    except json.JSONDecodeError as exc:
        raise _EvidenceError(f"{label} is invalid JSON: {exc}") from exc
    if not isinstance(value, Mapping) or not value:
        raise _EvidenceError(f"{label} must be a non-empty JSON object")
    return value


def _evidence_file(path: Path) -> dict[str, Any]:
    return {
        "path": str(path),
        "sha256": file_sha256(path),
        "size_bytes": path.stat().st_size,
    }


def _validate_step(
    step: Mapping[str, Any],
    *,
    run_directory: Path,
    design_directory: Path,
    input_snapshot_directory: Path | None,
    pdk: str,
    expected_script_sha256: str,
    expected_gate_tcl_sha256: str,
) -> tuple[dict[str, Any], dict[str, Any], dict[str, Any]]:
    ordinal = step.get("ordinal")
    if isinstance(ordinal, bool) or not isinstance(ordinal, int) or ordinal < 1:
        raise _EvidenceError("Yosys.EQY ordinal must be a positive integer")
    directory = _canonical_directory(step.get("directory"))
    if directory.parent != run_directory:
        raise _EvidenceError("Yosys.EQY directory must be a direct child of the run directory")
    directory_match = _STEP_DIRECTORY_RE.fullmatch(directory.name)
    if directory_match is None or int(directory_match.group(1)) != ordinal:
        raise _EvidenceError("Yosys.EQY directory ordinal does not match the step record")

    config_path = _canonical_child(
        str(directory / "config.json"),
        directory,
        label="Yosys.EQY config",
        expected_name="config.json",
    )
    step_config = _read_json_object(config_path, label="Yosys.EQY config")
    meta = step_config.get("meta")
    if not isinstance(meta, Mapping) or meta.get("step") != EQY_STEP_ID:
        raise _EvidenceError("Yosys.EQY config does not attest the exact Step ID")
    if step_config.get("PDK") != pdk:
        raise _EvidenceError("Yosys.EQY Step config does not bind the requested PDK")
    raw_script_path = step_config.get("EQY_SCRIPT")
    if not isinstance(raw_script_path, str) or not raw_script_path:
        raise _EvidenceError("Yosys.EQY Step config does not bind EQY_SCRIPT")
    script_candidate = Path(raw_script_path)
    try:
        script_relative = script_candidate.relative_to(design_directory)
    except ValueError as exc:
        raise _EvidenceError(
            f"Yosys.EQY script is outside the logical design directory: {exc}"
        ) from exc
    if (
        not script_candidate.is_absolute()
        or ".." in script_relative.parts
        or script_candidate != design_directory / script_relative
        or script_candidate.parent != design_directory / "eqy"
        or script_candidate.suffix != ".eqy"
    ):
        raise _EvidenceError("Yosys.EQY logical script identity is invalid")

    try:
        if input_snapshot_directory is None:
            script_path = script_candidate.resolve(strict=True)
            if script_path != script_candidate or script_candidate.is_symlink():
                raise ValueError("logical script path is not canonical")
        else:
            script_path = canonical_regular_file(
                input_snapshot_directory / script_relative,
                root=input_snapshot_directory,
                label="snapshotted Yosys.EQY script",
                minimum_size=1,
                maximum_size=64 * 1024 * 1024,
            )
    except (OSError, RuntimeError, ValueError) as exc:
        raise _EvidenceError(f"Yosys.EQY script snapshot is unavailable: {exc}") from exc
    if file_sha256(script_path) != expected_script_sha256:
        raise _EvidenceError("Yosys.EQY script SHA-256 is invalid")

    logical_gate_tcl_path = script_candidate.with_name(f"{script_candidate.stem}-gate.tcl")
    gate_tcl_relative = logical_gate_tcl_path.relative_to(design_directory)
    try:
        if input_snapshot_directory is None:
            gate_tcl_path = logical_gate_tcl_path.resolve(strict=True)
            if gate_tcl_path != logical_gate_tcl_path or logical_gate_tcl_path.is_symlink():
                raise ValueError("logical gate Tcl path is not canonical")
        else:
            gate_tcl_path = canonical_regular_file(
                input_snapshot_directory / gate_tcl_relative,
                root=input_snapshot_directory,
                label="snapshotted Yosys.EQY gate Tcl",
                minimum_size=1,
                maximum_size=64 * 1024 * 1024,
            )
    except (OSError, RuntimeError, ValueError) as exc:
        raise _EvidenceError(f"Yosys.EQY gate Tcl snapshot is unavailable: {exc}") from exc
    if file_sha256(gate_tcl_path) != expected_gate_tcl_sha256:
        raise _EvidenceError("Yosys.EQY gate Tcl SHA-256 is invalid")

    if step.get("completed") is not True:
        raise _EvidenceError("Yosys.EQY Step has not completed")
    state_path = _canonical_child(
        step.get("state_out"),
        directory,
        label="Yosys.EQY state_out",
        expected_name="state_out.json",
    )
    _read_json_object(state_path, label="Yosys.EQY state_out")
    state_in_path = _canonical_child(
        str(directory / "state_in.json"),
        directory,
        label="Yosys.EQY state_in",
        expected_name="state_in.json",
    )
    _read_json_object(state_in_path, label="Yosys.EQY state_in")

    raw_logs = step.get("logs")
    if (
        not isinstance(raw_logs, Sequence)
        or isinstance(raw_logs, (str, bytes, bytearray))
        or not raw_logs
    ):
        raise _EvidenceError("Yosys.EQY must provide at least one log")
    logs: list[str] = []
    log_evidence: list[dict[str, Any]] = []
    verdict_logs: list[Path] = []
    seen: set[Path] = set()
    for raw_log in raw_logs:
        log_path = _canonical_child(raw_log, directory, label="Yosys.EQY log")
        if log_path.suffix != ".log":
            raise _EvidenceError("Yosys.EQY log must use the .log suffix")
        if log_path in seen:
            raise _EvidenceError("Yosys.EQY log list contains a duplicate path")
        seen.add(log_path)
        try:
            text = log_path.read_text(encoding="utf-8")
        except (OSError, UnicodeError) as exc:
            raise _EvidenceError(f"Yosys.EQY log is not readable: {exc}") from exc
        if not text:
            raise _EvidenceError("Yosys.EQY log is empty")
        if (
            "Successfully proved designs equivalent" in text
            and "DONE (PASS, rc=0)" in text
        ):
            verdict_logs.append(log_path)
        logs.append(str(log_path))
        log_evidence.append(_evidence_file(log_path))
    if len(verdict_logs) != 1:
        raise _EvidenceError(
            "Yosys.EQY requires exactly one log containing the full-design PASS verdict"
        )

    scratch = _canonical_directory(str(directory / "scratch"))
    if scratch.parent != directory or scratch.name != "scratch":
        raise _EvidenceError("Yosys.EQY scratch must be a direct canonical child")
    for failure_name in ("FAIL", "UNKNOWN", "ERROR", "TIMEOUT"):
        if (scratch / failure_name).exists():
            raise _EvidenceError(f"Yosys.EQY scratch contains failure status {failure_name}")
    pass_path = _canonical_child(
        str(scratch / "PASS"), scratch, label="Yosys.EQY PASS", expected_name="PASS"
    )
    try:
        pass_text = pass_path.read_text(encoding="utf-8")
    except (OSError, UnicodeError) as exc:
        raise _EvidenceError(f"Yosys.EQY PASS is not readable: {exc}") from exc
    if not pass_text.strip():
        raise _EvidenceError("Yosys.EQY PASS status file is empty")

    step_evidence = {
        "id": EQY_STEP_ID,
        "ordinal": ordinal,
        "directory": str(directory),
    }
    evidence = {
        "config": _evidence_file(config_path),
        "eqy_script": {
            **_evidence_file(script_path),
            "logical_path": str(script_candidate),
        },
        "eqy_gate_tcl": {
            **_evidence_file(gate_tcl_path),
            "logical_path": str(logical_gate_tcl_path),
        },
        "state_in": _evidence_file(state_in_path),
        "state_out": _evidence_file(state_path),
        "logs": log_evidence,
        "status": _evidence_file(pass_path),
    }
    verdict = {
        "status": "PASS",
        "source": "eqy_status_file_and_step_log",
        "status_path": str(pass_path),
        "status_sha256": evidence["status"]["sha256"],
        "log_path": str(verdict_logs[0]),
        "log_sha256": file_sha256(verdict_logs[0]),
        "status_marker": "PASS status file",
        "full_design_marker": "Successfully proved designs equivalent",
        "log_marker": "DONE (PASS, rc=0)",
    }
    return step_evidence, evidence, verdict


def evaluate_eqy_assurance(
    config: Mapping[str, Any],
    pdk: str,
    steps: Sequence[Mapping[str, Any]],
    *,
    run_id: str | None = None,
    run_dir: str | Path | None = None,
    design_dir: str | Path | None = None,
    input_snapshot_dir: str | Path | None = None,
) -> dict[str, Any]:
    """Evaluate a LibreLane ``RUN_EQY`` request from discovered evidence.

    A passing result requires one and only one completed ``Yosys.EQY`` Step
    whose on-disk identity, state, directory, and logs can all be verified.
    """

    if not isinstance(config, Mapping):
        return _result(
            requested=False,
            status="invalid_configuration",
            gate="fail",
            reasons=("config must be an already-parsed mapping",),
            run_id=run_id,
            run_dir=str(run_dir) if run_dir is not None else None,
        )
    run_eqy = config.get("RUN_EQY", False)
    if not isinstance(run_eqy, bool):
        return _result(
            requested=False,
            status="invalid_configuration",
            gate="fail",
            reasons=("RUN_EQY must be a boolean",),
            run_id=run_id,
            run_dir=str(run_dir) if run_dir is not None else None,
        )
    if not run_eqy:
        return _result(
            requested=False,
            status="not_requested",
            gate="not_applicable",
            reasons=("RUN_EQY is disabled",),
            run_id=run_id,
            run_dir=str(run_dir) if run_dir is not None else None,
        )
    expected_script_sha256 = config.get("EQY_SCRIPT_SHA256")
    if (
        not isinstance(expected_script_sha256, str)
        or re.fullmatch(r"[0-9a-f]{64}", expected_script_sha256) is None
    ):
        return _result(
            requested=True,
            status="invalid_configuration",
            gate="fail",
            reasons=("EQY_SCRIPT_SHA256 must be a lowercase SHA-256 digest",),
            run_id=run_id,
            run_dir=str(run_dir) if run_dir is not None else None,
        )
    expected_gate_tcl_sha256 = config.get("EQY_GATE_TCL_SHA256")
    if (
        not isinstance(expected_gate_tcl_sha256, str)
        or re.fullmatch(r"[0-9a-f]{64}", expected_gate_tcl_sha256) is None
    ):
        return _result(
            requested=True,
            status="invalid_configuration",
            gate="fail",
            reasons=("EQY_GATE_TCL_SHA256 must be a lowercase SHA-256 digest",),
            run_id=run_id,
            run_dir=str(run_dir) if run_dir is not None else None,
        )
    if pdk != "sky130A":
        return _result(
            requested=True,
            status="unsupported",
            gate="fail",
            reasons=("LibreLane RUN_EQY assurance is supported only for sky130A",),
            run_id=run_id,
            run_dir=str(run_dir) if run_dir is not None else None,
        )
    if not isinstance(run_id, str) or not run_id or run_dir is None:
        return _result(
            requested=True,
            status="incomplete",
            gate="fail",
            reasons=("Yosys.EQY run identity is missing",),
            run_id=run_id,
            run_dir=str(run_dir) if run_dir is not None else None,
        )
    try:
        run_directory = _canonical_directory(str(run_dir))
    except _EvidenceError as exc:
        return _result(
            requested=True,
            status="incomplete",
            gate="fail",
            reasons=(str(exc),),
            run_id=run_id,
            run_dir=str(run_dir),
        )
    if run_directory.name != f"PI_LANE_{run_id}":
        return _result(
            requested=True,
            status="incomplete",
            gate="fail",
            reasons=("Yosys.EQY run directory does not match the managed run ID",),
            run_id=run_id,
            run_dir=str(run_directory),
        )
    try:
        if design_dir is None and input_snapshot_dir is None:
            design_directory = run_directory.parent.parent
            input_snapshot_directory = None
        elif design_dir is None or input_snapshot_dir is None:
            raise ValueError(
                "design_dir and input_snapshot_dir must be provided together"
            )
        else:
            design_directory = canonical_fs_directory(
                Path(design_dir), label="Yosys.EQY logical design directory"
            )
            input_snapshot_directory = canonical_fs_directory(
                Path(input_snapshot_dir), label="Yosys.EQY input snapshot directory"
            )
    except (OSError, RuntimeError, ValueError) as exc:
        return _result(
            requested=True,
            status="incomplete",
            gate="fail",
            reasons=(f"Yosys.EQY input identity is invalid: {exc}",),
            run_id=run_id,
            run_dir=str(run_directory),
        )
    if (
        not isinstance(steps, Sequence)
        or isinstance(steps, (str, bytes, bytearray))
    ):
        return _result(
            requested=True,
            status="incomplete",
            gate="fail",
            reasons=("discovered Steps must be a sequence",),
            run_id=run_id,
            run_dir=str(run_directory),
        )

    eqy_steps = [
        step
        for step in steps
        if isinstance(step, Mapping) and step.get("id") == EQY_STEP_ID
    ]
    if len(eqy_steps) != 1:
        reason = (
            "Yosys.EQY Step evidence is missing"
            if not eqy_steps
            else "multiple Yosys.EQY Step records are ambiguous and untrusted"
        )
        return _result(
            requested=True,
            status="incomplete",
            gate="fail",
            reasons=(reason,),
            run_id=run_id,
            run_dir=str(run_directory),
        )

    try:
        step_evidence, evidence, verdict = _validate_step(
            eqy_steps[0],
            run_directory=run_directory,
            design_directory=design_directory,
            input_snapshot_directory=input_snapshot_directory,
            pdk=pdk,
            expected_script_sha256=expected_script_sha256,
            expected_gate_tcl_sha256=expected_gate_tcl_sha256,
        )
    except _EvidenceError as exc:
        return _result(
            requested=True,
            status="incomplete",
            gate="fail",
            reasons=(str(exc),),
            run_id=run_id,
            run_dir=str(run_directory),
        )
    return _result(
        requested=True,
        status="pass",
        gate="pass",
        artifact=evidence["state_out"]["path"],
        logs=[item["path"] for item in evidence["logs"]],
        run_id=run_id,
        run_dir=str(run_directory),
        step=step_evidence,
        evidence=evidence,
        verdict=verdict,
        reasons=(),
    )


def open_pdk_assurance(pdk: str) -> dict[str, Any]:
    """Return the fixed assurance boundary for a supported open PDK."""

    if pdk not in OPEN_PDKS:
        raise ValueError(f"unsupported open PDK: {pdk}")
    return {
        "pdk": pdk,
        "open_pdk": True,
        "foundry_signoff": False,
        "tapeout_guarantee": False,
        "limitations": OPEN_PDK_LIMITATIONS,
    }
