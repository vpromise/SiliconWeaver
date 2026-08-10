from __future__ import annotations

import json
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

from rtl_closure.contract import content_hash
from rtl_closure.errors import ContainerInfrastructureError
from rtl_closure.formal import (
    FORMAL_BATCH_SIZE,
    FORMAL_MIN_BATCH_SIZE,
    ADAPTIVE_PROPERTY_RESERVE,
    _hydrate_property,
    _select_property_rows,
    merge_formal_result,
    merge_lint_result,
    render_formal_batch_script,
    run_bounded_trace_formal,
    run_verilator_lint,
)
from rtl_closure.repair import candidate_improves
from rtl_closure.spec import validate_spec
from rtl_closure.repair import (
    INDEPENDENT_PROMPT_EVIDENCE_MAX_BYTES,
    NonRepairableVerificationError,
    RepairOutcome,
    VerificationInfrastructureError,
    _evaluate,
    _independent_failure_fingerprint,
    _independent_prompt_evidence,
    close_independent_verification,
    repair_loop,
)
from rtl_closure.adaptive import suite_hash
from rtl_closure.verification import VERIFY_IMAGE, generate_expected_trace


EXAMPLE = Path(__file__).parents[1] / "examples" / "counter.json"
ORACLE = """class TopModule:
    def __init__(self):
        self.count = 0

    def eval(self, inputs):
        if not inputs.get("rst_n", 1):
            self.count = 0
        elif inputs.get("enable", 0):
            self.count = (self.count + 1) & 255
        return {"count": self.count}
"""
GOOD_RTL = """module rtl_closure_counter(
  input wire clk,
  input wire rst_n,
  input wire enable,
  output reg [7:0] count
);
always @(posedge clk) begin
  if (!rst_n) count <= 8'b0;
  else if (enable) count <= count + 1'b1;
end
endmodule"""
BAD_RTL = GOOD_RTL.replace("count + 1'b1", "count + 2")
MISSING_RESET_RTL = """module rtl_closure_counter(
  input wire clk,
  input wire rst_n,
  input wire enable,
  output reg [7:0] count = 8'b0
);
always @(posedge clk) begin
  if (enable) count <= count + 1'b1;
end
endmodule"""


def formal_success(count: int) -> subprocess.CompletedProcess:
    return subprocess.CompletedProcess(
        args=["yosys"],
        returncode=0,
        stdout="SAT proof finished - no model found: SUCCESS!\n" * count,
        stderr="",
    )


def write_valid_counterexample(path: Path) -> None:
    path.write_text(
        json.dumps({"signal": [{"name": "count", "data": ["00000000"]}]})
        + "\n",
        encoding="utf-8",
    )


class FormalYosysLiteralTests(unittest.TestCase):
    def test_script_uses_fixed_width_bitvectors_for_wide_values(self):
        raw = json.loads(EXAMPLE.read_text())
        raw["design"]["name"] = "wide_formal_fixture"
        raw["design"]["ports"] = [
            {"name": "clk", "direction": "input", "width": 1},
            {"name": "rst_n", "direction": "input", "width": 1},
            {"name": "data", "direction": "input", "width": 64},
            {"name": "result", "direction": "output", "width": 65},
        ]
        spec = validate_spec(raw)
        input_value = (1 << 63) + 5
        expected_value = (1 << 64) + (1 << 63) + 7
        script = render_formal_batch_script(
            spec,
            [
                {
                    "steps": [
                        {"inputs": {"rst_n": 1, "data": input_value}},
                        {"inputs": {"rst_n": 1, "data": -1}},
                    ],
                    "signal": "result",
                    "expected": expected_value,
                }
            ],
        )

        self.assertIn(
            "-set-at 1 data 64'b1000000000000000000000000000000000000000000000000000000000000101",
            script,
        )
        self.assertIn(f"-set-at 2 data 64'b{'1' * 64}", script)
        self.assertIn("-set-at 1 rst_n 1'b1", script)
        self.assertIn(
            "-prove result 65'b11000000000000000000000000000000000000000000000000000000000000111",
            script,
        )
        self.assertNotIn(str(input_value), script)
        self.assertNotIn(str(expected_value), script)


