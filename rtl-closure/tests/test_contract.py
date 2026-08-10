from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from rtl_closure.contract import content_hash, plan_contract, verify_frozen
from rtl_closure.spec import SpecError, load_spec, validate_spec


EXAMPLE = Path(__file__).parents[1] / "examples" / "counter.json"


class InvalidPlanner:
    def generate(self, *args, **kwargs):
        return ["not json"]


class EmptyPlanner:
    def generate(self, *args, **kwargs):
        return ['{"requirements": [], "directed_tests": []}']


class ContractTests(unittest.TestCase):
    def test_invalid_planner_uses_stable_declaration_only_fallback(self):
        spec = load_spec(EXAMPLE)
        first_contract, first_plan, metadata = plan_contract(spec, InvalidPlanner())
        second_contract, second_plan, _ = plan_contract(spec, InvalidPlanner())
        self.assertEqual(metadata["source"], "fallback")
        self.assertEqual(content_hash(first_contract), content_hash(second_contract))
        self.assertEqual(content_hash(first_plan), content_hash(second_plan))
        self.assertGreaterEqual(len(first_plan["scenarios"]), 3)
        for scenario in first_plan["scenarios"]:
            for cycle in scenario["cycles"]:
                self.assertNotIn("expected", cycle)

    def test_empty_planner_output_falls_back_and_covers_every_requirement(self):
        spec = load_spec(EXAMPLE)
        contract, plan, metadata = plan_contract(spec, EmptyPlanner())
        requirement_ids = {row["id"] for row in contract["requirements"]}
        self.assertEqual(metadata["source"], "fallback")
        self.assertTrue(requirement_ids)
        self.assertTrue(plan["scenarios"])
        for scenario in plan["scenarios"]:
            self.assertEqual(set(scenario["requirement_ids"]), requirement_ids)

    def test_fallback_and_random_ids_preserve_all_requirement_links(self):
        raw = json.loads(EXAMPLE.read_text())
        requirements = [
            {"id": "REQ_A", "text": "The counter can hold its current value."},
            {"id": "REQ_B", "text": "The counter increments when enabled."},
        ]
        raw["verification"] = {
            "requirements": requirements,
            "random_regression_cycles": 1,
        }
        spec = validate_spec(raw)
        contract, plan, metadata = plan_contract(spec, InvalidPlanner())
        requirement_ids = {row["id"] for row in contract["requirements"]}
        self.assertEqual(metadata["source"], "fallback")
        for scenario in plan["scenarios"]:
            self.assertEqual(set(scenario["requirement_ids"]), requirement_ids)

        raw["verification"]["directed_tests"] = [
            {
                "id": "RANDOM_REGRESSION_1",
                "description": "User-owned scenario with a system-like identifier.",
                "requirement_ids": ["REQ_A", "REQ_B"],
                "cycles": [{"drive": {"enable": 0}}],
            }
        ]
        spec = validate_spec(raw)
        _, plan, metadata = plan_contract(spec, InvalidPlanner())
        scenario_ids = [scenario["id"] for scenario in plan["scenarios"]]
        self.assertEqual(metadata["source"], "user")
        self.assertEqual(len(scenario_ids), len(set(scenario_ids)))
        self.assertEqual(scenario_ids.count("RANDOM_REGRESSION_1"), 1)
        for scenario in plan["scenarios"]:
            self.assertEqual(set(scenario["requirement_ids"]), {"REQ_A", "REQ_B"})

    def test_unknown_signal_and_duplicate_requirement_are_rejected(self):
        raw = json.loads(EXAMPLE.read_text())
        raw["verification"] = {
            "requirements": [
                {"id": "REQ_A", "text": "First requirement", "signals": ["missing"]}
            ]
        }
        with self.assertRaises(SpecError):
            validate_spec(raw)

        raw["verification"]["requirements"] = [
            {"id": "REQ_A", "text": "First requirement"},
            {"id": "REQ_A", "text": "Duplicate requirement"},
        ]
        with self.assertRaises(SpecError):
            validate_spec(raw)

    def test_xhigh_reasoning_and_frozen_hash_check(self):
        raw = json.loads(EXAMPLE.read_text())
        raw["generation"]["reasoning_effort"] = "xhigh"
        self.assertEqual(validate_spec(raw).generation["reasoning_effort"], "xhigh")
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "contract.json"
            path.write_text('{"a": 1}\n', encoding="utf-8")
            expected = content_hash({"a": 1})
            verify_frozen(path, expected)
            path.write_text('{"a": 2}\n', encoding="utf-8")
            with self.assertRaises(RuntimeError):
                verify_frozen(path, expected)


if __name__ == "__main__":
    unittest.main()
