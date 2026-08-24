"""Local Unix-socket service coordinating Pi Lane workflows."""

from __future__ import annotations

import argparse
import fcntl
import json
import logging
import os
import signal
import socketserver
import subprocess
import threading
import time
import uuid
from pathlib import Path
from typing import Any, Callable, Mapping

from . import __version__
from .orchestrator import (
    DEFAULT_DB,
    DEFAULT_LOCK,
    DEFAULT_SOCKET,
    DATABASE_SCHEMA_VERSION,
    EXECUTING_STATUSES,
    ORCHESTRATOR_BUILD_ID,
    PROTOCOL_VERSION,
    SCHEMA_VERSION,
    SAFE_IDENTIFIER,
    TERMINAL_STATUSES,
    OrchestratorError,
    OrchestratorStore,
    canonical_json,
    canonical_spec_path,
    classify_backend_failure,
    ensure_private_directory,
    error_envelope,
    now_iso,
    parse_json_stdout,
    rtl_closure_runtime_config_fingerprint,
    sha256_text,
    structured_failure,
    success_envelope,
    unix_socket_endpoint_state,
)
from .integrity import file_sha256


MAX_REQUEST_BYTES = 1024 * 1024
MAX_RESPONSE_BYTES = 1024 * 1024
RECONCILE_INTERVAL_SECONDS = 5.0
RECONCILE_BACKOFF_MAX_SECONDS = 60.0
TERMINAL_INTEGRITY_AUDIT_INTERVAL_SECONDS = 300.0
DAEMON_HANDOFF_PROTOCOL_VERSION = 1
ORCHESTRATOR_OBSERVER_KEY = "orchestrator_observer"
MUTATING_METHODS = frozenset(
    {"run.start", "run.stop", "run.retry", "run.rerun"}
)
RTL_CLOSURE_REPORT_STATUSES = frozenset(
    {"running", "completed", "failed", "incomplete", "stopped"}
)
LOGGER = logging.getLogger(__name__)


def _backend_snapshot(report: Mapping[str, Any]) -> dict[str, Any]:
    """Bound the global projection; complete evidence stays in backend artifacts."""

    fields = (
        "schema_version",
        "run_id",
        "design",
        "pdk",
        "status",
        "component",
        "phase",
        "current_step",
        "last_completed_step",
        "completed_step_count",
        "started_step_count",
        "elapsed_seconds",
        "model_request",
        "coverage",
        "verification",
        "repair",
        "architecture_audit",
        "synthesis_equivalence",
        "mapped_equivalence_mode",
        "mapped_equivalence",
        "lane_run_id",
        "process_status",
        "flow_scope",
        "delivery_gate",
        "delivery_gate_reasons",
        "expected_final_step",
        "endpoint_reached",
        "status_detail",
        "metrics_path",
        "qor",
        "design_config",
        "publication_manifest",
        "publication_bundle_sha256",
        "lane_input_bundle_sha256",
        "release_integrity",
        "delivery_attestation_sha256",
        "last_integrity_audit_id",
        "failure_class",
        "failure_gate",
        "failure_code",
        "state_file",
        "pipeline_log",
    )
    snapshot = {key: report.get(key) for key in fields if key in report}
    artifacts = report.get("artifacts")
    if isinstance(artifacts, Mapping):
        snapshot["artifacts"] = {
            str(key): str(value)[:4096]
            for key, value in list(artifacts.items())[:128]
            if isinstance(key, str) and isinstance(value, (str, int, float, bool))
        }
    errors = report.get("errors")
    if isinstance(errors, list):
        snapshot["errors"] = [str(value)[:1000] for value in errors[-8:]]
    origin = report.get("origin")
    if isinstance(origin, Mapping):
        snapshot["origin"] = {
            key: origin.get(key)
            for key in (
                "source",
                "workflow_id",
                "idempotency_key",
                "idempotency_scope",
                "spec_sha256",
            )
        }
    # Backend verification reports can be very large. Retain only progress/gate data.
    for key in ("verification", "coverage", "repair", "architecture_audit", "synthesis_equivalence"):
        value = snapshot.get(key)
        if isinstance(value, Mapping):
            snapshot[key] = {
                field: value.get(field)
                for field in (
                    "status",
                    "passed",
                    "phase",
                    "round",
                    "max_rounds",
                    "candidate",
                    "candidate_count",
                    "failure_class",
                    "failure_code",
                    "detail",
                    "artifact",
                    "gate",
                )
                if field in value
            }
    release_integrity = snapshot.get("release_integrity")
    if isinstance(release_integrity, Mapping):
        snapshot["release_integrity"] = {
            "schema_version": release_integrity.get("schema_version"),
            "audit_id": release_integrity.get("audit_id"),
            "status": release_integrity.get("status"),
            "checked_at": release_integrity.get("checked_at"),
            "attestation_sha256": release_integrity.get("attestation_sha256"),
            "errors": [
                str(value)[:2048]
                for value in (release_integrity.get("errors") or [])[:16]
            ],
        }
    return snapshot


def _workflow_status(report: Mapping[str, Any], failure: Mapping[str, Any] | None) -> str:
    status = str(report.get("status") or "running")
    if status == "completed":
        return "completed"
    if status == "stopped":
        return "cancelled"
    if status in {"failed", "incomplete"}:
        domain = str((failure or {}).get("domain") or "")
        repairable = bool((failure or {}).get("repairable_by_rtl"))
        if repairable:
            return "failed"
        if domain in {"physical_tool", "user_intent"}:
            return "waiting_user"
        return "failed"
    return "running"


def _child_start_idempotency_key(workflow: Mapping[str, Any]) -> str:
    return "pi-lane:" + sha256_text(
        str(workflow["workflow_id"])
        + ":"
        + str(workflow["spec_sha256"])
        + ":"
        + str(workflow["idempotency_key"])
    )


def _rtl_closure_spec_sha256(workflow: Mapping[str, Any]) -> str:
    path = canonical_spec_path(Path(str(workflow["spec_path"])))
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise OrchestratorError(
            "INVALID_SPEC", f"cannot derive RTL-Closure specification identity: {exc}", category="state"
        ) from exc
    return sha256_text(canonical_json(raw))


def _rtl_closure_state_root() -> Path:
    return Path(
        os.environ.get("RTL_CLOSURE_STATE_DIR", "~/.local/state/rtl-closure")
    ).expanduser().resolve()


