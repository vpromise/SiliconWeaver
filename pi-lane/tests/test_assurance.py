import hashlib
import json
import tempfile
import unittest
from pathlib import Path

from pi_lane.assurance import (
    EQY_SCOPE,
    OPEN_PDK_LIMITATIONS,
    evaluate_eqy_assurance,
    open_pdk_assurance,
)


class EqyAssuranceTests(unittest.TestCase):
    EQY_SCRIPT = "[gold]\nread_verilog demo.sv\n"
    EQY_SCRIPT_SHA256 = hashlib.sha256(EQY_SCRIPT.encode("utf-8")).hexdigest()
    EQY_GATE_TCL = "yosys read_verilog demo.nl.v\n"
    EQY_GATE_TCL_SHA256 = hashlib.sha256(
        EQY_GATE_TCL.encode("utf-8")
    ).hexdigest()

    def make_step(self, root: Path, state: str = '{"state": "complete"}') -> dict:
        design_dir = root / "design"
        run_dir = design_dir / "runs" / "PI_LANE_eqy-test"
        step_dir = run_dir / "76-yosys-eqy"
        step_dir.mkdir(parents=True)
        script = design_dir / "eqy" / "demo.eqy"
        script.parent.mkdir()
        script.write_text(self.EQY_SCRIPT, encoding="utf-8")
        script.with_name("demo-gate.tcl").write_text(
            self.EQY_GATE_TCL, encoding="utf-8"
        )
        (step_dir / "config.json").write_text(
            json.dumps(
                {
                    "meta": {"step": "Yosys.EQY"},
                    "PDK": "sky130A",
                    "EQY_SCRIPT": str(script),
                }
            ),
            encoding="utf-8",
        )
        (step_dir / "state_in.json").write_text(
            '{"netlist":"demo.v"}', encoding="utf-8"
        )
        state_out = step_dir / "state_out.json"
        state_out.write_text(state, encoding="utf-8")
        log = step_dir / "yosys-eqy.log"
        log.write_text(
            "Successfully proved designs equivalent\nDONE (PASS, rc=0)\n",
            encoding="utf-8",
        )
        scratch = step_dir / "scratch"
        scratch.mkdir()
        (scratch / "PASS").write_text(
            "Elapsed clock time: 0:00:01\n", encoding="utf-8"
        )
        return {
            "ordinal": 76,
            "id": "Yosys.EQY",
            "directory": str(step_dir),
            "completed": True,
            "state_out": str(state_out),
            "commands": None,
            "logs": [str(log)],
            "runtime": None,
        }

    def evaluate(self, root: Path, steps: list[dict], **kwargs: object) -> dict:
        return evaluate_eqy_assurance(
            {
                "RUN_EQY": True,
                "EQY_SCRIPT_SHA256": self.EQY_SCRIPT_SHA256,
                "EQY_GATE_TCL_SHA256": self.EQY_GATE_TCL_SHA256,
            },
            "sky130A",
            steps,
            run_id="eqy-test",
            run_dir=root / "design" / "runs" / "PI_LANE_eqy-test",
            **kwargs,
        )

    def test_complete_unique_eqy_evidence_passes(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            step = self.make_step(root)
            result = self.evaluate(root, [step])

        self.assertTrue(result["requested"])
        self.assertEqual(result["status"], "pass")
        self.assertEqual(result["gate"], "pass")
        self.assertEqual(result["artifact"], step["state_out"])
        self.assertEqual(result["logs"], step["logs"])
        self.assertEqual(result["reasons"], [])
        self.assertIn("RTL versus synthesized PDK-mapped netlist", result["scope"])
        self.assertIn("not post-layout LEC", result["scope"])
        self.assertEqual(result["scope"], EQY_SCOPE)
        self.assertEqual(result["schema_version"], 2)
        self.assertEqual(result["provider"], "LibreLane Classic / Yosys.EQY")
        self.assertEqual(result["provider_version"], "LibreLane 3.0.5 / EQY 0.62")
        self.assertEqual(result["step"]["ordinal"], 76)
        self.assertEqual(result["verdict"]["status"], "PASS")
        self.assertEqual(
            result["verdict"]["source"], "eqy_status_file_and_step_log"
        )
        self.assertEqual(result["evidence"]["status"]["path"], result["verdict"]["status_path"])

    def test_schema5_eqy_content_is_read_from_the_immutable_snapshot(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            step = self.make_step(root)
            design = root / "design"
            logical_script = design / "eqy" / "demo.eqy"
            logical_gate_tcl = logical_script.with_name("demo-gate.tcl")
            snapshot = root / "controller" / "input-snapshot"
            snapshot_script = snapshot / "eqy" / "demo.eqy"
            snapshot_script.parent.mkdir(parents=True)
            snapshot_script.write_text(self.EQY_SCRIPT, encoding="utf-8")
            snapshot_gate_tcl = snapshot_script.with_name("demo-gate.tcl")
            snapshot_gate_tcl.write_text(self.EQY_GATE_TCL, encoding="utf-8")

            logical_script.unlink()
            logical_gate_tcl.unlink()
            result = self.evaluate(
                root,
                [step],
                design_dir=design,
                input_snapshot_dir=snapshot,
            )

            self.assertEqual(result["gate"], "pass")
            self.assertEqual(
                result["evidence"]["eqy_script"]["path"], str(snapshot_script)
            )
            self.assertEqual(
                result["evidence"]["eqy_script"]["logical_path"],
                str(logical_script),
            )
            snapshot_script.write_text("changed\n", encoding="utf-8")
            changed = self.evaluate(
                root,
                [step],
                design_dir=design,
                input_snapshot_dir=snapshot,
            )

        self.assertEqual(changed["gate"], "fail")
        self.assertIn("script SHA-256", changed["reasons"][0])

    def test_missing_eqy_evidence_is_incomplete(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            (root / "design" / "runs" / "PI_LANE_eqy-test").mkdir(parents=True)
            result = self.evaluate(root, [])

        self.assertTrue(result["requested"])
        self.assertEqual(result["status"], "incomplete")
        self.assertEqual(result["gate"], "fail")
        self.assertIsNone(result["artifact"])
        self.assertEqual(result["logs"], [])
        self.assertIn("missing", result["reasons"][0])

    def test_disabled_eqy_is_not_requested(self):
        result = evaluate_eqy_assurance({"RUN_EQY": False}, "sky130A", [])

        self.assertFalse(result["requested"])
        self.assertEqual(result["status"], "not_requested")
        self.assertEqual(result["gate"], "not_applicable")

    def test_eqy_on_non_sky130_is_unsupported_and_fails_gate(self):
        result = evaluate_eqy_assurance(
            {
                "RUN_EQY": True,
                "EQY_SCRIPT_SHA256": self.EQY_SCRIPT_SHA256,
                "EQY_GATE_TCL_SHA256": self.EQY_GATE_TCL_SHA256,
            },
            "gf180mcuD",
            [],
        )

        self.assertTrue(result["requested"])
        self.assertEqual(result["status"], "unsupported")
        self.assertEqual(result["gate"], "fail")

    def test_empty_state_is_incomplete(self):
        for state in ("", " \n", "{}"):
            with self.subTest(state=repr(state)), tempfile.TemporaryDirectory() as temp:
                step = self.make_step(Path(temp), state=state)
                result = self.evaluate(Path(temp), [step])

            self.assertEqual(result["status"], "incomplete")
            self.assertEqual(result["gate"], "fail")
            self.assertTrue(result["reasons"])

    def test_duplicate_and_escaped_evidence_never_pass(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            step = self.make_step(root)
            duplicate = self.evaluate(root, [step, dict(step)])
            outside = root / "outside.json"
            outside.write_text('{"state": "complete"}', encoding="utf-8")
            escaped_step = dict(step, state_out=str(outside))
            escaped = self.evaluate(root, [escaped_step])

        self.assertEqual(duplicate["gate"], "fail")
        self.assertIn("multiple", duplicate["reasons"][0])
        self.assertEqual(escaped["gate"], "fail")
        self.assertIn("direct child", escaped["reasons"][0])

    def test_eqy_requires_both_real_status_and_log_verdicts(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            step = self.make_step(root)
            step_dir = Path(step["directory"])
            (step_dir / "yosys-eqy.log").write_text("{}\n", encoding="utf-8")
            no_log_verdict = self.evaluate(root, [step])
            self.assertEqual(no_log_verdict["gate"], "fail")
            self.assertIn("full-design PASS verdict", no_log_verdict["reasons"][0])

            (step_dir / "yosys-eqy.log").write_text(
                "Successfully proved designs equivalent\nDONE (PASS, rc=0)\n",
                encoding="utf-8",
            )
            (step_dir / "scratch" / "PASS").write_text("", encoding="utf-8")
            no_status_verdict = self.evaluate(root, [step])
            self.assertEqual(no_status_verdict["gate"], "fail")
            self.assertIn("status file is empty", no_status_verdict["reasons"][0])

            (step_dir / "scratch" / "PASS").write_text(
                "Elapsed clock time: 0:00:01\n", encoding="utf-8"
            )
            (step_dir / "scratch" / "FAIL").write_text("failed\n", encoding="utf-8")
            contradictory = self.evaluate(root, [step])
            self.assertEqual(contradictory["gate"], "fail")
            self.assertIn("failure status FAIL", contradictory["reasons"][0])

    def test_eqy_is_bound_to_managed_run_and_non_symlink_evidence(self):
        with tempfile.TemporaryDirectory() as temp, tempfile.TemporaryDirectory() as other:
            root = Path(temp)
            step = self.make_step(root)
            wrong_run = evaluate_eqy_assurance(
                {
                    "RUN_EQY": True,
                    "EQY_SCRIPT_SHA256": self.EQY_SCRIPT_SHA256,
                    "EQY_GATE_TCL_SHA256": self.EQY_GATE_TCL_SHA256,
                },
                "sky130A",
                [step],
                run_id="different",
                run_dir=root / "design" / "runs" / "PI_LANE_eqy-test",
            )
            self.assertEqual(wrong_run["gate"], "fail")
            self.assertIn("run directory", wrong_run["reasons"][0])

            outside = Path(other) / "state_out.json"
            outside.write_text('{"state":"complete"}')
            state_out = Path(step["state_out"])
            state_out.unlink()
            state_out.symlink_to(outside)
            symlinked = self.evaluate(root, [step])
            self.assertEqual(symlinked["gate"], "fail")
            self.assertIn("canonical", symlinked["reasons"][0])


class OpenPdkAssuranceTests(unittest.TestCase):
    def test_open_pdk_statement_has_fixed_non_foundry_boundaries(self):
        statement = open_pdk_assurance("sky130A")

        self.assertTrue(statement["open_pdk"])
        self.assertFalse(statement["foundry_signoff"])
        self.assertFalse(statement["tapeout_guarantee"])
        self.assertEqual(statement["limitations"], OPEN_PDK_LIMITATIONS)

    def test_non_open_pdk_is_rejected(self):
        with self.assertRaises(ValueError):
            open_pdk_assurance("commercial-pdk")


if __name__ == "__main__":
    unittest.main()