class FormalPropertyQuotaTests(unittest.TestCase):
    @staticmethod
    def _large_suite() -> tuple[object, dict, dict]:
        spec = validate_spec(json.loads(EXAMPLE.read_text()))
        scenarios = [
            {
                "id": "BASE_LONG",
                "description": "Long frozen base scenario.",
                "requirement_ids": ["REQ_BASE"],
                "reset_before": True,
                "cycles": [{"drive": {"enable": 1}, "repeat": 193}],
            }
        ]
        trace = [
            {
                "scenario_index": 0,
                "scenario_id": "BASE_LONG",
                "cycle": cycle,
                "inputs": {"rst_n": 1, "enable": 1},
                "expected": {"count": (cycle + 1) & 255},
            }
            for cycle in range(193)
        ]
        for cex_index in range(1, 71):
            scenario_index = len(scenarios)
            scenario_id = f"CEX_{cex_index:03d}"
            scenarios.append(
                {
                    "id": scenario_id,
                    "description": "Controller-derived counterexample mutation.",
                    "requirement_ids": ["REQ_CEX"],
                    "reset_before": True,
                    "source": "counterexample_mutation",
                    "cycles": [{"drive": {"enable": cex_index & 1}, "repeat": 1}],
                }
            )
            trace.append(
                {
                    "scenario_index": scenario_index,
                    "scenario_id": scenario_id,
                    "cycle": 0,
                    "inputs": {"rst_n": 1, "enable": cex_index & 1},
                    "expected": {"count": cex_index & 255},
                }
            )
        plan = {
            "schema_version": 1,
            "design": spec.name,
            "clock": {
                "port": spec.clock_port,
                "period_ns": spec.clock_period_ns,
                "sample_phase": "post_rising_edge",
            },
            "reset": spec.reset,
            "reset_cycles": 3,
            "scenarios": scenarios,
        }
        expected = {
            "schema_version": 1,
            "trace": trace,
            "setup_observations": [],
            "oracle_sha256": "test-oracle",
        }
        return spec, plan, expected

    def test_batch_size_schedules_every_unique_obligation(self):
        spec, plan, expected = self._large_suite()
        with patch(
            "rtl_closure.formal._hydrate_property", wraps=_hydrate_property
        ) as hydrate:
            properties, selection = _select_property_rows(
                spec, plan, expected, depth=256
            )

        self.assertEqual(len(properties), 262)
        self.assertEqual(hydrate.call_count, 263)
        self.assertEqual(selection["batch_size"], FORMAL_BATCH_SIZE)
        self.assertEqual(selection["batch_count"], 9)
        self.assertEqual(selection["raw_obligation_count"], 263)
        self.assertEqual(selection["deduplicated_obligation_count"], 1)
        self.assertEqual(selection["eligible"], 262)
        self.assertEqual(selection["selected"], 262)
        self.assertEqual(selection["excluded"], 0)
        self.assertFalse(selection["truncated"])
        self.assertEqual(
            selection["adaptive_reserved_quota"], ADAPTIVE_PROPERTY_RESERVE
        )
        self.assertEqual(selection["adaptive_eligible"], 70)
        self.assertEqual(selection["adaptive_selected"], 70)
        self.assertEqual(selection["adaptive_excluded"], 0)
        self.assertEqual(selection["base_selected"], 192)
        self.assertTrue(selection["scenario_fairness_complete"])
        self.assertTrue(selection["requirement_fairness_complete"])
        self.assertEqual(selection["unrepresented_scenario_ids"], [])
        self.assertEqual(selection["unrepresented_requirement_ids"], [])

        selected_adaptive_indices = {
            origin["scenario_index"]
            for row in properties
            for origin in row["provenance"]
            if origin["adaptive"]
        }
        # Every eligible scenario is retained; the old limit is only a batch size.
        self.assertEqual(selected_adaptive_indices, set(range(1, 71)))
        self.assertTrue(any(not row["adaptive"] for row in properties))

    def test_canonical_obligations_keep_all_aliases_and_provenance(self):
        spec = validate_spec(json.loads(EXAMPLE.read_text()))
        scenarios = [
            {
                "id": f"ALIAS_{index}",
                "description": "Equivalent trace alias.",
                "requirement_ids": [f"REQ_{index}"],
                "reset_before": True,
                "cycles": [{"drive": {"enable": 1}, "repeat": 1}],
            }
            for index in (1, 2)
        ]
        plan = {"scenarios": scenarios}
        expected = {
            "trace": [
                {
                    "scenario_index": index,
                    "scenario_id": scenario["id"],
                    "cycle": 0,
                    "inputs": {"rst_n": 1, "enable": 1},
                    "expected": {"count": 1},
                }
                for index, scenario in enumerate(scenarios)
            ],
            "setup_observations": [],
        }
        properties, selection = _select_property_rows(
            spec, plan, expected, depth=1
        )
        self.assertEqual(len(properties), 1)
        self.assertEqual(selection["raw_obligation_count"], 2)
        self.assertEqual(selection["deduplicated_obligation_count"], 1)
        self.assertEqual(
            properties[0]["aliases"],
            ["TRACE_000_001_count", "TRACE_001_001_count"],
        )
        self.assertEqual(len(properties[0]["provenance"]), 2)
        self.assertEqual(properties[0]["requirement_ids"], ["REQ_1", "REQ_2"])

    def test_formal_report_exposes_property_selection_audit_counts(self):
        spec, plan, expected = self._large_suite()
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            with patch(
                "rtl_closure.formal._run_container",
                side_effect=[formal_success(FORMAL_BATCH_SIZE)] * 8
                + [formal_success(6)],
            ) as provider:
                report = run_bounded_trace_formal(
                    spec,
                    GOOD_RTL,
                    plan,
                    expected,
                    root,
                    depth=256,
                    timeout=30,
                )

            self.assertEqual(report["status"], "pass")
            self.assertEqual(report["eligible_property_count"], 262)
            self.assertEqual(provider.call_count, 9)
            self.assertEqual(report["selected_property_count"], 262)
            self.assertEqual(report["excluded_property_count"], 0)
            self.assertFalse(report["properties_truncated"])
            self.assertEqual(report["adaptive_selected_property_count"], 70)
            self.assertEqual(
                report["property_selection"]["adaptive_selected"], 70
            )
            self.assertEqual(report["verification_method"], "bounded_trace_checking")
            self.assertEqual(
                report["proof_scope"],
                "supported_unique_frozen_input_trace_obligations",
            )
            self.assertFalse(report["universal_proof"])
            self.assertTrue(report["selection_complete"])
            self.assertTrue(report["execution_complete"])
            self.assertTrue(report["evidence_complete"])
            self.assertEqual(report["batch_count"], 9)
            self.assertEqual(report["completed_batch_count"], 9)
            self.assertEqual(
                report["completeness"],
                "complete_for_all_unique_bounded_trace_obligations",
            )
            persisted = json.loads((root / "formal-report.json").read_text())
            self.assertEqual(
                persisted["property_selection"], report["property_selection"]
            )
            property_map = json.loads((root / "formal-properties.json").read_text())
            alias_rows = [row for row in property_map if len(row["aliases"]) > 1]
            self.assertEqual(len(alias_rows), 1)
            self.assertEqual(len(alias_rows[0]["provenance"]), 2)

    def test_all_batches_share_one_global_deadline(self):
        spec, plan, expected = self._large_suite()
        with tempfile.TemporaryDirectory() as temp:
            with (
                patch(
                    "rtl_closure.formal.time.monotonic",
                    side_effect=[100.0, 100.0, 104.0] + [104.0] * 7,
                ),
                patch(
                    "rtl_closure.formal._run_container",
                    side_effect=[formal_success(FORMAL_BATCH_SIZE)] * 8
                    + [formal_success(6)],
                ) as provider,
            ):
                report = run_bounded_trace_formal(
                    spec,
                    GOOD_RTL,
                    plan,
                    expected,
                    Path(temp),
                    depth=256,
                    timeout=10,
                )
        self.assertEqual(report["status"], "pass")
        self.assertEqual(provider.call_count, 9)
        self.assertEqual(provider.call_args_list[0].args[1], 10.0)
        self.assertEqual(provider.call_args_list[1].args[1], 6.0)

    def test_resource_pressure_splits_only_the_affected_batch(self):
        spec, plan, expected = self._large_suite()
        calls: list[str] = []

        def provider(command, timeout, *, purpose):
            del command, timeout
            calls.append(purpose)
            if purpose == "formal-batch-1":
                return subprocess.CompletedProcess(
                    args=["yosys"], returncode=137, stdout="", stderr="OOM-kill"
                )
            count = 16 if purpose in {"formal-batch-2", "formal-batch-3"} else 32
            if purpose == "formal-batch-11":
                count = 6
            return formal_success(count)

        with tempfile.TemporaryDirectory() as temp, patch(
            "rtl_closure.formal._run_container", side_effect=provider
        ):
            report = run_bounded_trace_formal(
                spec,
                GOOD_RTL,
                plan,
                expected,
                Path(temp),
                depth=256,
                timeout=30,
            )

        self.assertEqual(report["status"], "pass")
        self.assertTrue(report["evidence_complete"])
        self.assertEqual(report["reschedule_count"], 1)
        self.assertEqual(report["execution_attempt_count"], 11)
        self.assertEqual(report["batch_count"], 10)
        self.assertEqual(report["minimum_batch_size"], FORMAL_MIN_BATCH_SIZE)
        self.assertEqual(calls[:3], ["formal-batch-1", "formal-batch-2", "formal-batch-3"])

    def test_matching_checkpoint_reuses_completed_obligations(self):
        spec, plan, expected = self._large_suite()
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)

            def interrupted(command, timeout, *, purpose):
                del command, timeout
                if purpose == "formal-batch-1":
                    return formal_success(FORMAL_BATCH_SIZE)
                raise OSError("provider unavailable")

            with patch(
                "rtl_closure.formal._run_container", side_effect=interrupted
            ):
                first = run_bounded_trace_formal(
                    spec, GOOD_RTL, plan, expected, root, depth=256, timeout=30
                )
            self.assertEqual(first["status"], "error")

            def resumed(command, timeout, *, purpose):
                del command, timeout
                return formal_success(
                    6 if purpose == "formal-batch-17" else FORMAL_BATCH_SIZE
                )

            with patch(
                "rtl_closure.formal._run_container", side_effect=resumed
            ) as provider:
                second = run_bounded_trace_formal(
                    spec, GOOD_RTL, plan, expected, root, depth=256, timeout=30
                )

            self.assertEqual(second["status"], "pass")
            self.assertTrue(second["batches"][0]["resumed"])
            self.assertEqual(provider.call_count, 8)
            evidence = json.loads(
                (root / "formal-execution-evidence.json").read_text()
            )
            self.assertEqual(evidence["schema_version"], 2)

    def test_tampered_checkpoint_is_not_reused(self):
        spec, plan, expected = self._large_suite()
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)

            def interrupted(command, timeout, *, purpose):
                del command, timeout
                if purpose == "formal-batch-1":
                    return formal_success(FORMAL_BATCH_SIZE)
                raise OSError("provider unavailable")

            with patch(
                "rtl_closure.formal._run_container", side_effect=interrupted
            ):
                run_bounded_trace_formal(
                    spec, GOOD_RTL, plan, expected, root, depth=256, timeout=30
                )
            checkpoint_path = root / "formal-checkpoint.json"
            checkpoint = json.loads(checkpoint_path.read_text())
            checkpoint["next_attempt_index"] = 999
            checkpoint_path.write_text(json.dumps(checkpoint), encoding="utf-8")

            def restarted(command, timeout, *, purpose):
                del command, timeout
                attempt = int(purpose.rsplit("-", 1)[1])
                return formal_success(6 if attempt == 9 else FORMAL_BATCH_SIZE)

            with patch(
                "rtl_closure.formal._run_container", side_effect=restarted
            ) as provider:
                report = run_bounded_trace_formal(
                    spec, GOOD_RTL, plan, expected, root, depth=256, timeout=30
                )

            self.assertEqual(report["status"], "pass")
            self.assertEqual(provider.call_count, 9)
            self.assertTrue(all(not row["resumed"] for row in report["batches"]))

    def test_counterexample_wins_over_later_batch_infrastructure_error(self):
        spec, plan, expected = self._large_suite()
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)

            def provider(command, timeout, *, purpose):
                del command, timeout
                if purpose == "formal-batch-1":
                    cex = root / "formal" / "batch-0001" / "cex-0001.json"
                    write_valid_counterexample(cex)
                    return subprocess.CompletedProcess(
                        args=["yosys"],
                        returncode=0,
                        stdout=(
                            "SAT proof finished - no model found: SUCCESS!\n"
                            * (FORMAL_BATCH_SIZE - 1)
                        ),
                        stderr="",
                    )
                raise OSError("second batch provider unavailable")

            with patch("rtl_closure.formal._run_container", side_effect=provider):
                report = run_bounded_trace_formal(
                    spec,
                    BAD_RTL,
                    plan,
                    expected,
                    root,
                    depth=256,
                    timeout=30,
                )

        self.assertEqual(report["status"], "fail")
        self.assertEqual(len(report["counterexamples"]), 1)
        self.assertTrue(report["infrastructure_errors_present"])
        self.assertEqual(report["infrastructure_error_count"], 8)
        self.assertEqual(report["infrastructure_statuses"], ["error"])
        self.assertFalse(report["execution_complete"])

        simulation = {
            "passed": True,
            "total_checks": 1,
            "mismatched": 0,
            "passed_check_ids": ["sim:PASS"],
            "mismatches": [],
            "actual_trace": [],
        }
        for required in (False, True):
            with self.subTest(required=required):
                merged = merge_formal_result(
                    simulation, report, {"requirements": []}, plan, required=required
                )
                self.assertFalse(merged["passed"])
                self.assertEqual(merged["failure_class"], "formal_counterexample")
                self.assertNotIn("non_repairable", merged)
                self.assertEqual(len(merged["mismatches"]), 1)

    def test_counterexample_wins_over_later_batch_timeout(self):
        spec, plan, expected = self._large_suite()
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)

            def provider(command, timeout, *, purpose):
                del command
                if purpose == "formal-batch-1":
                    cex = root / "formal" / "batch-0001" / "cex-0001.json"
                    write_valid_counterexample(cex)
                    return subprocess.CompletedProcess(
                        args=["yosys"],
                        returncode=0,
                        stdout=(
                            "SAT proof finished - no model found: SUCCESS!\n"
                            * (FORMAL_BATCH_SIZE - 1)
                        ),
                        stderr="",
                    )
                raise subprocess.TimeoutExpired(cmd="yosys", timeout=timeout)

            with patch("rtl_closure.formal._run_container", side_effect=provider):
                report = run_bounded_trace_formal(
                    spec,
                    BAD_RTL,
                    plan,
                    expected,
                    root,
                    depth=256,
                    timeout=30,
                )

        self.assertEqual(report["status"], "fail")
        self.assertEqual(len(report["counterexamples"]), 1)
        self.assertEqual(report["infrastructure_statuses"], ["unknown"])
        self.assertTrue(report["infrastructure_errors_present"])
        self.assertFalse(report["execution_complete"])

    def test_counterexample_survives_same_batch_execution_error(self):
        spec, plan, expected = self._large_suite()
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)

            def provider(command, timeout, *, purpose):
                del command, timeout
                if purpose == "formal-batch-1":
                    cex = root / "formal" / "batch-0001" / "cex-0001.json"
                    write_valid_counterexample(cex)
                    return subprocess.CompletedProcess(
                        args=["yosys"], returncode=1, stdout="counterexample", stderr="crash"
                    )
                attempt = int(purpose.rsplit("-", 1)[1])
                return formal_success(6 if attempt == 9 else FORMAL_BATCH_SIZE)

            with patch("rtl_closure.formal._run_container", side_effect=provider):
                report = run_bounded_trace_formal(
                    spec,
                    BAD_RTL,
                    plan,
                    expected,
                    root,
                    depth=256,
                    timeout=30,
                )

        self.assertEqual(report["status"], "fail")
        self.assertEqual(len(report["counterexamples"]), 1)
        self.assertEqual(report["infrastructure_statuses"], ["error"])
        self.assertEqual(report["infrastructure_error_count"], 1)
        self.assertFalse(report["execution_complete"])

    def test_unparseable_counterexample_cannot_override_execution_error(self):
        spec, plan, expected = self._large_suite()
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)

            def provider(command, timeout, *, purpose):
                del command, timeout
                if purpose == "formal-batch-1":
                    cex = root / "formal" / "batch-0001" / "cex-0001.json"
                    cex.write_text('{"signal": []}\n', encoding="utf-8")
                    return subprocess.CompletedProcess(
                        args=["yosys"], returncode=1, stdout="", stderr="crash"
                    )
                attempt = int(purpose.rsplit("-", 1)[1])
                return formal_success(6 if attempt == 9 else FORMAL_BATCH_SIZE)

            with patch("rtl_closure.formal._run_container", side_effect=provider):
                report = run_bounded_trace_formal(
                    spec,
                    BAD_RTL,
                    plan,
                    expected,
                    root,
                    depth=256,
                    timeout=30,
                )

        self.assertEqual(report["status"], "error")
        self.assertEqual(report["counterexamples"], [])
        self.assertEqual(report["infrastructure_statuses"], ["error"])

    def test_unsupported_frozen_scenarios_make_required_evidence_incomplete(self):
        spec = validate_spec(json.loads(EXAMPLE.read_text()))
        plan = {
            "scenarios": [
                {
                    "id": "DIRECTED",
                    "requirement_ids": ["REQ_DIRECTED"],
                    "reset_before": True,
                    "cycles": [{"drive": {"enable": 1}}],
                },
                {
                    "id": "RANDOM",
                    "requirement_ids": ["REQ_RANDOM"],
                    "reset_before": True,
                    "source": "system_random",
                    "cycles": [{"drive": {"enable": 0}}],
                },
                {
                    "id": "CONTINUOUS",
                    "requirement_ids": ["REQ_CONTINUOUS"],
                    "reset_before": False,
                    "cycles": [{"drive": {"enable": 1}}],
                },
            ]
        }
        expected = {
            "trace": [
                {
                    "scenario_index": index,
                    "scenario_id": scenario["id"],
                    "cycle": 0,
                    "inputs": {"rst_n": 1, "enable": index != 1},
                    "expected": {"count": index + 1},
                }
                for index, scenario in enumerate(plan["scenarios"])
            ],
            "setup_observations": [],
        }
        properties, selection = _select_property_rows(
            spec, plan, expected, depth=1
        )
        self.assertEqual(len(properties), 2)
        self.assertEqual(selection["excluded"], 1)
        self.assertEqual(selection["unsupported_scenario_count"], 1)
        self.assertEqual(
            [row["scenario_id"] for row in selection["unsupported_scenarios"]],
            ["CONTINUOUS"],
        )
        self.assertEqual(
            [row["reason_code"] for row in selection["unsupported_scenarios"]],
            ["continuous_state_not_isolatable"],
        )
        self.assertEqual(
            selection["unrepresented_scenario_ids"], ["CONTINUOUS"]
        )
        self.assertEqual(
            selection["unrepresented_requirement_ids"],
            ["REQ_CONTINUOUS"],
        )

        completed = formal_success(2)
        with tempfile.TemporaryDirectory() as temp, patch(
            "rtl_closure.formal._run_container", return_value=completed
        ):
            report = run_bounded_trace_formal(
                spec,
                GOOD_RTL,
                plan,
                expected,
                Path(temp),
                depth=1,
                timeout=30,
            )
        self.assertEqual(report["status"], "pass")
        self.assertFalse(report["selection_complete"])
        self.assertFalse(report["evidence_complete"])
        self.assertEqual(report["completeness"], "incomplete_selection")

        simulation = {
            "passed": True,
            "total_checks": 1,
            "mismatched": 0,
            "passed_check_ids": ["sim:PASS"],
            "mismatches": [],
            "actual_trace": [],
        }
        advisory = merge_formal_result(
            simulation, report, {"requirements": []}, plan, required=False
        )
        required = merge_formal_result(
            simulation, report, {"requirements": []}, plan, required=True
        )
        self.assertTrue(advisory["passed"])
        self.assertFalse(advisory["formal"]["evidence_complete"])
        self.assertFalse(required["passed"])
        self.assertEqual(required["failure_class"], "formal_incomplete")
        self.assertTrue(required["non_repairable"])
        self.assertEqual(
            required["verification_error"]["context"]["unsupported_scenarios"],
            selection["unsupported_scenarios"],
        )

    def test_frozen_system_random_scenarios_are_formalized(self):
        spec = validate_spec(json.loads(EXAMPLE.read_text()))
        plan = {
            "scenarios": [
                {
                    "id": "RANDOM_ONLY",
                    "requirement_ids": ["REQ_RANDOM"],
                    "reset_before": True,
                    "source": "system_random",
                    "cycles": [{"drive": {"enable": 1}}],
                }
            ]
        }
        expected = {
            "trace": [
                {
                    "scenario_index": 0,
                    "scenario_id": "RANDOM_ONLY",
                    "cycle": 0,
                    "inputs": {"rst_n": 1, "enable": 1},
                    "expected": {"count": 1},
                }
            ],
            "setup_observations": [],
        }
        completed = formal_success(1)
        with tempfile.TemporaryDirectory() as temp, patch(
            "rtl_closure.formal._run_container", return_value=completed
        ):
            report = run_bounded_trace_formal(
                spec,
                GOOD_RTL,
                plan,
                expected,
                Path(temp),
                depth=1,
                timeout=30,
            )

        self.assertEqual(report["status"], "pass")
        self.assertEqual(report["property_count"], 1)
        self.assertTrue(report["selection_complete"])
        self.assertTrue(report["evidence_complete"])
        self.assertEqual(
            report["completeness"],
            "complete_for_all_unique_bounded_trace_obligations",
        )

        simulation = {
            "passed": True,
            "total_checks": 1,
            "mismatched": 0,
            "passed_check_ids": ["sim:PASS"],
            "mismatches": [],
            "actual_trace": [],
        }
        advisory = merge_formal_result(
            simulation, report, {"requirements": []}, plan, required=False
        )
        required = merge_formal_result(
            simulation, report, {"requirements": []}, plan, required=True
        )
        self.assertTrue(advisory["passed"])
        self.assertTrue(advisory["formal"]["evidence_complete"])
        self.assertTrue(required["passed"])

    def test_resetless_trace_is_not_compared_from_an_assumed_initial_state(self):
        raw = json.loads(EXAMPLE.read_text())
        raw["design"].pop("reset")
        spec = validate_spec(raw)
        plan = {
            "scenarios": [
                {
                    "id": "RESETLESS",
                    "requirement_ids": ["REQ_STATE"],
                    "reset_before": True,
                    "cycles": [{"drive": {"enable": 1}}],
                }
            ]
        }
        expected = {
            "trace": [
                {
                    "scenario_index": 0,
                    "scenario_id": "RESETLESS",
                    "cycle": 0,
                    "inputs": {"enable": 1},
                    "expected": {"count": 1},
                }
            ],
            "setup_observations": [],
        }

        properties, selection = _select_property_rows(spec, plan, expected, depth=1)

        self.assertEqual(properties, [])
        self.assertEqual(selection["unsupported_scenario_count"], 1)
        self.assertEqual(
            selection["unsupported_scenarios"][0]["reason_code"],
            "resetless_initial_state_unconstrained",
        )


