"""Durable workflow state and protocol helpers for Pi Lane orchestration.

The orchestrator owns only the cross-controller workflow graph. RTL-Closure
and lane-agent retain their isolated evidence directories and remain
authoritative for the artifacts they produce.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import socket
import sqlite3
import stat
import subprocess
import time
import uuid
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Iterable, Mapping

from .linux_process import proc_identity


PROTOCOL_VERSION = 3
SCHEMA_VERSION = 1
EVENT_SCHEMA_VERSION = 1
DATABASE_SCHEMA_VERSION = 5
OPEN_PDKS = frozenset({"sky130A", "gf180mcuD", "ihp-sg13g2"})
TERMINAL_STATUSES = frozenset({"completed", "failed", "cancelled"})
EXECUTING_STATUSES = frozenset({"created", "queued", "running"})
DORMANT_STATUSES = frozenset({"waiting_user", "blocked_infrastructure"})
ACTIVE_STATUSES = frozenset(
    EXECUTING_STATUSES | DORMANT_STATUSES
)
DEFAULT_ROOT = Path(
    os.environ.get("PI_LANE_ORCHESTRATOR_DIR", "~/.local/state/pi-lane/orchestrator")
).expanduser()
DEFAULT_DB = DEFAULT_ROOT / "orchestrator.sqlite3"
DEFAULT_SOCKET = DEFAULT_ROOT / "orchestrator.sock"
DEFAULT_LOG = DEFAULT_ROOT / "orchestrator.log"
DEFAULT_LOCK = DEFAULT_ROOT / "orchestrator.lock"
DEFAULT_LEASE_TTL_SECONDS = 300
SAFE_IDENTIFIER = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,255}$")


def ensure_private_directory(path: Path, *, label: str) -> Path:
    """Create a dedicated state directory, or reject an unsafe existing one."""

    expanded = path.expanduser()
    try:
        metadata = expanded.lstat()
    except FileNotFoundError:
        expanded.mkdir(parents=True, exist_ok=True, mode=0o700)
        metadata = expanded.lstat()
    if (
        stat.S_ISLNK(metadata.st_mode)
        or not stat.S_ISDIR(metadata.st_mode)
        or metadata.st_uid != os.geteuid()
        or stat.S_IMODE(metadata.st_mode) & 0o077
    ):
        raise OrchestratorError(
            "INSECURE_STATE_DIRECTORY",
            f"{label} must be an owner-only real directory (mode 0700): {expanded}",
            category="state",
        )
    return expanded.resolve()


def unix_socket_endpoint_state(
    socket_path: Path,
) -> tuple[str, tuple[int, int, int] | None]:
    """Classify a Unix socket without sending an application request."""

    try:
        before = socket_path.lstat()
    except FileNotFoundError:
        return "absent", None
    identity = (before.st_dev, before.st_ino, before.st_mode)
    if not stat.S_ISSOCK(before.st_mode):
        return "unsafe", identity
    probe = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    probe.settimeout(0.25)
    try:
        probe.connect(str(socket_path))
    except FileNotFoundError:
        return "absent", None
    except ConnectionRefusedError:
        result = "stale"
    except (OSError, TimeoutError):
        return "unsafe", identity
    else:
        return "live", identity
    finally:
        probe.close()
    try:
        after = socket_path.lstat()
    except FileNotFoundError:
        return "absent", None
    if (after.st_dev, after.st_ino, after.st_mode) != identity:
        return "raced", identity
    return result, identity


def _implementation_build_id() -> str:
    digest = hashlib.sha256()
    module_root = Path(__file__).resolve().parent
    for name in ("orchestrator.py", "orchestrator_server.py", "orchestrator_cli.py"):
        try:
            content = (module_root / name).read_bytes()
        except OSError:
            content = name.encode("ascii")
        digest.update(name.encode("ascii") + b"\0" + content + b"\0")
    return f"pi-lane-orchestrator-{digest.hexdigest()[:16]}"


ORCHESTRATOR_BUILD_ID = _implementation_build_id()


class OrchestratorError(RuntimeError):
    """Stable machine-facing orchestration error."""

    def __init__(
        self,
        code: str,
        message: str,
        *,
        category: str = "orchestration",
        retryable: bool = False,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.category = category
        self.retryable = retryable


def now_iso() -> str:
    return datetime.now().astimezone().isoformat(timespec="milliseconds")


def canonical_json(value: object) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def sha256_text(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def rtl_closure_runtime_config_fingerprint(
    environment: Mapping[str, str] | None = None,
) -> str:
    """Return a non-secret identity for the daemon's RTL-Closure runtime."""

    values = os.environ if environment is None else environment
    provider = str(
        values.get("RTL_CLOSURE_PROVIDER") or "openai_compatible"
    ).strip().casefold()
    model = str(values.get("RTL_CLOSURE_MODEL") or "").strip()
    base_url = str(values.get("RTL_CLOSURE_BASE_URL") or "").strip().rstrip("/")
    api_key = values.get("RTL_CLOSURE_API_KEY")
    key_identity = (
        sha256_text("present\0" + str(api_key))
        if api_key is not None
        else sha256_text("absent")
    )
    return sha256_text(
        canonical_json(
            {
                "schema_version": 1,
                "provider": provider,
                "model": model,
                "base_url": base_url,
                "api_key_sha256": key_identity,
            }
        )
    )


def file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _read_spec_bytes(path: Path) -> tuple[Path, bytes]:
    """Read one immutable view of a regular specification file."""

    resolved = canonical_spec_path(path)
    flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(resolved, flags)
        try:
            metadata = os.fstat(descriptor)
            if not stat.S_ISREG(metadata.st_mode):
                raise OrchestratorError(
                    "INVALID_SPEC_PATH", "specification must be a regular file"
                )
            if metadata.st_size > 1024 * 1024:
                raise OrchestratorError(
                    "SPEC_TOO_LARGE", "specification exceeds 1 MiB"
                )
            chunks: list[bytes] = []
            remaining = 1024 * 1024 + 1
            while remaining:
                chunk = os.read(descriptor, min(remaining, 64 * 1024))
                if not chunk:
                    break
                chunks.append(chunk)
                remaining -= len(chunk)
            content = b"".join(chunks)
        finally:
            os.close(descriptor)
    except OrchestratorError:
        raise
    except OSError as exc:
        raise OrchestratorError(
            "INVALID_SPEC_PATH", f"specification path is unavailable: {exc}"
        ) from exc
    if len(content) > 1024 * 1024:
        raise OrchestratorError("SPEC_TOO_LARGE", "specification exceeds 1 MiB")
    return resolved, content


def canonical_spec_path(path: Path) -> Path:
    """Resolve a specification path while rejecting aliases and missing files."""

    candidate = path.expanduser()
    try:
        if candidate.is_symlink():
            raise OrchestratorError(
                "INVALID_SPEC_PATH", "specification path must not be a symbolic link"
            )
        resolved = candidate.resolve(strict=True)
        if not resolved.is_file() or resolved.is_symlink():
            raise OrchestratorError(
                "INVALID_SPEC_PATH", "spec must be a canonical regular file"
            )
    except OrchestratorError:
        raise
    except OSError as exc:
        raise OrchestratorError(
            "INVALID_SPEC_PATH", f"specification path is unavailable: {exc}"
        ) from exc
    return resolved


def success_envelope(kind: str, data: object, *, request_id: str | None = None) -> dict[str, Any]:
    return {
        "schema_version": SCHEMA_VERSION,
        "protocol_version": PROTOCOL_VERSION,
        "ok": True,
        "kind": kind,
        "request_id": request_id,
        "data": data,
    }


def error_envelope(error: BaseException, *, request_id: str | None = None) -> dict[str, Any]:
    if isinstance(error, OrchestratorError):
        code = error.code
        category = error.category
        retryable = error.retryable
    else:
        code = "INTERNAL_ERROR"
        category = "orchestration"
        retryable = False
    return {
        "schema_version": SCHEMA_VERSION,
        "protocol_version": PROTOCOL_VERSION,
        "ok": False,
        "kind": "error",
        "request_id": request_id,
        "error": {
            "code": code,
            "category": category,
            "message": str(error)[:2000],
            "retryable": retryable,
        },
    }


def structured_failure(
    *,
    code: str,
    domain: str,
    message: str,
    retryable: bool,
    repairable_by_rtl: bool,
    user_action_required: bool,
    evidence: Iterable[str] = (),
    caused_by_event_id: str | None = None,
) -> dict[str, Any]:
    return {
        "code": code,
        "domain": domain,
        "message": message[:2000],
        "retryable": retryable,
        "repairable_by_rtl": repairable_by_rtl,
        "user_action_required": user_action_required,
        "caused_by_event_id": caused_by_event_id,
        "evidence": [str(value) for value in evidence],
    }


