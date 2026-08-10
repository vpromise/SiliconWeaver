from __future__ import annotations

import json
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from rtl_closure.universal import (
    UniversalPropertyError,
    _build_harness,
    _normalize_ports,
    _normalize_properties,
    _yosys_script,
    classify_universal_execution_log,
    merge_universal_result,
    render_universal_execution_inputs,
    run_universal_properties,
    universal_requirement_coverage,
)


PORTS = [
    {"name": "clk", "direction": "input", "width": 1},
    {"name": "rst_n", "direction": "input", "width": 1},
    {"name": "valid", "direction": "input", "width": 1},
    {"name": "data", "direction": "input", "width": 8},
    {"name": "ready", "direction": "output", "width": 1},
    {"name": "count", "direction": "output", "width": 8},
]
RESET = {"port": "rst_n", "active_low": True, "asynchronous": True}
RTL = """
module demo(
  input wire clk, input wire rst_n, input wire valid, input wire [7:0] data,
  output wire ready, output reg [7:0] count
);
assign ready = count != 8'hff;
always @(posedge clk or negedge rst_n)
  if (!rst_n) count <= 0;
  else if (valid && ready) count <= count + data;
endmodule
""".strip()


def property_row() -> dict:
    return {
        "id": "PROP_ACCEPT_INCREMENTS",
        "kind": "always_implication",
        "requirement_ids": ["REQ-COUNT-1"],
        "antecedent": {
            "op": "and",
            "args": [
                {"port": "valid", "at": "previous"},
                {"port": "ready", "at": "previous"},
            ],
        },
        "consequent": {
            "op": "eq",
            "left": {"port": "count", "at": "current"},
            "right": {
                "op": "add",
                "left": {"port": "count", "at": "previous"},
                "right": {"port": "data", "at": "previous"},
            },
        },
    }


class UniversalDslTests(unittest.TestCase):
    def test_rejects_unknown_port_and_arbitrary_expression_shape(self):
        unknown = property_row()
        unknown["consequent"] = {
            "op": "eq",
            "left": {"port": "internal_state"},
            "right": {"const": 0, "width": 1},
        }
        with self.assertRaisesRegex(UniversalPropertyError, "unknown port"):
            run_universal_properties(
                RTL, "demo", PORTS, "clk", RESET, [unknown]
            )

        injected = property_row()
        injected["antecedent"] = {"sv": "$finish;"}
        with self.assertRaisesRegex(UniversalPropertyError, "missing required field"):
            run_universal_properties(
                RTL, "demo", PORTS, "clk", RESET, [injected]
            )

    def test_rejects_width_mismatch_and_non_boolean_implication(self):
        mismatch = property_row()
        mismatch["consequent"] = {
            "op": "eq",
            "left": {"port": "count"},
            "right": {"const": 1, "width": 1},
        }
        with self.assertRaisesRegex(UniversalPropertyError, "operand widths differ"):
            run_universal_properties(
                RTL, "demo", PORTS, "clk", RESET, [mismatch]
            )

        non_boolean = property_row()
        non_boolean["antecedent"] = {"port": "data"}
        with self.assertRaisesRegex(UniversalPropertyError, "must each be one bit"):
            run_universal_properties(
                RTL, "demo", PORTS, "clk", RESET, [non_boolean]
            )

    def test_compiler_uses_symbolic_inputs_assert_and_temporal_induction(self):
        ports = _normalize_ports(PORTS)
        port_map = {port.name: port for port in ports}
        prop = _normalize_properties([property_row()], port_map)[0]
        harness = _build_harness(
            top="demo",
            ports=ports,
            clock_port="clk",
            reset={**RESET, "asserted_edges": 2},
            prop=prop,
            property_index=1,
        )
        script = _yosys_script(1, 8, 115)
        self.assertIn("(* anyseq *) wire __cm_port_valid", harness)
        self.assertIn("(* anyseq *) wire [7:0] __cm_port_data", harness)
        self.assertIn("always @($global_clock)", harness)
        self.assertIn("assert(__cm_property_holds)", harness)
        self.assertIn("__cm_prev_count", harness)
        self.assertNotIn("$finish", harness)
        self.assertIn("-tempinduct", script)
        self.assertIn("-prove-asserts", script)
        self.assertIn("-set-def-formal", script)


