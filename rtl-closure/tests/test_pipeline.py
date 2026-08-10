from __future__ import annotations

import json
import hashlib
import io
import os
import subprocess
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from rtl_closure.contract import content_hash
from rtl_closure.adaptive import suite_hash
from rtl_closure.cli import build_parser, cmd_list, cmd_report, cmd_start, cmd_status
from rtl_closure.coverage_refinement import (
    ORACLE_COVERAGE_LIMITATION,
    ORACLE_COVERAGE_METRIC,
)
from rtl_closure.errors import (
    LaneLaunchError,
    LaneReconciliationError,
    RepairExhaustedError,
)
from rtl_closure.formal import (
    FORMAL_BATCH_SIZE,
    FORMAL_MIN_BATCH_SIZE,
    build_formal_evidence_contract,
    render_formal_batch_script,
)
from rtl_closure.oracle import _trace_behavior_hash
from rtl_closure.pipeline import (
    DESIGN_ROOT,
    STATE_ROOT,
    _artifact_hash,
    _distinct_oracle_backends,
    _load_frozen_spec,
    _launch_pipeline_worker,
    _merge_model_progress,
    _merge_repair_progress,
    _mapped_equivalence_mode,
    _mapped_equivalence_evidence_errors,
    _process_identity_matches,
    _record_repair_outcome,
    _required_release_artifacts,
    _validate_lane_state_identity,
    _verify_publication,
    _start_lane_unlocked,
    _update,
    build_report,
    format_report,
    format_state,
    list_states,
    load_state,
    publish_design,
    refresh_state,
    retry_lane,
    save_state,
    start_pipeline,
    stop_pipeline,
    worker,
)
from rtl_closure.spec import (
    compute_intent_binding,
    load_spec,
    render_eqy_gate_tcl,
    render_eqy_script_for_name,
    validate_spec,
)
from rtl_closure.universal import (
    universal_proof_scope,
    universal_requirement_coverage,
)
from rtl_closure.verification import VERIFY_IMAGE, derive_independent_stimuli
from rtl_closure.runtime import (
    SANDBOX_IMAGE,
    bind_sandbox_image,
    bind_verify_image,
    sandbox_image_ref,
    verify_image_ref,
)


EXAMPLE = Path(__file__).parents[1] / "examples" / "counter.json"


def lane_response(
    run_id: str,
    *,
    status: str = "running",
    audit_id: str = "lane-audit-test",
    release_status: str | None = None,
    release_errors: list[str] | None = None,
    **fields: object,
) -> dict[str, object]:
    payload: dict[str, object] = {
        "schema_version": 6,
        "run_id": run_id,
        "status": status,
        **fields,
    }
    if status == "completed" and release_status is None:
        release_status = "verified"
    if release_status is not None:
        attestation: dict[str, object] = {
            "schema_version": 1,
            "run_id": run_id,
            "files": [],
        }
        attestation_sha256 = content_hash(attestation)
        attestation["attestation_sha256"] = attestation_sha256
        payload["delivery_attestation"] = attestation
        payload["release_integrity"] = {
            "schema_version": 1,
            "status": release_status,
            "audit_id": audit_id,
            "errors": list(release_errors or []),
            "attestation_sha256": attestation_sha256,
        }
    return payload


class TypedPipelineFailureTests(unittest.TestCase):
    def test_report_preserves_structured_retry_and_repair_disposition(self):
        with tempfile.TemporaryDirectory() as temp:
            pipeline_log = Path(temp) / "pipeline.log"
            pipeline_log.write_text("", encoding="utf-8")
            state = {
                "run_id": "run-model-config-failed",
                "design_name": "demo",
                "pdk": "sky130A",
                "status": "failed",
                "phase": "failed",
                "elapsed_seconds": 1,
                "pipeline_log": str(pipeline_log),
                "failure_class": "infrastructure",
                "failure_gate": "model_request",
                "failure_code": "infrastructure/model_request",
                "failure": {
                    "code": "infrastructure/model_request",
                    "category": "infrastructure",
                    "domain": "model_request",
                    "repairable_by_rtl": False,
                    "retryable": False,
                    "message": "model configuration is incomplete",
                    "evidence": [],
                },
            }

            report = build_report(state, compact=True)

        self.assertFalse(report["failure"]["retryable"])
        self.assertFalse(report["failure"]["repairable_by_rtl"])
        rendered = format_report(report)
        self.assertIn("failure_retryable: False", rendered)
        self.assertIn("failure_repairable_by_rtl: False", rendered)

    def test_repair_exhaustion_preserves_primary_failure_identity(self):
        error = RepairExhaustedError(
            "repair exhausted", failure_class="architecture_constraint"
        )

        self.assertEqual(error.failure_class, "architecture_constraint")
        self.assertEqual(error.failure_gate, "rtl_repair")
        self.assertEqual(
            error.failure_code, "architecture_constraint/repair_exhausted"
        )

    def test_failed_repair_stops_before_release_evidence(self):
        spec = SimpleNamespace(
            name="demo",
            verification={
                "requirements": [{"id": "REQ_ARCH"}],
                "trace_window_cycles": 4,
                "formal_mode": "required",
                "universal_mode": "required",
                "universal_properties": [{"id": "PROP_REQUIRED"}],
            },
        )
        rtl = "module demo(input logic clk); endmodule"
        verification = {
            "passed": False,
            "failure_class": "architecture_constraint",
            "detail": "max_sequential_bits failed",
            "total_checks": 1,
            "mismatched": 1,
            "first_failure_ordinal": 0,
            "mismatches": [
                {
                    "check_id": "architecture:max_sequential_bits",
                    "ordinal": 0,
                    "scenario_index": None,
                    "scenario_id": None,
                    "requirement_ids": [],
                    "cycle": None,
                    "phase": "static_architecture",
                    "signal": "max_sequential_bits",
                    "expected": 71,
                    "actual": 73,
                    "inputs": {},
                    "failure_class": "architecture_constraint",
                }
            ],
            "passed_check_ids": [],
            "actual_trace": [],
            "suite_sha256": "a" * 64,
            "coverage": {},
        }
        outcome = SimpleNamespace(
            rtl=rtl,
            verification=verification,
            history=[],
            counterexamples=[],
            repaired=False,
            exhausted=True,
        )

        with tempfile.TemporaryDirectory() as temp:
            run_dir = Path(temp)
            state = {
                "run_id": "run-repair-exhausted",
                "state_file": str(run_dir / "state.json"),
                "events_file": str(run_dir / "events.jsonl"),
                "event_sequence": 0,
                "status": "running",
                "phase": "repair_evaluating",
            }
            save_state(state)

            with self.assertRaises(RepairExhaustedError) as caught:
                _record_repair_outcome(
                    spec, outcome, rtl, run_dir, {"epochs": []}, state
                )

            self.assertEqual(caught.exception.failure_class, "architecture_constraint")
            persisted_state = json.loads((run_dir / "state.json").read_text())
            self.assertEqual(persisted_state["phase"], "repair_exhausted")
            self.assertEqual(
                persisted_state["failure_code"],
                "architecture_constraint/repair_exhausted",
            )
            self.assertTrue((run_dir / "verification-result.json").is_file())
            self.assertTrue((run_dir / "last-failure.json").is_file())
            universal = json.loads(
                (run_dir / "universal-properties.json").read_text()
            )
            self.assertEqual(universal["status"], "not_run")
            self.assertEqual(universal["blocked_by"], "architecture_constraint")
            self.assertEqual(universal["requested_property_count"], 1)
            self.assertFalse((run_dir / "final-suite-quorum.json").exists())
            self.assertFalse(
                (run_dir / "universal-execution-evidence.json").exists()
            )


class OracleBackendSelectionTests(unittest.TestCase):
    def test_sources_are_deduplicated_by_provider_and_model(self):
        first = SimpleNamespace(provider="provider-a", model="model-a")
        duplicate = SimpleNamespace(provider="provider-a", model="model-a")
        distinct = SimpleNamespace(provider="provider-b", model="model-b")
        self.assertEqual(
            _distinct_oracle_backends(first, duplicate, distinct),
            [first, distinct],
        )


class CliTerminalStatusTests(unittest.TestCase):
    def test_incomplete_status_and_report_are_successful_queries(self):
        args = SimpleNamespace(run_id="incomplete-run", json=True, log_lines=20)
        with patch(
            "rtl_closure.cli.load_state", return_value={"status": "incomplete"}
        ), patch("builtins.print"):
            self.assertEqual(cmd_status(args), 0)
        with patch(
            "rtl_closure.cli.report_state",
            return_value={"status": "incomplete"},
        ), patch("builtins.print"):
            self.assertEqual(cmd_report(args), 0)