def classify_backend_failure(report: Mapping[str, Any]) -> dict[str, Any] | None:
    """Map backend evidence to a stable owner and retry policy."""

    status = str(report.get("status") or "")
    if status == "stopped":
        return None
    if status not in {"failed", "incomplete"}:
        return None
    nested_failure = report.get("failure")
    if not isinstance(nested_failure, Mapping):
        nested_failure = {}
    failure_class = str(
        report.get("failure_class")
        or nested_failure.get("class")
        or nested_failure.get("failure_class")
        or ""
    ).casefold()
    failure_gate = str(
        report.get("failure_gate") or nested_failure.get("gate") or ""
    ).casefold()
    raw_errors = report.get("errors") or []
    if not isinstance(raw_errors, list):
        raw_errors = [raw_errors]
    message = str(
        nested_failure.get("message")
        or report.get("status_detail")
        or report.get("error")
        or ""
    )
    if not message and raw_errors:
        message = str(raw_errors[0])
    message = message or f"backend ended with status {status}"
    evidence = [
        str(value)
        for value in (
            report.get("state_file"),
            report.get("pipeline_log"),
            report.get("metrics_path"),
        )
        if value
    ]

    infrastructure_markers = {
        "formal_unknown",
        "formal_error",
        "formal_incomplete",
        "formal_capacity",
        "verification_configuration",
        "verification_infrastructure",
        "verification_provider",
        "universal_inconclusive",
    }
    design_markers = {
        "compile_error",
        "interface_error",
        "policy_error",
        "architecture_constraint",
        "value_mismatch",
        "formal_counterexample",
        "universal_counterexample",
        "synthesis_equivalence",
    }
    failure_code = str(
        nested_failure.get("code") or report.get("failure_code") or failure_class or ""
    )
    # A concrete counterexample or design-class failure always wins over a
    # generic gate such as ``formal``. Otherwise real RTL failures become
    # infrastructure incidents and are retried without useful repair evidence.
    if failure_class == "release_integrity" or failure_gate in {
        "publication_manifest",
        "lane_delivery_attestation",
    }:
        inferred = structured_failure(
            code=(failure_code or "RELEASE_INTEGRITY_FAILED").upper(),
            domain="release_integrity",
            message=message,
            retryable=False,
            repairable_by_rtl=False,
            user_action_required=True,
            evidence=evidence,
        )
    elif failure_class in design_markers or failure_gate in {"rtl_repair", "architecture"}:
        inferred = structured_failure(
            code=(failure_code or "RTL_VERIFICATION_FAILED").upper(),
            domain="generated_rtl",
            message=message,
            retryable=False,
            repairable_by_rtl=True,
            user_action_required=False,
            evidence=evidence,
        )
    elif failure_class in {"infrastructure", "lane_launch"} or failure_gate == "lane_launch":
        inferred = structured_failure(
            code=(failure_code or "BACKEND_INFRASTRUCTURE_FAILED").upper(),
            domain="infrastructure",
            message=message,
            retryable=True,
            repairable_by_rtl=False,
            user_action_required=True,
            evidence=evidence,
        )
    elif failure_class in infrastructure_markers or failure_gate in {
        "formal",
        "verification_infrastructure",
    }:
        inferred = structured_failure(
            code=(failure_code or "VERIFICATION_INCOMPLETE").upper(),
            domain="verification",
            message=message,
            retryable=True,
            repairable_by_rtl=False,
            user_action_required=True,
            evidence=evidence,
        )
    elif str(report.get("component") or "") == "librelane":
        inferred = structured_failure(
            code="PHYSICAL_FLOW_FAILED" if status == "failed" else "DELIVERY_INCOMPLETE",
            domain="physical_tool",
            message=message,
            retryable=True,
            repairable_by_rtl=False,
            user_action_required=True,
            evidence=evidence,
        )
    else:
        inferred = structured_failure(
            code=(failure_code or "ORCHESTRATION_BACKEND_FAILED").upper(),
            domain="orchestration",
            message=message,
            retryable=False,
            repairable_by_rtl=False,
            user_action_required=True,
            evidence=evidence,
        )

    typed_code = nested_failure.get("code")
    if isinstance(typed_code, str) and typed_code:
        inferred["code"] = typed_code[:128]
    typed_domain = nested_failure.get("domain")
    if isinstance(typed_domain, str) and typed_domain:
        inferred["domain"] = typed_domain[:128]
    typed_category = nested_failure.get("category")
    if isinstance(typed_category, str) and typed_category:
        inferred["category"] = typed_category[:128]
    for field in ("retryable", "repairable_by_rtl", "user_action_required"):
        typed_value = nested_failure.get(field)
        if type(typed_value) is bool:
            inferred[field] = typed_value
    return inferred


def _connect(path: Path) -> sqlite3.Connection:
    connection = sqlite3.connect(path, timeout=30, isolation_level=None)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA foreign_keys=ON")
    connection.execute("PRAGMA busy_timeout=30000")
    return connection


@dataclass(frozen=True)
class WorkflowIdentity:
    workflow_id: str
    idempotency_key: str
    spec_path: str
    spec_sha256: str
    design_name: str
    pdk: str


