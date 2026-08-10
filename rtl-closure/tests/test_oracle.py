from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from rtl_closure.contract import build_contract, content_hash
from rtl_closure.errors import ContainerInfrastructureError, PreRtlGateError
from rtl_closure.oracle import (
    build_oracle_origin_bundle,
    generate_frozen_oracle,
    revalidate_oracle_quorum,
    validate_oracle_origin_bundle,
)
from rtl_closure.spec import load_spec, validate_spec


EXAMPLE = Path(__file__).parents[1] / "examples" / "counter.json"
ZERO = """class TopModule:
    def eval(self, inputs):
        return {"count": 0}
"""
ONE = """class TopModule:
    def eval(self, inputs):
        return {"count": 1}
"""
BRANCH = """class TopModule:
    def eval(self, inputs):
        return {"count": 1 if inputs.get("enable", 0) else 0}
"""
INVALID = "import os\n" + ZERO


def wrapped(code: str) -> str:
    return f"<answer>\n```python\n{code}\n```\n</answer>"


def fake_trace(spec, code, plan, artifact_dir):
    del spec, artifact_dir
    value = 1 if '"count": 1' in code else 0
    return {
        "trace": [
            {
                "scenario_index": 0,
                "scenario_id": plan["scenarios"][0]["id"],
                "cycle": 0,
                "expected": {"count": value},
            }
        ],
        "setup_observations": [],
    }


class SequenceBackend:
    def __init__(self, candidates, review, *, provider=None, model=None):
        self.candidates = candidates
        self.review = review
        self.calls = 0
        if provider is not None:
            self.provider = provider
        if model is not None:
            self.model = model

    def generate(self, prompt, n=1, **kwargs):
        del prompt, kwargs
        self.calls += 1
        if self.calls == 1:
            return [wrapped(code) for code in self.candidates[:n]]
        return [wrapped(self.review)]


