from __future__ import annotations

import json
import subprocess
import tempfile
import unittest
from pathlib import Path

from pi_lane.classic import CLASSIC_STEP_IDS
from pi_lane.config import load_design_config
from pi_lane.preflight import PREFLIGHT_PREFIX, build_preflight_command, validate_config_in_image


class PreflightTests(unittest.TestCase):
    def make_config(self, root: Path):
        config = root / "config.yaml"
        rtl = root / "demo.sv"
        rtl.write_text("module demo(input clk); endmodule\n", encoding="utf-8")
        config.write_text(
            "DESIGN_NAME: demo\nPDK: sky130A\nVERILOG_FILES: [dir::demo.sv]\nmeta:\n  flow: Classic\n",
            encoding="utf-8",
        )
        return load_design_config(config)

    def test_command_uses_real_cwd_read_only_mounts_and_no_network(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            config = self.make_config(root)
            command = build_preflight_command(
                config,
                image="librelane:test",
                runtime_image_ref="sha256:" + "d" * 64,
                pdk_root=root / "pdk",
                overrides=("FP_CORE_UTIL=40",),
            )
            self.assertIn("none", command)
            self.assertIn("--read-only", command)
            self.assertIn(f"{root}:{root}:ro", command)
            self.assertIn(str(root), command)
            self.assertIn("sha256:" + "d" * 64, command)
            self.assertNotIn("librelane:test", command)
            self.assertIn("io.pi-lane.image-tag=librelane:test", command)
            self.assertIn(
                "io.pi-lane.image-digest=sha256:" + "d" * 64,
                command,
            )
            self.assertIn("Flow.factory.get(\"Classic\")", command[-1])
            self.assertIn("FP_CORE_UTIL=40", command[-1])

    def test_command_and_attestation_use_the_immutable_input_snapshot(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            config = self.make_config(root)
            snapshot = root / "snapshot"
            snapshot.mkdir()
            (snapshot / "config.yaml").write_bytes(config.source_bytes)
            (snapshot / "demo.sv").write_text(
                "module demo(input clk); endmodule\n", encoding="utf-8"
            )
            command = build_preflight_command(
                config,
                image="librelane:test",
                runtime_image_ref="sha256:" + "d" * 64,
                pdk_root=root / "pdk",
                input_snapshot_dir=snapshot,
            )
            self.assertIn(f"{snapshot}:{root}:ro", command)

            body = {
                "valid": True,
                "flow": "Classic",
                "design_name": "demo",
                "pdk": "sky130A",
                "step_count": len(CLASSIC_STEP_IDS),
                "step_ids": list(CLASSIC_STEP_IDS),
                "first_step": CLASSIC_STEP_IDS[0],
                "last_step": CLASSIC_STEP_IDS[-1],
            }

            def runner(*_args, **_kwargs):
                return subprocess.CompletedProcess(
                    [], 0, PREFLIGHT_PREFIX + json.dumps(body) + "\n", ""
                )

            config.path.write_text("changed after capture\n", encoding="utf-8")
            report = validate_config_in_image(
                config,
                image="librelane:test",
                image_digest="sha256:" + "d" * 64,
                pdk_root=root / "pdk",
                input_snapshot_dir=snapshot,
                runner=runner,
            )
            self.assertEqual(report["config_sha256"], config.sha256)
            self.assertEqual(report["input_snapshot_dir"], str(snapshot))
            self.assertEqual(report["runtime_image_ref"], "sha256:" + "d" * 64)

    def test_structured_verdict_is_attested(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            config = self.make_config(root)
            body = {
                "valid": True,
                "flow": "Classic",
                "design_name": "demo",
                "pdk": "sky130A",
                "step_count": len(CLASSIC_STEP_IDS),
                "step_ids": list(CLASSIC_STEP_IDS),
                "first_step": CLASSIC_STEP_IDS[0],
                "last_step": CLASSIC_STEP_IDS[-1],
            }

            def runner(*_args, **_kwargs):
                return subprocess.CompletedProcess([], 0, PREFLIGHT_PREFIX + json.dumps(body) + "\n", "")

            report = validate_config_in_image(
                config,
                image="librelane:test",
                image_digest="sha256:" + "d" * 64,
                pdk_root=root / "pdk",
                runner=runner,
            )
            self.assertEqual(report["status"], "pass")
            self.assertEqual(report["step_count"], len(CLASSIC_STEP_IDS))

    def test_container_error_fails_before_run(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            config = self.make_config(root)

            def runner(*_args, **_kwargs):
                return subprocess.CompletedProcess([], 1, "", "TypeError: unexpected meta key")

            with self.assertRaisesRegex(RuntimeError, "unexpected meta key"):
                validate_config_in_image(
                    config,
                    image="librelane:test",
                    image_digest="sha256:" + "d" * 64,
                    pdk_root=root / "pdk",
                    runner=runner,
                )


if __name__ == "__main__":
    unittest.main()
