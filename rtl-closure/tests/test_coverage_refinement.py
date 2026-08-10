from __future__ import annotations

import json
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from rtl_closure.contract import build_contract, content_hash
from rtl_closure.coverage_refinement import (
    ORACLE_COVERAGE_METRIC,
    enforce_oracle_line_coverage,
    refine_oracle_coverage,
    validate_oracle_line_coverage_evidence,
)
from rtl_closure.errors import ModelInfrastructureError, PreRtlGateError
from rtl_closure.spec import validate_spec
from rtl_closure.verification import VERIFY_IMAGE, generate_expected_trace


EXAMPLE = Path(__file__).parents[1] / "examples" / "counter.json"
BRANCHY_ORACLE = """class TopModule:
    def __init__(self):
        self.count = 0

    def eval(self, inputs):
        if not inputs.get("rst_n", 1):
            self.count = 0
        elif inputs.get("enable", 0):
            if self.count == 2:
                self.count = 7
            else:
                self.count = (self.count + 1) & 255
        else:
            self.count = self.count
        return {"count": self.count}
"""
ORACLE_STUB = "class TopModule: pass"


def docker_ready() -> bool:
    return shutil.which("docker") is not None and subprocess.run(
        ["docker", "image", "inspect", VERIFY_IMAGE],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        check=False,
    ).returncode == 0


class ScenarioBackend:
    def generate(self, prompt, **kwargs):
        del prompt, kwargs
        return [
            json.dumps(
                {
                    "scenarios": [
                        {
                            "id": "COV_ENABLE",
                            "description": "Exercise the enabled increment branch.",
                            "requirement_ids": ["REQ_COUNT"],
                            "reset_before": True,
                            "cycles": [{"drive": {"enable": 1}, "repeat": 3}],
                        }
                    ]
                }
            )
        ]


class FailingBackend:
    def generate(self, prompt, **kwargs):
        del prompt, kwargs
        raise RuntimeError("planner transport failed")


class NeverBackend:
    def generate(self, prompt, **kwargs):
        del prompt, kwargs
        raise AssertionError("coverage planner should not have been called")


class NoImprovementBackend:
    def generate(self, prompt, **kwargs):
        del prompt, kwargs
        return [
            json.dumps(
                {
                    "scenarios": [
                        {
                            "id": "COV_NO_GAIN",
                            "description": "A valid input scenario with no coverage gain.",
                            "requirement_ids": ["REQ_COUNT"],
                            "reset_before": True,
                            "cycles": [{"drive": {"enable": 0}, "repeat": 2}],
                        }
                    ]
                }
            )
        ]


