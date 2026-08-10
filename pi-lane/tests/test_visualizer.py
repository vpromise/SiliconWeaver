from __future__ import annotations

import contextlib
import io
import json
import struct
import subprocess
import sys
import tempfile
import types
import unittest
import zlib
from argparse import Namespace
from pathlib import Path
from unittest.mock import patch

from pi_lane.cli import build_parser as build_cli_parser
from pi_lane.controller import discover_visualization
from pi_lane.integrity import file_sha256
from pi_lane.visualizer import (
    DEFAULT_RENDER_TIMEOUT_SECONDS,
    RENDER_SCRIPT,
    build_parser,
    run_visualizer,
)


def png_bytes() -> bytes:
    def chunk(kind: bytes, payload: bytes) -> bytes:
        return (
            struct.pack(">I", len(payload))
            + kind
            + payload
            + struct.pack(">I", zlib.crc32(kind + payload) & 0xFFFFFFFF)
        )

    ihdr = struct.pack(">IIBBBBB", 1, 1, 8, 2, 0, 0, 0)
    return (
        b"\x89PNG\r\n\x1a\n"
        + chunk(b"IHDR", ihdr)
        + chunk(b"IDAT", zlib.compress(b"\x00\x00\x00\x00"))
        + chunk(b"IEND", b"")
    )


class VisualizerFixture(unittest.TestCase):
    def make_args(
        self,
        root: Path,
        *,
        run_id: str,
        retry_failed: bool = False,
        main_complete: bool = True,
    ) -> Namespace:
        design_dir = root / "design"
        design_dir.mkdir(exist_ok=True)
        config_path = design_dir / "config.yaml"
        config_path.write_text("DESIGN_NAME: demo\nPDK: sky130A\n", encoding="utf-8")
        input_snapshot_dir = root / "input-snapshot"
        input_snapshot_dir.mkdir(exist_ok=True)
        (input_snapshot_dir / "config.yaml").write_bytes(config_path.read_bytes())
        pdk_root = root / "pdk"
        pdk_root.mkdir(exist_ok=True)
        main_exit = root / "main-exit"
        if main_complete:
            main_exit.write_text("0\n", encoding="utf-8")
        return Namespace(
            run_id=run_id,
            run_dir=design_dir / "runs" / f"PI_LANE_{run_id}",
            design_dir=design_dir,
            design_name="demo",
            config=config_path,
            config_sha256=file_sha256(config_path),
            input_snapshot_dir=input_snapshot_dir,
            input_bundle_sha256="e" * 64,
            pdk="sky130A",
            pdk_revision="b" * 40,
            pdk_integrity_sha256="c" * 64,
            pdk_root=pdk_root,
            image="ghcr.io/librelane/librelane:3.0.5",
            image_digest="sha256:" + "d" * 64,
            runtime_image_ref="sha256:" + "d" * 64,
            librelane_version="3.0.5",
            main_exit_file=main_exit,
            main_pid=4321,
            main_pid_start_time=9876,
            exit_file=root / "visualizer-exit",
            overrides=[],
            poll_interval=0.0,
            render_timeout_seconds=12.5,
            retry_failed=retry_failed,
        )

    def make_step(self, run_dir: Path, ordinal: int, step_id: str) -> Path:
        step_dir = run_dir / f"{ordinal:02d}-{step_id.lower().replace('.', '-')}"
        step_dir.mkdir(parents=True)
        (step_dir / "config.json").write_text(
            json.dumps({"meta": {"step": step_id}}), encoding="utf-8"
        )
        (step_dir / "state_in.json").write_text('{"def":null}', encoding="utf-8")
        (step_dir / "state_out.json").write_text(
            json.dumps({"def": str(step_dir / "demo.def")}), encoding="utf-8"
        )
        return step_dir

    def identity(self, args: Namespace) -> dict[str, object]:
        return {
            "run_id": args.run_id,
            "run_dir": str(args.run_dir.resolve()),
            "design_dir": str(args.design_dir.resolve()),
            "design_name": args.design_name,
            "config_path": str(args.config.resolve()),
            "config_sha256": args.config_sha256,
            "input_snapshot_dir": str(args.input_snapshot_dir.resolve()),
            "input_bundle_sha256": args.input_bundle_sha256,
            "pdk": args.pdk,
            "pdk_revision": args.pdk_revision,
            "pdk_integrity_sha256": args.pdk_integrity_sha256,
            "pdk_root": str(args.pdk_root.resolve()),
            "image": args.image,
            "image_digest": args.image_digest,
            "runtime_image_ref": args.runtime_image_ref,
            "librelane_version": args.librelane_version,
            "main_pid": args.main_pid,
            "main_pid_start_time": args.main_pid_start_time,
            "overrides": [],
        }

    def write_manifest(
        self, args: Namespace, records: list[dict[str, object]]
    ) -> tuple[Path, Path]:
        visual = args.run_dir / "visualizations"
        visual.mkdir()
        manifest = visual / "manifest.json"
        timeline = visual / "timeline.jsonl"
        manifest.write_text(
            json.dumps(
                {
                    "schema_version": 3,
                    **self.identity(args),
                    "status": "completed_with_errors",
                    "steps": records,
                    "errors": ["old aggregate error"],
                }
            ),
            encoding="utf-8",
        )
        timeline.write_text('{"event":"original"}\n', encoding="utf-8")
        return manifest, timeline

    @staticmethod
    def record(
        ordinal: int,
        step_id: str,
        *,
        visualization_status: str,
        image: str | None = None,
        directory: str | None = None,
        error: str | None = None,
    ) -> dict[str, object]:
        return {
            "ordinal": ordinal,
            "id": step_id,
            "preview_kind": "render",
            "visualization_status": visualization_status,
            "visualization_directory": directory,
            "image": image,
            "visualization_error": error,
        }


