from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from rtl_closure.adaptive import (
    behavioral_coverage,
    build_suite_epoch,
    derive_counterexample_scenarios,
    suite_hash,
    verify_epoch_chain,
)
from rtl_closure.contract import content_hash
from rtl_closure.repair import _local_prompt, candidate_improves
from rtl_closure.spec import load_spec


EXAMPLE = Path(__file__).parents[1] / "examples" / "counter.json"


class AdaptiveSuiteTests(unittest.TestCase):
    def test_toggle_coverage_respects_reset_scenario_boundaries(self):
        contract = {
            "requirements": [],
            "ports": [
                {"name": "a", "direction": "input", "width": 1},
                {"name": "y", "direction": "output", "width": 1},
            ],
        }
        plan = {
            "scenarios": [
                {"id": "FIRST", "reset_before": True},
                {"id": "SECOND", "reset_before": True},
            ]
        }
        result = {
            "actual_trace": [
                {
                    "scenario_index": 0,
                    "scenario_id": "FIRST",
                    "inputs": {"a": 0},
                    "actual": {"y": 0},
                },
                {
                    "scenario_index": 1,
                    "scenario_id": "SECOND",
                    "inputs": {"a": 1},
                    "actual": {"y": 1},
                },
            ],
            "mismatches": [],
            "passed_check_ids": [],
        }
        coverage = behavioral_coverage(contract, plan, result)
        self.assertEqual(coverage["inputs"]["a"]["toggles"], 0)
        self.assertEqual(coverage["outputs"]["y"]["toggles"], 0)

        plan["scenarios"][1]["reset_before"] = False
        continuous = behavioral_coverage(contract, plan, result)
        self.assertEqual(continuous["inputs"]["a"]["toggles"], 1)
        self.assertEqual(continuous["outputs"]["y"]["toggles"], 1)

    def test_requirement_labels_are_not_reported_as_condition_coverage(self):
        contract = {
            "requirements": [
                {
                    "id": "REQ_COUNT",
                    "signals": ["enable", "count"],
                }
            ],
            "ports": [
                {"name": "enable", "direction": "input", "width": 1},
                {"name": "count", "direction": "output", "width": 8},
            ],
        }
        plan = {
            "scenarios": [
                {
                    "id": "TEST_COUNT",
                    "requirement_ids": ["REQ_COUNT"],
                }
            ]
        }
        trace = {
            "actual_trace": [
                {
                    "scenario_index": 0,
                    "scenario_id": "TEST_COUNT",
                    "cycle": 0,
                    "inputs": {"enable": 0},
                    "expected": {"count": 0},
                    "actual": {"count": 0},
                },
                {
                    "scenario_index": 0,
                    "scenario_id": "TEST_COUNT",
                    "cycle": 1,
                    "inputs": {"enable": 1},
                    "expected": {"count": 1},
                    "actual": {"count": 1},
                },
            ],
            "mismatches": [],
            "passed_check_ids": ["A", "B"],
            "mismatched": 0,
        }
        coverage = behavioral_coverage(contract, plan, trace)
        requirement = coverage["requirements"]["REQ_COUNT"]
        self.assertEqual(requirement["scenario_associated_cycles"], 2)
        self.assertEqual(requirement["io_observation_cycles"], 2)
        self.assertEqual(requirement["condition_hit_cycles"], 0)
        self.assertEqual(requirement["status"], "observed")
        self.assertFalse(
            coverage["requirement_coverage_semantics"][
                "scenario_association_is_coverage"
            ]
        )

        trace["actual_trace"][1]["requirement_hits"] = ["REQ_COUNT"]
        explicit = behavioral_coverage(contract, plan, trace)
        requirement = explicit["requirements"]["REQ_COUNT"]
        self.assertEqual(requirement["condition_hit_cycles"], 1)
        self.assertEqual(requirement["status"], "passing")
        self.assertEqual(requirement["coverage_basis"], "explicit_condition_hit")

    def test_repeated_payload_wraparound_is_reported_as_weak_evidence(self):
        contract = {
            "requirements": [],
            "ports": [
                {"name": "push", "direction": "input", "width": 1},
                {"name": "data_in", "direction": "input", "width": 8},
                {"name": "data_out", "direction": "output", "width": 8},
            ],
        }
        plan = {
            "scenarios": [
                {
                    "id": "TEST_POINTER_WRAPAROUND",
                    "description": "Exercise circular pointer wraparound.",
                    "requirement_ids": [],
                }
            ]
        }
        result = {
            "actual_trace": [
                {
                    "scenario_index": 0,
                    "scenario_id": "TEST_POINTER_WRAPAROUND",
                    "cycle": cycle,
                    "phase": "post_rising_edge",
                    "inputs": {"push": 1, "data_in": 0x22},
                    "expected": {"data_out": 0x22},
                    "actual": {"data_out": 0x22},
                }
                for cycle in range(8)
            ],
            "mismatches": [],
            "passed_check_ids": [],
            "mismatched": 0,
        }
        repeated = behavioral_coverage(contract, plan, result)
        evidence = repeated["wraparound_payload_evidence"][0]
        self.assertEqual(evidence["status"], "insufficient_repeated_payload")
        self.assertEqual(
            evidence["payload_signals"]["data_in"]["distinct_value_count"], 1
        )
        self.assertEqual(
            repeated["quality_warnings"][0]["code"],
            "repeated_payload_wraparound",
        )

        for cycle, row in enumerate(result["actual_trace"]):
            row["inputs"]["data_in"] = cycle & 3
        diverse = behavioral_coverage(contract, plan, result)
        self.assertEqual(
            diverse["wraparound_payload_evidence"][0]["status"],
            "payload_diversity_observed",
        )
        self.assertEqual(diverse["quality_warnings"], [])

    def test_counterexample_mutations_are_bounded_and_epoch_is_hash_chained(self):
        spec = load_spec(EXAMPLE)
        plan = {
            "schema_version": 1,
            "scenarios": [
                {
                    "id": "TEST_COUNT",
                    "requirement_ids": ["REQ_COUNT"],
                    "reset_before": True,
                    "cycles": [
                        {"drive": {"enable": 0}},
                        {"drive": {"enable": 1}, "repeat": 2},
                    ],
                }
            ],
        }
        packet = {
            "fingerprint": "f" * 64,
            "scenario_id": "TEST_COUNT",
            "requirement_ids": ["REQ_COUNT"],
            "first_failure_cycle": 1,
        }
        additions = derive_counterexample_scenarios(
            spec, plan, packet, round_index=1
        )
        self.assertTrue(additions)
        self.assertLessEqual(
            len(additions), spec.verification["adaptive_tests_per_round"]
        )
        active = json.loads(json.dumps(plan))
        active["scenarios"].extend(additions)
        base_expected = {"trace": [{"expected": {"count": 0}}]}
        active_expected = json.loads(json.dumps(base_expected))
        active_expected["trace"].extend(
            {"expected": {"count": index + 1}}
            for index, _ in enumerate(additions)
        )
        base = suite_hash(plan, base_expected)
        quorum = {"schema_version": 2, "quorum_met": True}
        epoch = build_suite_epoch(
            epoch=1,
            parent_epoch_sha256=None,
            parent_suite_sha256=base,
            additions=additions,
            plan=active,
            expected_trace=active_expected,
            oracle_quorum=quorum,
        )
        with tempfile.TemporaryDirectory() as temp:
            artifact_root = Path(temp)
            epoch_dir = artifact_root / "epoch-01"
            epoch_dir.mkdir()
            (epoch_dir / "testplan.json").write_text(json.dumps(active))
            (epoch_dir / "expected-trace.json").write_text(
                json.dumps(active_expected)
            )
            (epoch_dir / "oracle-quorum.json").write_text(json.dumps(quorum))
            (epoch_dir / "epoch.json").write_text(json.dumps(epoch))
            self.assertEqual(
                verify_epoch_chain(
                    [epoch],
                    plan,
                    base_expected,
                    epoch_artifact_root=artifact_root,
                ),
                epoch["suite_sha256"],
            )
            tampered = json.loads(json.dumps(epoch))
            tampered["scenario_ids"].append("INJECTED")
            with self.assertRaisesRegex(RuntimeError, "scenario IDs"):
                verify_epoch_chain(
                    [tampered],
                    plan,
                    base_expected,
                    epoch_artifact_root=artifact_root,
                )
            changed_base = json.loads(json.dumps(plan))
            changed_base["scenarios"][0]["description"] = "tampered"
            with self.assertRaisesRegex(RuntimeError, "discontinuous"):
                verify_epoch_chain(
                    [epoch],
                    changed_base,
                    base_expected,
                    epoch_artifact_root=artifact_root,
                )

    def test_counterexample_mutation_rejects_non_replayable_continuous_state(self):
        spec = load_spec(EXAMPLE)
        packet = {
            "scenario_id": "CONTINUE",
            "first_failure_cycle": 0,
            "fingerprint": "f" * 64,
        }
        plan = {
            "scenarios": [
                {
                    "id": "CONTINUE",
                    "reset_before": False,
                    "cycles": [{"drive": {"enable": 1}}],
                }
            ]
        }
        self.assertEqual(
            derive_counterexample_scenarios(spec, plan, packet, round_index=1), []
        )

    def test_epoch_builder_rejects_additions_not_present_as_plan_suffix(self):
        base_plan = {"scenarios": [{"id": "BASE", "cycles": []}]}
        addition = {"id": "ADDED", "cycles": [{"drive": {"x": 1}}]}
        with self.assertRaisesRegex(ValueError, "suffix"):
            build_suite_epoch(
                epoch=1,
                parent_epoch_sha256=None,
                parent_suite_sha256="0" * 64,
                additions=[addition],
                plan=base_plan,
                expected_trace={"trace": []},
                oracle_quorum={"schema_version": 2, "quorum_met": True},
            )

    def test_candidate_results_from_different_suites_cannot_be_compared(self):
        champion = {
            "passed": False,
            "suite_sha256": content_hash("suite-a"),
            "first_failure_ordinal": 1,
            "mismatched": 2,
            "passed_check_ids": ["A"],
        }
        candidate = {
            "passed": True,
            "suite_sha256": content_hash("suite-b"),
            "total_checks": 10,
            "passed_check_ids": ["A", "B"],
        }
        improved, reason = candidate_improves(champion, candidate)
        self.assertFalse(improved)
        self.assertIn("different suite hashes", reason)

    def test_accumulated_counterexamples_are_replayed_into_repair_prompt(self):
        packet = {
            "fingerprint": "latest",
            "failure_class": "value_mismatch",
            "suspect_blocks": [],
        }
        counterexamples = [
            {
                "fingerprint": "older",
                "source": "formal",
                "failure_class": "formal_counterexample",
                "scenario_id": "TEST_OLD",
                "requirement_ids": ["REQ_OLD"],
                "first_failure_cycle": 7,
                "signal": "done",
                "expected": 1,
                "actual": 0,
                "inputs": {"start": 1},
                "internal_state_delta": [{"signal": "state", "before": 1, "at_failure": 2}],
                "mismatch_cluster": [],
            }
        ]
        prompt = _local_prompt(
            {"requirements": []},
            packet,
            "module d; endmodule",
            [],
            counterexamples,
        )
        self.assertIn("older", prompt)
        self.assertIn("formal_counterexample", prompt)
        self.assertIn("state", prompt)


if __name__ == "__main__":
    unittest.main()