class ListStatesTests(unittest.TestCase):
    def test_future_publications_default_to_xdg_state(self):
        if "RTL_CLOSURE_DESIGN_ROOT" not in os.environ:
            self.assertEqual(DESIGN_ROOT, STATE_ROOT / "published-designs")

    @staticmethod
    def _write_state(root: Path, run_id: str, status: str, started_epoch: float):
        run_dir = root / run_id
        run_dir.mkdir()
        state = {
            "schema_version": 2,
            "run_id": run_id,
            "status": status,
            "phase": status,
            "started_epoch": started_epoch,
            "design_name": "demo",
            "pdk": "sky130A",
        }
        path = run_dir / "state.json"
        path.write_text(json.dumps(state), encoding="utf-8")
        return path, state

    def test_active_runs_are_newest_first_and_limited(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            self._write_state(root, "run-created", "created", 10)
            self._write_state(root, "run-queued", "queued", 20)
            self._write_state(root, "run-running", "running", 30)
            self._write_state(root, "run-completed", "completed", 40)
            malformed = root / "broken"
            malformed.mkdir()
            (malformed / "state.json").write_text("{broken", encoding="utf-8")
            wrong_shape = root / "wrong-shape"
            wrong_shape.mkdir()
            (wrong_shape / "state.json").write_text("[]", encoding="utf-8")

            with patch("rtl_closure.pipeline.STATE_ROOT", root):
                states = list_states(active_only=True, limit=2)

            self.assertEqual(
                [state["run_id"] for state in states],
                ["run-running", "run-queued"],
            )

    def test_list_is_a_pure_read_and_does_not_use_current(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            state_path, state = self._write_state(root, "run-running", "running", 1)
            current = root / "current"
            current.write_text("old-terminal-run\n", encoding="utf-8")
            state_before = (state_path.read_bytes(), state_path.stat().st_mtime_ns)
            current_before = (current.read_bytes(), current.stat().st_mtime_ns)

            with (
                patch("rtl_closure.pipeline.STATE_ROOT", root),
                patch("rtl_closure.pipeline.CURRENT", current),
            ):
                listed = list_states(active_only=True)

            self.assertEqual(len(listed), 1)
            self.assertEqual(listed[0]["run_id"], state["run_id"])
            self.assertEqual(listed[0]["status"], state["status"])
            self.assertEqual(listed[0]["state_file"], str(state_path))
            self.assertNotIn("verification", listed[0])

            self.assertEqual(
                (state_path.read_bytes(), state_path.stat().st_mtime_ns), state_before
            )
            self.assertEqual(
                (current.read_bytes(), current.stat().st_mtime_ns), current_before
            )

    def test_cli_exposes_active_limit_json(self):
        args = build_parser().parse_args(["list", "--active", "--limit", "3", "--json"])
        listed = [{"run_id": "run-running", "status": "running"}]
        output = io.StringIO()
        with patch("rtl_closure.cli.list_states", return_value=listed) as mocked:
            with redirect_stdout(output):
                self.assertEqual(cmd_list(args), 0)

        mocked.assert_called_once_with(active_only=True, limit=3)
        self.assertEqual(json.loads(output.getvalue()), listed)

    def test_limit_is_bounded(self):
        with self.assertRaisesRegex(ValueError, "limit must be from 1 to 1000"):
            list_states(limit=0)


class EventJournalRecoveryTests(unittest.TestCase):
    @staticmethod
    def _state(root: Path, run_id: str = "run-journal") -> dict:
        run_dir = root / run_id
        run_dir.mkdir()
        state = {
            "schema_version": 2,
            "run_id": run_id,
            "run_dir": str(run_dir),
            "state_file": str(run_dir / "state.json"),
            "pipeline_log": str(run_dir / "pipeline.log"),
            "events_file": str(run_dir / "events.jsonl"),
            "spec_file": str(run_dir / "spec.json"),
            "event_sequence": 0,
            "revision": 0,
            "design_name": "demo",
            "pdk": "sky130A",
            "status": "running",
            "phase": "generating",
            "started_epoch": 1,
        }
        (run_dir / "pipeline.log").write_text("", encoding="utf-8")
        (run_dir / "spec.json").write_text("{}\n", encoding="utf-8")
        save_state(state)
        return state

    @staticmethod
    def _load(root: Path, run_id: str = "run-journal") -> dict:
        with (
            patch("rtl_closure.pipeline.STATE_ROOT", root),
            patch("rtl_closure.pipeline.CURRENT", root / "current"),
        ):
            return load_state(run_id)

    @staticmethod
    def _events(state: dict) -> list[dict]:
        path = Path(state["events_file"])
        if not path.exists():
            return []
        return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]

    def test_load_repairs_exactly_one_event_missing_after_state_commit(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            state = self._state(root)
            pending = Path(state["state_file"]).with_name("event-pending.json")

            with patch(
                "rtl_closure.pipeline._append_event_locked",
                side_effect=OSError("simulated append failure"),
            ):
                with self.assertRaisesRegex(OSError, "simulated append failure"):
                    _update(state, phase="completed", status="completed")

            state_path = Path(state["state_file"])
            durable_state = state_path.read_bytes()
            self.assertTrue(pending.is_file())
            self.assertEqual(self._events(state), [])

            recovered = self._load(root)

            self.assertEqual(state_path.read_bytes(), durable_state)
            self.assertFalse(pending.exists())
            self.assertEqual(recovered["status"], "completed")
            events = self._events(recovered)
            self.assertEqual(len(events), 1)
            event = events[0]
            self.assertEqual(event["schema_version"], 3)
            self.assertEqual(event["sequence"], 1)
            self.assertTrue(event["terminal"])
            claimed = event.pop("event_sha256")
            self.assertEqual(claimed, content_hash(event))
            self.assertEqual(recovered["last_event_sha256"], claimed)

    def test_load_clears_pending_without_duplicating_already_appended_event(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            state = self._state(root)
            pending = Path(state["state_file"]).with_name("event-pending.json")

            with patch(
                "rtl_closure.pipeline._clear_pending_event_locked",
                side_effect=OSError("simulated cleanup failure"),
            ):
                with self.assertRaisesRegex(OSError, "simulated cleanup failure"):
                    _update(state, phase="verifying")

            self.assertTrue(pending.is_file())
            self.assertEqual(len(self._events(state)), 1)

            first = self._load(root)
            second = self._load(root)

            self.assertFalse(pending.exists())
            self.assertEqual(len(self._events(first)), 1)
            self.assertEqual(len(self._events(second)), 1)

    def test_load_rejects_tampering_in_a_nonterminal_event_chain_prefix(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            state = self._state(root)
            _update(state, phase="verifying")
            _update(state, phase="publishing")
            events = self._events(state)
            original_first_hash = events[0]["event_sha256"]
            events[0]["phase"] = "tampered"
            first_payload = dict(events[0])
            first_payload.pop("event_sha256")
            events[0]["event_sha256"] = content_hash(first_payload)
            self.assertNotEqual(events[0]["event_sha256"], original_first_hash)
            Path(state["events_file"]).write_text(
                "".join(json.dumps(row) + "\n" for row in events),
                encoding="utf-8",
            )

            with self.assertRaisesRegex(RuntimeError, "hash chain"):
                self._load(root)

    def test_load_rejects_state_projection_tampering_with_an_intact_journal(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            state = self._state(root)
            _update(state, phase="verifying")
            state_path = Path(state["state_file"])
            persisted = json.loads(state_path.read_text(encoding="utf-8"))
            persisted["status"] = "completed"
            persisted["phase"] = "completed"
            state_path.write_text(json.dumps(persisted), encoding="utf-8")

            with self.assertRaisesRegex(RuntimeError, "state projection"):
                self._load(root)

    def test_load_discards_pending_when_state_commit_did_not_happen(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            state = self._state(root)
            state_path = Path(state["state_file"])
            durable_state = state_path.read_bytes()
            pending = state_path.with_name("event-pending.json")

            with patch(
                "rtl_closure.pipeline._persist_state_locked",
                side_effect=OSError("simulated state commit failure"),
            ):
                with self.assertRaisesRegex(OSError, "simulated state commit failure"):
                    _update(state, phase="verifying")

            self.assertTrue(pending.is_file())
            self.assertEqual(state_path.read_bytes(), durable_state)
            self.assertEqual(self._events(state), [])

            recovered = self._load(root)

            self.assertFalse(pending.exists())
            self.assertEqual(recovered["phase"], "generating")
            self.assertEqual(recovered["event_sequence"], 0)
            self.assertEqual(self._events(recovered), [])

    def test_update_refuses_to_overwrite_corrupt_durable_state(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            state = self._state(root)
            state_path = Path(state["state_file"])
            state_path.write_text("{not-json\n", encoding="utf-8")

            with self.assertRaisesRegex(RuntimeError, "cannot be read safely"):
                _update(state, phase="verifying")

            self.assertEqual(state_path.read_text(encoding="utf-8"), "{not-json\n")

    def test_recovery_refuses_a_multi_event_journal_gap(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            state = self._state(root)
            _update(state, phase="verifying")
            with patch(
                "rtl_closure.pipeline._append_event_locked",
                side_effect=OSError("simulated append failure"),
            ):
                with self.assertRaises(OSError):
                    _update(state, phase="formal")
            Path(state["events_file"]).write_text("", encoding="utf-8")

            with self.assertRaisesRegex(RuntimeError, "sequence gap"):
                self._load(root)

    def test_recovery_refuses_a_journal_ahead_of_state(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            state = self._state(root)
            _update(state, phase="verifying")
            state_path = Path(state["state_file"])
            persisted = json.loads(state_path.read_text(encoding="utf-8"))
            persisted["event_sequence"] = 0
            persisted.pop("last_event_sha256", None)
            persisted.pop("last_event_timestamp", None)
            state_path.write_text(json.dumps(persisted), encoding="utf-8")

            with self.assertRaisesRegex(RuntimeError, "ahead of state"):
                self._load(root)

    def test_recovery_refuses_pending_event_not_derivable_from_state(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            state = self._state(root)
            with patch(
                "rtl_closure.pipeline._append_event_locked",
                side_effect=OSError("simulated append failure"),
            ):
                with self.assertRaises(OSError):
                    _update(state, phase="completed", status="completed")

            state_path = Path(state["state_file"])
            pending_path = state_path.with_name("event-pending.json")
            persisted = json.loads(state_path.read_text(encoding="utf-8"))
            pending = json.loads(pending_path.read_text(encoding="utf-8"))
            persisted["phase"] = "tampered"
            pending["state_sha256"] = content_hash(persisted)
            pending.pop("pending_sha256")
            pending["pending_sha256"] = content_hash(pending)
            state_path.write_text(json.dumps(persisted), encoding="utf-8")
            pending_path.write_text(json.dumps(pending), encoding="utf-8")

            with self.assertRaisesRegex(RuntimeError, "uniquely derivable"):
                self._load(root)


class PipelineLifecycleSafetyTests(unittest.TestCase):
    class FakeProcess:
        def __init__(self, pid: int):
            self.pid = pid
            self.terminated = False
            self.killed = False

        def terminate(self):
            self.terminated = True

        def kill(self):
            self.killed = True

        def wait(self, timeout=None):
            del timeout
            return 0

    @staticmethod
    def _identity(pid: int) -> dict:
        return {
            "pid": pid,
            "start_time_ticks": pid * 10,
            "cmdline_sha256": f"{pid:064x}"[-64:],
        }

    @staticmethod
    def _persist(root: Path, run_id: str, **values):
        run_dir = root / run_id
        run_dir.mkdir()
        state = {
            "schema_version": 2,
            "run_id": run_id,
            "run_dir": str(run_dir),
            "state_file": str(run_dir / "state.json"),
            "events_file": str(run_dir / "events.jsonl"),
            "pipeline_log": str(run_dir / "pipeline.log"),
            "spec_file": str(run_dir / "spec.json"),
            "event_sequence": 0,
            "status": "running",
            "phase": "generating",
            "started_epoch": 1,
            "pid": None,
            "worker_process": None,
            "lane_run_id": None,
            **values,
        }
        (run_dir / "pipeline.log").write_text("", encoding="utf-8")
        save_state(state)
        return state

    def test_load_state_derives_control_paths_from_run_identity(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            run_id = "run-paths"
            run_dir = root / run_id
            run_dir.mkdir()
            poisoned = {
                "schema_version": 2,
                "run_id": run_id,
                "status": "running",
                "phase": "queued",
                "run_dir": "/tmp/foreign",
                "state_file": "/tmp/foreign/state.json",
                "pipeline_log": "/tmp/foreign/pipeline.log",
                "events_file": "/tmp/foreign/events.jsonl",
                "spec_file": "/tmp/foreign/spec.json",
            }
            (run_dir / "state.json").write_text(json.dumps(poisoned), encoding="utf-8")
            with patch("rtl_closure.pipeline.STATE_ROOT", root):
                loaded = load_state(run_id)
            self.assertEqual(loaded["run_dir"], str(run_dir))
            self.assertEqual(loaded["state_file"], str(run_dir / "state.json"))
            self.assertEqual(loaded["pipeline_log"], str(run_dir / "pipeline.log"))
            self.assertEqual(loaded["events_file"], str(run_dir / "events.jsonl"))
            self.assertEqual(loaded["spec_file"], str(run_dir / "spec.json"))

    def test_frozen_spec_hash_is_checked_before_worker_or_retry(self):
        with tempfile.TemporaryDirectory() as temp:
            run_dir = Path(temp)
            raw = json.loads(EXAMPLE.read_text())
            spec_file = run_dir / "spec.json"
            spec_file.write_text(json.dumps(raw), encoding="utf-8")
            state = {
                "run_dir": str(run_dir),
                "spec_file": str(spec_file),
                "origin": {"spec_sha256": content_hash(load_spec(EXAMPLE).raw)},
            }
            self.assertEqual(_load_frozen_spec(state).name, raw["design"]["name"])
            raw["design"]["name"] = "changed_after_launch"
            spec_file.write_text(json.dumps(raw), encoding="utf-8")
            with self.assertRaisesRegex(RuntimeError, "content changed"):
                _load_frozen_spec(state)

    def test_load_state_rejects_symlinked_state_file(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            outside = root / "outside.json"
            outside.write_text(
                json.dumps(
                    {
                        "run_id": "run-symlink",
                        "status": "running",
                        "phase": "queued",
                    }
                ),
                encoding="utf-8",
            )
            run_dir = root / "run-symlink"
            run_dir.mkdir()
            (run_dir / "state.json").symlink_to(outside)
            with patch("rtl_closure.pipeline.STATE_ROOT", root):
                with self.assertRaisesRegex(RuntimeError, "path identity"):
                    load_state("run-symlink")

    def test_mutating_cli_requires_an_explicit_target(self):
        parser = build_parser()
        with patch("sys.stderr", io.StringIO()):
            with self.assertRaises(SystemExit):
                parser.parse_args(["stop"])
            with self.assertRaises(SystemExit):
                parser.parse_args(["retry-lane"])
        self.assertTrue(parser.parse_args(["stop", "--current"]).current)
        self.assertTrue(
            parser.parse_args(["stop", "--run-id", "run-1", "--json"]).json
        )
        self.assertEqual(
            parser.parse_args(["retry-lane", "--run-id", "run-1"]).run_id,
            "run-1",
        )
        self.assertEqual(
            parser.parse_args(
                [
                    "retry-lane",
                    "--run-id",
                    "run-1",
                    "--idempotency-key",
                    "workflow-1:retry-1",
                ]
            ).idempotency_key,
            "workflow-1:retry-1",
        )

    def test_same_idempotency_key_is_isolated_by_workflow(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            current = root / "current"
            processes = [self.FakeProcess(4101), self.FakeProcess(4102)]

            def identity(pid):
                return self._identity(pid)

            with (
                patch("rtl_closure.pipeline.STATE_ROOT", root),
                patch("rtl_closure.pipeline.CURRENT", current),
                patch("rtl_closure.pipeline.subprocess.Popen", side_effect=processes),
                patch("rtl_closure.pipeline._read_process_identity", side_effect=identity),
            ):
                first = start_pipeline(
                    EXAMPLE,
                    generate_only=True,
                    workflow_id="workflow-a",
                    idempotency_key="shared-key",
                )
                second = start_pipeline(
                    EXAMPLE,
                    generate_only=True,
                    workflow_id="workflow-b",
                    idempotency_key="shared-key",
                )

            self.assertNotEqual(first["run_id"], second["run_id"])
            self.assertEqual(first["origin"]["idempotency_scope"], "workflow-a")
            self.assertEqual(second["origin"]["idempotency_scope"], "workflow-b")

    def test_popen_failure_is_persisted_as_terminal_run(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            current = root / "current"
            with (
                patch("rtl_closure.pipeline.STATE_ROOT", root),
                patch("rtl_closure.pipeline.CURRENT", current),
                patch(
                    "rtl_closure.pipeline.subprocess.Popen",
                    side_effect=OSError("exec failed"),
                ),
            ):
                with self.assertRaisesRegex(RuntimeError, "could not be started"):
                    start_pipeline(EXAMPLE, generate_only=True)

            run_id = current.read_text(encoding="utf-8").strip()
            state = json.loads((root / run_id / "state.json").read_text())
            self.assertEqual(state["status"], "failed")
            self.assertEqual(state["phase"], "failed")
            self.assertEqual(state["failure_code"], "infrastructure/worker_launch")
            self.assertIsNone(state["pid"])

    def test_repeated_idempotent_start_recovers_created_worker_launch(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            current = root / "current"
            process = self.FakeProcess(4199)
            with (
                patch("rtl_closure.pipeline.STATE_ROOT", root),
                patch("rtl_closure.pipeline.CURRENT", current),
                patch(
                    "rtl_closure.pipeline.subprocess.Popen",
                    side_effect=[KeyboardInterrupt(), process],
                ),
                patch(
                    "rtl_closure.pipeline._read_process_identity",
                    return_value=self._identity(process.pid),
                ),
            ):
                with self.assertRaises(KeyboardInterrupt):
                    start_pipeline(
                        EXAMPLE,
                        generate_only=True,
                        workflow_id="workflow-recover",
                        idempotency_key="request-recover",
                    )
                stranded = next(root.glob("*/state.json"))
                self.assertEqual(json.loads(stranded.read_text())["status"], "created")

                recovered = start_pipeline(
                    EXAMPLE,
                    generate_only=True,
                    workflow_id="workflow-recover",
                    idempotency_key="request-recover",
                )

            self.assertEqual(recovered["status"], "running")
            self.assertEqual(recovered["phase"], "queued")
            self.assertEqual(recovered["pid"], process.pid)
            self.assertEqual(recovered["launch_generation"], 2)
            self.assertEqual(current.read_text().strip(), recovered["run_id"])
            self.assertEqual(len(list(root.glob("*/state.json"))), 1)

    def test_superseded_worker_generation_cannot_mutate_replacement_state(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            current = root / "current"
            captured: dict[str, object] = {}

            def interrupted_popen(*args, **kwargs):
                command = args[0]
                generation_index = command.index("--launch-generation") + 1
                captured["generation"] = int(command[generation_index])
                captured["token"] = kwargs["env"][
                    "RTL_CLOSURE_WORKER_LAUNCH_TOKEN"
                ]
                durable = json.loads(next(root.glob("*/state.json")).read_text())
                self.assertEqual(
                    durable["launch_generation"], captured["generation"]
                )
                self.assertEqual(
                    durable["launch_token_sha256"],
                    hashlib.sha256(str(captured["token"]).encode()).hexdigest(),
                )
                self.assertIsNone(durable["pid"])
                raise KeyboardInterrupt()

            with (
                patch("rtl_closure.pipeline.STATE_ROOT", root),
                patch("rtl_closure.pipeline.CURRENT", current),
                patch(
                    "rtl_closure.pipeline.subprocess.Popen",
                    side_effect=interrupted_popen,
                ),
            ):
                with self.assertRaises(KeyboardInterrupt):
                    start_pipeline(
                        EXAMPLE,
                        generate_only=True,
                        workflow_id="workflow-fence",
                        idempotency_key="request-fence",
                    )

            replacement = self.FakeProcess(4198)
            with (
                patch("rtl_closure.pipeline.STATE_ROOT", root),
                patch("rtl_closure.pipeline.CURRENT", current),
                patch(
                    "rtl_closure.pipeline.subprocess.Popen",
                    return_value=replacement,
                ),
                patch(
                    "rtl_closure.pipeline._read_process_identity",
                    return_value=self._identity(replacement.pid),
                ),
            ):
                recovered = start_pipeline(
                    EXAMPLE,
                    generate_only=True,
                    workflow_id="workflow-fence",
                    idempotency_key="request-fence",
                )
                state_file = Path(recovered["state_file"])
                before = state_file.read_bytes()
                result = worker(
                    recovered["run_id"],
                    int(captured["generation"]),
                    str(captured["token"]),
                )

            self.assertEqual(recovered["launch_generation"], 2)
            self.assertEqual(result, 75)
            self.assertEqual(state_file.read_bytes(), before)

    def test_child_self_registers_after_parent_crashes_post_popen(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            current = root / "current"
            process = self.FakeProcess(4197)
            captured: dict[str, object] = {}

            def fake_popen(*args, **kwargs):
                command = args[0]
                generation_index = command.index("--launch-generation") + 1
                captured["generation"] = int(command[generation_index])
                captured["token"] = kwargs["env"][
                    "RTL_CLOSURE_WORKER_LAUNCH_TOKEN"
                ]
                return process

            with (
                patch("rtl_closure.pipeline.STATE_ROOT", root),
                patch("rtl_closure.pipeline.CURRENT", current),
                patch(
                    "rtl_closure.pipeline.subprocess.Popen", side_effect=fake_popen
                ),
                patch(
                    "rtl_closure.pipeline._read_process_identity",
                    side_effect=KeyboardInterrupt(),
                ),
            ):
                with self.assertRaises(KeyboardInterrupt):
                    start_pipeline(EXAMPLE, generate_only=True)

            state_file = next(root.glob("*/state.json"))
            stranded = json.loads(state_file.read_text())
            self.assertEqual(stranded["status"], "created")
            self.assertEqual(stranded["phase"], "worker_launching")
            self.assertFalse(process.terminated)

            child_identity = self._identity(os.getpid())
            with (
                patch("rtl_closure.pipeline.STATE_ROOT", root),
                patch("rtl_closure.pipeline.CURRENT", current),
                patch(
                    "rtl_closure.pipeline._read_process_identity",
                    return_value=child_identity,
                ),
                patch(
                    "rtl_closure.pipeline._load_frozen_spec",
                    side_effect=KeyboardInterrupt(),
                ),
            ):
                with self.assertRaises(KeyboardInterrupt):
                    worker(
                        stranded["run_id"],
                        int(captured["generation"]),
                        str(captured["token"]),
                    )

            registered = json.loads(state_file.read_text())
            self.assertEqual(registered["status"], "running")
            self.assertEqual(registered["phase"], "queued")
            self.assertEqual(registered["pid"], os.getpid())
            self.assertEqual(registered["worker_process"], child_identity)
            self.assertEqual(current.read_text().strip(), stranded["run_id"])

    def test_stale_launch_caller_cannot_replace_registered_worker(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            current = root / "current"
            process = self.FakeProcess(4196)
            with (
                patch("rtl_closure.pipeline.STATE_ROOT", root),
                patch("rtl_closure.pipeline.CURRENT", current),
                patch(
                    "rtl_closure.pipeline.subprocess.Popen", return_value=process
                ) as popen,
                patch(
                    "rtl_closure.pipeline._read_process_identity",
                    return_value=self._identity(process.pid),
                ),
            ):
                running = start_pipeline(EXAMPLE, generate_only=True)
                stale = {
                    **running,
                    "status": "created",
                    "phase": "worker_launching",
                    "launch_generation": 0,
                    "launch_token_sha256": None,
                    "pid": None,
                    "worker_process": None,
                }
                observed = _launch_pipeline_worker(stale)

            self.assertEqual(popen.call_count, 1)
            self.assertEqual(observed["status"], "running")
            self.assertEqual(observed["phase"], "queued")
            self.assertEqual(observed["launch_generation"], 1)
            self.assertEqual(observed["pid"], process.pid)

    def test_reconcile_recovers_created_worker_launch_without_idempotency_key(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            process = self.FakeProcess(4200)
            with (
                patch("rtl_closure.pipeline.STATE_ROOT", root),
                patch("rtl_closure.pipeline.CURRENT", root / "current"),
                patch(
                    "rtl_closure.pipeline.subprocess.Popen",
                    side_effect=KeyboardInterrupt(),
                ),
            ):
                with self.assertRaises(KeyboardInterrupt):
                    start_pipeline(EXAMPLE, generate_only=True)

            run_id = next(root.glob("*/state.json")).parent.name
            with (
                patch("rtl_closure.pipeline.STATE_ROOT", root),
                patch("rtl_closure.pipeline.CURRENT", root / "current"),
                patch("rtl_closure.pipeline.subprocess.Popen", return_value=process),
                patch(
                    "rtl_closure.pipeline._read_process_identity",
                    return_value=self._identity(process.pid),
                ),
            ):
                recovered = refresh_state(load_state(run_id))

            self.assertEqual(recovered["status"], "running")
            self.assertEqual(recovered["phase"], "queued")
            self.assertEqual(recovered["pid"], process.pid)

    def test_registration_failure_terminates_spawned_worker(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            process = self.FakeProcess(4201)
            with (
                patch("rtl_closure.pipeline.STATE_ROOT", root),
                patch("rtl_closure.pipeline.CURRENT", root / "current"),
                patch("rtl_closure.pipeline.subprocess.Popen", return_value=process),
                patch("rtl_closure.pipeline._read_process_identity", return_value=None),
            ):
                with self.assertRaisesRegex(RuntimeError, "identity could not be attested"):
                    start_pipeline(EXAMPLE, generate_only=True)

            self.assertTrue(process.terminated)
            state_file = next(root.glob("*/state.json"))
            self.assertEqual(json.loads(state_file.read_text())["status"], "failed")

    def test_process_identity_rejects_reused_pid(self):
        expected = self._identity(4301)
        reused = {**expected, "start_time_ticks": expected["start_time_ticks"] + 1}
        with patch("rtl_closure.pipeline._read_process_identity", return_value=reused):
            self.assertFalse(_process_identity_matches(4301, expected))

    def test_completed_refresh_reconciles_and_downgrades_damaged_lane_delivery(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            attempt = {
                "attempt_id": "lane-attempt-001",
                "lane_run_id": "lane-terminal-reconcile",
                "launch_key": "launch-terminal-reconcile",
                "status": "completed",
                "started_at": "2026-01-01T00:00:00+00:00",
                "completed_at": "2026-01-01T00:01:00+00:00",
                "supersedes": None,
            }
            state = self._persist(
                root,
                "run-terminal-reconcile",
                status="completed",
                phase="completed",
                mapped_equivalence_mode="off",
                origin={
                    "source": "rtl-closure-cli",
                    "workflow_id": "workflow-terminal-reconcile",
                    "idempotency_key": None,
                    "idempotency_scope": "rtl-closure-cli",
                    "spec_sha256": "a" * 64,
                },
                lane_run_id="lane-terminal-reconcile",
                active_lane_attempt_id="lane-attempt-001",
                lane_attempts=[attempt],
                lane_state=lane_response(
                    "lane-terminal-reconcile",
                    status="completed",
                    workflow_id="workflow-terminal-reconcile",
                    attempt_id="lane-attempt-001",
                    idempotency_key="launch-terminal-reconcile",
                    flow_scope="full",
                    delivery_gate="pass",
                ),
            )
            child = lane_response(
                "lane-terminal-reconcile",
                status="incomplete",
                workflow_id="workflow-terminal-reconcile",
                attempt_id="lane-attempt-001",
                idempotency_key="launch-terminal-reconcile",
                flow_scope="full",
                delivery_gate="incomplete",
                delivery_gate_reasons=["canonical GDS artifact is missing"],
            )
            response = SimpleNamespace(
                returncode=0, stdout=json.dumps(child), stderr=""
            )
            with patch("rtl_closure.pipeline.STATE_ROOT", root), patch(
                "rtl_closure.pipeline.subprocess.run", return_value=response
            ) as runner:
                refreshed = refresh_state(state)

            self.assertEqual(refreshed["status"], "incomplete")
            self.assertEqual(refreshed["phase"], "lane_delivery_incomplete")
            self.assertEqual(refreshed["lane_state"]["delivery_gate"], "incomplete")
            runner.assert_called_once_with(
                [
                    "lane-agent",
                    "reconcile",
                    "--run-id",
                    "lane-terminal-reconcile",
                    "--json",
                ],
                capture_output=True,
                text=True,
                timeout=20,
                check=False,
            )

    def test_completed_refresh_surfaces_retryable_reconciliation_failure(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            attempt = {
                "attempt_id": "lane-attempt-001",
                "lane_run_id": "lane-terminal-transient",
                "launch_key": "launch-terminal-transient",
                "status": "completed",
                "started_at": "2026-01-01T00:00:00+00:00",
                "completed_at": "2026-01-01T00:01:00+00:00",
                "supersedes": None,
            }
            state = self._persist(
                root,
                "run-terminal-transient",
                status="completed",
                phase="completed",
                mapped_equivalence_mode="off",
                lane_run_id="lane-terminal-transient",
                active_lane_attempt_id="lane-attempt-001",
                lane_attempts=[attempt],
                lane_state=lane_response(
                    "lane-terminal-transient",
                    status="completed",
                    flow_scope="full",
                    delivery_gate="pass",
                ),
            )
            response = SimpleNamespace(
                returncode=1,
                stdout="",
                stderr="lane-agent report temporarily unavailable",
            )
            with patch("rtl_closure.pipeline.STATE_ROOT", root), patch(
                "rtl_closure.pipeline.subprocess.run", return_value=response
            ):
                with self.assertRaises(LaneReconciliationError) as raised:
                    refresh_state(state)

            self.assertTrue(raised.exception.retryable)
            self.assertEqual(raised.exception.failure_gate, "lane_reconcile")
            persisted = json.loads((root / "run-terminal-transient/state.json").read_text())
            self.assertEqual(persisted["status"], "completed")
            self.assertEqual(persisted["phase"], "completed")
            self.assertIsNone(persisted.get("lane_status_error"))

    def test_stale_worker_update_cannot_mutate_terminal_state(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            state = self._persist(
                root,
                "run-terminal-update",
                status="stopped",
                phase="stopped",
                error=None,
            )
            state_file = Path(state["state_file"])
            before = state_file.read_bytes()
            stale = {**state, "status": "running", "phase": "generating"}
            result = _update(
                stale,
                phase="publishing",
                status="failed",
                error="late worker error",
            )
            self.assertEqual(result["status"], "stopped")
            self.assertEqual(result["phase"], "stopped")
            self.assertEqual(state_file.read_bytes(), before)

    def test_reused_worker_pid_is_never_signalled(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            expected = self._identity(4401)
            self._persist(
                root,
                "run-reused-pid",
                pid=4401,
                worker_process=expected,
            )
            reused = {**expected, "start_time_ticks": expected["start_time_ticks"] + 1}
            with (
                patch("rtl_closure.pipeline.STATE_ROOT", root),
                patch("rtl_closure.pipeline._read_process_identity", return_value=reused),
                patch("rtl_closure.pipeline.os.killpg") as killer,
            ):
                result = stop_pipeline("run-reused-pid")
            self.assertEqual(result["status"], "failed")
            killer.assert_not_called()

    def test_matching_worker_identity_is_confirmed_before_stopped(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            expected = self._identity(4501)
            self._persist(
                root,
                "run-live-worker",
                pid=4501,
                worker_process=expected,
            )
            with (
                patch("rtl_closure.pipeline.STATE_ROOT", root),
                patch(
                    "rtl_closure.pipeline._read_process_identity",
                    side_effect=[expected, expected, None, None],
                ),
                patch("rtl_closure.pipeline.os.getpgid", return_value=4501),
                patch("rtl_closure.pipeline.os.killpg") as killer,
            ):
                result = stop_pipeline("run-live-worker")
            self.assertEqual(result["status"], "stopped")
            killer.assert_called_once_with(4501, 15)

    def test_worker_outside_its_expected_process_group_is_not_signalled(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            expected = self._identity(4551)
            self._persist(
                root,
                "run-wrong-process-group",
                pid=4551,
                worker_process=expected,
            )
            with (
                patch("rtl_closure.pipeline.STATE_ROOT", root),
                patch(
                    "rtl_closure.pipeline._read_process_identity",
                    return_value=expected,
                ),
                patch("rtl_closure.pipeline.os.getpgid", return_value=9999),
                patch("rtl_closure.pipeline.os.killpg") as killer,
            ):
                with self.assertRaisesRegex(RuntimeError, "session leader"):
                    stop_pipeline("run-wrong-process-group")
            killer.assert_not_called()

    def test_lane_stop_requires_successful_identity_attested_response(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            workflow_id = "workflow-stop"
            attempt_id = "lane-attempt-001"
            lane_run_id = "lane-stop"
            self._persist(
                root,
                "run-lane-stop",
                lane_run_id=lane_run_id,
                lane_state={"status": "running"},
                active_lane_attempt_id=attempt_id,
                lane_attempts=[
                    {
                        "attempt_id": attempt_id,
                        "lane_run_id": lane_run_id,
                        "status": "running",
                    }
                ],
                origin={"workflow_id": workflow_id},
            )
            running = lane_response(
                lane_run_id,
                workflow_id=workflow_id,
                attempt_id=attempt_id,
            )
            stopped = {**running, "status": "stopped"}
            responses = [
                SimpleNamespace(returncode=0, stdout=json.dumps(running), stderr=""),
                SimpleNamespace(returncode=0, stdout=json.dumps(stopped), stderr=""),
            ]
            with (
                patch("rtl_closure.pipeline.STATE_ROOT", root),
                patch("rtl_closure.pipeline.subprocess.run", side_effect=responses) as runner,
            ):
                result = stop_pipeline("run-lane-stop")
            self.assertEqual(result["status"], "stopped")
            self.assertEqual(result["lane_attempts"][0]["status"], "stopped")
            self.assertIn("--json", runner.call_args_list[1].args[0])

    def test_lane_stop_failure_does_not_claim_stopped(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            lane_run_id = "lane-stop-failed"
            self._persist(
                root,
                "run-lane-stop-failed",
                lane_run_id=lane_run_id,
                lane_state={"status": "running"},
            )
            running = lane_response(lane_run_id)
            responses = [
                SimpleNamespace(returncode=0, stdout=json.dumps(running), stderr=""),
                SimpleNamespace(returncode=3, stdout="", stderr="stop failed"),
            ]
            with (
                patch("rtl_closure.pipeline.STATE_ROOT", root),
                patch("rtl_closure.pipeline.subprocess.run", side_effect=responses),
            ):
                with self.assertRaisesRegex(RuntimeError, "stop failed"):
                    stop_pipeline("run-lane-stop-failed")
                persisted = load_state("run-lane-stop-failed")
            self.assertEqual(persisted["status"], "running")

    def test_publication_corruption_does_not_block_stopping_identified_child(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            lane_run_id = "lane-publication-corrupt"
            self._persist(
                root,
                "run-publication-corrupt",
                lane_run_id=lane_run_id,
                lane_state={"run_id": lane_run_id, "status": "running"},
                publication_bundle_sha256="a" * 64,
                design_config=str(root / "missing-publication" / "config.yaml"),
            )
            response = SimpleNamespace(
                returncode=0,
                stdout=json.dumps(lane_response(lane_run_id, status="stopped")),
                stderr="",
            )
            with (
                patch("rtl_closure.pipeline.STATE_ROOT", root),
                patch("rtl_closure.pipeline.subprocess.run", return_value=response) as runner,
            ):
                stopped = stop_pipeline("run-publication-corrupt")

            self.assertEqual(stopped["status"], "incomplete")
            self.assertEqual(
                stopped["failure_code"], "release_integrity/publication_invalid"
            )
            self.assertEqual(stopped["lane_state"]["status"], "stopped")
            self.assertIn("lane-agent", runner.call_args.args[0])

    def test_lane_retry_launch_failure_returns_parent_to_terminal_state(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            run_id = "run-retry-launch-failed"
            run_dir = root / run_id
            config = run_dir / "config.yaml"
            spec = load_spec(EXAMPLE)
            state = self._persist(
                root,
                run_id,
                status="failed",
                phase="failed",
                design_config=str(config),
                origin={
                    "workflow_id": "workflow-retry",
                    "spec_sha256": content_hash(spec.raw),
                },
                failure_class="old_failure",
                failure_gate="old_gate",
                failure_code="old_failure/old_gate",
            )
            config.write_text("meta: {}\n", encoding="utf-8")
            Path(state["spec_file"]).write_text(
                json.dumps(spec.raw), encoding="utf-8"
            )
            response = SimpleNamespace(
                returncode=2,
                stdout="",
                stderr="Lane backend unavailable",
            )
            with (
                patch("rtl_closure.pipeline.STATE_ROOT", root),
                patch("rtl_closure.pipeline.subprocess.run", return_value=response),
            ):
                with self.assertRaisesRegex(LaneLaunchError, "backend unavailable"):
                    retry_lane(run_id)
                persisted = load_state(run_id)
            self.assertEqual(persisted["status"], "failed")
            self.assertEqual(persisted["phase"], "failed")
            self.assertEqual(persisted["failure_code"], "infrastructure/lane_launch")

    def test_lane_retry_request_reuses_unknown_launch(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            run_id = "run-retry-idempotent"
            run_dir = root / run_id
            config = run_dir / "config.yaml"
            spec = load_spec(EXAMPLE)
            state = self._persist(
                root,
                run_id,
                status="failed",
                phase="failed",
                design_config=str(config),
                origin={
                    "workflow_id": "workflow-retry",
                    "spec_sha256": content_hash(spec.raw),
                },
                failure_class="release_integrity",
                failure_gate="lane_input_binding",
                failure_code="release_integrity/lane_input_mismatch",
            )
            config.write_text("meta: {}\n", encoding="utf-8")
            Path(state["spec_file"]).write_text(
                json.dumps(spec.raw), encoding="utf-8"
            )
            launch_count = 0

            def launch(command: list[str], **_kwargs: object) -> SimpleNamespace:
                nonlocal launch_count
                if command[1] == "reconcile":
                    active = load_state(run_id)
                    launch_key = active["lane_attempts"][-1]["launch_key"]
                    return SimpleNamespace(
                        returncode=0,
                        stdout=json.dumps(
                            lane_response(
                                "lane-retry-idempotent",
                                workflow_id="workflow-retry",
                                attempt_id="lane-attempt-001",
                                idempotency_key=launch_key,
                            )
                        ),
                        stderr="",
                    )
                launch_count += 1
                if launch_count == 1:
                    return SimpleNamespace(
                        returncode=2,
                        stdout="",
                        stderr="response serialization failed after launch",
                    )
                launch_key = command[command.index("--idempotency-key") + 1]
                return SimpleNamespace(
                    returncode=0,
                    stdout=json.dumps(
                        lane_response(
                            "lane-retry-idempotent",
                            workflow_id="workflow-retry",
                            attempt_id="lane-attempt-001",
                            idempotency_key=launch_key,
                        )
                    ),
                    stderr="",
                )

            with (
                patch("rtl_closure.pipeline.STATE_ROOT", root),
                patch("rtl_closure.pipeline.subprocess.run", side_effect=launch) as runner,
            ):
                with self.assertRaises(LaneLaunchError):
                    retry_lane(run_id, idempotency_key="workflow-retry:retry-1")
                current = retry_lane(
                    run_id, idempotency_key="workflow-retry:retry-1"
                )
                repeated = retry_lane(
                    run_id, idempotency_key="workflow-retry:retry-1"
                )

            first_command = runner.call_args_list[0].args[0]
            second_command = runner.call_args_list[1].args[0]
            first_key = first_command[first_command.index("--idempotency-key") + 1]
            second_key = second_command[second_command.index("--idempotency-key") + 1]
            self.assertEqual(first_key, second_key)
            self.assertEqual(launch_count, 2)
            self.assertEqual(len(current["lane_attempts"]), 1)
            self.assertEqual(current["lane_attempts"][0]["launch_retries"], 1)
            self.assertEqual(repeated["lane_run_id"], "lane-retry-idempotent")
            self.assertEqual(
                repeated["lane_retry_requests"][0]["status"], "completed"
            )
            self.assertIsNone(current["failure_class"])
            self.assertIsNone(current["failure_gate"])
            self.assertIsNone(current["failure_code"])

    def test_lane_start_rejects_mismatched_workflow_identity(self):
        spec = load_spec(EXAMPLE)
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            config = root / "config.yaml"
            config.write_text("meta: {}\n", encoding="utf-8")
            state = {
                "state_file": str(root / "state.json"),
                "events_file": str(root / "events.jsonl"),
                "event_sequence": 0,
                "run_id": "run-lane-start-identity",
                "status": "running",
                "phase": "restarting_lane",
                "design_config": str(config),
                "origin": {"workflow_id": "workflow-expected"},
            }
            save_state(state)
            response = SimpleNamespace(
                returncode=0,
                stdout=json.dumps(
                    lane_response(
                        "lane-created",
                        workflow_id="workflow-other",
                        attempt_id="lane-attempt-001",
                    )
                ),
                stderr="",
            )
            with patch("rtl_closure.pipeline.subprocess.run", return_value=response):
                with self.assertRaisesRegex(LaneLaunchError, "mismatched workflow_id"):
                    _start_lane_unlocked(spec, state)
            persisted = json.loads((root / "state.json").read_text())
            self.assertIsNone(persisted.get("lane_run_id"))
            self.assertEqual(persisted["lane_attempts"][0]["status"], "launch_unknown")

    def test_nonzero_lane_reconcile_cannot_smuggle_success_json(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            state = self._persist(
                root,
                "run-reconcile-error",
                lane_run_id="lane-reconcile-error",
                lane_state={"status": "running"},
            )
            response = SimpleNamespace(
                returncode=1,
                stdout=json.dumps(
                    lane_response(
                        "lane-reconcile-error",
                        status="completed",
                        flow_scope="full",
                        delivery_gate="pass",
                    )
                ),
                stderr="backend failed",
            )
            with patch("rtl_closure.pipeline.subprocess.run", return_value=response):
                result = refresh_state(state)
            self.assertEqual(result["status"], "running")
            self.assertEqual(result["phase"], "lane_status_unknown")
            self.assertEqual(result["lane_state"]["status"], "running")
            self.assertEqual(result["lane_status_error"], "backend failed")

    def test_lane_reconcile_rejects_another_run_identity(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            state = self._persist(
                root,
                "run-reconcile-identity",
                lane_run_id="lane-expected",
                lane_state={"status": "running"},
            )
            response = SimpleNamespace(
                returncode=0,
                stdout=json.dumps(lane_response("lane-other", status="completed")),
                stderr="",
            )
            with patch("rtl_closure.pipeline.subprocess.run", return_value=response):
                result = refresh_state(state)
            self.assertEqual(result["status"], "running")
            self.assertEqual(result["lane_state"]["status"], "running")
            self.assertIn("belongs to another run", result["lane_status_error"])


class PublishTests(unittest.TestCase):
    def setUp(self):
        self._verify_binding = patch(
            "rtl_closure.runtime._bound_verify_image", None
        )
        self._sandbox_binding = patch(
            "rtl_closure.runtime._bound_sandbox_image", None
        )
        self._verify_binding.start()
        self._sandbox_binding.start()
        self.addCleanup(self._sandbox_binding.stop)
        self.addCleanup(self._verify_binding.stop)

    def _release_state(self, spec, root: Path, rtl: str, oracle: str):
        verify_identity = {
            "schema_version": 1,
            "configured_reference": VERIFY_IMAGE,
            "image_id": "sha256:" + "1" * 64,
            "immutable_reference": "sha256:" + "1" * 64,
            "repo_digests": [],
        }
        sandbox_identity = {
            "schema_version": 1,
            "configured_reference": SANDBOX_IMAGE,
            "image_id": "sha256:" + "2" * 64,
            "immutable_reference": "sha256:" + "2" * 64,
            "repo_digests": [],
        }
        bind_verify_image(verify_identity)
        bind_sandbox_image(sandbox_identity)
        verify_ref = verify_image_ref()
        sandbox_ref = sandbox_image_ref()
        contract = {"schema_version": 1, "requirements": []}
        plan = {
            "schema_version": 1,
            "clock": {
                "port": spec.clock_port,
                "period_ns": spec.clock_period_ns,
                "sample_phase": "post_rising_edge",
            },
            "reset": spec.reset,
            "reset_cycles": 3,
            "scenarios": [
                {
                    "id": "TEST",
                    "requirement_ids": [],
                    "reset_before": True,
                    "cycles": [{"drive": {"enable": 0}}],
                }
            ],
        }
        coverage = {
            "metric": "oracle_line_coverage",
            "coverage_type": "python_source_line",
            "scope": "TopModule executable statement lines",
            "executable_lines": [1],
            "executed_lines": [1],
            "missing_lines": [],
            "covered": 1,
            "total": 1,
            "percent": 100.0,
        }
        expected = {
            "trace": [
                {
                    "ordinal": 0,
                    "scenario_index": 0,
                    "scenario_id": "TEST",
                    "requirement_ids": [],
                    "cycle": 0,
                    "inputs": {"rst_n": 1, "enable": 0},
                    "expected": {"count": 0},
                }
            ],
            "setup_observations": [
                {
                    "scenario_index": 0,
                    "scenario_id": "TEST",
                    "requirement_ids": [],
                    "inputs": {"rst_n": 1, "enable": 0},
                    "prime_asserted_edge": {"count": 0},
                    "prime_deasserted_edge": {"count": 0},
                    "dirty_edges": [{"count": 0}, {"count": 0}],
                    "asynchronous_assertion": None,
                    "asserted_edges": [
                        {"count": 0},
                        {"count": 0},
                        {"count": 0},
                    ],
                    "deasserted_edge": {"count": 0},
                }
            ],
            "testplan_sha256": content_hash(plan),
            "oracle_sha256": content_hash(oracle),
            "oracle_line_coverage": coverage,
        }
        manifest = {
            "schema_version": 1,
            "contract_sha256": content_hash(contract),
            "testplan_sha256": content_hash(plan),
            "oracle_sha256": content_hash(oracle),
            "expected_trace_sha256": content_hash(expected),
        }
        manifest["bundle_sha256"] = content_hash(manifest)
        suite = suite_hash(plan, expected)
        origins = []
        for index in (1, 2):
            response = f"candidate response {index}:\n{oracle}"
            origins.append(
                {
                    "origin_id": f"candidate-{index:02d}",
                    "candidate_index": index,
                    "response_sha256": content_hash(response),
                    "oracle_sha256": content_hash(oracle),
                    "response": response,
                    "oracle": oracle,
                    "initial_valid": True,
                    "initial_behavior_sha256": _trace_behavior_hash(expected),
                    "initial_error": None,
                    "provenance": {
                        "provider": f"fixture-provider-{index}",
                        "model": "fixture-oracle",
                        "source_id": content_hash(
                            {
                                "provider": f"fixture-provider-{index}",
                                "model": "fixture-oracle",
                            }
                        ),
                        "declared": True,
                    },
                }
            )
        origin_bundle = {"schema_version": 2, "origins": origins}
        behavior_hash = _trace_behavior_hash(expected)
        quorum = {
            "schema_version": 2,
            "quorum_met": True,
            "reviewer_counted": False,
            "final_testplan_sha256": content_hash(plan),
            "selected_oracle_sha256": content_hash(oracle),
            "selected_expected_trace_sha256": content_hash(expected),
            "consensus_behavior_sha256": behavior_hash,
            "consensus_count": 2,
            "quorum_origin_ids": ["candidate-01", "candidate-02"],
            "valid_candidate_count": 2,
            "origin_bundle_sha256": content_hash(origin_bundle),
            "origin_results": [
                {
                    "origin_id": f"candidate-{index:02d}",
                    "candidate_index": index,
                    "oracle_sha256": content_hash(oracle),
                    "valid": True,
                    "behavior_sha256": behavior_hash,
                    "error": None,
                }
                for index in (1, 2)
            ],
        }
        formal_property_rows, formal_properties, formal_selection = (
            build_formal_evidence_contract(
                spec,
                plan,
                expected,
                spec.verification["formal_depth"],
            )
        )
        formal_batches = []
        formal_execution_entries = []
        for batch_index, start in enumerate(
            range(0, len(formal_properties), FORMAL_BATCH_SIZE), 1
        ):
            count = min(FORMAL_BATCH_SIZE, len(formal_properties) - start)
            script = render_formal_batch_script(
                spec, formal_property_rows[start : start + count]
            )
            formal_log = (
                "SAT proof finished - no model found: SUCCESS!\n" * count
            )
            formal_batches.append(
                {
                    "batch_index": batch_index,
                    "status": "pass",
                    "property_start_index": start + 1,
                    "property_end_index": start + count,
                    "property_count": count,
                    "artifact": str(root / "formal" / f"batch-{batch_index:04d}"),
                    "error": None,
                    "infrastructure_status": None,
                    "returncode": 0,
                    "success_verdict_count": count,
                }
            )
            formal_execution_entries.append(
                {
                    "batch_index": batch_index,
                    "status": "pass",
                    "executed": True,
                    "returncode": 0,
                    "property_start_index": start + 1,
                    "property_end_index": start + count,
                    "property_count": count,
                    "success_verdict_count": count,
                    "dut_sha256": hashlib.sha256((rtl + "\n").encode()).hexdigest(),
                    "script_sha256": hashlib.sha256(script.encode()).hexdigest(),
                    "log_sha256": hashlib.sha256(formal_log.encode()).hexdigest(),
                    "script": script,
                    "log": formal_log,
                }
            )
        formal_execution = {
            "schema_version": 2,
            "provider": "yosys_bounded_trace_execution",
            "engine": "yosys_internal_sat",
            "verify_image": verify_ref,
            "rtl_sha256": content_hash(rtl),
            "suite_sha256": suite,
            "property_map_sha256": content_hash(formal_properties),
            "batch_size": FORMAL_BATCH_SIZE,
            "minimum_batch_size": FORMAL_MIN_BATCH_SIZE,
            "batch_count": len(formal_batches),
            "reschedule_count": 0,
            "reschedules": [],
            "entries": formal_execution_entries,
        }
        formal_execution["evidence_sha256"] = content_hash(formal_execution)
        formal = {
            "status": "pass",
            "provider": "yosys_bounded_trace",
            "authoritative": True,
            "detail": "all formal batches completed",
            "property_count": len(formal_properties),
            "passed_property_ids": [
                row["property_id"] for row in formal_properties
            ],
            "counterexamples": [],
            "depth": spec.verification["formal_depth"],
            "infrastructure_errors_present": False,
            "infrastructure_error_count": 0,
            "infrastructure_statuses": [],
            "rtl_sha256": content_hash(rtl),
            "suite_sha256": suite,
            "property_map_sha256": content_hash(formal_properties),
            "property_selection": formal_selection,
            "eligible_property_count": formal_selection["eligible"],
            "selected_property_count": formal_selection["selected"],
            "excluded_property_count": formal_selection["excluded"],
            "properties_truncated": formal_selection["truncated"],
            "adaptive_selected_property_count": formal_selection[
                "adaptive_selected"
            ],
            "verification_method": "bounded_trace_checking",
            "proof_scope": "supported_unique_frozen_input_trace_obligations",
            "universal_proof": False,
            "selection_complete": True,
            "batch_size": FORMAL_BATCH_SIZE,
            "minimum_batch_size": FORMAL_MIN_BATCH_SIZE,
            "batch_count": len(formal_batches),
            "reschedule_count": 0,
            "reschedules": [],
            "completed_batch_count": len(formal_batches),
            "batches": formal_batches,
            "errors": [],
            "error": None,
            "execution_complete": True,
            "evidence_complete": True,
            "completeness": "complete_for_all_unique_bounded_trace_obligations",
            "engine": "yosys_internal_sat",
            "verify_image": verify_ref,
            "execution_evidence_sha256": formal_execution["evidence_sha256"],
            "artifact": str(root / "formal-report.json"),
        }
        architecture = {
            "schema_version": 1,
            "status": "not_requested",
            "provider": "yosys_structural_architecture_audit",
            "top": spec.name,
            "rtl_sha256": content_hash(rtl),
            "constraints_sha256": content_hash(spec.architecture),
            "constraints": spec.architecture,
            "sandbox_image": sandbox_ref,
            "observed": None,
            "checks": [],
        }
        universal = {
            "schema_version": 2,
            "status": "not_requested",
            "provider": "yosys_sat_universal_properties",
            "verify_image": verify_ref,
            "authoritative": True,
            "authoritative_for": "listed_port_level_safety_properties_only",
            "complete_behavioral_proof": False,
            "complete_requirement_proof": False,
            "top": spec.name,
            "clock_port": spec.clock_port,
            "scope": universal_proof_scope(
                induction_depth=spec.verification["universal_induction_depth"],
                reset=spec.reset,
            ),
            "rtl_sha256": content_hash(rtl),
            "properties_sha256": content_hash(
                spec.verification["universal_properties"]
            ),
            "property_count": 0,
            "universal_proof": False,
            "passed_property_count": 0,
            "passed_property_ids": [],
            "failed_property_ids": [],
            "unknown_property_ids": [],
            "error_property_ids": [],
            "properties": [],
            "requirement_coverage": universal_requirement_coverage(
                [], [row["id"] for row in spec.verification["requirements"]]
            ),
        }
        universal_execution = {
            "schema_version": 1,
            "provider": "yosys_sat_universal_properties",
            "verify_image": verify_ref,
            "top": spec.name,
            "rtl_sha256": content_hash(rtl),
            "dut": rtl + "\n",
            "dut_sha256": hashlib.sha256((rtl + "\n").encode()).hexdigest(),
            "properties_sha256": content_hash(
                spec.verification["universal_properties"]
            ),
            "property_count": 0,
            "entries": [],
        }
        universal_execution["evidence_sha256"] = content_hash(
            universal_execution
        )
        equivalence = {
            "schema_version": 3,
            "status": "pass",
            "provider": "yosys_generic_synthesis_equivalence",
            "scope": "post_reset_observable_outputs_rtl_vs_generic_synthesized_netlist",
            "top": spec.name,
            "clock_port": spec.clock_port,
            "reset": spec.reset,
            "reset_base_case": {
                "status": "pass",
                "asserted_samples": 2,
                "post_reset_samples": 8,
                "sampling": "rising_clock_edge",
            },
            "sequential_induction_depth": 8,
            "comparison_semantics": {
                "points": "top_level_output_bits_only",
                "observable_output_bits": sum(
                    port.width for port in spec.ports if port.direction == "output"
                ),
                "output_ports": sorted(
                    port.name for port in spec.ports if port.direction == "output"
                ),
                "supporting_internal_match_names": 1,
                "internal_state": (
                    "supporting_inductive_relation_not_a_release_obligation"
                ),
                "reachable_state_base_proven": True,
            },
            "equivalence_cells": 12,
            "proven_cells": 12,
            "rtl_sha256": content_hash(rtl),
            "sandbox_image": sandbox_ref,
        }
        independent_num_tests = spec.generation["independent_verify_tests"]
        independent_trace = [{"count": 0} for _ in range(independent_num_tests)]

        def independent_run(index: int, offset: int) -> dict[str, object]:
            seed = spec.generation["seed"] + offset
            return {
                "run": index,
                "seed": seed,
                "num_tests": independent_num_tests,
                "passed": True,
                "verification": {
                    "match_rate": 1.0,
                    "num_tests": independent_num_tests,
                    "total_checks": independent_num_tests,
                    "mismatched": 0,
                    "mismatches": [],
                    "sv_error": None,
                    "py_error": None,
                    "meta_error": None,
                    "stimuli": derive_independent_stimuli(
                        spec, num_tests=independent_num_tests, seed=seed
                    ),
                    "verilog_trace": independent_trace,
                    "python_trace": independent_trace,
                },
            }

        independent_runs = [
            independent_run(index, offset)
            for index, offset in enumerate((1009, 2027), 1)
        ]
        evidence = {
            "contract.json": contract,
            "testplan.json": plan,
            "expected-trace.json": expected,
            "frozen-manifest.json": manifest,
            "oracle-origins.json": origin_bundle,
            "oracle-quorum.json": quorum,
            "final-suite-quorum.json": quorum,
            "verification-result.json": {
                "passed": True,
                "verify_image": verify_ref,
                "rtl_sha256": content_hash(rtl),
                "suite_sha256": suite,
                "formal": formal,
                "universal_properties": universal,
            },
            "repair-history.json": [],
            "counterexamples.json": [],
            "coverage-refinement.json": {
                "schema_version": 2,
                "status": "passed",
                "mode": spec.verification["oracle_line_coverage_mode"],
                "metric": ORACLE_COVERAGE_METRIC,
                "rtl_functional_coverage": False,
                "limitation": ORACLE_COVERAGE_LIMITATION,
                "target_met": True,
                "stop_reason": "target_met",
                "warning": None,
                "error": None,
                "target_percent": spec.verification[
                    "oracle_coverage_target_pct"
                ],
                "initial": coverage,
                "final": coverage,
                "rounds": [],
                "remaining_cycle_budget": spec.verification[
                    "oracle_coverage_cycle_budget"
                ],
                "added_scenario_count": 0,
                "final_testplan_sha256": content_hash(plan),
                "final_expected_trace_sha256": content_hash(expected),
                "final_oracle_sha256": content_hash(oracle),
                "final_oracle_quorum_sha256": content_hash(quorum),
                "evidence_bindings": {
                    "testplan_sha256": content_hash(plan),
                    "expected_trace_sha256": content_hash(expected),
                    "oracle_sha256": content_hash(oracle),
                },
            },
            "adaptive-corpus.json": {
                "schema_version": 2,
                "base_suite_sha256": suite,
                "active_suite_sha256": suite,
                "chain_head_sha256": None,
                "epochs": [],
            },
            "independent-verification.json": {
                "schema_version": 3,
                "status": "pass",
                "sandbox_image": sandbox_ref,
                "rtl_sha256": content_hash(rtl),
                "oracle_sha256": content_hash(oracle),
                "active_suite_sha256": suite,
                "max_repair_rounds": spec.verification["max_repair_rounds"],
                "runs": independent_runs,
                "attempts": [
                    {
                        "round": 0,
                        "candidate": None,
                        "rtl_sha256": content_hash(rtl),
                        "passed": True,
                        "runs": independent_runs,
                    }
                ],
                "counterexamples": [],
                "repair_history": [],
            },
            "yosys-check.log": "ok\n",
            "architecture-audit.json": architecture,
            "universal-properties.json": universal,
            "universal-execution-evidence.json": universal_execution,
            "synthesis-equivalence.json": equivalence,
            "synthesis-equivalence.log": (
                "SAT proof finished - no model found: SUCCESS!\n"
                "equivalence successfully proven\n"
            ),
            "formal-report.json": formal,
            "formal-properties.json": formal_properties,
            "formal-execution-evidence.json": formal_execution,
            "verilator-lint.json": {
                "status": "pass",
                "rtl_sha256": content_hash(rtl),
                "warning_counts": {},
                "quality_gate": {
                    "status": "pass",
                    "blocking_warning_codes": [],
                    "policy": "generated_rtl_semantic_and_structural_warnings_v1",
                },
            },
        }
        for name, value in evidence.items():
            path = root / name
            if name.endswith(".json"):
                path.write_text(json.dumps(value), encoding="utf-8")
            else:
                path.write_text(str(value), encoding="utf-8")
        required = _required_release_artifacts(spec)
        release_gate = {
            "passed": True,
            "rtl_sha256": content_hash(rtl),
            "oracle_sha256": content_hash(oracle),
            "frozen_bundle_sha256": manifest["bundle_sha256"],
            "formal": formal,
            "architecture_audit": architecture,
            "universal_properties": universal,
            "synthesis_equivalence": equivalence,
            "active_suite_sha256": suite,
            "adaptive_chain_head_sha256": None,
            "verify_image_identity": verify_identity,
            "sandbox_image_identity": sandbox_identity,
            "required_artifacts": list(required),
            "artifact_hashes": {
                name: _artifact_hash(root / name) for name in required
            },
        }
        return {
            "schema_version": 2,
            "run_id": "20260720-120000-rtl_closure_counter",
            "run_dir": str(root),
            "model": {"profile": "api", "model": "test"},
            "verification": {"passed": True, "suite_sha256": suite},
            "verify_image_identity": verify_identity,
            "sandbox_image_identity": sandbox_identity,
            "release_gate": release_gate,
        }

    def test_state_id_cannot_escape_state_root(self):
        with self.assertRaises(RuntimeError):
            load_state("../../foreign-project/state")

    def test_generate_only_rejects_required_mapped_equivalence(self):
        raw = json.loads(EXAMPLE.read_text(encoding="utf-8"))
        raw.setdefault("flow", {})["mapped_equivalence_mode"] = "required"
        with tempfile.TemporaryDirectory() as temp:
            spec_path = Path(temp) / "required-eqy.json"
            spec_path.write_text(json.dumps(raw), encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "cannot satisfy required"):
                start_pipeline(spec_path, generate_only=True)

    def test_publish_is_independent_and_read_only(self):
        spec = load_spec(EXAMPLE)
        rtl = "module rtl_closure_counter(input wire clk); endmodule"
        python_model = "class TopModule: pass"
        result = {"matched": True, "best_match_rate": 1.0}
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            run_dir = root / "run"
            run_dir.mkdir()
            state = self._release_state(spec, run_dir, rtl, python_model)
            expected = json.loads((run_dir / "expected-trace.json").read_text())
            quorum = json.loads((run_dir / "final-suite-quorum.json").read_text())
            with patch(
                "rtl_closure.pipeline.DESIGN_ROOT", root / "designs"
            ), patch(
                "rtl_closure.pipeline.revalidate_oracle_bundle_quorum",
                return_value=(expected, quorum),
            ):
                slug, config = publish_design(spec, state, rtl, python_model, result)
            self.assertTrue(config.is_file())
            self.assertTrue((config.parent / "runs").is_dir())
            self.assertEqual(config.stat().st_mode & 0o222, 0)
            pin_order = config.parent / "pin_order.cfg"
            self.assertTrue(pin_order.is_file())
            self.assertEqual(pin_order.stat().st_mode & 0o222, 0)
            self.assertIn("count\\[\\d+\\]", pin_order.read_text())
            self.assertIn(
                "IO_PIN_ORDER_CFG: dir::pin_order.cfg",
                config.read_text(),
            )
            provenance = json.loads(
                (config.parent / "rtl_closure-provenance.json").read_text()
            )
            publication = json.loads(
                (config.parent / "publication-manifest.json").read_text()
            )
            state.update(
                {
                    "design_name": spec.name,
                    "design_config": str(config),
                    "publication_manifest": str(
                        config.parent / "publication-manifest.json"
                    ),
                    "publication_bundle_sha256": publication[
                        "publication_bundle_sha256"
                    ],
                    "lane_input_bundle_sha256": publication[
                        "lane_input_bundle_sha256"
                    ],
                }
            )
            self.assertEqual(_verify_publication(state), publication)
            self.assertTrue(
                {
                    "config.yaml",
                    "src/rtl_closure_counter.sv",
                    "constraints.sdc",
                    "pin_order.cfg",
                    "spec.json",
                    "reference_model.py",
                    "rtl_closure-provenance.json",
                    "verification/verification-result.json",
                    "verification/formal-report.json",
                }.issubset({row["path"] for row in publication["files"]})
            )
            self.assertEqual(publication["schema_version"], 2)
            lane_state = {
                "schema_version": 6,
                "run_id": "lane-1",
                "status": "running",
                "input_manifest": publication["lane_input_manifest"],
                "input_bundle_sha256": publication["lane_input_bundle_sha256"],
            }
            self.assertEqual(
                _validate_lane_state_identity(
                    lane_state,
                    expected_run_id="lane-1",
                    expected_workflow_id=None,
                    expected_attempt_id=None,
                    expected_input_manifest=publication["lane_input_manifest"],
                    expected_input_bundle_sha256=publication[
                        "lane_input_bundle_sha256"
                    ],
                ),
                lane_state,
            )
        self.assertEqual(provenance["generator"], "rtl-closure")
        self.assertTrue(provenance["release_gate"]["passed"])
        self.assertTrue(slug.startswith("rtl_closure_counter_"))

    def test_publish_binds_independent_seeds_limits_and_complete_traces(self):
        spec = load_spec(EXAMPLE)
        rtl = "module rtl_closure_counter(input wire clk); endmodule"
        oracle = "class TopModule: pass"

        def wrong_seed(evidence):
            evidence["runs"][0]["seed"] += 1
            evidence["attempts"][0]["runs"][0]["seed"] += 1

        def wrong_round_limit(evidence):
            evidence["max_repair_rounds"] += 1

        def incomplete_trace(evidence):
            evidence["runs"][0]["verification"]["python_trace"].pop()
            evidence["attempts"][0]["runs"][0]["verification"][
                "python_trace"
            ].pop()

        def mutate_first_run(evidence, mutate):
            mutate(evidence["runs"][0]["verification"])
            mutate(evidence["attempts"][0]["runs"][0]["verification"])

        def wrong_stimulus(evidence):
            mutate_first_run(
                evidence,
                lambda verification: verification["stimuli"][0].__setitem__(
                    "enable", 1 - verification["stimuli"][0]["enable"]
                ),
            )

        def missing_input(evidence):
            mutate_first_run(
                evidence,
                lambda verification: verification["stimuli"][0].pop("enable"),
            )

        def out_of_range_input(evidence):
            mutate_first_run(
                evidence,
                lambda verification: verification["stimuli"][0].__setitem__(
                    "enable", 2
                ),
            )

        def wrong_stimulus_order(evidence):
            mutate_first_run(
                evidence,
                lambda verification: verification["stimuli"].reverse(),
            )

        def none_output(evidence):
            def mutate(verification):
                verification["verilog_trace"][0]["count"] = None
                verification["python_trace"][0]["count"] = None

            mutate_first_run(evidence, mutate)

        def missing_output(evidence):
            def mutate(verification):
                verification["verilog_trace"][0].pop("count")
                verification["python_trace"][0].pop("count")

            mutate_first_run(evidence, mutate)

        def out_of_range_output(evidence):
            def mutate(verification):
                verification["verilog_trace"][0]["count"] = 256
                verification["python_trace"][0]["count"] = 256

            mutate_first_run(evidence, mutate)

        for label, mutate in (
            ("seed", wrong_seed),
            ("max-rounds", wrong_round_limit),
            ("trace", incomplete_trace),
            ("stimulus", wrong_stimulus),
            ("input-key", missing_input),
            ("input-range", out_of_range_input),
            ("stimulus-order", wrong_stimulus_order),
            ("output-none", none_output),
            ("output-key", missing_output),
            ("output-range", out_of_range_output),
        ):
            with self.subTest(label=label), tempfile.TemporaryDirectory() as temp:
                root = Path(temp)
                state = self._release_state(spec, root, rtl, oracle)
                path = root / "independent-verification.json"
                evidence = json.loads(path.read_text())
                mutate(evidence)
                path.write_text(json.dumps(evidence))
                state["release_gate"]["artifact_hashes"][path.name] = _artifact_hash(
                    path
                )
                expected = json.loads((root / "expected-trace.json").read_text())
                quorum = json.loads((root / "final-suite-quorum.json").read_text())
                with patch(
                    "rtl_closure.pipeline.DESIGN_ROOT", root / "designs"
                ), patch(
                    "rtl_closure.pipeline.revalidate_oracle_bundle_quorum",
                    return_value=(expected, quorum),
                ):
                    with self.assertRaisesRegex(RuntimeError, "independent"):
                        publish_design(
                            spec,
                            state,
                            rtl,
                            oracle,
                            {"matched": True, "best_match_rate": 1.0},
                        )

    def test_publication_manifest_detects_rtl_tampering(self):
        spec = load_spec(EXAMPLE)
        rtl = "module rtl_closure_counter(input wire clk); endmodule"
        oracle = "class TopModule: pass"
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            run_dir = root / "run"
            run_dir.mkdir()
            state = self._release_state(spec, run_dir, rtl, oracle)
            expected = json.loads((root / "run" / "expected-trace.json").read_text())
            quorum = json.loads((root / "run" / "final-suite-quorum.json").read_text())
            with patch(
                "rtl_closure.pipeline.DESIGN_ROOT", root / "designs"
            ), patch(
                "rtl_closure.pipeline.revalidate_oracle_bundle_quorum",
                return_value=(expected, quorum),
            ):
                _, config = publish_design(
                    spec,
                    state,
                    rtl,
                    oracle,
                    {"matched": True, "best_match_rate": 1.0},
                )
            manifest = json.loads(
                (config.parent / "publication-manifest.json").read_text()
            )
            attested = {
                **state,
                "design_name": spec.name,
                "design_config": str(config),
                "publication_manifest": str(
                    config.parent / "publication-manifest.json"
                ),
                "publication_bundle_sha256": manifest[
                    "publication_bundle_sha256"
                ],
                "lane_input_bundle_sha256": manifest["lane_input_bundle_sha256"],
            }
            rtl_path = config.parent / "src" / f"{spec.name}.sv"
            rtl_path.chmod(0o644)
            rtl_path.write_text(rtl + "\n// tampered\n", encoding="utf-8")
            with self.assertRaisesRegex(RuntimeError, "changed after release"):
                _verify_publication(attested)
            state_file = run_dir / "state.json"
            pipeline_log = run_dir / "pipeline.log"
            pipeline_log.write_text("", encoding="utf-8")
            attested.update(
                {
                    "state_file": str(state_file),
                    "events_file": str(run_dir / "events.jsonl"),
                    "pipeline_log": str(pipeline_log),
                    "spec_file": str(run_dir / "spec.json"),
                    "event_sequence": 0,
                    "revision": 0,
                    "status": "completed",
                    "phase": "completed",
                    "started_epoch": 1,
                    "lane_state": {"status": "completed", "delivery_gate": "pass"},
                }
            )
            save_state(attested)

            degraded = refresh_state(attested)

            self.assertEqual(degraded["status"], "incomplete")
            self.assertEqual(
                degraded["failure_code"],
                "release_integrity/publication_invalid",
            )
            self.assertEqual(refresh_state(degraded)["status"], "incomplete")
            persisted = json.loads(state_file.read_text(encoding="utf-8"))
            self.assertEqual(persisted["status"], "incomplete")

    def test_publication_manifest_detects_reference_and_evidence_tampering(self):
        spec = load_spec(EXAMPLE)
        rtl = "module rtl_closure_counter(input wire clk); endmodule"
        oracle = "class TopModule: pass"
        for relative in (
            "reference_model.py",
            "verification/verification-result.json",
        ):
            with self.subTest(relative=relative), tempfile.TemporaryDirectory() as temp:
                root = Path(temp)
                run_dir = root / "run"
                run_dir.mkdir()
                state = self._release_state(spec, run_dir, rtl, oracle)
                expected = json.loads((run_dir / "expected-trace.json").read_text())
                quorum = json.loads((run_dir / "final-suite-quorum.json").read_text())
                with patch(
                    "rtl_closure.pipeline.DESIGN_ROOT", root / "designs"
                ), patch(
                    "rtl_closure.pipeline.revalidate_oracle_bundle_quorum",
                    return_value=(expected, quorum),
                ):
                    _, config = publish_design(
                        spec,
                        state,
                        rtl,
                        oracle,
                        {"matched": True, "best_match_rate": 1.0},
                    )
                manifest = json.loads(
                    (config.parent / "publication-manifest.json").read_text()
                )
                attested = {
                    **state,
                    "design_name": spec.name,
                    "design_config": str(config),
                    "publication_manifest": str(
                        config.parent / "publication-manifest.json"
                    ),
                    "publication_bundle_sha256": manifest[
                        "publication_bundle_sha256"
                    ],
                    "lane_input_bundle_sha256": manifest[
                        "lane_input_bundle_sha256"
                    ],
                }
                manifest_path = config.parent / "publication-manifest.json"
                omitted = {
                    **manifest,
                    "files": [
                        row for row in manifest["files"] if row["path"] != relative
                    ],
                }
                omitted_payload = dict(omitted)
                omitted_payload.pop("publication_bundle_sha256")
                omitted["publication_bundle_sha256"] = content_hash(omitted_payload)
                manifest_path.chmod(0o644)
                manifest_path.write_text(json.dumps(omitted), encoding="utf-8")
                with self.assertRaisesRegex(RuntimeError, "manifest omits"):
                    _verify_publication(
                        {
                            **attested,
                            "publication_bundle_sha256": omitted[
                                "publication_bundle_sha256"
                            ],
                        }
                    )
                manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
                manifest_path.chmod(0o444)
                protected = config.parent / relative
                protected.chmod(0o644)
                protected.write_text("tampered\n", encoding="utf-8")
                with self.assertRaisesRegex(RuntimeError, "changed after release"):
                    _verify_publication(attested)

    def test_required_eqy_publication_binds_script_and_gate_tcl_to_lane_snapshot(self):
        raw = json.loads(EXAMPLE.read_text(encoding="utf-8"))
        raw["flow"]["mapped_equivalence_mode"] = "required"
        spec = validate_spec(raw)
        rtl = "module rtl_closure_counter(input wire clk); endmodule"
        oracle = "class TopModule: pass"
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            run_dir = root / "run"
            run_dir.mkdir()
            state = self._release_state(spec, run_dir, rtl, oracle)
            expected = json.loads((run_dir / "expected-trace.json").read_text())
            quorum = json.loads((run_dir / "final-suite-quorum.json").read_text())
            with patch(
                "rtl_closure.pipeline.DESIGN_ROOT", root / "designs"
            ), patch(
                "rtl_closure.pipeline.revalidate_oracle_bundle_quorum",
                return_value=(expected, quorum),
            ):
                _, config = publish_design(
                    spec,
                    state,
                    rtl,
                    oracle,
                    {"matched": True, "best_match_rate": 1.0},
                )
            manifest = json.loads(
                (config.parent / "publication-manifest.json").read_text()
            )
            self.assertIn(
                "eqy/rtl_closure_counter.eqy", manifest["lane_input_paths"]
            )
            self.assertIn(
                "eqy/rtl_closure_counter-gate.tcl", manifest["lane_input_paths"]
            )
            self.assertEqual(
                {row["path"] for row in manifest["lane_input_manifest"]["files"]},
                set(manifest["lane_input_paths"]),
            )

    def test_lane_input_binding_rejects_different_snapshot(self):
        expected = {
            "schema_version": 2,
            "files": [
                {"path": "config.yaml", "size_bytes": 1, "sha256": "a" * 64}
            ],
            "bundle_sha256": "b" * 64,
        }
        child = lane_response(
            "lane-1",
            input_manifest={**expected, "bundle_sha256": "c" * 64},
            input_bundle_sha256="c" * 64,
        )
        with self.assertRaisesRegex(ValueError, "different immutable input manifest"):
            _validate_lane_state_identity(
                child,
                expected_run_id="lane-1",
                expected_workflow_id=None,
                expected_attempt_id=None,
                expected_input_manifest=expected,
                expected_input_bundle_sha256=expected["bundle_sha256"],
            )
        child.update(
            {
                "input_manifest": expected,
                "input_bundle_sha256": expected["bundle_sha256"],
                "idempotency_key": "wrong-launch-key",
            }
        )
        with self.assertRaisesRegex(ValueError, "mismatched idempotency_key"):
            _validate_lane_state_identity(
                child,
                expected_run_id="lane-1",
                expected_workflow_id=None,
                expected_attempt_id=None,
                expected_idempotency_key="expected-launch-key",
                expected_input_manifest=expected,
                expected_input_bundle_sha256=expected["bundle_sha256"],
            )

    def test_reconcile_fails_closed_on_lane_input_identity_mismatch(self):
        spec = load_spec(EXAMPLE)
        rtl = "module rtl_closure_counter(input wire clk); endmodule"
        oracle = "class TopModule: pass"
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            release_dir = root / "release"
            release_dir.mkdir()
            release_state = self._release_state(spec, release_dir, rtl, oracle)
            expected = json.loads((release_dir / "expected-trace.json").read_text())
            quorum = json.loads((release_dir / "final-suite-quorum.json").read_text())
            with patch(
                "rtl_closure.pipeline.DESIGN_ROOT", root / "designs"
            ), patch(
                "rtl_closure.pipeline.revalidate_oracle_bundle_quorum",
                return_value=(expected, quorum),
            ):
                _, config = publish_design(
                    spec,
                    release_state,
                    rtl,
                    oracle,
                    {"matched": True, "best_match_rate": 1.0},
                )
            manifest_path = config.parent / "publication-manifest.json"
            manifest = json.loads(manifest_path.read_text())
            controller = root / release_state["run_id"]
            controller.mkdir()
            state = {
                "schema_version": 2,
                "run_id": release_state["run_id"],
                "state_file": str(controller / "state.json"),
                "events_file": str(controller / "events.jsonl"),
                "pipeline_log": str(controller / "pipeline.log"),
                "spec_file": str(controller / "spec.json"),
                "event_sequence": 0,
                "revision": 0,
                "design_name": spec.name,
                "design_config": str(config),
                "publication_manifest": str(manifest_path),
                "publication_bundle_sha256": manifest[
                    "publication_bundle_sha256"
                ],
                "lane_input_bundle_sha256": manifest["lane_input_bundle_sha256"],
                "pdk": spec.pdk,
                "status": "running",
                "phase": "lane_running",
                "started_epoch": 1,
                "lane_run_id": "lane-1",
                "active_lane_attempt_id": "lane-attempt-001",
                "lane_attempts": [
                    {
                        "attempt_id": "lane-attempt-001",
                        "lane_run_id": "lane-1",
                        "launch_key": "launch-lane-1",
                        "status": "running",
                        "started_at": "2026-01-01T00:00:00+00:00",
                        "completed_at": None,
                        "supersedes": None,
                    }
                ],
                "lane_state": lane_response(
                    "lane-1",
                    input_manifest=manifest["lane_input_manifest"],
                    input_bundle_sha256=manifest["lane_input_bundle_sha256"],
                ),
            }
            save_state(state)
            response = SimpleNamespace(
                returncode=0,
                stdout=json.dumps(
                    lane_response(
                        "lane-1",
                        attempt_id="lane-attempt-001",
                        idempotency_key="launch-lane-1",
                        input_manifest=manifest["lane_input_manifest"],
                        input_bundle_sha256="f" * 64,
                    )
                ),
                stderr="",
            )
            with patch(
                "rtl_closure.pipeline.subprocess.run", return_value=response
            ):
                current = refresh_state(state)
            self.assertEqual(current["status"], "failed")
            self.assertEqual(
                current["failure_code"], "release_integrity/lane_input_mismatch"
            )
            stop_response = SimpleNamespace(
                returncode=0,
                stdout=json.dumps(
                    lane_response(
                        "lane-1",
                        status="stopped",
                        attempt_id="lane-attempt-001",
                        idempotency_key="launch-lane-1",
                        input_manifest=manifest["lane_input_manifest"],
                        input_bundle_sha256="f" * 64,
                    )
                ),
                stderr="",
            )
            with (
                patch("rtl_closure.pipeline.STATE_ROOT", root),
                patch(
                    "rtl_closure.pipeline.subprocess.run",
                    return_value=stop_response,
                ),
            ):
                stopped = stop_pipeline(release_state["run_id"])
            self.assertEqual(stopped["status"], "failed")
            self.assertEqual(stopped["phase"], "failed")
            self.assertEqual(stopped["lane_state"]["status"], "stopped")
            self.assertTrue(stopped["stop_requested"])
            self.assertEqual(
                stopped["failure_code"], "release_integrity/lane_input_mismatch"
            )
            with (
                patch("rtl_closure.pipeline.STATE_ROOT", root),
                patch("rtl_closure.pipeline._load_frozen_spec", return_value=spec),
                patch(
                    "rtl_closure.pipeline._start_lane_unlocked",
                    side_effect=lambda _spec, retry_state: retry_state,
                ) as starter,
            ):
                retried = retry_lane(release_state["run_id"])
            self.assertEqual(retried["status"], "running")
            starter.assert_called_once()

    def test_publish_rejects_quorum_without_origin_results(self):
        spec = load_spec(EXAMPLE)
        rtl = "module rtl_closure_counter(input wire clk); endmodule"
        oracle = "class TopModule: pass"
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            state = self._release_state(spec, root, rtl, oracle)
            quorum_path = root / "oracle-quorum.json"
            quorum = json.loads(quorum_path.read_text())
            quorum.pop("origin_results")
            quorum_path.write_text(json.dumps(quorum))
            state["release_gate"]["artifact_hashes"]["oracle-quorum.json"] = (
                _artifact_hash(quorum_path)
            )
            with patch("rtl_closure.pipeline.DESIGN_ROOT", root / "designs"):
                with self.assertRaisesRegex(RuntimeError, "quorum evidence.*schema"):
                    publish_design(
                        spec,
                        state,
                        rtl,
                        oracle,
                        {"matched": True, "best_match_rate": 1.0},
                    )

    def test_publish_rejects_missing_required_intent_closure_evidence(self):
        raw = json.loads(EXAMPLE.read_text())
        raw["verification"] = {
            "requirements": [
                {
                    "id": "REQ_COUNT",
                    "text": "The counter increments whenever enable is high.",
                    "signals": ["enable", "count"],
                }
            ],
            "directed_tests": [
                {
                    "id": "TEST_COUNT",
                    "description": "Exercise one enabled transition.",
                    "requirement_ids": ["REQ_COUNT"],
                    "cycles": [{"drive": {"enable": 1}}],
                }
            ],
        }
        raw["intent_closure"] = {
            "mode": "required",
            "user_confirmed": True,
            "unresolved_questions": [],
            "assumptions": [],
            "confirmed_sha256": compute_intent_binding(raw),
        }
        spec = validate_spec(raw)
        rtl = "module rtl_closure_counter(input wire clk); endmodule"
        oracle = "class TopModule: pass"
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            state = self._release_state(spec, root, rtl, oracle)
            with patch("rtl_closure.pipeline.DESIGN_ROOT", root / "designs"):
                with self.assertRaisesRegex(RuntimeError, "required user-confirmed"):
                    publish_design(
                        spec,
                        state,
                        rtl,
                        oracle,
                        {"matched": True, "best_match_rate": 1.0},
                    )

    def test_publish_rejects_source_change_during_snapshot(self):
        spec = load_spec(EXAMPLE)
        rtl = "module rtl_closure_counter(input wire clk); endmodule"
        oracle = "class TopModule: pass"
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            state = self._release_state(spec, root, rtl, oracle)
            original_write = __import__(
                "rtl_closure.pipeline", fromlist=["atomic_write"]
            ).atomic_write
            changed = False

            def racing_write(path, content, mode=0o600):
                nonlocal changed
                if not changed and ".staging-" in str(path):
                    changed = True
                    (root / "repair-history.json").write_text('[{"injected": true}]')
                return original_write(path, content, mode)

            with patch(
                "rtl_closure.pipeline.DESIGN_ROOT", root / "designs"
            ), patch("rtl_closure.pipeline.atomic_write", side_effect=racing_write):
                with self.assertRaisesRegex(RuntimeError, "release evidence changed"):
                    publish_design(
                        spec,
                        state,
                        rtl,
                        oracle,
                        {"matched": True, "best_match_rate": 1.0},
                    )

    def test_publish_rejects_final_quorum_replay_drift(self):
        spec = load_spec(EXAMPLE)
        rtl = "module rtl_closure_counter(input wire clk); endmodule"
        oracle = "class TopModule: pass"
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            state = self._release_state(spec, root, rtl, oracle)
            expected = json.loads((root / "expected-trace.json").read_text())
            replayed = json.loads(json.dumps(expected))
            replayed["trace"][0]["expected"]["count"] = 1
            quorum = json.loads((root / "final-suite-quorum.json").read_text())
            with patch(
                "rtl_closure.pipeline.DESIGN_ROOT", root / "designs"
            ), patch(
                "rtl_closure.pipeline.revalidate_oracle_bundle_quorum",
                return_value=(replayed, quorum),
            ):
                with self.assertRaisesRegex(RuntimeError, "quorum replay changed"):
                    publish_design(
                        spec,
                        state,
                        rtl,
                        oracle,
                        {"matched": True, "best_match_rate": 1.0},
                    )

    def test_publish_fails_closed_without_release_evidence(self):
        spec = load_spec(EXAMPLE)
        state = {
            "run_id": "20260720-120000-rtl_closure_counter",
            "model": {"profile": "api", "model": "test"},
        }
        with self.assertRaisesRegex(RuntimeError, "verification has not passed"):
            publish_design(
                spec,
                state,
                "module rtl_closure_counter(input wire clk); endmodule",
                "class TopModule: pass",
                {"matched": True, "best_match_rate": 1.0},
            )

    def test_publish_rejects_formal_counterexample(self):
        spec = load_spec(EXAMPLE)
        rtl = "module rtl_closure_counter(input wire clk); endmodule"
        oracle = "class TopModule: pass"
        state = {
            "run_id": "20260720-120000-rtl_closure_counter",
            "model": {"profile": "api", "model": "test"},
            "verification": {"passed": True},
            "release_gate": {
                "passed": True,
                "rtl_sha256": content_hash(rtl),
                "oracle_sha256": content_hash(oracle),
                "formal": {"status": "fail"},
            },
        }
        with self.assertRaisesRegex(RuntimeError, "formal"):
            publish_design(
                spec,
                state,
                rtl,
                oracle,
                {"matched": True, "best_match_rate": 1.0},
            )

    def test_publish_rejects_formal_report_for_another_rtl(self):
        spec = load_spec(EXAMPLE)
        rtl = "module rtl_closure_counter(input wire clk); endmodule"
        oracle = "class TopModule: pass"
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            state = self._release_state(spec, root, rtl, oracle)
            formal_path = root / "formal-report.json"
            formal = json.loads(formal_path.read_text())
            formal["rtl_sha256"] = content_hash("another RTL")
            formal_path.write_text(json.dumps(formal))
            state["release_gate"]["artifact_hashes"]["formal-report.json"] = (
                _artifact_hash(formal_path)
            )
            expected = json.loads((root / "expected-trace.json").read_text())
            quorum = json.loads((root / "final-suite-quorum.json").read_text())
            with patch(
                "rtl_closure.pipeline.DESIGN_ROOT", root / "designs"
            ), patch(
                "rtl_closure.pipeline.revalidate_oracle_bundle_quorum",
                return_value=(expected, quorum),
            ):
                with self.assertRaisesRegex(RuntimeError, "formal evidence"):
                    publish_design(
                        spec,
                        state,
                        rtl,
                        oracle,
                        {"matched": True, "best_match_rate": 1.0},
                    )

    def test_publish_rejects_forged_minimal_formal_pass(self):
        spec = load_spec(EXAMPLE)
        rtl = "module rtl_closure_counter(input wire clk); endmodule"
        oracle = "class TopModule: pass"
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            state = self._release_state(spec, root, rtl, oracle)
            forged = {
                "status": "pass",
                "rtl_sha256": content_hash(rtl),
                "suite_sha256": state["release_gate"]["active_suite_sha256"],
                "property_map_sha256": content_hash(
                    json.loads((root / "formal-properties.json").read_text())
                ),
            }
            formal_path = root / "formal-report.json"
            formal_path.write_text(json.dumps(forged))
            verification_path = root / "verification-result.json"
            verification = json.loads(verification_path.read_text())
            verification["formal"] = forged
            verification_path.write_text(json.dumps(verification))
            state["release_gate"]["formal"] = forged
            for path in (formal_path, verification_path):
                state["release_gate"]["artifact_hashes"][path.name] = (
                    _artifact_hash(path)
                )
            expected = json.loads((root / "expected-trace.json").read_text())
            quorum = json.loads((root / "final-suite-quorum.json").read_text())
            with patch(
                "rtl_closure.pipeline.DESIGN_ROOT", root / "designs"
            ), patch(
                "rtl_closure.pipeline.revalidate_oracle_bundle_quorum",
                return_value=(expected, quorum),
            ):
                with self.assertRaisesRegex(RuntimeError, "formal evidence contract"):
                    publish_design(
                        spec,
                        state,
                        rtl,
                        oracle,
                        {"matched": True, "best_match_rate": 1.0},
                    )

    def test_publish_rejects_formal_manifest_without_solver_verdict(self):
        spec = load_spec(EXAMPLE)
        rtl = "module rtl_closure_counter(input wire clk); endmodule"
        oracle = "class TopModule: pass"
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            state = self._release_state(spec, root, rtl, oracle)
            execution_path = root / "formal-execution-evidence.json"
            execution = json.loads(execution_path.read_text())
            execution["entries"][0]["log"] = "Yosys exited without a proof verdict\n"
            execution["entries"][0]["log_sha256"] = hashlib.sha256(
                execution["entries"][0]["log"].encode()
            ).hexdigest()
            execution.pop("evidence_sha256")
            execution["evidence_sha256"] = content_hash(execution)
            execution_path.write_text(json.dumps(execution))

            formal_path = root / "formal-report.json"
            formal = json.loads(formal_path.read_text())
            formal["execution_evidence_sha256"] = execution["evidence_sha256"]
            formal_path.write_text(json.dumps(formal))
            verification_path = root / "verification-result.json"
            verification = json.loads(verification_path.read_text())
            verification["formal"] = formal
            verification_path.write_text(json.dumps(verification))
            state["release_gate"]["formal"] = formal
            for path in (execution_path, formal_path, verification_path):
                state["release_gate"]["artifact_hashes"][path.name] = (
                    _artifact_hash(path)
                )
            expected = json.loads((root / "expected-trace.json").read_text())
            quorum = json.loads((root / "final-suite-quorum.json").read_text())
            with patch(
                "rtl_closure.pipeline.DESIGN_ROOT", root / "designs"
            ), patch(
                "rtl_closure.pipeline.revalidate_oracle_bundle_quorum",
                return_value=(expected, quorum),
            ):
                with self.assertRaisesRegex(RuntimeError, "formal execution evidence"):
                    publish_design(
                        spec,
                        state,
                        rtl,
                        oracle,
                        {"matched": True, "best_match_rate": 1.0},
                    )

    def test_publish_rejects_contradictory_coverage_pass(self):
        spec = load_spec(EXAMPLE)
        rtl = "module rtl_closure_counter(input wire clk); endmodule"
        oracle = "class TopModule: pass"
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            state = self._release_state(spec, root, rtl, oracle)
            path = root / "coverage-refinement.json"
            coverage = json.loads(path.read_text())
            coverage["final"].update(
                {
                    "executed_lines": [],
                    "missing_lines": [1],
                    "covered": 0,
                    "percent": 0.0,
                }
            )
            coverage["target_met"] = True
            coverage["final_testplan_sha256"] = "0" * 64
            coverage["evidence_bindings"]["testplan_sha256"] = "0" * 64
            path.write_text(json.dumps(coverage))
            state["release_gate"]["artifact_hashes"][path.name] = _artifact_hash(path)
            expected = json.loads((root / "expected-trace.json").read_text())
            quorum = json.loads((root / "final-suite-quorum.json").read_text())
            with patch(
                "rtl_closure.pipeline.DESIGN_ROOT", root / "designs"
            ), patch(
                "rtl_closure.pipeline.revalidate_oracle_bundle_quorum",
                return_value=(expected, quorum),
            ):
                with self.assertRaisesRegex(RuntimeError, "coverage evidence is invalid"):
                    publish_design(
                        spec,
                        state,
                        rtl,
                        oracle,
                        {"matched": True, "best_match_rate": 1.0},
                    )

    def test_publish_rejects_forged_universal_execution_manifest(self):
        spec = load_spec(EXAMPLE)
        rtl = "module rtl_closure_counter(input wire clk); endmodule"
        oracle = "class TopModule: pass"
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            state = self._release_state(spec, root, rtl, oracle)
            path = root / "universal-execution-evidence.json"
            evidence = json.loads(path.read_text())
            evidence["dut"] = "module forged; endmodule\n"
            evidence["dut_sha256"] = hashlib.sha256(
                evidence["dut"].encode()
            ).hexdigest()
            evidence.pop("evidence_sha256")
            evidence["evidence_sha256"] = content_hash(evidence)
            path.write_text(json.dumps(evidence))
            state["release_gate"]["artifact_hashes"][path.name] = _artifact_hash(path)
            expected = json.loads((root / "expected-trace.json").read_text())
            quorum = json.loads((root / "final-suite-quorum.json").read_text())
            with patch(
                "rtl_closure.pipeline.DESIGN_ROOT", root / "designs"
            ), patch(
                "rtl_closure.pipeline.revalidate_oracle_bundle_quorum",
                return_value=(expected, quorum),
            ):
                with self.assertRaisesRegex(RuntimeError, "universal execution evidence"):
                    publish_design(
                        spec,
                        state,
                        rtl,
                        oracle,
                        {"matched": True, "best_match_rate": 1.0},
                    )

    def test_publish_rejects_tampered_release_artifact(self):
        spec = load_spec(EXAMPLE)
        rtl = "module rtl_closure_counter(input wire clk); endmodule"
        oracle = "class TopModule: pass"
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            state = self._release_state(spec, root, rtl, oracle)
            (root / "adaptive-corpus.json").write_text(
                json.dumps({"active_suite_sha256": "tampered", "epochs": [1]})
            )
            design_root = root / "designs"
            with patch("rtl_closure.pipeline.DESIGN_ROOT", design_root):
                with self.assertRaisesRegex(RuntimeError, "release evidence"):
                    publish_design(
                        spec,
                        state,
                        rtl,
                        oracle,
                        {"matched": True, "best_match_rate": 1.0},
                    )
            self.assertEqual(list(design_root.glob(".staging-*")), [])
            self.assertEqual(list(design_root.glob(".*.staging-*")), [])

    def test_publish_rejects_semantically_forged_universal_evidence(self):
        spec = load_spec(EXAMPLE)
        rtl = "module rtl_closure_counter(input wire clk); endmodule"
        oracle = "class TopModule: pass"
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            state = self._release_state(spec, root, rtl, oracle)
            universal_path = root / "universal-properties.json"
            universal = json.loads(universal_path.read_text())
            universal["universal_proof"] = True
            universal_path.write_text(json.dumps(universal))
            verification_path = root / "verification-result.json"
            verification = json.loads(verification_path.read_text())
            verification["universal_properties"] = universal
            verification_path.write_text(json.dumps(verification))
            state["release_gate"]["universal_properties"] = universal
            for path in (universal_path, verification_path):
                state["release_gate"]["artifact_hashes"][path.name] = _artifact_hash(
                    path
                )
            expected = json.loads((root / "expected-trace.json").read_text())
            quorum = json.loads((root / "final-suite-quorum.json").read_text())
            with patch(
                "rtl_closure.pipeline.DESIGN_ROOT", root / "designs"
            ), patch(
                "rtl_closure.pipeline.revalidate_oracle_bundle_quorum",
                return_value=(expected, quorum),
            ):
                with self.assertRaisesRegex(RuntimeError, "universal property evidence"):
                    publish_design(
                        spec,
                        state,
                        rtl,
                        oracle,
                        {"matched": True, "best_match_rate": 1.0},
                    )

    def test_publish_rejects_forged_universal_capability_scope(self):
        spec = load_spec(EXAMPLE)
        rtl = "module rtl_closure_counter(input wire clk); endmodule"
        oracle = "class TopModule: pass"
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            state = self._release_state(spec, root, rtl, oracle)
            universal_path = root / "universal-properties.json"
            universal = json.loads(universal_path.read_text())
            universal["scope"]["capabilities"]["liveness_properties"] = True
            universal_path.write_text(json.dumps(universal))
            verification_path = root / "verification-result.json"
            verification = json.loads(verification_path.read_text())
            verification["universal_properties"] = universal
            verification_path.write_text(json.dumps(verification))
            state["release_gate"]["universal_properties"] = universal
            for path in (universal_path, verification_path):
                state["release_gate"]["artifact_hashes"][path.name] = _artifact_hash(
                    path
                )
            expected = json.loads((root / "expected-trace.json").read_text())
            quorum = json.loads((root / "final-suite-quorum.json").read_text())
            with patch(
                "rtl_closure.pipeline.DESIGN_ROOT", root / "designs"
            ), patch(
                "rtl_closure.pipeline.revalidate_oracle_bundle_quorum",
                return_value=(expected, quorum),
            ):
                with self.assertRaisesRegex(
                    RuntimeError, "universal property evidence"
                ):
                    publish_design(
                        spec,
                        state,
                        rtl,
                        oracle,
                        {"matched": True, "best_match_rate": 1.0},
                    )

    def test_publish_rejects_semantically_forged_architecture_evidence(self):
        spec = load_spec(EXAMPLE)
        rtl = "module rtl_closure_counter(input wire clk); endmodule"
        oracle = "class TopModule: pass"
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            state = self._release_state(spec, root, rtl, oracle)
            path = root / "architecture-audit.json"
            architecture = json.loads(path.read_text())
            architecture["checks"] = [
                {"id": "required_module:forged", "status": "pass"}
            ]
            path.write_text(json.dumps(architecture))
            state["release_gate"]["architecture_audit"] = architecture
            state["release_gate"]["artifact_hashes"][path.name] = _artifact_hash(path)
            expected = json.loads((root / "expected-trace.json").read_text())
            quorum = json.loads((root / "final-suite-quorum.json").read_text())
            with patch(
                "rtl_closure.pipeline.DESIGN_ROOT", root / "designs"
            ), patch(
                "rtl_closure.pipeline.revalidate_oracle_bundle_quorum",
                return_value=(expected, quorum),
            ):
                with self.assertRaisesRegex(RuntimeError, "architecture audit evidence"):
                    publish_design(
                        spec,
                        state,
                        rtl,
                        oracle,
                        {"matched": True, "best_match_rate": 1.0},
                    )

    def test_publish_rejects_forged_synthesis_equivalence_log(self):
        spec = load_spec(EXAMPLE)
        rtl = "module rtl_closure_counter(input wire clk); endmodule"
        oracle = "class TopModule: pass"
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            state = self._release_state(spec, root, rtl, oracle)
            path = root / "synthesis-equivalence.log"
            path.write_text("no proof verdict\n")
            state["release_gate"]["artifact_hashes"][path.name] = _artifact_hash(path)
            expected = json.loads((root / "expected-trace.json").read_text())
            quorum = json.loads((root / "final-suite-quorum.json").read_text())
            with patch(
                "rtl_closure.pipeline.DESIGN_ROOT", root / "designs"
            ), patch(
                "rtl_closure.pipeline.revalidate_oracle_bundle_quorum",
                return_value=(expected, quorum),
            ):
                with self.assertRaisesRegex(RuntimeError, "synthesis equivalence evidence"):
                    publish_design(
                        spec,
                        state,
                        rtl,
                        oracle,
                        {"matched": True, "best_match_rate": 1.0},
                    )

    def test_publish_rejects_missing_required_artifact_even_if_not_claimed(self):
        spec = load_spec(EXAMPLE)
        rtl = "module rtl_closure_counter(input wire clk); endmodule"
        oracle = "class TopModule: pass"
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            state = self._release_state(spec, root, rtl, oracle)
            (root / "counterexamples.json").unlink()
            state["release_gate"]["artifact_hashes"].pop("counterexamples.json")
            state["release_gate"]["required_artifacts"].remove("counterexamples.json")
            with patch("rtl_closure.pipeline.DESIGN_ROOT", root / "designs"):
                with self.assertRaisesRegex(RuntimeError, "manifest"):
                    publish_design(
                        spec,
                        state,
                        rtl,
                        oracle,
                        {"matched": True, "best_match_rate": 1.0},
                    )

    def test_publish_requires_the_generated_rtl_quality_gate(self):
        spec = load_spec(EXAMPLE)
        rtl = "module rtl_closure_counter(input wire clk); endmodule"
        oracle = "class TopModule: pass"
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            state = self._release_state(spec, root, rtl, oracle)
            lint_path = root / "verilator-lint.json"
            lint = json.loads(lint_path.read_text(encoding="utf-8"))
            lint.pop("quality_gate")
            lint_path.write_text(json.dumps(lint), encoding="utf-8")
            state["release_gate"]["artifact_hashes"]["verilator-lint.json"] = (
                _artifact_hash(lint_path)
            )
            expected = json.loads((root / "expected-trace.json").read_text())
            quorum = json.loads((root / "final-suite-quorum.json").read_text())
            with patch(
                "rtl_closure.pipeline.DESIGN_ROOT", root / "designs"
            ), patch(
                "rtl_closure.pipeline.revalidate_oracle_bundle_quorum",
                return_value=(expected, quorum),
            ):
                with self.assertRaisesRegex(RuntimeError, "Verilator lint"):
                    publish_design(
                        spec,
                        state,
                        rtl,
                        oracle,
                        {"matched": True, "best_match_rate": 1.0},
                    )

    def test_repair_progress_does_not_leak_previous_candidate_formal_state(self):
        previous = {
            "round": 1,
            "candidate": 1,
            "failure_class": "formal_counterexample",
            "formal_status": "fail",
            "verilator_status": "pass",
            "result": "failed",
            "universal_status": "fail",
            "failed_property_ids": ["PROP_OLD"],
            "failed_checks": ["required_module:old"],
        }
        current = _merge_repair_progress(
            previous,
            "repair_evaluating",
            {"round": 1, "candidate": 2, "action": "evaluate"},
        )
        self.assertNotIn("failure_class", current)
        self.assertNotIn("formal_status", current)
        self.assertNotIn("verilator_status", current)
        self.assertNotIn("result", current)
        self.assertNotIn("universal_status", current)
        self.assertNotIn("failed_property_ids", current)
        self.assertNotIn("failed_checks", current)
        self.assertEqual(current["candidate"], 2)

    def test_model_progress_tracks_concurrent_requests_without_cycles(self):
        active: dict[int, dict] = {}
        first = _merge_model_progress(
            active,
            {
                "event": "request_started",
                "request_id": 1,
                "candidate_index": 1,
                "candidate_count": 2,
                "attempt": 1,
                "max_attempts": 3,
            },
        )
        _merge_model_progress(
            active,
            {
                "event": "request_started",
                "request_id": 2,
                "candidate_index": 2,
                "candidate_count": 2,
                "attempt": 1,
                "max_attempts": 3,
            },
        )
        completed = _merge_model_progress(
            active,
            {
                "event": "request_completed",
                "request_id": 1,
                "candidate_index": 1,
                "candidate_count": 2,
                "attempt": 1,
            },
        )

        json.dumps(first)
        json.dumps(completed)
        self.assertEqual(first["active_request_count"], 1)
        self.assertEqual(completed["active_request_count"], 1)
        self.assertEqual(completed["active_requests"][0]["request_id"], 2)

    def test_report_includes_progress_qor_and_errors(self):
        with tempfile.TemporaryDirectory() as temp:
            log = Path(temp) / "pipeline.log"
            log.write_text("phase=lane_running\n[ERROR] route failed\n")
            state = {
                "run_id": "20260720-120000-demo",
                "design_name": "demo",
                "design_slug": "rtl_closure_demo",
                "pdk": "sky130A",
                "status": "failed",
                "phase": "failed",
                "elapsed_seconds": 42,
                "model_request": {
                    "event": "request_retrying",
                    "candidate_index": 2,
                    "candidate_count": 4,
                    "attempt": 1,
                    "max_attempts": 3,
                    "active_request_count": 2,
                    "http_status": 524,
                    "ray_id": "test-ray",
                },
                "pipeline_log": str(log),
                "state_file": str(Path(temp) / "state.json"),
                "lane_run_id": "lane-1",
                "error": "LibreLane flow failed",
                "failure_class": "verification_configuration",
                "failure_gate": "oracle_line_coverage",
                "failure_code": "verification_configuration/coverage",
                "coverage": {"coverage_percent": 88.0},
                "verification": {
                    "passed": True,
                    "total_checks": 10,
                    "mismatched": 0,
                    "universal_properties": {
                        "status": "pass",
                        "property_count": 1,
                        "passed_property_count": 1,
                        "complete_behavioral_proof": False,
                        "complete_requirement_proof": False,
                        "scope": universal_proof_scope(
                            induction_depth=8, reset=None
                        ),
                        "requirement_coverage": universal_requirement_coverage(
                            [
                                {
                                    "status": "pass",
                                    "requirement_ids": ["REQ-A"],
                                }
                            ],
                            ["REQ-A", "REQ-B"],
                        ),
                    },
                },
                "architecture_audit": {
                    "status": "pass",
                    "failed_checks": [],
                    "artifact": "/tmp/architecture-audit.json",
                },
                "synthesis_equivalence": {
                    "status": "pass",
                    "equivalence_cells": 12,
                    "proven_cells": 12,
                },
                "mapped_equivalence_mode": "required",
                "lane_state": {
                    "active_step": "OpenROAD.DetailedRouting",
                    "last_completed_step": "OpenROAD.GlobalRouting",
                    "completed_step_count": 25,
                    "started_step_count": 26,
                    "process_status": "completed",
                    "flow_scope": "full",
                    "delivery_gate": "fail",
                    "delivery_gate_reasons": ["route DRC failed"],
                    "expected_final_step": "Misc.ReportManufacturability",
                    "endpoint_reached": False,
                    "metrics_path": "/tmp/metrics.json",
                    "qor": {"gate": "fail"},
                    "artifacts": {"latest_odb": "/tmp/route.odb"},
                    "mapped_equivalence": {
                        "requested": True,
                        "status": "pass",
                        "gate": "pass",
                        "scope": (
                            "RTL versus synthesized PDK-mapped netlist equivalence only; "
                            "this is not post-layout LEC"
                        ),
                    },
                    "assurance": {
                        "pdk": "sky130A",
                        "open_pdk": True,
                        "foundry_signoff": False,
                        "tapeout_guarantee": False,
                        "limitations": [
                            "Open-source PDK checks are not commercial foundry signoff."
                        ],
                    },
                    "errors": ["[ERROR DRT-0001] route failed"],
                },
            }
            report = build_report(state)
        self.assertEqual(report["component"], "librelane")
        self.assertTrue(report["state_file"].endswith("state.json"))
        self.assertEqual(report["current_step"], "OpenROAD.DetailedRouting")
        self.assertEqual(report["qor"]["gate"], "fail")
        self.assertEqual(report["process_status"], "completed")
        self.assertEqual(report["flow_scope"], "full")
        self.assertEqual(report["delivery_gate"], "fail")
        self.assertEqual(report["delivery_gate_reasons"], ["route DRC failed"])
        self.assertFalse(report["endpoint_reached"])
        self.assertEqual(report["metrics_path"], "/tmp/metrics.json")
        self.assertEqual(report["coverage"]["coverage_percent"], 88.0)
        self.assertEqual(report["architecture_audit"]["status"], "pass")
        self.assertEqual(report["synthesis_equivalence"]["proven_cells"], 12)
        self.assertEqual(report["mapped_equivalence"]["gate"], "pass")
        self.assertFalse(report["assurance"]["foundry_signoff"])
        self.assertEqual(report["model_request"]["http_status"], 524)
        self.assertEqual(report["failure_class"], "verification_configuration")
        self.assertEqual(report["failure_gate"], "oracle_line_coverage")
        self.assertEqual(
            report["failure_code"], "verification_configuration/coverage"
        )
        self.assertIn("[ERROR DRT-0001] route failed", report["errors"])
        self.assertIn("[ERROR] route failed", report["diagnostic_log_tail"])
        rendered = format_report(report)
        self.assertIn("model_request: event=request_retrying", rendered)
        self.assertIn("model_request_ray_id: test-ray", rendered)
        self.assertIn("delivery_gate: fail", rendered)
        self.assertIn("mapped_equivalence: pass (gate=pass)", rendered)
        self.assertIn("foundry_signoff: False", rendered)
        self.assertIn(
            "assurance_limitation: Open-source PDK checks are not commercial foundry signoff.",
            rendered,
        )
        self.assertIn("failure_gate: oracle_line_coverage", rendered)
        self.assertIn(
            "failure_code: verification_configuration/coverage", rendered
        )
        self.assertIn(
            "universal_scope: listed_port_level_safety_properties_only", rendered
        )
        self.assertIn("universal_supports_liveness: False", rendered)
        self.assertIn("universal_supports_internal_state: False", rendered)
        self.assertIn("universal_complete_behavioral_proof: False", rendered)
        self.assertIn("universal_requirement_mapping: 1/2", rendered)
        self.assertIn("universal_uncovered_requirements: REQ-B", rendered)

    def test_compact_report_and_transition_events_drop_large_evidence_payloads(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            state = {
                "run_id": "run-compact-report",
                "state_file": str(root / "state.json"),
                "events_file": str(root / "events.jsonl"),
                "pipeline_log": str(root / "pipeline.log"),
                "event_sequence": 0,
                "revision": 0,
                "design_name": "demo",
                "pdk": "sky130A",
                "status": "running",
                "phase": "verification",
                "elapsed_seconds": 1,
            }
            (root / "pipeline.log").write_text("running\n", encoding="utf-8")
            save_state(state)
            huge_rows = [{"payload": "x" * 4096} for _ in range(128)]
            _update(
                state,
                verification={
                    "passed": True,
                    "rtl_sha256": "a" * 64,
                    "suite_sha256": "b" * 64,
                    "formal": {"status": "pass", "rows": huge_rows},
                    "universal_properties": {"status": "pass"},
                    "raw_evidence": huge_rows,
                },
                announce=False,
            )
            event_bytes = (root / "events.jsonl").read_bytes()
            self.assertLess(len(event_bytes), 64 * 1024)
            self.assertNotIn(b'"payload"', event_bytes)
            report = build_report(state, compact=True)
            encoded = json.dumps(report).encode()
            self.assertTrue(report["compact"])
            self.assertLess(len(encoded), 64 * 1024)
            self.assertNotIn(b'"payload"', encoded)

    def test_report_parser_accepts_monitor_compact_mode(self):
        args = build_parser().parse_args(
            ["report", "--run-id", "run-1", "--json", "--compact"]
        )
        self.assertTrue(args.compact)

    def test_parent_completes_only_for_full_delivery_pass(self):
        cases = [
            (
                "full-pass",
                {
                    "status": "completed",
                    "process_status": "completed",
                    "flow_scope": "full",
                    "delivery_gate": "pass",
                    "delivery_gate_reasons": [],
                },
                "completed",
                "completed",
            ),
            (
                "partial",
                {
                    "status": "completed",
                    "process_status": "completed",
                    "flow_scope": "partial",
                    "delivery_gate": "not_applicable",
                    "delivery_gate_reasons": ["partial range"],
                },
                "incomplete",
                "lane_partial_completed",
            ),
            (
                "full-incomplete",
                {
                    "status": "incomplete",
                    "process_status": "completed",
                    "flow_scope": "full",
                    "delivery_gate": "incomplete",
                    "delivery_gate_reasons": ["QoR unknown"],
                },
                "incomplete",
                "lane_delivery_incomplete",
            ),
        ]
        for name, child, expected_status, expected_phase in cases:
            with self.subTest(name=name), tempfile.TemporaryDirectory() as temp:
                root = Path(temp)
                state = {
                    "schema_version": 2,
                    "state_file": str(root / "state.json"),
                    "run_id": f"run-{name}",
                    "design_name": "demo",
                    "pdk": "sky130A",
                    "mapped_equivalence_mode": "off",
                    "status": "running",
                    "phase": "lane_running",
                    "started_epoch": 1,
                    "pipeline_log": str(root / "pipeline.log"),
                    "lane_run_id": f"lane-{name}",
                    "lane_state": {"status": "running"},
                    "active_lane_attempt_id": "lane-attempt-001",
                    "lane_attempts": [
                        {
                            "attempt_id": "lane-attempt-001",
                            "lane_run_id": f"lane-{name}",
                            "launch_key": f"launch-{name}",
                            "status": "running",
                            "started_at": "2026-01-01T00:00:00+00:00",
                            "completed_at": None,
                            "supersedes": None,
                        }
                    ],
                }
                (root / "pipeline.log").write_text("", encoding="utf-8")
                save_state(state)
                child_fields = dict(child)
                child_status = str(child_fields.pop("status"))
                child = lane_response(
                    f"lane-{name}",
                    status=child_status,
                    attempt_id="lane-attempt-001",
                    idempotency_key=f"launch-{name}",
                    **child_fields,
                )
                response = SimpleNamespace(
                    returncode=0,
                    stdout=json.dumps(child),
                    stderr="",
                )
                with patch(
                    "rtl_closure.pipeline.subprocess.run", return_value=response
                ):
                    current = refresh_state(state)
                self.assertEqual(current["status"], expected_status)
                self.assertEqual(current["phase"], expected_phase)
                self.assertEqual(current["lane_state"]["process_status"], "completed")

    def test_reconcile_rejects_stale_child_release_integrity_audit(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            state = {
                "state_file": str(root / "state.json"),
                "run_id": "run-stale-release-audit",
                "design_name": "demo",
                "pdk": "sky130A",
                "mapped_equivalence_mode": "off",
                "status": "running",
                "phase": "lane_running",
                "started_epoch": 1,
                "pipeline_log": str(root / "pipeline.log"),
                "lane_run_id": "lane-stale-release-audit",
                "lane_state": lane_response("lane-stale-release-audit"),
                "active_lane_attempt_id": "lane-attempt-001",
                "lane_attempts": [
                    {
                        "attempt_id": "lane-attempt-001",
                        "lane_run_id": "lane-stale-release-audit",
                        "launch_key": "launch-stale-release-audit",
                        "status": "running",
                    }
                ],
            }
            (root / "pipeline.log").write_text("", encoding="utf-8")
            save_state(state)
            child = lane_response(
                "lane-stale-release-audit",
                status="completed",
                audit_id="lane-audit-stale",
                attempt_id="lane-attempt-001",
                idempotency_key="launch-stale-release-audit",
                process_status="completed",
                flow_scope="full",
                delivery_gate="pass",
            )
            response = SimpleNamespace(
                returncode=0,
                stdout=json.dumps(child),
                stderr="",
            )
            with patch(
                "rtl_closure.pipeline.subprocess.run", return_value=response
            ) as runner:
                current = refresh_state(
                    state, integrity_audit_id="pi-audit-current"
                )

        self.assertEqual(current["status"], "running")
        self.assertEqual(current["phase"], "lane_status_unknown")
        self.assertIn("stale release integrity audit", current["lane_status_error"])
        self.assertEqual(
            runner.call_args.args[0][-2:],
            ["--integrity-audit-id", "pi-audit-current"],
        )

    def test_degraded_child_missing_attestation_makes_parent_incomplete(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            state = {
                "state_file": str(root / "state.json"),
                "events_file": str(root / "events.jsonl"),
                "event_sequence": 0,
                "run_id": "run-degraded-release",
                "design_name": "demo",
                "pdk": "sky130A",
                "mapped_equivalence_mode": "off",
                "status": "running",
                "phase": "lane_running",
                "started_epoch": 1,
                "pipeline_log": str(root / "pipeline.log"),
                "lane_run_id": "lane-degraded-release",
                "lane_state": lane_response("lane-degraded-release"),
                "active_lane_attempt_id": "lane-attempt-001",
                "lane_attempts": [
                    {
                        "attempt_id": "lane-attempt-001",
                        "lane_run_id": "lane-degraded-release",
                        "launch_key": "launch-degraded-release",
                        "status": "running",
                    }
                ],
            }
            (root / "pipeline.log").write_text("", encoding="utf-8")
            save_state(state)
            child = lane_response(
                "lane-degraded-release",
                status="incomplete",
                audit_id="pi-audit-current",
                release_status="degraded",
                release_errors=["canonical GDS sha256 changed"],
                attempt_id="lane-attempt-001",
                idempotency_key="launch-degraded-release",
                process_status="completed",
                flow_scope="full",
                delivery_gate="incomplete",
                delivery_gate_reasons=["delivery evidence changed"],
            )
            child.pop("delivery_attestation")
            child["release_integrity"]["attestation_sha256"] = None
            response = SimpleNamespace(
                returncode=0,
                stdout=json.dumps(child),
                stderr="",
            )
            with patch(
                "rtl_closure.pipeline.subprocess.run", return_value=response
            ):
                current = refresh_state(
                    state, integrity_audit_id="pi-audit-current"
                )

        self.assertEqual(current["status"], "incomplete")
        self.assertEqual(current["phase"], "lane_release_integrity_incomplete")
        self.assertEqual(current["failure_class"], "release_integrity")
        self.assertEqual(current["failure_gate"], "lane_delivery_attestation")
        self.assertEqual(
            current["failure_code"],
            "release_integrity/lane_artifact_changed",
        )
        self.assertEqual(current["last_integrity_audit_id"], "pi-audit-current")

    def test_verified_unknown_and_completed_lane_states_require_attestation(self):
        cases = (
            ("incomplete", "verified", []),
            ("incomplete", "unknown", ["audit unavailable"]),
            ("completed", "degraded", ["delivery changed"]),
        )
        for status, integrity_status, errors in cases:
            with self.subTest(status=status, integrity=integrity_status):
                child = lane_response(
                    "lane-attestation-required",
                    status=status,
                    release_status=integrity_status,
                    release_errors=errors,
                )
                child.pop("delivery_attestation")
                child["release_integrity"]["attestation_sha256"] = None
                with self.assertRaisesRegex(
                    ValueError, "delivery integrity evidence is malformed"
                ):
                    _validate_lane_state_identity(
                        child,
                        expected_run_id="lane-attestation-required",
                        expected_workflow_id=None,
                        expected_attempt_id=None,
                    )

    def test_lane_release_integrity_error_semantics_are_bounded(self):
        cases = (
            ("verified", ["unexpected error"]),
            ("unknown", []),
            ("degraded", []),
            ("unknown", [""]),
            ("degraded", [f"error {index}" for index in range(17)]),
        )
        for integrity_status, errors in cases:
            with self.subTest(integrity=integrity_status, errors=len(errors)):
                child = lane_response(
                    "lane-invalid-errors",
                    status="incomplete",
                    release_status=integrity_status,
                    release_errors=errors,
                )
                with self.assertRaisesRegex(
                    ValueError, "delivery attestation binding is invalid"
                ):
                    _validate_lane_state_identity(
                        child,
                        expected_run_id="lane-invalid-errors",
                        expected_workflow_id=None,
                        expected_attempt_id=None,
                    )

    def test_required_mapped_equivalence_blocks_unattested_delivery(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            state = {
                "schema_version": 2,
                "state_file": str(root / "state.json"),
                "run_id": "run-required-eqy",
                "design_name": "demo",
                "pdk": "sky130A",
                "mapped_equivalence_mode": "required",
                "status": "running",
                "phase": "lane_running",
                "started_epoch": 1,
                "pipeline_log": str(root / "pipeline.log"),
                "lane_run_id": "lane-required-eqy",
                "lane_state": {"status": "running"},
                "active_lane_attempt_id": "lane-attempt-001",
                "lane_attempts": [
                    {
                        "attempt_id": "lane-attempt-001",
                        "lane_run_id": "lane-required-eqy",
                        "launch_key": "launch-required-eqy",
                        "status": "running",
                        "started_at": "2026-01-01T00:00:00+00:00",
                        "completed_at": None,
                        "supersedes": None,
                    }
                ],
            }
            (root / "pipeline.log").write_text("", encoding="utf-8")
            save_state(state)
            response = SimpleNamespace(
                returncode=0,
                stdout=json.dumps(
                    lane_response(
                        "lane-required-eqy",
                        status="completed",
                        attempt_id="lane-attempt-001",
                        idempotency_key="launch-required-eqy",
                        process_status="completed",
                        flow_scope="full",
                        delivery_gate="pass",
                        delivery_gate_reasons=[],
                    )
                ),
                stderr="",
            )
            with patch(
                "rtl_closure.pipeline.subprocess.run", return_value=response
            ):
                current = refresh_state(state)

        self.assertEqual(current["status"], "incomplete")
        self.assertEqual(current["phase"], "lane_delivery_incomplete")
        self.assertEqual(current["lane_state"]["delivery_gate"], "incomplete")
        self.assertIn(
            "mapped_equivalence: required mapped-equivalence evidence is missing",
            current["lane_state"]["delivery_gate_reasons"],
        )

    def test_mapped_equivalence_mode_requires_persisted_current_state(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            raw = json.loads(EXAMPLE.read_text())
            raw["flow"]["mapped_equivalence_mode"] = "required"
            spec_file = root / "spec.json"
            spec_file.write_text(json.dumps(raw), encoding="utf-8")
            state = {
                "schema_version": 2,
                "spec_file": str(spec_file),
            }
            self.assertEqual(_mapped_equivalence_mode(state), "invalid")

    def test_required_mapped_equivalence_accepts_complete_child_evidence(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            lane_run_id = "lane-required-eqy-pass"
            published_dir = root / "published-design"
            design_config = published_dir / "config.yaml"
            lane_run_dir = published_dir / "runs" / f"PI_LANE_{lane_run_id}"
            step_dir = lane_run_dir / "14-yosys-eqy"
            scratch = step_dir / "scratch"
            scratch.mkdir(parents=True)
            eqy_script = published_dir / "eqy" / "demo.eqy"
            eqy_script.parent.mkdir()
            eqy_script.write_text(
                render_eqy_script_for_name("demo"), encoding="utf-8"
            )
            eqy_gate_tcl = eqy_script.with_name("demo-gate.tcl")
            eqy_gate_tcl.write_text(render_eqy_gate_tcl(), encoding="utf-8")
            design_config.write_text(
                "meta:\n"
                "  version: 2\n"
                "  flow: Classic\n"
                "PDK: sky130A\n"
                "DESIGN_NAME: demo\n"
                "RUN_EQY: true\n"
                "EQY_SCRIPT: dir::eqy/demo.eqy\n",
                encoding="utf-8",
            )
            config = step_dir / "config.json"
            state_in = step_dir / "state_in.json"
            state_out = step_dir / "state_out.json"
            status_file = scratch / "PASS"
            log = step_dir / "eqy.log"
            config.write_text(
                json.dumps(
                    {
                        "meta": {"step": "Yosys.EQY"},
                        "PDK": "sky130A",
                        "EQY_SCRIPT": str(eqy_script),
                    }
                ),
                encoding="utf-8",
            )
            state_in.write_text('{"stage": "input"}\n', encoding="utf-8")
            state_out.write_text('{"stage": "output"}\n', encoding="utf-8")
            status_file.write_text(
                "Elapsed clock time: 0:00:01\n", encoding="utf-8"
            )
            log.write_text(
                "Successfully proved designs equivalent\nDONE (PASS, rc=0)\n",
                encoding="utf-8",
            )

            def attestation(path: Path) -> dict:
                return {
                    "path": str(path),
                    "sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
                    "size_bytes": path.stat().st_size,
                }

            evidence_files = {
                "config": attestation(config),
                "eqy_script": attestation(eqy_script),
                "eqy_gate_tcl": attestation(eqy_gate_tcl),
                "state_in": attestation(state_in),
                "state_out": attestation(state_out),
                "logs": [attestation(log)],
                "status": attestation(status_file),
            }
            mapped_equivalence = {
                "schema_version": 2,
                "requested": True,
                "status": "pass",
                "gate": "pass",
                "provider": "LibreLane Classic / Yosys.EQY",
                "provider_version": "LibreLane 3.0.5 / EQY 0.62",
                "scope": (
                    "RTL versus synthesized PDK-mapped netlist equivalence only; "
                    "this is not post-layout LEC"
                ),
                "run_id": lane_run_id,
                "run_dir": str(lane_run_dir),
                "step": {
                    "id": "Yosys.EQY",
                    "ordinal": 14,
                    "directory": str(step_dir),
                },
                "evidence": evidence_files,
                "verdict": {
                    "status": "PASS",
                    "source": "eqy_status_file_and_step_log",
                    "status_path": str(status_file),
                    "status_sha256": evidence_files["status"]["sha256"],
                    "log_path": str(log),
                    "log_sha256": evidence_files["logs"][0]["sha256"],
                    "status_marker": "PASS status file",
                    "full_design_marker": "Successfully proved designs equivalent",
                    "log_marker": "DONE (PASS, rc=0)",
                },
                "artifact": str(state_out),
                "logs": [str(log)],
                "reasons": [],
            }
            state = {
                "schema_version": 2,
                "state_file": str(root / "state.json"),
                "run_id": "run-required-eqy-pass",
                "design_name": "demo",
                "pdk": "sky130A",
                "mapped_equivalence_mode": "required",
                "status": "running",
                "phase": "lane_running",
                "started_epoch": 1,
                "pipeline_log": str(root / "pipeline.log"),
                "lane_run_id": lane_run_id,
                "lane_state": {"status": "running"},
                "active_lane_attempt_id": "lane-attempt-001",
                "lane_attempts": [
                    {
                        "attempt_id": "lane-attempt-001",
                        "lane_run_id": lane_run_id,
                        "launch_key": "launch-required-eqy-pass",
                        "status": "running",
                        "started_at": "2026-01-01T00:00:00+00:00",
                        "completed_at": None,
                        "supersedes": None,
                    }
                ],
                "design_dir": str(published_dir),
                "design_config": str(design_config),
            }
            (root / "pipeline.log").write_text("", encoding="utf-8")
            save_state(state)
            response = SimpleNamespace(
                returncode=0,
                stdout=json.dumps(
                    lane_response(
                        lane_run_id,
                        status="completed",
                        process_status="completed",
                        flow_scope="full",
                        delivery_gate="pass",
                        delivery_gate_reasons=[],
                        attempt_id="lane-attempt-001",
                        idempotency_key="launch-required-eqy-pass",
                        design_dir=str(published_dir),
                        config_path=str(design_config),
                        config_sha256=hashlib.sha256(
                            design_config.read_bytes()
                        ).hexdigest(),
                        librelane_run_dir=str(lane_run_dir),
                        step_history=[
                            {
                                "id": "Yosys.EQY",
                                "ordinal": 14,
                                "directory": str(step_dir),
                                "status": "completed",
                                "state_out": str(state_out),
                                "logs": [str(log)],
                            }
                        ],
                        mapped_equivalence=mapped_equivalence,
                        assurance={
                            "pdk": "sky130A",
                            "open_pdk": True,
                            "foundry_signoff": False,
                            "tapeout_guarantee": False,
                            "limitations": [
                                "Open-source PDK checks are not commercial foundry signoff."
                            ],
                        },
                    )
                ),
                stderr="",
            )
            with patch(
                "rtl_closure.pipeline.subprocess.run", return_value=response
            ):
                current = refresh_state(state)
            replayed = json.loads(response.stdout)
            replayed["mapped_equivalence"]["run_id"] = "another-lane-run"
            self.assertIn(
                "mapped-equivalence evidence belongs to another run",
                _mapped_equivalence_evidence_errors(
                    replayed,
                    expected_run_id=lane_run_id,
                    expected_pdk="sky130A",
                    expected_design_config=design_config,
                ),
            )
            outside = root / "foreign-state-out.json"
            outside.write_text('{"stage": "forged"}\n', encoding="utf-8")
            forged = json.loads(response.stdout)
            forged["mapped_equivalence"]["evidence"]["state_out"] = attestation(
                outside
            )
            forged["mapped_equivalence"]["artifact"] = str(outside)
            forged["step_history"][0]["state_out"] = str(outside)
            self.assertTrue(
                any(
                    "state_out path is outside the run" in error
                    for error in _mapped_equivalence_evidence_errors(
                        forged,
                        expected_run_id=lane_run_id,
                        expected_pdk="sky130A",
                        expected_design_config=design_config,
                    )
                )
            )
            other_design = root / "other-published-design"
            other_design.mkdir()
            other_config = other_design / "config.yaml"
            other_config.write_bytes(design_config.read_bytes())
            self.assertTrue(
                any(
                    "run directory identity is invalid" in error
                    or "does not match publication" in error
                    for error in _mapped_equivalence_evidence_errors(
                        json.loads(response.stdout),
                        expected_run_id=lane_run_id,
                        expected_pdk="sky130A",
                        expected_design_config=other_config,
                    )
                )
            )
            self.assertEqual(current["status"], "completed")
            log.write_text("tampered after completion\n", encoding="utf-8")
            degraded = refresh_state(current)

        self.assertEqual(degraded["status"], "incomplete")
        self.assertEqual(degraded["phase"], "lane_delivery_incomplete")
        self.assertEqual(
            degraded["failure_code"],
            "release_integrity/mapped_equivalence_evidence",
        )
        report = build_report(current)
        self.assertEqual(report["mapped_equivalence"]["artifact"], str(state_out))
        self.assertFalse(report["assurance"]["tapeout_guarantee"])
        rendered = format_state(current)
        self.assertIn("mapped_equivalence: pass (gate=pass)", rendered)
        self.assertIn("open_pdk: True", rendered)
        self.assertIn("foundry_signoff: False", rendered)

    def test_refresh_does_not_increase_terminal_elapsed_time(self):
        state = {
            "run_id": "run-1",
            "status": "failed",
            "phase": "failed",
            "elapsed_seconds": 42,
            "started_epoch": 1,
        }

        current = refresh_state(state)

        self.assertIs(current, state)
        self.assertEqual(current["elapsed_seconds"], 42)

    def test_stale_status_snapshot_cannot_roll_back_worker_phase(self):
        with tempfile.TemporaryDirectory() as temp:
            state_file = Path(temp) / "state.json"
            state = {
                "state_file": str(state_file),
                "run_id": "run-1",
                "status": "running",
                "phase": "queued",
            }
            save_state(state)
            stale = dict(state)
            _update(state, phase="repair_evaluating", repair={"round": 2})
            _update(stale, announce=False, elapsed_seconds=5)
            current = json.loads(state_file.read_text())
            self.assertEqual(current["phase"], "repair_evaluating")
            self.assertEqual(current["repair"]["round"], 2)
            self.assertEqual(current["elapsed_seconds"], 5)

    def test_child_start_clears_stale_parent_errors(self):
        spec = load_spec(EXAMPLE)
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            config = root / "config.yaml"
            config.write_text("meta: {}\n", encoding="utf-8")
            state = {
                "state_file": str(root / "state.json"),
                "run_id": "run-1",
                "status": "running",
                "phase": "restarting_lane",
                "design_config": str(config),
                "error": "old error",
                "lane_status_error": "old transport error",
            }
            save_state(state)
            def launch(command: list[str], **_kwargs: object) -> SimpleNamespace:
                launch_key = command[command.index("--idempotency-key") + 1]
                return SimpleNamespace(
                    returncode=0,
                    stdout=json.dumps(
                        lane_response(
                            "lane-2",
                            workflow_id="run-1",
                            attempt_id="lane-attempt-001",
                            idempotency_key=launch_key,
                        )
                    ),
                    stderr="",
                )

            with patch("rtl_closure.pipeline.subprocess.run", side_effect=launch):
                current = _start_lane_unlocked(spec, state)
            self.assertIsNone(current["error"])
            self.assertIsNone(current["lane_status_error"])
            self.assertEqual(current["lane_run_id"], "lane-2")
            self.assertEqual(current["active_lane_attempt_id"], "lane-attempt-001")
            self.assertEqual(len(current["lane_attempts"]), 1)
            self.assertEqual(current["lane_attempts"][0]["lane_run_id"], "lane-2")
            self.assertEqual(current["lane_attempts"][0]["status"], "running")

    def test_unknown_child_launch_reuses_the_same_idempotency_key(self):
        spec = load_spec(EXAMPLE)
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            config = root / "config.yaml"
            config.write_text("meta: {}\n", encoding="utf-8")
            state = {
                "state_file": str(root / "state.json"),
                "events_file": str(root / "events.jsonl"),
                "event_sequence": 0,
                "run_id": "run-1",
                "status": "running",
                "phase": "restarting_lane",
                "design_config": str(config),
                "origin": {"workflow_id": "workflow-1"},
            }
            save_state(state)
            launch_count = 0

            def launch(command: list[str], **_kwargs: object) -> SimpleNamespace:
                nonlocal launch_count
                launch_count += 1
                if launch_count == 1:
                    raise subprocess.TimeoutExpired(cmd="lane-agent", timeout=60)
                launch_key = command[command.index("--idempotency-key") + 1]
                return SimpleNamespace(
                    returncode=0,
                    stdout=json.dumps(
                        lane_response(
                            "lane-2",
                            workflow_id="workflow-1",
                            attempt_id="lane-attempt-001",
                            idempotency_key=launch_key,
                        )
                    ),
                    stderr="",
                )

            with patch(
                "rtl_closure.pipeline.subprocess.run",
                side_effect=launch,
            ) as launcher:
                with self.assertRaises(LaneLaunchError):
                    _start_lane_unlocked(spec, state)
                current = _start_lane_unlocked(spec, state)

            first_command = launcher.call_args_list[0].args[0]
            second_command = launcher.call_args_list[1].args[0]
            first_key = first_command[first_command.index("--idempotency-key") + 1]
            second_key = second_command[second_command.index("--idempotency-key") + 1]
            self.assertEqual(first_key, second_key)
            self.assertEqual(len(current["lane_attempts"]), 1)
            self.assertEqual(current["lane_attempts"][0]["launch_retries"], 1)
            self.assertEqual(current["lane_attempts"][0]["lane_run_id"], "lane-2")

    def test_child_terminal_transition_records_one_durable_event(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            state = {
                "state_file": str(root / "state.json"),
                "events_file": str(root / "events.jsonl"),
                "event_sequence": 0,
                "run_id": "run-terminal",
                "design_name": "demo",
                "pdk": "sky130A",
                "status": "running",
                "phase": "lane_running",
                "started_epoch": 1,
                "pipeline_log": str(root / "pipeline.log"),
                "lane_run_id": "lane-terminal",
                "active_lane_attempt_id": "lane-attempt-001",
                "lane_attempts": [
                    {
                        "attempt_id": "lane-attempt-001",
                        "lane_run_id": "lane-terminal",
                        "status": "running",
                    }
                ],
                "lane_state": {"status": "running"},
                "origin": {"workflow_id": "workflow-1"},
            }
            (root / "pipeline.log").write_text("", encoding="utf-8")
            save_state(state)
            response = SimpleNamespace(
                returncode=0,
                stdout=json.dumps(
                    lane_response(
                        "lane-terminal",
                        status="completed",
                        workflow_id="workflow-1",
                        attempt_id="lane-attempt-001",
                        process_status="completed",
                        flow_scope="full",
                        delivery_gate="pass",
                    )
                ),
                stderr="",
            )
            with patch(
                "rtl_closure.pipeline.subprocess.run", return_value=response
            ):
                first = refresh_state(state)
                second = refresh_state(first)

            events = [
                json.loads(line)
                for line in (root / "events.jsonl").read_text().splitlines()
            ]
            self.assertEqual(len(events), 1)
            self.assertEqual(events[0]["type"], "run_terminal")
            self.assertEqual(events[0]["workflow_id"], "workflow-1")
            self.assertEqual(events[0]["lane_attempt_id"], "lane-attempt-001")
            self.assertEqual(second["lane_attempts"][0]["status"], "completed")
            self.assertIsNotNone(second["lane_attempts"][0]["completed_at"])

    def test_start_idempotency_key_returns_the_existing_run(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            current = root / "current"
            popen_calls = []

            def fake_popen(*args, **kwargs):
                popen_calls.append((args, kwargs))
                return SimpleNamespace(pid=4321)

            with (
                patch("rtl_closure.pipeline.STATE_ROOT", root),
                patch("rtl_closure.pipeline.CURRENT", current),
                patch("rtl_closure.pipeline.subprocess.Popen", side_effect=fake_popen),
                patch(
                    "rtl_closure.pipeline._read_process_identity",
                    return_value={
                        "pid": 4321,
                        "start_time_ticks": 10,
                        "cmdline_sha256": "a" * 64,
                    },
                ),
            ):
                first = start_pipeline(
                    EXAMPLE,
                    generate_only=True,
                    workflow_id="workflow-1",
                    idempotency_key="request-1",
                )
                second = start_pipeline(
                    EXAMPLE,
                    generate_only=True,
                    workflow_id="workflow-1",
                    idempotency_key="request-1",
                )

            self.assertEqual(first["run_id"], second["run_id"])
            self.assertEqual(len(popen_calls), 1)
            self.assertEqual(second["origin"]["workflow_id"], "workflow-1")

    def test_first_json_start_emits_exactly_one_json_document(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            current = root / "current"
            output = io.StringIO()
            args = build_parser().parse_args(
                [
                    "start",
                    "--spec",
                    str(EXAMPLE),
                    "--generate-only",
                    "--workflow-id",
                    "workflow-1",
                    "--idempotency-key",
                    "request-1",
                    "--json",
                ]
            )

            with (
                patch("rtl_closure.pipeline.STATE_ROOT", root),
                patch("rtl_closure.pipeline.CURRENT", current),
                patch(
                    "rtl_closure.pipeline.subprocess.Popen",
                    return_value=SimpleNamespace(pid=4321),
                ),
                patch(
                    "rtl_closure.pipeline._read_process_identity",
                    return_value={
                        "pid": 4321,
                        "start_time_ticks": 10,
                        "cmdline_sha256": "a" * 64,
                    },
                ),
                redirect_stdout(output),
            ):
                self.assertEqual(cmd_start(args), 0)

            payload = json.loads(output.getvalue())
            self.assertEqual(payload["status"], "running")
            self.assertEqual(payload["phase"], "queued")
            self.assertEqual(payload["origin"]["workflow_id"], "workflow-1")

    def test_current_pointer_is_written_after_worker_pid_registration(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            current = root / "current"

            def fake_popen(*args, **kwargs):
                del args, kwargs
                self.assertFalse(current.exists())
                return SimpleNamespace(pid=4321)

            with (
                patch("rtl_closure.pipeline.STATE_ROOT", root),
                patch("rtl_closure.pipeline.CURRENT", current),
                patch("rtl_closure.pipeline.subprocess.Popen", side_effect=fake_popen),
                patch(
                    "rtl_closure.pipeline._read_process_identity",
                    return_value={
                        "pid": 4321,
                        "start_time_ticks": 10,
                        "cmdline_sha256": "a" * 64,
                    },
                ),
            ):
                state = start_pipeline(EXAMPLE, generate_only=True)
            self.assertEqual(state["pid"], 4321)
            self.assertEqual(current.read_text(encoding="utf-8").strip(), state["run_id"])


if __name__ == "__main__":
    unittest.main()
