from __future__ import annotations

import json
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from rtl_closure.contract import build_contract, content_hash
from rtl_closure.errors import ContainerInfrastructureError
from rtl_closure.oracle import _trace_behavior_hash
from rtl_closure.repair import repair_loop
from rtl_closure.spec import load_spec, validate_spec
from rtl_closure.verification import (
    VERIFY_IMAGE,
    _run_container,
    check_oracle_policy,
    generate_expected_trace,
    validate_expected_trace,
    verify_rtl,
)


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
INTERNAL_RTL = """module rtl_closure_counter(
  input wire clk,
  input wire rst_n,
  input wire enable,
  output reg [7:0] count
);
reg [7:0] next_count;
always @* begin
  next_count = count;
  if (!rst_n) next_count = 8'b0;
  else if (enable) next_count = count + 1'b1;
end
always @(posedge clk) count <= next_count;
endmodule"""


def docker_ready() -> bool:
    if shutil.which("docker") is None:
        return False
    return (
        subprocess.run(
            ["docker", "image", "inspect", VERIFY_IMAGE],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            check=False,
        ).returncode
        == 0
    )


class FixBackend:
    def generate(self, prompt, n=1, **kwargs):
        del prompt, kwargs
        proposal = {
            "diagnosis": "The counter increments by two instead of one.",
            "confidence": 1.0,
            "edits": [
                {
                    "block_id": "B001",
                    "replacement": """always @(posedge clk) begin
  if (!rst_n) count <= 8'b0;
  else if (enable) count <= count + 1'b1;
end""",
                }
            ],
        }
        return [json.dumps(proposal) for _ in range(n)]