def _validate_rtl_closure_report(
    report: Mapping[str, Any],
    *,
    expected_run_id: str,
    workflow: Mapping[str, Any],
    expected_integrity_audit_id: str | None = None,
) -> None:
    if report.get("schema_version") != 1:
        raise OrchestratorError(
            "BACKEND_SCHEMA_MISMATCH",
            f"RTL-Closure report schema {report.get('schema_version')!r} is unsupported",
            category="state",
        )
    reported_run_id = report.get("run_id")
    if (
        not isinstance(reported_run_id, str)
        or SAFE_IDENTIFIER.fullmatch(reported_run_id) is None
    ):
        raise OrchestratorError(
            "INVALID_BACKEND_JSON",
            "RTL-Closure report has no safe run_id",
            category="state",
        )
    if reported_run_id != expected_run_id:
        raise OrchestratorError(
            "BACKEND_IDENTITY_MISMATCH",
            f"RTL-Closure report returned {reported_run_id}, expected {expected_run_id}",
            category="state",
        )
    origin = report.get("origin")
    expected_origin = {
        "source": "pi-lane",
        "workflow_id": workflow.get("workflow_id"),
        "idempotency_key": _child_start_idempotency_key(workflow),
        "idempotency_scope": workflow.get("workflow_id"),
        "spec_sha256": _rtl_closure_spec_sha256(workflow),
    }
    if not isinstance(origin, Mapping) or any(
        origin.get(key) != value for key, value in expected_origin.items()
    ):
        raise OrchestratorError(
            "BACKEND_IDENTITY_MISMATCH",
            "RTL-Closure report origin does not match the frozen Pi-Lane workflow",
            category="state",
        )
    if report.get("design") != workflow.get("design_name") or report.get("pdk") != workflow.get("pdk"):
        raise OrchestratorError(
            "BACKEND_IDENTITY_MISMATCH",
            "RTL-Closure report design identity does not match the Pi-Lane workflow",
            category="state",
        )
    expected_state_file = _rtl_closure_state_root() / expected_run_id / "state.json"
    state_file = report.get("state_file")
    if not isinstance(state_file, str) or Path(state_file).expanduser().resolve() != expected_state_file:
        raise OrchestratorError(
            "BACKEND_STATE_ROOT_MISMATCH",
            "RTL-Closure report state_file is outside the configured state root",
            category="state",
        )
    status = report.get("status")
    if not isinstance(status, str) or status not in RTL_CLOSURE_REPORT_STATUSES:
        raise OrchestratorError(
            "INVALID_BACKEND_JSON",
            f"RTL-Closure report has an unsupported status: {status!r}",
            category="state",
        )
    lane_run_id = report.get("lane_run_id")
    if lane_run_id is not None and (
        not isinstance(lane_run_id, str)
        or not SAFE_IDENTIFIER.fullmatch(lane_run_id)
    ):
        raise OrchestratorError(
            "INVALID_BACKEND_JSON",
            "RTL-Closure report has an invalid Lane run id",
            category="state",
        )
    release_integrity = report.get("release_integrity")
    delivery_attestation_sha256 = report.get("delivery_attestation_sha256")
    last_integrity_audit_id = report.get("last_integrity_audit_id")
    integrity_fields = {
        "release_integrity",
        "delivery_attestation_sha256",
        "last_integrity_audit_id",
    }
    integrity_evidence_present = any(
        value is not None
        for value in (
            release_integrity,
            delivery_attestation_sha256,
            last_integrity_audit_id,
        )
    )
    if (
        integrity_evidence_present or expected_integrity_audit_id is not None
    ) and not integrity_fields <= report.keys():
        raise OrchestratorError(
            "BACKEND_RELEASE_INTEGRITY_INVALID",
            "RTL-Closure report omitted required release integrity evidence",
            category="state",
        )
    if integrity_evidence_present or expected_integrity_audit_id is not None:
        integrity_status = (
            release_integrity.get("status")
            if isinstance(release_integrity, Mapping)
            else None
        )
        attestation_binding = (
            release_integrity.get("attestation_sha256")
            if isinstance(release_integrity, Mapping)
            else None
        )
        integrity_errors = (
            release_integrity.get("errors")
            if isinstance(release_integrity, Mapping)
            else None
        )
        errors_are_bounded = (
            isinstance(integrity_errors, list)
            and len(integrity_errors) <= 16
            and all(
                isinstance(error, str) and bool(error.strip())
                for error in integrity_errors
            )
        )
        errors_match_status = errors_are_bounded and (
            (integrity_status == "verified" and integrity_errors == [])
            or (
                integrity_status in {"unknown", "degraded"}
                and bool(integrity_errors)
            )
        )
        hash_is_valid = (
            isinstance(delivery_attestation_sha256, str)
            and len(delivery_attestation_sha256) == 64
            and all(
                character in "0123456789abcdef"
                for character in delivery_attestation_sha256
            )
        )
        degraded_without_attestation = (
            status == "incomplete"
            and integrity_status == "degraded"
            and delivery_attestation_sha256 is None
            and attestation_binding is None
            and isinstance(release_integrity, Mapping)
            and "attestation_sha256" in release_integrity
        )
        if (
            not isinstance(release_integrity, Mapping)
            or release_integrity.get("schema_version") != 1
            or integrity_status not in {"verified", "unknown", "degraded"}
            or not isinstance(release_integrity.get("audit_id"), str)
            or not SAFE_IDENTIFIER.fullmatch(str(release_integrity["audit_id"]))
            or not errors_match_status
            or not (hash_is_valid or degraded_without_attestation)
            or attestation_binding != delivery_attestation_sha256
            or last_integrity_audit_id != release_integrity.get("audit_id")
        ):
            raise OrchestratorError(
                "BACKEND_RELEASE_INTEGRITY_INVALID",
                "RTL-Closure release integrity evidence is malformed or unbound",
                category="state",
            )
        if (
            expected_integrity_audit_id is not None
            and last_integrity_audit_id != expected_integrity_audit_id
        ):
            raise OrchestratorError(
                "BACKEND_AUDIT_STALE",
                "RTL-Closure report does not contain the requested fresh integrity audit",
                category="state",
                retryable=True,
            )
    if status != "completed":
        return
    contradictions: list[str] = []
    required = (
        ("process_status", report.get("process_status"), "completed"),
        ("flow_scope", report.get("flow_scope"), "full"),
        ("endpoint_reached", report.get("endpoint_reached"), True),
        ("delivery_gate", report.get("delivery_gate"), "pass"),
    )
    for field, actual, expected in required:
        if actual != expected or (
            field == "endpoint_reached" and actual is not True
        ):
            contradictions.append(f"{field}={actual!r}, expected {expected!r}")
    if not isinstance(lane_run_id, str) or not lane_run_id:
        contradictions.append("lane_run_id is missing")
    if not isinstance(release_integrity, Mapping):
        contradictions.append("release_integrity is missing")
    else:
        if release_integrity.get("status") != "verified":
            contradictions.append(
                f"release_integrity.status={release_integrity.get('status')!r}, "
                "expected 'verified'"
            )
        if release_integrity.get("errors") != []:
            contradictions.append("release_integrity.errors is not empty")
    if expected_integrity_audit_id is not None and (
        last_integrity_audit_id != expected_integrity_audit_id
    ):
        raise OrchestratorError(
            "BACKEND_AUDIT_STALE",
            "RTL-Closure completed report contains a stale integrity audit",
            category="state",
            retryable=True,
        )
    if contradictions:
        raise OrchestratorError(
            "BACKEND_TERMINAL_CONTRADICTION",
            "RTL-Closure completed report violates the delivery contract: "
            + "; ".join(contradictions),
            category="state",
        )


def _observer_snapshot(workflow: Mapping[str, Any]) -> dict[str, Any]:
    snapshot = workflow.get("snapshot")
    if not isinstance(snapshot, Mapping):
        return {}
    observer = snapshot.get(ORCHESTRATOR_OBSERVER_KEY)
    return dict(observer) if isinstance(observer, Mapping) else {}


def _reconcile_is_due(workflow: Mapping[str, Any], *, now_epoch: float) -> bool:
    observer = _observer_snapshot(workflow)
    if observer.get("status") != "degraded":
        return True
    next_epoch = observer.get("next_reconcile_epoch")
    if (
        isinstance(next_epoch, bool)
        or not isinstance(next_epoch, (int, float))
    ):
        return True
    return float(next_epoch) <= now_epoch


def _degraded_backend_snapshot(
    workflow: Mapping[str, Any], error: OrchestratorError
) -> tuple[dict[str, Any], dict[str, Any]]:
    snapshot = dict(workflow.get("snapshot") or {})
    prior = _observer_snapshot(workflow)
    prior_count = prior.get("consecutive_failures")
    if isinstance(prior_count, bool) or not isinstance(prior_count, int):
        prior_count = 0
    consecutive = min(prior_count + 1, 32)
    backoff = min(
        RECONCILE_INTERVAL_SECONDS * (2 ** min(consecutive - 1, 4)),
        RECONCILE_BACKOFF_MAX_SECONDS,
    )
    current_epoch = time.time()
    observer = {
        "status": "degraded",
        "consecutive_failures": consecutive,
        "backoff_seconds": backoff,
        "last_failure_at": now_iso(),
        "next_reconcile_epoch": current_epoch + backoff,
        "last_error": {
            "code": error.code,
            "message": str(error)[:2000],
            "retryable": True,
        },
    }
    snapshot[ORCHESTRATOR_OBSERVER_KEY] = observer
    return snapshot, observer


def _backend_snapshot_with_recovery(
    workflow: Mapping[str, Any], report: Mapping[str, Any]
) -> tuple[dict[str, Any], bool]:
    snapshot = _backend_snapshot(report)
    prior = _observer_snapshot(workflow)
    recovered = prior.get("status") == "degraded"
    if prior:
        if recovered:
            snapshot[ORCHESTRATOR_OBSERVER_KEY] = {
                "status": "healthy",
                "consecutive_failures": 0,
                "last_recovered_at": now_iso(),
            }
        else:
            snapshot[ORCHESTRATOR_OBSERVER_KEY] = prior
    return snapshot, recovered


def _meaningful_snapshot(value: Mapping[str, Any]) -> dict[str, Any]:
    """Exclude poll-time counters so observation does not create event floods."""

    result = dict(value)
    result.pop("elapsed_seconds", None)
    request = result.get("model_request")
    if isinstance(request, Mapping):
        result["model_request"] = {
            key: item
            for key, item in request.items()
            if key not in {"elapsed_seconds", "chunk_count", "last_event_at"}
        }
    return result


