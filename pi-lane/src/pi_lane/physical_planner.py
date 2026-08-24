"""Durable background supervisor for tool-driven physical planning."""

from __future__ import annotations

import fcntl
import hashlib
import json
import math
import os
import re
import shlex
import signal
import subprocess
import sys
import time
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path
from typing import Any, Iterator, Mapping

from .config import OPEN_PDKS, load_design_config
from .controller import (
    IMAGE,
    PDK_ROOT,
    reconcile_status,
    start_planning_probe,
    start_run,
    stop_run,
)
from .integrity import canonical_regular_file, file_sha256
from .physical_plan import (
    PLAN_ID_RE,
    PlanningInputs,
    candidate_domain_projection,
    canonical_sha256,
    derive_lower_bounds,
    export_geometry_lock_document,
    finalize_plan,
    generate_candidates,
    geometry_lock,
    load_geometry_lock_document,
    load_plan,
    load_policy,
    minimum_geometry_classification,
    parse_bbox,
    select_minimum_robust_candidate,
)
from .run_state import STATE_ROOT, atomic_write, now_iso, pid_identity_alive, pid_start_time


PLANNING_ROOT = STATE_ROOT / "physical-plans"
PLANNING_CURRENT = PLANNING_ROOT / "current"
TERMINAL = frozenset({"completed", "failed", "stopped"})
PLANNING_STATE_SCHEMA_VERSION = 1
PLANNING_EVENT_SCHEMA_VERSION = 1
PIN_SIDE_RE = re.compile(r"^#([NSEW])$")
IDEMPOTENCY_KEY_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:/-]{0,511}$")
IO_SIDE_HEADER_RE = re.compile(r"^Placement details for the ([NSEW]) side$")
IO_COUNT_RE = re.compile(r"^(Actual pin count|Total pin count|Tracks count):\s+(\d+)$")
IO_CAPACITY_ERROR_RE = re.compile(
    r"The ([NSEW]) side of the floorplan doesn't have enough slots for all the "
    r"pins:\s*(\d+) pins/(\d+) slots"
)

PROBE_SPECS: tuple[tuple[str, str, str], ...] = (
    ("pdn_gate", "OpenROAD.GeneratePDN", "pdn"),
    ("io_preflight_gate", "Odb.CustomIOPlacement", "io_preflight"),
    ("detailed_placement_gate", "OpenROAD.DetailedPlacement", "detailed_placement"),
    ("robustness_gate", "OpenROAD.STAMidPNR-3", "robustness"),
)
PROBE_ENDPOINTS = {gate: endpoint for gate, endpoint, _ in PROBE_SPECS}
PROBE_PREFIXES = {gate: prefix for gate, _, prefix in PROBE_SPECS}


class PhysicalPlanningFailure(RuntimeError):
    def __init__(self, code: str, message: str, *, details: Mapping[str, Any] | None = None):
        super().__init__(message)
        self.code = code
        self.details = dict(details or {})


def _make_plan_id(design_name: str) -> str:
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    slug = re.sub(r"[^A-Za-z0-9_.-]+", "-", design_name).strip("-").lower()
    base = f"{stamp}-{slug}-physical-plan"
    candidate = base
    ordinal = 2
    while (PLANNING_ROOT / candidate).exists():
        candidate = f"{base}-{ordinal}"
        ordinal += 1
    return candidate


def _paths(plan_id: str) -> tuple[Path, Path, Path]:
    if not isinstance(plan_id, str) or PLAN_ID_RE.fullmatch(plan_id) is None:
        raise ValueError("physical planning ID is malformed")
    root = PLANNING_ROOT / plan_id
    return root, root / "state.json", root / "events.jsonl"


@contextmanager
def _start_lock() -> Iterator[None]:
    PLANNING_ROOT.mkdir(parents=True, exist_ok=True, mode=0o700)
    descriptor = os.open(PLANNING_ROOT / "start.lock", os.O_RDWR | os.O_CREAT, 0o600)
    with os.fdopen(descriptor, "a+") as handle:
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


def _load_idempotency_reservations() -> dict[str, dict[str, str]]:
    path = PLANNING_ROOT / "idempotency.json"
    if not path.is_file():
        return {}
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise RuntimeError("physical planning idempotency index is malformed")
    reservations: dict[str, dict[str, str]] = {}
    for key, row in value.items():
        if (
            not isinstance(key, str)
            or IDEMPOTENCY_KEY_RE.fullmatch(key) is None
            or not isinstance(row, dict)
            or set(row) != {"plan_id", "request_fingerprint"}
            or not isinstance(row["plan_id"], str)
            or PLAN_ID_RE.fullmatch(row["plan_id"]) is None
            or not isinstance(row["request_fingerprint"], str)
            or re.fullmatch(r"[0-9a-f]{64}", row["request_fingerprint"]) is None
        ):
            raise RuntimeError("physical planning idempotency index is malformed")
        reservations[key] = dict(row)
    return reservations


@contextmanager
def _lock(plan_id: str) -> Iterator[None]:
    root, _, _ = _paths(plan_id)
    root.mkdir(parents=True, exist_ok=True, mode=0o700)
    descriptor = os.open(root / "state.lock", os.O_RDWR | os.O_CREAT, 0o600)
    with os.fdopen(descriptor, "a+") as handle:
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


def _load_unlocked(plan_id: str) -> dict[str, Any]:
    _, state_file, _ = _paths(plan_id)
    raw = json.loads(state_file.read_text(encoding="utf-8"))
    if not isinstance(raw, dict) or raw.get("schema_version") != PLANNING_STATE_SCHEMA_VERSION:
        raise RuntimeError("physical planning state is malformed")
    if raw.get("plan_id") != plan_id or raw.get("state_file") != str(state_file):
        raise RuntimeError("physical planning state identity is invalid")
    return raw


def _event_bytes(event: Mapping[str, Any]) -> bytes:
    return json.dumps(event, sort_keys=True, separators=(",", ":")).encode("ascii")