def docker_ready() -> bool:
    return shutil.which("docker") is not None and subprocess.run(
        ["docker", "image", "inspect", VERIFY_IMAGE],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        check=False,
    ).returncode == 0


class VerilatorQualityGateTests(unittest.TestCase):
    def test_high_risk_warning_fails_generated_rtl_quality_gate(self):
        spec = validate_spec(json.loads(EXAMPLE.read_text()))
        completed = subprocess.CompletedProcess(
            ["verilator"],
            0,
            stdout="",
            stderr="%Warning-WIDTH: dut.sv:12: assignment truncates value\n",
        )
        with tempfile.TemporaryDirectory() as temp, patch(
            "rtl_closure.formal._run_container", return_value=completed
        ):
            report = run_verilator_lint(spec, GOOD_RTL, Path(temp))
        self.assertEqual(report["status"], "fail")
        self.assertEqual(report["warning_counts"], {"WIDTH": 1})
        self.assertEqual(
            report["quality_gate"]["blocking_warning_codes"], ["WIDTH"]
        )

    def test_informational_warning_remains_visible_without_blocking(self):
        spec = validate_spec(json.loads(EXAMPLE.read_text()))
        completed = subprocess.CompletedProcess(
            ["verilator"],
            0,
            stdout="",
            stderr="%Warning-UNUSEDSIGNAL: dut.sv:4: signal is unused\n",
        )
        with tempfile.TemporaryDirectory() as temp, patch(
            "rtl_closure.formal._run_container", return_value=completed
        ):
            report = run_verilator_lint(spec, GOOD_RTL, Path(temp))
        self.assertEqual(report["status"], "pass")
        self.assertEqual(report["warning_counts"], {"UNUSEDSIGNAL": 1})
        self.assertEqual(report["quality_gate"]["status"], "pass")


