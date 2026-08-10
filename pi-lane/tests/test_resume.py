from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from pi_lane.classic import (
    CLASSIC_REGISTRY_VERSION,
    LIBRELANE_IMAGE,
    LIBRELANE_VERSION,
)
from pi_lane.config import capture_design_inputs, load_design_config
from pi_lane.resume import (
    build_resume_plan,
    file_sha256,
    link_chain_proof,
    state_attestation_sha256,
    validate_initial_state,
    verify_resume_chain,
)


class ResumeValidationTests(unittest.TestCase):
    IMAGE_DIGEST = "sha256:" + "a" * 64
    PDK_REVISION = "b" * 40
    PDK_INTEGRITY = "c" * 64

    def _fixture(self, root: Path):
        design = root / "design"
        design.mkdir()
        config_path = design / "config.yaml"
        config_path.write_text(
            "DESIGN_NAME: demo\nPDK: sky130A\nmeta:\n  flow: Classic\n",
            encoding="utf-8",
        )
        config = load_design_config(config_path)
        input_manifest, captured = capture_design_inputs(config)
        controller_dir = root / "controller"
        input_snapshot = controller_dir / "input-snapshot"
        input_snapshot.mkdir(parents=True)
        for relative, content in captured.items():
            destination = input_snapshot / relative
            destination.parent.mkdir(parents=True, exist_ok=True)
            destination.write_bytes(content)
        input_manifest_path = controller_dir / "input-manifest.json"
        input_manifest_path.write_text(json.dumps(input_manifest), encoding="utf-8")
        pdk_root = root / "pdk"
        pdk_root.mkdir()
        step_dir = design / "runs" / "PI_LANE_old" / "06-yosys-synthesis"
        step_dir.mkdir(parents=True)
        state_path = step_dir / "state_out.json"
        state_path.write_text(json.dumps({"netlist": ["demo.nl.v"]}), encoding="utf-8")
        source = {
            "schema_version": 6,
            "run_id": "old",
            "is_running": False,
            "process_status": "failed",
            "flow": "Classic",
            "flow_registry_version": CLASSIC_REGISTRY_VERSION,
            "config_sha256": config.sha256,
            "config_path": str(config_path.resolve()),
            "controller_dir": str(controller_dir.resolve()),
            "input_snapshot_dir": str(input_snapshot.resolve()),
            "input_manifest_path": str(input_manifest_path.resolve()),
            "input_bundle_sha256": input_manifest["bundle_sha256"],
            "pdk": "sky130A",
            "pdk_revision": self.PDK_REVISION,
            "pdk_integrity_sha256": self.PDK_INTEGRITY,
            "image": LIBRELANE_IMAGE,
            "image_digest": self.IMAGE_DIGEST,
            "librelane_version": LIBRELANE_VERSION,
            "design_dir": str(design.resolve()),
            "librelane_run_dir": str((design / "runs" / "PI_LANE_old").resolve()),
            "pdk_root": str(pdk_root.resolve()),
            "overrides": [],
            "jobs": 4,
            "run_eqy_requested": False,
            "from": None,
            "to": None,
            "initial_state": None,
            "initial_state_sha256": None,
            "parent_run_id": None,
            "resume": None,
            "last_completed_step": "Yosys.Synthesis",
            "artifacts": {"latest_state": str(state_path.resolve())},
            "step_history": [
                {
                    "ordinal": 6,
                    "id": "Yosys.Synthesis",
                    "status": "completed",
                    "state_out": str(state_path.resolve()),
                }
            ],
        }
        return config, state_path, source

    def _plan(self, source: dict, config, **kwargs: object):
        return build_resume_plan(
            source,
            config,
            current_image_digest=self.IMAGE_DIGEST,
            current_pdk_revision=self.PDK_REVISION,
            current_pdk_integrity_sha256=self.PDK_INTEGRITY,
            **kwargs,
        )

    def test_resume_is_bound_to_the_immediate_next_step_and_state_hash(self):
        with tempfile.TemporaryDirectory() as temp:
            config, state_path, source = self._fixture(Path(temp))
            plan = self._plan(source, config, requested_to="OpenROAD.CTS")
            self.assertEqual(plan.parent_run_id, "old")
            self.assertEqual(plan.root_run_id, "old")
            self.assertEqual(plan.chain_depth, 1)
            self.assertEqual(plan.frm, "Checker.YosysUnmappedCells")
            self.assertEqual(plan.to, "OpenROAD.CTS")
            self.assertEqual(plan.initial_state, state_path.resolve())
            self.assertEqual(plan.initial_state_sha256, file_sha256(state_path))
            self.assertEqual(plan.parent_state_attestation_sha256, state_attestation_sha256(source))

    def test_resume_rejects_step_skips_and_changed_execution_inputs(self):
        with tempfile.TemporaryDirectory() as temp:
            config, _, source = self._fixture(Path(temp))
            with self.assertRaisesRegex(ValueError, "immediate Step"):
                self._plan(source, config, requested_from="OpenROAD.Floorplan")
            with self.assertRaisesRegex(ValueError, "image digest changed"):
                build_resume_plan(
                    source,
                    config,
                    current_image_digest="sha256:" + "f" * 64,
                    current_pdk_revision=self.PDK_REVISION,
                    current_pdk_integrity_sha256=self.PDK_INTEGRITY,
                )
            with self.assertRaisesRegex(ValueError, "PDK revision changed"):
                build_resume_plan(
                    source,
                    config,
                    current_image_digest=self.IMAGE_DIGEST,
                    current_pdk_revision="different",
                    current_pdk_integrity_sha256=self.PDK_INTEGRITY,
                )
            with self.assertRaisesRegex(ValueError, "PDK content integrity changed"):
                build_resume_plan(
                    source,
                    config,
                    current_image_digest=self.IMAGE_DIGEST,
                    current_pdk_revision=self.PDK_REVISION,
                    current_pdk_integrity_sha256="d" * 64,
                )
            source["config_sha256"] = "changed"
            with self.assertRaisesRegex(ValueError, "configuration changed"):
                self._plan(source, config)

    def test_resume_rejects_registry_image_and_unproven_midflow_parent(self):
        with tempfile.TemporaryDirectory() as temp:
            config, _, source = self._fixture(Path(temp))
            source["flow_registry_version"] = None
            with self.assertRaisesRegex(ValueError, "registry"):
                self._plan(source, config)
            source["flow_registry_version"] = CLASSIC_REGISTRY_VERSION
            source["image"] = "ghcr.io/librelane/librelane:latest"
            with self.assertRaisesRegex(ValueError, "pinned LibreLane"):
                self._plan(source, config)
            source["image"] = LIBRELANE_IMAGE
            source["from"] = "Yosys.Synthesis"
            source["initial_state"] = "/untrusted/state_out.json"
            with self.assertRaisesRegex(ValueError, "verifiable parent link"):
                self._plan(source, config)

    def test_schema5_resume_proof_binds_and_revalidates_full_input_bundle(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            config, _, source = self._fixture(root)
            manifest, captured = capture_design_inputs(config)
            controller = root / "state" / "old"
            snapshot = controller / "input-snapshot"
            snapshot.mkdir(parents=True)
            for relative, content in captured.items():
                destination = snapshot / relative
                destination.parent.mkdir(parents=True, exist_ok=True)
                destination.write_bytes(content)
            manifest_path = controller / "input-manifest.json"
            manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
            source.update(
                {
                    "schema_version": 6,
                    "controller_dir": str(controller),
                    "input_snapshot_dir": str(snapshot),
                    "input_manifest_path": str(manifest_path),
                    "input_bundle_sha256": manifest["bundle_sha256"],
                }
            )

            before = state_attestation_sha256(source)
            self.assertTrue(verify_resume_chain(source, None)["verified"])
            source["input_bundle_sha256"] = "f" * 64

            self.assertNotEqual(state_attestation_sha256(source), before)
            with self.assertRaisesRegex(ValueError, "bundle identity mismatch"):
                verify_resume_chain(source, None)

    def test_recursive_chain_hashes_parent_and_detects_parent_mutation(self):
        with tempfile.TemporaryDirectory() as temp:
            config, root_state_path, root = self._fixture(Path(temp))
            plan = self._plan(root, config)
            child_step = (
                config.design_dir
                / "runs"
                / "PI_LANE_child"
                / "07-checker-yosysunmappedcells"
            )
            child_step.mkdir(parents=True)
            child_state_path = child_step / "state_out.json"
            child_state_path.write_text('{"netlist":["demo.checked.nl.v"]}')
            child = {
                **root,
                "run_id": "child",
                "librelane_run_dir": str(child_step.parent.resolve()),
                "from": plan.frm,
                "initial_state": str(root_state_path.resolve()),
                "initial_state_sha256": plan.initial_state_sha256,
                "parent_run_id": "old",
                "last_completed_step": "Checker.YosysUnmappedCells",
                "artifacts": {"latest_state": str(child_state_path.resolve())},
                "step_history": [
                    {
                        "ordinal": 7,
                        "id": "Checker.YosysUnmappedCells",
                        "status": "completed",
                        "state_out": str(child_state_path.resolve()),
                    }
                ],
            }
            link = link_chain_proof(
                parent_chain_proof_sha256=plan.parent_chain_proof_sha256,
                parent_state_attestation_sha256=plan.parent_state_attestation_sha256,
                parent_run_id="old",
                run_id="child",
                root_run_id="old",
                chain_depth=1,
                frm=plan.frm,
                initial_state_sha256=plan.initial_state_sha256,
                input_bundle_sha256=child.get("input_bundle_sha256"),
            )
            child["resume"] = {
                "parent_run_id": "old",
                "root_run_id": "old",
                "chain_depth": 1,
                "from": plan.frm,
                "initial_state": str(root_state_path.resolve()),
                "initial_state_sha256": plan.initial_state_sha256,
                "input_bundle_sha256": child.get("input_bundle_sha256"),
                "parent_state_attestation_sha256": plan.parent_state_attestation_sha256,
                "parent_chain_proof_sha256": plan.parent_chain_proof_sha256,
                "chain_proof_sha256": link,
            }
            states = {"old": root, "child": child}
            verified = verify_resume_chain(child, states.__getitem__)
            self.assertTrue(verified["verified"])
            self.assertEqual(verified["root_run_id"], "old")
            self.assertEqual(verified["chain_depth"], 1)

            second_plan = self._plan(child, config, state_loader=states.__getitem__)
            self.assertEqual(second_plan.chain_depth, 2)
            root_state_path.write_text('{"netlist":["tampered.nl.v"]}')
            with self.assertRaisesRegex(ValueError, "attestation changed"):
                verify_resume_chain(child, states.__getitem__)

    def test_compressed_observed_ordinals_are_allowed_but_order_tampering_is_not(self):
        with tempfile.TemporaryDirectory() as temp:
            config, _, source = self._fixture(Path(temp))
            source["step_history"][0]["ordinal"] = 42
            self.assertEqual(self._plan(source, config).chain_depth, 1)
            source["step_history"].insert(
                0,
                {
                    "ordinal": 43,
                    "id": "Verilator.Lint",
                    "status": "completed",
                    "state_out": source["artifacts"]["latest_state"],
                },
            )
            with self.assertRaisesRegex(ValueError, "strictly increasing"):
                self._plan(source, config)

    def test_initial_state_must_be_a_real_snapshot_inside_the_design(self):
        with tempfile.TemporaryDirectory() as temp, tempfile.TemporaryDirectory() as other:
            design = Path(temp)
            external = Path(other) / "state_out.json"
            external.write_text('{"def":"demo.def"}', encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "inside the mounted design"):
                validate_initial_state(external, design)
            empty = design / "state_out.json"
            empty.write_text("{}", encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "non-empty"):
                validate_initial_state(empty, design)
            empty.unlink()
            real = design / "real.json"
            real.write_text('{"def":"demo.def"}')
            linked = design / "state_out.json"
            linked.symlink_to(real)
            with self.assertRaisesRegex(ValueError, "canonical regular"):
                validate_initial_state(linked, design)

    def test_checkpoint_cannot_be_substituted_from_another_run(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            config, _, source = self._fixture(root)
            other_step = (
                root
                / "design"
                / "runs"
                / "PI_LANE_other"
                / "06-yosys-synthesis"
            )
            other_step.mkdir(parents=True)
            other_state = other_step / "state_out.json"
            other_state.write_text('{"netlist":["other.nl.v"]}', encoding="utf-8")
            source["artifacts"]["latest_state"] = str(other_state)
            source["step_history"][0]["state_out"] = str(other_state)

            with self.assertRaisesRegex(ValueError, "inside the mounted design"):
                self._plan(source, config)


if __name__ == "__main__":
    unittest.main()