@unittest.skipUnless(docker_ready(), "trusted verification image is unavailable")
class VerificationIntegrationTests(unittest.TestCase):
    def _spec_and_plan(self):
        raw = json.loads(EXAMPLE.read_text())
        raw["verification"] = {
            "requirements": [
                {
                    "id": "REQ_COUNT",
                    "text": "Increment by exactly one when enable is high.",
                    "kind": "functional",
                    "signals": ["enable", "count"],
                }
            ],
            "directed_tests": [
                {
                    "id": "TEST_COUNT",
                    "description": "Hold then increment for three cycles.",
                    "requirement_ids": ["REQ_COUNT"],
                    "reset_before": True,
                    "cycles": [
                        {"drive": {"enable": 0}, "repeat": 1},
                        {"drive": {"enable": 1}, "repeat": 3},
                    ],
                }
            ],
            "max_repair_rounds": 1,
            "repair_candidates": 1,
            "random_regression_cycles": 0,
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
        contract = build_contract(spec, spec.verification["requirements"])
        return spec, contract, plan

    def test_cocotb_pass_and_structured_failure(self):
        spec, _, plan = self._spec_and_plan()
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            expected = generate_expected_trace(spec, ORACLE, plan, root / "oracle")
            good = verify_rtl(spec, GOOD_RTL, plan, expected, root / "good")
            bad = verify_rtl(spec, BAD_RTL, plan, expected, root / "bad")
            self.assertTrue(good["passed"])
            self.assertFalse(bad["passed"])
            self.assertEqual(good["rtl_sha256"], content_hash(GOOD_RTL))
            self.assertEqual(bad["rtl_sha256"], content_hash(BAD_RTL))
            self.assertEqual(bad["failure_class"], "value_mismatch")
            self.assertEqual(bad["mismatches"][0]["scenario_id"], "TEST_COUNT")
            self.assertIsNotNone(bad["artifacts"]["waveform"])
            self.assertIn("oracle_line_coverage", expected)
            self.assertEqual(
                expected["oracle_line_coverage"]["metric"],
                "oracle_line_coverage",
            )
            self.assertNotIn("oracle_coverage", expected)

    def test_dependency_cone_internal_signal_is_sampled(self):
        spec, _, plan = self._spec_and_plan()
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            expected = generate_expected_trace(spec, ORACLE, plan, root / "oracle")
            result = verify_rtl(
                spec,
                INTERNAL_RTL,
                plan,
                expected,
                root / "internal",
                debug_signals=["next_count"],
            )
        self.assertTrue(result["passed"], result)
        self.assertIn("next_count", result["observed_internal_signals"])
        self.assertTrue(
            any("next_count" in row["internal"] for row in result["actual_trace"])
        )

    def _reset_spec_and_plan(self, *, active_low: bool, asynchronous: bool):
        raw = json.loads(EXAMPLE.read_text())
        raw["design"]["reset"]["active_low"] = active_low
        raw["design"]["reset"]["asynchronous"] = asynchronous
        raw["verification"] = {
            "requirements": [
                {
                    "id": "REQ_RESET",
                    "text": "Reset clears count to zero with the declared timing.",
                    "kind": "reset",
                    "signals": ["rst_n", "count"],
                }
            ],
            "directed_tests": [
                {
                    "id": "TEST_RESET",
                    "description": "Assert reset, release it, then count twice.",
                    "requirement_ids": ["REQ_RESET"],
                    "reset_before": True,
                    "cycles": [{"drive": {"enable": 1}, "repeat": 2}],
                }
            ],
            "random_regression_cycles": 0,
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
        reset_expression = (
            'not inputs.get("rst_n", 1)'
            if active_low
            else 'inputs.get("rst_n", 0)'
        )
        oracle = f"""class TopModule:
    def __init__(self):
        self.count = 0

    def eval(self, inputs):
        if {reset_expression}:
            self.count = 0
        elif inputs.get("enable", 0):
            self.count = (self.count + 1) & 255
        return {{"count": self.count}}
"""
        return spec, plan, oracle

    @staticmethod
    def _counter_rtl(*, active_low: bool, asynchronous: bool, reset_value: int = 0):
        reset_condition = "!rst_n" if active_low else "rst_n"
        reset_edge = "negedge rst_n" if active_low else "posedge rst_n"
        sensitivity = (
            f"posedge clk or {reset_edge}" if asynchronous else "posedge clk"
        )
        return f"""module rtl_closure_counter(
  input wire clk,
  input wire rst_n,
  input wire enable,
  output reg [7:0] count
);
always @({sensitivity}) begin
  if ({reset_condition}) count <= 8'd{reset_value};
  else if (enable) count <= count + 1'b1;
end
endmodule"""

    def test_reset_outputs_are_checked_while_asserted(self):
        spec, plan, oracle = self._reset_spec_and_plan(
            active_low=True, asynchronous=False
        )
        broken = self._counter_rtl(
            active_low=True, asynchronous=False, reset_value=170
        )
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            expected = generate_expected_trace(spec, oracle, plan, root / "oracle")
            result = verify_rtl(spec, broken, plan, expected, root / "broken")
        self.assertFalse(result["passed"])
        self.assertEqual(result["failure_class"], "reset_mismatch")
        self.assertIn(
            result["mismatches"][0]["phase"],
            {"reset_prime_asserted", "reset_asserted"},
        )

    def test_dirty_reset_probe_rejects_declaration_init_without_reset_logic(self):
        spec, plan, oracle = self._reset_spec_and_plan(
            active_low=True, asynchronous=False
        )
        missing_reset = """module rtl_closure_counter(
  input wire clk,
  input wire rst_n,
  input wire enable,
  output reg [7:0] count = 8'b0
);
always @(posedge clk) begin
  if (enable) count <= count + 1'b1;
end
endmodule"""
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            expected = generate_expected_trace(spec, oracle, plan, root / "oracle")
            correct = verify_rtl(
                spec,
                self._counter_rtl(active_low=True, asynchronous=False),
                plan,
                expected,
                root / "correct",
            )
            broken = verify_rtl(
                spec, missing_reset, plan, expected, root / "missing-reset"
            )
        self.assertTrue(correct["passed"], correct)
        self.assertFalse(broken["passed"])
        self.assertEqual(broken["failure_class"], "reset_mismatch")
        self.assertEqual(broken["mismatches"][0]["phase"], "reset_asserted")

    def test_expected_trace_cannot_omit_functional_rows_or_use_out_of_range_inputs(self):
        spec, _, plan = self._spec_and_plan()
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            expected = generate_expected_trace(spec, ORACLE, plan, root / "oracle")
        missing = json.loads(json.dumps(expected))
        missing["trace"].pop()
        with self.assertRaisesRegex(RuntimeError, "sample count"):
            validate_expected_trace(spec, plan, missing)
        missing_prime = json.loads(json.dumps(expected))
        missing_prime["setup_observations"][0].pop("prime_asserted_edge")
        with self.assertRaisesRegex(RuntimeError, "prime_asserted_edge"):
            validate_expected_trace(spec, plan, missing_prime)
        invalid_plan = json.loads(json.dumps(plan))
        invalid_plan["scenarios"][0]["cycles"][0]["drive"]["enable"] = 2
        invalid_expected = json.loads(json.dumps(expected))
        invalid_expected["testplan_sha256"] = content_hash(invalid_plan)
        with self.assertRaisesRegex(RuntimeError, "out of range"):
            validate_expected_trace(spec, invalid_plan, invalid_expected)

    def test_synchronous_reset_cannot_masquerade_as_async_for_either_polarity(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            for active_low in (True, False):
                with self.subTest(active_low=active_low):
                    spec, plan, oracle = self._reset_spec_and_plan(
                        active_low=active_low, asynchronous=True
                    )
                    expected = generate_expected_trace(
                        spec, oracle, plan, root / f"oracle-{active_low}"
                    )
                    synchronous = verify_rtl(
                        spec,
                        self._counter_rtl(
                            active_low=active_low, asynchronous=False
                        ),
                        plan,
                        expected,
                        root / f"sync-{active_low}",
                    )
                    asynchronous = verify_rtl(
                        spec,
                        self._counter_rtl(
                            active_low=active_low, asynchronous=True
                        ),
                        plan,
                        expected,
                        root / f"async-{active_low}",
                    )
                    self.assertFalse(synchronous["passed"])
                    self.assertEqual(
                        synchronous["mismatches"][0]["phase"],
                        "reset_async_assertion",
                    )
                    self.assertTrue(asynchronous["passed"])

    def test_reused_artifact_directory_cannot_return_a_stale_pass(self):
        spec, _, plan = self._spec_and_plan()
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            expected = generate_expected_trace(spec, ORACLE, plan, root / "oracle")
            attempt = root / "same-attempt"
            first = verify_rtl(spec, GOOD_RTL, plan, expected, attempt)
            second = verify_rtl(
                spec,
                "module rtl_closure_counter( BROKEN endmodule",
                plan,
                expected,
                attempt,
            )
        self.assertTrue(first["passed"])
        self.assertFalse(second["passed"])
        self.assertNotEqual(second["returncode"], 0)

    def test_check_ids_remain_unique_when_scenario_names_repeat(self):
        spec, _, plan = self._spec_and_plan()
        duplicate = json.loads(json.dumps(plan["scenarios"][0]))
        plan["scenarios"] = [duplicate, json.loads(json.dumps(duplicate))]
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            expected = generate_expected_trace(spec, ORACLE, plan, root / "oracle")
            result = verify_rtl(spec, GOOD_RTL, plan, expected, root / "verified")
        self.assertTrue(result["passed"])
        check_ids = result["passed_check_ids"]
        self.assertEqual(len(check_ids), result["total_checks"])
        self.assertEqual(len(check_ids), len(set(check_ids)))
        self.assertTrue(any(check_id.startswith("0:TEST_COUNT:") for check_id in check_ids))
        self.assertTrue(any(check_id.startswith("1:TEST_COUNT:") for check_id in check_ids))

    def test_canned_local_repair_closes_the_loop(self):
        spec, contract, plan = self._spec_and_plan()
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            contract_path = root / "contract.json"
            plan_path = root / "testplan.json"
            oracle_path = root / "reference_model.py"
            expected_path = root / "expected-trace.json"
            manifest_path = root / "frozen-manifest.json"
            contract_path.write_text(json.dumps(contract), encoding="utf-8")
            plan_path.write_text(json.dumps(plan), encoding="utf-8")
            oracle_path.write_text(ORACLE, encoding="utf-8")
            expected = generate_expected_trace(spec, ORACLE, plan, root / "oracle")
            expected_path.write_text(json.dumps(expected), encoding="utf-8")
            manifest = {
                "schema_version": 1,
                "contract_sha256": content_hash(contract),
                "testplan_sha256": content_hash(plan),
                "oracle_sha256": content_hash(ORACLE),
                "expected_trace_sha256": content_hash(expected),
            }
            manifest["bundle_sha256"] = content_hash(manifest)
            manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
            behavior_hash = _trace_behavior_hash(expected)
            origin_bundle = {
                "schema_version": 2,
                "origins": [
                    {
                        "origin_id": f"candidate-{index:02d}",
                        "candidate_index": index,
                        "response_sha256": content_hash(f"response-{index}"),
                        "oracle_sha256": content_hash(ORACLE),
                        "response": f"response-{index}",
                        "oracle": ORACLE,
                        "initial_valid": True,
                        "initial_behavior_sha256": behavior_hash,
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
                    for index in (1, 2)
                ],
            }
            progress_events = []
            outcome = repair_loop(
                spec,
                FixBackend(),
                BAD_RTL,
                contract,
                plan,
                expected,
                root,
                contract_path=contract_path,
                plan_path=plan_path,
                oracle_path=oracle_path,
                expected_path=expected_path,
                manifest_path=manifest_path,
                frozen_manifest=manifest,
                origin_bundle=origin_bundle,
                progress=lambda phase, payload: progress_events.append(
                    (phase, payload)
                ),
            )
            self.assertTrue(outcome.verification["passed"])
            self.assertTrue(outcome.repaired)
            self.assertFalse(outcome.exhausted)
            self.assertEqual(sum(row["accepted"] for row in outcome.history), 1)
            self.assertTrue((root / "attempts" / "000" / "failure-packet.json").is_file())
            self.assertTrue(
                (root / "attempts" / "001-candidate-01" / "verification-result.json").is_file()
            )
            completed = [
                payload
                for phase, payload in progress_events
                if phase == "repair_candidate_completed"
            ]
            self.assertEqual(completed[0]["result"], "passed")
            self.assertTrue(completed[0]["eligible"])
            adaptive = json.loads((root / "adaptive-corpus.json").read_text())
            self.assertTrue(adaptive["epochs"])
            self.assertNotEqual(
                adaptive["base_suite_sha256"], adaptive["active_suite_sha256"]
            )
            epoch_quorum = json.loads(
                (root / "adaptive" / "epoch-01" / "oracle-quorum.json").read_text()
            )
            self.assertEqual(epoch_quorum["schema_version"], 2)
            self.assertEqual(epoch_quorum["consensus_count"], 2)
            self.assertEqual(
                adaptive["epochs"][0]["oracle_quorum_sha256"],
                content_hash(epoch_quorum),
            )
            formal = json.loads((root / "formal-report.json").read_text())
            self.assertEqual(formal["status"], "pass")
            phases = [phase for phase, _ in progress_events]
            self.assertIn("repair_refining_tests", phases)
            self.assertIn("repair_suite_extended", phases)
            self.assertIn("formal_running", phases)
            self.assertIn("formal_completed", phases)

    def test_oracle_policy_rejects_imports(self):
        with self.assertRaises(RuntimeError):
            check_oracle_policy("import os\nclass TopModule:\n def eval(self, inputs): return {}")

    def test_oracle_policy_rejects_dangerous_builtin_aliases(self):
        for builtin in ("open", "__import__", "exec"):
            oracle = f"""class TopModule:
    def eval(self, inputs):
        dangerous = {builtin}
        return {{}}
"""
            with self.subTest(builtin=builtin):
                with self.assertRaisesRegex(RuntimeError, "forbidden function"):
                    check_oracle_policy(oracle)


class VerificationUnitTests(unittest.TestCase):
    def test_oracle_and_cocotb_timeouts_are_typed_retryable_infrastructure_errors(self):
        spec = load_spec(EXAMPLE)
        timeout = subprocess.TimeoutExpired(["docker", "run"], 1)
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            with patch(
                "rtl_closure.verification._run_container", side_effect=timeout
            ):
                with self.assertRaises(ContainerInfrastructureError) as oracle_error:
                    generate_expected_trace(
                        spec,
                        ORACLE,
                        {"schema_version": 1, "scenarios": []},
                        root / "oracle",
                    )
            self.assertEqual(oracle_error.exception.provider, "oracle")
            self.assertTrue(oracle_error.exception.retryable)

            with patch(
                "rtl_closure.verification.validate_expected_trace",
                return_value={"expected_check_count": 0, "sample_count": 0},
            ), patch(
                "rtl_closure.verification._run_container", side_effect=timeout
            ):
                with self.assertRaises(ContainerInfrastructureError) as verify_error:
                    verify_rtl(
                        spec,
                        GOOD_RTL,
                        {"schema_version": 1, "scenarios": []},
                        {},
                        root / "verify",
                    )
            self.assertEqual(verify_error.exception.provider, "verify")
            self.assertTrue(verify_error.exception.retryable)

    def test_non_object_runner_json_is_a_structured_parse_failure(self):
        spec = load_spec(EXAMPLE)
        completed = subprocess.CompletedProcess(
            args=[], returncode=0, stdout="", stderr=""
        )
        with tempfile.TemporaryDirectory() as temp:
            artifact_dir = Path(temp)

            def emit_array(*_args, **_kwargs):
                result_path = artifact_dir / "cocotb" / "verification-result.json"
                result_path.write_text("[]\n", encoding="utf-8")
                return completed

            with patch(
                "rtl_closure.verification.validate_expected_trace",
                return_value={"expected_check_count": 0, "sample_count": 0},
            ), patch(
                "rtl_closure.verification._run_container", side_effect=emit_array
            ):
                result = verify_rtl(spec, GOOD_RTL, {"scenarios": []}, {}, artifact_dir)
        self.assertFalse(result["passed"])
        self.assertEqual(result["failure_class"], "result_parse_error")
        self.assertIn("JSON object", result["detail"])

    def test_oracle_behavior_hash_includes_reset_observations(self):
        common_trace = [
            {
                "scenario_index": 0,
                "scenario_id": "TEST_RESET",
                "cycle": 0,
                "expected": {"count": 0},
            }
        ]
        first = {
            "trace": common_trace,
            "setup_observations": [
                {
                    "scenario_index": 0,
                    "scenario_id": "TEST_RESET",
                    "asynchronous_assertion": {"count": 0},
                    "asserted_edges": [{"count": 0}],
                    "deasserted_edge": {"count": 0},
                }
            ],
        }
        second = json.loads(json.dumps(first))
        second["setup_observations"][0]["asserted_edges"] = [{"count": 1}]
        self.assertNotEqual(_trace_behavior_hash(first), _trace_behavior_hash(second))

    def test_container_is_force_removed_when_runner_times_out(self):
        timeout = subprocess.TimeoutExpired(["docker", "run"], 1)
        with patch("rtl_closure.verification._run", side_effect=timeout) as run:
            with patch("rtl_closure.verification._force_remove_container") as remove:
                with self.assertRaises(subprocess.TimeoutExpired):
                    _run_container(
                        ["docker", "run", "--rm", "example.invalid/image"],
                        1,
                        purpose="test",
                    )
        remove.assert_called_once()
        container_name = remove.call_args.args[0]
        invoked = run.call_args.args[0]
        self.assertEqual(invoked[:4], ["docker", "run", "--name", container_name])


if __name__ == "__main__":
    unittest.main()