@unittest.skipUnless(docker_ready(), "trusted verification image is unavailable")
class FormalIntegrationTests(unittest.TestCase):
    def _fixture(self, root: Path):
        raw = json.loads(EXAMPLE.read_text())
        raw["verification"] = {
            "requirements": [
                {
                    "id": "REQ_COUNT",
                    "text": "Increment by exactly one when enabled.",
                    "signals": ["enable", "count"],
                }
            ],
            "directed_tests": [
                {
                    "id": "TEST_COUNT",
                    "description": "Reset, hold, and increment twice.",
                    "requirement_ids": ["REQ_COUNT"],
                    "cycles": [
                        {"drive": {"enable": 0}},
                        {"drive": {"enable": 1}, "repeat": 2},
                    ],
                }
            ],
            "random_regression_cycles": 0,
            "formal_depth": 8,
        }
        spec = validate_spec(raw)
        plan = {
            "schema_version": 1,
            "design": spec.name,
            "clock": {
                "port": spec.clock_port,
                "period_ns": spec.clock_period_ns,
                "sample_phase": "post_rising_edge",
            },
            "reset": spec.reset,
            "reset_cycles": 3,
            "scenarios": spec.verification["directed_tests"],
        }
        expected = generate_expected_trace(spec, ORACLE, plan, root / "oracle")
        return spec, plan, expected

    def test_verilator_lint_and_bounded_trace_distinguish_good_and_bad_rtl(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            spec, plan, expected = self._fixture(root)
            lint = run_verilator_lint(spec, GOOD_RTL, root / "lint")
            good = run_bounded_trace_formal(
                spec, GOOD_RTL, plan, expected, root / "good", depth=8, timeout=120
            )
            bad = run_bounded_trace_formal(
                spec, BAD_RTL, plan, expected, root / "bad", depth=8, timeout=120
            )
            self.assertEqual(lint["status"], "pass", lint)
            self.assertEqual(good["status"], "pass", good)
            self.assertEqual(bad["status"], "fail", bad)
            self.assertEqual(lint["rtl_sha256"], content_hash(GOOD_RTL))
            self.assertEqual(good["rtl_sha256"], content_hash(GOOD_RTL))
            self.assertEqual(bad["rtl_sha256"], content_hash(BAD_RTL))
            self.assertTrue(bad["counterexamples"])
            self.assertEqual(
                bad["counterexamples"][0]["requirement_ids"], ["REQ_COUNT"]
            )
            self.assertEqual(bad["counterexamples"][0]["actual"], 2)
            self.assertGreaterEqual(
                bad["counterexamples"][0]["property_index"], 1
            )

    def test_formal_ignores_declaration_init_and_rejects_missing_reset_logic(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            spec, plan, expected = self._fixture(root)
            report = run_bounded_trace_formal(
                spec,
                MISSING_RESET_RTL,
                plan,
                expected,
                root / "missing-reset",
                depth=8,
                timeout=120,
            )
            self.assertEqual(report["status"], "fail", report)
            self.assertTrue(report["counterexamples"])
            self.assertEqual(
                report["counterexamples"][0]["phase"], "reset_asserted"
            )


class FormalNormalizationTests(unittest.TestCase):
    @staticmethod
    def _simulation_result(*, passed: bool = True) -> dict:
        mismatch = {
            "check_id": "0:TEST:post_rising_edge:0:count",
            "ordinal": 0,
            "scenario_index": 0,
            "scenario_id": "TEST",
            "requirement_ids": ["REQ_GENERATED"],
            "cycle": 0,
            "phase": "post_rising_edge",
            "signal": "count",
            "expected": 1,
            "actual": 0,
            "inputs": {"rst_n": 1, "enable": 1},
            "failure_class": "value_mismatch",
        }
        return {
            "passed": passed,
            "failure_class": None if passed else "value_mismatch",
            "total_checks": 1,
            "mismatched": 0 if passed else 1,
            "first_failure_ordinal": None if passed else 0,
            "mismatches": [] if passed else [mismatch],
            "passed_check_ids": [mismatch["check_id"]] if passed else [],
            "actual_trace": [
                {
                    "ordinal": 0,
                    "scenario_index": 0,
                    "scenario_id": "TEST",
                    "cycle": 0,
                    "phase": "post_rising_edge",
                    "inputs": mismatch["inputs"],
                    "expected": {"count": 1},
                    "actual": {"count": 1 if passed else 0},
                }
            ],
            "suite_sha256": "suite",
        }

    @staticmethod
    def _contract_and_plan() -> tuple[dict, dict]:
        return (
            {
                "requirements": [
                    {
                        "id": "REQ_GENERATED",
                        "text": "Generated requirement.",
                        "kind": "functional",
                        "signals": ["enable", "count"],
                    }
                ],
                "ports": [
                    {"name": "enable", "direction": "input", "width": 1},
                    {"name": "count", "direction": "output", "width": 8},
                ],
            },
            {
                "scenarios": [
                    {
                        "id": "TEST",
                        "requirement_ids": ["REQ_GENERATED"],
                        "reset_before": True,
                        "cycles": [{"drive": {"enable": 1}, "repeat": 1}],
                    }
                ]
            },
        )

    def test_lint_is_aggregated_without_overwriting_formal_counterexamples(self):
        simulation = {
            "passed": True,
            "total_checks": 2,
            "mismatched": 0,
            "passed_check_ids": ["sim:A", "sim:B"],
            "actual_trace": [],
            "suite_sha256": "suite",
        }
        formal = {
            "status": "fail",
            "provider": "yosys_bounded_trace",
            "property_count": 3,
            "passed_property_ids": ["P2"],
            "counterexamples": [
                {"property_index": 1, "property_id": "P1", "signal": "count"},
                {"property_index": 3, "property_id": "P3", "signal": "count"},
            ],
        }
        contract, plan = self._contract_and_plan()
        formal_result = merge_formal_result(
            simulation, formal, contract, plan, required=True
        )
        combined = merge_lint_result(
            formal_result,
            {
                "status": "fail",
                "provider": "verilator_lint",
                "detail": "lint failed",
            },
            contract,
            plan,
            required=True,
        )
        self.assertEqual(combined["formal"], formal)
        self.assertEqual(
            [row["check_id"] for row in combined["mismatches"]],
            ["formal:P1", "formal:P3", "lint:VERILATOR_LINT"],
        )
        self.assertEqual(combined["mismatched"], 3)
        self.assertEqual(
            combined["total_checks"] - len(combined["passed_check_ids"]),
            combined["mismatched"],
        )

    def test_required_mode_marks_incomplete_evidence_nonrepairable(self):
        simulation = self._simulation_result()
        formal = {
            "status": "pass",
            "provider": "yosys_bounded_trace",
            "property_count": 2,
            "passed_property_ids": ["P1", "P2"],
            "properties_truncated": True,
            "selection_complete": False,
            "property_selection": {
                "eligible": 3,
                "selected": 2,
                "excluded": 1,
                "unrepresented_scenario_ids": [],
                "unrepresented_requirement_ids": [],
            },
        }
        contract, plan = self._contract_and_plan()
        advisory = merge_formal_result(
            simulation, formal, contract, plan, required=False
        )
        required = merge_formal_result(
            simulation, formal, contract, plan, required=True
        )
        self.assertTrue(advisory["passed"])
        self.assertFalse(required["passed"])
        self.assertEqual(required["failure_class"], "formal_incomplete")
        self.assertTrue(required["non_repairable"])
        self.assertEqual(
            required["verification_error"]["code"],
            "formal_incomplete_evidence",
        )
        self.assertEqual(required["verification_error"]["category"], "evidence")
        self.assertEqual(required["mismatches"], [])
        self.assertEqual(
            required["total_checks"] - len(required["passed_check_ids"]),
            required["mismatched"],
        )

    def test_evaluate_recomputes_generated_requirement_coverage(self):
        raw = json.loads(EXAMPLE.read_text())
        raw["verification"] = {"formal_mode": "off", "verilator_lint": False}
        spec = validate_spec(raw)
        contract, plan = self._contract_and_plan()
        simulation = self._simulation_result(passed=False)
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            with (
                patch("rtl_closure.repair.verify_frozen_assets"),
                patch("rtl_closure.repair.normalize_top_module", return_value=GOOD_RTL),
                patch("rtl_closure.repair.check_rtl_policy"),
                patch("rtl_closure.repair.check_exact_interface"),
                patch("rtl_closure.repair.yosys_check", return_value="ok"),
                patch("rtl_closure.repair.extract_rtl_blocks", return_value=[]),
                patch("rtl_closure.repair.verify_rtl", return_value=simulation),
            ):
                _, result = _evaluate(
                    spec,
                    GOOD_RTL,
                    contract,
                    plan,
                    {"trace": []},
                    root / "attempt",
                    contract_path=root / "contract.json",
                    plan_path=root / "testplan.json",
                    oracle_path=root / "oracle.py",
                    expected_path=root / "expected.json",
                    manifest_path=root / "manifest.json",
                    frozen_manifest={},
                )
        requirement = result["coverage"]["requirements"]["REQ_GENERATED"]
        self.assertEqual(requirement["status"], "failing")
        self.assertEqual(requirement["mismatches"], 1)

    def test_evaluate_turns_architecture_failure_into_repair_feedback(self):
        raw = json.loads(EXAMPLE.read_text())
        raw["design"]["architecture"] = {
            "required_modules": ["counter_control"],
            "forbidden_operators": ["multiply"],
        }
        raw["verification"] = {"formal_mode": "off", "verilator_lint": False}
        spec = validate_spec(raw)
        contract, plan = self._contract_and_plan()
        audit = {
            "status": "fail",
            "checks": [
                {
                    "id": "required_module:counter_control",
                    "status": "fail",
                    "expected": {"reachable": True},
                    "actual": {"reachable": False},
                },
                {
                    "id": "forbidden_operator:multiply",
                    "status": "pass",
                    "expected": 0,
                    "actual": 0,
                },
            ],
        }
        events = []
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            attempt = root / "attempt"
            with (
                patch("rtl_closure.repair.verify_frozen_assets"),
                patch("rtl_closure.repair.normalize_top_module", return_value=GOOD_RTL),
                patch("rtl_closure.repair.check_rtl_policy"),
                patch("rtl_closure.repair.check_exact_interface"),
                patch("rtl_closure.repair.yosys_check", return_value="ok"),
                patch(
                    "rtl_closure.repair.run_architecture_audit",
                    return_value=audit,
                ),
                patch("rtl_closure.repair.verify_rtl") as simulation,
            ):
                _, result = _evaluate(
                    spec,
                    GOOD_RTL,
                    contract,
                    plan,
                    {"trace": []},
                    attempt,
                    contract_path=root / "contract.json",
                    plan_path=root / "testplan.json",
                    oracle_path=root / "oracle.py",
                    expected_path=root / "expected.json",
                    manifest_path=root / "manifest.json",
                    frozen_manifest={},
                    progress=lambda phase, values: events.append((phase, values)),
                )
            simulation.assert_not_called()
            self.assertTrue((attempt / "architecture-audit.json").is_file())
        self.assertFalse(result["passed"])
        self.assertEqual(result["failure_class"], "architecture_constraint")
        self.assertEqual(result["mismatched"], 1)
        self.assertIn(
            "architecture:forbidden_operator:multiply",
            result["passed_check_ids"],
        )
        self.assertEqual(
            [phase for phase, _ in events],
            ["architecture_audit_running", "architecture_audit_completed"],
        )

    def test_provider_events_respect_disabled_providers(self):
        contract, plan = self._contract_and_plan()
        simulation = self._simulation_result()
        for lint_enabled, expected_event_count in ((False, 0), (True, 2)):
            with self.subTest(lint_enabled=lint_enabled):
                raw = json.loads(EXAMPLE.read_text())
                raw["verification"] = {
                    "formal_mode": "off",
                    "verilator_lint": lint_enabled,
                }
                spec = validate_spec(raw)
                events = []
                with tempfile.TemporaryDirectory() as temp:
                    root = Path(temp)
                    with (
                        patch("rtl_closure.repair.verify_frozen_assets"),
                        patch(
                            "rtl_closure.repair.normalize_top_module",
                            return_value=GOOD_RTL,
                        ),
                        patch("rtl_closure.repair.check_rtl_policy"),
                        patch("rtl_closure.repair.check_exact_interface"),
                        patch("rtl_closure.repair.yosys_check", return_value="ok"),
                        patch("rtl_closure.repair.extract_rtl_blocks", return_value=[]),
                        patch("rtl_closure.repair.verify_rtl", return_value=simulation),
                        patch(
                            "rtl_closure.repair.run_verilator_lint",
                            return_value={
                                "status": "pass",
                                "provider": "verilator_lint",
                            },
                        ),
                        patch(
                            "rtl_closure.repair.run_bounded_trace_formal"
                        ) as formal_provider,
                    ):
                        _evaluate(
                            spec,
                            GOOD_RTL,
                            contract,
                            plan,
                            {"trace": []},
                            root / "attempt",
                            contract_path=root / "contract.json",
                            plan_path=root / "testplan.json",
                            oracle_path=root / "oracle.py",
                            expected_path=root / "expected.json",
                            manifest_path=root / "manifest.json",
                            frozen_manifest={},
                            progress=lambda phase, values: events.append(
                                (phase, values)
                            ),
                        )
                    formal_provider.assert_not_called()
                self.assertEqual(len(events), expected_event_count)
                for _, payload in events:
                    self.assertEqual(payload["formal_status"], "not_run")

    def test_required_provider_inconclusive_retries_then_aborts_without_repair(self):
        raw = json.loads(EXAMPLE.read_text())
        raw["verification"] = {
            "formal_mode": "required",
            "verilator_lint": False,
        }
        spec = validate_spec(raw)
        inconclusive = {
            "status": "unknown",
            "provider": "yosys_bounded_trace",
            "property_count": 1,
            "passed_property_ids": [],
            "detail": "solver timeout",
        }
        simulation = {
            "passed": True,
            "total_checks": 1,
            "mismatched": 0,
            "passed_check_ids": ["sim:A"],
            "actual_trace": [],
            "suite_sha256": "suite",
        }
        events = []
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            (root / "attempt" / "formal-evidence").mkdir(parents=True)
            with (
                patch("rtl_closure.repair.verify_frozen_assets"),
                patch("rtl_closure.repair.normalize_top_module", return_value=GOOD_RTL),
                patch("rtl_closure.repair.check_rtl_policy"),
                patch("rtl_closure.repair.check_exact_interface"),
                patch("rtl_closure.repair.yosys_check", return_value="ok"),
                patch("rtl_closure.repair.extract_rtl_blocks", return_value=[]),
                patch("rtl_closure.repair.verify_rtl", return_value=simulation),
                patch(
                    "rtl_closure.repair.run_bounded_trace_formal",
                    side_effect=[dict(inconclusive), dict(inconclusive)],
                ) as provider,
            ):
                with self.assertRaisesRegex(
                    VerificationInfrastructureError, "inconclusive after retry"
                ):
                    _evaluate(
                        spec,
                        GOOD_RTL,
                        {"requirements": [], "ports": []},
                        {"scenarios": []},
                        {"trace": []},
                        root / "attempt",
                        contract_path=root / "contract.json",
                        plan_path=root / "testplan.json",
                        oracle_path=root / "oracle.py",
                        expected_path=root / "expected.json",
                        manifest_path=root / "manifest.json",
                        frozen_manifest={},
                        progress=lambda phase, values: events.append((phase, values)),
                    )
            self.assertEqual(provider.call_count, 2)
            self.assertTrue((root / "attempt" / "verification-result.json").is_file())
        self.assertIn("formal_retrying", [phase for phase, _ in events])
        self.assertEqual(events[-1][0], "formal_completed")
        self.assertEqual(events[-1][1]["result"], "inconclusive")

    def test_nonrepairable_formal_evidence_never_calls_model_backend(self):
        raw = json.loads(EXAMPLE.read_text())
        raw["verification"] = {
            "formal_mode": "required",
            "verilator_lint": False,
            "max_repair_rounds": 2,
        }
        spec = validate_spec(raw)
        error = {
            "code": "formal_incomplete_evidence",
            "category": "evidence",
            "repairable": False,
            "provider": "yosys_bounded_trace",
            "message": "one or more unique obligations were not executed",
            "context": {"batch_count": 2, "completed_batch_count": 1},
        }
        result = {
            "passed": False,
            "failure_class": "formal_incomplete",
            "detail": error["message"],
            "total_checks": 1,
            "mismatched": 0,
            "first_failure_ordinal": None,
            "mismatches": [],
            "passed_check_ids": ["sim:A"],
            "actual_trace": [],
            "suite_sha256": "suite",
            "formal": {"status": "pass", "error": error},
            "verification_error": error,
            "non_repairable": True,
        }
        backend = Mock()
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            oracle_path = root / "oracle.py"
            oracle_path.write_text("class TopModule: pass\n", encoding="utf-8")
            with (
                patch("rtl_closure.repair.verify_frozen_assets"),
                patch(
                    "rtl_closure.repair._evaluate",
                    return_value=(BAD_RTL, result),
                ),
                patch(
                    "rtl_closure.repair.derive_counterexample_scenarios"
                ) as derive,
            ):
                with self.assertRaises(VerificationInfrastructureError) as raised:
                    repair_loop(
                        spec,
                        backend,
                        BAD_RTL,
                        {"requirements": [], "ports": []},
                        {"scenarios": []},
                        {"trace": []},
                        root,
                        contract_path=root / "contract.json",
                        plan_path=root / "testplan.json",
                        oracle_path=oracle_path,
                        expected_path=root / "expected.json",
                        manifest_path=root / "manifest.json",
                        frozen_manifest={"oracle_sha256": "oracle"},
                        origin_bundle={},
                    )
            backend.generate.assert_not_called()
            derive.assert_not_called()
            self.assertFalse(
                (root / "attempts" / "000" / "failure-packet.json").exists()
            )
        self.assertEqual(raised.exception.error["code"], "formal_incomplete_evidence")
        self.assertFalse(raised.exception.error["repairable"])

    def test_enabled_lint_is_required_even_when_formal_is_advisory(self):
        raw = json.loads(EXAMPLE.read_text())
        raw["verification"] = {
            "formal_mode": "advisory",
            "verilator_lint": True,
        }
        spec = validate_spec(raw)
        contract, plan = self._contract_and_plan()
        simulation = self._simulation_result()
        lint_unknown = {
            "status": "unknown",
            "provider": "verilator_lint",
            "detail": "lint container timeout",
        }
        formal_pass = {
            "status": "pass",
            "provider": "yosys_bounded_trace",
            "property_count": 1,
            "passed_property_ids": ["P1"],
            "detail": "formal completed",
        }
        events = []
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            (root / "attempt" / "formal-evidence").mkdir(parents=True)
            with (
                patch("rtl_closure.repair.verify_frozen_assets"),
                patch("rtl_closure.repair.normalize_top_module", return_value=GOOD_RTL),
                patch("rtl_closure.repair.check_rtl_policy"),
                patch("rtl_closure.repair.check_exact_interface"),
                patch("rtl_closure.repair.yosys_check", return_value="ok"),
                patch("rtl_closure.repair.extract_rtl_blocks", return_value=[]),
                patch("rtl_closure.repair.verify_rtl", return_value=simulation),
                patch(
                    "rtl_closure.repair.run_verilator_lint",
                    side_effect=[dict(lint_unknown), dict(lint_unknown)],
                ),
                patch(
                    "rtl_closure.repair.run_bounded_trace_formal",
                    return_value=formal_pass,
                ),
            ):
                with self.assertRaisesRegex(
                    VerificationInfrastructureError,
                    r"provider=verilator_lint, status=unknown",
                ):
                    _evaluate(
                        spec,
                        GOOD_RTL,
                        contract,
                        plan,
                        {"trace": []},
                        root / "attempt",
                        contract_path=root / "contract.json",
                        plan_path=root / "testplan.json",
                        oracle_path=root / "oracle.py",
                        expected_path=root / "expected.json",
                        manifest_path=root / "manifest.json",
                        frozen_manifest={},
                        progress=lambda phase, values: events.append((phase, values)),
                    )
        self.assertEqual(events[-1][0], "formal_completed")
        self.assertEqual(events[-1][1]["result"], "inconclusive")
        self.assertEqual(events[-1][1]["formal_status"], "pass")
        self.assertEqual(events[-1][1]["verilator_status"], "unknown")
        self.assertEqual(events[-1][1]["formal_detail"], "lint container timeout")

    def test_adaptive_rebaseline_infrastructure_error_is_not_relabelled(self):
        raw = json.loads(EXAMPLE.read_text())
        raw["verification"] = {
            "formal_mode": "required",
            "verilator_lint": False,
            "max_repair_rounds": 1,
            "repair_candidates": 1,
        }
        spec = validate_spec(raw)
        contract, partial_plan = self._contract_and_plan()
        plan = {
            "schema_version": 1,
            "design": spec.name,
            "clock": {
                "port": spec.clock_port,
                "period_ns": spec.clock_period_ns,
                "sample_phase": "post_rising_edge",
            },
            "reset": spec.reset,
            "reset_cycles": 3,
            "scenarios": partial_plan["scenarios"],
        }
        expected = {"trace": [], "setup_observations": [], "oracle_sha256": "oracle"}
        initial = self._simulation_result(passed=False)
        initial["suite_sha256"] = suite_hash(plan, expected)
        addition = {
            "id": "ADAPT_01",
            "description": "Adaptive replay.",
            "requirement_ids": ["REQ_GENERATED"],
            "reset_before": True,
            "cycles": [{"drive": {"enable": 0}, "repeat": 1}],
        }
        candidate_expected = {
            "trace": [],
            "setup_observations": [],
            "oracle_sha256": "oracle",
        }
        events = []

        def chain_head(epochs, base_plan, base_expected, **kwargs):
            del kwargs
            return (
                epochs[-1]["suite_sha256"]
                if epochs
                else suite_hash(base_plan, base_expected)
            )

        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            (root / "attempts" / "000").mkdir(parents=True)
            oracle_path = root / "oracle.py"
            oracle_path.write_text("class TopModule: pass\n", encoding="utf-8")
            with (
                patch("rtl_closure.repair.verify_frozen_assets"),
                patch(
                    "rtl_closure.repair._evaluate",
                    side_effect=[
                        (BAD_RTL, initial),
                        VerificationInfrastructureError("formal timeout"),
                    ],
                ),
                patch(
                    "rtl_closure.repair.derive_counterexample_scenarios",
                    return_value=[addition],
                ),
                patch(
                    "rtl_closure.repair.revalidate_oracle_bundle_quorum",
                    return_value=(
                        candidate_expected,
                        {"status": "pass", "execution_quorum": 2},
                    ),
                ),
                patch(
                    "rtl_closure.repair.verify_epoch_chain",
                    side_effect=chain_head,
                ),
            ):
                with self.assertRaisesRegex(
                    VerificationInfrastructureError, "formal timeout"
                ):
                    repair_loop(
                        spec,
                        object(),
                        BAD_RTL,
                        contract,
                        plan,
                        expected,
                        root,
                        contract_path=root / "contract.json",
                        plan_path=root / "testplan.json",
                        oracle_path=oracle_path,
                        expected_path=root / "expected.json",
                        manifest_path=root / "manifest.json",
                        frozen_manifest={"oracle_sha256": "oracle"},
                        origin_bundle={},
                        progress=lambda phase, values: events.append((phase, values)),
                    )
        phases = [phase for phase, _ in events]
        self.assertIn("repair_refining_tests", phases)
        self.assertNotIn("repair_suite_refinement_error", phases)

    def test_required_unknown_fails_closed_but_advisory_unknown_is_recorded(self):
        simulation = {
            "passed": True,
            "total_checks": 1,
            "mismatched": 0,
            "passed_check_ids": ["sim:A"],
            "actual_trace": [],
            "suite_sha256": "same-frozen-suite",
        }
        formal = {
            "status": "unknown",
            "property_count": 1,
            "passed_property_ids": [],
            "detail": "timeout",
        }
        contract = {"requirements": [], "ports": []}
        plan = {"scenarios": []}
        advisory = merge_formal_result(
            simulation, formal, contract, plan, required=False
        )
        required = merge_formal_result(
            simulation, formal, contract, plan, required=True
        )
        self.assertTrue(advisory["passed"])
        self.assertFalse(required["passed"])
        self.assertEqual(required["failure_class"], "formal_unknown")

    def test_all_counterexamples_are_ranked_for_incremental_repair(self):
        simulation = {
            "passed": True,
            "total_checks": 10,
            "mismatched": 0,
            "passed_check_ids": ["sim:A"],
            "actual_trace": [],
            "suite_sha256": "same-frozen-suite",
        }
        common = {
            "status": "fail",
            "property_count": 3,
        }
        champion = merge_formal_result(
            simulation,
            {
                **common,
                "passed_property_ids": ["P2"],
                "counterexamples": [
                    {"property_index": 1, "property_id": "P1", "signal": "count"},
                    {"property_index": 3, "property_id": "P3", "signal": "count"},
                ],
            },
            {"requirements": [], "ports": []},
            {"scenarios": []},
            required=True,
        )
        candidate = merge_formal_result(
            simulation,
            {
                **common,
                "passed_property_ids": ["P1", "P2"],
                "counterexamples": [
                    {"property_index": 3, "property_id": "P3", "signal": "count"}
                ],
            },
            {"requirements": [], "ports": []},
            {"scenarios": []},
            required=True,
        )
        self.assertEqual(champion["mismatched"], 2)
        self.assertEqual(len(champion["mismatches"]), 2)
        self.assertEqual(champion["first_failure_ordinal"], 10)
        self.assertEqual(candidate["first_failure_ordinal"], 12)
        self.assertTrue(candidate_improves(champion, candidate)[0])


class IndependentClosureTests(unittest.TestCase):
    @staticmethod
    def _minimal_independent_spec() -> SimpleNamespace:
        return SimpleNamespace(
            name="demo",
            architecture={},
            ports=(
                SimpleNamespace(name="clk", direction="input", width=1),
                SimpleNamespace(name="y", direction="output", width=1),
            ),
            clock_port="clk",
            reset=None,
            generation={
                "seed": 11,
                "temperature": 0.1,
                "independent_verify_tests": 2,
            },
            verification={"max_repair_rounds": 2, "repair_candidates": 2},
        )

    @staticmethod
    def _close_minimal_independent(
        root: Path, spec: SimpleNamespace, verifier: Mock, backend: Mock
    ):
        plan = {"schema_version": 1, "scenarios": []}
        expected = {"trace": [], "setup_observations": []}
        rtl = "module demo(input logic clk, output logic y); assign y = 0; endmodule"
        outcome = RepairOutcome(
            rtl,
            {"passed": True, "suite_sha256": suite_hash(plan, expected)},
            [],
            [],
            False,
            False,
        )
        return close_independent_verification(
            spec,
            backend,
            outcome,
            {"requirements": [], "ports": []},
            plan,
            expected,
            plan,
            expected,
            root,
            interface="module demo(input logic clk, output logic y); endmodule\n",
            python_model="class TopModule: pass\n",
            verifier=verifier,
            contract_path=root / "contract.json",
            plan_path=root / "testplan.json",
            oracle_path=root / "reference_model.py",
            expected_path=root / "expected-trace.json",
            manifest_path=root / "frozen-manifest.json",
            frozen_manifest={},
        )

    def test_prompt_evidence_is_deduplicated_minimal_and_byte_bounded(self):
        verification = {
            "match_rate": 0.5,
            "num_tests": 3,
            "total_checks": 3,
            "mismatched": 2,
            "mismatches": [
                {
                    "test": 1,
                    "signal": "y",
                    "width": 1,
                    "verilog": 0,
                    "python": 1,
                    "inputs": {"a": 1},
                },
                {
                    "test": 2,
                    "signal": "y",
                    "width": 1,
                    "verilog": 0,
                    "python": 1,
                    "inputs": {"a": 0},
                },
            ],
            "sv_error": None,
            "py_error": None,
            "meta_error": None,
            "stimuli": [{"a": 0}, {"a": 1}, {"a": 0}],
            "verilog_trace": [{"y": 0}, {"y": 0}, {"y": 0}],
            "python_trace": [{"y": 0}, {"y": 1}, {"y": 1}],
        }
        fingerprint = _independent_failure_fingerprint(
            seed=101, verification=verification
        )
        evidence = {
            "source": "independent_cross_verification",
            "seed": 101,
            "fingerprint": fingerprint,
            "verification": verification,
        }

        first = _independent_prompt_evidence([evidence, evidence])
        second = _independent_prompt_evidence([evidence, evidence])

        self.assertEqual(first, second)
        self.assertEqual(first["unique_counterexample_count"], 1)
        self.assertEqual(first["duplicate_counterexample_count"], 1)
        self.assertEqual(first["included_counterexample_count"], 1)
        replay = first["replay_slices"][0]
        self.assertEqual(replay["first_failure_test"], 1)
        self.assertEqual(replay["stimulus"], {"a": 1})
        self.assertEqual(replay["verilog_trace"], {"y": 0})
        self.assertEqual(replay["python_trace"], {"y": 1})
        self.assertEqual(len(replay["mismatches"]), 1)
        self.assertLessEqual(
            len(json.dumps(first, separators=(",", ":")).encode()),
            INDEPENDENT_PROMPT_EVIDENCE_MAX_BYTES,
        )

    def test_python_oracle_execution_error_does_not_trigger_rtl_repair(self):
        spec = SimpleNamespace(
            name="demo",
            architecture={},
            ports=(
                SimpleNamespace(name="clk", direction="input", width=1),
                SimpleNamespace(name="y", direction="output", width=1),
            ),
            clock_port="clk",
            reset=None,
            generation={
                "seed": 11,
                "temperature": 0.1,
                "independent_verify_tests": 2,
            },
            verification={"max_repair_rounds": 2, "repair_candidates": 2},
        )
        plan = {"schema_version": 1, "scenarios": []}
        expected = {"trace": [], "setup_observations": []}
        rtl = "module demo(input logic clk, output logic y); assign y = 0; endmodule"
        outcome = RepairOutcome(
            rtl,
            {"passed": True, "suite_sha256": suite_hash(plan, expected)},
            [],
            [],
            False,
            False,
        )
        verifier = Mock(
            return_value={
                "match_rate": 0.0,
                "num_tests": 2,
                "mismatched": 0,
                "mismatches": [],
                "sv_error": None,
                "py_error": "py_exec_fail: frozen oracle crashed",
                "meta_error": None,
                "stimuli": [{}, {}],
            }
        )
        backend = Mock()

        with tempfile.TemporaryDirectory() as temp, patch(
            "rtl_closure.repair.sandbox_image_ref", return_value="sha256:test"
        ):
            root = Path(temp)
            with self.assertRaises(NonRepairableVerificationError):
                close_independent_verification(
                    spec,
                    backend,
                    outcome,
                    {"requirements": [], "ports": []},
                    plan,
                    expected,
                    plan,
                    expected,
                    root,
                    interface="module demo(input logic clk, output logic y); endmodule\n",
                    python_model="class TopModule: pass\n",
                    verifier=verifier,
                    contract_path=root / "contract.json",
                    plan_path=root / "testplan.json",
                    oracle_path=root / "reference_model.py",
                    expected_path=root / "expected-trace.json",
                    manifest_path=root / "frozen-manifest.json",
                    frozen_manifest={},
                )
            persisted = json.loads(
                (root / "independent-verification.json").read_text()
            )

        self.assertEqual(persisted["status"], "failed")
        self.assertEqual(len(persisted["attempts"]), 1)
        backend.generate.assert_not_called()

    def test_malformed_independent_transcript_stops_before_rtl_repair(self):
        valid = {
            "match_rate": 1.0,
            "num_tests": 2,
            "total_checks": 2,
            "mismatched": 0,
            "mismatches": [],
            "sv_error": None,
            "py_error": None,
            "meta_error": None,
            "stimuli": [{}, {}],
            "verilog_trace": [{"y": 0}, {"y": 0}],
            "python_trace": [{"y": 0}, {"y": 0}],
        }

        def missing_output(result):
            result["verilog_trace"][0] = {}
            result["python_trace"][0] = {}

        def none_output(result):
            result["verilog_trace"][0]["y"] = None
            result["python_trace"][0]["y"] = None

        def unknown_output(result):
            result["verilog_trace"][0]["y"] = "X"
            result["python_trace"][0]["y"] = "Z"

        def out_of_range_output(result):
            result["verilog_trace"][0]["y"] = 2
            result["python_trace"][0]["y"] = 2

        def malformed_error_field(result):
            result["sv_error"] = {"unexpected": "object"}

        def extra_mismatch_field(result):
            result["match_rate"] = 0.5
            result["mismatched"] = 1
            result["python_trace"][0]["y"] = 1
            result["mismatches"] = [
                {
                    "test": 0,
                    "signal": "y",
                    "width": 1,
                    "verilog": 0,
                    "python": 1,
                    "inputs": {},
                    "untrusted": True,
                }
            ]

        for label, mutate in (
            ("missing", missing_output),
            ("none", none_output),
            ("unknown", unknown_output),
            ("range", out_of_range_output),
            ("error-field", malformed_error_field),
            ("mismatch-shape", extra_mismatch_field),
        ):
            with self.subTest(label=label), tempfile.TemporaryDirectory() as temp:
                result = json.loads(json.dumps(valid))
                mutate(result)
                backend = Mock()
                verifier = Mock(return_value=result)
                with self.assertRaises(
                    NonRepairableVerificationError
                ) as raised:
                    self._close_minimal_independent(
                        Path(temp),
                        self._minimal_independent_spec(),
                        verifier,
                        backend,
                    )
                self.assertFalse(
                    raised.exception.error["repairable_by_rtl"]
                )
                backend.generate.assert_not_called()

    def test_non_object_independent_result_is_nonrepairable_evidence(self):
        backend = Mock()
        with tempfile.TemporaryDirectory() as temp:
            with self.assertRaises(
                NonRepairableVerificationError
            ) as raised:
                self._close_minimal_independent(
                    Path(temp),
                    self._minimal_independent_spec(),
                    Mock(return_value=[]),
                    backend,
                )
        self.assertEqual(raised.exception.error["category"], "evidence")
        self.assertFalse(raised.exception.error["repairable_by_rtl"])
        backend.generate.assert_not_called()

    def test_independent_timeout_strings_are_retryable_infrastructure(self):
        for field, detail in (
            ("sv_error", "sv_compile_timeout: exceeded 30 seconds"),
            ("py_error", "py_exec_timeout: exceeded 30 seconds"),
        ):
            with self.subTest(field=field), tempfile.TemporaryDirectory() as temp:
                result = {
                    "match_rate": 0.0,
                    "num_tests": 2,
                    "mismatched": 0,
                    "mismatches": [],
                    "sv_error": None,
                    "py_error": None,
                    "meta_error": None,
                    "stimuli": [{}, {}],
                }
                result[field] = detail
                backend = Mock()
                with self.assertRaises(
                    NonRepairableVerificationError
                ) as raised:
                    self._close_minimal_independent(
                        Path(temp),
                        self._minimal_independent_spec(),
                        Mock(return_value=result),
                        backend,
                    )
                error = raised.exception.error
                self.assertEqual(error["category"], "infrastructure")
                self.assertFalse(error["repairable_by_rtl"])
                self.assertTrue(error["retryable"])
                backend.generate.assert_not_called()

    def test_typed_independent_infrastructure_error_skips_rtl_repair(self):
        backend = Mock()
        error = ContainerInfrastructureError(
            "independent verifier container timed out", provider="sv-compile"
        )
        with tempfile.TemporaryDirectory() as temp:
            with self.assertRaises(ContainerInfrastructureError) as raised:
                self._close_minimal_independent(
                    Path(temp),
                    self._minimal_independent_spec(),
                    Mock(side_effect=error),
                    backend,
                )
            persisted = json.loads(
                (Path(temp) / "independent-verification.json").read_text()
            )
        self.assertFalse(raised.exception.error["repairable_by_rtl"])
        self.assertTrue(raised.exception.error["retryable"])
        self.assertEqual(persisted["status"], "failed")
        backend.generate.assert_not_called()

    def test_counterexample_drives_bounded_candidate_closure(self):
        spec = SimpleNamespace(
            name="demo",
            architecture={},
            ports=(
                SimpleNamespace(name="clk", direction="input", width=1),
                SimpleNamespace(name="y", direction="output", width=1),
            ),
            clock_port="clk",
            reset=None,
            generation={
                "seed": 11,
                "temperature": 0.1,
                "independent_verify_tests": 2,
            },
            verification={
                "max_repair_rounds": 1,
                "repair_candidates": 1,
            },
        )
        plan = {"schema_version": 1, "scenarios": []}
        expected = {"trace": [], "setup_observations": []}
        active_suite = suite_hash(plan, expected)
        initial_rtl = """module demo(input logic clk, output logic y);
  assign y = 0;
endmodule"""
        repaired_rtl = """module demo(input logic clk, output logic y);
  assign y = 1;
endmodule"""
        outcome = RepairOutcome(
            initial_rtl,
            {"passed": True, "suite_sha256": active_suite},
            [],
            [],
            False,
            False,
        )
        failure = {
            "match_rate": 0.5,
            "num_tests": 2,
            "total_checks": 2,
            "mismatched": 1,
            "mismatches": [
                {
                    "test": 1,
                    "signal": "y",
                    "width": 1,
                    "verilog": 0,
                    "python": 1,
                    "inputs": {},
                }
            ],
            "sv_error": None,
            "py_error": None,
            "meta_error": None,
            "stimuli": [{}, {}],
            "verilog_trace": [{"y": 0}, {"y": 0}],
            "python_trace": [{"y": 0}, {"y": 1}],
        }
        passed = {
            "match_rate": 1.0,
            "num_tests": 2,
            "total_checks": 2,
            "mismatched": 0,
            "mismatches": [],
            "sv_error": None,
            "py_error": None,
            "meta_error": None,
            "stimuli": [{}, {}],
            "verilog_trace": [{"y": 0}, {"y": 1}],
            "python_trace": [{"y": 0}, {"y": 1}],
            # These untrusted fields must remain nested and cannot replace the
            # controller's run envelope.
            "run": 999,
            "seed": -1,
            "passed": False,
        }
        verifier = Mock(side_effect=[failure, failure, passed, passed])
        backend = Mock()
        backend.generate.return_value = [
            f"<answer>\n```systemverilog\n{repaired_rtl}\n```\n</answer>"
        ]
        accepted_verification = {"passed": True, "suite_sha256": active_suite}

        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            with patch(
                "rtl_closure.repair._evaluate",
                return_value=(repaired_rtl, accepted_verification),
            ) as evaluate:
                accepted, report = close_independent_verification(
                    spec,
                    backend,
                    outcome,
                    {"requirements": [], "ports": []},
                    plan,
                    expected,
                    plan,
                    expected,
                    root,
                    interface="module demo(input logic clk, output logic y); endmodule\n",
                    python_model="class TopModule: pass\n",
                    verifier=verifier,
                    contract_path=root / "contract.json",
                    plan_path=root / "testplan.json",
                    oracle_path=root / "reference_model.py",
                    expected_path=root / "expected-trace.json",
                    manifest_path=root / "frozen-manifest.json",
                    frozen_manifest={},
                )

            self.assertEqual(accepted.rtl, repaired_rtl)
            self.assertTrue(accepted.repaired)
            self.assertEqual(report["status"], "pass")
            self.assertEqual(len(report["attempts"]), 2)
            self.assertEqual(len(report["counterexamples"]), 2)
            self.assertEqual(len(report["runs"]), 2)
            self.assertTrue(all(row["passed"] for row in report["runs"]))
            self.assertEqual([row["run"] for row in report["runs"]], [1, 2])
            self.assertEqual(
                [row["seed"] for row in report["runs"]], [1020, 2038]
            )
            self.assertTrue(
                all(row["verification"]["run"] == 999 for row in report["runs"])
            )
            replay = report["counterexamples"][0]["verification"]
            self.assertEqual(replay["stimuli"], [{}, {}])
            self.assertEqual(len(replay["verilog_trace"]), 2)
            self.assertEqual(len(replay["python_trace"]), 2)
            persisted = json.loads((root / "independent-verification.json").read_text())
            self.assertEqual(persisted, report)
            self.assertTrue(
                any(row.get("accepted") is True for row in accepted.history)
            )
            evaluate.assert_called_once()


if __name__ == "__main__":
    unittest.main()