class OrchestratorService:
    def __init__(
        self,
        store: OrchestratorStore,
        *,
        command_runner: Callable[..., subprocess.CompletedProcess[str]] = subprocess.run,
        socket_path: Path | None = None,
        lock_path: Path | None = None,
        runtime_config_fingerprint: str | None = None,
    ) -> None:
        self.store = store
        self.command_runner = command_runner
        self.socket_path = socket_path.expanduser().resolve() if socket_path else None
        self.lock_path = lock_path.expanduser().resolve() if lock_path else None
        self.runtime_config_fingerprint = (
            runtime_config_fingerprint or rtl_closure_runtime_config_fingerprint()
        )
        self.condition = threading.Condition()
        self.stopping = threading.Event()
        self.mutation_lock = threading.Lock()
        self._active_mutations: dict[str, int] = {}
        self.start_lock = threading.Lock()
        self.observer_lock = threading.Lock()
        self.observer_health: dict[str, Any] = {
            "status": "starting",
            "last_reconcile_at": None,
            "last_success_at": None,
            "last_error": None,
            "consecutive_failures": 0,
        }
        self.terminal_audit_health: dict[str, Any] = {
            "status": "starting",
            "last_reconcile_at": None,
            "last_success_at": None,
            "last_error": None,
            "consecutive_failures": 0,
        }
        self._last_observer_event: dict[str, tuple[str, float]] = {}

    def begin_mutation(self, operation: str) -> None:
        with self.mutation_lock:
            if self.stopping.is_set():
                raise OrchestratorError(
                    "DAEMON_STOPPING",
                    "orchestrator handoff has started; retry against the replacement daemon",
                    category="state",
                    retryable=True,
                )
            self._active_mutations[operation] = (
                self._active_mutations.get(operation, 0) + 1
            )

    def end_mutation(self, operation: str) -> None:
        with self.mutation_lock:
            remaining = self._active_mutations.get(operation, 0) - 1
            if remaining > 0:
                self._active_mutations[operation] = remaining
            else:
                self._active_mutations.pop(operation, None)

    def mutation_snapshot(self) -> dict[str, Any]:
        with self.mutation_lock:
            operations = dict(sorted(self._active_mutations.items()))
        return {"count": sum(operations.values()), "operations": operations}

    def request_daemon_stop(self) -> dict[str, Any]:
        with self.mutation_lock:
            operations = dict(sorted(self._active_mutations.items()))
            count = sum(operations.values())
            if count:
                return {
                    "stopping": False,
                    "busy": True,
                    "active_mutations": count,
                    "operations": operations,
                }
            self.stopping.set()
        return {
            "stopping": True,
            "busy": False,
            "active_mutations": 0,
            "operations": {},
        }

    def observer_snapshot(self) -> dict[str, Any]:
        with self.observer_lock:
            return dict(self.observer_health)

    def terminal_audit_snapshot(self) -> dict[str, Any]:
        with self.observer_lock:
            return dict(self.terminal_audit_health)

    def _observer_success(self) -> None:
        timestamp = time.time()
        with self.observer_lock:
            self.observer_health.update(
                {
                    "status": "healthy",
                    "last_reconcile_at": timestamp,
                    "last_success_at": timestamp,
                    "last_error": None,
                    "consecutive_failures": 0,
                }
            )

    def _observer_failure(
        self, exc: BaseException, workflow: Mapping[str, Any] | None = None
    ) -> None:
        timestamp = time.time()
        failure = {
            "code": "ORCHESTRATOR_RECONCILE_CRASH",
            "message": str(exc)[:2000],
            "workflow_id": workflow.get("workflow_id") if workflow else None,
            "at": timestamp,
        }
        with self.observer_lock:
            self.observer_health.update(
                {
                    "status": "degraded",
                    "last_reconcile_at": timestamp,
                    "last_error": failure,
                    "consecutive_failures": int(
                        self.observer_health.get("consecutive_failures") or 0
                    )
                    + 1,
                }
            )
        LOGGER.error(
            "orchestrator reconcile failure",
            exc_info=(type(exc), exc, exc.__traceback__),
        )
        if workflow is None:
            return
        workflow_id = str(workflow.get("workflow_id") or "")
        signature = f"{type(exc).__name__}:{exc}"
        prior = self._last_observer_event.get(workflow_id)
        if prior and prior[0] == signature and timestamp - prior[1] < 60:
            return
        self._last_observer_event[workflow_id] = (signature, timestamp)
        try:
            self.store.append_event(
                workflow_id,
                event_type="observer.crashed",
                source="pi-lane-orchestrator",
                phase=str(workflow.get("phase") or "unknown"),
                severity="error",
                payload={"code": failure["code"], "message": failure["message"]},
                failure=structured_failure(
                    code="ORCHESTRATOR_RECONCILE_CRASH",
                    domain="orchestration",
                    message=str(exc),
                    retryable=True,
                    repairable_by_rtl=False,
                    user_action_required=False,
                ),
            )
        except Exception:
            LOGGER.exception("failed to persist reconciler error event")

    def _terminal_audit_success(self) -> None:
        timestamp = time.time()
        with self.observer_lock:
            self.terminal_audit_health.update(
                {
                    "status": "healthy",
                    "last_reconcile_at": timestamp,
                    "last_success_at": timestamp,
                    "last_error": None,
                    "consecutive_failures": 0,
                }
            )

    def _terminal_audit_failure(
        self, exc: BaseException, workflow: Mapping[str, Any] | None = None
    ) -> None:
        timestamp = time.time()
        code = exc.code if isinstance(exc, OrchestratorError) else type(exc).__name__
        failure = {
            "code": str(code)[:256],
            "message": str(exc)[:2000],
            "workflow_id": workflow.get("workflow_id") if workflow else None,
            "at": timestamp,
        }
        with self.observer_lock:
            self.terminal_audit_health.update(
                {
                    "status": "degraded",
                    "last_reconcile_at": timestamp,
                    "last_error": failure,
                    "consecutive_failures": int(
                        self.terminal_audit_health.get("consecutive_failures") or 0
                    )
                    + 1,
                }
            )
        LOGGER.warning(
            "terminal integrity audit degraded for %s: %s",
            workflow.get("workflow_id") if workflow else None,
            exc,
        )

    def _observer_backend_degraded(
        self,
        error: OrchestratorError,
        workflow: Mapping[str, Any],
        observer: Mapping[str, Any],
    ) -> None:
        timestamp = time.time()
        failure = {
            "code": error.code,
            "message": str(error)[:2000],
            "workflow_id": workflow.get("workflow_id"),
            "at": timestamp,
            "retryable": True,
            "next_reconcile_epoch": observer.get("next_reconcile_epoch"),
        }
        with self.observer_lock:
            self.observer_health.update(
                {
                    "status": "degraded",
                    "last_reconcile_at": timestamp,
                    "last_error": failure,
                    "consecutive_failures": int(
                        observer.get("consecutive_failures") or 1
                    ),
                }
            )
        LOGGER.warning(
            "orchestrator backend observation degraded for %s: %s",
            workflow.get("workflow_id"),
            error,
        )

    def _run(
        self,
        command: list[str],
        *,
        timeout: float,
        allow_nonzero_status: bool = False,
    ) -> dict[str, Any]:
        try:
            completed = self.command_runner(
                command,
                capture_output=True,
                text=True,
                timeout=timeout,
                check=False,
            )
        except subprocess.TimeoutExpired as exc:
            raise OrchestratorError(
                "BACKEND_TIMEOUT",
                f"{' '.join(command[:2])} exceeded {timeout:g} seconds",
                retryable=True,
            ) from exc
        except OSError as exc:
            raise OrchestratorError(
                "BACKEND_UNAVAILABLE", str(exc), category="infrastructure", retryable=True
            ) from exc
        return parse_json_stdout(
            completed,
            command=" ".join(command[:2]),
            allow_nonzero_status=allow_nonzero_status,
        )

    @staticmethod
    def _verify_workflow_spec(workflow: Mapping[str, Any]) -> None:
        path = canonical_spec_path(Path(str(workflow["spec_path"])))
        if file_sha256(path) != workflow.get("spec_sha256"):
            raise OrchestratorError(
                "SPEC_SNAPSHOT_CHANGED",
                "workflow specification snapshot no longer matches its recorded digest",
                category="state",
            )

    def _start_rtl_closure_idempotently(
        self, command: list[str], *, workflow: Mapping[str, Any]
    ) -> tuple[dict[str, Any], bool]:
        """Recover an uncertain start response without creating a second run."""

        first_error: OrchestratorError | None = None
        for attempt, timeout in enumerate((90.0, 30.0), start=1):
            try:
                report = self._run(command, timeout=timeout)
                backend_run_id = report.get("run_id")
                if (
                    not isinstance(backend_run_id, str)
                    or SAFE_IDENTIFIER.fullmatch(backend_run_id) is None
                ):
                    raise OrchestratorError(
                        "INVALID_BACKEND_JSON",
                        "rtl-closure start response has no safe run_id",
                    )
                _validate_rtl_closure_report(
                    report, expected_run_id=backend_run_id, workflow=workflow
                )
                return report, attempt > 1
            except OrchestratorError as exc:
                if attempt == 1 and exc.code in {
                    "BACKEND_TIMEOUT",
                    "INVALID_BACKEND_JSON",
                }:
                    # `rtl-closure start` is bound to the child idempotency
                    # key in this command.  Repeating it either returns the run
                    # created by the uncertain first call or creates the sole
                    # run if the first call had no side effect.
                    first_error = exc
                    continue
                if first_error is not None:
                    raise OrchestratorError(
                        exc.code,
                        f"{exc}; idempotent recovery after "
                        f"{first_error.code} also failed",
                        category=exc.category,
                        retryable=True,
                    ) from exc
                raise

        raise AssertionError("unreachable RTL-Closure start recovery state")

    def start_workflow(self, params: Mapping[str, Any]) -> dict[str, Any]:
        with self.start_lock:
            return self._start_workflow(params)

    def _start_workflow(
        self,
        params: Mapping[str, Any],
        *,
        supersedes_workflow_id: str | None = None,
        expected_spec_sha256: str | None = None,
    ) -> dict[str, Any]:
        raw_spec = params.get("spec")
        if not isinstance(raw_spec, str) or not raw_spec:
            raise OrchestratorError("INVALID_REQUEST", "spec is required")
        spec_path = canonical_spec_path(Path(raw_spec))
        key = params.get("idempotency_key")
        if key is not None and not isinstance(key, str):
            raise OrchestratorError("INVALID_REQUEST", "idempotency_key must be a string")
        workflow, created = self.store.create_workflow(
            spec_path,
            idempotency_key=key,
            supersedes_workflow_id=supersedes_workflow_id,
            expected_spec_sha256=expected_spec_sha256,
        )
        if not created and (
            workflow.get("rtl_closure_run_id")
            or workflow.get("status") in TERMINAL_STATUSES
            or any(
                operation.get("kind") == "start"
                and operation.get("status") in {"completed", "failed", "cancelled"}
                for operation in workflow.get("operations", [])
            )
        ):
            return workflow
        workflow_id = str(workflow["workflow_id"])
        owner = f"start:{os.getpid()}:{uuid.uuid4().hex}"
        token = self.store.acquire_lease(workflow_id, owner=owner)
        if token is None:
            raise OrchestratorError(
                "OPERATION_IN_PROGRESS",
                "another workflow operation is in progress",
                retryable=True,
            )
        try:
            workflow = self.store.get_workflow(workflow_id)
            if (
                workflow.get("rtl_closure_run_id")
                or workflow.get("status") in TERMINAL_STATUSES
                or any(
                    operation.get("kind") == "start"
                    and operation.get("status") in {"completed", "failed", "cancelled"}
                    for operation in workflow.get("operations", [])
                )
            ):
                return workflow
            operation, _ = self.store.create_operation(
                workflow_id,
                kind="start",
                request={
                    "spec_path": workflow["spec_path"],
                    "spec_sha256": workflow["spec_sha256"],
                },
                idempotency_key=_child_start_idempotency_key(workflow),
                reuse_active=True,
            )
            return self._launch_workflow(workflow, operation=operation, created=created)
        finally:
            self.store.release_lease(workflow_id, owner=owner, token=token)

    def _launch_workflow(
        self,
        workflow: Mapping[str, Any],
        *,
        operation: Mapping[str, Any],
        created: bool,
    ) -> dict[str, Any]:
        workflow_id = str(workflow["workflow_id"])
        operation = self.store.mark_operation_attempt(str(operation["operation_id"]))
        try:
            workflow = self.store.update_workflow(
                workflow_id,
                status="running",
                phase="preflight",
                snapshot={"status": "running", "phase": "preflight"},
                expected_revision=int(workflow["revision"]),
                event_type="preflight.started" if created else "preflight.retried",
            )
            self._verify_workflow_spec(workflow)
            self._run(
                ["rtl-closure", "validate", "--spec", workflow["spec_path"]],
                timeout=60,
            )
        except OrchestratorError as exc:
            failure = structured_failure(
                code=exc.code,
                domain=(
                    "user_intent"
                    if exc.code
                    in {"BACKEND_COMMAND_FAILED", "INVALID_SPEC", "SPEC_SNAPSHOT_CHANGED"}
                    else "infrastructure"
                ),
                message=str(exc),
                retryable=exc.retryable,
                repairable_by_rtl=False,
                user_action_required=True,
                evidence=[str(workflow["spec_path"])],
            )
            if exc.retryable:
                self.store.defer_operation(
                    str(operation["operation_id"]), failure=failure, retryable=True
                )
            return self.store.update_workflow(
                workflow_id,
                status=(
                    "waiting_user"
                    if failure["domain"] == "user_intent"
                    else "blocked_infrastructure"
                ),
                phase="preflight_failed",
                snapshot={"status": "failed", "phase": "preflight"},
                failure=failure,
                event_type="preflight.failed",
                operation_id=(
                    None if exc.retryable else str(operation["operation_id"])
                ),
                operation_outcome="failed",
            )

        child_idempotency_key = str(operation["idempotency_key"])
        start_command = [
            "rtl-closure",
            "start",
            "--spec",
            str(workflow["spec_path"]),
            "--workflow-id",
            workflow_id,
            "--idempotency-key",
            child_idempotency_key,
            "--json",
        ]
        try:
            self._verify_workflow_spec(workflow)
            report, start_recovered = self._start_rtl_closure_idempotently(
                start_command, workflow=workflow
            )
            backend_run_id = str(report["run_id"])
        except OrchestratorError as exc:
            failure = structured_failure(
                code=exc.code,
                domain=(
                    "user_intent"
                    if exc.code == "SPEC_SNAPSHOT_CHANGED"
                    else "infrastructure"
                ),
                message=str(exc),
                retryable=exc.retryable,
                repairable_by_rtl=False,
                user_action_required=True,
                evidence=[str(workflow["spec_path"])],
            )
            if exc.retryable:
                self.store.defer_operation(
                    str(operation["operation_id"]), failure=failure, retryable=True
                )
            return self.store.update_workflow(
                workflow_id,
                status=(
                    "waiting_user"
                    if failure["domain"] == "user_intent"
                    else "blocked_infrastructure"
                ),
                phase="rtl_closure_start_failed",
                snapshot={"status": "failed", "phase": "rtl_closure_start"},
                failure=failure,
                event_type="rtl_closure.start_failed",
                operation_id=(
                    None if exc.retryable else str(operation["operation_id"])
                ),
                operation_outcome="failed",
            )
        failure = classify_backend_failure(report)
        status = _workflow_status(report, failure)
        close_attempts_as = {
            "completed": "completed",
            "failed": "failed",
            "incomplete": "failed",
            "stopped": "cancelled",
        }.get(str(report.get("status") or ""))
        updated, attempt = self.store.bind_attempt_and_update_workflow(
            workflow_id,
            kind="rtl_closure",
            backend_run_id=backend_run_id,
            launch_request_sha256=sha256_text(
                str(workflow["spec_sha256"]) + child_idempotency_key
            ),
            reason="start_response_recovered" if start_recovered else "initial_start",
            status=status,
            phase=str(report.get("phase") or "rtl_closure_running"),
            snapshot=_backend_snapshot(report),
            rtl_closure_run_id=backend_run_id,
            failure=failure,
            expected_revision=int(workflow["revision"]),
            event_type=(
                "rtl_closure.start_recovered"
                if start_recovered
                else "rtl_closure.started"
            ),
            close_running_attempts_as=close_attempts_as,
            operation_id=str(operation["operation_id"]),
        )
        updated["active_attempt_id"] = attempt["attempt_id"]
        with self.condition:
            self.condition.notify_all()
        return updated

    def reconcile_one(self, workflow: Mapping[str, Any]) -> dict[str, Any]:
        workflow_id = str(workflow["workflow_id"])
        current = self.store.get_workflow(workflow_id)
        if (
            not current.get("rtl_closure_run_id")
            and current.get("status") in EXECUTING_STATUSES
            and not current.get("operations")
        ):
            self.store.create_operation(
                workflow_id,
                kind="start",
                request={
                    "spec_path": current["spec_path"],
                    "spec_sha256": current["spec_sha256"],
                },
                idempotency_key=_child_start_idempotency_key(current),
                reuse_active=True,
            )
        pending = self.store.pending_operations(workflow_id=workflow_id, limit=1)
        if pending:
            owner = f"recovery:{os.getpid()}:{uuid.uuid4().hex}"
            token = self.store.acquire_lease(workflow_id, owner=owner)
            if token is None:
                return dict(workflow)
            try:
                current = self.store.get_workflow(workflow_id)
                operation = self.store.pending_operations(
                    workflow_id=workflow_id, limit=1
                )
                if not operation:
                    return current
                kind = str(operation[0]["kind"])
                if kind == "start":
                    if current.get("rtl_closure_run_id"):
                        raise OrchestratorError(
                            "OPERATION_STATE_MISMATCH",
                            "pending start operation conflicts with an existing backend binding",
                            category="state",
                        )
                    return self._launch_workflow(
                        current, operation=operation[0], created=False
                    )
                if kind == "stop":
                    return self._execute_stop_operation(current, operation[0])
                if kind == "retry":
                    return self._execute_retry_operation(current, operation[0])
                raise OrchestratorError(
                    "INVALID_OPERATION", f"unsupported pending operation: {kind}", category="state"
                )
            finally:
                self.store.release_lease(
                    workflow_id, owner=owner, token=token
                )
        run_id = workflow.get("rtl_closure_run_id")
        terminal_integrity_audit = workflow.get("status") == "completed"
        recovering_blocked_backend = bool(
            workflow.get("status") == "blocked_infrastructure" and run_id
        )
        if (
            not run_id
            or (
                workflow.get("status") not in EXECUTING_STATUSES
                and not terminal_integrity_audit
                and not recovering_blocked_backend
            )
        ):
            return dict(workflow)
        if (
            not terminal_integrity_audit
            and not recovering_blocked_backend
            and not _reconcile_is_due(workflow, now_epoch=time.time())
        ):
            return dict(workflow)
        owner = f"reconcile:{os.getpid()}:{uuid.uuid4().hex}"
        token = self.store.acquire_lease(workflow_id, owner=owner)
        if token is None:
            return dict(workflow)
        try:
            # Reconciliation is the only operation allowed to advance durable
            # child-derived state. report/status remain side-effect free.
            integrity_audit_id = (
                f"pi-audit-{uuid.uuid4().hex}"
                if terminal_integrity_audit
                else None
            )
            reconcile_command = [
                "rtl-closure",
                "reconcile",
                "--run-id",
                str(run_id),
                "--json",
            ]
            if integrity_audit_id is not None:
                reconcile_command.extend(
                    ["--integrity-audit-id", integrity_audit_id]
                )
            reconciled = self._run(
                reconcile_command,
                timeout=30,
                allow_nonzero_status=True,
            )
            if (
                integrity_audit_id is not None
                and reconciled.get("status") == "completed"
                and reconciled.get("last_integrity_audit_id")
                != integrity_audit_id
            ):
                raise OrchestratorError(
                    "BACKEND_AUDIT_STALE",
                    "RTL-Closure reconcile did not persist the requested integrity audit",
                    category="state",
                    retryable=True,
                )
            report = self._run(
                ["rtl-closure", "report", "--run-id", str(run_id), "--json"],
                timeout=30,
                allow_nonzero_status=True,
            )
            _validate_rtl_closure_report(
                report,
                expected_run_id=str(run_id),
                workflow=workflow,
                expected_integrity_audit_id=(
                    integrity_audit_id
                    if report.get("status") == "completed"
                    or report.get("failure_gate")
                    == "lane_delivery_attestation"
                    else None
                ),
            )
            snapshot, observer_recovered = _backend_snapshot_with_recovery(
                workflow, report
            )
            failure = classify_backend_failure(report)
            status = _workflow_status(report, failure)
            if terminal_integrity_audit and report.get("status") != "completed":
                status = "failed"
            phase = str(report.get("phase") or workflow.get("phase") or "unknown")
            lane_run_id = report.get("lane_run_id")
            if terminal_integrity_audit and lane_run_id != workflow.get(
                "active_lane_run_id"
            ):
                raise OrchestratorError(
                    "BACKEND_IDENTITY_MISMATCH",
                    "terminal integrity audit returned a different Lane run identity",
                    category="state",
                )
            if terminal_integrity_audit:
                prior_snapshot = workflow.get("snapshot") or {}
                for field in (
                    "run_id",
                    "state_file",
                    "pipeline_log",
                    "design_config",
                    "publication_manifest",
                    "publication_bundle_sha256",
                    "lane_input_bundle_sha256",
                ):
                    prior_value = prior_snapshot.get(field)
                    if prior_value is not None and snapshot.get(field) != prior_value:
                        raise OrchestratorError(
                            "BACKEND_IDENTITY_MISMATCH",
                            f"terminal integrity audit changed immutable {field}",
                            category="state",
                        )
            snapshot_sha256 = sha256_text(canonical_json(snapshot))
            if terminal_integrity_audit and status == "completed":
                current = self.store.get_workflow(workflow_id)
                self.store.record_terminal_integrity_audit(
                    workflow_id,
                    checked_revision=int(current["revision"]),
                    backend_status="completed",
                    backend_snapshot_sha256=snapshot_sha256,
                    interval_seconds=TERMINAL_INTEGRITY_AUDIT_INTERVAL_SECONDS,
                )
                return current
            changed = any(
                (
                    workflow.get("status") != status,
                    workflow.get("phase") != phase,
                    workflow.get("active_lane_run_id") != lane_run_id and lane_run_id is not None,
                    _meaningful_snapshot(workflow.get("snapshot") or {})
                    != _meaningful_snapshot(snapshot),
                    workflow.get("failure") != failure,
                )
            )
            if not changed:
                current = self.store.get_workflow(workflow_id)
                return current
            event_type = (
                "workflow.integrity_degraded"
                if terminal_integrity_audit
                else "workflow.terminal"
                if status in TERMINAL_STATUSES
                else "observer.recovered"
                if observer_recovered
                else "workflow.progress"
            )
            close_attempts_as = {
                "completed": "completed",
                "failed": "failed",
                "incomplete": "failed",
                "stopped": "cancelled",
            }.get(str(report.get("status") or ""))
            close_attempt_kind = "lane" if status == "waiting_user" else None
            if lane_run_id and lane_run_id != workflow.get("active_lane_run_id"):
                updated, _ = self.store.bind_attempt_and_update_workflow(
                    workflow_id,
                    kind="lane",
                    backend_run_id=str(lane_run_id),
                    launch_request_sha256=sha256_text(
                        str(report.get("design_config") or "")
                        + str(lane_run_id)
                    ),
                    reason=(
                        "rtl_closure_handoff"
                        if not workflow.get("active_lane_run_id")
                        else "lane_retry"
                    ),
                    status=status,
                    phase=phase,
                    snapshot=snapshot,
                    active_lane_run_id=str(lane_run_id),
                    failure=failure,
                    expected_revision=int(workflow["revision"]),
                    event_type=event_type,
                    close_running_attempts_as=close_attempts_as,
                    close_attempt_kind=close_attempt_kind,
                )
            else:
                updated = self.store.update_workflow(
                    workflow_id,
                    status=status,
                    phase=phase,
                    snapshot=snapshot,
                    active_lane_run_id=(
                        str(lane_run_id) if lane_run_id else None
                    ),
                    failure=failure,
                    expected_revision=int(workflow["revision"]),
                    event_type=event_type,
                    close_running_attempts_as=close_attempts_as,
                    close_attempt_kind=close_attempt_kind,
                    allow_terminal_degradation=terminal_integrity_audit,
                )
            if terminal_integrity_audit:
                self.store.record_terminal_integrity_audit(
                    workflow_id,
                    checked_revision=int(updated["revision"]),
                    backend_status=str(report.get("status") or "unknown"),
                    backend_snapshot_sha256=snapshot_sha256,
                    interval_seconds=TERMINAL_INTEGRITY_AUDIT_INTERVAL_SECONDS,
                )
            with self.condition:
                self.condition.notify_all()
            return updated
        except OrchestratorError as exc:
            latest = self.store.get_workflow(workflow_id)
            if latest["status"] in TERMINAL_STATUSES:
                if terminal_integrity_audit and latest["status"] == "completed":
                    if exc.retryable:
                        self.store.record_terminal_integrity_audit(
                            workflow_id,
                            checked_revision=int(latest["revision"]),
                            backend_status=f"error:{exc.code}"[:128],
                            backend_snapshot_sha256=sha256_text(
                                canonical_json(latest.get("snapshot") or {})
                            ),
                            interval_seconds=RECONCILE_BACKOFF_MAX_SECONDS,
                        )
                        raise
                    failure = structured_failure(
                        code=exc.code,
                        domain="orchestration",
                        message=str(exc),
                        retryable=False,
                        repairable_by_rtl=False,
                        user_action_required=True,
                        evidence=[],
                    )
                    snapshot = dict(latest.get("snapshot") or {})
                    snapshot[ORCHESTRATOR_OBSERVER_KEY] = {
                        "status": "failed",
                        "last_error": {
                            "code": exc.code,
                            "message": str(exc)[:2000],
                            "retryable": False,
                        },
                    }
                    updated = self.store.update_workflow(
                        workflow_id,
                        status="failed",
                        phase="terminal_integrity_audit_failed",
                        snapshot=snapshot,
                        failure=failure,
                        expected_revision=int(latest["revision"]),
                        event_type="workflow.integrity_degraded",
                        allow_terminal_degradation=True,
                    )
                    self.store.record_terminal_integrity_audit(
                        workflow_id,
                        checked_revision=int(updated["revision"]),
                        backend_status=f"error:{exc.code}"[:128],
                        backend_snapshot_sha256=sha256_text(
                            canonical_json(snapshot)
                        ),
                        interval_seconds=TERMINAL_INTEGRITY_AUDIT_INTERVAL_SECONDS,
                    )
                    with self.condition:
                        self.condition.notify_all()
                    return updated
                return latest
            if not exc.retryable:
                failure = structured_failure(
                    code=exc.code,
                    domain="orchestration",
                    message=str(exc),
                    retryable=False,
                    repairable_by_rtl=False,
                    user_action_required=True,
                    evidence=[],
                )
                snapshot = dict(latest.get("snapshot") or {})
                snapshot[ORCHESTRATOR_OBSERVER_KEY] = {
                    "status": "failed",
                    "consecutive_failures": 0,
                    "last_error": {
                        "code": exc.code,
                        "message": str(exc)[:2000],
                        "retryable": False,
                    },
                }
                updated = self.store.update_workflow(
                    workflow_id,
                    status="failed",
                    phase="backend_protocol_invalid",
                    snapshot=snapshot,
                    failure=failure,
                    expected_revision=latest["revision"],
                    event_type="workflow.terminal",
                    close_running_attempts_as="failed",
                )
                with self.condition:
                    self.condition.notify_all()
                return updated

            failure = structured_failure(
                code=exc.code,
                domain="infrastructure",
                message=str(exc),
                retryable=True,
                repairable_by_rtl=False,
                user_action_required=False,
                evidence=[],
            )
            snapshot, observer = _degraded_backend_snapshot(latest, exc)
            updated = self.store.update_workflow(
                workflow_id,
                status=str(latest["status"]),
                phase=str(latest["phase"]),
                snapshot=snapshot,
                failure=failure,
                expected_revision=latest["revision"],
                event_type="observer.degraded",
            )
            self._observer_backend_degraded(exc, latest, observer)
            with self.condition:
                self.condition.notify_all()
            return updated
        finally:
            self.store.release_lease(workflow_id, owner=owner, token=token)

    def reconcile_loop(self) -> None:
        while not self.stopping.wait(RECONCILE_INTERVAL_SECONDS):
            try:
                workflows = self.store.active_for_reconcile()
            except Exception as exc:
                self._observer_failure(exc)
                continue
            pass_failed = False
            for workflow in workflows:
                if self.stopping.is_set():
                    return
                operation = "observer.reconcile"
                began = False
                try:
                    self.begin_mutation(operation)
                    began = True
                    updated = self.reconcile_one(workflow)
                    if _observer_snapshot(updated).get("status") == "degraded":
                        pass_failed = True
                except Exception as exc:
                    if self.stopping.is_set() and isinstance(
                        exc, OrchestratorError
                    ) and exc.code == "DAEMON_STOPPING":
                        return
                    pass_failed = True
                    self._observer_failure(exc, workflow)
                finally:
                    if began:
                        self.end_mutation(operation)
            if not pass_failed:
                self._observer_success()

    def terminal_audit_loop(self) -> None:
        while not self.stopping.wait(RECONCILE_INTERVAL_SECONDS):
            self.terminal_audit_once()

    def terminal_audit_once(self) -> None:
        try:
            workflows = self.store.terminal_audits_due(limit=1)
        except Exception as exc:
            self._terminal_audit_failure(exc)
            return
        if not workflows:
            if self.terminal_audit_snapshot()["status"] == "starting":
                self._terminal_audit_success()
            return
        workflow = workflows[0]
        operation = "observer.terminal_integrity_audit"
        began = False
        try:
            self.begin_mutation(operation)
            began = True
            self.reconcile_one(workflow)
        except Exception as exc:
            if self.stopping.is_set() and isinstance(
                exc, OrchestratorError
            ) and exc.code == "DAEMON_STOPPING":
                return
            self._terminal_audit_failure(exc, workflow)
        else:
            self._terminal_audit_success()
        finally:
            if began:
                self.end_mutation(operation)

    def event_report(self, params: Mapping[str, Any]) -> dict[str, Any]:
        acknowledge = _boolean(params.get("acknowledge", False), "acknowledge")
        workflow_id = _workflow_id(params, self.store, required=acknowledge)
        after = _integer(params.get("after_sequence", 0), "after_sequence", low=0)
        limit = _integer(params.get("limit", 256), "limit", low=1, high=1000)
        wait_seconds = _number(params.get("wait_seconds", 0), "wait_seconds", low=0, high=30)
        client_id = params.get("client_id")
        if client_id is not None and (not isinstance(client_id, str) or not client_id):
            raise OrchestratorError("INVALID_REQUEST", "client_id must be a non-empty string")
        if acknowledge and client_id is None:
            raise OrchestratorError(
                "INVALID_REQUEST", "client_id is required when acknowledge is true"
            )
        deadline = time.monotonic() + wait_seconds
        while True:
            report = self.store.events(
                workflow_id,
                after_sequence=after,
                limit=limit,
                client_id=client_id,
                acknowledge=acknowledge,
                max_bytes=_integer(
                    params.get("max_bytes", 512 * 1024),
                    "max_bytes",
                    low=64 * 1024,
                    high=900 * 1024,
                ),
            )
            if report["events"] or report["snapshot"]["status"] in TERMINAL_STATUSES:
                return report
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return report
            with self.condition:
                self.condition.wait(min(remaining, 1.0))

    def stop_workflow(self, params: Mapping[str, Any]) -> dict[str, Any]:
        workflow_id = _workflow_id(params, self.store, required=True)
        workflow = self.store.get_workflow(workflow_id)
        if workflow["status"] in TERMINAL_STATUSES:
            return workflow
        owner = f"stop:{os.getpid()}:{uuid.uuid4().hex}"
        token = self.store.acquire_lease(workflow_id, owner=owner)
        if token is None:
            raise OrchestratorError(
                "OPERATION_IN_PROGRESS",
                "another workflow operation is in progress",
                retryable=True,
            )
        try:
            workflow = self.store.get_workflow(workflow_id)
            if workflow["status"] in TERMINAL_STATUSES:
                return workflow
            operation, _ = self.store.create_operation(
                workflow_id,
                kind="stop",
                request={
                    "rtl_closure_run_id": workflow.get("rtl_closure_run_id"),
                    "active_lane_run_id": workflow.get("active_lane_run_id"),
                    "spec_sha256": workflow["spec_sha256"],
                },
                reuse_active=True,
                cancel_active_kinds=("retry",),
            )
            current = self.store.get_workflow(workflow_id)
            if any(
                item.get("kind") == "start"
                and item.get("status") in {"pending", "executing"}
                for item in current.get("operations", [])
            ):
                return current
            return self._execute_stop_operation(current, operation)
        finally:
            self.store.release_lease(workflow_id, owner=owner, token=token)

    def _execute_stop_operation(
        self, workflow: Mapping[str, Any], operation: Mapping[str, Any]
    ) -> dict[str, Any]:
        workflow_id = str(workflow["workflow_id"])
        operation = self.store.mark_operation_attempt(str(operation["operation_id"]))
        run_id = workflow.get("rtl_closure_run_id")
        if not run_id:
            updated = self.store.update_workflow(
                workflow_id,
                status="cancelled",
                phase="cancelled",
                snapshot=workflow.get("snapshot") or {},
                expected_revision=int(workflow["revision"]),
                event_type="stop.confirmed",
                operation_id=str(operation["operation_id"]),
            )
            with self.condition:
                self.condition.notify_all()
            return updated
        try:
            report = self._run(
                [
                    "rtl-closure",
                    "stop",
                    "--run-id",
                    str(run_id),
                    "--json",
                ],
                timeout=30,
            )
            _validate_rtl_closure_report(
                report, expected_run_id=str(run_id), workflow=workflow
            )
            reported_lane_run_id = (
                str(report["lane_run_id"]) if report.get("lane_run_id") else None
            )
            current_lane_run_id = workflow.get("active_lane_run_id")
            if (
                reported_lane_run_id is not None
                and current_lane_run_id is not None
                and reported_lane_run_id != current_lane_run_id
            ):
                raise OrchestratorError(
                    "BACKEND_IDENTITY_MISMATCH",
                    "stop response returned a different Lane run identity",
                    category="state",
                )
        except OrchestratorError as exc:
            failure = structured_failure(
                code=exc.code,
                domain="infrastructure" if exc.retryable else "orchestration",
                message=str(exc),
                retryable=exc.retryable,
                repairable_by_rtl=False,
                user_action_required=not exc.retryable,
            )
            self.store.defer_operation(
                str(operation["operation_id"]), failure=failure, retryable=exc.retryable
            )
            raise
        snapshot = _backend_snapshot(report)
        failure = classify_backend_failure(report)
        backend_status = str(report["status"])
        status = _workflow_status(report, failure)
        phase = str(report.get("phase") or "stopping")
        event_type = "stop.pending"
        if backend_status == "stopped":
            status = "cancelled"
            phase = "cancelled"
            failure = None
            event_type = "stop.confirmed"
        elif backend_status in {"completed", "failed", "incomplete"}:
            event_type = "stop.raced_with_terminal"
        close_attempts_as = {
            "completed": "completed",
            "failed": "failed",
            "incomplete": "failed",
            "stopped": "cancelled",
        }.get(backend_status)
        close_attempt_kind = "lane" if status == "waiting_user" else None
        terminal_operation_id = (
            None
            if backend_status == "running"
            else str(operation["operation_id"])
        )
        if reported_lane_run_id is not None and current_lane_run_id is None:
            updated, _ = self.store.bind_attempt_and_update_workflow(
                workflow_id,
                kind="lane",
                backend_run_id=reported_lane_run_id,
                launch_request_sha256=sha256_text(
                    canonical_json(
                        {
                            "operation": "stop_discovered_lane",
                            "spec_sha256": workflow["spec_sha256"],
                            "rtl_closure_run_id": run_id,
                            "lane_run_id": reported_lane_run_id,
                        }
                    )
                ),
                reason="stop_discovered_child",
                status=status,
                phase=phase,
                snapshot=snapshot,
                active_lane_run_id=reported_lane_run_id,
                failure=failure,
                expected_revision=int(workflow["revision"]),
                event_type=event_type,
                close_running_attempts_as=close_attempts_as,
                close_attempt_kind=close_attempt_kind,
                operation_id=terminal_operation_id,
            )
        else:
            updated = self.store.update_workflow(
                workflow_id,
                status=status,
                phase=phase,
                snapshot=snapshot,
                active_lane_run_id=reported_lane_run_id,
                failure=failure,
                expected_revision=int(workflow["revision"]),
                event_type=event_type,
                close_running_attempts_as=close_attempts_as,
                close_attempt_kind=close_attempt_kind,
                operation_id=terminal_operation_id,
            )
        with self.condition:
            self.condition.notify_all()
        return updated

    def retry_workflow(self, params: Mapping[str, Any]) -> dict[str, Any]:
        workflow_id = _workflow_id(params, self.store, required=True)
        scope = params.get("scope")
        if scope not in {"lane", "formal", "model"}:
            raise OrchestratorError("INVALID_RETRY_SCOPE", "scope must be lane, formal, or model")
        workflow = self.store.get_workflow(workflow_id)
        if scope != "lane":
            raise OrchestratorError(
                "RETRY_REQUIRES_BACKEND_CHECKPOINT",
                f"{scope} retry is available only when the backend exposes a resumable checkpoint",
                category="verification",
            )
        if workflow["status"] in TERMINAL_STATUSES:
            raise OrchestratorError(
                "TERMINAL_STATE", "terminal workflow is immutable; use run.rerun"
            )
        failure = workflow.get("failure")
        if (
            workflow["status"] != "waiting_user"
            or not isinstance(failure, Mapping)
            or failure.get("retryable") is not True
            or failure.get("domain") != "physical_tool"
        ):
            raise OrchestratorError(
                "RETRY_NOT_ALLOWED",
                "Lane retry requires a waiting_user workflow with a retryable physical failure",
            )
        owner = f"retry:{os.getpid()}:{uuid.uuid4().hex}"
        token = self.store.acquire_lease(workflow_id, owner=owner)
        if token is None:
            raise OrchestratorError(
                "OPERATION_IN_PROGRESS",
                "another workflow operation is in progress",
                retryable=True,
            )
        try:
            workflow = self.store.get_workflow(workflow_id)
            failure = workflow.get("failure")
            if (
                workflow["status"] != "waiting_user"
                or not isinstance(failure, Mapping)
                or failure.get("retryable") is not True
                or failure.get("domain") != "physical_tool"
            ):
                raise OrchestratorError(
                    "RETRY_NOT_ALLOWED", "workflow changed before retry could start"
                )
            run_id = workflow.get("rtl_closure_run_id")
            if not run_id:
                raise OrchestratorError(
                    "RETRY_UNAVAILABLE", "workflow has no RTL-Closure run"
                )
            operation, _ = self.store.create_operation(
                workflow_id,
                kind="retry",
                request={
                    "rtl_closure_run_id": run_id,
                    "active_lane_run_id": workflow.get("active_lane_run_id"),
                    "spec_sha256": workflow["spec_sha256"],
                },
                reuse_active=True,
            )
            return self._execute_retry_operation(workflow, operation)
        finally:
            self.store.release_lease(workflow_id, owner=owner, token=token)

    def _execute_retry_operation(
        self, workflow: Mapping[str, Any], operation: Mapping[str, Any]
    ) -> dict[str, Any]:
        workflow_id = str(workflow["workflow_id"])
        run_id = workflow.get("rtl_closure_run_id")
        if not run_id:
            raise OrchestratorError("RETRY_UNAVAILABLE", "workflow has no RTL-Closure run")
        operation = self.store.mark_operation_attempt(str(operation["operation_id"]))
        retry_idempotency_key = str(operation["idempotency_key"])
        try:
            self._run(
                [
                    "rtl-closure",
                    "retry-lane",
                    "--run-id",
                    str(run_id),
                    "--idempotency-key",
                    retry_idempotency_key,
                    "--json",
                ],
                timeout=90,
            )
            report = self._run(
                ["rtl-closure", "report", "--run-id", str(run_id), "--json"],
                timeout=30,
                allow_nonzero_status=True,
            )
            _validate_rtl_closure_report(
                report, expected_run_id=str(run_id), workflow=workflow
            )
            lane_run_id = report.get("lane_run_id")
            if not lane_run_id:
                raise OrchestratorError("INVALID_BACKEND_JSON", "retry response has no Lane run id")
            if str(lane_run_id) == str(workflow.get("active_lane_run_id") or ""):
                raise OrchestratorError(
                    "RETRY_DID_NOT_START", "retry response reused the prior Lane run id"
                )
            failure = classify_backend_failure(report)
            status = _workflow_status(report, failure)
            close_attempts_as = {
                "completed": "completed",
                "failed": "failed",
                "incomplete": "failed",
                "stopped": "cancelled",
            }.get(str(report.get("status") or ""))
            updated, _ = self.store.bind_attempt_and_update_workflow(
                workflow_id,
                kind="lane",
                backend_run_id=str(lane_run_id),
                launch_request_sha256=sha256_text(
                    str(workflow["spec_sha256"]) + retry_idempotency_key
                ),
                reason="explicit_user_retry",
                status=status,
                phase=str(report.get("phase") or "lane_running"),
                snapshot=_backend_snapshot(report),
                active_lane_run_id=str(lane_run_id),
                failure=failure,
                expected_revision=int(workflow["revision"]),
                event_type="lane.retry_started",
                close_running_attempts_as=close_attempts_as,
                close_attempt_kind="lane" if status == "waiting_user" else None,
                operation_id=str(operation["operation_id"]),
            )
            return updated
        except OrchestratorError as exc:
            failure = structured_failure(
                code=exc.code,
                domain="infrastructure" if exc.retryable else "orchestration",
                message=str(exc),
                retryable=exc.retryable,
                repairable_by_rtl=False,
                user_action_required=not exc.retryable,
            )
            self.store.defer_operation(
                str(operation["operation_id"]), failure=failure, retryable=exc.retryable
            )
            raise

    def rerun_workflow(self, params: Mapping[str, Any]) -> dict[str, Any]:
        workflow_id = _workflow_id(params, self.store, required=True)
        parent = self.store.get_workflow(workflow_id)
        failure = parent.get("failure")
        resumable_physical_failure = bool(
            parent["status"] == "waiting_user"
            and isinstance(failure, Mapping)
            and failure.get("domain") == "physical_tool"
        )
        if parent["status"] not in TERMINAL_STATUSES and not resumable_physical_failure:
            raise OrchestratorError(
                "RERUN_NOT_ALLOWED",
                "only a terminal or waiting physical-failure workflow can be rerun",
            )
        self._verify_workflow_spec(parent)
        if resumable_physical_failure:
            parent = self.store.update_workflow(
                workflow_id,
                status="cancelled",
                phase="superseded_by_rerun",
                snapshot=parent.get("snapshot") or {},
                failure=None,
                expected_revision=int(parent["revision"]),
                event_type="rerun.parent_cancelled",
                close_running_attempts_as="cancelled",
            )
        key = params.get("idempotency_key")
        if key is None:
            key = "rerun:" + sha256_text(
                canonical_json(
                    {
                        "workflow_id": workflow_id,
                        "revision": parent["revision"],
                        "spec_sha256": parent["spec_sha256"],
                    }
                )
            )
        elif not isinstance(key, str) or not key:
            raise OrchestratorError(
                "INVALID_REQUEST", "idempotency_key must be a non-empty string"
            )
        with self.start_lock:
            return self._start_workflow(
                {"spec": parent["spec_path"], "idempotency_key": key},
                supersedes_workflow_id=workflow_id,
                expected_spec_sha256=str(parent["spec_sha256"]),
            )

    def dispatch(self, method: str, params: Mapping[str, Any]) -> object:
        if method == "daemon.ping":
            return {
                "ready": True,
                "pid": os.getpid(),
                "protocol_version": PROTOCOL_VERSION,
                "schema_version": SCHEMA_VERSION,
                "database_schema_version": DATABASE_SCHEMA_VERSION,
                "build_id": ORCHESTRATOR_BUILD_ID,
                "runtime_config_fingerprint": self.runtime_config_fingerprint,
                "handoff_protocol_version": DAEMON_HANDOFF_PROTOCOL_VERSION,
                "db_path": str(self.store.db_path),
                "socket_path": str(self.socket_path) if self.socket_path else None,
                "lock_path": str(self.lock_path) if self.lock_path else None,
                "observer": self.observer_snapshot(),
                "terminal_integrity_auditor": self.terminal_audit_snapshot(),
                "mutations": self.mutation_snapshot(),
            }
        if method == "run.start":
            return self.start_workflow(params)
        if method == "run.get":
            return self.store.compact_status(_workflow_id(params, self.store))
        if method == "run.events":
            return self.event_report(params)
        if method == "run.list":
            return self.store.list_compact_workflows(
                active_only=_boolean(params.get("active_only", False), "active_only"),
                limit=_integer(params.get("limit", 100), "limit", low=1, high=100),
            )
        if method == "run.stop":
            return self.stop_workflow(params)
        if method == "run.retry":
            return self.retry_workflow(params)
        if method == "run.rerun":
            return self.rerun_workflow(params)
        if method == "daemon.stop":
            return self.request_daemon_stop()
        raise OrchestratorError("METHOD_NOT_FOUND", f"unsupported method: {method}")