class OrchestratorStore:
    """SQLite-backed single-writer workflow projection and event journal."""

    def __init__(self, db_path: Path = DEFAULT_DB) -> None:
        expanded = db_path.expanduser()
        parent = ensure_private_directory(
            expanded.parent, label="orchestrator database parent"
        )
        self.db_path = parent / expanded.name
        if self.db_path.is_symlink():
            raise OrchestratorError(
                "INVALID_STATE_PATH",
                "orchestrator database must not be a symlink",
                category="state",
            )
        self._initialize()

    def _initialize(self) -> None:
        with _connect(self.db_path) as connection:
            tables = {
                str(row["name"])
                for row in connection.execute(
                    "SELECT name FROM sqlite_master WHERE type='table'"
                ).fetchall()
                if not str(row["name"]).startswith("sqlite_")
            }
            parsed_version: int | None = None
            if tables:
                if "metadata" not in tables:
                    raise OrchestratorError(
                        "INVALID_DATABASE_SCHEMA",
                        "an existing orchestrator database has no schema metadata",
                        category="state",
                    )
                try:
                    stored_version = connection.execute(
                        "SELECT value FROM metadata WHERE key='schema_version'"
                    ).fetchone()
                    parsed_version = int(stored_version["value"])
                except (sqlite3.DatabaseError, TypeError, ValueError) as exc:
                    raise OrchestratorError(
                        "INVALID_DATABASE_SCHEMA",
                        "orchestrator database schema version is malformed",
                        category="state",
                    ) from exc
                if parsed_version != DATABASE_SCHEMA_VERSION:
                    raise OrchestratorError(
                        "UNSUPPORTED_DATABASE_SCHEMA",
                        f"database schema {parsed_version} is unsupported; create a fresh "
                        f"schema {DATABASE_SCHEMA_VERSION} database",
                        category="state",
                    )
            expected_columns = {
                "workflows": {
                    "workflow_id", "idempotency_key", "spec_path", "source_spec_path",
                    "spec_sha256", "design_name", "pdk", "status", "phase", "revision",
                    "rtl_closure_run_id", "active_lane_run_id", "failure_json",
                    "snapshot_json", "supersedes_workflow_id", "created_at", "updated_at",
                },
                "leases": {
                    "workflow_id", "lease_token", "owner", "pid", "process_start_time",
                    "heartbeat_at", "expires_at",
                },
                "operations": {
                    "operation_id", "workflow_id", "kind", "ordinal", "status",
                    "idempotency_key", "request_json", "result_json", "failure_json",
                    "attempt_count", "next_attempt_epoch", "created_at", "updated_at",
                    "completed_at",
                },
                "attempts": {
                    "attempt_id", "workflow_id", "kind", "ordinal",
                    "backend_run_id", "status", "supersedes_attempt_id",
                    "launch_request_sha256", "reason", "started_at", "finished_at",
                    "failure_json",
                },
                "events": {
                    "workflow_id", "sequence", "event_id", "timestamp", "source",
                    "attempt_id", "type", "severity", "state_revision", "phase",
                    "payload_json", "failure_json", "previous_sha256", "sha256",
                },
                "artifacts": {
                    "artifact_id", "workflow_id", "attempt_id", "kind", "path",
                    "sha256", "size_bytes",
                },
                "client_cursors": {
                    "client_id", "workflow_id", "sequence", "updated_at",
                },
                "terminal_integrity_audits": {
                    "workflow_id", "checked_revision", "last_checked_at",
                    "next_check_epoch", "backend_status", "backend_snapshot_sha256",
                },
                "metadata": {"key", "value"},
            }
            if parsed_version == DATABASE_SCHEMA_VERSION:
                if tables != set(expected_columns):
                    raise OrchestratorError(
                        "INVALID_DATABASE_SCHEMA",
                        f"schema {DATABASE_SCHEMA_VERSION} has an invalid table set",
                        category="state",
                    )
                for table, expected in expected_columns.items():
                    actual = {
                        str(row["name"])
                        for row in connection.execute(f"PRAGMA table_info({table})").fetchall()
                    }
                    if actual != expected:
                        raise OrchestratorError(
                            "INVALID_DATABASE_SCHEMA",
                            f"schema {DATABASE_SCHEMA_VERSION} table {table} has an invalid layout",
                            category="state",
                        )
            connection.execute("PRAGMA journal_mode=WAL")
            connection.execute("PRAGMA synchronous=FULL")
            connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS workflows (
                    workflow_id TEXT PRIMARY KEY,
                    idempotency_key TEXT NOT NULL UNIQUE,
                    spec_path TEXT NOT NULL,
                    source_spec_path TEXT,
                    spec_sha256 TEXT NOT NULL,
                    design_name TEXT NOT NULL,
                    pdk TEXT NOT NULL,
                    status TEXT NOT NULL,
                    phase TEXT NOT NULL,
                    revision INTEGER NOT NULL DEFAULT 0,
                    rtl_closure_run_id TEXT,
                    active_lane_run_id TEXT,
                    failure_json TEXT,
                    snapshot_json TEXT NOT NULL DEFAULT '{}',
                    supersedes_workflow_id TEXT REFERENCES workflows(workflow_id),
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS attempts (
                    attempt_id TEXT PRIMARY KEY,
                    workflow_id TEXT NOT NULL REFERENCES workflows(workflow_id),
                    kind TEXT NOT NULL,
                    ordinal INTEGER NOT NULL,
                    backend_run_id TEXT,
                    status TEXT NOT NULL,
                    supersedes_attempt_id TEXT REFERENCES attempts(attempt_id),
                    launch_request_sha256 TEXT NOT NULL,
                    reason TEXT,
                    started_at TEXT NOT NULL,
                    finished_at TEXT,
                    failure_json TEXT,
                    UNIQUE(workflow_id, kind, ordinal),
                    UNIQUE(workflow_id, kind, backend_run_id)
                );
                CREATE TABLE IF NOT EXISTS events (
                    workflow_id TEXT NOT NULL REFERENCES workflows(workflow_id),
                    sequence INTEGER NOT NULL,
                    event_id TEXT NOT NULL UNIQUE,
                    timestamp TEXT NOT NULL,
                    source TEXT NOT NULL,
                    attempt_id TEXT,
                    type TEXT NOT NULL,
                    severity TEXT NOT NULL,
                    state_revision INTEGER NOT NULL,
                    phase TEXT NOT NULL,
                    payload_json TEXT NOT NULL,
                    failure_json TEXT,
                    previous_sha256 TEXT,
                    sha256 TEXT NOT NULL,
                    PRIMARY KEY(workflow_id, sequence)
                );
                CREATE TABLE IF NOT EXISTS artifacts (
                    artifact_id TEXT PRIMARY KEY,
                    workflow_id TEXT NOT NULL REFERENCES workflows(workflow_id),
                    attempt_id TEXT,
                    kind TEXT NOT NULL,
                    path TEXT NOT NULL,
                    sha256 TEXT,
                    size_bytes INTEGER,
                    UNIQUE(workflow_id, path)
                );
                CREATE TABLE IF NOT EXISTS leases (
                    workflow_id TEXT PRIMARY KEY REFERENCES workflows(workflow_id),
                    lease_token TEXT NOT NULL UNIQUE,
                    owner TEXT NOT NULL,
                    pid INTEGER NOT NULL,
                    process_start_time TEXT,
                    heartbeat_at TEXT NOT NULL,
                    expires_at REAL NOT NULL
                );
                CREATE TABLE IF NOT EXISTS client_cursors (
                    client_id TEXT NOT NULL,
                    workflow_id TEXT NOT NULL REFERENCES workflows(workflow_id),
                    sequence INTEGER NOT NULL,
                    updated_at TEXT NOT NULL,
                    PRIMARY KEY(client_id, workflow_id)
                );
                CREATE TABLE IF NOT EXISTS terminal_integrity_audits (
                    workflow_id TEXT PRIMARY KEY REFERENCES workflows(workflow_id),
                    checked_revision INTEGER NOT NULL,
                    last_checked_at TEXT NOT NULL,
                    next_check_epoch REAL NOT NULL,
                    backend_status TEXT NOT NULL,
                    backend_snapshot_sha256 TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS operations (
                    operation_id TEXT PRIMARY KEY,
                    workflow_id TEXT NOT NULL REFERENCES workflows(workflow_id),
                    kind TEXT NOT NULL,
                    ordinal INTEGER NOT NULL,
                    status TEXT NOT NULL,
                    idempotency_key TEXT NOT NULL,
                    request_json TEXT NOT NULL,
                    result_json TEXT,
                    failure_json TEXT,
                    attempt_count INTEGER NOT NULL DEFAULT 0,
                    next_attempt_epoch REAL NOT NULL DEFAULT 0,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    completed_at TEXT,
                    UNIQUE(workflow_id,kind,ordinal),
                    UNIQUE(workflow_id,idempotency_key)
                );
                CREATE TABLE IF NOT EXISTS metadata (
                    key TEXT PRIMARY KEY,
                    value TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS workflow_status_idx ON workflows(status, updated_at);
                CREATE INDEX IF NOT EXISTS attempt_workflow_idx ON attempts(workflow_id, kind, ordinal);
                CREATE INDEX IF NOT EXISTS operation_pending_idx ON operations(status, updated_at);
                CREATE UNIQUE INDEX IF NOT EXISTS lease_token_idx ON leases(lease_token);
                """
            )
            for table, expected in expected_columns.items():
                actual = {
                    str(row["name"])
                    for row in connection.execute(f"PRAGMA table_info({table})").fetchall()
                }
                if actual != expected:
                    raise OrchestratorError(
                        "INVALID_DATABASE_SCHEMA",
                        f"schema {DATABASE_SCHEMA_VERSION} table {table} has an invalid layout",
                        category="state",
                    )
            connection.execute("BEGIN IMMEDIATE")
            try:
                connection.execute(
                    "INSERT INTO metadata(key,value) VALUES('schema_version',?) "
                    "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                    (str(DATABASE_SCHEMA_VERSION),),
                )
                connection.execute("COMMIT")
            except Exception:
                connection.execute("ROLLBACK")
                raise
        os.chmod(self.db_path, 0o600)

    def create_workflow(
        self,
        spec_path: Path,
        *,
        idempotency_key: str | None,
        workflow_id: str | None = None,
        supersedes_workflow_id: str | None = None,
        expected_spec_sha256: str | None = None,
    ) -> tuple[dict[str, Any], bool]:
        resolved, content = _read_spec_bytes(spec_path)
        try:
            raw = json.loads(content.decode("utf-8"))
        except (UnicodeError, json.JSONDecodeError) as exc:
            raise OrchestratorError("INVALID_SPEC", f"invalid JSON specification: {exc}") from exc
        if not isinstance(raw, dict):
            raise OrchestratorError("INVALID_SPEC", "specification root must be an object")
        design = raw.get("design") or {}
        if not isinstance(design, dict):
            raise OrchestratorError("INVALID_SPEC", "design must be an object")
        name = design.get("name")
        pdk = design.get("pdk")
        if not isinstance(name, str) or not name:
            raise OrchestratorError("INVALID_SPEC", "design.name is required")
        if pdk not in OPEN_PDKS:
            raise OrchestratorError(
                "PDK_NOT_ALLOWED",
                f"design.pdk must be one of {', '.join(sorted(OPEN_PDKS))}",
                category="policy",
            )
        spec_hash = hashlib.sha256(content).hexdigest()
        if expected_spec_sha256 is not None and spec_hash != expected_spec_sha256:
            raise OrchestratorError(
                "SPEC_SNAPSHOT_CHANGED",
                "workflow specification snapshot no longer matches its recorded digest",
                category="state",
            )
        if idempotency_key is not None and not isinstance(idempotency_key, str):
            raise OrchestratorError(
                "INVALID_IDEMPOTENCY_KEY", "idempotency key must be a string"
            )
        key = f"auto:{spec_hash}" if idempotency_key is None else idempotency_key.strip()
        if not key or len(key) > 256:
            raise OrchestratorError("INVALID_IDEMPOTENCY_KEY", "idempotency key must be 1-256 characters")
        identifier = workflow_id or f"wf-{datetime.now().strftime('%Y%m%d-%H%M%S')}-{uuid.uuid4().hex[:10]}"
        if not SAFE_IDENTIFIER.fullmatch(identifier):
            raise OrchestratorError(
                "INVALID_WORKFLOW_ID", "workflow_id must be a non-empty safe identifier"
            )
        timestamp = now_iso()
        snapshot_root = self.db_path.parent / "spec-snapshots"
        snapshot_path = snapshot_root / f"{identifier}.json"
        snapshot_created = False
        with _connect(self.db_path) as connection:
            connection.execute("BEGIN IMMEDIATE")
            existing = connection.execute(
                "SELECT * FROM workflows WHERE idempotency_key=?", (key,)
            ).fetchone()
            if existing is not None:
                operation_conflict = any(
                    (
                        existing["spec_sha256"] != spec_hash,
                        existing["supersedes_workflow_id"]
                        != supersedes_workflow_id,
                        workflow_id is not None
                        and existing["workflow_id"] != workflow_id,
                    )
                )
                if operation_conflict:
                    connection.execute("ROLLBACK")
                    raise OrchestratorError(
                        "IDEMPOTENCY_CONFLICT",
                        "idempotency key already belongs to a different workflow operation",
                    )
                connection.execute("COMMIT")
                return self.get_workflow(str(existing["workflow_id"])), False
            if supersedes_workflow_id is not None:
                parent = connection.execute(
                    "SELECT * FROM workflows WHERE workflow_id=?",
                    (supersedes_workflow_id,),
                ).fetchone()
                if parent is None:
                    connection.execute("ROLLBACK")
                    raise OrchestratorError(
                        "RUN_NOT_FOUND",
                        f"superseded workflow does not exist: {supersedes_workflow_id}",
                        category="state",
                    )
                self._verify_workflow_projection(connection, parent)
                if parent["status"] not in TERMINAL_STATUSES:
                    connection.execute("ROLLBACK")
                    raise OrchestratorError(
                        "RERUN_NOT_ALLOWED", "only a terminal workflow can be rerun"
                    )
            try:
                if snapshot_root.is_symlink():
                    raise OrchestratorError(
                        "INVALID_STATE_PATH", "spec snapshot directory must not be a symlink"
                    )
                snapshot_root = ensure_private_directory(
                    snapshot_root, label="spec snapshot directory"
                )
                descriptor = os.open(
                    snapshot_path,
                    os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_CLOEXEC", 0),
                    0o400,
                )
                try:
                    offset = 0
                    while offset < len(content):
                        offset += os.write(descriptor, content[offset:])
                    os.fsync(descriptor)
                finally:
                    os.close(descriptor)
                directory_descriptor = os.open(
                    snapshot_root,
                    os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_CLOEXEC", 0),
                )
                try:
                    os.fsync(directory_descriptor)
                finally:
                    os.close(directory_descriptor)
                snapshot_created = True
                connection.execute(
                    """
                    INSERT INTO workflows(
                        workflow_id,idempotency_key,spec_path,source_spec_path,spec_sha256,
                        design_name,pdk,status,phase,revision,snapshot_json,
                        supersedes_workflow_id,created_at,updated_at
                    ) VALUES(?,?,?,?,?,?,?,'created','created',0,'{}',?,?,?)
                    """,
                    (
                        identifier,
                        key,
                        str(snapshot_path),
                        str(resolved),
                        spec_hash,
                        name,
                        pdk,
                        supersedes_workflow_id,
                        timestamp,
                        timestamp,
                    ),
                )
                self._insert_event(
                    connection,
                    identifier,
                    state_revision=0,
                    event_type="workflow.created",
                    source="pi-lane-orchestrator",
                    phase="created",
                    severity="info",
                    attempt_id=None,
                    payload={
                        "status": "created",
                        "revision": 0,
                        "spec_sha256": spec_hash,
                        "supersedes_workflow_id": supersedes_workflow_id,
                    },
                    failure=None,
                )
                connection.execute("COMMIT")
            except Exception:
                connection.execute("ROLLBACK")
                if snapshot_created:
                    snapshot_path.unlink(missing_ok=True)
                raise
        return self.get_workflow(identifier), True

    @staticmethod
    def _workflow_dict(row: sqlite3.Row) -> dict[str, Any]:
        value = dict(row)
        for key in ("failure_json", "snapshot_json"):
            raw = value.pop(key, None)
            value[key.removesuffix("_json")] = json.loads(raw) if raw else None
        return value

    def get_workflow(self, workflow_id: str) -> dict[str, Any]:
        with _connect(self.db_path) as connection:
            row = connection.execute(
                "SELECT * FROM workflows WHERE workflow_id=?", (workflow_id,)
            ).fetchone()
            if row is None:
                raise OrchestratorError("RUN_NOT_FOUND", f"workflow does not exist: {workflow_id}", category="state")
            self._verify_workflow_projection(connection, row)
            value = self._workflow_dict(row)
            attempts = connection.execute(
                "SELECT * FROM attempts WHERE workflow_id=? ORDER BY kind,ordinal",
                (workflow_id,),
            ).fetchall()
            value["attempts"] = [self._attempt_dict(item) for item in attempts]
            operations = connection.execute(
                "SELECT * FROM operations WHERE workflow_id=? ORDER BY rowid",
                (workflow_id,),
            ).fetchall()
            value["operations"] = [self._operation_dict(item) for item in operations]
            return value

    def find_by_rtl_closure_run(self, run_id: str) -> dict[str, Any] | None:
        with _connect(self.db_path) as connection:
            row = connection.execute(
                "SELECT * FROM workflows WHERE rtl_closure_run_id=?", (run_id,)
            ).fetchone()
            if row is None:
                return None
            self._verify_workflow_projection(connection, row)
            return self._workflow_dict(row)

    def list_workflows(self, *, active_only: bool = False, limit: int = 100) -> list[dict[str, Any]]:
        if not 1 <= limit <= 1000:
            raise OrchestratorError("INVALID_LIMIT", "limit must be from 1 to 1000")
        where = (
            "WHERE status NOT IN ('completed','failed','cancelled')"
            if active_only
            else ""
        )
        with _connect(self.db_path) as connection:
            rows = connection.execute(
                f"SELECT * FROM workflows {where} ORDER BY updated_at DESC LIMIT ?", (limit,)
            ).fetchall()
            for row in rows:
                self._verify_workflow_projection(connection, row)
            return [self._workflow_dict(row) for row in rows]

    def list_compact_workflows(
        self, *, active_only: bool = False, limit: int = 100
    ) -> list[dict[str, Any]]:
        if not 1 <= limit <= 100:
            raise OrchestratorError("INVALID_LIMIT", "compact list limit must be from 1 to 100")
        where = (
            "WHERE status NOT IN ('completed','failed','cancelled')"
            if active_only
            else ""
        )
        with _connect(self.db_path) as connection:
            connection.execute("BEGIN")
            rows = connection.execute(
                f"SELECT * FROM workflows {where} ORDER BY updated_at DESC LIMIT ?",
                (limit,),
            ).fetchall()
            values = [self._compact_status_from_row(connection, row) for row in rows]
            connection.execute("COMMIT")
            return values

    @staticmethod
    def _attempt_dict(row: sqlite3.Row) -> dict[str, Any]:
        value = dict(row)
        raw = value.pop("failure_json", None)
        value["failure"] = json.loads(raw) if raw else None
        return value

    @staticmethod
    def _operation_dict(row: sqlite3.Row) -> dict[str, Any]:
        value = dict(row)
        for key in ("request_json", "result_json", "failure_json"):
            raw = value.pop(key, None)
            value[key.removesuffix("_json")] = json.loads(raw) if raw else None
        return value

    def create_operation(
        self,
        workflow_id: str,
        *,
        kind: str,
        request: Mapping[str, Any],
        idempotency_key: str | None = None,
        reuse_active: bool = False,
        cancel_active_kinds: Iterable[str] = (),
    ) -> tuple[dict[str, Any], bool]:
        if kind not in {"start", "stop", "retry"}:
            raise OrchestratorError("INVALID_OPERATION", f"unsupported operation: {kind}")
        request_text = canonical_json(dict(request))
        if len(request_text.encode("utf-8")) > 16 * 1024:
            raise OrchestratorError("OPERATION_TOO_LARGE", "operation request exceeds 16 KiB")
        timestamp = now_iso()
        with _connect(self.db_path) as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                workflow = connection.execute(
                    "SELECT * FROM workflows WHERE workflow_id=?", (workflow_id,)
                ).fetchone()
                if workflow is None:
                    raise OrchestratorError(
                        "RUN_NOT_FOUND", f"workflow does not exist: {workflow_id}", category="state"
                    )
                self._verify_workflow_projection(connection, workflow)
                if workflow["status"] in TERMINAL_STATUSES:
                    raise OrchestratorError(
                        "TERMINAL_STATE",
                        "terminal workflow state is immutable; create a rerun",
                        category="state",
                    )
                if reuse_active:
                    current = connection.execute(
                        "SELECT * FROM operations WHERE workflow_id=? AND kind=? "
                        "AND status IN ('pending','executing') ORDER BY ordinal DESC LIMIT 1",
                        (workflow_id, kind),
                    ).fetchone()
                    if current is not None:
                        if canonical_json(json.loads(current["request_json"])) != request_text:
                            raise OrchestratorError(
                                "OPERATION_IDENTITY_MISMATCH",
                                "active operation belongs to a different request",
                                category="state",
                            )
                        connection.execute("COMMIT")
                        return self._operation_dict(current), False
                cancel_kinds = tuple(dict.fromkeys(cancel_active_kinds))
                if cancel_kinds:
                    placeholders = ",".join("?" for _ in cancel_kinds)
                    connection.execute(
                        f"UPDATE operations SET status='cancelled',completed_at=?,updated_at=? "
                        f"WHERE workflow_id=? AND kind IN ({placeholders}) "
                        "AND status IN ('pending','executing')",
                        (timestamp, timestamp, workflow_id, *cancel_kinds),
                    )
                prior = connection.execute(
                    "SELECT ordinal FROM operations WHERE workflow_id=? AND kind=? "
                    "ORDER BY ordinal DESC LIMIT 1",
                    (workflow_id, kind),
                ).fetchone()
                ordinal = int(prior["ordinal"]) + 1 if prior else 1
                operation_id = f"op-{kind}-{ordinal:03d}-{uuid.uuid4().hex[:10]}"
                key = idempotency_key or (
                    f"pi-lane:{kind}:" + sha256_text(
                        canonical_json(
                            {
                                "workflow_id": workflow_id,
                                "operation_id": operation_id,
                                "ordinal": ordinal,
                                "request": dict(request),
                            }
                        )
                    )
                )
                if not isinstance(key, str) or not 1 <= len(key) <= 256:
                    raise OrchestratorError(
                        "INVALID_IDEMPOTENCY_KEY", "operation idempotency key is invalid"
                    )
                connection.execute(
                    """
                    INSERT INTO operations(
                        operation_id,workflow_id,kind,ordinal,status,idempotency_key,
                        request_json,created_at,updated_at
                    ) VALUES(?,?,?,?,'pending',?,?,?,?)
                    """,
                    (
                        operation_id,
                        workflow_id,
                        kind,
                        ordinal,
                        key,
                        request_text,
                        timestamp,
                        timestamp,
                    ),
                )
                self._insert_event(
                    connection,
                    workflow_id,
                    state_revision=int(workflow["revision"]),
                    event_type=f"{kind}.requested",
                    source="pi-lane-orchestrator",
                    phase=str(workflow["phase"]),
                    severity="info",
                    attempt_id=None,
                    payload={
                        "operation_id": operation_id,
                        "ordinal": ordinal,
                        "idempotency_key_sha256": sha256_text(key),
                    },
                    failure=None,
                )
                row = connection.execute(
                    "SELECT * FROM operations WHERE operation_id=?", (operation_id,)
                ).fetchone()
                connection.execute("COMMIT")
            except Exception:
                connection.execute("ROLLBACK")
                raise
        assert row is not None
        return self._operation_dict(row), True

    def pending_operations(
        self, *, workflow_id: str | None = None, limit: int = 100
    ) -> list[dict[str, Any]]:
        if not 1 <= limit <= 1000:
            raise OrchestratorError("INVALID_LIMIT", "operation limit must be from 1 to 1000")
        current_epoch = time.time()
        where = (
            "operations.status IN ('pending','executing') "
            "AND operations.next_attempt_epoch<=? "
            "AND NOT EXISTS (SELECT 1 FROM operations AS earlier "
            "WHERE earlier.workflow_id=operations.workflow_id "
            "AND earlier.status IN ('pending','executing') "
            "AND earlier.rowid<operations.rowid)"
        )
        params: list[Any] = [current_epoch]
        if workflow_id is not None:
            where += " AND operations.workflow_id=?"
            params.append(workflow_id)
        params.append(limit)
        with _connect(self.db_path) as connection:
            rows = connection.execute(
                "SELECT operations.* FROM operations JOIN workflows USING(workflow_id) "
                f"WHERE {where} ORDER BY operations.rowid LIMIT ?",
                tuple(params),
            ).fetchall()
            for row in rows:
                workflow = connection.execute(
                    "SELECT * FROM workflows WHERE workflow_id=?", (row["workflow_id"],)
                ).fetchone()
                assert workflow is not None
                self._verify_workflow_projection(connection, workflow)
            return [self._operation_dict(row) for row in rows]

    def mark_operation_attempt(self, operation_id: str) -> dict[str, Any]:
        with _connect(self.db_path) as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                row = connection.execute(
                    "SELECT * FROM operations WHERE operation_id=?", (operation_id,)
                ).fetchone()
                if row is None:
                    raise OrchestratorError("OPERATION_NOT_FOUND", "operation does not exist", category="state")
                if row["status"] not in {"pending", "executing"}:
                    raise OrchestratorError("OPERATION_NOT_ACTIVE", "operation is already terminal", category="state")
                workflow = connection.execute(
                    "SELECT * FROM workflows WHERE workflow_id=?", (row["workflow_id"],)
                ).fetchone()
                assert workflow is not None
                self._verify_workflow_projection(connection, workflow)
                attempt_count = int(row["attempt_count"]) + 1
                connection.execute(
                    "UPDATE operations SET status='executing',attempt_count=?,failure_json=NULL,updated_at=? "
                    "WHERE operation_id=?",
                    (attempt_count, now_iso(), operation_id),
                )
                self._insert_event(
                    connection,
                    str(row["workflow_id"]),
                    state_revision=int(workflow["revision"]),
                    event_type=f"{row['kind']}.dispatched",
                    source="pi-lane-orchestrator",
                    phase=str(workflow["phase"]),
                    severity="info",
                    attempt_id=None,
                    payload={"operation_id": operation_id, "attempt_count": attempt_count},
                    failure=None,
                )
                updated = connection.execute(
                    "SELECT * FROM operations WHERE operation_id=?", (operation_id,)
                ).fetchone()
                connection.execute("COMMIT")
            except Exception:
                connection.execute("ROLLBACK")
                raise
        assert updated is not None
        return self._operation_dict(updated)

    def defer_operation(
        self, operation_id: str, *, failure: Mapping[str, Any], retryable: bool
    ) -> dict[str, Any]:
        with _connect(self.db_path) as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                row = connection.execute(
                    "SELECT * FROM operations WHERE operation_id=?", (operation_id,)
                ).fetchone()
                if row is None:
                    raise OrchestratorError("OPERATION_NOT_FOUND", "operation does not exist", category="state")
                if row["status"] not in {"pending", "executing"}:
                    connection.execute("COMMIT")
                    return self._operation_dict(row)
                workflow = connection.execute(
                    "SELECT * FROM workflows WHERE workflow_id=?", (row["workflow_id"],)
                ).fetchone()
                assert workflow is not None
                self._verify_workflow_projection(connection, workflow)
                terminal = not retryable
                attempt_count = int(row["attempt_count"])
                backoff = min(5.0 * (2 ** min(max(attempt_count - 1, 0), 4)), 60.0)
                status = "failed" if terminal else "pending"
                timestamp = now_iso()
                connection.execute(
                    "UPDATE operations SET status=?,failure_json=?,next_attempt_epoch=?,updated_at=?,completed_at=? "
                    "WHERE operation_id=?",
                    (
                        status,
                        canonical_json(failure),
                        0 if terminal else time.time() + backoff,
                        timestamp,
                        timestamp if terminal else None,
                        operation_id,
                    ),
                )
                self._insert_event(
                    connection,
                    str(row["workflow_id"]),
                    state_revision=int(workflow["revision"]),
                    event_type=f"{row['kind']}.{'failed' if terminal else 'deferred'}",
                    source="pi-lane-orchestrator",
                    phase=str(workflow["phase"]),
                    severity="error" if terminal else "warning",
                    attempt_id=None,
                    payload={
                        "operation_id": operation_id,
                        "attempt_count": attempt_count,
                        "retryable": retryable,
                        "backoff_seconds": None if terminal else backoff,
                    },
                    failure=failure,
                )
                updated = connection.execute(
                    "SELECT * FROM operations WHERE operation_id=?", (operation_id,)
                ).fetchone()
                connection.execute("COMMIT")
            except Exception:
                connection.execute("ROLLBACK")
                raise
        assert updated is not None
        return self._operation_dict(updated)

    @staticmethod
    def _complete_operation_locked(
        connection: sqlite3.Connection,
        operation_id: str,
        *,
        result: Mapping[str, Any],
    ) -> None:
        row = connection.execute(
            "SELECT status FROM operations WHERE operation_id=?", (operation_id,)
        ).fetchone()
        if row is None:
            raise OrchestratorError("OPERATION_NOT_FOUND", "operation does not exist", category="state")
        if row["status"] not in {"pending", "executing"}:
            raise OrchestratorError("OPERATION_NOT_ACTIVE", "operation is already terminal", category="state")
        timestamp = now_iso()
        connection.execute(
            "UPDATE operations SET status='completed',result_json=?,failure_json=NULL,"
            "next_attempt_epoch=0,updated_at=?,completed_at=? WHERE operation_id=?",
            (canonical_json(dict(result)), timestamp, timestamp, operation_id),
        )

    @staticmethod
    def _fail_operation_locked(
        connection: sqlite3.Connection,
        operation_id: str,
        *,
        failure: Mapping[str, Any],
    ) -> None:
        row = connection.execute(
            "SELECT status FROM operations WHERE operation_id=?", (operation_id,)
        ).fetchone()
        if row is None:
            raise OrchestratorError("OPERATION_NOT_FOUND", "operation does not exist", category="state")
        if row["status"] not in {"pending", "executing"}:
            raise OrchestratorError("OPERATION_NOT_ACTIVE", "operation is already terminal", category="state")
        timestamp = now_iso()
        connection.execute(
            "UPDATE operations SET status='failed',failure_json=?,next_attempt_epoch=0,"
            "updated_at=?,completed_at=? WHERE operation_id=?",
            (canonical_json(dict(failure)), timestamp, timestamp, operation_id),
        )

    def add_attempt(
        self,
        workflow_id: str,
        *,
        kind: str,
        backend_run_id: str | None,
        launch_request_sha256: str,
        reason: str,
        status: str = "running",
    ) -> dict[str, Any]:
        if kind not in {"rtl_closure", "lane"}:
            raise OrchestratorError(
                "INVALID_ATTEMPT_KIND", f"unsupported attempt kind: {kind}"
            )
        with _connect(self.db_path) as connection:
            connection.execute("BEGIN IMMEDIATE")
            workflow = connection.execute(
                "SELECT * FROM workflows WHERE workflow_id=?",
                (workflow_id,),
            ).fetchone()
            if workflow is None:
                connection.execute("ROLLBACK")
                raise OrchestratorError(
                    "RUN_NOT_FOUND",
                    f"workflow does not exist: {workflow_id}",
                    category="state",
                )
            self._verify_workflow_projection(connection, workflow)
            if workflow["status"] in TERMINAL_STATUSES:
                connection.execute("ROLLBACK")
                raise OrchestratorError(
                    "TERMINAL_STATE",
                    "terminal workflow state is immutable; create a rerun",
                )
            existing = None
            if backend_run_id:
                existing = connection.execute(
                    "SELECT * FROM attempts WHERE workflow_id=? AND kind=? AND backend_run_id=?",
                    (workflow_id, kind, backend_run_id),
                ).fetchone()
            if existing is not None:
                connection.execute("COMMIT")
                return self._attempt_dict(existing)
            prior = connection.execute(
                "SELECT * FROM attempts WHERE workflow_id=? AND kind=? ORDER BY ordinal DESC LIMIT 1",
                (workflow_id, kind),
            ).fetchone()
            ordinal = int(prior["ordinal"]) + 1 if prior else 1
            attempt_id = f"{kind}-{ordinal:03d}-{uuid.uuid4().hex[:8]}"
            timestamp = now_iso()
            if prior is not None and prior["status"] == "running":
                connection.execute(
                    "UPDATE attempts SET status='superseded',finished_at=? WHERE attempt_id=?",
                    (timestamp, prior["attempt_id"]),
                )
            connection.execute(
                """
                INSERT INTO attempts(
                    attempt_id,workflow_id,kind,ordinal,backend_run_id,status,
                    supersedes_attempt_id,launch_request_sha256,reason,started_at
                ) VALUES(?,?,?,?,?,?,?,?,?,?)
                """,
                (
                    attempt_id,
                    workflow_id,
                    kind,
                    ordinal,
                    backend_run_id,
                    status,
                    prior["attempt_id"] if prior else None,
                    launch_request_sha256,
                    reason,
                    timestamp,
                ),
            )
            self._insert_event(
                connection,
                workflow_id,
                state_revision=int(
                    connection.execute(
                        "SELECT revision FROM workflows WHERE workflow_id=?", (workflow_id,)
                    ).fetchone()["revision"]
                ),
                event_type=f"{kind}.attempt_started",
                source="pi-lane-orchestrator",
                phase=f"{kind}_running",
                severity="info",
                attempt_id=attempt_id,
                payload={
                    "backend_run_id": backend_run_id,
                    "ordinal": ordinal,
                    "supersedes_attempt_id": prior["attempt_id"] if prior else None,
                },
                failure=None,
            )
            row = connection.execute(
                "SELECT * FROM attempts WHERE attempt_id=?", (attempt_id,)
            ).fetchone()
            connection.execute("COMMIT")
        assert row is not None
        return self._attempt_dict(row)

    def bind_attempt_and_update_workflow(
        self,
        workflow_id: str,
        *,
        kind: str,
        backend_run_id: str,
        launch_request_sha256: str,
        reason: str,
        status: str,
        phase: str,
        snapshot: Mapping[str, Any],
        rtl_closure_run_id: str | None = None,
        active_lane_run_id: str | None = None,
        failure: Mapping[str, Any] | None = None,
        expected_revision: int | None = None,
        event_type: str = "workflow.transition",
        close_running_attempts_as: str | None = None,
        close_attempt_kind: str | None = None,
        operation_id: str | None = None,
        operation_outcome: str = "completed",
    ) -> tuple[dict[str, Any], dict[str, Any]]:
        """Atomically bind one backend attempt to its workflow projection."""

        if kind not in {"rtl_closure", "lane"}:
            raise OrchestratorError("INVALID_ATTEMPT_KIND", f"unsupported attempt kind: {kind}")
        if not isinstance(backend_run_id, str) or not SAFE_IDENTIFIER.fullmatch(
            backend_run_id
        ):
            raise OrchestratorError(
                "INVALID_BACKEND_RUN_ID", "backend run id must be a safe identifier"
            )
        if not re.fullmatch(r"[0-9a-f]{64}", launch_request_sha256):
            raise OrchestratorError(
                "INVALID_LAUNCH_IDENTITY", "launch request SHA-256 is malformed"
            )
        if status not in ACTIVE_STATUSES | TERMINAL_STATUSES:
            raise OrchestratorError("INVALID_STATUS", f"unsupported workflow status: {status}")
        snapshot_text = canonical_json(dict(snapshot))
        if len(snapshot_text.encode("utf-8")) > 256 * 1024:
            raise OrchestratorError("SNAPSHOT_TOO_LARGE", "workflow snapshot exceeds 256 KiB")

        timestamp = now_iso()
        with _connect(self.db_path) as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                current = connection.execute(
                    "SELECT * FROM workflows WHERE workflow_id=?", (workflow_id,)
                ).fetchone()
                if current is None:
                    raise OrchestratorError(
                        "RUN_NOT_FOUND",
                        f"workflow does not exist: {workflow_id}",
                        category="state",
                    )
                revision = int(current["revision"])
                if expected_revision is not None and revision != expected_revision:
                    raise OrchestratorError(
                        "REVISION_CONFLICT",
                        f"expected revision {expected_revision}, found {revision}",
                        retryable=True,
                    )
                if current["status"] in TERMINAL_STATUSES:
                    raise OrchestratorError(
                        "TERMINAL_STATE",
                        "terminal workflow state is immutable; create a rerun",
                    )
                self._verify_workflow_projection(connection, current)
                if (
                    rtl_closure_run_id is not None
                    and current["rtl_closure_run_id"] is not None
                    and current["rtl_closure_run_id"] != rtl_closure_run_id
                ):
                    raise OrchestratorError(
                        "BACKEND_IDENTITY_MISMATCH",
                        "workflow is already bound to a different RTL-Closure run",
                        category="state",
                    )
                if kind == "rtl_closure" and rtl_closure_run_id != backend_run_id:
                    raise OrchestratorError(
                        "BACKEND_IDENTITY_MISMATCH",
                        "RTL-Closure attempt identity does not match the workflow binding",
                        category="state",
                    )
                if kind == "lane" and active_lane_run_id != backend_run_id:
                    raise OrchestratorError(
                        "BACKEND_IDENTITY_MISMATCH",
                        "Lane attempt identity does not match the workflow binding",
                        category="state",
                    )

                attempt = connection.execute(
                    "SELECT * FROM attempts WHERE workflow_id=? AND kind=? AND backend_run_id=?",
                    (workflow_id, kind, backend_run_id),
                ).fetchone()
                if attempt is None:
                    prior = connection.execute(
                        "SELECT * FROM attempts WHERE workflow_id=? AND kind=? "
                        "ORDER BY ordinal DESC LIMIT 1",
                        (workflow_id, kind),
                    ).fetchone()
                    ordinal = int(prior["ordinal"]) + 1 if prior else 1
                    attempt_id = f"{kind}-{ordinal:03d}-{uuid.uuid4().hex[:8]}"
                    if prior is not None and prior["status"] == "running":
                        connection.execute(
                            "UPDATE attempts SET status='superseded',finished_at=? "
                            "WHERE attempt_id=?",
                            (timestamp, prior["attempt_id"]),
                        )
                    connection.execute(
                        """
                        INSERT INTO attempts(
                            attempt_id,workflow_id,kind,ordinal,backend_run_id,status,
                            supersedes_attempt_id,launch_request_sha256,reason,started_at
                        ) VALUES(?,?,?,?,?,'running',?,?,?,?)
                        """,
                        (
                            attempt_id,
                            workflow_id,
                            kind,
                            ordinal,
                            backend_run_id,
                            prior["attempt_id"] if prior else None,
                            launch_request_sha256,
                            reason,
                            timestamp,
                        ),
                    )
                    self._insert_event(
                        connection,
                        workflow_id,
                        state_revision=revision,
                        event_type=f"{kind}.attempt_started",
                        source="pi-lane-orchestrator",
                        phase=f"{kind}_running",
                        severity="info",
                        attempt_id=attempt_id,
                        payload={
                            "backend_run_id": backend_run_id,
                            "ordinal": ordinal,
                            "supersedes_attempt_id": (
                                prior["attempt_id"] if prior else None
                            ),
                        },
                        failure=None,
                    )
                else:
                    if attempt["launch_request_sha256"] != launch_request_sha256:
                        raise OrchestratorError(
                            "ATTEMPT_IDENTITY_MISMATCH",
                            "backend attempt is already bound to a different launch request",
                            category="state",
                        )
                    if attempt["status"] != "running":
                        raise OrchestratorError(
                            "ATTEMPT_NOT_ACTIVE",
                            "a terminal backend attempt cannot be rebound",
                            category="state",
                        )
                    attempt_id = str(attempt["attempt_id"])

                next_revision = revision + 1
                connection.execute(
                    """
                    UPDATE workflows SET status=?,phase=?,revision=?,
                        rtl_closure_run_id=COALESCE(?,rtl_closure_run_id),
                        active_lane_run_id=COALESCE(?,active_lane_run_id),
                        failure_json=?,snapshot_json=?,updated_at=?
                    WHERE workflow_id=?
                    """,
                    (
                        status,
                        phase,
                        next_revision,
                        rtl_closure_run_id,
                        active_lane_run_id,
                        canonical_json(failure) if failure else None,
                        snapshot_text,
                        timestamp,
                        workflow_id,
                    ),
                )
                attempt_status = close_running_attempts_as
                if attempt_status is None and status in TERMINAL_STATUSES:
                    attempt_status = {
                        "completed": "completed",
                        "failed": "failed",
                        "cancelled": "cancelled",
                    }[status]
                if attempt_status is not None:
                    if attempt_status not in {"completed", "failed", "cancelled"}:
                        raise OrchestratorError(
                            "INVALID_ATTEMPT_STATUS",
                            f"unsupported terminal attempt status: {attempt_status}",
                        )
                    connection.execute(
                        """
                        UPDATE attempts SET status=?,finished_at=?,failure_json=?
                        WHERE workflow_id=? AND status='running'
                          AND (? IS NULL OR kind=?)
                        """,
                        (
                            attempt_status,
                            timestamp,
                            canonical_json(failure) if failure else None,
                            workflow_id,
                            close_attempt_kind,
                            close_attempt_kind,
                        ),
                    )
                self._insert_event(
                    connection,
                    workflow_id,
                    state_revision=next_revision,
                    event_type=event_type,
                    source="pi-lane-orchestrator",
                    phase=phase,
                    severity=(
                        "error"
                        if status == "failed"
                        else "warning"
                        if status.startswith("blocked")
                        else "info"
                    ),
                    attempt_id=attempt_id,
                    payload={"status": status, "revision": next_revision},
                    failure=failure,
                )
                if operation_id is not None:
                    if operation_outcome == "completed":
                        self._complete_operation_locked(
                            connection,
                            operation_id,
                            result={
                                "status": status,
                                "revision": next_revision,
                                "backend_run_id": backend_run_id,
                            },
                        )
                    elif operation_outcome == "failed" and failure is not None:
                        self._fail_operation_locked(
                            connection, operation_id, failure=failure
                        )
                    else:
                        raise OrchestratorError(
                            "INVALID_OPERATION_OUTCOME", "operation outcome is malformed"
                        )
                if status in TERMINAL_STATUSES:
                    connection.execute(
                        "UPDATE operations SET status='cancelled',next_attempt_epoch=0,"
                        "updated_at=?,completed_at=? WHERE workflow_id=? "
                        "AND status IN ('pending','executing') "
                        "AND (? IS NULL OR operation_id<>?)",
                        (timestamp, timestamp, workflow_id, operation_id, operation_id),
                    )
                attempt = connection.execute(
                    "SELECT * FROM attempts WHERE attempt_id=?", (attempt_id,)
                ).fetchone()
                connection.execute("COMMIT")
            except Exception:
                connection.execute("ROLLBACK")
                raise
        assert attempt is not None
        return self.get_workflow(workflow_id), self._attempt_dict(attempt)

    def _insert_event(
        self,
        connection: sqlite3.Connection,
        workflow_id: str,
        *,
        state_revision: int,
        event_type: str,
        source: str,
        phase: str,
        severity: str,
        attempt_id: str | None,
        payload: Mapping[str, Any],
        failure: Mapping[str, Any] | None,
    ) -> dict[str, Any]:
        payload_value = dict(payload)
        payload_text = canonical_json(payload_value)
        if len(payload_text.encode("utf-8")) > 16 * 1024:
            raise OrchestratorError("EVENT_TOO_LARGE", "event payload exceeds 16 KiB")
        prior = connection.execute(
            "SELECT sequence,sha256 FROM events WHERE workflow_id=? ORDER BY sequence DESC LIMIT 1",
            (workflow_id,),
        ).fetchone()
        sequence = int(prior["sequence"]) + 1 if prior else 1
        previous_sha = str(prior["sha256"]) if prior else None
        timestamp = now_iso()
        event_id = f"{workflow_id}:{sequence}"
        body = {
            "schema_version": EVENT_SCHEMA_VERSION,
            "event_id": event_id,
            "sequence": sequence,
            "timestamp": timestamp,
            "source": source,
            "workflow_id": workflow_id,
            "attempt_id": attempt_id,
            "type": event_type,
            "severity": severity,
            "state_revision": state_revision,
            "phase": phase,
            "payload": payload_value,
            "failure": dict(failure) if failure else None,
            "previous_sha256": previous_sha,
        }
        digest = sha256_text(canonical_json(body))
        connection.execute(
            """
            INSERT INTO events(
                workflow_id,sequence,event_id,timestamp,source,attempt_id,type,severity,
                state_revision,phase,payload_json,failure_json,previous_sha256,sha256
            ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)
            """,
            (
                workflow_id,
                sequence,
                event_id,
                timestamp,
                source,
                attempt_id,
                event_type,
                severity,
                state_revision,
                phase,
                payload_text,
                canonical_json(failure) if failure else None,
                previous_sha,
                digest,
            ),
        )
        return {**body, "sha256": digest}

    def append_event(
        self,
        workflow_id: str,
        *,
        event_type: str,
        source: str,
        phase: str,
        severity: str = "info",
        attempt_id: str | None = None,
        payload: Mapping[str, Any] | None = None,
        failure: Mapping[str, Any] | None = None,
    ) -> dict[str, Any]:
        with _connect(self.db_path) as connection:
            connection.execute("BEGIN IMMEDIATE")
            workflow = connection.execute(
                "SELECT * FROM workflows WHERE workflow_id=?", (workflow_id,)
            ).fetchone()
            if workflow is None:
                connection.execute("ROLLBACK")
                raise OrchestratorError("RUN_NOT_FOUND", f"workflow does not exist: {workflow_id}", category="state")
            self._verify_workflow_projection(connection, workflow)
            event = self._insert_event(
                connection,
                workflow_id,
                state_revision=int(workflow["revision"]),
                event_type=event_type,
                source=source,
                phase=phase,
                severity=severity,
                attempt_id=attempt_id,
                payload=dict(payload or {}),
                failure=failure,
            )
            connection.execute("COMMIT")
        return event

    def update_workflow(
        self,
        workflow_id: str,
        *,
        status: str,
        phase: str,
        snapshot: Mapping[str, Any],
        rtl_closure_run_id: str | None = None,
        active_lane_run_id: str | None = None,
        failure: Mapping[str, Any] | None = None,
        expected_revision: int | None = None,
        event_type: str = "workflow.transition",
        close_running_attempts_as: str | None = None,
        allow_terminal_degradation: bool = False,
        close_attempt_kind: str | None = None,
        operation_id: str | None = None,
        operation_outcome: str = "completed",
    ) -> dict[str, Any]:
        if status not in ACTIVE_STATUSES | TERMINAL_STATUSES:
            raise OrchestratorError("INVALID_STATUS", f"unsupported workflow status: {status}")
        snapshot_text = canonical_json(dict(snapshot))
        if len(snapshot_text.encode("utf-8")) > 256 * 1024:
            raise OrchestratorError("SNAPSHOT_TOO_LARGE", "workflow snapshot exceeds 256 KiB")
        timestamp = now_iso()
        with _connect(self.db_path) as connection:
            connection.execute("BEGIN IMMEDIATE")
            current = connection.execute(
                "SELECT * FROM workflows WHERE workflow_id=?", (workflow_id,)
            ).fetchone()
            if current is None:
                connection.execute("ROLLBACK")
                raise OrchestratorError("RUN_NOT_FOUND", f"workflow does not exist: {workflow_id}", category="state")
            self._verify_workflow_projection(connection, current)
            revision = int(current["revision"])
            if expected_revision is not None and revision != expected_revision:
                connection.execute("ROLLBACK")
                raise OrchestratorError(
                    "REVISION_CONFLICT",
                    f"expected revision {expected_revision}, found {revision}",
                    retryable=True,
                )
            terminal_degradation = bool(
                allow_terminal_degradation
                and current["status"] == "completed"
                and status == "failed"
            )
            if current["status"] in TERMINAL_STATUSES and not terminal_degradation:
                connection.execute("ROLLBACK")
                raise OrchestratorError(
                    "TERMINAL_STATE", "terminal workflow state is immutable; create a rerun"
                )
            next_revision = revision + 1
            connection.execute(
                """
                UPDATE workflows SET status=?,phase=?,revision=?,rtl_closure_run_id=COALESCE(?,rtl_closure_run_id),
                    active_lane_run_id=COALESCE(?,active_lane_run_id),failure_json=?,snapshot_json=?,updated_at=?
                WHERE workflow_id=?
                """,
                (
                    status,
                    phase,
                    next_revision,
                    rtl_closure_run_id,
                    active_lane_run_id,
                    canonical_json(failure) if failure else None,
                    snapshot_text,
                    timestamp,
                    workflow_id,
                ),
            )
            attempt_status = close_running_attempts_as
            if attempt_status is None and status in TERMINAL_STATUSES:
                attempt_status = {
                    "completed": "completed",
                    "failed": "failed",
                    "cancelled": "cancelled",
                }[status]
            if attempt_status is not None:
                if attempt_status not in {"completed", "failed", "cancelled"}:
                    connection.execute("ROLLBACK")
                    raise OrchestratorError(
                        "INVALID_ATTEMPT_STATUS",
                        f"unsupported terminal attempt status: {attempt_status}",
                    )
                connection.execute(
                    """
                    UPDATE attempts SET status=?,finished_at=?,failure_json=?
                    WHERE workflow_id=? AND status='running'
                      AND (? IS NULL OR kind=?)
                    """,
                    (
                        attempt_status,
                        timestamp,
                        canonical_json(failure) if failure else None,
                        workflow_id,
                        close_attempt_kind,
                        close_attempt_kind,
                    ),
                )
            self._insert_event(
                connection,
                workflow_id,
                state_revision=next_revision,
                event_type=event_type,
                source="pi-lane-orchestrator",
                phase=phase,
                severity=(
                    "error" if status == "failed" else "warning" if status.startswith("blocked") else "info"
                ),
                attempt_id=None,
                payload={"status": status, "revision": next_revision},
                failure=failure,
            )
            if operation_id is not None:
                if operation_outcome == "completed":
                    self._complete_operation_locked(
                        connection,
                        operation_id,
                        result={"status": status, "revision": next_revision},
                    )
                elif operation_outcome == "failed" and failure is not None:
                    self._fail_operation_locked(
                        connection, operation_id, failure=failure
                    )
                else:
                    raise OrchestratorError(
                        "INVALID_OPERATION_OUTCOME", "operation outcome is malformed"
                    )
            if status in TERMINAL_STATUSES:
                connection.execute(
                    "UPDATE operations SET status='cancelled',next_attempt_epoch=0,"
                    "updated_at=?,completed_at=? WHERE workflow_id=? "
                    "AND status IN ('pending','executing') "
                    "AND (? IS NULL OR operation_id<>?)",
                    (timestamp, timestamp, workflow_id, operation_id, operation_id),
                )
            connection.execute("COMMIT")
        return self.get_workflow(workflow_id)

    def events(
        self,
        workflow_id: str,
        *,
        after_sequence: int = 0,
        limit: int = 256,
        client_id: str | None = None,
        acknowledge: bool = False,
        max_bytes: int = 512 * 1024,
    ) -> dict[str, Any]:
        if (
            isinstance(after_sequence, bool)
            or after_sequence < 0
            or not 1 <= limit <= 1000
            or not 64 * 1024 <= max_bytes <= 900 * 1024
        ):
            raise OrchestratorError("INVALID_CURSOR", "invalid event cursor or limit")
        with _connect(self.db_path) as connection:
            connection.execute("BEGIN IMMEDIATE" if acknowledge else "BEGIN")
            exists = connection.execute(
                "SELECT * FROM workflows WHERE workflow_id=?", (workflow_id,)
            ).fetchone()
            if exists is None:
                connection.execute("ROLLBACK")
                raise OrchestratorError("RUN_NOT_FOUND", f"workflow does not exist: {workflow_id}", category="state")
            verified_rows = self._verified_event_rows(connection, workflow_id)
            journal_sequence = len(verified_rows)
            if after_sequence > journal_sequence:
                raise OrchestratorError(
                    "CURSOR_AHEAD",
                    f"event cursor {after_sequence} is ahead of journal sequence {journal_sequence}",
                    category="state",
                )
            rows = connection.execute(
                """
                SELECT * FROM events WHERE workflow_id=? AND sequence>?
                ORDER BY sequence LIMIT ?
                """,
                (workflow_id, after_sequence, limit + 1),
            ).fetchall()
            if after_sequence < journal_sequence and not rows:
                connection.execute("ROLLBACK")
                raise OrchestratorError(
                    "JOURNAL_INTEGRITY_ERROR",
                    f"event sequence {after_sequence + 1} is missing",
                    category="state",
                )
            if after_sequence == 0:
                prior_cursor_sha256: str | None = None
            else:
                prior_cursor = connection.execute(
                    "SELECT sha256 FROM events WHERE workflow_id=? AND sequence=?",
                    (workflow_id, after_sequence),
                ).fetchone()
                if prior_cursor is None:
                    connection.execute("ROLLBACK")
                    raise OrchestratorError(
                        "JOURNAL_INTEGRITY_ERROR",
                        f"event sequence {after_sequence} is missing",
                        category="state",
                    )
                prior_cursor_sha256 = str(prior_cursor["sha256"])
            snapshot = self._compact_status_from_row(connection, exists)
            event_budget = max_bytes - len(canonical_json(snapshot).encode("utf-8")) - 4096
            events: list[dict[str, Any]] = []
            used = 0
            for row in rows[:limit]:
                expected_sequence = after_sequence + len(events) + 1
                if (
                    int(row["sequence"]) != expected_sequence
                    or row["previous_sha256"] != prior_cursor_sha256
                ):
                    connection.execute("ROLLBACK")
                    raise OrchestratorError(
                        "JOURNAL_INTEGRITY_ERROR",
                        f"event chain is discontinuous at sequence {expected_sequence}",
                        category="state",
                    )
                event = self._event_dict(row)
                size = len(canonical_json(event).encode("utf-8"))
                if events and used + size > event_budget:
                    break
                if size > event_budget:
                    connection.execute("ROLLBACK")
                    raise OrchestratorError(
                        "EVENT_PAGE_TOO_SMALL", "max_bytes cannot contain the next event"
                    )
                events.append(event)
                used += size
                prior_cursor_sha256 = str(row["sha256"])
            next_sequence = events[-1]["sequence"] if events else after_sequence
            has_more = next_sequence < journal_sequence
            if events:
                cursor_sha256 = events[-1]["sha256"]
            elif next_sequence == 0:
                cursor_sha256 = sha256_text("")
            else:
                assert prior_cursor_sha256 is not None
                cursor_sha256 = prior_cursor_sha256
            if client_id and acknowledge:
                connection.execute(
                    """
                    INSERT INTO client_cursors(client_id,workflow_id,sequence,updated_at)
                    VALUES(?,?,?,?) ON CONFLICT(client_id,workflow_id) DO UPDATE SET
                    sequence=MAX(sequence,excluded.sequence),updated_at=excluded.updated_at
                    """,
                    (client_id, workflow_id, next_sequence, now_iso()),
                )
            cursor = connection.execute(
                "SELECT sequence FROM client_cursors WHERE client_id=? AND workflow_id=?",
                (client_id, workflow_id),
            ).fetchone() if client_id else None
            connection.execute("COMMIT")
        return {
            "workflow_id": workflow_id,
            "journal_id": workflow_id,
            "generation": workflow_id,
            "after_sequence": after_sequence,
            "next_after_sequence": next_sequence,
            "journal_sequence": journal_sequence,
            "snapshot_revision": int(snapshot["revision"]),
            "snapshot_journal_sequence": journal_sequence,
            "has_more": has_more,
            "events": events,
            "cursor_sha256": cursor_sha256,
            "acknowledged_sequence": int(cursor["sequence"]) if cursor else None,
            "snapshot": snapshot,
        }

    @staticmethod
    def _event_dict(row: sqlite3.Row) -> dict[str, Any]:
        value = dict(row)
        value["payload"] = json.loads(value.pop("payload_json"))
        raw_failure = value.pop("failure_json")
        value["failure"] = json.loads(raw_failure) if raw_failure else None
        return value

    @staticmethod
    def _event_hash_body(event: Mapping[str, Any]) -> dict[str, Any]:
        return {
            "schema_version": EVENT_SCHEMA_VERSION,
            "event_id": event["event_id"],
            "sequence": event["sequence"],
            "timestamp": event["timestamp"],
            "source": event["source"],
            "workflow_id": event["workflow_id"],
            "attempt_id": event["attempt_id"],
            "type": event["type"],
            "severity": event["severity"],
            "state_revision": event["state_revision"],
            "phase": event["phase"],
            "payload": event["payload"],
            "failure": event["failure"],
            "previous_sha256": event["previous_sha256"],
        }

    @classmethod
    def _verify_workflow_projection(
        cls, connection: sqlite3.Connection, workflow: sqlite3.Row
    ) -> None:
        rows = cls._verified_event_rows(
            connection, str(workflow["workflow_id"])
        )
        projection: dict[str, Any] | None = None
        for row in reversed(rows):
            try:
                event = cls._event_dict(row)
            except (TypeError, ValueError, json.JSONDecodeError) as exc:
                raise OrchestratorError(
                    "JOURNAL_INTEGRITY_ERROR",
                    "workflow projection event is malformed",
                    category="state",
                ) from exc
            payload = event.get("payload")
            if isinstance(payload, dict) and {
                "status",
                "revision",
            }.issubset(payload):
                projection = event
                break
            if event.get("type") == "workflow.created" and int(
                workflow["revision"]
            ) == 0:
                projection = {
                    **event,
                    "payload": {"status": "created", "revision": 0},
                }
                break
        if projection is None:
            raise OrchestratorError(
                "JOURNAL_INTEGRITY_ERROR",
                "workflow projection has no matching state event",
                category="state",
            )
        payload = projection["payload"]
        if (
            projection.get("state_revision") != int(workflow["revision"])
            or projection.get("phase") != workflow["phase"]
            or payload.get("revision") != int(workflow["revision"])
            or payload.get("status") != workflow["status"]
        ):
            raise OrchestratorError(
                "JOURNAL_INTEGRITY_ERROR",
                "workflow projection disagrees with its latest state event",
                category="state",
            )

    @classmethod
    def _verified_event_rows(
        cls, connection: sqlite3.Connection, workflow_id: str
    ) -> list[sqlite3.Row]:
        rows = connection.execute(
            "SELECT * FROM events WHERE workflow_id=? ORDER BY sequence",
            (workflow_id,),
        ).fetchall()
        previous_sha256: str | None = None
        for expected_sequence, row in enumerate(rows, start=1):
            try:
                event = cls._event_dict(row)
                sequence = int(row["sequence"])
            except (TypeError, ValueError, json.JSONDecodeError) as exc:
                raise OrchestratorError(
                    "JOURNAL_INTEGRITY_ERROR",
                    f"event {expected_sequence} contains malformed data",
                    category="state",
                ) from exc
            if (
                sequence != expected_sequence
                or row["previous_sha256"] != previous_sha256
            ):
                raise OrchestratorError(
                    "JOURNAL_INTEGRITY_ERROR",
                    f"event chain is discontinuous at sequence {expected_sequence}",
                    category="state",
                )
            stored_sha256 = str(row["sha256"])
            if (
                sha256_text(canonical_json(cls._event_hash_body(event)))
                != stored_sha256
            ):
                raise OrchestratorError(
                    "JOURNAL_INTEGRITY_ERROR",
                    f"event hash mismatch at sequence {expected_sequence}",
                    category="state",
                )
            previous_sha256 = stored_sha256
        return rows

    def _compact_status_from_row(
        self, connection: sqlite3.Connection, row: sqlite3.Row
    ) -> dict[str, Any]:
        self._verify_workflow_projection(connection, row)
        workflow = self._workflow_dict(row)
        snapshot = workflow.get("snapshot") or {}
        if not isinstance(snapshot, dict):
            snapshot = {}
        failure = workflow.get("failure")
        compact = {
            "workflow_id": workflow["workflow_id"],
            "design": workflow["design_name"],
            "pdk": workflow["pdk"],
            "status": workflow["status"],
            "phase": workflow["phase"],
            "revision": workflow["revision"],
            "rtl_closure_run_id": workflow.get("rtl_closure_run_id"),
            "lane_run_id": workflow.get("active_lane_run_id"),
            "current_step": snapshot.get("current_step"),
            "last_completed_step": snapshot.get("last_completed_step"),
            "completed_step_count": snapshot.get("completed_step_count", 0),
            "elapsed_seconds": snapshot.get("elapsed_seconds", 0),
            "model_request": snapshot.get("model_request"),
            "repair": snapshot.get("repair"),
            "verification": snapshot.get("verification"),
            "qor": snapshot.get("qor"),
            "delivery_gate": snapshot.get("delivery_gate"),
            "orchestrator_observer": snapshot.get("orchestrator_observer"),
            "failure": failure,
            "updated_at": workflow["updated_at"],
        }
        encoded = canonical_json(compact).encode("utf-8")
        if len(encoded) > 8 * 1024:
            for key in ("verification", "repair", "model_request", "qor"):
                value = compact.get(key)
                if isinstance(value, dict):
                    compact[key] = {
                        field: value.get(field)
                        for field in ("status", "phase", "event", "round", "candidate", "gate")
                        if field in value
                    }
                if len(canonical_json(compact).encode("utf-8")) <= 8 * 1024:
                    break
        if len(canonical_json(compact).encode("utf-8")) > 8 * 1024:
            delivery = compact.get("delivery_gate")
            if isinstance(delivery, Mapping):
                compact["delivery_gate"] = {
                    key: delivery.get(key)
                    for key in ("status", "passed", "complete", "gate")
                    if key in delivery
                }
            failure_value = compact.get("failure")
            if isinstance(failure_value, Mapping):
                compact["failure"] = {
                    "code": failure_value.get("code"),
                    "domain": failure_value.get("domain"),
                    "message": str(failure_value.get("message") or "")[:1000],
                    "retryable": failure_value.get("retryable"),
                    "repairable_by_rtl": failure_value.get("repairable_by_rtl"),
                    "user_action_required": failure_value.get("user_action_required"),
                }
        for key, value in tuple(compact.items()):
            if isinstance(value, str) and len(value) > 1024:
                compact[key] = value[:1021] + "..."
        for key in (
            "verification",
            "repair",
            "model_request",
            "qor",
            "delivery_gate",
            "failure",
        ):
            if len(canonical_json(compact).encode("utf-8")) <= 8 * 1024:
                break
            compact[key] = None
        return compact

    def compact_status(self, workflow_id: str) -> dict[str, Any]:
        with _connect(self.db_path) as connection:
            connection.execute("BEGIN")
            row = connection.execute(
                "SELECT * FROM workflows WHERE workflow_id=?", (workflow_id,)
            ).fetchone()
            if row is None:
                connection.execute("ROLLBACK")
                raise OrchestratorError(
                    "RUN_NOT_FOUND",
                    f"workflow does not exist: {workflow_id}",
                    category="state",
                )
            compact = self._compact_status_from_row(connection, row)
            connection.execute("COMMIT")
            return compact

    def active_for_reconcile(self) -> list[dict[str, Any]]:
        placeholders = ",".join("?" for _ in EXECUTING_STATUSES)
        with _connect(self.db_path) as connection:
            rows = connection.execute(
                f"SELECT * FROM workflows WHERE (status IN ({placeholders}) "
                "OR (status='blocked_infrastructure' AND rtl_closure_run_id IS NOT NULL) "
                "OR EXISTS (SELECT 1 FROM operations WHERE operations.workflow_id=workflows.workflow_id "
                "AND operations.status IN ('pending','executing'))) "
                "ORDER BY updated_at ASC LIMIT 1000",
                tuple(sorted(EXECUTING_STATUSES)),
            ).fetchall()
            for row in rows:
                self._verify_workflow_projection(connection, row)
            return [self._workflow_dict(row) for row in rows]

    def terminal_audits_due(
        self, *, now_epoch: float | None = None, limit: int = 1
    ) -> list[dict[str, Any]]:
        if not 1 <= limit <= 32:
            raise OrchestratorError(
                "INVALID_LIMIT", "terminal audit limit must be from 1 to 32"
            )
        current_epoch = time.time() if now_epoch is None else float(now_epoch)
        with _connect(self.db_path) as connection:
            rows = connection.execute(
                """
                SELECT workflows.* FROM workflows
                LEFT JOIN terminal_integrity_audits AS audits
                    ON audits.workflow_id=workflows.workflow_id
                WHERE workflows.status='completed'
                  AND workflows.rtl_closure_run_id IS NOT NULL
                  AND (
                    audits.workflow_id IS NULL
                    OR audits.checked_revision != workflows.revision
                    OR audits.next_check_epoch <= ?
                  )
                ORDER BY COALESCE(audits.next_check_epoch, 0), workflows.updated_at
                LIMIT ?
                """,
                (current_epoch, limit),
            ).fetchall()
            for row in rows:
                self._verify_workflow_projection(connection, row)
            return [self._workflow_dict(row) for row in rows]

    def record_terminal_integrity_audit(
        self,
        workflow_id: str,
        *,
        checked_revision: int,
        backend_status: str,
        backend_snapshot_sha256: str,
        checked_epoch: float | None = None,
        interval_seconds: float = 300.0,
    ) -> None:
        if (
            isinstance(checked_revision, bool)
            or not isinstance(checked_revision, int)
            or checked_revision < 0
            or not isinstance(backend_status, str)
            or not backend_status
            or len(backend_status) > 128
            or not re.fullmatch(r"[0-9a-f]{64}", backend_snapshot_sha256)
            or interval_seconds <= 0
        ):
            raise OrchestratorError(
                "INVALID_INTEGRITY_AUDIT", "terminal integrity audit is malformed"
            )
        current_epoch = time.time() if checked_epoch is None else float(checked_epoch)
        with _connect(self.db_path) as connection:
            connection.execute("BEGIN IMMEDIATE")
            workflow = connection.execute(
                "SELECT * FROM workflows WHERE workflow_id=?", (workflow_id,)
            ).fetchone()
            if workflow is None:
                connection.execute("ROLLBACK")
                raise OrchestratorError(
                    "RUN_NOT_FOUND",
                    f"workflow does not exist: {workflow_id}",
                    category="state",
                )
            self._verify_workflow_projection(connection, workflow)
            if int(workflow["revision"]) != checked_revision:
                connection.execute("ROLLBACK")
                raise OrchestratorError(
                    "REVISION_CONFLICT",
                    "workflow changed before its integrity audit was recorded",
                    retryable=True,
                )
            connection.execute(
                """
                INSERT INTO terminal_integrity_audits(
                    workflow_id,checked_revision,last_checked_at,next_check_epoch,
                    backend_status,backend_snapshot_sha256
                ) VALUES(?,?,?,?,?,?)
                ON CONFLICT(workflow_id) DO UPDATE SET
                    checked_revision=excluded.checked_revision,
                    last_checked_at=excluded.last_checked_at,
                    next_check_epoch=excluded.next_check_epoch,
                    backend_status=excluded.backend_status,
                    backend_snapshot_sha256=excluded.backend_snapshot_sha256
                """,
                (
                    workflow_id,
                    checked_revision,
                    now_iso(),
                    current_epoch + float(interval_seconds),
                    backend_status,
                    backend_snapshot_sha256,
                ),
            )
            connection.execute("COMMIT")

    def acquire_lease(
        self,
        workflow_id: str,
        *,
        owner: str,
        ttl_seconds: int = DEFAULT_LEASE_TTL_SECONDS,
    ) -> str | None:
        now = time.time()
        pid = os.getpid()
        start_time = process_start_time(pid)
        token = uuid.uuid4().hex
        with _connect(self.db_path) as connection:
            connection.execute("BEGIN IMMEDIATE")
            workflow = connection.execute(
                "SELECT * FROM workflows WHERE workflow_id=?", (workflow_id,)
            ).fetchone()
            if workflow is None:
                connection.execute("ROLLBACK")
                raise OrchestratorError(
                    "RUN_NOT_FOUND", f"workflow does not exist: {workflow_id}", category="state"
                )
            self._verify_workflow_projection(connection, workflow)
            current = connection.execute(
                "SELECT * FROM leases WHERE workflow_id=?", (workflow_id,)
            ).fetchone()
            current_alive = False
            if current is not None:
                recorded_start = current["process_start_time"]
                observed_start = process_start_time(int(current["pid"]))
                if recorded_start is not None and observed_start is not None:
                    current_alive = str(recorded_start) == str(observed_start)
                else:
                    try:
                        os.kill(int(current["pid"]), 0)
                    except ProcessLookupError:
                        current_alive = False
                    except (PermissionError, OSError):
                        current_alive = True
                    else:
                        current_alive = True
            if current and float(current["expires_at"]) > now and current_alive:
                connection.execute("COMMIT")
                return None
            connection.execute(
                """
                INSERT INTO leases(workflow_id,lease_token,owner,pid,process_start_time,heartbeat_at,expires_at)
                VALUES(?,?,?,?,?,?,?) ON CONFLICT(workflow_id) DO UPDATE SET
                lease_token=excluded.lease_token,owner=excluded.owner,pid=excluded.pid,
                process_start_time=excluded.process_start_time,
                heartbeat_at=excluded.heartbeat_at,expires_at=excluded.expires_at
                """,
                (workflow_id, token, owner, pid, start_time, now_iso(), now + ttl_seconds),
            )
            connection.execute("COMMIT")
        return token

    def release_lease(self, workflow_id: str, *, owner: str, token: str) -> None:
        with _connect(self.db_path) as connection:
            connection.execute(
                "DELETE FROM leases WHERE workflow_id=? AND lease_token=? AND owner=? AND pid=?",
                (workflow_id, token, owner, os.getpid()),
            )


def process_start_time(pid: int) -> str | None:
    identity = proc_identity(pid)
    return str(identity[1]) if identity is not None else None


def parse_json_stdout(
    completed: subprocess.CompletedProcess[str],
    *,
    command: str,
    allow_nonzero_status: bool = False,
) -> dict[str, Any]:
    stdout = completed.stdout.strip()
    try:
        value = json.loads(stdout)
    except json.JSONDecodeError as exc:
        value = None
        parse_error = exc
    else:
        parse_error = None
    if completed.returncode != 0 and not (
        allow_nonzero_status and isinstance(value, dict) and value.get("status")
    ):
        detail = (completed.stderr.strip() or stdout or f"exit code {completed.returncode}")[:2000]
        raise OrchestratorError("BACKEND_COMMAND_FAILED", f"{command}: {detail}", retryable=True)
    if parse_error is not None:
        raise OrchestratorError(
            "INVALID_BACKEND_JSON", f"{command} returned invalid JSON: {parse_error}"
        ) from parse_error
    if not isinstance(value, dict):
        raise OrchestratorError("INVALID_BACKEND_JSON", f"{command} must return one JSON object")
    return value