class VisualizerTimeoutTests(VisualizerFixture):
    def test_render_script_loads_the_full_classic_flow_configuration(self):
        calls: dict[str, object] = {}
        full_config = object()

        class FakeFlow:
            def __init__(self, config_path: str, **kwargs: object):
                calls["flow_init"] = (config_path, kwargs)
                self.config = full_config

        class FakeFlowFactory:
            @staticmethod
            def get(name: str) -> type[FakeFlow] | None:
                calls["flow_name"] = name
                return FakeFlow

        class FakeLoadedStep:
            def start(self, *, step_dir: str) -> None:
                calls["step_dir"] = step_dir

        class FakeTarget:
            @staticmethod
            def load(
                config: object, state_path: str, **kwargs: object
            ) -> FakeLoadedStep:
                calls["step_load"] = (config, state_path, kwargs)
                return FakeLoadedStep()

        class FakeStepFactory:
            @staticmethod
            def get(name: str) -> type[FakeTarget] | None:
                calls["step_name"] = name
                return FakeTarget

        librelane = types.ModuleType("librelane")
        librelane.__path__ = []  # type: ignore[attr-defined]
        flows = types.ModuleType("librelane.flows")
        flows.Flow = types.SimpleNamespace(factory=FakeFlowFactory())
        steps = types.ModuleType("librelane.steps")
        steps.Step = types.SimpleNamespace(factory=FakeStepFactory())
        modules = {
            "librelane": librelane,
            "librelane.flows": flows,
            "librelane.steps": steps,
        }
        argv = [
            "render-worker",
            "/pi-lane/design/config.yaml",
            "/host/run/state_out.json",
            "/pdk",
            "sky130A",
            "/pi-lane/visualizations/13-floorplan",
            '["RUN_EQY=true"]',
        ]
        with patch.dict(sys.modules, modules), patch.object(sys, "argv", argv):
            exec(RENDER_SCRIPT, {})

        self.assertEqual(calls["flow_name"], "Classic")
        self.assertEqual(calls["step_name"], "KLayout.Render")
        config_path, flow_kwargs = calls["flow_init"]
        self.assertEqual(config_path, argv[1])
        self.assertEqual(flow_kwargs["design_dir"], "/pi-lane/design")
        self.assertEqual(flow_kwargs["config_override_strings"], ["RUN_EQY=true"])
        loaded_config, state_path, load_kwargs = calls["step_load"]
        self.assertIs(loaded_config, full_config)
        self.assertEqual(state_path, argv[2])
        self.assertEqual(load_kwargs, {"pdk_root": "/pdk"})
        self.assertEqual(calls["step_dir"], argv[5])

    def required_arguments(self, root: Path) -> list[str]:
        return [
            "--run-id",
            "run",
            "--run-dir",
            str(root / "design" / "runs" / "PI_LANE_run"),
            "--design-dir",
            str(root / "design"),
            "--design-name",
            "demo",
            "--config",
            str(root / "design" / "config.yaml"),
            "--input-snapshot-dir",
            str(root / "input-snapshot"),
            "--input-bundle-sha256",
            "e" * 64,
            "--config-sha256",
            "a" * 64,
            "--pdk",
            "sky130A",
            "--pdk-revision",
            "b" * 40,
            "--pdk-integrity-sha256",
            "c" * 64,
            "--pdk-root",
            str(root / "pdk"),
            "--image",
            "ghcr.io/librelane/librelane:3.0.5",
            "--runtime-image-ref",
            "sha256:" + "d" * 64,
            "--image-digest",
            "sha256:" + "d" * 64,
            "--librelane-version",
            "3.0.5",
            "--main-exit-file",
            str(root / "main-exit"),
            "--main-pid",
            "4321",
            "--main-pid-start-time",
            "9876",
            "--exit-file",
            str(root / "visualizer-exit"),
        ]

    def test_parser_accepts_a_bounded_render_timeout(self):
        with tempfile.TemporaryDirectory() as temp:
            arguments = self.required_arguments(Path(temp))
            self.assertEqual(
                build_parser().parse_args(arguments).render_timeout_seconds,
                DEFAULT_RENDER_TIMEOUT_SECONDS,
            )
            self.assertEqual(
                build_parser()
                .parse_args(arguments + ["--render-timeout-seconds", "12.5"])
                .render_timeout_seconds,
                12.5,
            )
            for invalid in ("0", "9.9", "3600.1", "nan", "not-a-number"):
                with (
                    self.subTest(invalid=invalid),
                    contextlib.redirect_stderr(io.StringIO()),
                    self.assertRaises(SystemExit),
                ):
                    build_parser().parse_args(
                        arguments + ["--render-timeout-seconds", invalid]
                    )

    def test_retry_flags_are_exposed_by_worker_and_lane_agent(self):
        with tempfile.TemporaryDirectory() as temp:
            arguments = self.required_arguments(Path(temp))
            self.assertTrue(
                build_parser().parse_args(arguments + ["--retry-failed"]).retry_failed
            )
        self.assertTrue(
            build_cli_parser()
            .parse_args(["visualize", "--run-id", "run", "--retry-failed"])
            .retry_failed
        )
        with contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit):
            build_cli_parser().parse_args(["status", "--legacy-exit-status"])

    def test_timeout_is_recorded_and_named_container_is_removed(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            args = self.make_args(root, run_id="timeout-run")
            self.make_step(args.run_dir, 1, "OpenROAD.Floorplan")
            calls: list[list[str]] = []

            def fake_run(
                command: list[str], **kwargs: object
            ) -> subprocess.CompletedProcess:
                calls.append(command)
                if command[:2] == ["docker", "run"]:
                    self.assertEqual(kwargs["timeout"], 12.5)
                    raise subprocess.TimeoutExpired(command, 12.5)
                return subprocess.CompletedProcess(command, 0)

            with patch("pi_lane.visualizer.subprocess.run", side_effect=fake_run):
                self.assertEqual(run_visualizer(args), 1)
            container = "pi-lane-viz-timeout-run-1"
            self.assertIn(["docker", "stop", "--time", "5", container], calls)
            self.assertIn(["docker", "rm", "-f", container], calls)
            visual = args.run_dir / "visualizations"
            manifest = json.loads((visual / "manifest.json").read_text())
            self.assertEqual(manifest["schema_version"], 3)
            self.assertEqual(manifest["status"], "completed_with_errors")
            self.assertEqual(manifest["config_sha256"], args.config_sha256)
            self.assertIn("timed out after 12.5 seconds", manifest["errors"][0])
            self.assertFalse((visual / "current_container").exists())

    def test_run_identity_and_config_hash_are_fail_closed(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            args = self.make_args(root, run_id="identity")
            args.run_dir.mkdir(parents=True)
            args.run_dir = root / "other-run"
            with contextlib.redirect_stdout(io.StringIO()):
                self.assertEqual(run_visualizer(args), 1)

            args = self.make_args(root, run_id="identity")
            args.run_dir.mkdir(parents=True, exist_ok=True)
            (args.input_snapshot_dir / "config.yaml").write_text("changed\n")
            with contextlib.redirect_stdout(io.StringIO()):
                self.assertEqual(run_visualizer(args), 1)

    def test_writer_schema_v3_is_readable_and_every_identity_field_is_bound(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            args = self.make_args(root, run_id="writer-reader")
            args.run_dir.mkdir(parents=True)
            self.assertEqual(run_visualizer(args), 0)
            manifest_path = args.run_dir / "visualizations" / "manifest.json"
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            identity = self.identity(args)
            state = {
                "run_id": args.run_id,
                "librelane_run_dir": identity["run_dir"],
                "visualization_dir": str(args.run_dir / "visualizations"),
                "design_dir": identity["design_dir"],
                "design_name": args.design_name,
                "config_path": identity["config_path"],
                "config_sha256": args.config_sha256,
                "input_snapshot_dir": identity["input_snapshot_dir"],
                "input_bundle_sha256": args.input_bundle_sha256,
                "pdk": args.pdk,
                "pdk_revision": args.pdk_revision,
                "pdk_integrity_sha256": args.pdk_integrity_sha256,
                "pdk_root": identity["pdk_root"],
                "image": args.image,
                "image_digest": args.image_digest,
                "runtime_image_ref": args.runtime_image_ref,
                "librelane_version": args.librelane_version,
                "pid": args.main_pid,
                "pid_start_time": args.main_pid_start_time,
                "overrides": [],
                "status": "completed",
                "visualizer_pid": None,
                "visualizer_pid_start_time": None,
                "visualizer_launch_attempted": True,
                "visualizer_exit_file": str(args.exit_file),
            }
            accepted = discover_visualization(state)
            self.assertEqual(accepted["status"], "completed")
            self.assertEqual(accepted["errors"], [])

            immutable_fields = tuple(identity)
            for field in immutable_fields:
                tampered = dict(manifest)
                value = tampered[field]
                if isinstance(value, list):
                    tampered[field] = [*value, "TAMPERED=1"]
                elif isinstance(value, int):
                    tampered[field] = value + 1
                else:
                    tampered[field] = str(value) + ".tampered"
                manifest_path.write_text(json.dumps(tampered), encoding="utf-8")
                with self.subTest(field=field):
                    rejected = discover_visualization(state)
                    self.assertEqual(rejected["status"], "failed")
                    self.assertIn(field, "\n".join(rejected["errors"]))
            manifest_path.write_text(json.dumps(manifest), encoding="utf-8")

    def test_sidecar_fails_when_main_pid_dies_without_an_exit_record(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            args = self.make_args(
                root,
                run_id="orphan-sidecar",
                main_complete=False,
            )
            args.run_dir.mkdir(parents=True)
            with patch("pi_lane.visualizer.pid_identity_alive", return_value=False):
                self.assertEqual(run_visualizer(args), 1)
            manifest = json.loads(
                (args.run_dir / "visualizations" / "manifest.json").read_text(
                    encoding="utf-8"
                )
            )
            self.assertEqual(manifest["status"], "failed")
            self.assertIn("PID was reused", "\n".join(manifest["errors"]))


class VisualizerRetryTests(VisualizerFixture):
    def test_retry_rejects_schema_two_without_rewriting(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            args = self.make_args(root, run_id="retry-run", retry_failed=True)
            args.run_dir.mkdir(parents=True)
            manifest, timeline = self.write_manifest(args, [])
            body = json.loads(manifest.read_text(encoding="utf-8"))
            body["schema_version"] = 2
            manifest.write_text(json.dumps(body), encoding="utf-8")
            before = manifest.read_bytes(), timeline.read_bytes()

            with contextlib.redirect_stdout(io.StringIO()):
                self.assertEqual(run_visualizer(args), 1)

            self.assertEqual((manifest.read_bytes(), timeline.read_bytes()), before)

    def test_retry_skips_valid_success_and_clears_recovered_failure(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            args = self.make_args(root, run_id="retry-run", retry_failed=True)
            first = self.make_step(args.run_dir, 1, "OpenROAD.Floorplan")
            self.make_step(args.run_dir, 2, "OpenROAD.GlobalPlacement")
            visual = args.run_dir / "visualizations"
            visual.mkdir()
            first_output = visual / first.name
            first_output.mkdir()
            first_image = first_output / "existing.png"
            first_image.write_bytes(png_bytes())
            records = [
                self.record(
                    1,
                    "OpenROAD.Floorplan",
                    visualization_status="completed",
                    image=str(first_image),
                    directory=str(first_output),
                ),
                self.record(
                    2,
                    "OpenROAD.GlobalPlacement",
                    visualization_status="failed",
                    error="first render failed",
                ),
            ]
            manifest = visual / "manifest.json"
            timeline = visual / "timeline.jsonl"
            manifest.write_text(
                json.dumps(
                    {
                        "schema_version": 3,
                        **self.identity(args),
                        "status": "completed_with_errors",
                        "steps": records,
                        "errors": ["old"],
                    }
                )
            )
            timeline.write_text('{"event":"original"}\n')
            rendered: list[int] = []

            def recover(record: dict[str, object], **kwargs: object) -> None:
                rendered.append(int(record["ordinal"]))
                output = (
                    Path(kwargs["visualization_root"])
                    / Path(str(record["directory"])).name
                )
                output.mkdir(exist_ok=True)
                image = output / "recovered.png"
                image.write_bytes(png_bytes())
                record["visualization_directory"] = str(output)
                record["image"] = str(image)
                record["visualization_status"] = "completed"

            with patch("pi_lane.visualizer._render_step", side_effect=recover):
                self.assertEqual(run_visualizer(args), 0)
            self.assertEqual(rendered, [2])
            result = json.loads(manifest.read_text())
            self.assertEqual(result["status"], "completed")
            self.assertEqual(result["errors"], [])
            self.assertEqual(
                result["steps"][1]["previous_error"], "first render failed"
            )

    def test_repeated_retry_failures_preserve_attempt_history(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            args = self.make_args(root, run_id="retry-run", retry_failed=True)
            self.make_step(args.run_dir, 1, "OpenROAD.Floorplan")
            manifest, timeline = self.write_manifest(
                args,
                [
                    self.record(
                        1,
                        "OpenROAD.Floorplan",
                        visualization_status="failed",
                        error="original failure",
                    )
                ],
            )
            errors = iter((RuntimeError("retry one"), RuntimeError("retry two")))
            with patch("pi_lane.visualizer._render_step", side_effect=errors):
                self.assertEqual(run_visualizer(args), 1)
                self.assertEqual(run_visualizer(args), 1)
            record = json.loads(manifest.read_text())["steps"][0]
            self.assertEqual(
                [item["error"] for item in record["attempts"]],
                ["retry one", "retry two"],
            )
            events = [
                json.loads(line)["event"] for line in timeline.read_text().splitlines()
            ]
            self.assertEqual(
                events,
                [
                    "original",
                    "retry_started",
                    "retry_failed",
                    "retry_started",
                    "retry_failed",
                ],
            )

    def test_retry_rejects_every_identity_mismatch_without_rewriting(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            args = self.make_args(root, run_id="retry-run", retry_failed=True)
            args.run_dir.mkdir(parents=True)
            for field, replacement in (
                ("run_id", "other"),
                ("config_sha256", "f" * 64),
                ("input_snapshot_dir", str(root / "other-snapshot")),
                ("input_bundle_sha256", "f" * 64),
                ("pdk_revision", "wrong"),
                ("pdk_integrity_sha256", "e" * 64),
                ("image_digest", "sha256:" + "e" * 64),
                ("runtime_image_ref", "sha256:" + "e" * 64),
                ("librelane_version", "3.0.4"),
                ("main_pid", args.main_pid + 1),
                ("main_pid_start_time", args.main_pid_start_time + 1),
            ):
                visual = args.run_dir / "visualizations"
                if visual.exists():
                    for child in visual.iterdir():
                        child.unlink()
                else:
                    visual.mkdir()
                body = {
                    "schema_version": 3,
                    **self.identity(args),
                    "status": "completed_with_errors",
                    "steps": [],
                    "errors": [],
                }
                body[field] = replacement
                manifest = visual / "manifest.json"
                timeline = visual / "timeline.jsonl"
                manifest.write_text(json.dumps(body))
                timeline.write_text('{"event":"original"}\n')
                before = manifest.read_bytes(), timeline.read_bytes()
                with (
                    self.subTest(field=field),
                    contextlib.redirect_stdout(io.StringIO()),
                ):
                    self.assertEqual(run_visualizer(args), 1)
                self.assertEqual((manifest.read_bytes(), timeline.read_bytes()), before)

    def test_retry_rejects_missing_step_and_symlinked_manifest(self):
        with (
            tempfile.TemporaryDirectory() as temp,
            tempfile.TemporaryDirectory() as other,
        ):
            root = Path(temp)
            args = self.make_args(root, run_id="retry-run", retry_failed=True)
            args.run_dir.mkdir(parents=True)
            manifest, timeline = self.write_manifest(
                args,
                [self.record(9, "OpenROAD.Floorplan", visualization_status="failed")],
            )
            before = manifest.read_bytes(), timeline.read_bytes()
            with contextlib.redirect_stdout(io.StringIO()):
                self.assertEqual(run_visualizer(args), 1)
            self.assertEqual((manifest.read_bytes(), timeline.read_bytes()), before)

            outside = Path(other) / "manifest.json"
            outside.write_bytes(manifest.read_bytes())
            manifest.unlink()
            manifest.symlink_to(outside)
            with contextlib.redirect_stdout(io.StringIO()):
                self.assertEqual(run_visualizer(args), 1)
            self.assertEqual(outside.read_bytes(), before[0])

    def test_stale_or_invalid_png_cannot_make_render_pass(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            args = self.make_args(root, run_id="stale-run")
            step = self.make_step(args.run_dir, 1, "OpenROAD.Floorplan")
            visual = args.run_dir / "visualizations"
            output = visual / step.name
            output.mkdir(parents=True)
            stale = output / "stale.png"
            stale.write_bytes(png_bytes())

            def fake_run(
                command: list[str], **_: object
            ) -> subprocess.CompletedProcess:
                if command[:2] == ["docker", "run"]:
                    (output / "invalid.png").write_bytes(b"not-a-png")
                return subprocess.CompletedProcess(command, 0)

            with patch("pi_lane.visualizer.subprocess.run", side_effect=fake_run):
                self.assertEqual(run_visualizer(args), 1)
            self.assertFalse(stale.exists())
            self.assertTrue((output / "invalid.png").exists())
            manifest = json.loads((visual / "manifest.json").read_text())
            self.assertIn("fresh valid PNG", manifest["errors"][0])


if __name__ == "__main__":
    unittest.main()