def _workflow_id(
    params: Mapping[str, Any], store: OrchestratorStore, *, required: bool = False
) -> str:
    if "workflow_id" in params:
        value = params["workflow_id"]
        if not isinstance(value, str) or not SAFE_IDENTIFIER.fullmatch(value):
            raise OrchestratorError(
                "INVALID_WORKFLOW_ID",
                "workflow_id must be a non-empty safe identifier",
            )
        return value
    if required:
        raise OrchestratorError(
            "WORKFLOW_ID_REQUIRED", "workflow_id is required for mutating operations"
        )
    workflows = store.list_workflows(limit=1)
    if not workflows:
        raise OrchestratorError("RUN_NOT_FOUND", "no Pi Lane workflow exists", category="state")
    return str(workflows[0]["workflow_id"])


def _integer(value: object, label: str, *, low: int, high: int | None = None) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < low or (high is not None and value > high):
        raise OrchestratorError("INVALID_REQUEST", f"{label} is out of range")
    return value


def _number(value: object, label: str, *, low: float, high: float) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not low <= float(value) <= high:
        raise OrchestratorError("INVALID_REQUEST", f"{label} is out of range")
    return float(value)


def _boolean(value: object, label: str) -> bool:
    if not isinstance(value, bool):
        raise OrchestratorError("INVALID_REQUEST", f"{label} must be a JSON boolean")
    return value


