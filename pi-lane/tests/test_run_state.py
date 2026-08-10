from __future__ import annotations

import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from pi_lane.run_state import (
    StateConflictError,
    commit_state,
    load_state,
    pid_alive,
    pid_identity_alive,
    pid_start_time,
    read_event_journal,
    read_terminal_event,
)


class RevisionedRunStateTests(unittest.TestCase):
    def state(self, root: Path) -> dict[str, object]:
        return {
            "schema_version": 6,
            "state_revision": 0,
            "run_id": "run-1",
            "workflow_id": "workflow-1",
            "attempt_id": "attempt-1",
            "origin": {"component": "test"},
            "controller_dir": str(root),
            "state_file": str(root / "state.json"),
            "controller_events_file": str(root / "controller-events.jsonl"),
            "status": "running",
        }

    def test_revision_cas_rejects_a_stale_writer(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            first = self.state(root)
            commit_state(first, expected_revision=0)
            stale = dict(first)
            first["status_detail"] = "newer"
            commit_state(first, expected_revision=1)
            stale["status_detail"] = "stale"
            with self.assertRaises(StateConflictError):
                commit_state(stale, expected_revision=1)
            saved = json.loads((root / "state.json").read_text(encoding="utf-8"))
        self.assertEqual(saved["state_revision"], 2)
        self.assertEqual(saved["status_detail"], "newer")

    def test_domain_revision_is_not_accepted_as_the_controller_revision(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            state = self.state(root)
            state.pop("state_revision")
            state["revision"] = 7
            (root / "state.json").write_text(json.dumps(state), encoding="utf-8")
            with self.assertRaisesRegex(RuntimeError, "state_revision"):
                commit_state(state, expected_revision=7)

    def test_old_run_schema_and_revision_field_are_rejected(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            run_root = root / "old-run"
            run_root.mkdir()
            old = self.state(run_root)
            old["run_id"] = "old-run"
            old["schema_version"] = 4
            (run_root / "state.json").write_text(json.dumps(old), encoding="utf-8")
            with patch("pi_lane.run_state.STATE_ROOT", root), self.assertRaisesRegex(
                RuntimeError, "schema.*unsupported"
            ):
                load_state("old-run")

            old["schema_version"] = 5
            (run_root / "state.json").write_text(json.dumps(old), encoding="utf-8")
            with patch("pi_lane.run_state.STATE_ROOT", root), self.assertRaisesRegex(
                RuntimeError, "schema.*unsupported"
            ):
                load_state("old-run")

            old["schema_version"] = 6
            old.pop("state_revision")
            old["revision"] = 3
            (run_root / "state.json").write_text(json.dumps(old), encoding="utf-8")
            with patch("pi_lane.run_state.STATE_ROOT", root), self.assertRaisesRegex(
                RuntimeError, "revision"
            ):
                load_state("old-run")

    def test_pdk_git_revision_is_not_the_controller_revision(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            pdk_revision = "8afc8346a57fe1ab7934ba5a6056ea8b43078e71"
            state = {
                "schema_version": 1,
                "kind": "pdk_install",
                "revision": pdk_revision,
                "run_id": "pdk-run",
                "state_file": str(root / "state.json"),
                "status": "completed",
            }
            (root / "state.json").write_text(json.dumps(state), encoding="utf-8")
            commit_state(state)
            commit_state(state)
            saved = json.loads((root / "state.json").read_text(encoding="utf-8"))
        self.assertEqual(saved["revision"], pdk_revision)
        self.assertEqual(saved["state_revision"], 2)

    def test_event_cursor_hash_chain_and_terminal_are_durable(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            state = self.state(root)
            commit_state(
                state,
                expected_revision=0,
                events=({"type": "run_started", "message": "started"},),
            )
            state["status"] = "completed"
            commit_state(
                state,
                expected_revision=1,
                events=(
                    {
                        "type": "run_completed",
                        "message": "completed",
                        "terminal": True,
                    },
                ),
            )
            # A replayed terminal transition is ignored, but auxiliary evidence
            # may continue after the main workflow outcome becomes immutable.
            commit_state(
                state,
                expected_revision=2,
                events=(
                    {"type": "run_completed", "terminal": True},
                    {"type": "late_event"},
                ),
            )
            first = read_event_journal(state, limit=1)
            second = read_event_journal(
                state,
                after_sequence=first["next_after_sequence"],
                generation=first["generation"],
                cursor_sha256=first["cursor_sha256"],
            )
            terminal = read_terminal_event(state)
        self.assertEqual([row["type"] for row in first["events"]], ["run_started"])
        self.assertTrue(first["has_more"])
        self.assertEqual(
            [row["type"] for row in second["events"]],
            ["run_completed", "late_event"],
        )
        self.assertTrue(second["events"][0]["terminal"])
        self.assertFalse(second["events"][1]["terminal"])
        self.assertEqual(state["event_sequence"], 3)
        self.assertEqual(state["terminal_event_id"], "run-1:2")
        self.assertEqual(terminal["event_id"], "run-1:2")
        self.assertEqual(terminal["type"], "run_completed")

    def test_event_schema_two_is_explicitly_rejected(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            state = self.state(root)
            commit_state(state, expected_revision=0, events=({"type": "run_started"},))
            journal = root / "controller-events.jsonl"
            row = json.loads(journal.read_text(encoding="utf-8"))
            row["schema_version"] = 2
            journal.write_text(json.dumps(row) + "\n", encoding="utf-8")

            with self.assertRaisesRegex(ValueError, "schema.*unsupported"):
                read_event_journal(state)

    def test_terminal_outcome_cannot_flip_after_terminal_event(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            state = self.state(root)
            state.update(
                {
                    "status": "failed",
                    "process_status": "failed",
                    "delivery_gate": "fail",
                    "failure": {"code": "LIBRELANE_FAILED"},
                }
            )
            commit_state(
                state,
                expected_revision=0,
                events=({"type": "run_failed", "terminal": True},),
            )
            proposed = dict(state)
            proposed.update(
                {
                    "status": "completed",
                    "process_status": "completed",
                    "delivery_gate": "pass",
                    "failure": None,
                }
            )

            with self.assertRaisesRegex(StateConflictError, "terminal.*immutable"):
                commit_state(proposed, expected_revision=1)

            saved = json.loads((root / "state.json").read_text(encoding="utf-8"))
            journal = read_event_journal(state)
            self.assertEqual(saved["status"], "failed")
            self.assertEqual(saved["delivery_gate"], "fail")
            self.assertEqual([row["type"] for row in journal["events"]], ["run_failed"])

    def test_terminal_runtime_identity_cannot_be_rebound(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            state = self.state(root)
            state.update(
                {
                    "status": "completed",
                    "image": "fixed:image",
                    "image_digest": "sha256:" + "d" * 64,
                    "runtime_image_ref": "sha256:" + "d" * 64,
                    "pdk": "sky130A",
                    "pdk_root": "/pdk",
                    "pdk_revision": "a" * 40,
                    "pdk_integrity_sha256": "b" * 64,
                }
            )
            commit_state(
                state,
                expected_revision=0,
                events=({"type": "run_completed", "terminal": True},),
            )
            rebound = dict(state)
            rebound["runtime_image_ref"] = "sha256:" + "e" * 64
            with self.assertRaisesRegex(
                StateConflictError, "runtime_image_ref"
            ):
                commit_state(rebound, expected_revision=1)

    def test_terminal_execution_and_checkpoint_identity_cannot_be_rebound(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            state = self.state(root)
            state.update(
                {
                    "status": "completed",
                    "config_sha256": "a" * 64,
                    "input_bundle_sha256": "b" * 64,
                    "design_dir": "/design",
                    "request_fingerprint": "c" * 64,
                    "artifacts": {
                        "latest_state": "/design/runs/run/01-step/state_out.json",
                        "visualizations": {"images": []},
                    },
                }
            )
            commit_state(
                state,
                expected_revision=0,
                events=({"type": "run_completed", "terminal": True},),
            )

            for key, replacement in (
                ("config_sha256", "d" * 64),
                ("input_bundle_sha256", "e" * 64),
                ("design_dir", "/other"),
                ("request_fingerprint", "f" * 64),
            ):
                proposed = dict(state)
                proposed[key] = replacement
                with self.subTest(key=key), self.assertRaisesRegex(
                    StateConflictError, key
                ):
                    commit_state(proposed, expected_revision=1)

            visualization_update = dict(state)
            visualization_update["artifacts"] = {
                **state["artifacts"],
                "visualizations": {"images": ["preview.png"]},
            }
            commit_state(visualization_update, expected_revision=1)

    def test_cursor_detects_a_rewritten_prefix(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            state = self.state(root)
            commit_state(
                state,
                expected_revision=0,
                events=({"type": "run_started"}, {"type": "status_changed"}),
            )
            first = read_event_journal(state, limit=1)
            journal = root / "controller-events.jsonl"
            rows = journal.read_text(encoding="utf-8").splitlines()
            rows[0] = rows[0].replace("run_started", "run_changed")
            journal.write_text("\n".join(rows) + "\n", encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "hash|prefix changed"):
                read_event_journal(
                    state,
                    after_sequence=1,
                    generation=first["generation"],
                    cursor_sha256=first["cursor_sha256"],
                )

    def test_pid_identity_requires_a_positive_matching_start_time(self):
        pid = os.getpid()
        start_time = pid_start_time(pid)
        self.assertIsInstance(start_time, int)
        self.assertGreater(start_time, 0)
        self.assertTrue(pid_identity_alive(pid, start_time))
        self.assertFalse(pid_identity_alive(pid, None))
        self.assertFalse(pid_identity_alive(pid, 0))
        self.assertFalse(pid_identity_alive(pid, True))
        self.assertFalse(pid_identity_alive(pid, start_time + 1))
        self.assertFalse(pid_identity_alive(True, start_time))

    def test_proc_stat_parser_handles_spaced_names_and_zombies_fail_closed(self):
        running_fields = ["S", *("0" for _ in range(18)), "123456"]
        running_stat = "4321 (worker with spaces) " + " ".join(running_fields)
        with (
            patch.object(Path, "is_file", return_value=True),
            patch.object(Path, "read_text", return_value=running_stat),
            patch("pi_lane.run_state.os.kill") as kill,
        ):
            self.assertTrue(pid_alive(4321))
            self.assertEqual(pid_start_time(4321), 123456)
        kill.assert_called_once_with(4321, 0)

        zombie_stat = running_stat.replace(") S ", ") Z ", 1)
        with (
            patch.object(Path, "is_file", return_value=True),
            patch.object(Path, "read_text", return_value=zombie_stat),
            patch("pi_lane.run_state.os.kill") as kill,
        ):
            self.assertFalse(pid_alive(4321))
        kill.assert_not_called()

        with (
            patch.object(Path, "is_file", return_value=True),
            patch.object(Path, "read_text", return_value="malformed stat"),
            patch("pi_lane.run_state.os.kill") as kill,
        ):
            self.assertFalse(pid_alive(4321))
            self.assertIsNone(pid_start_time(4321))
        kill.assert_not_called()


if __name__ == "__main__":
    unittest.main()