class CoverageRefinementStatusTests(unittest.TestCase):
    def _inputs(
        self,
        *,
        rounds: int,
        budget: int,
        mode: str = "advisory",
        target: int = 85,
    ):
        raw = json.loads(EXAMPLE.read_text())
        raw["verification"] = {
            "oracle_coverage_target_pct": target,
            "oracle_coverage_rounds": rounds,
            "oracle_coverage_cycle_budget": budget,
            "oracle_line_coverage_mode": mode,
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
            "scenarios": [],
        }
        line_coverage = {
            "metric": "oracle_line_coverage",
            "coverage_type": "python_source_line",
            "scope": "TopModule executable statement lines",
            "executable_lines": [1, 2, 3, 4],
            "executed_lines": [1, 2],
            "missing_lines": [3, 4],
            "covered": 2,
            "total": 4,
            "percent": 50.0,
        }
        trace = {
            "trace": [],
            "setup_observations": [],
            "oracle_line_coverage": dict(line_coverage),
            "testplan_sha256": content_hash(plan),
            "oracle_sha256": content_hash(ORACLE_STUB),
        }
        return spec, plan, trace

    def test_planner_error_is_reported_to_progress_without_losing_base_suite(self):
        spec, plan, trace = self._inputs(rounds=1, budget=8)
        progress = []
        with tempfile.TemporaryDirectory() as temp:
            refined_plan, refined_trace, report = refine_oracle_coverage(
                spec,
                FailingBackend(),
                {"requirements": []},
                plan,
                ORACLE_STUB,
                trace,
                Path(temp),
                progress=lambda phase, summary: progress.append((phase, summary)),
            )
        self.assertEqual(refined_plan, plan)
        self.assertEqual(refined_trace, trace)
        self.assertEqual(report["status"], "error")
        self.assertFalse(report["target_met"])
        self.assertEqual(report["remaining_cycle_budget"], 8)
        self.assertEqual(report["error"], "planner transport failed")
        self.assertIn("target was not met", report["warning"])
        self.assertEqual(progress[-1][0], "coverage_refinement_error")
        self.assertEqual(progress[-1][1]["error"], "planner transport failed")
        self.assertEqual(report["metric"], ORACLE_COVERAGE_METRIC)
        self.assertFalse(report["rtl_functional_coverage"])
        self.assertIn("not RTL functional", report["limitation"])

    def test_model_infrastructure_failure_is_not_demoted_to_coverage_warning(self):
        spec, plan, trace = self._inputs(rounds=1, budget=8)
        failure = ModelInfrastructureError("model gateway unavailable", retryable=True)

        class InfrastructureFailureBackend:
            def generate(self, *_args, **_kwargs):
                raise failure

        with tempfile.TemporaryDirectory() as temp:
            with self.assertRaises(ModelInfrastructureError) as raised:
                refine_oracle_coverage(
                    spec,
                    InfrastructureFailureBackend(),
                    {"requirements": []},
                    plan,
                    ORACLE_STUB,
                    trace,
                    Path(temp),
                )
        self.assertIs(raised.exception, failure)
        self.assertTrue(raised.exception.retryable)

    def test_no_improvement_does_not_commit_scenarios_or_consume_budget(self):
        spec, plan, trace = self._inputs(rounds=1, budget=8)

        def unchanged_trace(_spec, _oracle, candidate_plan, _artifact_dir):
            candidate = json.loads(json.dumps(trace))
            candidate["testplan_sha256"] = content_hash(candidate_plan)
            return candidate

        with tempfile.TemporaryDirectory() as temp, patch(
            "rtl_closure.coverage_refinement.generate_expected_trace",
            side_effect=unchanged_trace,
        ):
            refined_plan, refined_trace, report = refine_oracle_coverage(
                spec,
                NoImprovementBackend(),
                {
                    "requirements": [
                        {
                            "id": "REQ_COUNT",
                            "text": "Enable controls whether the count changes.",
                            "signals": ["enable", "count"],
                        }
                    ]
                },
                plan,
                ORACLE_STUB,
                trace,
                Path(temp),
            )
        self.assertEqual(refined_plan, plan)
        self.assertEqual(refined_trace, trace)
        self.assertEqual(report["remaining_cycle_budget"], 8)
        self.assertEqual(report["added_scenario_count"], 0)
        self.assertEqual(report["stop_reason"], "no_improvement")
        self.assertFalse(report["rounds"][0]["improved"])
        self.assertEqual(report["rounds"][0]["accepted_scenarios"], [])
        self.assertEqual(report["rounds"][0]["candidate_scenarios"], ["COV_NO_GAIN"])

    def test_exhausted_refinement_is_an_explicit_warning(self):
        spec, plan, trace = self._inputs(rounds=0, budget=0)
        progress = []
        with tempfile.TemporaryDirectory() as temp:
            _, _, report = refine_oracle_coverage(
                spec,
                NeverBackend(),
                {"requirements": []},
                plan,
                ORACLE_STUB,
                trace,
                Path(temp),
                progress=lambda phase, summary: progress.append((phase, summary)),
            )
        self.assertEqual(report["status"], "warning")
        self.assertFalse(report["target_met"])
        self.assertIsNone(report["error"])
        self.assertEqual(report["stop_reason"], "rounds_exhausted")
        self.assertEqual(progress[-1][0], "coverage_refinement_warning")

    def test_required_proxy_coverage_is_a_typed_pre_rtl_gate(self):
        spec, plan, trace = self._inputs(rounds=0, budget=0, mode="required")
        with tempfile.TemporaryDirectory() as temp:
            _, _, report = refine_oracle_coverage(
                spec,
                NeverBackend(),
                {"requirements": []},
                plan,
                ORACLE_STUB,
                trace,
                Path(temp),
            )
        with self.assertRaises(PreRtlGateError) as raised:
            enforce_oracle_line_coverage(spec, report)
        self.assertEqual(raised.exception.failure_class, "verification_configuration")
        self.assertEqual(raised.exception.failure_gate, "oracle_line_coverage")
        self.assertEqual(
            raised.exception.failure_code, "verification_configuration/coverage"
        )

    def test_strict_validator_recomputes_result_and_binds_final_artifacts(self):
        spec, plan, trace = self._inputs(
            rounds=0, budget=0, mode="required", target=50
        )
        with tempfile.TemporaryDirectory() as temp:
            _, _, report = refine_oracle_coverage(
                spec,
                NeverBackend(),
                {"requirements": []},
                plan,
                ORACLE_STUB,
                trace,
                Path(temp),
            )
        quorum = {
            "final_testplan_sha256": content_hash(plan),
            "selected_expected_trace_sha256": content_hash(trace),
            "selected_oracle_sha256": content_hash(ORACLE_STUB),
        }
        report["final_oracle_quorum_sha256"] = content_hash(quorum)
        result = validate_oracle_line_coverage_evidence(
            spec,
            report,
            final_plan=plan,
            final_expected_trace=trace,
            final_quorum=quorum,
        )
        self.assertTrue(result["target_met"])
        enforce_oracle_line_coverage(
            spec,
            report,
            final_plan=plan,
            final_expected_trace=trace,
            final_quorum=quorum,
        )

        mutations = []
        forged = json.loads(json.dumps(report))
        forged["target_met"] = False
        mutations.append(forged)
        forged = json.loads(json.dumps(report))
        forged["final"]["percent"] = 99.0
        mutations.append(forged)
        forged = json.loads(json.dumps(report))
        forged["status"] = "warning"
        mutations.append(forged)
        forged = json.loads(json.dumps(report))
        forged["mode"] = "advisory"
        mutations.append(forged)
        forged = json.loads(json.dumps(report))
        forged["final_testplan_sha256"] = "0" * 64
        mutations.append(forged)
        for forged in mutations:
            with self.subTest(forged=forged):
                with self.assertRaisesRegex(RuntimeError, "evidence is invalid"):
                    validate_oracle_line_coverage_evidence(
                        spec,
                        forged,
                        final_plan=plan,
                        final_expected_trace=trace,
                        final_quorum=quorum,
                    )

    def test_required_gate_rejects_a_forged_target_met_claim(self):
        spec, plan, trace = self._inputs(
            rounds=0, budget=0, mode="required", target=85
        )
        with tempfile.TemporaryDirectory() as temp:
            _, _, report = refine_oracle_coverage(
                spec,
                NeverBackend(),
                {"requirements": []},
                plan,
                ORACLE_STUB,
                trace,
                Path(temp),
            )
        report["target_met"] = True
        report["status"] = "passed"
        report["stop_reason"] = "target_met"
        report["warning"] = None
        with self.assertRaisesRegex(PreRtlGateError, "target_met disagrees"):
            enforce_oracle_line_coverage(spec, report)

    def test_off_mode_does_not_refine_or_emit_a_functional_claim(self):
        spec, plan, trace = self._inputs(rounds=3, budget=8, mode="off")
        progress = []
        with tempfile.TemporaryDirectory() as temp:
            refined_plan, _, report = refine_oracle_coverage(
                spec,
                NeverBackend(),
                {"requirements": []},
                plan,
                ORACLE_STUB,
                trace,
                Path(temp),
                progress=lambda phase, summary: progress.append((phase, summary)),
            )
        self.assertEqual(refined_plan, plan)
        self.assertEqual(report["status"], "not_requested")
        self.assertEqual(report["stop_reason"], "disabled")
        self.assertFalse(report["rtl_functional_coverage"])
        self.assertEqual(progress[-1][0], "coverage_refinement_disabled")


