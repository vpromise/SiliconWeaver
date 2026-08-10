"""Persistent state for managed LibreLane runs."""

from __future__ import annotations

import hashlib
import json
import os
import re
import time
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path
from typing import Any, Iterable, Iterator, Mapping

import fcntl

from .linux_process import proc_identity


STATE_ROOT = Path(
    os.environ.get("PI_LANE_STATE_DIR", "~/.local/state/lane-agent")
).expanduser()
CURRENT_FILE = STATE_ROOT / "current"
RUN_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,254}$")
EVENT_SCHEMA_VERSION = 3
RUN_STATE_SCHEMA_VERSION = 6
MAX_EVENT_JOURNAL_BYTES = 64 * 1024 * 1024
TERMINAL_RUN_STATUSES = frozenset({"completed", "failed", "incomplete", "stopped"})
TERMINAL_OUTCOME_KEYS = (
    "schema_version",
    "run_id",
    "workflow_id",
    "attempt_id",
    "idempotency_key",
    "request_fingerprint",
    "origin",
    "controller_dir",
    "state_file",
    "controller_events_file",
    "status",
    "process_status",
    "exit_code",
    "failure_kind",
    "failure",
    "delivery_gate",
    "delivery_gate_reasons",
    "flow_scope",
    "expected_final_step",
    "endpoint_reached",
    "last_completed_step",
    "completed_step_count",
    "started_step_count",
    "failed_step",
    "metrics_path",
    "qor",
    "mapped_equivalence",
    "assurance",
    "config_path",
    "config_sha256",
    "input_snapshot_dir",
    "input_manifest_path",
    "input_manifest",
    "input_bundle_sha256",
    "input_bundle_attested",
    "design_dir",
    "design_name",
    "flow",
    "flow_registry_version",
    "tag",
    "librelane_run_dir",
    "pid",
    "pid_start_time",
    "container_name",
    "image",
    "image_digest",
    "runtime_image_ref",
    "librelane_version",
    "container_identity",
    "config_preflight",
    "pdk",
    "pdk_root",
    "pdk_revision",
    "pdk_integrity_sha256",
    "pdk_integrity_manifest",
    "overrides",
    "from",
    "to",
    "jobs",
    "run_eqy_requested",
    "eqy_script_path",
    "eqy_script_sha256",
    "eqy_gate_tcl_path",
    "eqy_gate_tcl_sha256",
    "eqy_tool_path",
    "eqy_tool_sha256",
    "parent_run_id",
    "initial_state",
    "initial_state_sha256",
    "initial_state_snapshot",
    "resume",
    "artifacts",
    "canonical_gds",
    "runtime_identity_attested",
    "runtime_environment_attestation",
    "delivery_attestation",
)


class StateConflictError(RuntimeError):
    """Raised when a stale writer attempts to replace a newer run snapshot."""


def now_iso() -> str:
    return datetime.now().astimezone().isoformat(timespec="seconds")


