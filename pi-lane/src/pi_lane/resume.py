"""Validation for creating a new Classic run from a finished State snapshot."""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Mapping

from .classic import (
    CLASSIC_REGISTRY_VERSION,
    CLASSIC_STEP_IDS,
    LIBRELANE_IMAGE,
    LIBRELANE_VERSION,
    canonical_classic_step,
    next_classic_step,
    validate_classic_range,
)
from .config import DesignConfig, validate_override, verify_design_input_snapshot
from .integrity import (
    canonical_directory,
    canonical_regular_file,
    direct_child_directory,
    file_sha256,
)
from .run_state import RUN_STATE_SCHEMA_VERSION


StateLoader = Callable[[str], Mapping[str, Any]]
MAX_RESUME_CHAIN_DEPTH = 128


@dataclass(frozen=True)
class ResumePlan:
    parent_run_id: str
    root_run_id: str
    chain_depth: int
    initial_state: Path
    initial_state_sha256: str
    parent_state_attestation_sha256: str
    parent_chain_proof_sha256: str
    frm: str
    to: str | None


def _json_sha256(value: Mapping[str, Any]) -> str:
    import hashlib

    return hashlib.sha256(
        json.dumps(value, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()


def validate_initial_state(path: Path, design_dir: Path) -> tuple[Path, str]:
    """Validate a LibreLane State snapshot mounted inside the design directory."""
    design_root = design_dir.expanduser().resolve(strict=True)
    try:
        resolved = canonical_regular_file(
            path,
            root=design_root,
            label="initial State",
            minimum_size=2,
            maximum_size=64 * 1024 * 1024,
        )
    except ValueError as exc:
        raise ValueError(
            "initial State must be a canonical regular file inside the mounted design directory"
        ) from exc
    if resolved.name != "state_out.json":
        raise ValueError("initial State must be a completed Step state_out.json file")
    try:
        raw = json.loads(resolved.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError(f"initial State is not valid JSON: {exc}") from exc
    if not isinstance(raw, dict) or not raw:
        raise ValueError("initial State must be a non-empty JSON object")
    return resolved, file_sha256(resolved)


def _checkpoint(state: Mapping[str, Any]) -> tuple[Path, str]:
    artifacts = state.get("artifacts")
    if not isinstance(artifacts, Mapping):
        raise ValueError("run does not expose a structured artifact record")
    artifact_state = artifacts.get("latest_state")
    if not isinstance(artifact_state, str) or not artifact_state:
        raise ValueError("run does not expose its latest State artifact")
    run_dir = state.get("librelane_run_dir")
    if not isinstance(run_dir, str) or not run_dir:
        raise ValueError("run has no canonical LibreLane run directory")
    return validate_initial_state(Path(artifact_state), Path(run_dir))


def _validate_execution_identity(state: Mapping[str, Any]) -> None:
    if str(state.get("flow", "")).casefold() != "classic":
        raise ValueError("resume chain is not a Classic flow")
    if state.get("flow_registry_version") != CLASSIC_REGISTRY_VERSION:
        raise ValueError("resume chain registry version is missing or incompatible")
    if state.get("image") != LIBRELANE_IMAGE or state.get("librelane_version") != LIBRELANE_VERSION:
        raise ValueError("resume chain is not bound to the pinned LibreLane image/version")
    hashes = (
        ("configuration", state.get("config_sha256"), r"[0-9a-f]{64}"),
        ("PDK content integrity", state.get("pdk_integrity_sha256"), r"[0-9a-f]{64}"),
        ("image digest", state.get("image_digest"), r"sha256:[0-9a-f]{64}"),
        ("PDK revision", state.get("pdk_revision"), r"[0-9a-f]{40}"),
    )
    for label, value, pattern in hashes:
        if not isinstance(value, str) or re.fullmatch(pattern, value) is None:
            raise ValueError(f"resume chain has an invalid {label}")
    if state.get("schema_version") != RUN_STATE_SCHEMA_VERSION:
        raise ValueError(
            "resume chain requires managed run state schema "
            f"{RUN_STATE_SCHEMA_VERSION}"
        )
    bundle = state.get("input_bundle_sha256")
    if not isinstance(bundle, str) or re.fullmatch(r"[0-9a-f]{64}", bundle) is None:
        raise ValueError("resume chain has an invalid design input bundle")
    snapshot_errors = verify_design_input_snapshot(dict(state))
    if snapshot_errors:
        raise ValueError(snapshot_errors[0])
    design_dir = state.get("design_dir")
    config_path = state.get("config_path")
    pdk_root = state.get("pdk_root")
    run_dir = state.get("librelane_run_dir")
    if not all(
        isinstance(value, str) and value
        for value in (design_dir, config_path, pdk_root, run_dir)
    ):
        raise ValueError("resume chain filesystem identity is incomplete")
    trusted_design = canonical_directory(Path(design_dir), label="resume design directory")
    canonical_regular_file(
        Path(config_path),
        root=trusted_design,
        label="resume config",
        minimum_size=2,
        maximum_size=16 * 1024 * 1024,
    )
    runs_root = canonical_directory(
        trusted_design / "runs", label="resume runs directory"
    )
    trusted_run = direct_child_directory(
        Path(run_dir), root=runs_root, label="resume LibreLane run directory"
    )
    if trusted_run.name != f"PI_LANE_{state.get('run_id')}":
        raise ValueError("resume run directory does not match its run ID")
    canonical_directory(Path(pdk_root), label="resume PDK root")
    overrides = state.get("overrides") or []
    if not isinstance(overrides, list) or any(
        not isinstance(item, str) or validate_override(item) != item
        for item in overrides
    ):
        raise ValueError("resume chain overrides are malformed or controller-managed")


def state_attestation_sha256(state: Mapping[str, Any]) -> str:
    """Hash stable execution identity plus the current completed checkpoint."""
    checkpoint, checkpoint_sha256 = _checkpoint(state)
    payload = {
        "schema_version": 2,
        "run_id": state.get("run_id"),
        "config_sha256": state.get("config_sha256"),
        "input_bundle_sha256": state.get("input_bundle_sha256"),
        "design_dir": state.get("design_dir"),
        "librelane_run_dir": state.get("librelane_run_dir"),
        "config_path": state.get("config_path"),
        "pdk": state.get("pdk"),
        "pdk_revision": state.get("pdk_revision"),
        "pdk_integrity_sha256": state.get("pdk_integrity_sha256"),
        "pdk_root": state.get("pdk_root"),
        "image": state.get("image"),
        "image_digest": state.get("image_digest"),
        "librelane_version": state.get("librelane_version"),
        "flow": state.get("flow"),
        "flow_registry_version": state.get("flow_registry_version"),
        "overrides": state.get("overrides") or [],
        "jobs": state.get("jobs"),
        "run_eqy_requested": state.get("run_eqy_requested"),
        "from": state.get("from"),
        "to": state.get("to"),
        "initial_state_sha256": state.get("initial_state_sha256"),
        "parent_run_id": state.get("parent_run_id"),
        "last_completed_step": state.get("last_completed_step"),
        "checkpoint": str(checkpoint),
        "checkpoint_sha256": checkpoint_sha256,
    }
    return _json_sha256(payload)


def _root_chain_proof(state: Mapping[str, Any]) -> str:
    return _json_sha256(
        {
            "schema_version": 2,
            "kind": "pi-lane-resume-root",
            "run_id": state.get("run_id"),
            "config_sha256": state.get("config_sha256"),
            "input_bundle_sha256": state.get("input_bundle_sha256"),
            "design_dir": state.get("design_dir"),
            "librelane_run_dir": state.get("librelane_run_dir"),
            "config_path": state.get("config_path"),
            "pdk": state.get("pdk"),
            "pdk_revision": state.get("pdk_revision"),
            "pdk_integrity_sha256": state.get("pdk_integrity_sha256"),
            "pdk_root": state.get("pdk_root"),
            "image": state.get("image"),
            "image_digest": state.get("image_digest"),
            "librelane_version": state.get("librelane_version"),
            "flow": state.get("flow"),
            "flow_registry_version": state.get("flow_registry_version"),
            "overrides": state.get("overrides") or [],
            "jobs": state.get("jobs"),
            "run_eqy_requested": state.get("run_eqy_requested"),
        }
    )


def link_chain_proof(
    *,
    parent_chain_proof_sha256: str,
    parent_state_attestation_sha256: str,
    parent_run_id: str,
    run_id: str,
    root_run_id: str,
    chain_depth: int,
    frm: str,
    initial_state_sha256: str,
    input_bundle_sha256: str,
) -> str:
    if re.fullmatch(r"[0-9a-f]{64}", input_bundle_sha256) is None:
        raise ValueError("resume link requires a valid design input bundle identity")
    return _json_sha256(
        {
            "schema_version": 2,
            "kind": "pi-lane-resume-link",
            "parent_chain_proof_sha256": parent_chain_proof_sha256,
            "parent_state_attestation_sha256": parent_state_attestation_sha256,
            "parent_run_id": parent_run_id,
            "run_id": run_id,
            "root_run_id": root_run_id,
            "chain_depth": chain_depth,
            "from": frm,
            "initial_state_sha256": initial_state_sha256,
            "input_bundle_sha256": input_bundle_sha256,
        }
    )


def _same_execution_identity(child: Mapping[str, Any], parent: Mapping[str, Any]) -> bool:
    keys = (
        "config_sha256",
        "input_bundle_sha256",
        "config_path",
        "design_dir",
        "pdk",
        "pdk_revision",
        "pdk_integrity_sha256",
        "pdk_root",
        "image",
        "image_digest",
        "librelane_version",
        "flow",
        "flow_registry_version",
        "jobs",
        "run_eqy_requested",
    )
    return all(child.get(key) == parent.get(key) for key in keys) and (
        child.get("overrides") or []
    ) == (parent.get("overrides") or [])


def verify_resume_chain(
    state: Mapping[str, Any],
    state_loader: StateLoader | None,
    *,
    _visited: frozenset[str] = frozenset(),
    _require_checkpoint: bool = False,
) -> dict[str, Any]:
    """Recursively verify one root-to-child chain from immutable link evidence."""
    run_id = state.get("run_id")
    if not isinstance(run_id, str) or not run_id or run_id in _visited:
        raise ValueError("resume chain contains an invalid or cyclic run ID")
    _validate_execution_identity(state)
    if _require_checkpoint:
        checkpoint, _ = _checkpoint(state)
        last_completed = state.get("last_completed_step")
        if not isinstance(last_completed, str):
            raise ValueError("resume parent has no completed Step checkpoint")
        _validate_step_history(
            state, canonical_classic_step(last_completed), checkpoint
        )

    resume = state.get("resume")
    if state.get("from") is None and state.get("initial_state") is None and resume is None:
        return {
            "verified": True,
            "root_run_id": run_id,
            "chain_depth": 0,
            "chain_proof_sha256": _root_chain_proof(state),
        }
    if not isinstance(resume, Mapping) or state_loader is None:
        raise ValueError("resume chain requires a verifiable parent link")
    if (
        resume.get("from") != state.get("from")
        or resume.get("initial_state") != state.get("initial_state")
        or resume.get("initial_state_sha256") != state.get("initial_state_sha256")
        or resume.get("input_bundle_sha256") != state.get("input_bundle_sha256")
    ):
        raise ValueError("resume link fields do not match the child run identity")
    parent_run_id = resume.get("parent_run_id")
    if (
        not isinstance(parent_run_id, str)
        or not parent_run_id
        or parent_run_id != state.get("parent_run_id")
    ):
        raise ValueError("resume parent run ID is missing or inconsistent")
    parent = state_loader(parent_run_id)
    if parent.get("run_id") != parent_run_id:
        raise ValueError("resume parent state identity does not match its run ID")
    parent_proof = verify_resume_chain(
        parent,
        state_loader,
        _visited=_visited | {run_id},
        _require_checkpoint=True,
    )
    if not _same_execution_identity(state, parent):
        raise ValueError("resume execution identity differs from its parent")
    parent_attestation = state_attestation_sha256(parent)
    if resume.get("parent_state_attestation_sha256") != parent_attestation:
        raise ValueError("resume parent state attestation changed")
    if resume.get("parent_chain_proof_sha256") != parent_proof["chain_proof_sha256"]:
        raise ValueError("resume parent chain proof changed")

    parent_checkpoint, parent_checkpoint_sha256 = _checkpoint(parent)
    initial_state, initial_state_sha256 = validate_initial_state(
        Path(str(state.get("initial_state"))),
        Path(str(parent.get("librelane_run_dir"))),
    )
    if initial_state != parent_checkpoint or initial_state_sha256 != parent_checkpoint_sha256:
        raise ValueError("resume initial State is not the parent checkpoint")
    if state.get("initial_state_sha256") != initial_state_sha256:
        raise ValueError("resume initial State hash is inconsistent")
    parent_last = canonical_classic_step(str(parent.get("last_completed_step")))
    expected_from = next_classic_step(parent_last)
    if expected_from is None or state.get("from") != expected_from:
        raise ValueError("resume does not start at the immediate next Classic Step")
    root_run_id = parent_proof["root_run_id"]
    chain_depth = parent_proof["chain_depth"] + 1
    if chain_depth > MAX_RESUME_CHAIN_DEPTH:
        raise ValueError("resume chain exceeds the supported depth")
    if resume.get("root_run_id") != root_run_id or resume.get("chain_depth") != chain_depth:
        raise ValueError("resume root or chain depth is inconsistent")
    expected_link = link_chain_proof(
        parent_chain_proof_sha256=parent_proof["chain_proof_sha256"],
        parent_state_attestation_sha256=parent_attestation,
        parent_run_id=parent_run_id,
        run_id=run_id,
        root_run_id=root_run_id,
        chain_depth=chain_depth,
        frm=expected_from,
        initial_state_sha256=initial_state_sha256,
        input_bundle_sha256=str(state["input_bundle_sha256"]),
    )
    if resume.get("chain_proof_sha256") != expected_link:
        raise ValueError("resume link proof is missing or invalid")
    return {
        "verified": True,
        "root_run_id": root_run_id,
        "chain_depth": chain_depth,
        "chain_proof_sha256": expected_link,
    }


def _validate_step_history(
    source: Mapping[str, Any], last_completed: str, initial_state: Path
) -> None:
    history = source.get("step_history")
    if not isinstance(history, list):
        raise ValueError("source run has no Step history to bind the checkpoint")
    completed = [
        item
        for item in history
        if isinstance(item, Mapping) and item.get("status") == "completed"
    ]
    if not completed:
        raise ValueError("source run has no completed Step history")
    previous_ordinal = 0
    previous_registry_index = -1
    seen_ordinals: set[int] = set()
    for item in completed:
        ordinal = item.get("ordinal")
        if isinstance(ordinal, bool) or not isinstance(ordinal, int) or ordinal <= previous_ordinal:
            raise ValueError("source Step ordinals must be positive and strictly increasing")
        if ordinal in seen_ordinals:
            raise ValueError("source Step history contains duplicate ordinals")
        seen_ordinals.add(ordinal)
        step_id = canonical_classic_step(str(item.get("id")))
        registry_index = CLASSIC_STEP_IDS.index(step_id)
        if registry_index <= previous_registry_index:
            raise ValueError("source Step IDs do not follow the Classic registry order")
        previous_ordinal = ordinal
        previous_registry_index = registry_index
    last = completed[-1]
    if last.get("id") != last_completed or last.get("state_out") != str(initial_state):
        raise ValueError("latest State is not bound to the last completed Step")


def build_resume_plan(
    source: Mapping[str, Any],
    config: DesignConfig,
    *,
    current_image_digest: str,
    current_pdk_revision: str | None,
    current_pdk_integrity_sha256: str | None,
    state_loader: StateLoader | None = None,
    requested_from: str | None = None,
    requested_to: str | None = None,
) -> ResumePlan:
    """Bind a new run to one compatible parent checkpoint without skipping Steps."""
    parent_run_id = source.get("run_id")
    if not isinstance(parent_run_id, str) or not parent_run_id:
        raise ValueError("resume source has no valid run ID")
    if source.get("is_running") or source.get("process_status") == "running":
        raise RuntimeError("cannot resume an active run")
    if str(source.get("flow", "")).casefold() != "classic":
        raise ValueError("validated resume currently supports only the Classic flow")
    registry = source.get("flow_registry_version")
    if registry != CLASSIC_REGISTRY_VERSION:
        raise ValueError(
            f"resume source uses incompatible Step registry {registry!r}"
        )
    if source.get("config_sha256") != config.sha256:
        raise ValueError("design configuration changed since the source run")
    if source.get("pdk") != config.pdk:
        raise ValueError("source run PDK does not match the design configuration")
    source_pdk_revision = source.get("pdk_revision")
    if not isinstance(source_pdk_revision, str) or not source_pdk_revision:
        raise ValueError("source run has no verified PDK revision")
    if source_pdk_revision != current_pdk_revision:
        raise ValueError("PDK revision changed since the source run")
    source_pdk_integrity = source.get("pdk_integrity_sha256")
    if (
        not isinstance(source_pdk_integrity, str)
        or not source_pdk_integrity
        or source_pdk_integrity != current_pdk_integrity_sha256
    ):
        raise ValueError("PDK content integrity changed since the source run")
    if source.get("image") != LIBRELANE_IMAGE or source.get("librelane_version") != LIBRELANE_VERSION:
        raise ValueError("source run is not bound to the pinned LibreLane image/version")
    if source.get("image_digest") != current_image_digest:
        raise ValueError("LibreLane image digest changed since the source run")
    source_design_dir = Path(str(source.get("design_dir", ""))).expanduser().resolve()
    if source_design_dir != config.design_dir.resolve():
        raise ValueError("source run design directory does not match the configuration")

    parent_proof = verify_resume_chain(source, state_loader)
    root_run_id = parent_proof["root_run_id"]
    chain_depth = parent_proof["chain_depth"] + 1
    if chain_depth > MAX_RESUME_CHAIN_DEPTH:
        raise ValueError("resume chain exceeds the supported depth")

    last_completed = source.get("last_completed_step")
    if not isinstance(last_completed, str):
        raise ValueError("source run has no completed Step checkpoint")
    last_completed = canonical_classic_step(last_completed)
    automatic_from = next_classic_step(last_completed)
    if automatic_from is None:
        raise ValueError("source run already reached the final Classic Step")
    frm = canonical_classic_step(requested_from) if requested_from else automatic_from
    if frm != automatic_from:
        raise ValueError(
            "resume must start at the immediate Step after the checkpoint: "
            f"{automatic_from}"
        )
    frm, to = validate_classic_range(frm, requested_to)
    assert frm is not None

    initial_state, state_sha256 = _checkpoint(source)
    _validate_step_history(source, last_completed, initial_state)
    parent_attestation = state_attestation_sha256(source)
    return ResumePlan(
        parent_run_id=parent_run_id,
        root_run_id=root_run_id,
        chain_depth=chain_depth,
        initial_state=initial_state,
        initial_state_sha256=state_sha256,
        parent_state_attestation_sha256=parent_attestation,
        parent_chain_proof_sha256=parent_proof["chain_proof_sha256"],
        frm=frm,
        to=to,
    )