@unittest.skipUnless(docker_ready(), "trusted verification image is unavailable")
class CoverageRefinementIntegrationTests(unittest.TestCase):
    def test_missing_oracle_lines_drive_validated_scenario_refinement(self):
        raw = json.loads(EXAMPLE.read_text())
        raw["verification"] = {
            "requirements": [
                {
                    "id": "REQ_COUNT",
                    "text": "Hold or increment according to enable.",
                    "signals": ["enable", "count"],
                }
            ],
            "directed_tests": [
                {
                    "id": "TEST_HOLD",
                    "description": "Exercise only the hold branch.",
                    "requirement_ids": ["REQ_COUNT"],
                    "cycles": [{"drive": {"enable": 0}, "repeat": 2}],
                }
            ],
            "random_regression_cycles": 0,
            "oracle_coverage_target_pct": 100,
            "oracle_coverage_rounds": 2,
            "oracle_coverage_cycle_budget": 8,
        }
        spec = validate_spec(raw)
        contract = build_contract(spec, spec.verification["requirements"])
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
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            initial = generate_expected_trace(
                spec, BRANCHY_ORACLE, plan, root / "initial"
            )
            refined_plan, refined_trace, report = refine_oracle_coverage(
                spec,
                ScenarioBackend(),
                contract,
                plan,
                BRANCHY_ORACLE,
                initial,
                root / "refinement",
            )
        self.assertLess(initial["oracle_line_coverage"]["percent"], 100)
        self.assertGreater(
            refined_trace["oracle_line_coverage"]["percent"],
            initial["oracle_line_coverage"]["percent"],
        )
        self.assertEqual(report["added_scenario_count"], 1)
        self.assertEqual(refined_plan["scenarios"][-1]["source"], "oracle_coverage_refinement")


if __name__ == "__main__":
    unittest.main()