class RequestHandler(socketserver.StreamRequestHandler):
    def handle(self) -> None:
        self.connection.settimeout(35)
        raw = self.rfile.readline(MAX_REQUEST_BYTES + 1)
        if len(raw) > MAX_REQUEST_BYTES:
            response = error_envelope(OrchestratorError("REQUEST_TOO_LARGE", "request exceeds 1 MiB"))
        else:
            request_id: str | None = None
            try:
                request = json.loads(raw.decode("utf-8"))
                if not isinstance(request, dict):
                    raise OrchestratorError("INVALID_REQUEST", "request must be a JSON object")
                request_id = request.get("request_id")
                if request_id is not None and (
                    not isinstance(request_id, str) or not 1 <= len(request_id) <= 128
                ):
                    request_id = None
                    raise OrchestratorError(
                        "INVALID_REQUEST", "request_id must be a 1-128 character string"
                    )
                if request.get("protocol_version") != PROTOCOL_VERSION:
                    raise OrchestratorError("PROTOCOL_MISMATCH", "unsupported protocol version")
                method = request.get("method")
                params = request.get("params", {})
                if not isinstance(method, str) or not isinstance(params, dict):
                    raise OrchestratorError("INVALID_REQUEST", "method and object params are required")
                service = self.server.service  # type: ignore[attr-defined]
                mutation = method in MUTATING_METHODS or (
                    method == "run.events" and params.get("acknowledge") is True
                )
                if mutation:
                    service.begin_mutation(method)
                try:
                    data = service.dispatch(method, params)
                finally:
                    if mutation:
                        service.end_mutation(method)
                response = success_envelope(method, data, request_id=request_id)
                if (
                    method == "daemon.stop"
                    and isinstance(data, Mapping)
                    and data.get("stopping") is True
                ):
                    threading.Thread(target=self.server.shutdown, daemon=True).start()
            except (UnicodeDecodeError, json.JSONDecodeError) as exc:
                response = error_envelope(
                    OrchestratorError("INVALID_JSON", str(exc)), request_id=request_id
                )
            except Exception as exc:
                response = error_envelope(exc, request_id=request_id)
        encoded = (json.dumps(response, separators=(",", ":")) + "\n").encode("utf-8")
        if len(encoded) > MAX_RESPONSE_BYTES:
            response = error_envelope(
                OrchestratorError(
                    "RESPONSE_TOO_LARGE",
                    "response exceeds 1 MiB; request a smaller event page or list limit",
                ),
                request_id=response.get("request_id") if isinstance(response, dict) else None,
            )
            encoded = (json.dumps(response, separators=(",", ":")) + "\n").encode("utf-8")
        self.wfile.write(encoded)


