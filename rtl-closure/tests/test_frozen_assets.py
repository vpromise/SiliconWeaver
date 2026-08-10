from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from rtl_closure.contract import content_hash
from rtl_closure.repair import verify_frozen_assets


class FrozenAssetTests(unittest.TestCase):
    def test_manifest_is_the_baseline_and_expected_trace_cannot_change(self):
        contract = {"schema_version": 1, "requirements": [{"id": "REQ_A"}]}
        plan = {"schema_version": 1, "scenarios": [{"id": "TEST_A"}]}
        expected = {"trace": [{"expected": {"y": 1}}]}
        oracle = "class TopModule:\n    def eval(self, inputs): return {'y': 1}\n"
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            contract_path = root / "contract.json"
            plan_path = root / "testplan.json"
            expected_path = root / "expected-trace.json"
            oracle_path = root / "reference_model.py"
            manifest_path = root / "frozen-manifest.json"
            contract_path.write_text(json.dumps(contract), encoding="utf-8")
            plan_path.write_text(json.dumps(plan), encoding="utf-8")
            expected_path.write_text(json.dumps(expected), encoding="utf-8")
            oracle_path.write_text(oracle, encoding="utf-8")
            manifest = {
                "schema_version": 1,
                "contract_sha256": content_hash(contract),
                "testplan_sha256": content_hash(plan),
                "oracle_sha256": content_hash(oracle),
                "expected_trace_sha256": content_hash(expected),
            }
            manifest["bundle_sha256"] = content_hash(manifest)
            manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
            arguments = {
                "contract": contract,
                "plan": plan,
                "expected_trace": expected,
                "contract_path": contract_path,
                "plan_path": plan_path,
                "oracle_path": oracle_path,
                "expected_path": expected_path,
                "manifest_path": manifest_path,
                "frozen_manifest": manifest,
            }
            verify_frozen_assets(**arguments)
            expected_path.write_text(
                json.dumps({"trace": [{"expected": {"y": 0}}]}),
                encoding="utf-8",
            )
            with self.assertRaisesRegex(RuntimeError, "expected-trace.json"):
                verify_frozen_assets(**arguments)

    def test_oracle_hash_is_byte_exact(self):
        contract = {"schema_version": 1}
        plan = {"schema_version": 1}
        expected = {"trace": []}
        oracle = "class TopModule: pass"
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            paths = {
                "contract_path": root / "contract.json",
                "plan_path": root / "testplan.json",
                "expected_path": root / "expected-trace.json",
                "oracle_path": root / "reference_model.py",
                "manifest_path": root / "frozen-manifest.json",
            }
            paths["contract_path"].write_text(json.dumps(contract), encoding="utf-8")
            paths["plan_path"].write_text(json.dumps(plan), encoding="utf-8")
            paths["expected_path"].write_text(json.dumps(expected), encoding="utf-8")
            paths["oracle_path"].write_text(oracle, encoding="utf-8")
            manifest = {
                "schema_version": 1,
                "contract_sha256": content_hash(contract),
                "testplan_sha256": content_hash(plan),
                "oracle_sha256": content_hash(oracle),
                "expected_trace_sha256": content_hash(expected),
            }
            manifest["bundle_sha256"] = content_hash(manifest)
            paths["manifest_path"].write_text(json.dumps(manifest), encoding="utf-8")
            paths["oracle_path"].write_text(oracle + "\n", encoding="utf-8")
            with self.assertRaisesRegex(RuntimeError, "Python oracle changed"):
                verify_frozen_assets(
                    contract=contract,
                    plan=plan,
                    expected_trace=expected,
                    frozen_manifest=manifest,
                    **paths,
                )


if __name__ == "__main__":
    unittest.main()
