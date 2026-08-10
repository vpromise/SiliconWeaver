from __future__ import annotations

import json
import os
import socket
import sqlite3
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from typing import Any
from unittest.mock import Mock, patch

from pi_lane.orchestrator import (
    OrchestratorError,
    OrchestratorStore,
    ORCHESTRATOR_BUILD_ID,
    PROTOCOL_VERSION,
    canonical_json,
    classify_backend_failure,
    ensure_private_directory,
    parse_json_stdout,
    process_start_time,
    rtl_closure_runtime_config_fingerprint,
    sha256_text,
    structured_failure,
)
from pi_lane.orchestrator_cli import (
    MUTATION_TIMEOUT_SECONDS,
    _daemon_environment,
    _forget_daemon,
    _recognized_daemon_response,
    build_parser,
    ensure_daemon,
    main as orchestrator_cli_main,
    request,
)
from pi_lane.orchestrator_server import (
    OrchestratorService as ProductionOrchestratorService,
    _validate_rtl_closure_report,
)


def write_spec(root: Path, *, name: str = "demo", pdk: str = "sky130A") -> Path:
    path = root / f"{name}.json"
    path.write_text(
        json.dumps({"schema_version": 2, "design": {"name": name, "pdk": pdk}}),
        encoding="utf-8",
    )
    return path


def backend_report(
    workflow: dict[str, Any],
    run_id: str,
    *,
    status: str = "running",
    **values: Any,
) -> dict[str, Any]:
    spec = json.loads(Path(workflow["spec_path"]).read_text(encoding="utf-8"))
    child_key = "pi-lane:" + sha256_text(
        workflow["workflow_id"]
        + ":"
        + workflow["spec_sha256"]
        + ":"
        + workflow["idempotency_key"]
    )
    state_root = Path(
        os.environ.get("RTL_CLOSURE_STATE_DIR", "~/.local/state/rtl-closure")
    ).expanduser().resolve()
    report: dict[str, Any] = {
        "schema_version": 1,
        "run_id": run_id,
        "state_file": str(state_root / run_id / "state.json"),
        "design": workflow["design_name"],
        "pdk": workflow["pdk"],
        "status": status,
        "origin": {
            "source": "pi-lane",
            "workflow_id": workflow["workflow_id"],
            "idempotency_key": child_key,
            "idempotency_scope": workflow["workflow_id"],
            "spec_sha256": sha256_text(canonical_json(spec)),
        },
    }
    report.update(values)
    if report.get("status") == "completed":
        audit_id = str(
            report.get("last_integrity_audit_id")
            or f"test-audit-{sha256_text(run_id)[:16]}"
        )
        attestation_sha256 = str(
            report.get("delivery_attestation_sha256") or "a" * 64
        )
        report["last_integrity_audit_id"] = audit_id
        report["delivery_attestation_sha256"] = attestation_sha256
        if not isinstance(report.get("release_integrity"), dict):
            report["release_integrity"] = {
                "schema_version": 1,
                "audit_id": audit_id,
                "status": "verified",
                "checked_at": "2026-08-03T00:00:00+08:00",
                "attestation_sha256": attestation_sha256,
                "errors": [],
            }
    return report


def start_report(
    command: list[str], run_id: str, *, status: str = "running", **values: Any
) -> dict[str, Any]:
    spec_path = Path(command[command.index("--spec") + 1])
    workflow_id = command[command.index("--workflow-id") + 1]
    child_key = command[command.index("--idempotency-key") + 1]
    spec = json.loads(spec_path.read_text(encoding="utf-8"))
    state_root = Path(
        os.environ.get("RTL_CLOSURE_STATE_DIR", "~/.local/state/rtl-closure")
    ).expanduser().resolve()
    report: dict[str, Any] = {
        "schema_version": 1,
        "run_id": run_id,
        "state_file": str(state_root / run_id / "state.json"),
        "design": spec["design"]["name"],
        "pdk": spec["design"]["pdk"],
        "status": status,
        "origin": {
            "source": "pi-lane",
            "workflow_id": workflow_id,
            "idempotency_key": child_key,
            "idempotency_scope": workflow_id,
            "spec_sha256": sha256_text(canonical_json(spec)),
        },
    }
    report.update(values)
    return report


def contract_runner(store: OrchestratorStore, runner):
    retry_reports: dict[str, dict[str, Any]] = {}
    integrity_audits: dict[str, str] = {}

    def wrapped(command, **kwargs):
        action = command[1] if len(command) > 1 else ""
        if action == "report":
            run_id = command[command.index("--run-id") + 1]
            cached = retry_reports.pop(run_id, None)
            if cached is not None:
                workflow = store.find_by_rtl_closure_run(run_id)
                assert workflow is not None
                protected = {
                    "run_id",
                    "schema_version",
                    "origin",
                    "state_file",
                    "design",
                    "pdk",
                }
                cached_values = {
                    key: value
                    for key, value in cached.items()
                    if key not in protected
                }
                if run_id in integrity_audits:
                    cached_values["last_integrity_audit_id"] = integrity_audits[
                        run_id
                    ]
                payload = backend_report(workflow, run_id, **cached_values)
                return subprocess.CompletedProcess(
                    command, 0, stdout=json.dumps(payload), stderr=""
                )
        completed = runner(command, **kwargs)
        if action not in {"start", "stop", "report", "reconcile", "retry-lane"}:
            return completed
        try:
            payload = json.loads(completed.stdout)
        except (TypeError, json.JSONDecodeError):
            return completed
        if not isinstance(payload, dict):
            return completed
        if action == "reconcile":
            run_id = payload.get("run_id")
            if (
                isinstance(run_id, str)
                and "--integrity-audit-id" in command
            ):
                audit_id = command[command.index("--integrity-audit-id") + 1]
                if payload.get("status") == "completed":
                    integrity_audits[run_id] = audit_id
                    payload["last_integrity_audit_id"] = audit_id
                elif payload.get("last_integrity_audit_id") == audit_id:
                    integrity_audits[run_id] = audit_id
            return subprocess.CompletedProcess(
                completed.args,
                completed.returncode,
                stdout=json.dumps(payload),
                stderr=completed.stderr,
            )
        if action == "retry-lane":
            run_id = command[command.index("--run-id") + 1]
            retry_reports[run_id] = dict(payload)
            return completed
        run_id = payload.get("run_id")
        if not isinstance(run_id, str):
            return completed
        if action == "start":
            base = start_report(command, run_id, status=str(payload.get("status") or "running"))
        else:
            workflow = store.find_by_rtl_closure_run(run_id)
            if workflow is None:
                return completed
            report_values: dict[str, Any] = {}
            if run_id in integrity_audits:
                report_values["last_integrity_audit_id"] = integrity_audits[run_id]
            base = backend_report(
                workflow,
                run_id,
                status=str(payload.get("status") or "running"),
                **report_values,
            )
        enriched = {**base, **payload}
        return subprocess.CompletedProcess(
            completed.args,
            completed.returncode,
            stdout=json.dumps(enriched),
            stderr=completed.stderr,
        )

    return wrapped


class OrchestratorService(ProductionOrchestratorService):
    def __init__(self, store, *, command_runner=subprocess.run, **kwargs):
        if command_runner is not subprocess.run:
            command_runner = contract_runner(store, command_runner)
        super().__init__(store, command_runner=command_runner, **kwargs)