class UnixServer(socketserver.ThreadingUnixStreamServer):
    daemon_threads = False
    allow_reuse_address = False

    def __init__(self, socket_path: Path, service: OrchestratorService) -> None:
        self.service = service
        super().__init__(str(socket_path), RequestHandler)


def serve(*, db_path: Path = DEFAULT_DB, socket_path: Path = DEFAULT_SOCKET, lock_path: Path = DEFAULT_LOCK) -> int:
    db_path = db_path.expanduser().resolve()
    socket_path = socket_path.expanduser().resolve()
    lock_path = lock_path.expanduser().resolve()
    root = socket_path.parent
    ensure_private_directory(root, label="orchestrator socket parent")
    ensure_private_directory(lock_path.parent, label="orchestrator lock parent")
    lock_handle = lock_path.open("a+", encoding="utf-8")
    try:
        fcntl.flock(lock_handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        lock_handle.close()
        return 0
    lock_handle.seek(0)
    lock_handle.truncate()
    lock_handle.write(f"{os.getpid()}\n")
    lock_handle.flush()
    os.fchmod(lock_handle.fileno(), 0o600)
    endpoint_state, endpoint_identity = unix_socket_endpoint_state(socket_path)
    if endpoint_state not in {"absent", "stale"}:
        lock_handle.close()
        return 1
    if endpoint_state == "stale":
        try:
            current = socket_path.lstat()
        except FileNotFoundError:
            current_identity = None
        else:
            current_identity = (current.st_dev, current.st_ino, current.st_mode)
        if current_identity != endpoint_identity:
            lock_handle.close()
            return 1
        socket_path.unlink()
    store = OrchestratorStore(db_path)
    service = OrchestratorService(
        store, socket_path=socket_path, lock_path=lock_path
    )
    server = UnixServer(socket_path, service)
    os.chmod(socket_path, 0o600)
    bound_socket = socket_path.lstat()
    bound_socket_identity = (
        bound_socket.st_dev,
        bound_socket.st_ino,
        bound_socket.st_mode,
    )
    observer = threading.Thread(
        target=service.reconcile_loop,
        name="pi-lane-reconciler",
        daemon=False,
    )
    terminal_auditor = threading.Thread(
        target=service.terminal_audit_loop,
        name="pi-lane-terminal-auditor",
        daemon=False,
    )
    observer.start()
    terminal_auditor.start()

    def stop(_signum: int, _frame: object) -> None:
        service.stopping.set()
        threading.Thread(target=server.shutdown, daemon=True).start()

    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)
    try:
        server.serve_forever(poll_interval=0.5)
    finally:
        service.stopping.set()
        server.server_close()
        observer.join(timeout=65)
        terminal_auditor.join(timeout=65)
        try:
            current_socket = socket_path.lstat()
        except FileNotFoundError:
            current_socket_identity = None
        else:
            current_socket_identity = (
                current_socket.st_dev,
                current_socket.st_ino,
                current_socket.st_mode,
            )
        if current_socket_identity == bound_socket_identity:
            socket_path.unlink()
        fcntl.flock(lock_handle.fileno(), fcntl.LOCK_UN)
        lock_handle.close()
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    parser.add_argument("--db", type=Path, default=DEFAULT_DB)
    parser.add_argument("--socket", type=Path, default=DEFAULT_SOCKET)
    parser.add_argument("--lock", type=Path, default=DEFAULT_LOCK)
    return parser


def main() -> int:
    args = build_parser().parse_args()
    return serve(db_path=args.db, socket_path=args.socket, lock_path=args.lock)


if __name__ == "__main__":
    raise SystemExit(main())