def atomic_write(path: Path, content: str, mode: int = 0o600) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_name(f".{path.name}.tmp.{os.getpid()}.{time.time_ns()}")
    try:
        descriptor = os.open(temp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, mode)
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temp, path)
        directory = os.open(path.parent, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
    finally:
        temp.unlink(missing_ok=True)


def _state_path(state: Mapping[str, Any]) -> Path:
    path = state.get("state_file")
    if not isinstance(path, str) or not path:
        raise ValueError("managed run state has no state_file")
    return Path(path)


@contextmanager
def run_lock(state_file: Path) -> Iterator[None]:
    """Serialize all writers for one managed run."""
    lock_path = state_file.with_name("state.lock")
    lock_path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    descriptor = os.open(lock_path, os.O_RDWR | os.O_CREAT, 0o600)
    with os.fdopen(descriptor, "a+") as handle:
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


def _read_state_file(path: Path) -> dict[str, Any] | None:
    if not path.is_file():
        return None
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise RuntimeError(f"managed state is not a JSON object: {path}")
    return value


def managed_state_revision(state: Mapping[str, Any] | None) -> int:
    """Return the controller CAS revision without consuming domain revisions."""

    if state is None:
        return 0
    value = state.get("state_revision", 0)
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise RuntimeError("managed state revision is malformed")
    return value


def _has_managed_state_revision(state: Mapping[str, Any]) -> bool:
    return "state_revision" in state


def _validate_lane_state_contract(state: Mapping[str, Any]) -> None:
    """Reject non-schema-6 Lane projections without affecting installer records."""

    if state.get("kind") in {"image_install", "pdk_install"}:
        return
    if state.get("schema_version") != RUN_STATE_SCHEMA_VERSION:
        raise RuntimeError(
            "managed run state schema is unsupported; "
            f"expected {RUN_STATE_SCHEMA_VERSION}"
        )
    if "revision" in state:
        raise RuntimeError(
            "managed run state uses unsupported revision; state_revision is required"
        )
    if "state_revision" not in state:
        raise RuntimeError("managed run state has no state_revision")
    managed_state_revision(state)


def _event_journal_path(state: Mapping[str, Any]) -> Path:
    expected = _state_path(state).with_name("controller-events.jsonl")
    configured = state.get("controller_events_file")
    if isinstance(configured, str) and configured:
        if Path(configured) != expected:
            raise ValueError("controller event journal must be beside state.json")
    return expected


def _canonical_event_bytes(event: Mapping[str, Any]) -> bytes:
    return json.dumps(
        event, sort_keys=True, separators=(",", ":"), ensure_ascii=True
    ).encode("utf-8")


def _read_journal_tail(
    path: Path,
) -> tuple[int, str | None, str | None, set[str], dict[str, Any] | None]:
    sequence = 0
    digest: str | None = None
    terminal_event_id: str | None = None
    terminal_event: dict[str, Any] | None = None
    dedupe_keys: set[str] = set()
    if path.is_symlink():
        raise RuntimeError("controller event journal must not be a symlink")
    if not path.is_file():
        return sequence, digest, terminal_event_id, dedupe_keys, terminal_event
    if path.stat().st_size > MAX_EVENT_JOURNAL_BYTES:
        raise RuntimeError("controller event journal exceeds the 64 MiB safety limit")
    with path.open("rb") as handle:
        for raw_line in handle:
            try:
                value = json.loads(raw_line.decode("utf-8", errors="strict"))
            except (UnicodeDecodeError, json.JSONDecodeError) as exc:
                raise RuntimeError("controller event journal is malformed") from exc
            if not isinstance(value, dict):
                raise RuntimeError("controller event journal contains a non-object")
            if value.get("schema_version") != EVENT_SCHEMA_VERSION:
                raise RuntimeError("controller event journal schema is unsupported")
            current = value.get("sequence")
            if isinstance(current, bool) or not isinstance(current, int) or current != sequence + 1:
                raise RuntimeError("controller event journal sequence is not contiguous")
            recorded_digest = value.get("event_sha256")
            event_without_digest = dict(value)
            event_without_digest.pop("event_sha256", None)
            actual_digest = hashlib.sha256(
                _canonical_event_bytes(event_without_digest)
            ).hexdigest()
            if recorded_digest != actual_digest:
                raise RuntimeError("controller event journal hash verification failed")
            if value.get("previous_event_sha256") != digest:
                raise RuntimeError("controller event journal hash chain is broken")
            sequence = current
            digest = actual_digest
            if value.get("terminal") is True:
                if terminal_event_id is not None:
                    raise RuntimeError("controller event journal has multiple terminal events")
                terminal_event_id = str(value.get("event_id") or "") or None
                terminal_event = dict(value)
            dedupe_key = value.get("dedupe_key")
            if isinstance(dedupe_key, str) and dedupe_key:
                dedupe_keys.add(dedupe_key)
    return sequence, digest, terminal_event_id, dedupe_keys, terminal_event


def read_terminal_event(state: Mapping[str, Any]) -> dict[str, Any]:
    """Verify the complete event chain and return its unique terminal record."""

    _, _, terminal_event_id, _, terminal_event = _read_journal_tail(
        _event_journal_path(state)
    )
    expected_event_id = state.get("terminal_event_id")
    if (
        not isinstance(expected_event_id, str)
        or not expected_event_id
        or terminal_event_id != expected_event_id
        or terminal_event is None
    ):
        raise ValueError("controller terminal event identity is missing or mismatched")
    return terminal_event


def _append_event_unlocked(
    state: dict[str, Any], event: Mapping[str, Any], *, state_revision: int
) -> dict[str, Any] | None:
    path = _event_journal_path(state)
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    (
        sequence,
        previous_digest,
        terminal_event_id,
        dedupe_keys,
        _,
    ) = _read_journal_tail(path)
    terminal = event.get("terminal") is True
    dedupe_key = event.get("dedupe_key")
    if (terminal and terminal_event_id is not None) or (
        isinstance(dedupe_key, str) and dedupe_key in dedupe_keys
    ):
        if terminal_event_id is not None:
            state["terminal_event_id"] = terminal_event_id
        state["event_sequence"] = sequence
        state["last_event_id"] = f"{state.get('run_id', '')}:{sequence}"
        state["last_event_sha256"] = previous_digest
        return None

    run_id = str(state.get("run_id") or "")
    data = event.get("data") or {}
    if not isinstance(data, Mapping):
        raise ValueError("controller event data must be an object")
    failure = event.get("failure")
    failure_message = (
        failure.get("message") if isinstance(failure, Mapping) else None
    )
    occurred_at = now_iso()
    row: dict[str, Any] = {
        "schema_version": EVENT_SCHEMA_VERSION,
        "event_id": f"{run_id}:{sequence + 1}",
        "sequence": sequence + 1,
        "occurred_at": occurred_at,
        "type": str(event["type"]),
        "source": "pi_lane_controller",
        "run_id": run_id,
        "workflow_id": state.get("workflow_id") or run_id,
        "attempt_id": state.get("attempt_id") or run_id,
        "origin": state.get("origin") or {"component": "lane-agent"},
        "state_revision": state_revision,
        "phase": event.get("phase") or state.get("coarse_phase") or "startup",
        "status": event.get("status") or state.get("status"),
        "severity": event.get("severity") or "info",
        "message": event.get("message") or str(event["type"]),
        "data": dict(data),
        "failure": failure,
        "ordinal": data.get("ordinal"),
        "step": data.get("step_id"),
        "directory": data.get("directory"),
        "commands": data.get("commands"),
        "logs": data.get("logs") or [],
        "image": data.get("image"),
        "error": data.get("error") or failure_message,
        "attempt": data.get("attempt"),
        "terminal": terminal,
        "dedupe_key": dedupe_key,
        "previous_event_sha256": previous_digest,
    }
    row["event_sha256"] = hashlib.sha256(_canonical_event_bytes(row)).hexdigest()
    raw = _canonical_event_bytes(row) + b"\n"
    descriptor = os.open(
        path,
        os.O_WRONLY
        | os.O_APPEND
        | os.O_CREAT
        | getattr(os, "O_NOFOLLOW", 0),
        0o600,
    )
    try:
        written = os.write(descriptor, raw)
        if written != len(raw):
            raise OSError("short write to controller event journal")
        os.fsync(descriptor)
    finally:
        os.close(descriptor)
    state["controller_events_file"] = str(path)
    state["event_schema_version"] = EVENT_SCHEMA_VERSION
    state["event_sequence"] = row["sequence"]
    state["last_event_id"] = row["event_id"]
    state["last_event_sha256"] = row["event_sha256"]
    if terminal_event_id is not None:
        state["terminal_event_id"] = terminal_event_id
    if terminal:
        state["terminal_event_id"] = row["event_id"]
        state["terminal_event_type"] = row["type"]
    return row


def commit_state(
    state: dict[str, Any],
    *,
    expected_revision: int | None = None,
    events: Iterable[Mapping[str, Any]] = (),
    locked: bool = False,
) -> dict[str, Any]:
    """CAS-write a state projection and its controller events under one run lock."""
    path = _state_path(state)

    def commit() -> dict[str, Any]:
        current = _read_state_file(path)
        _validate_lane_state_contract(state)
        if current is not None:
            _validate_lane_state_contract(current)
        current_revision = managed_state_revision(current)
        expected = (
            expected_revision
            if expected_revision is not None
            else (
                managed_state_revision(state)
                if _has_managed_state_revision(state)
                else current_revision
            )
        )
        if expected != current_revision:
            raise StateConflictError(
                f"stale managed state revision: expected {expected}, found {current_revision}"
            )
        terminal_event_id: str | None = None
        if current is not None and current.get("status") in TERMINAL_RUN_STATUSES:
            recorded_terminal = current.get("terminal_event_id")
            terminal_event_id = (
                recorded_terminal
                if isinstance(recorded_terminal, str) and recorded_terminal
                else _read_journal_tail(_event_journal_path(current))[2]
            )
        if terminal_event_id is not None:
            def terminal_value(source: Mapping[str, Any], key: str) -> Any:
                value = source.get(key)
                if key == "artifacts" and isinstance(value, Mapping):
                    value = dict(value)
                    value.pop("visualizations", None)
                return value

            changed = [
                key
                for key in TERMINAL_OUTCOME_KEYS
                if terminal_value(state, key) != terminal_value(current, key)
            ]
            integrity_degradation = (
                current.get("status") == "completed"
                and state.get("status") == "incomplete"
                and state.get("failure_kind") == "release_integrity"
                and state.get("delivery_gate") == "incomplete"
                and isinstance(state.get("release_integrity"), Mapping)
                and state["release_integrity"].get("status") == "degraded"
                and set(changed)
                <= {
                    "status",
                    "failure_kind",
                    "failure",
                    "delivery_gate",
                    "delivery_gate_reasons",
                }
            )
            if changed and not integrity_degradation:
                raise StateConflictError(
                    "terminal managed state outcome is immutable; changed fields: "
                    + ", ".join(changed)
                )
        next_revision = current_revision + 1
        for event in events:
            _append_event_unlocked(state, event, state_revision=next_revision)
        state["state_revision"] = next_revision
        state["updated_at"] = now_iso()
        atomic_write(path, json.dumps(state, indent=2, sort_keys=True) + "\n")
        return state

    if locked:
        return commit()
    with run_lock(path):
        return commit()


def save_state(state: dict[str, Any]) -> None:
    """Commit a revisioned state projection under its run lock."""
    commit_state(state)


def set_current(run_id: str) -> None:
    STATE_ROOT.mkdir(parents=True, exist_ok=True, mode=0o700)
    atomic_write(CURRENT_FILE, run_id + "\n")


def resolve_run_id(run_id: str | None) -> str:
    value = run_id
    if value is None:
        if not CURRENT_FILE.is_file():
            raise RuntimeError("no managed LibreLane run exists")
        value = CURRENT_FILE.read_text(encoding="utf-8").strip()
    if not value:
        raise RuntimeError("the current LibreLane run pointer is empty")
    if not RUN_ID_RE.fullmatch(value) or value in {".", ".."}:
        raise RuntimeError("invalid managed run id")
    return value


def load_state(run_id: str | None = None) -> dict[str, Any]:
    resolved = resolve_run_id(run_id)
    path = STATE_ROOT / resolved / "state.json"
    if not path.is_file():
        raise RuntimeError(f"run does not exist: {resolved}")
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise RuntimeError(f"managed state is not a JSON object: {path}")
    _validate_lane_state_contract(value)
    if value.get("run_id") != resolved:
        raise RuntimeError("managed run state identity does not match its directory")
    return value


def make_run_id(design_name: str) -> str:
    STATE_ROOT.mkdir(parents=True, exist_ok=True, mode=0o700)
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    slug = re.sub(r"[^A-Za-z0-9_.-]+", "-", design_name).strip("-").lower()
    base = f"{stamp}-{slug}"
    candidate = base
    counter = 2
    while (STATE_ROOT / candidate).exists():
        candidate = f"{base}-{counter}"
        counter += 1
    return candidate


def pid_alive(pid: int | None) -> bool:
    if isinstance(pid, bool) or not isinstance(pid, int) or pid <= 0:
        return False
    try:
        identity = proc_identity(pid)
        if identity is None or identity[0] == "Z":
            return False
        os.kill(pid, 0)
        return True
    except OSError:
        return False


def pid_start_time(pid: int | None) -> int | None:
    """Return Linux /proc start ticks so PID reuse cannot impersonate a worker."""
    identity = proc_identity(pid)
    return identity[1] if identity is not None else None


def pid_identity_alive(pid: int | None, expected_start_time: int | None) -> bool:
    if (
        isinstance(expected_start_time, bool)
        or not isinstance(expected_start_time, int)
        or expected_start_time <= 0
    ):
        return False
    if not pid_alive(pid):
        return False
    return pid_start_time(pid) == expected_start_time


def read_event_journal(
    state: Mapping[str, Any],
    *,
    after_sequence: int = 0,
    limit: int = 256,
    generation: str | None = None,
    cursor_sha256: str | None = None,
) -> dict[str, Any]:
    """Read a prefix-hash protected window from the authoritative journal."""
    if (
        isinstance(after_sequence, bool)
        or not isinstance(after_sequence, int)
        or after_sequence < 0
    ):
        raise ValueError("after_sequence must be a non-negative integer")
    if (
        isinstance(limit, bool)
        or not isinstance(limit, int)
        or not 1 <= limit <= 1000
    ):
        raise ValueError("event limit must be from 1 to 1000")
    if after_sequence > 0 and (generation is None or cursor_sha256 is None):
        raise ValueError("event continuation requires both generation and cursor_sha256")
    if after_sequence == 0 and (generation is None) != (cursor_sha256 is None):
        raise ValueError("event generation and cursor_sha256 must be supplied together")
    if generation is not None and not re.fullmatch(
        r"[0-9a-f]+:[0-9a-f]+", generation
    ):
        raise ValueError("event generation is malformed")
    if cursor_sha256 is not None and not re.fullmatch(
        r"[0-9a-f]{64}", cursor_sha256
    ):
        raise ValueError("event cursor SHA-256 is malformed")

    path = _event_journal_path(state)
    empty_digest = hashlib.sha256(b"").hexdigest()
    if not path.exists():
        if after_sequence or generation is not None:
            raise ValueError("controller event journal disappeared after cursor creation")
        return {
            "events_file": str(path),
            "events": [],
            "after_sequence": 0,
            "next_after_sequence": 0,
            "has_more": False,
            "invalid_line_count": 0,
            "generation": None,
            "cursor_sha256": empty_digest,
        }
    if path.is_symlink() or not path.is_file():
        raise ValueError("controller event journal must be a regular non-symlink file")
    stat_result = path.stat()
    if stat_result.st_size > MAX_EVENT_JOURNAL_BYTES:
        raise ValueError("controller event journal exceeds the 64 MiB safety limit")
    actual_generation = f"{stat_result.st_dev:x}:{stat_result.st_ino:x}"
    if generation is not None and generation != actual_generation:
        raise ValueError("controller event journal was replaced after cursor creation")
    if after_sequence == 0 and cursor_sha256 not in {None, empty_digest}:
        raise ValueError("event cursor SHA-256 does not match the empty prefix")

    digest = hashlib.sha256()
    events: list[dict[str, Any]] = []
    next_after = after_sequence
    prefix_verified = after_sequence == 0
    has_more = False
    expected_sequence = 1
    previous_event_digest: str | None = None
    with path.open("rb") as handle:
        for raw_line in handle:
            try:
                value = json.loads(raw_line.decode("utf-8", errors="strict"))
            except (UnicodeDecodeError, json.JSONDecodeError) as exc:
                raise ValueError("controller event journal is malformed") from exc
            if not isinstance(value, dict) or value.get("sequence") != expected_sequence:
                raise ValueError("controller event journal sequence is not contiguous")
            if value.get("schema_version") != EVENT_SCHEMA_VERSION:
                raise ValueError("controller event journal schema is unsupported")
            recorded_event_digest = value.get("event_sha256")
            event_without_digest = dict(value)
            event_without_digest.pop("event_sha256", None)
            actual_event_digest = hashlib.sha256(
                _canonical_event_bytes(event_without_digest)
            ).hexdigest()
            if recorded_event_digest != actual_event_digest:
                raise ValueError("controller event journal hash verification failed")
            if value.get("previous_event_sha256") != previous_event_digest:
                raise ValueError("controller event journal hash chain is broken")
            previous_event_digest = actual_event_digest
            sequence = expected_sequence
            expected_sequence += 1
            if sequence <= after_sequence:
                digest.update(raw_line)
                if sequence == after_sequence:
                    prefix_verified = True
                    if cursor_sha256 is not None and digest.hexdigest() != cursor_sha256:
                        raise ValueError("controller event journal prefix changed after cursor creation")
                continue
            if len(events) >= limit:
                has_more = True
                break
            events.append(dict(value))
            next_after = sequence
            digest.update(raw_line)
    if not prefix_verified:
        raise ValueError("event cursor is beyond the end of a truncated controller journal")
    return {
        "events_file": str(path),
        "events": events,
        "after_sequence": after_sequence,
        "next_after_sequence": next_after,
        "has_more": has_more,
        "invalid_line_count": 0,
        "generation": actual_generation,
        "cursor_sha256": digest.hexdigest(),
    }


def elapsed_seconds(state: dict[str, Any]) -> int:
    end_epoch = float(state.get("finished_epoch", time.time()))
    return max(0, round(end_epoch - float(state["started_epoch"])))