class StoreTests(unittest.TestCase):
    def test_existing_attempt_reuse_requires_same_active_launch_identity(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            store = OrchestratorStore(root / "state.sqlite3")
            workflow, _ = store.create_workflow(
                write_spec(root), idempotency_key="attempt-identity"
            )
            bound, first_attempt = store.bind_attempt_and_update_workflow(
                workflow["workflow_id"],
                kind="lane",
                backend_run_id="lane-stable",
                launch_request_sha256="a" * 64,
                reason="initial",
                status="running",
                phase="lane_running",
                snapshot={"status": "running"},
                active_lane_run_id="lane-stable",
                expected_revision=workflow["revision"],
            )
            rebound, repeated_attempt = store.bind_attempt_and_update_workflow(
                workflow["workflow_id"],
                kind="lane",
                backend_run_id="lane-stable",
                launch_request_sha256="a" * 64,
                reason="idempotent_replay",
                status="running",
                phase="lane_running",
                snapshot={"status": "running", "replayed": True},
                active_lane_run_id="lane-stable",
                expected_revision=bound["revision"],
            )
            self.assertEqual(repeated_attempt["attempt_id"], first_attempt["attempt_id"])

            with self.assertRaises(OrchestratorError) as identity_error:
                store.bind_attempt_and_update_workflow(
                    workflow["workflow_id"],
                    kind="lane",
                    backend_run_id="lane-stable",
                    launch_request_sha256="b" * 64,
                    reason="wrong_request",
                    status="running",
                    phase="lane_running",
                    snapshot={},
                    active_lane_run_id="lane-stable",
                    expected_revision=rebound["revision"],
                )
            self.assertEqual(identity_error.exception.code, "ATTEMPT_IDENTITY_MISMATCH")

            closed = store.update_workflow(
                workflow["workflow_id"],
                status="waiting_user",
                phase="lane_failed",
                snapshot={"status": "failed"},
                expected_revision=rebound["revision"],
                close_running_attempts_as="failed",
            )
            with self.assertRaises(OrchestratorError) as inactive:
                store.bind_attempt_and_update_workflow(
                    workflow["workflow_id"],
                    kind="lane",
                    backend_run_id="lane-stable",
                    launch_request_sha256="a" * 64,
                    reason="terminal_reuse",
                    status="running",
                    phase="lane_running",
                    snapshot={},
                    active_lane_run_id="lane-stable",
                    expected_revision=closed["revision"],
                )
            self.assertEqual(inactive.exception.code, "ATTEMPT_NOT_ACTIVE")

    def test_attempt_backend_and_projection_identity_must_match(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            store = OrchestratorStore(root / "state.sqlite3")
            workflow, _ = store.create_workflow(
                write_spec(root), idempotency_key="attempt-projection-identity"
            )
            with self.assertRaises(OrchestratorError) as mismatch:
                store.bind_attempt_and_update_workflow(
                    workflow["workflow_id"],
                    kind="lane",
                    backend_run_id="lane-attempt",
                    launch_request_sha256="a" * 64,
                    reason="mismatch",
                    status="running",
                    phase="lane_running",
                    snapshot={},
                    active_lane_run_id="lane-projection",
                    expected_revision=workflow["revision"],
                )
            self.assertEqual(mismatch.exception.code, "BACKEND_IDENTITY_MISMATCH")

    def test_newer_database_schema_is_rejected_without_downgrade(self):
        with tempfile.TemporaryDirectory() as directory:
            database = Path(directory) / "state.sqlite3"
            OrchestratorStore(database)
            with sqlite3.connect(database) as connection:
                connection.execute(
                    "UPDATE metadata SET value='99' WHERE key='schema_version'"
                )
            with self.assertRaises(OrchestratorError) as unsupported:
                OrchestratorStore(database)
            self.assertEqual(unsupported.exception.code, "UNSUPPORTED_DATABASE_SCHEMA")
            with sqlite3.connect(database) as connection:
                version = connection.execute(
                    "SELECT value FROM metadata WHERE key='schema_version'"
                ).fetchone()[0]
            self.assertEqual(version, "99")

    def test_newer_database_schema_is_rejected_before_any_ddl(self):
        with tempfile.TemporaryDirectory() as directory:
            database = Path(directory) / "future.sqlite3"
            with sqlite3.connect(database) as connection:
                connection.execute(
                    "CREATE TABLE metadata (key TEXT PRIMARY KEY, value TEXT NOT NULL)"
                )
                connection.execute(
                    "INSERT INTO metadata(key,value) VALUES('schema_version','99')"
                )
                connection.execute("CREATE TABLE future_only (value TEXT)")
                before = connection.execute(
                    "SELECT type,name,sql FROM sqlite_master ORDER BY type,name"
                ).fetchall()
                journal_mode = connection.execute("PRAGMA journal_mode").fetchone()[0]

            with self.assertRaises(OrchestratorError) as unsupported:
                OrchestratorStore(database)

            self.assertEqual(unsupported.exception.code, "UNSUPPORTED_DATABASE_SCHEMA")
            with sqlite3.connect(database) as connection:
                after = connection.execute(
                    "SELECT type,name,sql FROM sqlite_master ORDER BY type,name"
                ).fetchall()
                self.assertEqual(
                    connection.execute("PRAGMA journal_mode").fetchone()[0], journal_mode
                )
            self.assertEqual(after, before)

    def test_malformed_schema_five_is_rejected_without_repair(self):
        with tempfile.TemporaryDirectory() as directory:
            database = Path(directory) / "state.sqlite3"
            OrchestratorStore(database)
            with sqlite3.connect(database) as connection:
                connection.execute("DROP TABLE events")
                before = connection.execute(
                    "SELECT type,name,sql FROM sqlite_master ORDER BY type,name"
                ).fetchall()

            with self.assertRaises(OrchestratorError) as malformed:
                OrchestratorStore(database)

            self.assertEqual(malformed.exception.code, "INVALID_DATABASE_SCHEMA")
            with sqlite3.connect(database) as connection:
                after = connection.execute(
                    "SELECT type,name,sql FROM sqlite_master ORDER BY type,name"
                ).fetchall()
            self.assertEqual(after, before)

    def test_workflow_uses_a_private_snapshot_of_exact_source_bytes(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = write_spec(root, name="before")
            original = source.read_bytes()
            store = OrchestratorStore(root / "state.sqlite3")
            workflow, created = store.create_workflow(source, idempotency_key=None)

            self.assertTrue(created)
            snapshot = Path(workflow["spec_path"])
            self.assertNotEqual(snapshot, source)
            self.assertEqual(snapshot.read_bytes(), original)
            self.assertEqual(workflow["source_spec_path"], str(source.resolve()))
            self.assertEqual(snapshot.stat().st_mode & 0o777, 0o400)

            source.write_text('{"design":{"name":"after","pdk":"sky130A"}}')
            self.assertEqual(snapshot.read_bytes(), original)

    def test_idempotency_event_chain_and_terminal_cas(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            store = OrchestratorStore(root / "state.sqlite3")
            spec = write_spec(root)
            workflow, created = store.create_workflow(spec, idempotency_key="same")
            self.assertTrue(created)
            again, created = store.create_workflow(spec, idempotency_key="same")
            self.assertFalse(created)
            self.assertEqual(again["workflow_id"], workflow["workflow_id"])
            self.assertIn("attempts", again)

            first = store.events(workflow["workflow_id"])["events"][0]
            self.assertEqual(first["type"], "workflow.created")
            self.assertIsNone(first["previous_sha256"])
            updated = store.update_workflow(
                workflow["workflow_id"],
                status="completed",
                phase="completed",
                snapshot={"status": "completed"},
                expected_revision=0,
                event_type="workflow.terminal",
            )
            self.assertEqual(updated["revision"], 1)
            events = store.events(workflow["workflow_id"])["events"]
            self.assertEqual(events[1]["previous_sha256"], events[0]["sha256"])
            with self.assertRaisesRegex(OrchestratorError, "terminal"):
                store.update_workflow(
                    workflow["workflow_id"],
                    status="running",
                    phase="repairing",
                    snapshot={},
                )

    def test_terminal_transition_cancels_stranded_operations(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            store = OrchestratorStore(root / "state.sqlite3")
            workflow, _ = store.create_workflow(
                write_spec(root), idempotency_key="terminal-operations"
            )
            store.create_operation(
                workflow["workflow_id"], kind="retry", request={"scope": "lane"}
            )
            terminal = store.update_workflow(
                workflow["workflow_id"],
                status="completed",
                phase="completed",
                snapshot={"status": "completed"},
            )
            self.assertEqual(terminal["operations"][0]["status"], "cancelled")
            with self.assertRaises(OrchestratorError) as immutable:
                store.create_operation(
                    workflow["workflow_id"], kind="stop", request={}
                )
            self.assertEqual(immutable.exception.code, "TERMINAL_STATE")

    def test_event_payload_hash_is_verified_when_read(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            database = root / "state.sqlite3"
            store = OrchestratorStore(database)
            workflow, _ = store.create_workflow(
                write_spec(root), idempotency_key="event-integrity"
            )
            with sqlite3.connect(database) as connection:
                connection.execute(
                    "UPDATE events SET payload_json='{}' "
                    "WHERE workflow_id=? AND sequence=1",
                    (workflow["workflow_id"],),
                )

            with self.assertRaises(OrchestratorError) as corrupted:
                store.events(workflow["workflow_id"])

            self.assertEqual(corrupted.exception.code, "JOURNAL_INTEGRITY_ERROR")

    def test_idempotency_conflict_and_open_pdk_policy(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            store = OrchestratorStore(root / "state.sqlite3")
            first = write_spec(root, name="one")
            second = write_spec(root, name="two")
            store.create_workflow(first, idempotency_key="key")
            with self.assertRaisesRegex(
                OrchestratorError, "different workflow operation"
            ):
                store.create_workflow(second, idempotency_key="key")
            commercial = write_spec(root, name="nda", pdk="commercial28")
            with self.assertRaisesRegex(OrchestratorError, "must be one of"):
                store.create_workflow(commercial, idempotency_key="nda")
            with self.assertRaises(OrchestratorError) as unsafe_id:
                store.create_workflow(
                    first,
                    idempotency_key="unsafe-id",
                    workflow_id="../outside",
                )
            self.assertEqual(unsafe_id.exception.code, "INVALID_WORKFLOW_ID")

    def test_idempotency_key_is_bound_to_rerun_lineage_and_explicit_identity(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            store = OrchestratorStore(root / "state.sqlite3")
            spec = write_spec(root)
            first_parent, _ = store.create_workflow(
                spec, idempotency_key="first-parent"
            )
            second_parent, _ = store.create_workflow(
                spec, idempotency_key="second-parent"
            )
            for parent in (first_parent, second_parent):
                store.update_workflow(
                    parent["workflow_id"],
                    status="completed",
                    phase="completed",
                    snapshot={"status": "completed"},
                )
            successor, created = store.create_workflow(
                spec,
                idempotency_key="shared-successor",
                supersedes_workflow_id=first_parent["workflow_id"],
            )
            self.assertTrue(created)
            replay, created = store.create_workflow(
                spec,
                idempotency_key="shared-successor",
                supersedes_workflow_id=first_parent["workflow_id"],
            )
            self.assertFalse(created)
            self.assertEqual(replay["workflow_id"], successor["workflow_id"])
            for kwargs in (
                {"supersedes_workflow_id": second_parent["workflow_id"]},
                {},
                {
                    "workflow_id": "different-workflow",
                    "supersedes_workflow_id": first_parent["workflow_id"],
                },
            ):
                with self.assertRaises(OrchestratorError) as conflict:
                    store.create_workflow(
                        spec,
                        idempotency_key="shared-successor",
                        **kwargs,
                    )
                self.assertEqual(conflict.exception.code, "IDEMPOTENCY_CONFLICT")

    def test_private_state_directories_are_rejected_without_chmod(self):
        with tempfile.TemporaryDirectory() as directory:
            public = Path(directory) / "shared-project"
            public.mkdir(mode=0o775)
            os.chmod(public, 0o775)
            with self.assertRaises(OrchestratorError) as rejected:
                OrchestratorStore(public / "orchestrator.sqlite3")
            self.assertEqual(rejected.exception.code, "INSECURE_STATE_DIRECTORY")
            self.assertEqual(public.stat().st_mode & 0o777, 0o775)
            with self.assertRaises(OrchestratorError):
                ensure_private_directory(public, label="test state")
            self.assertEqual(public.stat().st_mode & 0o777, 0o775)

    def test_attempt_lineage_cursor_and_compact_budget(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            store = OrchestratorStore(root / "state.sqlite3")
            workflow, _ = store.create_workflow(write_spec(root), idempotency_key="key")
            one = store.add_attempt(
                workflow["workflow_id"],
                kind="lane",
                backend_run_id="lane-1",
                launch_request_sha256="a" * 64,
                reason="initial",
            )
            two = store.add_attempt(
                workflow["workflow_id"],
                kind="lane",
                backend_run_id="lane-2",
                launch_request_sha256="b" * 64,
                reason="retry",
            )
            self.assertEqual(two["supersedes_attempt_id"], one["attempt_id"])
            attempts = store.get_workflow(workflow["workflow_id"])["attempts"]
            self.assertEqual(attempts[0]["status"], "superseded")
            self.assertIsNotNone(attempts[0]["finished_at"])
            report = store.events(
                workflow["workflow_id"],
                client_id="monitor",
                acknowledge=True,
            )
            self.assertEqual(report["acknowledged_sequence"], report["next_after_sequence"])
            stable = store.events(
                workflow["workflow_id"],
                after_sequence=report["next_after_sequence"],
            )
            self.assertEqual(stable["events"], [])
            self.assertEqual(stable["cursor_sha256"], report["cursor_sha256"])
            with self.assertRaisesRegex(OrchestratorError, "ahead"):
                store.events(workflow["workflow_id"], after_sequence=10_000)
            compact = store.compact_status(workflow["workflow_id"])
            self.assertLessEqual(len(json.dumps(compact).encode("utf-8")), 8192)

    def test_compact_list_never_exposes_specs_or_unbounded_backend_fields(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            store = OrchestratorStore(root / "state.sqlite3")
            workflow, _ = store.create_workflow(write_spec(root), idempotency_key="key")
            store.update_workflow(
                workflow["workflow_id"],
                status="running",
                phase="lane_running",
                snapshot={
                    "current_step": "x" * 100_000,
                    "delivery_gate": {"status": "running", "detail": "y" * 100_000},
                },
            )
            listing = store.list_compact_workflows(limit=100)
            self.assertEqual(len(listing), 1)
            self.assertNotIn("spec_path", listing[0])
            self.assertLessEqual(len(canonical_json(listing[0]).encode("utf-8")), 8192)

    def test_concurrent_expected_revision_has_one_winner(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            store = OrchestratorStore(root / "state.sqlite3")
            workflow, _ = store.create_workflow(write_spec(root), idempotency_key="key")
            barrier = threading.Barrier(2)
            outcomes: list[str] = []

            def update(phase: str) -> None:
                barrier.wait()
                try:
                    store.update_workflow(
                        workflow["workflow_id"],
                        status="running",
                        phase=phase,
                        snapshot={"phase": phase},
                        expected_revision=0,
                    )
                    outcomes.append("updated")
                except OrchestratorError as exc:
                    outcomes.append(exc.code)

            threads = [threading.Thread(target=update, args=(f"phase-{index}",)) for index in range(2)]
            for thread in threads:
                thread.start()
            for thread in threads:
                thread.join()
            self.assertCountEqual(outcomes, ["updated", "REVISION_CONFLICT"])
            events = store.events(workflow["workflow_id"])["events"]
            self.assertEqual([row["sequence"] for row in events], [1, 2])

    def test_dormant_workflows_are_not_hot_reconciled(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            store = OrchestratorStore(root / "state.sqlite3")
            running, _ = store.create_workflow(
                write_spec(root, name="running"), idempotency_key="running"
            )
            waiting, _ = store.create_workflow(
                write_spec(root, name="waiting"), idempotency_key="waiting"
            )
            store.update_workflow(
                running["workflow_id"],
                status="running",
                phase="generating",
                snapshot={},
            )
            store.update_workflow(
                waiting["workflow_id"],
                status="waiting_user",
                phase="failed",
                snapshot={},
            )
            self.assertEqual(
                [item["workflow_id"] for item in store.active_for_reconcile()],
                [running["workflow_id"]],
            )

    def test_workflow_projection_must_match_its_latest_transition_event(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            database = root / "state.sqlite3"
            store = OrchestratorStore(database)
            workflow, _ = store.create_workflow(
                write_spec(root), idempotency_key="projection-integrity"
            )
            store.update_workflow(
                workflow["workflow_id"],
                status="running",
                phase="generating",
                snapshot={"status": "running"},
            )
            with sqlite3.connect(database) as connection:
                connection.execute(
                    "UPDATE workflows SET status='completed',phase='completed' "
                    "WHERE workflow_id=?",
                    (workflow["workflow_id"],),
                )

            with self.assertRaises(OrchestratorError) as corrupted:
                store.get_workflow(workflow["workflow_id"])

            self.assertEqual(corrupted.exception.code, "JOURNAL_INTEGRITY_ERROR")

    def test_workflow_projection_event_hash_is_verified_on_workflow_read(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            database = root / "state.sqlite3"
            store = OrchestratorStore(database)
            workflow, _ = store.create_workflow(
                write_spec(root), idempotency_key="projection-event-integrity"
            )
            with sqlite3.connect(database) as connection:
                connection.execute(
                    "UPDATE events SET payload_json=? "
                    "WHERE workflow_id=? AND sequence=1",
                    (
                        canonical_json({"status": "created", "revision": 0}),
                        workflow["workflow_id"],
                    ),
                )

            with self.assertRaises(OrchestratorError) as corrupted:
                store.get_workflow(workflow["workflow_id"])

            self.assertEqual(corrupted.exception.code, "JOURNAL_INTEGRITY_ERROR")

    def test_default_lease_covers_the_longest_backend_operation(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            database = root / "state.sqlite3"
            store = OrchestratorStore(database)
            workflow, _ = store.create_workflow(
                write_spec(root), idempotency_key="lease-ttl"
            )
            with patch("pi_lane.orchestrator.time.time", return_value=100.0):
                self.assertTrue(
                    store.acquire_lease(workflow["workflow_id"], owner="test")
                )
            with sqlite3.connect(database) as connection:
                expires_at = connection.execute(
                    "SELECT expires_at FROM leases WHERE workflow_id=?",
                    (workflow["workflow_id"],),
                ).fetchone()[0]
            self.assertEqual(expires_at, 400.0)

    def test_event_page_is_bounded_and_carries_one_snapshot_watermark(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            store = OrchestratorStore(root / "state.sqlite3")
            workflow, _ = store.create_workflow(write_spec(root), idempotency_key="key")
            for index in range(12):
                store.append_event(
                    workflow["workflow_id"],
                    event_type="test.large",
                    source="test",
                    phase="test",
                    payload={"index": index, "data": "x" * 9000},
                )
            report = store.events(
                workflow["workflow_id"], limit=1000, max_bytes=64 * 1024
            )
            self.assertTrue(report["has_more"])
            self.assertEqual(
                report["snapshot_revision"], report["snapshot"]["revision"]
            )
            self.assertEqual(
                report["snapshot_journal_sequence"], report["journal_sequence"]
            )
            self.assertLess(len(canonical_json(report).encode("utf-8")), 64 * 1024)

    def test_failure_classification_prefers_counterexample_over_formal_gate(self):
        failure = classify_backend_failure(
            {
                "status": "failed",
                "failure_gate": "formal",
                "failure_class": "formal_counterexample",
                "error": "counterexample at cycle 7",
            }
        )
        self.assertIsNotNone(failure)
        self.assertEqual(failure["domain"], "generated_rtl")
        self.assertTrue(failure["repairable_by_rtl"])
        provider = classify_backend_failure(
            {"status": "failed", "failure_class": "verification_provider"}
        )
        self.assertEqual(provider["domain"], "verification")
        self.assertTrue(provider["retryable"])
        lane_launch = classify_backend_failure(
            {"status": "failed", "failure_class": "lane_launch"}
        )
        self.assertEqual(lane_launch["domain"], "infrastructure")
        self.assertFalse(lane_launch["repairable_by_rtl"])

    def test_typed_backend_failure_overrides_marker_inference_field_by_field(self):
        failure = classify_backend_failure(
            {
                "status": "failed",
                "failure_class": "lane_launch",
                "failure": {
                    "code": "MODEL_CONFIGURATION_MISSING",
                    "category": "model_configuration",
                    "domain": "user_intent",
                    "message": "the selected model is not configured",
                    "retryable": False,
                    "repairable_by_rtl": False,
                    "user_action_required": True,
                },
            }
        )
        self.assertEqual(failure["code"], "MODEL_CONFIGURATION_MISSING")
        self.assertEqual(failure["category"], "model_configuration")
        self.assertEqual(failure["domain"], "user_intent")
        self.assertEqual(failure["message"], "the selected model is not configured")
        self.assertFalse(failure["retryable"])
        self.assertFalse(failure["repairable_by_rtl"])
        self.assertTrue(failure["user_action_required"])

        partial = classify_backend_failure(
            {
                "status": "failed",
                "failure_class": "verification_provider",
                "failure": {"code": "PROVIDER_UNAVAILABLE", "retryable": False},
            }
        )
        self.assertEqual(partial["code"], "PROVIDER_UNAVAILABLE")
        self.assertEqual(partial["domain"], "verification")
        self.assertFalse(partial["retryable"])
        self.assertFalse(partial["repairable_by_rtl"])

    def test_attempt_binding_rolls_back_the_attempt_and_projection_together(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            store = OrchestratorStore(root / "state.sqlite3")
            workflow, _ = store.create_workflow(write_spec(root), idempotency_key="key")
            initial_events = store.events(workflow["workflow_id"])["events"]

            with patch.object(
                store, "_insert_event", side_effect=RuntimeError("injected event failure")
            ):
                with self.assertRaisesRegex(RuntimeError, "injected event failure"):
                    store.bind_attempt_and_update_workflow(
                        workflow["workflow_id"],
                        kind="rtl_closure",
                        backend_run_id="rtl-atomic",
                        launch_request_sha256="a" * 64,
                        reason="fault_injection",
                        status="running",
                        phase="generating",
                        snapshot={"status": "running"},
                        rtl_closure_run_id="rtl-atomic",
                        expected_revision=workflow["revision"],
                        event_type="rtl_closure.started",
                    )

            unchanged = store.get_workflow(workflow["workflow_id"])
            self.assertEqual(unchanged["revision"], workflow["revision"])
            self.assertIsNone(unchanged["rtl_closure_run_id"])
            self.assertEqual(unchanged["attempts"], [])
            self.assertEqual(
                store.events(workflow["workflow_id"])["events"], initial_events
            )


class ServiceTests(unittest.TestCase):
    def test_current_protocol_daemon_response_requires_full_service_identity(self):
        valid = {
            "ok": True,
            "data": {
                "ready": True,
                "pid": 42,
                "protocol_version": PROTOCOL_VERSION,
                "build_id": "pi-lane-orchestrator-test",
            },
        }
        self.assertTrue(
            _recognized_daemon_response(valid, protocol_version=PROTOCOL_VERSION)
        )
        for field, replacement in (
            ("pid", 0),
            ("protocol_version", 1),
            ("build_id", "unrelated-service"),
        ):
            malformed = json.loads(json.dumps(valid))
            malformed["data"][field] = replacement
            with self.subTest(field=field):
                self.assertFalse(
                    _recognized_daemon_response(
                        malformed, protocol_version=PROTOCOL_VERSION
                    )
                )

    def test_cli_never_dispatches_start_during_deferred_build_handoff(self):
        with tempfile.TemporaryDirectory() as directory:
            spec = write_spec(Path(directory))
            deferred = {
                "ok": True,
                "data": {
                    "ready": True,
                    "reload_deferred": True,
                    "reload_deferred_reason": "active mutations",
                },
            }
            with (
                patch.object(
                    sys,
                    "argv",
                    ["pi-lanectl", "start", "--spec", str(spec), "--json"],
                ),
                patch(
                    "pi_lane.orchestrator_cli.ensure_daemon",
                    return_value=deferred,
                ),
                patch("pi_lane.orchestrator_cli.request") as daemon_request,
                patch("builtins.print"),
            ):
                result = orchestrator_cli_main()
            self.assertEqual(result, 1)
            daemon_request.assert_not_called()

    def test_stop_records_lane_attempt_discovered_in_terminal_response(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            store = OrchestratorStore(root / "state.sqlite3")
            workflow, _ = store.create_workflow(
                write_spec(root), idempotency_key="stop-discovers-lane"
            )
            running = store.update_workflow(
                workflow["workflow_id"],
                status="running",
                phase="rtl_closure_running",
                snapshot={"status": "running"},
                rtl_closure_run_id="rtl-stop-discovery",
            )

            def runner(command, **_kwargs):
                self.assertEqual(command[0], "rtl-closure")
                self.assertEqual(command[1], "stop")
                return subprocess.CompletedProcess(
                    command,
                    0,
                    stdout=json.dumps(
                        {
                            "run_id": "rtl-stop-discovery",
                            "lane_run_id": "lane-stop-discovery",
                            "status": "stopped",
                            "phase": "stopped",
                        }
                    ),
                    stderr="",
                )

            stopped = OrchestratorService(store, command_runner=runner).stop_workflow(
                {"workflow_id": running["workflow_id"]}
            )
            self.assertEqual(stopped["status"], "cancelled")
            self.assertEqual(stopped["active_lane_run_id"], "lane-stop-discovery")
            lane_attempts = [
                attempt for attempt in stopped["attempts"] if attempt["kind"] == "lane"
            ]
            self.assertEqual(len(lane_attempts), 1)
            self.assertEqual(lane_attempts[0]["backend_run_id"], "lane-stop-discovery")
            self.assertEqual(lane_attempts[0]["status"], "cancelled")
            self.assertEqual(lane_attempts[0]["reason"], "stop_discovered_child")

    def test_historical_blocked_backend_child_reconciles_to_failed_terminal(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            store = OrchestratorStore(root / "state.sqlite3")
            workflow, _ = store.create_workflow(
                write_spec(root), idempotency_key="historical-blocked-child"
            )
            running, _ = store.bind_attempt_and_update_workflow(
                workflow["workflow_id"],
                kind="rtl_closure",
                backend_run_id="rtl-historical-blocked",
                launch_request_sha256="a" * 64,
                reason="initial",
                status="running",
                phase="generating",
                snapshot={"status": "running"},
                rtl_closure_run_id="rtl-historical-blocked",
                expected_revision=workflow["revision"],
            )
            blocked = store.update_workflow(
                workflow["workflow_id"],
                status="blocked_infrastructure",
                phase="backend_unavailable",
                snapshot={"status": "failed"},
                expected_revision=running["revision"],
            )
            self.assertIn(
                blocked["workflow_id"],
                [item["workflow_id"] for item in store.active_for_reconcile()],
            )

            def runner(command, **_kwargs):
                report = {
                    "run_id": "rtl-historical-blocked",
                    "status": "failed",
                    "phase": "failed",
                    "failure_class": "infrastructure",
                    "error": "provider unavailable",
                }
                return subprocess.CompletedProcess(
                    command, 1, stdout=json.dumps(report), stderr=""
                )

            failed = OrchestratorService(store, command_runner=runner).reconcile_one(
                blocked
            )
            self.assertEqual(failed["status"], "failed")
            self.assertEqual(failed["failure"]["domain"], "infrastructure")
            self.assertTrue(failed["failure"]["retryable"])
            self.assertTrue(
                all(attempt["status"] == "failed" for attempt in failed["attempts"])
            )
            self.assertNotIn(
                failed["workflow_id"],
                [item["workflow_id"] for item in store.active_for_reconcile()],
            )

    def test_cli_mutation_timeout_exceeds_server_start_budget(self):
        self.assertGreater(MUTATION_TIMEOUT_SECONDS, 60 + 90 + 30)

    def test_stop_requires_a_confirmed_backend_terminal_state(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            store = OrchestratorStore(root / "state.sqlite3")
            workflow, _ = store.create_workflow(
                write_spec(root), idempotency_key="stop-confirmation"
            )
            running = store.update_workflow(
                workflow["workflow_id"],
                status="running",
                phase="generating",
                snapshot={"status": "running"},
                rtl_closure_run_id="rtl-stop-confirmation",
            )
            responses = iter(
                (
                    {
                        "run_id": "rtl-stop-confirmation",
                        "status": "running",
                        "phase": "stopping",
                    },
                    {
                        "run_id": "rtl-stop-confirmation",
                        "status": "stopped",
                        "phase": "stopped",
                    },
                )
            )

            def runner(command, **_kwargs):
                self.assertEqual(command[1], "stop")
                self.assertIn("--json", command)
                return subprocess.CompletedProcess(
                    command, 0, stdout=json.dumps(next(responses)), stderr=""
                )

            service = OrchestratorService(store, command_runner=runner)
            pending = service.stop_workflow({"workflow_id": running["workflow_id"]})
            self.assertEqual(pending["status"], "running")
            self.assertEqual(pending["phase"], "stopping")
            self.assertEqual(pending["operations"][0]["status"], "executing")

            stopped = service.reconcile_one(pending)
            self.assertEqual(stopped["status"], "cancelled")
            self.assertEqual(stopped["phase"], "cancelled")
            self.assertIsNone(stopped["failure"])
            self.assertEqual(stopped["operations"][0]["status"], "completed")

    def test_terminal_integrity_audit_degrades_a_completed_workflow(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            store = OrchestratorStore(root / "state.sqlite3")
            workflow, _ = store.create_workflow(
                write_spec(root), idempotency_key="terminal-audit"
            )
            running = store.update_workflow(
                workflow["workflow_id"],
                status="running",
                phase="lane_running",
                snapshot={"status": "running"},
                rtl_closure_run_id="rtl-terminal-audit",
                active_lane_run_id="lane-terminal-audit",
            )
            completed = store.update_workflow(
                running["workflow_id"],
                status="completed",
                phase="completed",
                snapshot={"status": "completed"},
            )
            self.assertIn(
                completed["workflow_id"],
                [row["workflow_id"] for row in store.terminal_audits_due()],
            )
            incomplete = {
                "run_id": "rtl-terminal-audit",
                "lane_run_id": "lane-terminal-audit",
                "status": "incomplete",
                "phase": "release_integrity_incomplete",
                "failure_class": "release_integrity",
                "failure_gate": "publication_manifest",
                "failure_code": "release_integrity/publication_invalid",
                "error": "published release evidence changed",
            }

            def runner(command, **_kwargs):
                return subprocess.CompletedProcess(
                    command, 0, stdout=json.dumps(incomplete), stderr=""
                )

            degraded = OrchestratorService(
                store, command_runner=runner
            ).reconcile_one(completed)

            self.assertEqual(degraded["status"], "failed")
            self.assertEqual(degraded["phase"], "release_integrity_incomplete")
            self.assertEqual(
                degraded["failure"]["code"],
                "RELEASE_INTEGRITY/PUBLICATION_INVALID",
            )
            events = store.events(degraded["workflow_id"])["events"]
            self.assertEqual(events[-1]["type"], "workflow.integrity_degraded")
            self.assertNotIn(
                degraded["workflow_id"],
                [row["workflow_id"] for row in store.active_for_reconcile()],
            )

    def test_successful_terminal_audit_is_scheduled_without_mutating_completion(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            store = OrchestratorStore(root / "state.sqlite3")
            workflow, _ = store.create_workflow(
                write_spec(root), idempotency_key="terminal-audit-pass"
            )
            running = store.update_workflow(
                workflow["workflow_id"],
                status="running",
                phase="completed",
                snapshot={"status": "running"},
                rtl_closure_run_id="rtl-terminal-audit-pass",
                active_lane_run_id="lane-terminal-audit-pass",
            )
            completed = store.update_workflow(
                running["workflow_id"],
                status="completed",
                phase="completed",
                snapshot={"status": "completed"},
            )
            report = {
                "run_id": "rtl-terminal-audit-pass",
                "lane_run_id": "lane-terminal-audit-pass",
                "status": "completed",
                "phase": "completed",
                "process_status": "completed",
                "flow_scope": "full",
                "endpoint_reached": True,
                "delivery_gate": "pass",
            }

            def runner(command, **_kwargs):
                return subprocess.CompletedProcess(
                    command, 0, stdout=json.dumps(report), stderr=""
                )

            audited = OrchestratorService(store, command_runner=runner).reconcile_one(
                completed
            )
            with sqlite3.connect(store.db_path) as connection:
                next_check = connection.execute(
                    "SELECT next_check_epoch FROM terminal_integrity_audits "
                    "WHERE workflow_id=?",
                    (completed["workflow_id"],),
                ).fetchone()[0]

            self.assertEqual(audited["status"], "completed")
            self.assertNotIn(
                completed["workflow_id"],
                [
                    row["workflow_id"]
                    for row in store.terminal_audits_due(
                        now_epoch=float(next_check) - 1
                    )
                ],
            )
            self.assertIn(
                completed["workflow_id"],
                [
                    row["workflow_id"]
                    for row in store.terminal_audits_due(
                        now_epoch=float(next_check) + 1
                    )
                ],
            )

    def test_terminal_integrity_audit_rejects_a_stale_backend_audit_id(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            store = OrchestratorStore(root / "state.sqlite3")
            workflow, _ = store.create_workflow(
                write_spec(root), idempotency_key="terminal-audit-stale"
            )
            running = store.update_workflow(
                workflow["workflow_id"],
                status="running",
                phase="completed",
                snapshot={"status": "running"},
                rtl_closure_run_id="rtl-terminal-audit-stale",
                active_lane_run_id="lane-terminal-audit-stale",
            )
            completed = store.update_workflow(
                running["workflow_id"],
                status="completed",
                phase="completed",
                snapshot={"status": "completed"},
            )
            stale_audit_id = "pi-audit-stale"

            def runner(command, **_kwargs):
                if command[1] == "reconcile":
                    payload = {
                        "run_id": "rtl-terminal-audit-stale",
                        "status": "completed",
                        "last_integrity_audit_id": stale_audit_id,
                    }
                else:
                    payload = backend_report(
                        completed,
                        "rtl-terminal-audit-stale",
                        status="completed",
                        lane_run_id="lane-terminal-audit-stale",
                        phase="completed",
                        process_status="completed",
                        flow_scope="full",
                        endpoint_reached=True,
                        delivery_gate="pass",
                        last_integrity_audit_id=stale_audit_id,
                    )
                return subprocess.CompletedProcess(
                    command, 0, stdout=json.dumps(payload), stderr=""
                )

            service = ProductionOrchestratorService(
                store, command_runner=runner
            )
            with self.assertRaises(OrchestratorError) as raised:
                service.reconcile_one(completed)

            self.assertEqual(raised.exception.code, "BACKEND_AUDIT_STALE")
            self.assertTrue(raised.exception.retryable)
            current = store.get_workflow(completed["workflow_id"])
            self.assertEqual(current["status"], "completed")
            self.assertEqual(current["revision"], completed["revision"])

    def test_requested_integrity_audit_requires_all_evidence_fields(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            store = OrchestratorStore(root / "state.sqlite3")
            workflow, _ = store.create_workflow(
                write_spec(root), idempotency_key="missing-integrity-evidence"
            )
            report = backend_report(
                workflow,
                "rtl-missing-integrity-evidence",
                status="incomplete",
                failure_class="release_integrity",
                failure_gate="lane_delivery_attestation",
            )

            with self.assertRaises(OrchestratorError) as raised:
                _validate_rtl_closure_report(
                    report,
                    expected_run_id="rtl-missing-integrity-evidence",
                    workflow=workflow,
                    expected_integrity_audit_id="pi-audit-required",
                )

        self.assertEqual(
            raised.exception.code, "BACKEND_RELEASE_INTEGRITY_INVALID"
        )

    def test_degraded_integrity_audit_may_explicitly_have_no_attestation(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            store = OrchestratorStore(root / "state.sqlite3")
            workflow, _ = store.create_workflow(
                write_spec(root), idempotency_key="degraded-without-attestation"
            )
            audit_id = "pi-audit-degraded-without-attestation"
            report = backend_report(
                workflow,
                "rtl-degraded-without-attestation",
                status="incomplete",
                failure_class="release_integrity",
                failure_gate="lane_delivery_attestation",
                release_integrity={
                    "schema_version": 1,
                    "audit_id": audit_id,
                    "status": "degraded",
                    "checked_at": "2026-08-03T00:00:00+08:00",
                    "attestation_sha256": None,
                    "errors": ["sealed delivery attestation is unavailable"],
                },
                delivery_attestation_sha256=None,
                last_integrity_audit_id=audit_id,
            )

            _validate_rtl_closure_report(
                report,
                expected_run_id="rtl-degraded-without-attestation",
                workflow=workflow,
                expected_integrity_audit_id=audit_id,
            )

    def test_failed_report_rejects_degraded_null_attestation(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            store = OrchestratorStore(root / "state.sqlite3")
            workflow, _ = store.create_workflow(
                write_spec(root), idempotency_key="failed-null-attestation"
            )
            audit_id = "pi-audit-failed-null-attestation"
            report = backend_report(
                workflow,
                "rtl-failed-null-attestation",
                status="failed",
                failure_class="release_integrity",
                failure_gate="lane_delivery_attestation",
                release_integrity={
                    "schema_version": 1,
                    "audit_id": audit_id,
                    "status": "degraded",
                    "checked_at": "2026-08-03T00:00:00+08:00",
                    "attestation_sha256": None,
                    "errors": ["sealed delivery attestation is unavailable"],
                },
                delivery_attestation_sha256=None,
                last_integrity_audit_id=audit_id,
            )

            with self.assertRaises(OrchestratorError) as raised:
                _validate_rtl_closure_report(
                    report,
                    expected_run_id="rtl-failed-null-attestation",
                    workflow=workflow,
                    expected_integrity_audit_id=audit_id,
                )

        self.assertEqual(
            raised.exception.code, "BACKEND_RELEASE_INTEGRITY_INVALID"
        )

    def test_integrity_error_list_and_status_semantics_are_enforced(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            store = OrchestratorStore(root / "state.sqlite3")
            workflow, _ = store.create_workflow(
                write_spec(root), idempotency_key="integrity-error-semantics"
            )
            audit_id = "pi-audit-error-semantics"
            base = backend_report(
                workflow,
                "rtl-integrity-error-semantics",
                status="incomplete",
                failure_class="release_integrity",
                failure_gate="lane_delivery_attestation",
                delivery_attestation_sha256="a" * 64,
                last_integrity_audit_id=audit_id,
            )
            invalid_cases = (
                ("verified", ["unexpected error"]),
                ("unknown", []),
                ("degraded", []),
                ("unknown", [""]),
                ("unknown", ["error"] * 17),
            )
            for integrity_status, errors in invalid_cases:
                with self.subTest(status=integrity_status, errors=len(errors)):
                    report = {
                        **base,
                        "release_integrity": {
                            "schema_version": 1,
                            "audit_id": audit_id,
                            "status": integrity_status,
                            "checked_at": "2026-08-03T00:00:00+08:00",
                            "attestation_sha256": "a" * 64,
                            "errors": errors,
                        },
                    }
                    with self.assertRaises(OrchestratorError) as raised:
                        _validate_rtl_closure_report(
                            report,
                            expected_run_id="rtl-integrity-error-semantics",
                            workflow=workflow,
                            expected_integrity_audit_id=audit_id,
                        )
                    self.assertEqual(
                        raised.exception.code,
                        "BACKEND_RELEASE_INTEGRITY_INVALID",
                    )

    def test_completed_report_rejects_degraded_null_attestation(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            store = OrchestratorStore(root / "state.sqlite3")
            workflow, _ = store.create_workflow(
                write_spec(root), idempotency_key="completed-null-attestation"
            )
            audit_id = "pi-audit-completed-null-attestation"
            report = backend_report(
                workflow,
                "rtl-completed-null-attestation",
                status="completed",
                lane_run_id="lane-completed-null-attestation",
                process_status="completed",
                flow_scope="full",
                endpoint_reached=True,
                delivery_gate="pass",
                release_integrity={
                    "schema_version": 1,
                    "audit_id": audit_id,
                    "status": "degraded",
                    "checked_at": "2026-08-03T00:00:00+08:00",
                    "attestation_sha256": None,
                    "errors": ["delivery attestation is unavailable"],
                },
                delivery_attestation_sha256=None,
                last_integrity_audit_id=audit_id,
            )
            report["delivery_attestation_sha256"] = None

            with self.assertRaises(OrchestratorError) as raised:
                _validate_rtl_closure_report(
                    report,
                    expected_run_id="rtl-completed-null-attestation",
                    workflow=workflow,
                    expected_integrity_audit_id=audit_id,
                )

        self.assertEqual(
            raised.exception.code, "BACKEND_RELEASE_INTEGRITY_INVALID"
        )

    def test_terminal_audits_use_a_separate_queue_from_active_reconciliation(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            store = OrchestratorStore(root / "state.sqlite3")
            running, _ = store.create_workflow(
                write_spec(root, name="active"), idempotency_key="active-queue"
            )
            completed, _ = store.create_workflow(
                write_spec(root, name="terminal"), idempotency_key="audit-queue"
            )
            running = store.update_workflow(
                running["workflow_id"],
                status="running",
                phase="generating",
                snapshot={},
                rtl_closure_run_id="rtl-active-queue",
            )
            completed = store.update_workflow(
                completed["workflow_id"],
                status="completed",
                phase="completed",
                snapshot={"status": "completed"},
                rtl_closure_run_id="rtl-audit-queue",
            )
            self.assertEqual(
                [row["workflow_id"] for row in store.active_for_reconcile()],
                [running["workflow_id"]],
            )
            self.assertEqual(
                [row["workflow_id"] for row in store.terminal_audits_due()],
                [completed["workflow_id"]],
            )

    def test_retryable_terminal_audit_failure_remains_degraded_until_retry(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            store = OrchestratorStore(root / "state.sqlite3")
            workflow, _ = store.create_workflow(
                write_spec(root), idempotency_key="audit-health"
            )
            running = store.update_workflow(
                workflow["workflow_id"],
                status="running",
                phase="lane_running",
                snapshot={"status": "running"},
                rtl_closure_run_id="rtl-audit-health",
                active_lane_run_id="lane-audit-health",
            )
            completed = store.update_workflow(
                running["workflow_id"],
                status="completed",
                phase="completed",
                snapshot={"status": "completed"},
            )

            def runner(command, **_kwargs):
                raise subprocess.TimeoutExpired(command, 30)

            service = OrchestratorService(store, command_runner=runner)
            service.terminal_audit_once()
            first = service.terminal_audit_snapshot()
            self.assertEqual(first["status"], "degraded")
            self.assertEqual(first["last_error"]["code"], "BACKEND_TIMEOUT")
            self.assertEqual(
                store.get_workflow(completed["workflow_id"])["status"],
                "completed",
            )
            service.terminal_audit_once()
            second = service.terminal_audit_snapshot()
            self.assertEqual(second["status"], "degraded")
            self.assertEqual(second["last_error"], first["last_error"])

    def test_ensure_daemon_does_not_replace_an_unrecognized_live_socket(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            socket_path = root / "foreign.sock"
            listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            listener.bind(str(socket_path))
            listener.listen(1)
            try:
                with (
                    patch("pi_lane.orchestrator_cli._daemon_ping", return_value=None),
                    patch("pi_lane.orchestrator_cli.subprocess.Popen") as popen,
                    self.assertRaises(OrchestratorError) as occupied,
                ):
                    ensure_daemon(socket_path=socket_path, wait_seconds=1)
                self.assertEqual(occupied.exception.code, "DAEMON_SOCKET_IN_USE")
                popen.assert_not_called()
                self.assertTrue(socket_path.exists())
            finally:
                listener.close()

    def test_ensure_daemon_reloads_an_old_build_and_passes_coherent_paths(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            socket_path = root / "custom.sock"
            old = {
                "ok": True,
                "data": {
                    "ready": True,
                    "build_id": "old",
                    "handoff_protocol_version": 1,
                    "db_path": str(root / "orchestrator.sqlite3"),
                    "socket_path": str(socket_path),
                    "lock_path": str(root / "orchestrator.lock"),
                },
            }
            current = {
                "ok": True,
                "data": {
                    "ready": True,
                    "build_id": ORCHESTRATOR_BUILD_ID,
                    "runtime_config_fingerprint": (
                        rtl_closure_runtime_config_fingerprint()
                    ),
                    "db_path": str(root / "orchestrator.sqlite3"),
                    "socket_path": str(socket_path),
                    "lock_path": str(root / "orchestrator.lock"),
                },
            }
            pings = iter((old, None, current))
            process = Mock()
            process.poll.return_value = None
            with (
                patch(
                    "pi_lane.orchestrator_cli._daemon_environment",
                    return_value=os.environ.copy(),
                ),
                patch(
                    "pi_lane.orchestrator_cli._daemon_ping",
                    side_effect=lambda _path: next(pings),
                ),
                patch(
                    "pi_lane.orchestrator_cli.request",
                    return_value={"ok": True, "data": {"stopping": True}},
                ) as stop_request,
                patch("pi_lane.orchestrator_cli.subprocess.Popen", return_value=process) as popen,
            ):
                result = ensure_daemon(socket_path=socket_path, wait_seconds=1)
            try:
                self.assertIs(result, current)
                stop_request.assert_called_once()
                command = popen.call_args.args[0]
                self.assertIn(str(root / "orchestrator.sqlite3"), command)
                self.assertIn(str(root / "orchestrator.lock"), command)
                self.assertTrue((root / "orchestrator.log").is_file())
            finally:
                _forget_daemon(process)

    def test_runtime_config_fingerprint_is_secret_free_and_key_sensitive(self):
        first_environment = {
            "RTL_CLOSURE_PROVIDER": "OpenAI",
            "RTL_CLOSURE_MODEL": "gpt-5.6-sol",
            "RTL_CLOSURE_BASE_URL": "https://example.test/v1/",
            "RTL_CLOSURE_API_KEY": "private-key-one",
        }
        second_environment = {
            **first_environment,
            "RTL_CLOSURE_API_KEY": "private-key-two",
        }
        first = rtl_closure_runtime_config_fingerprint(first_environment)
        second = rtl_closure_runtime_config_fingerprint(second_environment)
        self.assertRegex(first, r"^[0-9a-f]{64}$")
        self.assertNotEqual(first, second)
        with tempfile.TemporaryDirectory() as directory:
            service = ProductionOrchestratorService(
                OrchestratorStore(Path(directory) / "state.sqlite3"),
                runtime_config_fingerprint=first,
            )
            ping = service.dispatch("daemon.ping", {})
        self.assertEqual(ping["runtime_config_fingerprint"], first)
        self.assertNotIn("private-key-one", json.dumps(ping))

    def test_runtime_config_fingerprint_uses_rtl_closure_provider_default(self):
        implicit = rtl_closure_runtime_config_fingerprint({})
        explicit = rtl_closure_runtime_config_fingerprint(
            {"RTL_CLOSURE_PROVIDER": "openai_compatible"}
        )

        self.assertEqual(implicit, explicit)

    def test_daemon_environment_rehydrates_private_runtime_without_parent_leak(self):
        with tempfile.TemporaryDirectory() as directory:
            state = Path(directory) / "state"
            agent = state / "pi-lane" / "agent"
            agent.mkdir(mode=0o700, parents=True)
            private_files = {
                "models.json": {
                    "providers": {
                        "openai": {
                            "baseUrl": "https://models.example.test/v1",
                            "models": [{"id": "gpt-5.6-sol"}],
                        }
                    }
                },
                "settings.json": {
                    "defaultProvider": "openai",
                    "defaultModel": "gpt-5.6-sol",
                },
                "auth.json": {"openai": {"key": "private-runtime-key"}},
            }
            for name, payload in private_files.items():
                path = agent / name
                path.write_text(json.dumps(payload), encoding="utf-8")
                path.chmod(0o600)
            parent_environment = {"XDG_STATE_HOME": str(state)}
            with patch.dict(os.environ, parent_environment, clear=True):
                adapted = _daemon_environment()
                self.assertNotIn("RTL_CLOSURE_API_KEY", os.environ)

        self.assertEqual(adapted["RTL_CLOSURE_MODEL"], "gpt-5.6-sol")
        self.assertEqual(
            adapted["RTL_CLOSURE_BASE_URL"],
            "https://models.example.test/v1",
        )
        self.assertEqual(
            adapted["RTL_CLOSURE_API_KEY"], "private-runtime-key"
        )

    def test_runtime_config_secret_is_absent_from_daemon_output_and_log(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            socket_path = root / "orchestrator.sock"
            log_path = root / "orchestrator.log"
            api_key = "test-api-key-that-must-not-be-rendered"
            runtime_environment = {
                "RTL_CLOSURE_PROVIDER": "openai",
                "RTL_CLOSURE_MODEL": "gpt-5.6-sol",
                "RTL_CLOSURE_BASE_URL": "https://example.test/v1",
                "RTL_CLOSURE_API_KEY": api_key,
            }
            with patch.dict(os.environ, runtime_environment, clear=False):
                expected = rtl_closure_runtime_config_fingerprint()
                try:
                    ensured = ensure_daemon(
                        socket_path=socket_path,
                        log_path=log_path,
                        wait_seconds=5,
                    )
                    ping = request("daemon.ping", socket_path=socket_path, timeout=2)
                    rendered_output = json.dumps(
                        {"ensure": ensured, "ping": ping}, sort_keys=True
                    )
                    self.assertEqual(
                        ping["data"]["runtime_config_fingerprint"], expected
                    )
                    self.assertNotIn(api_key, rendered_output)
                finally:
                    if socket_path.exists():
                        request("daemon.stop", socket_path=socket_path, timeout=2)
            daemon_log = log_path.read_text(encoding="utf-8", errors="replace")
            self.assertNotIn(api_key, daemon_log)

    def test_ensure_daemon_reuses_same_runtime_config(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            socket_path = root / "orchestrator.sock"
            fingerprint = "a" * 64
            existing = {
                "ok": True,
                "data": {
                    "ready": True,
                    "build_id": ORCHESTRATOR_BUILD_ID,
                    "runtime_config_fingerprint": fingerprint,
                    "db_path": str(root / "orchestrator.sqlite3"),
                    "socket_path": str(socket_path),
                    "lock_path": str(root / "orchestrator.lock"),
                },
            }
            with (
                patch(
                    "pi_lane.orchestrator_cli.rtl_closure_runtime_config_fingerprint",
                    return_value=fingerprint,
                ),
                patch(
                    "pi_lane.orchestrator_cli._daemon_ping", return_value=existing
                ),
                patch("pi_lane.orchestrator_cli.request") as daemon_request,
                patch("pi_lane.orchestrator_cli.subprocess.Popen") as popen,
            ):
                result = ensure_daemon(socket_path=socket_path)
            self.assertIs(result, existing)
            daemon_request.assert_not_called()
            popen.assert_not_called()

    def test_ensure_daemon_reloads_same_build_when_runtime_config_changes(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            socket_path = root / "orchestrator.sock"
            paths = {
                "db_path": str(root / "orchestrator.sqlite3"),
                "socket_path": str(socket_path),
                "lock_path": str(root / "orchestrator.lock"),
            }
            old = {
                "ok": True,
                "data": {
                    "ready": True,
                    "build_id": ORCHESTRATOR_BUILD_ID,
                    "runtime_config_fingerprint": "a" * 64,
                    "handoff_protocol_version": 1,
                    **paths,
                },
            }
            current = {
                "ok": True,
                "data": {
                    "ready": True,
                    "build_id": ORCHESTRATOR_BUILD_ID,
                    "runtime_config_fingerprint": "b" * 64,
                    **paths,
                },
            }
            pings = iter((old, None, current))
            process = Mock()
            process.poll.return_value = None
            with (
                patch(
                    "pi_lane.orchestrator_cli.rtl_closure_runtime_config_fingerprint",
                    return_value="b" * 64,
                ),
                patch(
                    "pi_lane.orchestrator_cli._daemon_ping",
                    side_effect=lambda _path: next(pings),
                ),
                patch(
                    "pi_lane.orchestrator_cli.request",
                    return_value={"ok": True, "data": {"stopping": True}},
                ) as stop_request,
                patch(
                    "pi_lane.orchestrator_cli.subprocess.Popen",
                    return_value=process,
                ) as popen,
            ):
                result = ensure_daemon(socket_path=socket_path, wait_seconds=1)
            try:
                self.assertIs(result, current)
                stop_request.assert_called_once()
                popen.assert_called_once()
                self.assertIn("env", popen.call_args.kwargs)
            finally:
                _forget_daemon(process)

    def test_ensure_daemon_defers_reload_while_mutations_are_active(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            socket_path = root / "custom.sock"
            existing = {
                "ok": True,
                "data": {
                    "ready": True,
                    "build_id": "older-build",
                    "handoff_protocol_version": 1,
                    "db_path": str(root / "orchestrator.sqlite3"),
                    "socket_path": str(socket_path),
                    "lock_path": str(root / "orchestrator.lock"),
                },
            }
            with (
                patch(
                    "pi_lane.orchestrator_cli._daemon_ping",
                    return_value=existing,
                ),
                patch(
                    "pi_lane.orchestrator_cli.request",
                    return_value={
                        "ok": True,
                        "data": {
                            "stopping": False,
                            "busy": True,
                            "active_mutations": 1,
                        },
                    },
                ) as stop_request,
                patch("pi_lane.orchestrator_cli.subprocess.Popen") as popen,
            ):
                result = ensure_daemon(socket_path=socket_path, wait_seconds=1)
            self.assertTrue(result["data"]["reload_deferred"])
            self.assertIn("active mutations", result["data"]["reload_deferred_reason"])
            stop_request.assert_called_once()
            popen.assert_not_called()

    def test_daemon_stop_is_drain_aware_and_request_threads_are_joinable(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            service = OrchestratorService(
                OrchestratorStore(root / "state.sqlite3")
            )
            service.begin_mutation("run.start")
            busy = service.dispatch("daemon.stop", {})
            self.assertTrue(busy["busy"])
            self.assertFalse(busy["stopping"])
            self.assertFalse(service.stopping.is_set())
            service.end_mutation("run.start")
            stopped = service.dispatch("daemon.stop", {})
            self.assertTrue(stopped["stopping"])
            self.assertFalse(stopped["busy"])

            from pi_lane.orchestrator_server import UnixServer

            self.assertFalse(UnixServer.daemon_threads)

    def test_ensure_daemon_rejects_public_custom_parent_without_chmod(self):
        with tempfile.TemporaryDirectory() as directory:
            public = Path(directory) / "project"
            public.mkdir(mode=0o775)
            os.chmod(public, 0o775)
            with (
                patch("pi_lane.orchestrator_cli._daemon_ping", return_value=None),
                patch("pi_lane.orchestrator_cli.subprocess.Popen") as popen,
                self.assertRaises(OrchestratorError) as unsafe,
            ):
                ensure_daemon(socket_path=public / "orchestrator.sock")
            self.assertEqual(unsafe.exception.code, "INSECURE_STATE_DIRECTORY")
            self.assertEqual(public.stat().st_mode & 0o777, 0o775)
            popen.assert_not_called()

    def test_unexpected_reconcile_error_is_visible_and_rate_limited(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            store = OrchestratorStore(root / "state.sqlite3")
            workflow, _ = store.create_workflow(write_spec(root), idempotency_key="key")
            service = OrchestratorService(store)
            with self.assertLogs("pi_lane.orchestrator_server", level="ERROR"):
                service._observer_failure(RuntimeError("unexpected"), workflow)
                service._observer_failure(RuntimeError("unexpected"), workflow)
            health = service.dispatch("daemon.ping", {})["observer"]
            self.assertEqual(health["status"], "degraded")
            self.assertEqual(
                health["last_error"]["code"], "ORCHESTRATOR_RECONCILE_CRASH"
            )
            observer_events = [
                event
                for event in store.events(workflow["workflow_id"])["events"]
                if event["type"] == "observer.crashed"
            ]
            self.assertEqual(len(observer_events), 1)

    def test_concurrent_custom_daemon_ensure_uses_one_runtime_directory(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            socket_path = root / "custom.sock"
            barrier = threading.Barrier(2)
            outcomes: list[dict] = []
            errors: list[BaseException] = []

            def launch() -> None:
                barrier.wait()
                try:
                    outcomes.append(
                        ensure_daemon(socket_path=socket_path, wait_seconds=5)
                    )
                except BaseException as exc:
                    errors.append(exc)

            threads = [threading.Thread(target=launch) for _ in range(2)]
            for thread in threads:
                thread.start()
            for thread in threads:
                thread.join()
            try:
                self.assertEqual(errors, [])
                self.assertEqual(len(outcomes), 2)
                self.assertTrue((root / "orchestrator.sqlite3").is_file())
                self.assertTrue((root / "orchestrator.lock").is_file())
                self.assertTrue((root / "orchestrator.log").is_file())
                ping = request("daemon.ping", socket_path=socket_path, timeout=2)
                self.assertEqual(ping["data"]["build_id"], ORCHESTRATOR_BUILD_ID)
                self.assertIn("observer", ping["data"])
            finally:
                if socket_path.exists():
                    request("daemon.stop", socket_path=socket_path, timeout=2)

    def test_ensure_daemon_reloads_when_the_requested_database_changes(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            socket_path = root / "orchestrator.sock"
            first_database = root / "first.sqlite3"
            second_database = root / "second.sqlite3"
            try:
                first = ensure_daemon(
                    socket_path=socket_path,
                    db_path=first_database,
                    wait_seconds=5,
                )
                self.assertEqual(first["data"]["db_path"], str(first_database))

                second = ensure_daemon(
                    socket_path=socket_path,
                    db_path=second_database,
                    wait_seconds=5,
                )

                self.assertEqual(second["data"]["db_path"], str(second_database))
                self.assertEqual(second["data"]["socket_path"], str(socket_path))
                self.assertTrue(second_database.is_file())
            finally:
                if socket_path.exists():
                    request("daemon.stop", socket_path=socket_path, timeout=2)

    def test_mutations_require_explicit_valid_workflow_id_and_booleans_are_strict(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            store = OrchestratorStore(root / "state.sqlite3")
            workflow, _ = store.create_workflow(write_spec(root), idempotency_key="key")
            commands: list[list[str]] = []

            def runner(command, **_kwargs):
                commands.append(command)
                raise AssertionError("backend must not be called")

            service = OrchestratorService(store, command_runner=runner)
            for params in ({}, {"workflow_id": ""}, {"workflow_id": False}):
                with self.assertRaises(OrchestratorError):
                    service.stop_workflow(params)
            with self.assertRaises(OrchestratorError) as invalid_read:
                service.dispatch("run.get", {"workflow_id": 123})
            self.assertEqual(invalid_read.exception.code, "INVALID_WORKFLOW_ID")
            with self.assertRaises(OrchestratorError):
                service.dispatch("run.list", {"active_only": "false"})
            with self.assertRaises(OrchestratorError):
                service.event_report(
                    {"workflow_id": workflow["workflow_id"], "acknowledge": "false"}
                )
            self.assertEqual(commands, [])

    def test_source_change_during_validate_cannot_change_started_spec(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            store = OrchestratorStore(root / "state.sqlite3")
            source = write_spec(root, name="stable")
            original = source.read_bytes()
            seen: list[tuple[str, bytes]] = []

            def runner(command, **_kwargs):
                if command[1] == "validate":
                    path = Path(command[command.index("--spec") + 1])
                    seen.append(("validate", path.read_bytes()))
                    source.write_text(
                        json.dumps(
                            {"schema_version": 2, "design": {"name": "changed", "pdk": "sky130A"}}
                        )
                    )
                    return subprocess.CompletedProcess(command, 0, stdout='{"valid":true}', stderr="")
                if command[1] == "start":
                    path = Path(command[command.index("--spec") + 1])
                    seen.append(("start", path.read_bytes()))
                    return subprocess.CompletedProcess(
                        command,
                        0,
                        stdout='{"run_id":"rtl-stable","status":"running"}',
                        stderr="",
                    )
                raise AssertionError(command)

            workflow = OrchestratorService(store, command_runner=runner).start_workflow(
                {"spec": str(source)}
            )
            self.assertEqual(seen, [("validate", original), ("start", original)])
            self.assertNotEqual(Path(workflow["spec_path"]), source)

    def test_snapshot_tampering_between_validate_and_start_fails_closed(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            store = OrchestratorStore(root / "state.sqlite3")
            source = write_spec(root)
            starts = 0
            validations = 0

            def runner(command, **_kwargs):
                nonlocal starts, validations
                if command[1] == "validate":
                    validations += 1
                    snapshot = Path(command[command.index("--spec") + 1])
                    snapshot.chmod(0o600)
                    snapshot.write_text(
                        '{"schema_version":2,"design":{"name":"other","pdk":"sky130A"}}'
                    )
                    return subprocess.CompletedProcess(command, 0, stdout='{"valid":true}', stderr="")
                starts += 1
                raise AssertionError("start must not consume a changed snapshot")

            service = OrchestratorService(store, command_runner=runner)
            workflow = service.start_workflow({"spec": str(source)})
            replay = service.start_workflow({"spec": str(source)})
            self.assertEqual(starts, 0)
            self.assertEqual(validations, 1)
            self.assertEqual(workflow["status"], "waiting_user")
            self.assertEqual(workflow["failure"]["code"], "SPEC_SNAPSHOT_CHANGED")
            self.assertEqual(replay["workflow_id"], workflow["workflow_id"])
            self.assertEqual(replay["operations"][0]["status"], "failed")

    def test_start_without_key_is_idempotent_by_spec_hash(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            store = OrchestratorStore(root / "state.sqlite3")
            spec = write_spec(root)
            commands: list[list[str]] = []

            def runner(command, **_kwargs):
                commands.append(command)
                if command[1] == "validate":
                    return subprocess.CompletedProcess(
                        command, 0, stdout='{"valid":true}', stderr=""
                    )
                if command[1] == "start":
                    return subprocess.CompletedProcess(
                        command,
                        0,
                        stdout='{"run_id":"rtl-default-key","status":"running","phase":"queued"}',
                        stderr="",
                    )
                raise AssertionError(command)

            service = OrchestratorService(store, command_runner=runner)
            first = service.start_workflow({"spec": str(spec)})
            second = service.start_workflow({"spec": str(spec)})
            self.assertEqual(second["workflow_id"], first["workflow_id"])
            self.assertEqual(len(store.list_workflows()), 1)
            self.assertEqual(sum(command[1] == "start" for command in commands), 1)

    def test_stop_cannot_cancel_a_workflow_while_initial_start_holds_its_lease(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            store = OrchestratorStore(root / "state.sqlite3")
            spec = write_spec(root)
            validation_started = threading.Event()
            release_validation = threading.Event()
            starts: list[list[str]] = []
            results: list[dict[str, Any]] = []
            errors: list[BaseException] = []

            def runner(command, **_kwargs):
                if command[1] == "validate":
                    validation_started.set()
                    if not release_validation.wait(5):
                        raise AssertionError("test did not release validation")
                    payload = '{"valid":true}'
                elif command[1] == "start":
                    starts.append(command)
                    payload = '{"run_id":"rtl-race","status":"running"}'
                else:
                    raise AssertionError(command)
                return subprocess.CompletedProcess(
                    command, 0, stdout=payload, stderr=""
                )

            service = OrchestratorService(store, command_runner=runner)

            def launch() -> None:
                try:
                    results.append(
                        service.start_workflow(
                            {"spec": str(spec), "idempotency_key": "start-stop-race"}
                        )
                    )
                except BaseException as exc:
                    errors.append(exc)

            thread = threading.Thread(target=launch)
            thread.start()
            self.assertTrue(validation_started.wait(2))
            workflow_id = store.list_workflows()[0]["workflow_id"]
            try:
                with self.assertRaises(OrchestratorError) as blocked:
                    service.stop_workflow({"workflow_id": workflow_id})
                self.assertEqual(blocked.exception.code, "OPERATION_IN_PROGRESS")
            finally:
                release_validation.set()
            thread.join(5)

            self.assertFalse(thread.is_alive())
            self.assertEqual(errors, [])
            self.assertEqual(len(starts), 1)
            self.assertEqual(results[0]["rtl_closure_run_id"], "rtl-race")
            self.assertEqual(store.get_workflow(workflow_id)["status"], "running")

    def test_start_rejects_missing_and_symlinked_spec_paths_structurally(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            service = OrchestratorService(OrchestratorStore(root / "state.sqlite3"))
            with self.assertRaises(OrchestratorError) as missing:
                service.start_workflow({"spec": str(root / "missing.json")})
            self.assertEqual(missing.exception.code, "INVALID_SPEC_PATH")

            spec = write_spec(root)
            alias = root / "alias.json"
            alias.symlink_to(spec)
            with self.assertRaises(OrchestratorError) as symlinked:
                service.start_workflow({"spec": str(alias)})
            self.assertEqual(symlinked.exception.code, "INVALID_SPEC_PATH")

    def test_daemon_serves_strict_json_over_unix_socket(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            socket_path = root / "orchestrator.sock"
            command = [
                sys.executable,
                "-m",
                "pi_lane.orchestrator_server",
                "--db",
                str(root / "state.sqlite3"),
                "--socket",
                str(socket_path),
                "--lock",
                str(root / "orchestrator.lock"),
            ]
            process = subprocess.Popen(
                command,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.STDOUT,
                text=True,
                env=dict(os.environ),
            )
            try:
                deadline = time.monotonic() + 5
                while not socket_path.exists() and time.monotonic() < deadline:
                    if process.poll() is not None:
                        break
                    time.sleep(0.02)
                self.assertIsNone(process.poll())
                ping = request("daemon.ping", socket_path=socket_path, timeout=2)
                self.assertTrue(ping["ok"])
                self.assertEqual(ping["protocol_version"], PROTOCOL_VERSION)
                self.assertEqual(
                    ping["data"]["protocol_version"], PROTOCOL_VERSION
                )
                self.assertEqual(ping["data"]["db_path"], str(root / "state.sqlite3"))
                self.assertEqual(ping["data"]["socket_path"], str(socket_path))
                self.assertEqual(
                    ping["data"]["lock_path"], str(root / "orchestrator.lock")
                )

                unknown = request("not.a.method", socket_path=socket_path, timeout=2)
                self.assertFalse(unknown["ok"])
                self.assertEqual(unknown["error"]["code"], "METHOD_NOT_FOUND")
                stopped = request("daemon.stop", socket_path=socket_path, timeout=2)
                self.assertTrue(stopped["ok"])
                process.wait(timeout=5)
                self.assertEqual(process.returncode, 0)
            finally:
                if process.poll() is None:
                    process.terminate()
                    process.wait(timeout=5)

    def test_start_and_reconcile_preserve_lane_attempts(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            store = OrchestratorStore(root / "state.sqlite3")
            spec = write_spec(root)
            reports = [
                {
                    "run_id": "rtl-1",
                    "status": "running",
                    "phase": "generating",
                    "design": "demo",
                    "pdk": "sky130A",
                },
                {
                    "run_id": "rtl-1",
                    "status": "running",
                    "phase": "lane_running",
                    "component": "librelane",
                    "lane_run_id": "lane-1",
                    "design_config": "/tmp/design/config.yaml",
                },
            ]
            commands: list[list[str]] = []

            def runner(command, **_kwargs):
                commands.append(command)
                if command[1] == "validate":
                    return subprocess.CompletedProcess(command, 0, stdout='{"valid":true}', stderr="")
                if command[1] == "start":
                    return subprocess.CompletedProcess(command, 0, stdout=json.dumps(reports[0]), stderr="")
                return subprocess.CompletedProcess(command, 0, stdout=json.dumps(reports[1]), stderr="")

            service = OrchestratorService(store, command_runner=runner)
            workflow = service.start_workflow(
                {"spec": str(spec), "idempotency_key": "request-1"}
            )
            self.assertEqual(workflow["rtl_closure_run_id"], "rtl-1")
            start_command = next(command for command in commands if command[1] == "start")
            self.assertEqual(start_command[0], "rtl-closure")
            self.assertIn("--workflow-id", start_command)
            self.assertIn(workflow["workflow_id"], start_command)
            self.assertIn("--idempotency-key", start_command)
            reconciled = service.reconcile_one(store.get_workflow(workflow["workflow_id"]))
            self.assertEqual(reconciled["active_lane_run_id"], "lane-1")
            self.assertEqual(
                [command[1] for command in commands[-2:]], ["reconcile", "report"]
            )
            lane_attempts = [row for row in reconciled["attempts"] if row["kind"] == "lane"]
            self.assertEqual(len(lane_attempts), 1)

    def test_reconcile_rejects_a_report_without_the_bound_run_id(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            store = OrchestratorStore(root / "state.sqlite3")
            start_commands: list[list[str]] = []

            def runner(command, **_kwargs):
                if command[1] == "validate":
                    payload = '{"valid":true}'
                elif command[1] == "start":
                    start_commands.append(command)
                    payload = '{"run_id":"rtl-strict","status":"running"}'
                elif command[1] == "reconcile":
                    payload = '{"run_id":"rtl-strict","status":"running"}'
                else:
                    report = start_report(
                        start_commands[0],
                        "rtl-strict",
                        status="running",
                        phase="generating",
                    )
                    report.pop("run_id")
                    payload = json.dumps(report)
                return subprocess.CompletedProcess(
                    command, 0, stdout=payload, stderr=""
                )

            service = OrchestratorService(store, command_runner=runner)
            started = service.start_workflow({"spec": str(write_spec(root))})
            failed = service.reconcile_one(started)

            self.assertEqual(failed["status"], "failed")
            self.assertEqual(failed["phase"], "backend_protocol_invalid")
            self.assertEqual(failed["failure"]["code"], "INVALID_BACKEND_JSON")
            self.assertEqual(failed["failure"]["domain"], "orchestration")
            self.assertFalse(failed["failure"]["retryable"])
            self.assertTrue(
                all(attempt["status"] == "failed" for attempt in failed["attempts"])
            )

    def test_reconcile_rejects_a_contradictory_completed_report(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            store = OrchestratorStore(root / "state.sqlite3")

            def runner(command, **_kwargs):
                if command[1] == "validate":
                    report = {"valid": True}
                elif command[1] == "start":
                    report = {"run_id": "rtl-contract", "status": "running"}
                elif command[1] == "reconcile":
                    report = {"run_id": "rtl-contract", "status": "running"}
                else:
                    report = {
                        "run_id": "rtl-contract",
                        "lane_run_id": "lane-contract",
                        "status": "completed",
                        "process_status": "completed",
                        "flow_scope": "full",
                        "endpoint_reached": True,
                        "delivery_gate": "fail",
                    }
                return subprocess.CompletedProcess(
                    command, 0, stdout=json.dumps(report), stderr=""
                )

            service = OrchestratorService(store, command_runner=runner)
            started = service.start_workflow({"spec": str(write_spec(root))})
            failed = service.reconcile_one(started)

            self.assertEqual(failed["status"], "failed")
            self.assertEqual(
                failed["failure"]["code"], "BACKEND_TERMINAL_CONTRADICTION"
            )
            self.assertEqual(failed["failure"]["domain"], "orchestration")
            self.assertFalse(failed["failure"]["retryable"])
            self.assertIsNone(failed["active_lane_run_id"])

    def test_reconcile_accepts_only_a_complete_delivery_contract(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            store = OrchestratorStore(root / "state.sqlite3")

            def runner(command, **_kwargs):
                if command[1] == "validate":
                    report = {"valid": True}
                elif command[1] == "start":
                    report = {"run_id": "rtl-complete", "status": "running"}
                elif command[1] == "reconcile":
                    report = {"run_id": "rtl-complete", "status": "running"}
                else:
                    report = {
                        "run_id": "rtl-complete",
                        "lane_run_id": "lane-complete",
                        "status": "completed",
                        "phase": "completed",
                        "process_status": "completed",
                        "flow_scope": "full",
                        "endpoint_reached": True,
                        "delivery_gate": "pass",
                    }
                return subprocess.CompletedProcess(
                    command, 0, stdout=json.dumps(report), stderr=""
                )

            service = OrchestratorService(store, command_runner=runner)
            started = service.start_workflow({"spec": str(write_spec(root))})
            completed = service.reconcile_one(started)

            self.assertEqual(completed["status"], "completed")
            self.assertEqual(completed["active_lane_run_id"], "lane-complete")
            self.assertTrue(
                all(
                    attempt["status"] == "completed"
                    for attempt in completed["attempts"]
                )
            )

    def test_reconcile_timeout_remains_active_backs_off_and_recovers(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            store = OrchestratorStore(root / "state.sqlite3")
            mode = "timeout"
            reconcile_calls = 0

            def runner(command, **_kwargs):
                nonlocal reconcile_calls
                if command[1] == "validate":
                    report = {"valid": True}
                elif command[1] == "start":
                    report = {"run_id": "rtl-timeout", "status": "running"}
                elif command[1] == "reconcile":
                    reconcile_calls += 1
                    if mode == "timeout":
                        raise subprocess.TimeoutExpired(command, 30)
                    report = {"run_id": "rtl-timeout", "status": "running"}
                else:
                    report = {
                        "run_id": "rtl-timeout",
                        "lane_run_id": "lane-timeout",
                        "status": "completed",
                        "phase": "completed",
                        "process_status": "completed",
                        "flow_scope": "full",
                        "endpoint_reached": True,
                        "delivery_gate": "pass",
                    }
                return subprocess.CompletedProcess(
                    command, 0, stdout=json.dumps(report), stderr=""
                )

            service = OrchestratorService(store, command_runner=runner)
            started = service.start_workflow({"spec": str(write_spec(root))})
            first_failure = service.reconcile_one(started)
            first_observer = first_failure["snapshot"]["orchestrator_observer"]

            self.assertEqual(first_failure["status"], "running")
            self.assertTrue(first_failure["failure"]["retryable"])
            self.assertEqual(first_observer["consecutive_failures"], 1)
            self.assertEqual(first_observer["backoff_seconds"], 5.0)
            self.assertIn(
                first_failure["workflow_id"],
                [row["workflow_id"] for row in store.active_for_reconcile()],
            )

            skipped = service.reconcile_one(first_failure)
            self.assertEqual(reconcile_calls, 1)
            self.assertEqual(skipped["revision"], first_failure["revision"])

            with patch(
                "pi_lane.orchestrator_server.time.time",
                return_value=first_observer["next_reconcile_epoch"] + 1,
            ):
                second_failure = service.reconcile_one(first_failure)
            second_observer = second_failure["snapshot"]["orchestrator_observer"]
            self.assertEqual(reconcile_calls, 2)
            self.assertEqual(second_observer["consecutive_failures"], 2)
            self.assertEqual(second_observer["backoff_seconds"], 10.0)

            mode = "complete"
            with (
                patch(
                    "pi_lane.orchestrator_server.time.time",
                    return_value=second_observer["next_reconcile_epoch"] + 1,
                ),
                patch.object(service.stopping, "wait", side_effect=[False, True]),
            ):
                service.reconcile_loop()

            completed = store.get_workflow(started["workflow_id"])
            self.assertEqual(completed["status"], "completed")
            self.assertIsNone(completed["failure"])
            self.assertEqual(
                completed["snapshot"]["orchestrator_observer"]["status"],
                "healthy",
            )
            self.assertEqual(service.observer_snapshot()["status"], "healthy")

    def test_start_binding_failure_reuses_backend_identity_without_an_orphan(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            store = OrchestratorStore(root / "state.sqlite3")
            spec = write_spec(root)
            start_keys: list[str] = []

            def runner(command, **_kwargs):
                if command[1] == "validate":
                    report = {"valid": True}
                elif command[1] == "start":
                    start_keys.append(
                        command[command.index("--idempotency-key") + 1]
                    )
                    report = {"run_id": "rtl-atomic", "status": "running"}
                else:
                    raise AssertionError(command)
                return subprocess.CompletedProcess(
                    command, 0, stdout=json.dumps(report), stderr=""
                )

            service = OrchestratorService(store, command_runner=runner)
            original_bind = store.bind_attempt_and_update_workflow
            bind_calls = 0

            def flaky_bind(*args, **kwargs):
                nonlocal bind_calls
                bind_calls += 1
                if bind_calls == 1:
                    raise sqlite3.OperationalError("injected commit failure")
                return original_bind(*args, **kwargs)

            with patch.object(
                store, "bind_attempt_and_update_workflow", side_effect=flaky_bind
            ):
                with self.assertRaisesRegex(sqlite3.OperationalError, "injected"):
                    service.start_workflow(
                        {"spec": str(spec), "idempotency_key": "atomic-start"}
                    )
                unbound = store.list_workflows()[0]
                self.assertIsNone(unbound["rtl_closure_run_id"])
                self.assertEqual(
                    store.get_workflow(unbound["workflow_id"])["attempts"], []
                )
                recovered = service.start_workflow(
                    {"spec": str(spec), "idempotency_key": "atomic-start"}
                )

            self.assertEqual(start_keys[0], start_keys[1])
            self.assertEqual(recovered["rtl_closure_run_id"], "rtl-atomic")
            self.assertEqual(len(recovered["attempts"]), 1)
            self.assertEqual(len(store.list_workflows()), 1)

    def test_terminal_child_closes_attempts_even_when_workflow_waits_for_user(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            store = OrchestratorStore(root / "state.sqlite3")
            spec = write_spec(root)

            def runner(command, **_kwargs):
                if command[1] == "validate":
                    return subprocess.CompletedProcess(command, 0, stdout='{"valid":true}', stderr="")
                if command[1] == "start":
                    return subprocess.CompletedProcess(
                        command,
                        0,
                        stdout='{"run_id":"rtl-failed","status":"running"}',
                        stderr="",
                    )
                if command[1] == "reconcile":
                    return subprocess.CompletedProcess(
                        command, 1, stdout='{"run_id":"rtl-failed","status":"failed"}', stderr=""
                    )
                return subprocess.CompletedProcess(
                    command,
                    1,
                    stdout=json.dumps(
                        {
                            "run_id": "rtl-failed",
                            "lane_run_id": "lane-failed",
                            "status": "failed",
                            "phase": "failed",
                            "component": "librelane",
                            "error": "route failed",
                        }
                    ),
                    stderr="",
                )

            service = OrchestratorService(store, command_runner=runner)
            started = service.start_workflow({"spec": str(spec)})
            failed = service.reconcile_one(started)
            self.assertEqual(failed["status"], "waiting_user")
            self.assertTrue(failed["attempts"])
            statuses = {attempt["kind"]: attempt["status"] for attempt in failed["attempts"]}
            self.assertEqual(statuses, {"lane": "failed", "rtl_closure": "running"})
            self.assertEqual(store.active_for_reconcile(), [])

    def test_retry_preflight_rejects_terminal_before_backend_side_effect(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            store = OrchestratorStore(root / "state.sqlite3")
            workflow, _ = store.create_workflow(write_spec(root), idempotency_key="key")
            store.update_workflow(
                workflow["workflow_id"],
                status="failed",
                phase="failed",
                snapshot={},
                rtl_closure_run_id="rtl-terminal",
            )
            commands: list[list[str]] = []

            def runner(command, **_kwargs):
                commands.append(command)
                raise AssertionError("backend must not be called")

            service = OrchestratorService(store, command_runner=runner)
            with self.assertRaises(OrchestratorError) as rejected:
                service.retry_workflow(
                    {"workflow_id": workflow["workflow_id"], "scope": "lane"}
                )
            self.assertEqual(rejected.exception.code, "TERMINAL_STATE")
            self.assertEqual(commands, [])
            self.assertEqual(
                store.get_workflow(workflow["workflow_id"])["attempts"], []
            )

    def test_lane_retry_requires_typed_failure_then_records_one_attempt(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            store = OrchestratorStore(root / "state.sqlite3")
            workflow, _ = store.create_workflow(write_spec(root), idempotency_key="key")
            failure = structured_failure(
                code="PHYSICAL_FLOW_FAILED",
                domain="physical_tool",
                message="route failed",
                retryable=True,
                repairable_by_rtl=False,
                user_action_required=True,
            )
            waiting = store.update_workflow(
                workflow["workflow_id"],
                status="waiting_user",
                phase="failed",
                snapshot={},
                rtl_closure_run_id="rtl-1",
                failure=failure,
            )

            def runner(command, **_kwargs):
                self.assertEqual(command[0], "rtl-closure")
                self.assertEqual(command[1], "retry-lane")
                self.assertIn("--idempotency-key", command)
                return subprocess.CompletedProcess(
                    command,
                    0,
                    stdout='{"run_id":"rtl-1","lane_run_id":"lane-2","status":"running"}',
                    stderr="",
                )

            updated = OrchestratorService(store, command_runner=runner).retry_workflow(
                {"workflow_id": waiting["workflow_id"], "scope": "lane"}
            )
            self.assertEqual(updated["status"], "running")
            self.assertEqual(updated["active_lane_run_id"], "lane-2")
            self.assertEqual(len(updated["attempts"]), 1)

    def test_physical_incomplete_retry_closes_complete_attempt_lineage(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            store = OrchestratorStore(root / "state.sqlite3")
            spec = write_spec(root, name="physical_incomplete")
            report_mode = "incomplete"

            def runner(command, **_kwargs):
                action = command[1]
                if action == "validate":
                    payload = {"valid": True}
                elif action == "start":
                    payload = {"run_id": "rtl-physical-retry", "status": "running"}
                elif action == "reconcile":
                    payload = {"run_id": "rtl-physical-retry", "status": "running"}
                elif action == "retry-lane":
                    payload = {
                        "run_id": "rtl-physical-retry",
                        "lane_run_id": "lane-physical-2",
                        "status": "running",
                        "phase": "lane_running",
                    }
                elif action == "report" and report_mode == "incomplete":
                    payload = {
                        "run_id": "rtl-physical-retry",
                        "lane_run_id": "lane-physical-1",
                        "status": "incomplete",
                        "phase": "delivery_incomplete",
                        "component": "librelane",
                        "error": "delivery gate did not complete",
                    }
                elif action == "report":
                    payload = {
                        "run_id": "rtl-physical-retry",
                        "lane_run_id": "lane-physical-2",
                        "status": "completed",
                        "phase": "completed",
                        "process_status": "completed",
                        "flow_scope": "full",
                        "endpoint_reached": True,
                        "delivery_gate": "pass",
                    }
                else:
                    raise AssertionError(command)
                return subprocess.CompletedProcess(
                    command, 0, stdout=json.dumps(payload), stderr=""
                )

            service = OrchestratorService(store, command_runner=runner)
            started = service.start_workflow({"spec": str(spec)})
            incomplete = service.reconcile_one(started)
            self.assertEqual(incomplete["status"], "waiting_user")
            self.assertEqual(incomplete["failure"]["code"], "DELIVERY_INCOMPLETE")
            self.assertTrue(incomplete["failure"]["retryable"])
            self.assertEqual(
                {
                    (attempt["kind"], attempt["ordinal"]): attempt["status"]
                    for attempt in incomplete["attempts"]
                },
                {("rtl_closure", 1): "running", ("lane", 1): "failed"},
            )

            retried = service.retry_workflow(
                {"workflow_id": incomplete["workflow_id"], "scope": "lane"}
            )
            self.assertEqual(retried["status"], "running")
            self.assertEqual(retried["active_lane_run_id"], "lane-physical-2")
            report_mode = "completed"
            completed = service.reconcile_one(retried)
            self.assertEqual(completed["status"], "completed")
            self.assertEqual(
                {
                    (attempt["kind"], attempt["ordinal"]): attempt["status"]
                    for attempt in completed["attempts"]
                },
                {
                    ("rtl_closure", 1): "completed",
                    ("lane", 1): "failed",
                    ("lane", 2): "completed",
                },
            )

    def test_lane_retry_binding_failure_reuses_the_same_idempotency_key(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            store = OrchestratorStore(root / "state.sqlite3")
            workflow, _ = store.create_workflow(
                write_spec(root), idempotency_key="retry-atomic"
            )
            failure = structured_failure(
                code="PHYSICAL_FLOW_FAILED",
                domain="physical_tool",
                message="route failed",
                retryable=True,
                repairable_by_rtl=False,
                user_action_required=True,
            )
            waiting = store.update_workflow(
                workflow["workflow_id"],
                status="waiting_user",
                phase="failed",
                snapshot={},
                rtl_closure_run_id="rtl-retry",
                active_lane_run_id="lane-old",
                failure=failure,
            )
            retry_keys: list[str] = []

            def runner(command, **_kwargs):
                retry_keys.append(
                    command[command.index("--idempotency-key") + 1]
                )
                return subprocess.CompletedProcess(
                    command,
                    0,
                    stdout=(
                        '{"run_id":"rtl-retry","lane_run_id":"lane-new",'
                        '"status":"running"}'
                    ),
                    stderr="",
                )

            service = OrchestratorService(store, command_runner=runner)
            original_bind = store.bind_attempt_and_update_workflow
            bind_calls = 0

            def flaky_bind(*args, **kwargs):
                nonlocal bind_calls
                bind_calls += 1
                if bind_calls == 1:
                    raise sqlite3.OperationalError("injected retry commit failure")
                return original_bind(*args, **kwargs)

            with patch.object(
                store, "bind_attempt_and_update_workflow", side_effect=flaky_bind
            ):
                with self.assertRaisesRegex(sqlite3.OperationalError, "injected"):
                    service.retry_workflow(
                        {"workflow_id": waiting["workflow_id"], "scope": "lane"}
                    )
                unbound = store.get_workflow(waiting["workflow_id"])
                self.assertEqual(unbound["status"], "waiting_user")
                self.assertEqual(unbound["active_lane_run_id"], "lane-old")
                self.assertEqual(unbound["attempts"], [])
                recovered = service.retry_workflow(
                    {"workflow_id": waiting["workflow_id"], "scope": "lane"}
                )

            self.assertEqual(retry_keys[0], retry_keys[1])
            self.assertTrue(retry_keys[0].startswith("pi-lane:retry:"))
            self.assertEqual(recovered["status"], "running")
            self.assertEqual(recovered["active_lane_run_id"], "lane-new")
            self.assertEqual(len(recovered["attempts"]), 1)

    def test_rerun_creates_successor_without_mutating_terminal_parent(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            store = OrchestratorStore(root / "state.sqlite3")
            parent, _ = store.create_workflow(write_spec(root), idempotency_key="parent")
            parent = store.update_workflow(
                parent["workflow_id"],
                status="completed",
                phase="completed",
                snapshot={"status": "completed"},
            )

            def runner(command, **_kwargs):
                if command[1] == "validate":
                    return subprocess.CompletedProcess(command, 0, stdout='{"valid":true}', stderr="")
                if command[1] == "start":
                    return subprocess.CompletedProcess(
                        command,
                        0,
                        stdout='{"run_id":"rtl-successor","status":"running"}',
                        stderr="",
                    )
                raise AssertionError(command)

            successor = OrchestratorService(store, command_runner=runner).rerun_workflow(
                {"workflow_id": parent["workflow_id"], "idempotency_key": "successor"}
            )
            self.assertNotEqual(successor["workflow_id"], parent["workflow_id"])
            self.assertEqual(successor["supersedes_workflow_id"], parent["workflow_id"])
            unchanged = store.get_workflow(parent["workflow_id"])
            self.assertEqual(unchanged["status"], "completed")
            self.assertEqual(unchanged["revision"], parent["revision"])

    def test_rerun_without_a_key_is_idempotent_for_one_terminal_revision(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            store = OrchestratorStore(root / "state.sqlite3")
            parent, _ = store.create_workflow(
                write_spec(root), idempotency_key="rerun-parent"
            )
            parent = store.update_workflow(
                parent["workflow_id"],
                status="completed",
                phase="completed",
                snapshot={"status": "completed"},
            )
            starts = 0

            def runner(command, **_kwargs):
                nonlocal starts
                if command[1] == "validate":
                    payload = '{"valid":true}'
                elif command[1] == "start":
                    starts += 1
                    payload = '{"run_id":"rtl-rerun","status":"running"}'
                else:
                    raise AssertionError(command)
                return subprocess.CompletedProcess(
                    command, 0, stdout=payload, stderr=""
                )

            service = OrchestratorService(store, command_runner=runner)
            params = {"workflow_id": parent["workflow_id"]}
            first = service.rerun_workflow(params)
            second = service.rerun_workflow(params)

            self.assertEqual(second["workflow_id"], first["workflow_id"])
            self.assertEqual(starts, 1)
            self.assertEqual(len(store.list_workflows()), 2)

    def test_uncertain_child_start_is_recovered_in_the_same_request(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            store = OrchestratorStore(root / "state.sqlite3")
            spec = write_spec(root)
            start_commands: list[list[str]] = []

            def runner(command, **_kwargs):
                if command[1] == "validate":
                    return subprocess.CompletedProcess(command, 0, stdout='{"valid":true}', stderr="")
                if command[1] == "start":
                    start_commands.append(command)
                    if len(start_commands) == 1:
                        raise subprocess.TimeoutExpired(command, 90)
                    return subprocess.CompletedProcess(
                        command,
                        0,
                        stdout='{"run_id":"rtl-recovered","status":"running","phase":"queued"}',
                        stderr="",
                    )
                raise AssertionError(command)

            service = OrchestratorService(store, command_runner=runner)
            params = {"spec": str(spec), "idempotency_key": "uncertain-request"}
            recovered = service.start_workflow(params)
            self.assertEqual(recovered["rtl_closure_run_id"], "rtl-recovered")
            self.assertEqual(len(store.list_workflows()), 1)
            self.assertEqual(len(recovered["attempts"]), 1)
            self.assertEqual(
                recovered["attempts"][0]["reason"], "start_response_recovered"
            )
            first_key = start_commands[0][start_commands[0].index("--idempotency-key") + 1]
            second_key = start_commands[1][start_commands[1].index("--idempotency-key") + 1]
            self.assertEqual(first_key, second_key)

    def test_invalid_start_json_is_recovered_without_an_orphan_run(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            store = OrchestratorStore(root / "state.sqlite3")
            spec = write_spec(root)
            start_commands: list[list[str]] = []

            def runner(command, **_kwargs):
                if command[1] == "validate":
                    return subprocess.CompletedProcess(
                        command, 0, stdout='{"valid":true}', stderr=""
                    )
                if command[1] == "start":
                    start_commands.append(command)
                    if len(start_commands) == 1:
                        return subprocess.CompletedProcess(
                            command,
                            0,
                            stdout=(
                                "[2026-07-28T16:45:30+08:00] "
                                "phase=queued status=running\n"
                                '{"run_id":"rtl-created","status":"running"}'
                            ),
                            stderr="",
                        )
                    return subprocess.CompletedProcess(
                        command,
                        0,
                        stdout=(
                            '{"run_id":"rtl-created","status":"running",'
                            '"phase":"queued"}'
                        ),
                        stderr="",
                    )
                raise AssertionError(command)

            service = OrchestratorService(store, command_runner=runner)
            workflow = service.start_workflow(
                {"spec": str(spec), "idempotency_key": "request-with-bad-json"}
            )

            self.assertEqual(workflow["rtl_closure_run_id"], "rtl-created")
            self.assertEqual(len(start_commands), 2)
            self.assertEqual(len(workflow["attempts"]), 1)
            self.assertEqual(
                workflow["attempts"][0]["reason"], "start_response_recovered"
            )
            self.assertEqual(
                start_commands[0][start_commands[0].index("--idempotency-key") + 1],
                start_commands[1][start_commands[1].index("--idempotency-key") + 1],
            )

    def test_repeated_invalid_start_json_remains_retryable_and_unbound(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            store = OrchestratorStore(root / "state.sqlite3")
            spec = write_spec(root)
            start_commands: list[list[str]] = []

            def runner(command, **_kwargs):
                if command[1] == "validate":
                    return subprocess.CompletedProcess(
                        command, 0, stdout='{"valid":true}', stderr=""
                    )
                if command[1] == "start":
                    start_commands.append(command)
                    return subprocess.CompletedProcess(
                        command, 0, stdout="not-json", stderr=""
                    )
                raise AssertionError(command)

            service = OrchestratorService(store, command_runner=runner)
            workflow = service.start_workflow(
                {"spec": str(spec), "idempotency_key": "repeated-bad-json"}
            )

            self.assertEqual(workflow["status"], "blocked_infrastructure")
            self.assertEqual(workflow["phase"], "rtl_closure_start_failed")
            self.assertIsNone(workflow["rtl_closure_run_id"])
            self.assertEqual(workflow["attempts"], [])
            self.assertEqual(workflow["failure"]["code"], "INVALID_BACKEND_JSON")
            self.assertTrue(workflow["failure"]["retryable"])
            self.assertEqual(len(start_commands), 2)

    def test_start_rejects_an_unsafe_backend_run_id_before_binding(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            store = OrchestratorStore(root / "state.sqlite3")
            starts = 0

            def runner(command, **_kwargs):
                nonlocal starts
                if command[1] == "validate":
                    payload = '{"valid":true}'
                elif command[1] == "start":
                    starts += 1
                    payload = '{"run_id":"../../foreign","status":"running"}'
                else:
                    raise AssertionError(command)
                return subprocess.CompletedProcess(
                    command, 0, stdout=payload, stderr=""
                )

            service = OrchestratorService(store, command_runner=runner)
            workflow = service.start_workflow({"spec": str(write_spec(root))})

            self.assertEqual(starts, 2)
            self.assertEqual(workflow["status"], "blocked_infrastructure")
            self.assertIsNone(workflow["rtl_closure_run_id"])
            self.assertEqual(workflow["attempts"], [])
            self.assertEqual(workflow["failure"]["code"], "INVALID_BACKEND_JSON")

    def test_nonzero_status_json_is_a_valid_snapshot(self):
        completed = subprocess.CompletedProcess(
            ["rtl-closure", "report"],
            1,
            stdout='{"status":"failed","phase":"failed"}',
            stderr="",
        )
        value = parse_json_stdout(
            completed, command="rtl-closure report", allow_nonzero_status=True
        )
        self.assertEqual(value["status"], "failed")

    def test_schema_four_and_removed_import_command_are_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            database = Path(directory) / "state.sqlite3"
            OrchestratorStore(database)
            with sqlite3.connect(database) as connection:
                connection.execute(
                    "UPDATE metadata SET value='4' WHERE key='schema_version'"
                )
            with self.assertRaises(OrchestratorError) as unsupported:
                OrchestratorStore(database)
            self.assertEqual(
                unsupported.exception.code, "UNSUPPORTED_DATABASE_SCHEMA"
            )
        with self.assertRaises(SystemExit):
            build_parser().parse_args(["import-predecessor"])

    def test_reconcile_recovers_created_workflow_without_start_operation(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            store = OrchestratorStore(root / "state.sqlite3")
            workflow, _ = store.create_workflow(
                write_spec(root), idempotency_key="created-crash"
            )
            commands: list[str] = []

            def runner(command, **_kwargs):
                commands.append(command[1])
                if command[1] == "validate":
                    payload = {"valid": True}
                elif command[1] == "start":
                    payload = {"run_id": "rtl-created-recovered", "status": "running"}
                else:
                    raise AssertionError(command)
                return subprocess.CompletedProcess(
                    command, 0, stdout=json.dumps(payload), stderr=""
                )

            recovered = OrchestratorService(
                store, command_runner=runner
            ).reconcile_one(workflow)
            self.assertEqual(recovered["rtl_closure_run_id"], "rtl-created-recovered")
            self.assertEqual(commands, ["validate", "start"])
            self.assertEqual(recovered["operations"][0]["status"], "completed")

    def test_daemon_recovery_binds_uncertain_start_without_client_replay(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            store = OrchestratorStore(root / "state.sqlite3")
            spec = write_spec(root)
            keys: list[str] = []

            def runner(command, **_kwargs):
                if command[1] == "validate":
                    payload = {"valid": True}
                elif command[1] == "start":
                    keys.append(command[command.index("--idempotency-key") + 1])
                    payload = {"run_id": "rtl-daemon-recovered", "status": "running"}
                else:
                    raise AssertionError(command)
                return subprocess.CompletedProcess(
                    command, 0, stdout=json.dumps(payload), stderr=""
                )

            service = OrchestratorService(store, command_runner=runner)
            original = store.bind_attempt_and_update_workflow
            with patch.object(
                store,
                "bind_attempt_and_update_workflow",
                side_effect=sqlite3.OperationalError("crash before binding"),
            ):
                with self.assertRaises(sqlite3.OperationalError):
                    service.start_workflow(
                        {"spec": str(spec), "idempotency_key": "daemon-crash"}
                    )
            unbound = store.list_workflows()[0]
            self.assertIsNone(unbound["rtl_closure_run_id"])
            recovered = OrchestratorService(
                store, command_runner=runner
            ).reconcile_one(unbound)
            self.assertEqual(recovered["rtl_closure_run_id"], "rtl-daemon-recovered")
            self.assertEqual(keys[0], keys[1])
            self.assertEqual(len(recovered["attempts"]), 1)
            self.assertIsNotNone(original)

    def test_stop_waits_for_uncertain_start_discovery_then_stops_child(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            store = OrchestratorStore(root / "state.sqlite3")
            workflow, _ = store.create_workflow(
                write_spec(root), idempotency_key="stop-after-start-crash"
            )
            child_key = backend_report(workflow, "unused")["origin"]["idempotency_key"]
            start_operation, _ = store.create_operation(
                workflow["workflow_id"],
                kind="start",
                request={
                    "spec_path": workflow["spec_path"],
                    "spec_sha256": workflow["spec_sha256"],
                },
                idempotency_key=child_key,
            )
            store.mark_operation_attempt(start_operation["operation_id"])
            commands: list[str] = []

            def runner(command, **_kwargs):
                commands.append(command[1])
                if command[1] == "validate":
                    payload = {"valid": True}
                elif command[1] == "start":
                    payload = {"run_id": "rtl-stop-race", "status": "running"}
                elif command[1] == "stop":
                    payload = {"run_id": "rtl-stop-race", "status": "stopped"}
                else:
                    raise AssertionError(command)
                return subprocess.CompletedProcess(
                    command, 0, stdout=json.dumps(payload), stderr=""
                )

            service = OrchestratorService(store, command_runner=runner)
            requested = service.stop_workflow({"workflow_id": workflow["workflow_id"]})
            self.assertEqual(commands, [])
            self.assertEqual(
                [row["kind"] for row in requested["operations"]], ["start", "stop"]
            )
            bound = service.reconcile_one(requested)
            self.assertEqual(bound["rtl_closure_run_id"], "rtl-stop-race")
            stopped = service.reconcile_one(bound)
            self.assertEqual(stopped["status"], "cancelled")
            self.assertEqual(commands, ["validate", "start", "stop"])
            self.assertEqual(
                [row["status"] for row in stopped["operations"]],
                ["completed", "completed"],
            )

    def test_nonretryable_start_failure_is_atomically_recovered(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            store = OrchestratorStore(root / "state.sqlite3")
            spec = write_spec(root)

            def runner(command, **_kwargs):
                if command[1] != "validate":
                    raise AssertionError(command)
                snapshot = store.list_workflows()[0]["spec_path"]
                os.chmod(snapshot, 0o600)
                Path(snapshot).write_text(
                    json.dumps(
                        {"schema_version": 2, "design": {"name": "changed", "pdk": "sky130A"}}
                    ),
                    encoding="utf-8",
                )
                return subprocess.CompletedProcess(
                    command, 0, stdout='{"valid":true}', stderr=""
                )

            service = OrchestratorService(store, command_runner=runner)
            with patch.object(
                store,
                "_fail_operation_locked",
                side_effect=sqlite3.OperationalError("crash during terminal commit"),
            ):
                with self.assertRaises(sqlite3.OperationalError):
                    service.start_workflow({"spec": str(spec), "idempotency_key": "bad-spec"})
            interrupted_row = store.list_workflows()[0]
            interrupted = store.get_workflow(interrupted_row["workflow_id"])
            self.assertEqual(interrupted["operations"][0]["status"], "executing")
            recovered = OrchestratorService(store).reconcile_one(interrupted)
            self.assertEqual(recovered["status"], "waiting_user")
            self.assertEqual(recovered["failure"]["code"], "SPEC_SNAPSHOT_CHANGED")
            self.assertEqual(recovered["operations"][0]["status"], "failed")

    def test_report_identity_and_state_root_are_required(self):
        cases = {
            "schema": ("BACKEND_SCHEMA_MISMATCH", lambda report: report.update(schema_version=2)),
            "origin": (
                "BACKEND_IDENTITY_MISMATCH",
                lambda report: report["origin"].update(workflow_id="other-workflow"),
            ),
            "spec": (
                "BACKEND_IDENTITY_MISMATCH",
                lambda report: report["origin"].update(spec_sha256="0" * 64),
            ),
            "idempotency": (
                "BACKEND_IDENTITY_MISMATCH",
                lambda report: report["origin"].update(idempotency_key="wrong-key"),
            ),
            "state-root": (
                "BACKEND_STATE_ROOT_MISMATCH",
                lambda report: report.update(state_file="/tmp/foreign/state.json"),
            ),
        }
        for label, (expected_code, mutate) in cases.items():
            with self.subTest(label=label), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                store = OrchestratorStore(root / "state.sqlite3")
                workflow, _ = store.create_workflow(
                    write_spec(root, name=f"identity_{label.replace('-', '_')}"),
                    idempotency_key=f"identity-{label}",
                )
                workflow = store.update_workflow(
                    workflow["workflow_id"],
                    status="running",
                    phase="generating",
                    snapshot={},
                    rtl_closure_run_id=f"rtl-{label}",
                )
                report = backend_report(workflow, f"rtl-{label}")
                mutate(report)

                def runner(command, **_kwargs):
                    payload = report if command[1] == "report" else {"status": "running"}
                    return subprocess.CompletedProcess(
                        command, 0, stdout=json.dumps(payload), stderr=""
                    )

                failed = ProductionOrchestratorService(
                    store, command_runner=runner
                ).reconcile_one(workflow)
                self.assertEqual(failed["status"], "failed")
                self.assertEqual(failed["failure"]["code"], expected_code)

    def test_rerun_rejects_modified_frozen_spec(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            store = OrchestratorStore(root / "state.sqlite3")
            parent, _ = store.create_workflow(
                write_spec(root), idempotency_key="rerun-frozen"
            )
            parent = store.update_workflow(
                parent["workflow_id"],
                status="completed",
                phase="completed",
                snapshot={},
            )
            os.chmod(parent["spec_path"], 0o600)
            Path(parent["spec_path"]).write_text(
                json.dumps(
                    {"schema_version": 2, "design": {"name": "tampered", "pdk": "sky130A"}}
                ),
                encoding="utf-8",
            )
            runner = Mock(side_effect=AssertionError("backend must not run"))
            with self.assertRaises(OrchestratorError) as changed:
                OrchestratorService(store, command_runner=runner).rerun_workflow(
                    {"workflow_id": parent["workflow_id"]}
                )
            self.assertEqual(changed.exception.code, "SPEC_SNAPSHOT_CHANGED")
            runner.assert_not_called()

    def test_old_event_corruption_blocks_status_mutation_and_lease(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            store = OrchestratorStore(root / "state.sqlite3")
            workflow, _ = store.create_workflow(
                write_spec(root), idempotency_key="full-chain"
            )
            workflow = store.update_workflow(
                workflow["workflow_id"], status="running", phase="generating", snapshot={}
            )
            with sqlite3.connect(store.db_path) as connection:
                connection.execute(
                    "UPDATE events SET payload_json='{}' WHERE workflow_id=? AND sequence=1",
                    (workflow["workflow_id"],),
                )
            for operation in (
                lambda: store.compact_status(workflow["workflow_id"]),
                lambda: store.update_workflow(
                    workflow["workflow_id"], status="running", phase="next", snapshot={}
                ),
                lambda: store.acquire_lease(workflow["workflow_id"], owner="corrupt"),
            ):
                with self.assertRaises(OrchestratorError) as corrupted:
                    operation()
                self.assertEqual(corrupted.exception.code, "JOURNAL_INTEGRITY_ERROR")

    def test_lease_tokens_are_fenced_and_dead_process_is_reclaimed(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            store = OrchestratorStore(root / "state.sqlite3")
            workflow, _ = store.create_workflow(
                write_spec(root), idempotency_key="lease-fencing"
            )
            token = store.acquire_lease(workflow["workflow_id"], owner="first")
            self.assertIsInstance(token, str)
            self.assertIsNone(store.acquire_lease(workflow["workflow_id"], owner="second"))
            store.release_lease(workflow["workflow_id"], owner="first", token="wrong")
            self.assertIsNone(store.acquire_lease(workflow["workflow_id"], owner="second"))
            assert token is not None
            store.release_lease(workflow["workflow_id"], owner="first", token=token)
            reclaimed = store.acquire_lease(workflow["workflow_id"], owner="second")
            self.assertIsInstance(reclaimed, str)
            with sqlite3.connect(store.db_path) as connection:
                connection.execute(
                    "UPDATE leases SET pid=999999999,process_start_time='dead',expires_at=? "
                    "WHERE workflow_id=?",
                    (time.time() + 300, workflow["workflow_id"]),
                )
            dead_reclaimed = store.acquire_lease(workflow["workflow_id"], owner="third")
            self.assertIsInstance(dead_reclaimed, str)

    def test_lease_process_start_time_handles_spaced_process_names(self):
        fields = ["S", *("0" for _ in range(18)), "123456"]
        stat_row = "4321 (worker ) with spaces) " + " ".join(fields)
        with patch.object(Path, "read_text", return_value=stat_row):
            self.assertEqual(process_start_time(4321), "123456")

        with patch.object(Path, "read_text", return_value="malformed stat"):
            self.assertIsNone(process_start_time(4321))


if __name__ == "__main__":
    unittest.main()
