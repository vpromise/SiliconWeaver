from __future__ import annotations

import tempfile
import unittest
from contextlib import redirect_stdout
from io import StringIO
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from pi_lane.cli import build_parser, cmd_list, doctor_report
from pi_lane.config import load_design_config
from pi_lane.controller import PDK_REVISIONS, build_docker_command, start_run
from pi_lane.classic import LIBRELANE_IMAGE, LIBRELANE_VERSION


class DoctorAndFlowControlTests(unittest.TestCase):
    def test_doctor_requires_an_explicit_installed_target_pdk(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            state_root = root / "state"
            pdk_root = root / "pdk"
            pdk_root.mkdir()
            family, revision = PDK_REVISIONS["sky130A"]
            version = pdk_root / "ciel" / family / "versions" / revision / "sky130A"
            marker = version / "libs.tech" / "librelane" / "config.tcl"
            marker.parent.mkdir(parents=True)
            marker.write_text("# marker\n", encoding="utf-8")
            (version / "SOURCES").write_text(
                f"open_pdks {revision}\n", encoding="utf-8"
            )
            version.chmod(0o755)
            for path in version.rglob("*"):
                path.chmod(0o755 if path.is_dir() else 0o444)
            current = pdk_root / "ciel" / family / "current"
            current.write_text(revision, encoding="ascii")
            (pdk_root / "sky130A").symlink_to(version, target_is_directory=True)
            with (
                patch("pi_lane.cli.STATE_ROOT", state_root),
                patch("pi_lane.cli.shutil.which", return_value="/usr/bin/docker"),
                patch("pi_lane.cli.image_id", return_value="sha256:" + "a" * 64),
                patch(
                    "pi_lane.cli.image_librelane_version",
                    return_value=LIBRELANE_VERSION,
                ),
            ):
                unspecified = doctor_report(
                    image=LIBRELANE_IMAGE, pdk_root=pdk_root, target_pdk=None
                )
                missing = doctor_report(
                    image=LIBRELANE_IMAGE, pdk_root=pdk_root, target_pdk="gf180mcuD"
                )
                ready = doctor_report(
                    image=LIBRELANE_IMAGE, pdk_root=pdk_root, target_pdk="sky130A"
                )
        self.assertFalse(unspecified["ready"])
        self.assertTrue(
            any(
                "target PDK is unspecified" in reason
                for reason in unspecified["readiness_errors"]
            )
        )
        self.assertFalse(missing["ready"])
        self.assertTrue(ready["ready"])
        self.assertEqual(ready["target_pdk"], "sky130A")
        self.assertTrue(ready["checks"]["pdk_sky130A"])
        self.assertNotIn("pdk_gf180mcuD", ready["checks"])

    def test_parser_exposes_resume_steps_and_targeted_doctor(self):
        parser = build_parser()
        doctor = parser.parse_args(["doctor", "--pdk", "sky130A", "--json"])
        resume = parser.parse_args(["resume", "--run-id", "old", "--json"])
        steps = parser.parse_args(["steps", "--json"])
        events = parser.parse_args(
            ["events", "--run-id", "old", "--after-sequence", "12", "--limit", "7"]
        )
        start = parser.parse_args(
            [
                "start",
                "--config",
                "/tmp/config.yaml",
                "--workflow-id",
                "workflow-1",
                "--attempt-id",
                "attempt-2",
                "--idempotency-key",
                "request-3",
                "--origin",
                "rtl-closure",
                "--physical-plan",
                "/tmp/physical-plan.json",
            ]
        )
        reconcile = parser.parse_args(["reconcile", "--run-id", "old", "--json"])
        plan_start = parser.parse_args(
            ["plan-start", "--config", "/tmp/config.yaml", "--geometry-lock", "/tmp/human-lock.json", "--idempotency-key", "plan-key", "--json"]
        )
        plan_status = parser.parse_args(["plan-status", "--plan-id", "plan-1", "--json"])
        listing = parser.parse_args(["list", "--active", "--limit", "1", "--json"])
        self.assertEqual(doctor.pdk, "sky130A")
        self.assertEqual(resume.run_id, "old")
        self.assertTrue(steps.json)
        self.assertEqual(events.after_sequence, 12)
        self.assertEqual(events.limit, 7)
        self.assertEqual(start.workflow_id, "workflow-1")
        self.assertEqual(start.attempt_id, "attempt-2")
        self.assertEqual(start.idempotency_key, "request-3")
        self.assertEqual(start.origin, "rtl-closure")
        self.assertEqual(start.physical_plan, Path("/tmp/physical-plan.json"))
        self.assertEqual(plan_start.geometry_lock, Path("/tmp/human-lock.json"))
        self.assertEqual(plan_start.idempotency_key, "plan-key")
        self.assertEqual(plan_status.plan_id, "plan-1")
        self.assertEqual(reconcile.run_id, "old")
        self.assertTrue(reconcile.json)
        self.assertTrue(listing.active)
        self.assertEqual(listing.limit, 1)
        self.assertTrue(listing.json)

    def test_list_command_forwards_active_filter(self):
        output = StringIO()
        with (
            patch("pi_lane.cli.list_runs", return_value=[]) as list_runs,
            redirect_stdout(output),
        ):
            result = cmd_list(SimpleNamespace(active=True, limit=1, json=True))

        self.assertEqual(result, 0)
        self.assertEqual(output.getvalue().strip(), "[]")
        list_runs.assert_called_once_with(active_only=True, limit=1)

    def test_direct_from_is_rejected_before_runtime_discovery(self):
        with tempfile.TemporaryDirectory() as temp:
            config = Path(temp) / "config.yaml"
            config.write_text(
                "DESIGN_NAME: demo\nPDK: sky130A\nmeta:\n  flow: Classic\n",
                encoding="utf-8",
            )
            with self.assertRaisesRegex(ValueError, "lane-agent resume"):
                start_run(config, frm="OpenROAD.Floorplan")
            with self.assertRaisesRegex(ValueError, "unknown LibreLane Classic Step"):
                start_run(config, to="floorplan")

    def test_resume_command_mounts_the_validated_initial_state(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            config_path = root / "config.yaml"
            config_path.write_text("DESIGN_NAME: demo\nPDK: sky130A\n", encoding="utf-8")
            config = load_design_config(config_path)
            state_path = root / "runs" / "old" / "06-yosys" / "state_out.json"
            state_path.parent.mkdir(parents=True)
            state_path.write_text('{"netlist":"demo.nl.v"}', encoding="utf-8")
            command, _ = build_docker_command(
                config,
                run_id="new",
                tag="tag",
                pdk_root=root / "pdk",
                overrides=[],
                frm="Checker.YosysUnmappedCells",
                to=None,
                jobs=2,
                runtime_image_ref="sha256:" + "d" * 64,
                initial_state=state_path,
            )
        index = command.index("--with-initial-state")
        self.assertEqual(command[index + 1], str(state_path))
        self.assertLess(index, command.index("--from"))


if __name__ == "__main__":
    unittest.main()
