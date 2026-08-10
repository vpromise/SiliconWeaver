from __future__ import annotations

import os
from pathlib import Path
import subprocess
from types import SimpleNamespace
import tempfile
import unittest
from unittest.mock import patch

from pi_lane import launcher


class ExecCalled(RuntimeError):
    pass


class LauncherTests(unittest.TestCase):
    def test_version_is_local_and_does_not_create_state_or_probe_pi(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            state = Path(temporary) / "state"
            with (
                patch.dict(os.environ, {"XDG_STATE_HOME": str(state)}, clear=True),
                patch("pi_lane.launcher.shutil.which") as which,
                patch("pi_lane.launcher.subprocess.run") as run,
                patch("pi_lane.launcher.os.execvpe") as execute,
                patch("pi_lane.launcher.sys.argv", ["pi-lane", "--version"]),
                patch("builtins.print") as output,
            ):
                self.assertEqual(launcher.main(), 0)

            output.assert_called_once_with("pi-lane 0.5.1")
            which.assert_not_called()
            run.assert_not_called()
            execute.assert_not_called()
            self.assertFalse(state.exists())

    def test_help_is_forwarded_without_starting_daemon_or_creating_state(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            state = Path(temporary) / "state"
            environment = {"XDG_STATE_HOME": str(state)}
            with (
                patch.dict(os.environ, environment, clear=True),
                patch("pi_lane.launcher.shutil.which", return_value="/usr/bin/pi"),
                patch("pi_lane.launcher.subprocess.run") as run,
                patch("pi_lane.launcher._rtl_closure_environment") as adapter,
                patch("pi_lane.launcher.os.execvpe", side_effect=ExecCalled) as execute,
                patch("pi_lane.launcher.sys.argv", ["pi-lane", "--color", "--help"]),
                self.assertRaises(ExecCalled),
            ):
                launcher.main()

            run.assert_not_called()
            adapter.assert_not_called()
            execute.assert_called_once_with(
                "/usr/bin/pi", ["/usr/bin/pi", "--color", "--help"], environment
            )
            self.assertFalse(state.exists())

    def test_packaged_prompt_has_no_checkout_specific_home_path(self) -> None:
        prompt = launcher.resource_root() / "prompts" / "LIBRELANE_AGENT.md"
        content = prompt.read_text(encoding="utf-8")
        self.assertNotIn("/home/", content)

    def test_startup_record_ignores_stale_pid_temporary_file(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            path = root / "orchestrator-start.json"
            stale = root / f".{path.name}.{os.getpid()}.tmp"
            stale.write_text("stale\n", encoding="utf-8")

            launcher._write_startup_record(path, "current\n")

            self.assertEqual(path.read_text(encoding="utf-8"), "current\n")
            self.assertEqual(stale.read_text(encoding="utf-8"), "stale\n")
            self.assertEqual(path.stat().st_mode & 0o777, 0o600)

    def test_resource_root_uses_explicit_relocatable_bundle(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / "extensions").mkdir()
            (root / "prompts").mkdir()
            (root / "extensions" / "lane-monitor.ts").write_text("export {};\n")
            (root / "extensions" / "lane-monitor-core.ts").write_text("export {};\n")
            (root / "prompts" / "LIBRELANE_AGENT.md").write_text("prompt\n")
            with patch.dict(os.environ, {"PI_LANE_RESOURCE_ROOT": str(root)}):
                self.assertEqual(launcher.resource_root(), root.resolve())

    def test_main_uses_current_python_and_packaged_resources(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            state = root / "state"
            resources = root / "resources"
            workspace = root / "workspace"
            workspace.mkdir()
            agent = state / "pi-lane" / "agent"
            agent.mkdir(mode=0o700, parents=True)
            for name, value in {
                "models.json": {
                    "providers": {
                        "openai": {
                            "baseUrl": "https://models.example.test/v1",
                            "models": [{"id": "fallback-model"}, {"id": "selected-model"}],
                        }
                    }
                },
                "settings.json": {
                    "defaultProvider": "openai",
                    "defaultModel": "selected-model",
                },
                "auth.json": {"openai": {"key": "private-test-key"}},
            }.items():
                path = agent / name
                path.write_text(launcher.json.dumps(value), encoding="utf-8")
                path.chmod(0o600)
            (resources / "extensions").mkdir(parents=True)
            (resources / "prompts").mkdir()
            (resources / "extensions" / "lane-monitor.ts").write_text("export {};\n")
            (resources / "extensions" / "lane-monitor-core.ts").write_text(
                "export {};\n"
            )
            (resources / "prompts" / "LIBRELANE_AGENT.md").write_text("prompt\n")

            environment = {
                "XDG_STATE_HOME": str(state),
                "PI_LANE_RESOURCE_ROOT": str(resources),
                "PI_LANE_WORKSPACE": str(workspace),
            }
            completed = SimpleNamespace(
                returncode=0, stdout='{"ok": true}\n', stderr=""
            )
            old_cwd = Path.cwd()
            try:
                with (
                    patch.dict(os.environ, environment, clear=True),
                    patch(
                        "pi_lane.launcher.subprocess.run", return_value=completed
                    ) as run,
                    patch("pi_lane.launcher.shutil.which", return_value="/usr/bin/pi"),
                    patch(
                        "pi_lane.launcher.os.execvpe",
                        side_effect=ExecCalled,
                    ) as execute,
                    patch("pi_lane.launcher.sys.argv", ["pi-lane", "--print"]),
                    self.assertRaises(ExecCalled),
                ):
                    launcher.main()
            finally:
                os.chdir(old_cwd)

            daemon_command = run.call_args.args[0]
            self.assertEqual(daemon_command[0], launcher.sys.executable)
            self.assertEqual(daemon_command[-3:], ["daemon", "ensure", "--json"])
            self.assertEqual(run.call_args.kwargs["cwd"], workspace.resolve())
            self.assertEqual(run.call_args.kwargs["timeout"], 30)
            self.assertEqual(
                run.call_args.kwargs["env"]["PI_LANE_CONTROL_PYTHON"],
                str(Path(launcher.sys.executable).absolute()),
            )
            self.assertEqual(
                run.call_args.kwargs["env"]["RTL_CLOSURE_MODEL"], "selected-model"
            )
            self.assertEqual(
                run.call_args.kwargs["env"]["RTL_CLOSURE_BASE_URL"],
                "https://models.example.test/v1",
            )
            self.assertEqual(
                run.call_args.kwargs["env"]["RTL_CLOSURE_API_KEY"],
                "private-test-key",
            )
            arguments = execute.call_args.args[1]
            pi_environment = execute.call_args.args[2]
            self.assertNotIn("RTL_CLOSURE_MODEL", pi_environment)
            self.assertNotIn("RTL_CLOSURE_BASE_URL", pi_environment)
            self.assertNotIn("RTL_CLOSURE_API_KEY", pi_environment)
            self.assertIn(str(resources / "extensions" / "lane-monitor.ts"), arguments)
            self.assertIn(str(resources / "prompts" / "LIBRELANE_AGENT.md"), arguments)
            self.assertEqual(arguments[-1], "--print")
            self.assertEqual(
                (state / "pi-lane" / "agent").stat().st_mode & 0o777, 0o700
            )
            self.assertEqual(
                (state / "pi-lane" / "sessions").stat().st_mode & 0o777, 0o700
            )
            startup_record = state / "pi-lane" / "agent" / "orchestrator-start.json"
            self.assertEqual(startup_record.read_text(), '{"ok": true}\n')
            self.assertEqual(startup_record.stat().st_mode & 0o777, 0o600)

    def test_control_python_preserves_virtual_environment_symlink(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            system_python = root / "system-python"
            system_python.write_text("", encoding="utf-8")
            environment_python = root / "venv" / "bin" / "python"
            environment_python.parent.mkdir(parents=True)
            environment_python.symlink_to(system_python)

            with patch.object(
                launcher.sys, "executable", str(environment_python)
            ):
                selected = launcher._control_python()
            resolved_target = environment_python.resolve()

        self.assertEqual(selected, environment_python.absolute())
        self.assertNotEqual(selected, resolved_target)

    def test_existing_public_session_or_agent_directory_is_rejected_without_chmod(self) -> None:
        for public_variable in ("PI_LANE_SESSION_DIR", "PI_LANE_AGENT_DIR"):
            with self.subTest(variable=public_variable), tempfile.TemporaryDirectory() as temporary:
                root = Path(temporary)
                state = root / "state"
                public = root / "public"
                private = root / "private"
                public.mkdir(mode=0o775)
                private.mkdir(mode=0o700)
                public.chmod(0o775)
                environment = {
                    "XDG_STATE_HOME": str(state),
                    "PI_LANE_SESSION_DIR": str(
                        public if public_variable == "PI_LANE_SESSION_DIR" else private
                    ),
                    "PI_LANE_AGENT_DIR": str(
                        public if public_variable == "PI_LANE_AGENT_DIR" else private
                    ),
                }
                with (
                    patch.dict(os.environ, environment, clear=True),
                    patch("pi_lane.launcher.subprocess.run") as run,
                    patch("pi_lane.launcher.sys.argv", ["pi-lane"]),
                    self.assertRaisesRegex(RuntimeError, "owner-only"),
                ):
                    launcher.main()

                run.assert_not_called()
                self.assertEqual(public.stat().st_mode & 0o777, 0o775)

    def test_model_adapter_preserves_explicit_environment_without_reading_files(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            agent = Path(temporary) / "agent"
            agent.mkdir(mode=0o700)
            models = agent / "models.json"
            models.write_text(
                launcher.json.dumps(
                    {
                        "providers": {
                            "openai": {
                                "baseUrl": "https://ignored.example.test/v1",
                                "models": [{"id": "ignored-model"}],
                            }
                        }
                    }
                ),
                encoding="utf-8",
            )
            models.chmod(0o644)
            environment = {
                "RTL_CLOSURE_MODEL": "explicit-model",
                "RTL_CLOSURE_BASE_URL": "https://explicit.example.test/v1",
                "RTL_CLOSURE_API_KEY": "explicit-key",
            }

            with patch("pi_lane.launcher._read_private_json") as read:
                adapted = launcher._rtl_closure_environment(agent, environment)

            self.assertEqual(adapted, environment)
            self.assertIsNot(adapted, environment)
            read.assert_not_called()

    def test_model_adapter_ignores_public_or_symlinked_configuration(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            agent = root / "agent"
            agent.mkdir(mode=0o700)
            models = agent / "models.json"
            models.write_text(
                launcher.json.dumps(
                    {
                        "providers": {
                            "openai": {
                                "baseUrl": "https://unsafe.example.test/v1",
                                "models": [{"id": "unsafe-model"}],
                            }
                        }
                    }
                ),
                encoding="utf-8",
            )
            models.chmod(0o644)
            external_auth = root / "auth.json"
            external_auth.write_text(
                launcher.json.dumps({"openai": {"key": "unsafe-key"}}),
                encoding="utf-8",
            )
            external_auth.chmod(0o600)
            (agent / "auth.json").symlink_to(external_auth)

            adapted = launcher._rtl_closure_environment(agent, {})

            self.assertNotIn("RTL_CLOSURE_MODEL", adapted)
            self.assertNotIn("RTL_CLOSURE_BASE_URL", adapted)
            self.assertNotIn("RTL_CLOSURE_API_KEY", adapted)

    def test_source_launcher_resolves_a_symlink_before_selecting_workspace(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            alias = root / "pi-lane"
            alias.symlink_to(Path(__file__).resolve().parents[1] / "bin" / "pi-lane")
            fake_python = root / "python"
            fake_python.write_text(
                "#!/bin/sh\nprintf '%s\\n' \"$PI_LANE_WORKSPACE\"\nprintf '%s\\n' \"$*\"\n",
                encoding="utf-8",
            )
            fake_python.chmod(0o700)
            environment = os.environ.copy()
            environment.pop("PI_LANE_WORKSPACE", None)
            environment["PI_LANE_PYTHON"] = str(fake_python)

            completed = subprocess.run(
                [str(alias), "--probe"],
                env=environment,
                text=True,
                capture_output=True,
                check=False,
            )

            self.assertEqual(completed.returncode, 0, completed.stderr)
            lines = completed.stdout.splitlines()
            self.assertEqual(lines[0], str(Path(__file__).resolve().parents[1]))
            self.assertEqual(lines[1], "-m pi_lane.launcher --probe")

    def test_source_launcher_rejects_broken_and_cyclic_symlinks(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            broken = root / "broken"
            broken.symlink_to(root / "missing")
            with self.assertRaises(OSError):
                subprocess.run([str(broken)], timeout=5, check=False)

            first = root / "first"
            second = root / "second"
            first.symlink_to(second)
            second.symlink_to(first)
            with self.assertRaises(OSError):
                subprocess.run([str(first)], timeout=5, check=False)


if __name__ == "__main__":
    unittest.main()