class UniversalExecutionTests(unittest.TestCase):
    def test_release_inputs_reconstruct_runtime_harness_and_script(self):
        rendered = render_universal_execution_inputs(
            top="demo",
            ports=PORTS,
            clock_port="clk",
            reset=RESET,
            property_value=property_row(),
            property_index=1,
            induction_depth=8,
            timeout=120,
            requirement_ids=["REQ-COUNT-1"],
        )
        self.assertIn("module __rtl_closure_universal_0001", rendered["harness"])
        self.assertIn("-tempinduct", rendered["script"])
        self.assertIn("-timeout 115", rendered["script"])

    def test_persisted_log_classifier_matches_runtime_verdicts(self):
        self.assertEqual(
            classify_universal_execution_log(
                0, "Temporal induction proof successful!"
            ),
            "pass",
        )
        self.assertEqual(
            classify_universal_execution_log(1, "model found: FAIL!"),
            "fail",
        )
        self.assertEqual(
            classify_universal_execution_log(None, "proof exceeded deadline"),
            "unknown",
        )
        self.assertEqual(classify_universal_execution_log(1, "no verdict"), "error")
        with self.assertRaisesRegex(UniversalPropertyError, "returncode"):
            classify_universal_execution_log(True, "proof successful")

    def test_mock_pass_is_reported_as_universal_and_bound_to_hashes(self):
        completed = subprocess.CompletedProcess(
            args=[],
            returncode=0,
            stdout="Temporal induction proof successful!",
            stderr="",
        )
        with patch(
            "rtl_closure.universal._run_container", return_value=completed
        ) as run:
            report = run_universal_properties(
                RTL,
                "demo",
                PORTS,
                "clk",
                RESET,
                [property_row()],
                timeout=30,
                induction_depth=6,
                requirement_ids=["REQ-COUNT-1", "REQ-PROTOCOL-2"],
            )
        self.assertEqual(report["status"], "pass")
        self.assertTrue(report["universal_proof"])
        self.assertEqual(report["passed_property_ids"], ["PROP_ACCEPT_INCREMENTS"])
        self.assertEqual(report["properties"][0]["requirement_ids"], ["REQ-COUNT-1"])
        self.assertEqual(len(report["rtl_sha256"]), 64)
        self.assertEqual(len(report["properties_sha256"]), 64)
        self.assertEqual(
            report["scope"]["time_horizon"],
            "unbounded when temporal induction succeeds",
        )
        self.assertEqual(
            report["scope"]["claim"],
            "listed_port_level_safety_properties_only",
        )
        self.assertTrue(report["scope"]["capabilities"]["safety_properties"])
        self.assertFalse(report["scope"]["capabilities"]["liveness_properties"])
        self.assertFalse(
            report["scope"]["capabilities"]["internal_state_references"]
        )
        self.assertFalse(report["complete_behavioral_proof"])
        self.assertFalse(report["complete_requirement_proof"])
        self.assertEqual(
            report["requirement_coverage"]["covered_requirement_ids"],
            ["REQ-COUNT-1"],
        )
        self.assertEqual(
            report["requirement_coverage"]["uncovered_requirement_ids"],
            ["REQ-PROTOCOL-2"],
        )
        self.assertFalse(
            report["requirement_coverage"]["semantic_completeness_claimed"]
        )
        command = run.call_args.args[0]
        self.assertIn("--network", command)
        self.assertIn("none", command)

    def test_mock_failed_proof_has_property_counterexample_and_artifacts(self):
        completed = subprocess.CompletedProcess(
            args=[],
            returncode=1,
            stdout="SAT temporal induction proof finished - model found: FAIL!",
            stderr="ERROR: Called with -verify and proof did fail!",
        )
        with tempfile.TemporaryDirectory() as temp:
            with patch(
                "rtl_closure.universal._run_container", return_value=completed
            ):
                report = run_universal_properties(
                    RTL,
                    "demo",
                    PORTS,
                    "clk",
                    RESET,
                    [property_row()],
                    Path(temp),
                )
            row = report["properties"][0]
            self.assertEqual(report["status"], "fail")
            self.assertFalse(report["universal_proof"])
            self.assertEqual(report["failed_property_ids"], ["PROP_ACCEPT_INCREMENTS"])
            self.assertEqual(row["status"], "fail")
            self.assertIsNotNone(row["counterexample"])
            self.assertFalse(row["counterexample"]["available"])
            self.assertEqual(Path(row["artifacts"]["dut"]).read_text(), RTL + "\n")
            self.assertTrue(Path(row["artifacts"]["harness"]).exists())
            self.assertTrue((Path(temp) / "universal-properties.json").exists())

    def test_timeout_is_unknown_not_a_false_failure_or_pass(self):
        with patch(
            "rtl_closure.universal._run_container",
            side_effect=subprocess.TimeoutExpired(["docker"], 2),
        ):
            report = run_universal_properties(
                RTL, "demo", PORTS, "clk", RESET, [property_row()], timeout=2
            )
        self.assertEqual(report["status"], "unknown")
        self.assertEqual(report["unknown_property_ids"], ["PROP_ACCEPT_INCREMENTS"])
        self.assertFalse(report["universal_proof"])

    def test_requirement_coverage_separates_mapping_from_proof_status(self):
        coverage = universal_requirement_coverage(
            [
                {
                    "id": "PROP_A",
                    "status": "pass",
                    "requirement_ids": ["REQ-A"],
                },
                {
                    "id": "PROP_B",
                    "status": "fail",
                    "requirement_ids": ["REQ-B"],
                },
                {
                    "id": "PROP_C",
                    "status": "unknown",
                    "requirement_ids": ["REQ-C"],
                },
            ],
            ["REQ-A", "REQ-B", "REQ-C", "REQ-D"],
        )
        self.assertEqual(
            coverage["covered_requirement_ids"], ["REQ-A", "REQ-B", "REQ-C"]
        )
        self.assertEqual(coverage["uncovered_requirement_ids"], ["REQ-D"])
        self.assertEqual(coverage["fully_proven_requirement_ids"], ["REQ-A"])
        self.assertEqual(coverage["failed_requirement_ids"], ["REQ-B"])
        self.assertEqual(coverage["inconclusive_requirement_ids"], ["REQ-C"])
        self.assertFalse(coverage["all_declared_requirements_mapped"])
        self.assertFalse(coverage["semantic_completeness_claimed"])

    def test_no_properties_marks_declared_requirements_uncovered(self):
        report = run_universal_properties(
            RTL,
            "demo",
            PORTS,
            "clk",
            RESET,
            [],
            requirement_ids=["REQ-COUNT-1"],
        )
        self.assertEqual(report["status"], "not_requested")
        self.assertEqual(
            report["requirement_coverage"]["uncovered_requirement_ids"],
            ["REQ-COUNT-1"],
        )

    def test_counterexample_is_conclusive_when_another_property_times_out(self):
        second = property_row()
        second["id"] = "PROP_SECOND"
        failed = subprocess.CompletedProcess(
            args=[],
            returncode=1,
            stdout="SAT temporal induction proof finished - model found: FAIL!",
            stderr="ERROR: Called with -verify and proof did fail!",
        )
        with patch(
            "rtl_closure.universal._run_container",
            side_effect=[failed, subprocess.TimeoutExpired(["docker"], 2)],
        ):
            report = run_universal_properties(
                RTL,
                "demo",
                PORTS,
                "clk",
                RESET,
                [property_row(), second],
                timeout=2,
            )
        self.assertEqual(report["status"], "fail")
        self.assertEqual(report["failed_property_ids"], ["PROP_ACCEPT_INCREMENTS"])
        self.assertEqual(report["unknown_property_ids"], ["PROP_SECOND"])
        self.assertIn("PROP_ACCEPT_INCREMENTS", report["detail"])
        self.assertFalse(report["universal_proof"])

    def test_property_set_uses_one_global_deadline(self):
        second = property_row()
        second["id"] = "PROP_SECOND"
        completed = subprocess.CompletedProcess(
            args=[],
            returncode=0,
            stdout="Temporal induction proof successful!",
            stderr="",
        )
        with patch(
            "rtl_closure.universal.time.monotonic",
            side_effect=[0.0, 0.1, 2.0],
        ), patch(
            "rtl_closure.universal._run_container", return_value=completed
        ) as run:
            report = run_universal_properties(
                RTL,
                "demo",
                PORTS,
                "clk",
                RESET,
                [property_row(), second],
                timeout=1,
            )
        self.assertEqual(run.call_count, 1)
        self.assertEqual(report["passed_property_ids"], ["PROP_ACCEPT_INCREMENTS"])
        self.assertEqual(report["unknown_property_ids"], ["PROP_SECOND"])
        self.assertTrue(report["global_deadline_exhausted"])
        self.assertEqual(report["timeout_scope"], "complete_property_set")
        self.assertFalse(report["properties"][1]["executed"])

    def test_deadline_skipped_property_has_attestable_execution_inputs(self):
        with tempfile.TemporaryDirectory() as temp, patch(
            "rtl_closure.universal.time.monotonic", side_effect=[0.0, 2.0]
        ), patch("rtl_closure.universal._run_container") as run:
            report = run_universal_properties(
                RTL,
                "demo",
                PORTS,
                "clk",
                RESET,
                [property_row()],
                Path(temp),
                timeout=1,
            )
            run.assert_not_called()
            row = report["properties"][0]
            self.assertFalse(row["executed"])
            self.assertEqual(row["status"], "unknown")
            for path in row["artifacts"].values():
                self.assertTrue(Path(path).is_file())

    def test_shared_deadline_keeps_release_script_deterministic(self):
        completed = subprocess.CompletedProcess(
            args=[],
            returncode=0,
            stdout="Temporal induction proof successful!",
            stderr="",
        )
        with tempfile.TemporaryDirectory() as temp, patch(
            "rtl_closure.universal.time.monotonic", side_effect=[0.0, 10.0]
        ), patch(
            "rtl_closure.universal._run_container", return_value=completed
        ):
            report = run_universal_properties(
                RTL,
                "demo",
                PORTS,
                "clk",
                RESET,
                [property_row()],
                Path(temp),
                timeout=30,
            )
            expected = render_universal_execution_inputs(
                top="demo",
                ports=PORTS,
                clock_port="clk",
                reset=RESET,
                property_value=property_row(),
                property_index=1,
                induction_depth=8,
                timeout=30,
            )
            script = Path(report["properties"][0]["artifacts"]["script"]).read_text(
                encoding="utf-8"
            )
        self.assertEqual(script, expected["script"])

    def test_checkpoint_reuses_only_conclusive_matching_properties(self):
        second = property_row()
        second["id"] = "PROP_SECOND"
        completed = subprocess.CompletedProcess(
            args=[],
            returncode=0,
            stdout="Temporal induction proof successful!",
            stderr="",
        )
        with tempfile.TemporaryDirectory() as temp:
            artifact_dir = Path(temp)
            with patch(
                "rtl_closure.universal._run_container", return_value=completed
            ) as first_run:
                first = run_universal_properties(
                    RTL,
                    "demo",
                    PORTS,
                    "clk",
                    RESET,
                    [property_row(), second],
                    artifact_dir,
                )
            self.assertEqual(first_run.call_count, 2)
            self.assertEqual(first["status"], "pass")
            self.assertTrue((artifact_dir / "universal-checkpoint.json").is_file())

            with patch(
                "rtl_closure.universal._run_container",
                side_effect=AssertionError("a conclusive property was rerun"),
            ) as resumed_run:
                resumed = run_universal_properties(
                    RTL,
                    "demo",
                    PORTS,
                    "clk",
                    RESET,
                    [property_row(), second],
                    artifact_dir,
                )
            resumed_run.assert_not_called()
        self.assertEqual(resumed["status"], "pass")
        self.assertTrue(
            all(row.get("resumed_from_checkpoint") for row in resumed["properties"])
        )

    def test_tampered_checkpoint_is_not_reused(self):
        completed = subprocess.CompletedProcess(
            args=[],
            returncode=0,
            stdout="Temporal induction proof successful!",
            stderr="",
        )
        with tempfile.TemporaryDirectory() as temp:
            artifact_dir = Path(temp)
            with patch(
                "rtl_closure.universal._run_container", return_value=completed
            ):
                run_universal_properties(
                    RTL,
                    "demo",
                    PORTS,
                    "clk",
                    RESET,
                    [property_row()],
                    artifact_dir,
                )
            checkpoint_path = artifact_dir / "universal-checkpoint.json"
            checkpoint = json.loads(checkpoint_path.read_text(encoding="utf-8"))
            checkpoint["completed"][0]["status"] = "fail"
            checkpoint_path.write_text(json.dumps(checkpoint), encoding="utf-8")

            with patch(
                "rtl_closure.universal._run_container", return_value=completed
            ) as rerun:
                report = run_universal_properties(
                    RTL,
                    "demo",
                    PORTS,
                    "clk",
                    RESET,
                    [property_row()],
                    artifact_dir,
                )
        self.assertEqual(rerun.call_count, 1)
        self.assertEqual(report["status"], "pass")
        self.assertFalse(report["properties"][0].get("resumed_from_checkpoint", False))

    def test_merge_counterexample_enters_repair_contract(self):
        simulation = {
            "passed": True,
            "total_checks": 2,
            "mismatched": 0,
            "passed_check_ids": ["sim:A", "sim:B"],
            "mismatches": [],
        }
        report = {
            "status": "fail",
            "properties": [
                {
                    "id": "PROP_ACCEPT_INCREMENTS",
                    "status": "fail",
                    "requirement_ids": ["REQ-COUNT-1"],
                    "counterexample": {"available": True, "signals": []},
                }
            ],
        }
        merged = merge_universal_result(simulation, report, required=True)
        self.assertFalse(merged["passed"])
        self.assertEqual(merged["failure_class"], "universal_counterexample")
        self.assertEqual(merged["mismatched"], 1)
        self.assertEqual(merged["total_checks"], 3)
        self.assertEqual(
            merged["mismatches"][0]["requirement_ids"], ["REQ-COUNT-1"]
        )

    def test_required_inconclusive_adds_a_gate_failure(self):
        simulation = {
            "passed": True,
            "total_checks": 1,
            "mismatched": 0,
            "passed_check_ids": ["sim:A"],
            "mismatches": [],
        }
        merged = merge_universal_result(
            simulation,
            {"status": "unknown", "detail": "solver timeout", "properties": []},
            required=True,
        )
        self.assertFalse(merged["passed"])
        self.assertEqual(merged["failure_class"], "universal_inconclusive")
        self.assertEqual(
            merged["mismatches"][0]["check_id"],
            "universal:PROVIDER_CONCLUSIVE",
        )


if __name__ == "__main__":
    unittest.main()