def _commit(
    plan_id: str,
    changes: Mapping[str, Any],
    *,
    event_type: str,
    message: str,
    data: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    _, state_file, events_file = _paths(plan_id)
    with _lock(plan_id):
        state = _load_unlocked(plan_id)
        if state.get("status") in TERMINAL and changes.get("status", state["status"]) != state["status"]:
            raise RuntimeError("terminal physical planning state is immutable")
        revision = int(state.get("state_revision", 0)) + 1
        sequence = int(state.get("event_sequence", 0)) + 1
        event = {
            "schema_version": PLANNING_EVENT_SCHEMA_VERSION,
            "event_id": f"{plan_id}:{sequence}",
            "sequence": sequence,
            "previous_event_sha256": state.get("last_event_sha256"),
            "type": event_type,
            "timestamp": now_iso(),
            "message": message,
            "data": dict(data or {}),
        }
        event["event_sha256"] = hashlib.sha256(_event_bytes(event)).hexdigest()
        with events_file.open("ab", buffering=0) as handle:
            handle.write(_event_bytes(event) + b"\n")
            os.fsync(handle.fileno())
        state.update(changes)
        state.update(
            {
                "state_revision": revision,
                "event_sequence": sequence,
                "last_event_sha256": event["event_sha256"],
                "updated_at": now_iso(),
            }
        )
        atomic_write(state_file, json.dumps(state, indent=2, sort_keys=True) + "\n")
        return state


def load_planning_state(plan_id: str | None = None) -> dict[str, Any]:
    resolved = plan_id
    if resolved is None:
        if not PLANNING_CURRENT.is_file():
            raise RuntimeError("no physical planning run exists")
        resolved = PLANNING_CURRENT.read_text(encoding="utf-8").strip()
    assert resolved is not None
    with _lock(resolved):
        state = _load_unlocked(resolved)
    if state.get("status") not in TERMINAL and not pid_identity_alive(
        state.get("pid"), state.get("pid_start_time")
    ):
        state = _commit(
            resolved,
            {
                "status": "failed",
                "phase": "failed",
                "finished_at": now_iso(),
                "error": {
                    "code": "PLANNER_WORKER_DISAPPEARED",
                    "class": "infrastructure",
                    "rtl_repairable": False,
                    "message": "physical planning worker disappeared before a terminal state",
                },
            },
            event_type="planner_failed",
            message="physical planning worker identity was lost",
        )
    return state


def list_planning_states(*, limit: int = 100) -> list[dict[str, Any]]:
    if not 1 <= limit <= 1000:
        raise ValueError("physical planning list limit must be from 1 to 1000")
    rows: list[dict[str, Any]] = []
    if not PLANNING_ROOT.is_dir():
        return rows
    for path in sorted(PLANNING_ROOT.glob("*/state.json"), reverse=True):
        try:
            rows.append(load_planning_state(path.parent.name))
        except (OSError, RuntimeError, ValueError, json.JSONDecodeError):
            continue
    return rows[:limit]


def start_physical_planning(
    config_path: Path,
    *,
    policy_path: Path | None = None,
    pdk_root: Path = PDK_ROOT,
    image: str = IMAGE,
    jobs: int | None = None,
    geometry_lock_path: Path | None = None,
    idempotency_key: str | None = None,
    final_workflow_id: str | None = None,
    final_attempt_id: str | None = None,
    final_idempotency_key: str | None = None,
    final_to: str | None = None,
) -> dict[str, Any]:
    config = load_design_config(config_path)
    if config.pdk not in OPEN_PDKS:
        raise ValueError("physical planning supports only reviewed open PDKs")
    policy = load_policy(policy_path)
    if policy["pdk"] != config.pdk:
        raise ValueError("physical planning policy PDK does not match the design")
    if jobs is not None and not 1 <= jobs <= 256:
        raise ValueError("jobs must be from 1 to 256")
    final_values = (
        final_workflow_id,
        final_attempt_id,
        final_idempotency_key,
    )
    if any(value is not None for value in final_values) and not all(
        isinstance(value, str) and value for value in final_values
    ):
        raise ValueError(
            "automatic final Lane launch requires workflow, attempt, and idempotency IDs"
        )
    if idempotency_key is not None and (
        not isinstance(idempotency_key, str)
        or IDEMPOTENCY_KEY_RE.fullmatch(idempotency_key) is None
    ):
        raise ValueError("physical planning idempotency key is malformed")
    geometry_source = (
        load_geometry_lock_document(geometry_lock_path) if geometry_lock_path else None
    )
    if geometry_source is not None and geometry_source["pdk"] != config.pdk:
        raise ValueError("reference geometry PDK does not match the design")
    request_fingerprint = canonical_sha256(
        {
            "config_sha256": config.sha256,
            "policy_sha256": canonical_sha256(policy),
            "pdk_root": str(pdk_root.expanduser().resolve()),
            "image": image,
            "jobs": jobs,
            "geometry_lock_document_sha256": (
                geometry_source["document_sha256"] if geometry_source else None
            ),
            "final_launch": {
                "workflow_id": final_workflow_id,
                "attempt_id": final_attempt_id,
                "idempotency_key": final_idempotency_key,
                "to": final_to,
            },
        }
    )
    existing_plan_id: str | None = None
    with _start_lock():
        reservations = _load_idempotency_reservations()
        if idempotency_key is not None and idempotency_key in reservations:
            reservation = reservations[idempotency_key]
            if reservation["request_fingerprint"] != request_fingerprint:
                raise RuntimeError(
                    "physical planning idempotency key was reused with another request"
                )
            existing_plan_id = reservation["plan_id"]
            plan_id = existing_plan_id
            root, state_file, events_file = _paths(plan_id)
        else:
            plan_id = _make_plan_id(config.design_name)
            root, state_file, events_file = _paths(plan_id)
            root.mkdir(mode=0o700)
            if idempotency_key is not None:
                reservations[idempotency_key] = {
                    "plan_id": plan_id,
                    "request_fingerprint": request_fingerprint,
                }
                atomic_write(
                    PLANNING_ROOT / "idempotency.json",
                    json.dumps(reservations, indent=2, sort_keys=True) + "\n",
                )
    if existing_plan_id is not None:
        for _ in range(100):
            if state_file.is_file():
                return load_planning_state(existing_plan_id)
            time.sleep(0.05)
        raise RuntimeError("reserved physical planning state was not created")
    policy_snapshot = root / "policy.json"
    atomic_write(policy_snapshot, json.dumps(policy, indent=2, sort_keys=True) + "\n")
    policy_snapshot.chmod(0o400)
    geometry_snapshot: Path | None = None
    if geometry_source is not None:
        geometry_snapshot = root / "reference-geometry-lock.json"
        atomic_write(
            geometry_snapshot,
            json.dumps(geometry_source, indent=2, sort_keys=True) + "\n",
        )
        geometry_snapshot.chmod(0o400)
    state = {
        "schema_version": PLANNING_STATE_SCHEMA_VERSION,
        "state_revision": 0,
        "plan_id": plan_id,
        "mode": "reference-locked" if geometry_source is not None else "auto-safe",
        "status": "launching",
        "phase": "planner_starting",
        "created_at": now_iso(),
        "updated_at": now_iso(),
        "config_path": str(config.path),
        "config_sha256": config.sha256,
        "design_name": config.design_name,
        "pdk": config.pdk,
        "pdk_root": str(pdk_root.expanduser().resolve()),
        "image": image,
        "jobs": jobs,
        "policy_path": str(policy_snapshot),
        "policy_sha256": canonical_sha256(policy),
        "idempotency_key": idempotency_key,
        "request_fingerprint": request_fingerprint,
        "reference_geometry_lock_path": str(geometry_snapshot) if geometry_snapshot else None,
        "reference_geometry_lock_sha256": (
            geometry_source["geometry_lock_sha256"] if geometry_source else None
        ),
        "reference_geometry_document_sha256": (
            geometry_source["document_sha256"] if geometry_source else None
        ),
        "final_launch": (
            {
                "workflow_id": final_workflow_id,
                "attempt_id": final_attempt_id,
                "idempotency_key": final_idempotency_key,
                "to": final_to,
            }
            if final_workflow_id is not None
            else None
        ),
        "final_lane_run_id": None,
        "state_file": str(state_file),
        "events_file": str(events_file),
        "worker_log": str(root / "planner.log"),
        "pid": None,
        "pid_start_time": None,
        "stop_requested": False,
        "active_lane_run_id": None,
        "baseline_lane_run_id": None,
        "candidate_count": 0,
        "completed_candidate_count": 0,
        "candidates": [],
        "physical_plan_path": None,
        "error": None,
        "event_sequence": 0,
        "last_event_sha256": None,
    }
    atomic_write(state_file, json.dumps(state, indent=2, sort_keys=True) + "\n")
    _commit(
        plan_id,
        {},
        event_type="planner_launch_requested",
        message="physical planning worker launch requested",
        data={"mode": state["mode"], "policy_sha256": state["policy_sha256"]},
    )
    log = (root / "planner.log").open("ab", buffering=0)
    try:
        process = subprocess.Popen(
            [sys.executable, "-m", "pi_lane.physical_planner", "worker", "--plan-id", plan_id],
            stdin=subprocess.DEVNULL,
            stdout=log,
            stderr=subprocess.STDOUT,
            start_new_session=True,
            env={"PATH": os.environ.get("PATH", "/usr/bin:/bin")},
        )
    finally:
        log.close()
    identity = pid_start_time(process.pid)
    if identity is None:
        os.killpg(process.pid, signal.SIGTERM)
        raise RuntimeError("physical planning worker process identity is unavailable")
    state = _commit(
        plan_id,
        {
            "pid": process.pid,
            "pid_start_time": identity,
            "status": "running",
            "phase": "baseline_queued",
        },
        event_type="planner_started",
        message="physical planning worker started",
        data={"pid": process.pid, "pid_start_time": identity},
    )
    atomic_write(PLANNING_CURRENT, plan_id + "\n")
    return state


def _worker_state(plan_id: str) -> dict[str, Any]:
    while True:
        with _lock(plan_id):
            state = _load_unlocked(plan_id)
        if state.get("pid") == os.getpid():
            return state
        time.sleep(0.05)


def _check_stop(plan_id: str, active_run_id: str | None = None) -> None:
    with _lock(plan_id):
        state = _load_unlocked(plan_id)
    if state.get("stop_requested") is True:
        if active_run_id:
            stop_run(active_run_id)
        raise InterruptedError("physical planning was stopped by request")


def _wait_lane(
    plan_id: str,
    run_id: str,
    *,
    phase: str,
    timeout_seconds: int,
    poll_seconds: int,
) -> dict[str, Any]:
    started = time.monotonic()
    while True:
        _check_stop(plan_id, run_id)
        lane = reconcile_status(run_id)
        with _lock(plan_id):
            previous = _load_unlocked(plan_id)
        progress = {
            "phase": phase,
            "active_lane_run_id": run_id,
            "lane_status": lane.get("status"),
            "lane_active_step": lane.get("active_step"),
            "lane_last_completed_step": lane.get("last_completed_step"),
            "lane_elapsed_seconds": lane.get("elapsed_seconds"),
        }
        if any(previous.get(key) != value for key, value in progress.items() if key != "lane_elapsed_seconds"):
            _commit(
                plan_id,
                progress,
                event_type="lane_progress",
                message=f"{phase}: {lane.get('status')}",
                data={
                    "run_id": run_id,
                    "active_step": lane.get("active_step"),
                    "last_completed_step": lane.get("last_completed_step"),
                },
            )
        if lane.get("status") in {"completed", "failed", "incomplete", "stopped"}:
            return lane
        if time.monotonic() - started > timeout_seconds:
            stop_run(run_id)
            raise TimeoutError(f"{phase} exceeded {timeout_seconds} seconds")
        time.sleep(poll_seconds)


def _read_json(path: Path, *, root: Path, label: str) -> dict[str, Any]:
    safe = canonical_regular_file(path, root=root, label=label, minimum_size=2, maximum_size=64 * 1024 * 1024)
    value = json.loads(safe.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError(f"{label} is not a JSON object")
    return value


def _side_pin_bits_from_pin_order(
    widths: Mapping[str, int], pin_order_text: str
) -> dict[str, int]:
    """Apply CustomIOPlacement regexes to concrete top-level pin identities."""

    pins = [
        name if width == 1 else f"{name}[{index}]"
        for name, width in widths.items()
        for index in range(width)
    ]
    if len(pins) > 1_000_000:
        raise ValueError("top-level pin count exceeds the planning evidence limit")
    side_bits = {side: 0 for side in "NSEW"}
    assigned: set[str] = set()
    side: str | None = None
    for raw_line in pin_order_text.splitlines():
        line = raw_line.strip()
        match = PIN_SIDE_RE.fullmatch(line)
        if match:
            side = match.group(1)
            continue
        if not line or line.startswith("@") or side is None:
            continue
        try:
            pattern = re.compile(f"^(?:{line})$")
        except re.error as exc:
            raise ValueError(f"I/O pin order pattern is invalid: {line}") from exc
        for pin in pins:
            if pin not in assigned and pattern.fullmatch(pin):
                side_bits[side] += 1
                assigned.add(pin)
    for index, _ in enumerate(pin for pin in pins if pin not in assigned):
        side_bits["NSEW"[index % 4]] += 1
    return side_bits


def _planning_inputs(baseline: Mapping[str, Any]) -> tuple[PlanningInputs, dict[str, Any]]:
    artifacts = baseline.get("artifacts")
    state_path = artifacts.get("latest_state") if isinstance(artifacts, Mapping) else None
    if not isinstance(state_path, str):
        raise ValueError("synthesis baseline has no State artifact")
    run_root = Path(str(baseline["librelane_run_dir"]))
    state_out = _read_json(Path(state_path), root=run_root, label="synthesis baseline State")
    metrics = state_out.get("metrics")
    if not isinstance(metrics, Mapping):
        raise ValueError("synthesis baseline has no metrics")
    mapped_area = metrics.get("design__instance__area")
    if isinstance(mapped_area, bool) or not isinstance(mapped_area, (int, float)) or not math.isfinite(float(mapped_area)) or mapped_area <= 0:
        raise ValueError("synthesis baseline mapped area is unavailable")
    header_path = state_out.get("json_h")
    if not isinstance(header_path, str):
        raise ValueError("synthesis baseline has no Yosys header")
    header = _read_json(Path(header_path), root=run_root, label="Yosys header")
    modules = header.get("modules")
    design_name = str(baseline["design_name"])
    module = modules.get(design_name) if isinstance(modules, Mapping) else None
    ports = module.get("ports") if isinstance(module, Mapping) else None
    if not isinstance(ports, Mapping):
        raise ValueError("Yosys header lacks top-level ports")
    widths: dict[str, int] = {}
    for name, row in ports.items():
        bits = row.get("bits") if isinstance(row, Mapping) else None
        if isinstance(name, str) and isinstance(bits, list):
            widths[name] = len(bits)
    side_bits = {side: 0 for side in "NSEW"}
    config = load_design_config(Path(str(baseline["config_path"])))
    pin_ref = config.raw.get("IO_PIN_ORDER_CFG")
    pin_order_sha256: str | None = None
    if isinstance(pin_ref, str):
        relative = pin_ref.removeprefix("dir::")
        snapshot_root = Path(str(baseline["input_snapshot_dir"]))
        pin_path = canonical_regular_file(
            snapshot_root / relative,
            root=snapshot_root,
            label="I/O pin order",
            minimum_size=1,
            maximum_size=1024 * 1024,
        )
        pin_order_sha256 = file_sha256(pin_path)
        side_bits = _side_pin_bits_from_pin_order(
            widths, pin_path.read_text(encoding="utf-8")
        )
    else:
        for index in range(sum(widths.values())):
            side_bits["NSEW"[index % 4]] += 1
    cells = module.get("cells") if isinstance(module, Mapping) else None
    clocked = bool(config.raw.get("CLOCK_PORT")) or any(
        "ff" in str(row.get("type", "")).casefold()
        for row in (cells.values() if isinstance(cells, Mapping) else ())
        if isinstance(row, Mapping)
    )
    inputs = PlanningInputs(
        mapped_area_um2=float(mapped_area),
        port_bits=sum(widths.values()),
        side_pin_bits=side_bits,
        clocked=clocked,
    )
    evidence = {
        "lane_run_id": baseline["run_id"],
        "last_completed_step": baseline["last_completed_step"],
        "state_out": state_path,
        "state_out_sha256": file_sha256(Path(state_path)),
        "mapped_area_um2": float(mapped_area),
        "port_bits": inputs.port_bits,
        "side_pin_bits": side_bits,
        "pin_order_sha256": pin_order_sha256,
        "io_lock_sha256": canonical_sha256(
            {
                "schema_version": 1,
                "pin_order_sha256": pin_order_sha256,
                "side_pin_bits": side_bits,
                "port_widths": dict(sorted(widths.items())),
            }
        ),
        "clocked": clocked,
    }
    return inputs, evidence


def _candidate_overrides(candidate: Mapping[str, Any]) -> list[str]:
    return [
        'FP_SIZING="absolute"',
        "DIE_AREA=" + json.dumps(candidate["requested_die_area"], separators=(",", ":")),
        "CORE_AREA=" + json.dumps(candidate["requested_core_area"], separators=(",", ":")),
    ]


def _safe_file_sha256(path: object, *, root: Path, label: str) -> str | None:
    if not isinstance(path, str):
        return None
    try:
        safe = canonical_regular_file(
            Path(path),
            root=root,
            label=label,
            minimum_size=1,
            maximum_size=128 * 1024 * 1024,
        )
    except ValueError:
        return None
    return file_sha256(safe)


def _safe_file_text(path: object, *, root: Path, label: str) -> str | None:
    if not isinstance(path, str):
        return None
    try:
        safe = canonical_regular_file(
            Path(path),
            root=root,
            label=label,
            minimum_size=1,
            maximum_size=1024 * 1024,
        )
        return safe.read_text(encoding="utf-8")
    except (OSError, UnicodeError, ValueError):
        return None


def _probe_step(lane: Mapping[str, Any], endpoint: str) -> Mapping[str, Any] | None:
    history = lane.get("step_history")
    if not isinstance(history, list):
        return None
    rows = [row for row in history if isinstance(row, Mapping) and row.get("id") == endpoint]
    return rows[-1] if rows else None


def _tool_script_identity(
    lane: Mapping[str, Any], command_argv: list[str]
) -> dict[str, Any] | None:
    """Discover the actual pinned script from COMMANDS and hash it in-image."""

    try:
        script = command_argv[command_argv.index("-python") + 1]
    except (IndexError, ValueError):
        return None
    image = lane.get("runtime_image_ref")
    if (
        not isinstance(script, str)
        or not script.startswith("/")
        or not isinstance(image, str)
        or not image
    ):
        return None
    command = [
        "docker",
        "run",
        "--rm",
        "--init",
        "--network",
        "none",
        "--read-only",
        "--cap-drop",
        "ALL",
        "--security-opt",
        "no-new-privileges",
        "--user",
        f"{os.getuid()}:{os.getgid()}",
        "--tmpfs",
        "/tmp:rw,nosuid,nodev,size=16m",
        "-e",
        "HOME=/tmp",
        "--entrypoint",
        "sha256sum",
        image,
        script,
    ]
    try:
        completed = subprocess.run(
            command,
            capture_output=True,
            text=True,
            timeout=30,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    match = re.fullmatch(r"([0-9a-f]{64})\s+.+\n?", completed.stdout)
    if completed.returncode != 0 or match is None:
        return None
    return {
        "container_image": image,
        "script_path": script,
        "script_sha256": match.group(1),
        "discovery_argv": command,
    }


def _io_slot_evidence(
    log_text: str,
    *,
    fixed_headroom: int,
    headroom_percent: float,
    expected_side_pin_bits: Mapping[str, int],
) -> dict[str, Any] | None:
    """Extract the pinned io_place.py's actual slot accounting from its log."""

    side: str | None = None
    rows: dict[str, dict[str, int]] = {}
    capacity_failures: dict[str, dict[str, int]] = {}
    for raw in log_text.splitlines():
        line = raw.strip()
        capacity_error = IO_CAPACITY_ERROR_RE.search(line)
        if capacity_error:
            capacity_failures[capacity_error.group(1)] = {
                "demand": int(capacity_error.group(2)),
                "legal_slots": int(capacity_error.group(3)),
            }
        header = IO_SIDE_HEADER_RE.fullmatch(line)
        if header:
            side = header.group(1)
            rows[side] = {}
            continue
        match = IO_COUNT_RE.fullmatch(line)
        if side is not None and match:
            key = {
                "Actual pin count": "actual_pin_count",
                "Total pin count": "demand",
                "Tracks count": "legal_slots",
            }[match.group(1)]
            rows[side][key] = int(match.group(2))
    sides: dict[str, dict[str, Any]] = {}
    for name in "NSEW":
        expected = expected_side_pin_bits.get(name)
        if isinstance(expected, bool) or not isinstance(expected, int) or expected < 0:
            return None
        failure = capacity_failures.get(name)
        if failure is not None:
            demand = failure["demand"]
            if demand != expected:
                return None
            capacity = failure["legal_slots"]
            headroom = max(
                fixed_headroom, math.ceil(headroom_percent * demand / 100.0)
            )
            required = demand + headroom
            sides[name] = {
                "actual_pin_count": demand,
                "demand": demand,
                "legal_slots": capacity,
                "required_headroom_slots": headroom,
                "required_slots": required,
                "headroom_slots": capacity - demand,
                "slot_deficit": required - capacity,
                "status": "capacity_insufficient",
            }
            continue
        row = rows.get(name, {})
        # io_place.py returns before printing a side that has no requested pins.
        # A side with pins but no record is either an exact-capacity branch or
        # incomplete evidence, neither of which proves the configured headroom.
        if not row and expected == 0:
            sides[name] = {
                "actual_pin_count": 0,
                "demand": 0,
                "legal_slots": None,
                "required_headroom_slots": 0,
                "required_slots": 0,
                "headroom_slots": None,
                "status": "not_applicable",
            }
            continue
        if not row and capacity_failures:
            headroom = max(
                fixed_headroom, math.ceil(headroom_percent * expected / 100.0)
            )
            sides[name] = {
                "actual_pin_count": None,
                "demand": expected,
                "legal_slots": None,
                "required_headroom_slots": headroom,
                "required_slots": expected + headroom,
                "headroom_slots": None,
                "slot_deficit": None,
                "status": "not_evaluated",
            }
            continue
        if set(row) != {"actual_pin_count", "demand", "legal_slots"}:
            return None
        demand = row["demand"]
        if demand != expected:
            return None
        capacity = row["legal_slots"]
        headroom = max(fixed_headroom, math.ceil(headroom_percent * demand / 100.0))
        required = demand + headroom
        sides[name] = {
            **row,
            "required_headroom_slots": headroom,
            "required_slots": required,
            "headroom_slots": capacity - demand,
            "slot_deficit": max(0, required - capacity),
            "status": "pass" if capacity >= required else "feasible_not_robust",
        }
    if any(row["status"] == "capacity_insufficient" for row in sides.values()):
        status = "capacity_insufficient"
    elif all(row["status"] in {"pass", "not_applicable"} for row in sides.values()):
        status = "pass"
    else:
        status = "feasible_not_robust"
    return {
        "status": status,
        "headroom_rule": {
            "fixed_slots": fixed_headroom,
            "percent": headroom_percent,
        },
        "sides": sides,
    }


def _probe_evidence(lane: Mapping[str, Any], endpoint: str) -> dict[str, Any]:
    run_dir = Path(str(lane["librelane_run_dir"]))
    step = _probe_step(lane, endpoint)
    if step is None:
        return {"status": "unavailable", "reason": "probe Step evidence is missing"}
    command_text = _safe_file_text(
        step.get("commands"), root=run_dir, label="planning probe COMMANDS"
    )
    if command_text is None:
        return {"status": "unavailable", "reason": "probe COMMANDS is unavailable"}
    try:
        command_argv = shlex.split(command_text.strip())
    except ValueError:
        return {"status": "unavailable", "reason": "probe COMMANDS is malformed"}
    if not command_argv:
        return {"status": "unavailable", "reason": "probe COMMANDS is empty"}
    planning_probe = lane.get("planning_probe")
    parent_root = (
        Path(str(planning_probe["parent_run_dir"]))
        if isinstance(planning_probe, Mapping)
        and isinstance(planning_probe.get("parent_run_dir"), str)
        else run_dir
    )
    input_odb = command_argv[-1]
    log_hashes = [
        {"path": str(path), "sha256": digest}
        for path in (step.get("logs") or [])
        if (digest := _safe_file_sha256(path, root=run_dir, label="planning probe log"))
        is not None
    ]
    artifacts = lane.get("artifacts")
    latest_state = artifacts.get("latest_state") if isinstance(artifacts, Mapping) else None
    latest_odb = artifacts.get("latest_odb") if isinstance(artifacts, Mapping) else None
    return {
        "status": "available",
        "endpoint": endpoint,
        "lane_run_id": lane.get("run_id"),
        "exit_code": lane.get("exit_code"),
        "last_completed_step": lane.get("last_completed_step"),
        "qor_maturity": (lane.get("qor") or {}).get("status")
        if isinstance(lane.get("qor"), Mapping)
        else "unavailable",
        "input_odb_path": input_odb,
        "input_odb_sha256": _safe_file_sha256(
            input_odb, root=parent_root, label="planning probe input ODB"
        ),
        "output_odb_path": latest_odb,
        "output_odb_sha256": _safe_file_sha256(
            latest_odb, root=run_dir, label="planning probe output ODB"
        ),
        "state_out_path": latest_state,
        "state_out_sha256": _safe_file_sha256(
            latest_state, root=run_dir, label="planning probe state_out"
        ),
        "commands_sha256": hashlib.sha256(command_text.encode("utf-8")).hexdigest(),
        "command_argv": command_argv,
        "logs": log_hashes,
        "tool": _tool_script_identity(lane, command_argv)
        if endpoint == "Odb.CustomIOPlacement"
        else None,
        "resource_usage": {
            "elapsed_seconds": lane.get("elapsed_seconds"),
            "runtime_path": step.get("runtime"),
            "runtime_sha256": _safe_file_sha256(
                step.get("runtime"), root=run_dir, label="planning probe runtime"
            ),
        },
    }


def _probe_result(
    candidate: Mapping[str, Any],
    lane: Mapping[str, Any],
    *,
    gate: str,
    maximum_utilization_pct: float | None = None,
    minimum_setup_wns_ns: float | None = None,
    io_geometry: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    expected = PROBE_ENDPOINTS[gate]
    passed = lane.get("status") == "completed" and lane.get("last_completed_step") == expected
    result = dict(candidate)
    result[gate] = "pass" if passed else "fail"
    prefix = PROBE_PREFIXES[gate]
    result[f"{prefix}_lane_run_id"] = lane.get("run_id")
    result[f"{prefix}_last_completed_step"] = lane.get("last_completed_step")
    result[f"{prefix}_failed_step"] = lane.get("failed_step")
    result[f"{prefix}_errors"] = list(lane.get("errors") or [])[-8:]
    evidence = _probe_evidence(lane, expected)
    result[f"{prefix}_evidence"] = evidence
    if gate == "io_preflight_gate":
        log_paths = [str(row["path"]) for row in evidence.get("logs", [])]
        logs = [
            _safe_file_text(
                path,
                root=Path(str(lane["librelane_run_dir"])),
                label="I/O preflight log",
            )
            for path in log_paths
        ]
        slots = _io_slot_evidence(
            "\n".join(text for text in logs if text is not None),
            fixed_headroom=int(io_geometry["io_headroom_fixed_slots"])
            if io_geometry is not None
            else 0,
            headroom_percent=float(io_geometry["io_headroom_percent"])
            if io_geometry is not None
            else 0.0,
            expected_side_pin_bits=candidate.get("expected_side_pin_bits", {}),
        )
        result[f"{prefix}_evidence"] = {**evidence, "slot_evidence": slots}
        if evidence.get("tool") is None or slots is None or slots["status"] != "pass":
            result[gate] = "fail"
            deficits = [] if slots is None else [
                f"{name} demand={row['demand']} legal={row['legal_slots']} "
                f"required={row['required_slots']} deficit={row['slot_deficit']}"
                for name, row in slots["sides"].items()
                if row["status"] in {"capacity_insufficient", "feasible_not_robust"}
            ]
            result[f"{prefix}_errors"] = [
                "exact I/O preflight evidence is unavailable or lacks required headroom"
                + (": " + "; ".join(deficits) if deficits else "")
            ]
    if passed:
        qor = lane.get("qor") if isinstance(lane.get("qor"), Mapping) else {}
        die = parse_bbox(qor.get("design__die__bbox"))
        core = parse_bbox(qor.get("design__core__bbox"))
        if die is None or core is None:
            result[gate] = "fail"
            result[f"{prefix}_errors"] = ["probe completed without die/core bbox evidence"]
        else:
            result["observed_die_bbox"] = die
            result["observed_core_bbox"] = core
            result["post_probe_utilization_pct"] = qor.get("utilization_pct")
            result["post_probe_metric_status"] = qor.get("status")
            congestion = [
                float(value)
                for key, value in qor.items()
                if isinstance(key, str)
                and ("congestion" in key.casefold() or "overflow" in key.casefold())
                and isinstance(value, (int, float))
                and not isinstance(value, bool)
                and math.isfinite(float(value))
                and float(value) >= 0
            ]
            result["congestion_score"] = max(congestion) if congestion else None
            result["congestion_status"] = "available" if congestion else "unavailable"
            if maximum_utilization_pct is not None:
                utilization = qor.get("utilization_pct")
                if (
                    isinstance(utilization, bool)
                    or not isinstance(utilization, (int, float))
                    or not math.isfinite(float(utilization))
                    or float(utilization) > maximum_utilization_pct
                ):
                    result[gate] = "fail"
                    result[f"{prefix}_errors"] = [
                        "robust probe utilization is unavailable or exceeds "
                        f"{maximum_utilization_pct}%"
                    ]
            if gate == "robustness_gate" and minimum_setup_wns_ns is not None:
                setup_wns = qor.get("setup_ws_ns")
                setup_tns = qor.get("setup_tns_ns")
                max_slew_violations = qor.get("max_slew_violation_count")
                max_cap_violations = qor.get("max_cap_violation_count")
                result["midpnr_setup_wns_ns"] = setup_wns
                result["midpnr_setup_tns_ns"] = setup_tns
                result["midpnr_max_slew_violation_count"] = max_slew_violations
                result["midpnr_max_cap_violation_count"] = max_cap_violations
                result["minimum_midpnr_setup_wns_ns"] = minimum_setup_wns_ns
                qualified = (
                    isinstance(setup_wns, (int, float))
                    and not isinstance(setup_wns, bool)
                    and math.isfinite(float(setup_wns))
                    and float(setup_wns) >= minimum_setup_wns_ns
                    and isinstance(max_slew_violations, (int, float))
                    and not isinstance(max_slew_violations, bool)
                    and float(max_slew_violations) == 0.0
                    and isinstance(max_cap_violations, (int, float))
                    and not isinstance(max_cap_violations, bool)
                    and float(max_cap_violations) == 0.0
                )
                result["timing_qualification_gate"] = (
                    "pass" if qualified else "fail"
                )
                result["timing_qualification_evidence"] = {
                    "schema": "lane.midpnr-timing-qualification/v1",
                    "endpoint": expected,
                    "setup_wns_ns": setup_wns,
                    "setup_tns_ns": setup_tns,
                    "max_slew_violation_count": max_slew_violations,
                    "max_cap_violation_count": max_cap_violations,
                    "minimum_setup_wns_ns": minimum_setup_wns_ns,
                    "status": "pass" if qualified else "fail",
                    "lane_run_id": lane.get("run_id"),
                    "metric_source": lane.get("metrics_path"),
                }
                if not qualified:
                    result[f"{prefix}_errors"] = list(
                        result.get(f"{prefix}_errors") or []
                    ) + [
                        "mid-PnR timing qualification failed: setup WNS must be at "
                        f"least {minimum_setup_wns_ns} ns and slew/cap violations "
                        "must both be zero"
                    ]
    return result


def _skipped_probe(
    candidate: Mapping[str, Any], *, gate: str, upstream_gate: str
) -> dict[str, Any]:
    result = dict(candidate)
    prefix = PROBE_PREFIXES[gate]
    result[gate] = "not_run"
    result[f"{prefix}_errors"] = [
        f"not run because prerequisite {upstream_gate} did not pass"
    ]
    result[f"{prefix}_evidence"] = {
        "status": "not_run",
        "reason": f"prerequisite {upstream_gate} did not pass",
    }
    return result


def _candidate_snapshot(
    evaluated: list[Mapping[str, Any]], candidates: list[Mapping[str, Any]]
) -> list[dict[str, Any]]:
    return [dict(row) for row in evaluated] + [
        dict(row) for row in candidates[len(evaluated) :]
    ]


def _adaptive_io_successor(
    candidate: Mapping[str, Any],
    *,
    policy: Mapping[str, Any],
    existing_candidates: list[Mapping[str, Any]],
    expansion_index: int,
) -> dict[str, Any] | None:
    """Create one deterministic geometry successor for exact I/O capacity loss."""

    evidence = candidate.get("io_preflight_evidence")
    slots = evidence.get("slot_evidence") if isinstance(evidence, Mapping) else None
    if not isinstance(slots, Mapping) or slots.get("status") not in {
        "capacity_insufficient",
        "feasible_not_robust",
    }:
        return None
    failing = [
        (side, row)
        for side, row in (slots.get("sides") or {}).items()
        if isinstance(side, str)
        and side in "NSEW"
        and isinstance(row, Mapping)
        and row.get("status") in {"capacity_insufficient", "feasible_not_robust"}
        and isinstance(row.get("required_slots"), int)
        and isinstance(row.get("legal_slots"), int)
        and row["required_slots"] > row["legal_slots"]
    ]
    if not failing:
        return None
    failing.sort(
        key=lambda item: (
            -int(item[1]["required_slots"] - item[1]["legal_slots"]),
            item[0],
        )
    )
    geometry = policy["geometry"]
    width = float(candidate["core_width_um"])
    height = float(candidate["core_height_um"])
    width_quantum = float(geometry["width_quantum_um"])
    height_quantum = float(geometry["height_quantum_um"])
    target_width = width
    target_height = height
    expanded_sides: list[str] = []
    for side, row in failing:
        legal = max(1, int(row["legal_slots"]))
        required = int(row["required_slots"])
        if side in "EW":
            quantum = height_quantum
            target = math.ceil((height * required / legal) / quantum) * quantum
            target_height = max(target_height, target + quantum)
        else:
            quantum = width_quantum
            target = math.ceil((width * required / legal) / quantum) * quantum
            target_width = max(target_width, target + quantum)
        expanded_sides.append(side)
    target_width = round(target_width, 9)
    target_height = round(target_height, 9)
    max_dimension = float(
        policy["search"].get("adaptive_max_dimension_um", 5000.0)
    )
    if target_width > max_dimension or target_height > max_dimension:
        return None
    margin = float(geometry["core_margin_um"])
    key = (target_width, target_height)
    if any(
        abs(float(row.get("core_width_um", -1)) - key[0]) < 1e-9
        and abs(float(row.get("core_height_um", -1)) - key[1]) < 1e-9
        for row in existing_candidates
    ):
        return None
    return {
        "candidate_id": "candidate-pending",
        "core_width_um": target_width,
        "core_height_um": target_height,
        "core_area_um2": round(target_width * target_height, 9),
        "aspect_ratio": round(target_height / target_width, 12),
        "requested_core_area": [
            margin,
            margin,
            round(margin + target_width, 9),
            round(margin + target_height, 9),
        ],
        "requested_die_area": [
            0.0,
            0.0,
            round(target_width + 2 * margin, 9),
            round(target_height + 2 * margin, 9),
        ],
        "scale_factor": None,
        "expansion_parent_id": str(candidate["candidate_id"]),
        "expansion_index": expansion_index,
        "expansion_reason": {
            "kind": "exact_io_capacity",
            "sides": expanded_sides,
            "deficits": {
                side: int(row["required_slots"] - row["legal_slots"])
                for side, row in failing
            },
        },
    }


def run_worker(plan_id: str) -> None:
    state = _worker_state(plan_id)
    policy = load_policy(Path(str(state["policy_path"])))
    poll_seconds = int(policy["search"]["poll_interval_seconds"])
    timeout = int(policy["search"]["candidate_timeout_seconds"])
    try:
        _check_stop(plan_id)
        baseline = start_run(
            Path(str(state["config_path"])),
            pdk_root=Path(str(state["pdk_root"])),
            image=str(state["image"]),
            to="OpenROAD.STAPrePNR",
            jobs=state.get("jobs"),
            allow_concurrent=False,
            workflow_id=plan_id,
            attempt_id="synthesis-baseline",
            idempotency_key=f"{plan_id}-synthesis-baseline",
            origin="physical-planner-baseline",
            visualization_enabled=False,
            claim_current=False,
        )
        _commit(
            plan_id,
            {"baseline_lane_run_id": baseline["run_id"], "active_lane_run_id": baseline["run_id"], "phase": "baseline_running"},
            event_type="baseline_started",
            message="synthesis baseline started",
            data={"lane_run_id": baseline["run_id"], "endpoint": "OpenROAD.STAPrePNR"},
        )
        baseline = _wait_lane(
            plan_id, baseline["run_id"], phase="baseline_running",
            timeout_seconds=timeout, poll_seconds=poll_seconds,
        )
        if baseline.get("status") != "completed" or baseline.get("last_completed_step") != "OpenROAD.STAPrePNR":
            raise RuntimeError("synthesis baseline did not reach OpenROAD.STAPrePNR")
        inputs, baseline_evidence = _planning_inputs(baseline)
        bounds = derive_lower_bounds(inputs, policy)
        reference_path = state.get("reference_geometry_lock_path")
        reference_document = (
            load_geometry_lock_document(Path(reference_path))
            if isinstance(reference_path, str)
            else None
        )
        if reference_document is not None:
            expected_identity = {
                "pdk": baseline["pdk"],
                "pdk_revision": baseline["pdk_revision"],
                "pdk_integrity_sha256": baseline["pdk_integrity_sha256"],
                "image_digest": baseline["image_digest"],
                "librelane_version": baseline["librelane_version"],
                "flow_registry_version": baseline["flow_registry_version"],
                "policy_sha256": str(state["policy_sha256"]),
                "io_lock_sha256": baseline_evidence["io_lock_sha256"],
            }
            drift = [key for key, value in expected_identity.items() if reference_document.get(key) != value]
            if drift:
                raise RuntimeError(
                    "reference geometry tool/policy identity drifted: " + ", ".join(drift)
                )
            lock = reference_document["geometry_lock"]
            core = lock["core_area"]
            width = float(core[2]) - float(core[0])
            height = float(core[3]) - float(core[1])
            candidates = [{
                "candidate_id": "candidate-001",
                "core_width_um": width,
                "core_height_um": height,
                "core_area_um2": width * height,
                "aspect_ratio": height / width,
                "requested_core_area": list(lock["core_area"]),
                "requested_die_area": list(lock["die_area"]),
                "scale_factor": None,
                "reference_source_plan_sha256": reference_document["source_plan_sha256"],
                "reference_geometry_lock_sha256": reference_document["geometry_lock_sha256"],
            }]
        else:
            candidates = generate_candidates(bounds, policy)
        candidate_domain_sha256 = canonical_sha256(
            candidate_domain_projection(candidates)
        )
        _commit(
            plan_id,
            {
                "phase": "candidate_probing",
                "candidate_count": len(candidates),
                "candidates": candidates,
                "candidate_domain_sha256": candidate_domain_sha256,
                "synthesis_baseline": baseline_evidence,
                "lower_bounds": bounds,
            },
            event_type="candidates_derived",
            message="deterministic physical planning candidates derived",
            data={
                "candidate_count": len(candidates),
                "candidate_domain_sha256": candidate_domain_sha256,
                "lower_bounds": bounds,
            },
        )
        evaluated: list[dict[str, Any]] = []
        expansion_count = 0
        search_policy = policy["search"]
        maximum_total_candidates = int(
            search_policy.get("maximum_total_candidates", len(candidates))
        )
        maximum_adaptive_expansions = int(
            search_policy.get("maximum_adaptive_expansions", 0)
        )
        for raw_candidate in candidates:
            _check_stop(plan_id)
            candidate = {
                **raw_candidate,
                "expected_side_pin_bits": dict(inputs.side_pin_bits),
            }
            evaluated.append(candidate)
            preceding: str | None = None
            for gate, endpoint, prefix in PROBE_SPECS:
                if preceding is not None and candidate.get(preceding) != "pass":
                    candidate = _skipped_probe(
                        candidate, gate=gate, upstream_gate=preceding
                    )
                    evaluated[-1] = candidate
                    _commit(
                        plan_id,
                        {"candidates": _candidate_snapshot(evaluated, candidates)},
                        event_type="candidate_probe_skipped",
                        message=f"{candidate['candidate_id']} {prefix} probe not run",
                        data={
                            "candidate_id": candidate["candidate_id"],
                            "gate": gate,
                            "upstream_gate": preceding,
                        },
                    )
                    preceding = gate
                    continue
                policy_endpoint = {
                    "pdn_gate": "pdn_probe_to",
                    "io_preflight_gate": "io_preflight_to",
                    "detailed_placement_gate": "first_probe_to",
                    "robustness_gate": "robust_probe_to",
                }[gate]
                probe = start_planning_probe(
                    baseline["run_id"],
                    plan_id=plan_id,
                    candidate_id=f"{candidate['candidate_id']}-{prefix}",
                    policy_sha256=str(state["policy_sha256"]),
                    overrides=_candidate_overrides(candidate),
                    to=str(policy["search"][policy_endpoint]),
                    jobs=state.get("jobs"),
                )
                lane = _wait_lane(
                    plan_id,
                    probe["run_id"],
                    phase=f"{prefix}_probe",
                    timeout_seconds=timeout,
                    poll_seconds=poll_seconds,
                )
                candidate = _probe_result(
                    candidate,
                    lane,
                    gate=gate,
                    maximum_utilization_pct=(
                        float(policy["maximum_post_probe_utilization_pct"])
                        if gate == "robustness_gate"
                        else None
                    ),
                    minimum_setup_wns_ns=(
                        float(policy["search"]["minimum_midpnr_setup_wns_ns"])
                        if gate == "robustness_gate"
                        and "minimum_midpnr_setup_wns_ns" in policy["search"]
                        else None
                    ),
                    io_geometry=policy["geometry"]
                    if gate == "io_preflight_gate"
                    else None,
                )
                evaluated[-1] = candidate
                _commit(
                    plan_id,
                    {"candidates": _candidate_snapshot(evaluated, candidates)},
                    event_type="candidate_probe_completed",
                    message=(
                        f"{candidate['candidate_id']} {prefix} probe "
                        f"{candidate[gate]}"
                    ),
                    data={
                        "candidate_id": candidate["candidate_id"],
                        "gate": gate,
                        "endpoint": endpoint,
                        "lane_run_id": probe["run_id"],
                        "failed_step": candidate.get(f"{prefix}_failed_step"),
                    },
                )
                preceding = gate
            _commit(
                plan_id,
                {
                    "candidates": _candidate_snapshot(evaluated, candidates),
                    "completed_candidate_count": len(evaluated),
                },
                event_type="candidate_evaluation_completed",
                message=f"{candidate['candidate_id']} completed all applicable probes",
                data={"candidate_id": candidate["candidate_id"]},
            )
            if (
                expansion_count < maximum_adaptive_expansions
                and len(candidates) < maximum_total_candidates
            ):
                successor = _adaptive_io_successor(
                    candidate,
                    policy=policy,
                    existing_candidates=candidates,
                    expansion_index=expansion_count + 1,
                )
                if successor is not None:
                    successor["candidate_id"] = f"candidate-{len(candidates) + 1:03d}"
                    candidates.append(successor)
                    expansion_count += 1
                    candidate_domain_sha256 = canonical_sha256(
                        candidate_domain_projection(candidates)
                    )
                    _commit(
                        plan_id,
                        {
                            "candidates": _candidate_snapshot(evaluated, candidates),
                            "candidate_count": len(candidates),
                            "candidate_domain_sha256": candidate_domain_sha256,
                            "adaptive_expansion_count": expansion_count,
                        },
                        event_type="candidate_expanded_for_io_capacity",
                        message=(
                            f"{candidate['candidate_id']} generated a deterministic "
                            f"I/O-capacity successor {successor['candidate_id']}"
                        ),
                        data={
                            "parent_candidate_id": candidate["candidate_id"],
                            "successor_candidate_id": successor["candidate_id"],
                            "expansion_reason": successor["expansion_reason"],
                            "candidate_domain_sha256": candidate_domain_sha256,
                        },
                    )
        try:
            selected = select_minimum_robust_candidate(evaluated)
        except ValueError as exc:
            capacity_rows = []
            for row in evaluated:
                evidence = row.get("io_preflight_evidence")
                slots = evidence.get("slot_evidence") if isinstance(evidence, Mapping) else None
                if isinstance(slots, Mapping) and slots.get("status") == "capacity_insufficient":
                    capacity_rows.append(
                        {
                            "candidate_id": row.get("candidate_id"),
                            "sides": slots.get("sides"),
                        }
                    )
            if capacity_rows and all(
                row.get("io_preflight_gate") == "fail" for row in evaluated
            ):
                raise PhysicalPlanningFailure(
                    "IO_CAPACITY_EXHAUSTED",
                    "no candidate satisfied exact I/O capacity within the bounded adaptive search",
                    details={
                        "candidate_count": len(evaluated),
                        "maximum_total_candidates": maximum_total_candidates,
                        "maximum_adaptive_expansions": maximum_adaptive_expansions,
                        "capacity_evidence": capacity_rows,
                    },
                ) from exc
            timing_rows = [
                {
                    "candidate_id": row.get("candidate_id"),
                    "core_area_um2": row.get("core_area_um2"),
                    "midpnr_setup_wns_ns": row.get("midpnr_setup_wns_ns"),
                    "minimum_midpnr_setup_wns_ns": row.get(
                        "minimum_midpnr_setup_wns_ns"
                    ),
                }
                for row in evaluated
                if row.get("robustness_gate") == "pass"
                and row.get("timing_qualification_gate") == "fail"
            ]
            robust_rows = [
                row for row in evaluated if row.get("robustness_gate") == "pass"
            ]
            if timing_rows and len(timing_rows) == len(robust_rows):
                raise PhysicalPlanningFailure(
                    "TIMING_QUALIFICATION_EXHAUSTED",
                    "no robust candidate met the calibrated mid-PnR timing safety margin",
                    details={
                        "candidate_count": len(evaluated),
                        "timing_evidence": timing_rows,
                    },
                ) from exc
            raise
        classification = minimum_geometry_classification(
            selected,
            evaluated,
            candidate_domain_sha256=candidate_domain_sha256,
            search_complete=True,
        )
        locked = geometry_lock(selected, classification=classification)
        if reference_document is not None:
            expected_lock = reference_document["geometry_lock"]
            if (
                locked["die_area"] != expected_lock["die_area"]
                or locked["core_area"] != expected_lock["core_area"]
                or locked["observed_die_bbox"] != expected_lock["observed_die_bbox"]
                or locked["observed_core_bbox"] != expected_lock["observed_core_bbox"]
            ):
                raise RuntimeError("reference-locked probe geometry drifted from the Human lock")
            locked = dict(expected_lock)
        plan = finalize_plan(
            {
                "publishable": bool(policy["publishable"]),
                "plan_id": plan_id,
                "created_at": state["created_at"],
                "config_sha256": baseline["config_sha256"],
                "input_bundle_sha256": baseline["input_bundle_sha256"],
                "pdk": baseline["pdk"],
                "pdk_revision": baseline["pdk_revision"],
                "pdk_integrity_sha256": baseline["pdk_integrity_sha256"],
                "image_digest": baseline["image_digest"],
                "librelane_version": baseline["librelane_version"],
                "flow_registry_version": baseline["flow_registry_version"],
                "policy": policy,
                "policy_sha256": canonical_sha256(policy),
                "synthesis_baseline": {**baseline_evidence, "lower_bounds": bounds},
                "candidates": evaluated,
                "candidate_domain_sha256": candidate_domain_sha256,
                "candidate_domain_count": len(candidates),
                "search_complete": True,
                "selected_candidate_id": selected["candidate_id"],
                "io_lock_sha256": baseline_evidence["io_lock_sha256"],
                "geometry_lock": locked,
            }
        )
        root, _, _ = _paths(plan_id)
        output = root / "physical-plan.json"
        atomic_write(output, json.dumps(plan, indent=2, sort_keys=True) + "\n")
        output.chmod(0o400)
        completion = {
            "physical_plan_path": str(output),
            "plan_sha256": plan["plan_sha256"],
            "geometry_lock_sha256": plan["geometry_lock_sha256"],
            "io_lock_sha256": plan["io_lock_sha256"],
            "publishable": plan["publishable"],
            "selected_candidate_id": plan["selected_candidate_id"],
            "candidates": evaluated,
        }
        final_launch = state.get("final_launch")
        if isinstance(final_launch, Mapping):
            _commit(
                plan_id,
                {**completion, "phase": "final_lane_starting", "active_lane_run_id": None},
                event_type="geometry_frozen",
                message="physical geometry was frozen; final Lane launch is starting",
                data={"plan_sha256": plan["plan_sha256"], "geometry_lock_sha256": plan["geometry_lock_sha256"], "io_lock_sha256": plan["io_lock_sha256"]},
            )
            final_lane = start_run(
                Path(str(state["config_path"])),
                pdk_root=Path(str(state["pdk_root"])),
                image=str(state["image"]),
                jobs=state.get("jobs"),
                to=final_launch.get("to"),
                workflow_id=str(final_launch["workflow_id"]),
                attempt_id=str(final_launch["attempt_id"]),
                idempotency_key=str(final_launch["idempotency_key"]),
                origin="rtl-closure",
                physical_plan_path=output,
            )
            completion["final_lane_run_id"] = final_lane["run_id"]
        _commit(
            plan_id,
            {
                **completion,
                "status": "completed",
                "phase": "completed",
                "finished_at": now_iso(),
                "active_lane_run_id": None,
            },
            event_type="planner_completed",
            message="physical planning completed and geometry was frozen",
            data={"plan_sha256": plan["plan_sha256"], "geometry_lock_sha256": plan["geometry_lock_sha256"], "io_lock_sha256": plan["io_lock_sha256"], "selected_candidate_id": plan["selected_candidate_id"], "publishable": plan["publishable"], "final_lane_run_id": completion.get("final_lane_run_id")},
        )
    except InterruptedError as exc:
        _commit(
            plan_id,
            {"status": "stopped", "phase": "stopped", "finished_at": now_iso(), "active_lane_run_id": None, "error": {"code": "PLANNER_STOPPED", "class": "control", "rtl_repairable": False, "message": str(exc)}},
            event_type="planner_stopped", message=str(exc),
        )
    except PhysicalPlanningFailure as exc:
        _commit(
            plan_id,
            {
                "status": "failed",
                "phase": "failed",
                "finished_at": now_iso(),
                "active_lane_run_id": None,
                "error": {
                    "code": exc.code,
                    "class": "physical_planning",
                    "rtl_repairable": False,
                    "message": str(exc),
                    "details": exc.details,
                },
            },
            event_type="planner_failed",
            message=str(exc),
            data={"error": str(exc), "code": exc.code, "details": exc.details},
        )
    except Exception as exc:
        _commit(
            plan_id,
            {"status": "failed", "phase": "failed", "finished_at": now_iso(), "active_lane_run_id": None, "error": {"code": "PHYSICAL_PLANNING_FAILED", "class": "physical_planning", "rtl_repairable": False, "message": str(exc)}},
            event_type="planner_failed", message="physical planning failed", data={"error": str(exc)},
        )


def stop_physical_planning(plan_id: str | None = None) -> dict[str, Any]:
    state = load_planning_state(plan_id)
    if state["status"] in TERMINAL:
        return state
    active = state.get("active_lane_run_id")
    if isinstance(active, str):
        stop_run(active)
    state = _commit(
        state["plan_id"],
        {
            "stop_requested": True,
            "status": "stopped",
            "phase": "stopped",
            "finished_at": now_iso(),
            "active_lane_run_id": None,
            "error": {
                "code": "PLANNER_STOPPED",
                "class": "control",
                "rtl_repairable": False,
                "message": "physical planning was stopped by request",
            },
        },
        event_type="planner_stopped",
        message="physical planning was stopped by request",
    )
    pid = state.get("pid")
    if pid_identity_alive(pid, state.get("pid_start_time")):
        os.killpg(int(pid), signal.SIGTERM)
    return state


def export_planning_geometry(plan_id: str | None = None) -> dict[str, Any]:
    state = load_planning_state(plan_id)
    if state.get("status") != "completed" or not isinstance(
        state.get("physical_plan_path"), str
    ):
        raise RuntimeError("physical planning must complete before geometry export")
    return export_geometry_lock_document(load_plan(Path(state["physical_plan_path"])))


def main() -> int:
    import argparse

    parser = argparse.ArgumentParser()
    commands = parser.add_subparsers(dest="command", required=True)
    worker = commands.add_parser("worker")
    worker.add_argument("--plan-id", required=True)
    args = parser.parse_args()
    if args.command == "worker":
        run_worker(args.plan_id)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