class OracleQuorumTests(unittest.TestCase):
    def _fixture(self):
        spec = load_spec(EXAMPLE)
        requirements = [
            {
                "id": "REQ_COUNT",
                "text": "Count behavior.",
                "kind": "functional",
                "signals": ["count"],
            }
        ]
        contract = build_contract(spec, requirements)
        plan = {
            "scenarios": [
                {
                    "id": "TEST",
                    "requirement_ids": ["REQ_COUNT"],
                    "cycles": [{"drive": {"enable": 0}}],
                }
            ]
        }
        return spec, contract, plan

    def _diversity_spec(self, mode="required"):
        raw = json.loads(EXAMPLE.read_text())
        raw["verification"] = {
            "oracle_diversity_mode": mode,
            "oracle_min_distinct_sources": 2,
        }
        return validate_spec(raw)

    def test_required_diversity_rejects_single_source_before_model_call(self):
        spec, contract, plan = self._fixture()
        spec = self._diversity_spec()
        backend = SequenceBackend(
            [ZERO, ZERO, ZERO], ZERO, provider="provider-a", model="model-a"
        )
        with tempfile.TemporaryDirectory() as temp:
            with self.assertRaisesRegex(PreRtlGateError, "not met") as raised:
                generate_frozen_oracle(spec, backend, contract, plan, Path(temp))
        self.assertEqual(raised.exception.failure_gate, "oracle_diversity")
        self.assertEqual(backend.calls, 0)

    def test_distinct_provider_model_sources_are_recorded_and_enforced(self):
        _, contract, plan = self._fixture()
        spec = self._diversity_spec()
        provider_a = SequenceBackend(
            [ZERO], ZERO, provider="provider-a", model="model-a"
        )
        provider_b = SequenceBackend(
            [ZERO], ZERO, provider="provider-b", model="model-b"
        )
        with tempfile.TemporaryDirectory() as temp, patch(
            "rtl_closure.oracle.generate_expected_trace", side_effect=fake_trace
        ):
            root = Path(temp)
            _, _, metadata = generate_frozen_oracle(
                spec, [provider_a, provider_b], contract, plan, root
            )
            bundle = build_oracle_origin_bundle(metadata, root)
            validate_oracle_origin_bundle(bundle)
        self.assertTrue(metadata["diversity"]["satisfied"])
        self.assertEqual(metadata["diversity"]["distinct_source_count"], 2)
        self.assertEqual(
            {row["provenance"]["provider"] for row in metadata["candidate_origins"]},
            {"provider-a", "provider-b"},
        )
        self.assertEqual(bundle["schema_version"], 2)
        self.assertTrue(all("provenance" in row for row in bundle["origins"]))

    def test_advisory_completions_from_one_source_are_not_called_independent(self):
        _, contract, plan = self._fixture()
        spec = self._diversity_spec("advisory")
        backend = SequenceBackend(
            [ZERO, ZERO, INVALID], ZERO, provider="provider-a", model="model-a"
        )
        with tempfile.TemporaryDirectory() as temp, patch(
            "rtl_closure.oracle.generate_expected_trace", side_effect=fake_trace
        ):
            _, _, metadata = generate_frozen_oracle(
                spec, backend, contract, plan, Path(temp)
            )
        self.assertFalse(metadata["diversity"]["satisfied"])
        self.assertEqual(metadata["diversity"]["distinct_source_count"], 1)
        self.assertEqual(metadata["diversity"]["required_minimum_floor"], 2)
        self.assertTrue(metadata["diversity"]["minimum_configuration_valid"])
        self.assertIn("not provider-independent", metadata["diversity"]["warning"])

    def test_single_candidate_cannot_be_rescued_by_reviewer(self):
        spec, contract, plan = self._fixture()
        backend = SequenceBackend([ZERO, INVALID, INVALID], ONE)
        with tempfile.TemporaryDirectory() as temp, patch(
            "rtl_closure.oracle.generate_expected_trace", side_effect=fake_trace
        ):
            with self.assertRaisesRegex(RuntimeError, "quorum"):
                generate_frozen_oracle(
                    spec, backend, contract, plan, Path(temp)
                )

    def test_two_candidate_consensus_survives_disagreeing_reviewer(self):
        spec, contract, plan = self._fixture()
        backend = SequenceBackend([ZERO, ZERO, INVALID], ONE)
        with tempfile.TemporaryDirectory() as temp, patch(
            "rtl_closure.oracle.generate_expected_trace", side_effect=fake_trace
        ):
            code, trace, metadata = generate_frozen_oracle(
                spec, backend, contract, plan, Path(temp)
            )
        self.assertEqual(code.strip(), ZERO.strip())
        self.assertEqual(trace["trace"][0]["expected"]["count"], 0)
        self.assertEqual(metadata["execution_quorum"], 2)
        self.assertFalse(metadata["reviewed"])

    def test_agreeing_reviewer_is_recorded_but_not_counted_in_quorum(self):
        spec, contract, plan = self._fixture()
        backend = SequenceBackend([ZERO, ZERO, INVALID], ZERO)
        with tempfile.TemporaryDirectory() as temp, patch(
            "rtl_closure.oracle.generate_expected_trace", side_effect=fake_trace
        ):
            _, _, metadata = generate_frozen_oracle(
                spec, backend, contract, plan, Path(temp)
            )
        self.assertTrue(metadata["reviewed"])
        self.assertEqual(metadata["execution_quorum"], 2)
        self.assertEqual(metadata["quorum_origin_ids"], ["candidate-01", "candidate-02"])

    def test_candidate_container_failure_is_not_demoted_to_invalid_oracle(self):
        spec, contract, plan = self._fixture()
        backend = SequenceBackend([ZERO, ZERO, INVALID], ZERO)
        failure = ContainerInfrastructureError("Docker unavailable", provider="oracle")
        with tempfile.TemporaryDirectory() as temp, patch(
            "rtl_closure.oracle.generate_expected_trace", side_effect=failure
        ):
            with self.assertRaises(ContainerInfrastructureError) as raised:
                generate_frozen_oracle(spec, backend, contract, plan, Path(temp))
        self.assertIs(raised.exception, failure)
        self.assertTrue(raised.exception.retryable)

    def test_reviewer_infrastructure_failure_is_not_hidden_as_review_error(self):
        spec, contract, plan = self._fixture()
        backend = SequenceBackend([ZERO, ZERO, INVALID], ZERO)
        failure = ContainerInfrastructureError("review service unavailable", provider="reviewer")

        class FailingReviewer:
            provider = "fixture-reviewer"
            model = "fixture-model"

            def generate(self, *_args, **_kwargs):
                raise failure

        with tempfile.TemporaryDirectory() as temp, patch(
            "rtl_closure.oracle.generate_expected_trace", side_effect=fake_trace
        ):
            with self.assertRaises(ContainerInfrastructureError) as raised:
                generate_frozen_oracle(
                    spec,
                    backend,
                    contract,
                    plan,
                    Path(temp),
                    reviewer_backend=FailingReviewer(),
                )
        self.assertIs(raised.exception, failure)
        self.assertTrue(raised.exception.retryable)

    def test_final_plan_revalidation_detects_post_refinement_disagreement(self):
        spec, _, plan = self._fixture()
        final_plan = json.loads(json.dumps(plan))
        final_plan["scenarios"].append(
            {
                "id": "TEST_ENABLE",
                "requirement_ids": ["REQ_COUNT"],
                "cycles": [{"drive": {"enable": 1}}],
            }
        )

        def final_trace(spec, code, active_plan, artifact_dir):
            del spec, artifact_dir
            rows = []
            for scenario_index, scenario in enumerate(active_plan["scenarios"]):
                enabled = int(scenario["cycles"][0].get("drive", {}).get("enable", 0))
                value = enabled if "inputs.get" in code else 0
                rows.append(
                    {
                        "ordinal": scenario_index,
                        "scenario_index": scenario_index,
                        "scenario_id": scenario["id"],
                        "cycle": 0,
                        "expected": {"count": value},
                    }
                )
            return {"trace": rows, "setup_observations": []}

        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            origins = []
            for index, code in enumerate((BRANCH, BRANCH, ZERO), 1):
                candidate = root / "candidates" / f"candidate-{index:02d}"
                candidate.mkdir(parents=True)
                response = wrapped(code)
                (candidate / "oracle.py").write_text(code, encoding="utf-8")
                (candidate / "response.txt").write_text(response, encoding="utf-8")
                origins.append(
                    {
                        "origin_id": f"candidate-{index:02d}",
                        "candidate_index": index,
                        "response_sha256": content_hash(response),
                        "oracle_sha256": content_hash(code),
                        "valid": True,
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
            with patch(
                "rtl_closure.oracle.generate_expected_trace", side_effect=final_trace
            ):
                with self.assertRaisesRegex(RuntimeError, "final test plan"):
                    revalidate_oracle_quorum(
                        spec,
                        final_plan,
                        ZERO,
                        {"candidate_origins": origins},
                        root / "candidates",
                        root / "final",
                    )
                trace, metadata = revalidate_oracle_quorum(
                    spec,
                    final_plan,
                    BRANCH,
                    {"candidate_origins": origins},
                    root / "candidates",
                    root / "final-pass",
                )
        self.assertEqual(trace["trace"][-1]["expected"]["count"], 1)
        self.assertEqual(metadata["consensus_count"], 2)
        self.assertFalse(metadata["reviewer_counted"])


if __name__ == "__main__":
    unittest.main()
