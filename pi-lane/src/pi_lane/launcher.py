"""Relocatable launcher for the isolated Pi-Lane interactive profile."""

from __future__ import annotations

import json
import os
from importlib.metadata import PackageNotFoundError, distribution
from pathlib import Path
import shutil
import stat
import subprocess
import sys
import sysconfig
import tempfile
from typing import Iterable

from . import __version__

FORWARDED_INFORMATION_ARGUMENTS = frozenset({"-h", "--help"})
PRIVATE_CONFIG_MAX_BYTES = 1024 * 1024


def _control_python() -> Path:
    """Return the active environment's interpreter without resolving its symlink."""

    return Path(sys.executable).absolute()


def _state_home() -> Path:
    configured = os.environ.get("XDG_STATE_HOME")
    if configured:
        return Path(configured).expanduser()
    return Path.home() / ".local" / "state"


def _resource_candidates() -> Iterable[Path]:
    configured = os.environ.get("PI_LANE_RESOURCE_ROOT")
    if configured:
        yield Path(configured).expanduser()

    # Editable/source installs retain the repository resources next to src/.
    source_root = Path(__file__).resolve().parents[2]
    yield source_root

    # Distribution RECORD paths preserve the install scheme, including
    # user-site installs where sysconfig's default prefix may differ.
    try:
        installed = distribution("pi-lane")
    except PackageNotFoundError:
        installed = None
    if installed is not None:
        for entry in installed.files or ():
            if str(entry).replace("\\", "/").endswith(
                "share/pi-lane/extensions/lane-monitor.ts"
            ):
                yield Path(installed.locate_file(entry)).resolve().parents[1]
                break

    data_root = Path(sysconfig.get_path("data"))
    yield data_root / "share" / "pi-lane"


def resource_root() -> Path:
    """Return a directory containing the packaged extension and prompt."""

    checked: list[str] = []
    for candidate in _resource_candidates():
        root = candidate.resolve()
        checked.append(str(root))
        if (
            (root / "extensions" / "lane-monitor.ts").is_file()
            and (root / "extensions" / "lane-monitor-core.ts").is_file()
            and (root / "prompts" / "LIBRELANE_AGENT.md").is_file()
        ):
            return root
    locations = ", ".join(checked)
    raise RuntimeError(f"Pi-Lane resources are missing; checked: {locations}")


def _private_directory(path: Path) -> Path:
    try:
        metadata = path.lstat()
    except FileNotFoundError:
        path.mkdir(mode=0o700, parents=True, exist_ok=True)
        metadata = path.lstat()
    if (
        stat.S_ISLNK(metadata.st_mode)
        or not stat.S_ISDIR(metadata.st_mode)
        or metadata.st_uid != os.geteuid()
        or stat.S_IMODE(metadata.st_mode) & 0o077
    ):
        raise RuntimeError(
            f"Pi-Lane private path must be an owner-only real directory: {path}"
        )
    return path.resolve()


def _read_private_json(path: Path) -> dict[str, object] | None:
    """Read a bounded owner-only regular JSON file without following symlinks."""

    flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(path, flags)
    except OSError:
        return None
    try:
        metadata = os.fstat(descriptor)
        if (
            not stat.S_ISREG(metadata.st_mode)
            or metadata.st_uid != os.geteuid()
            or stat.S_IMODE(metadata.st_mode) & 0o077
            or metadata.st_size > PRIVATE_CONFIG_MAX_BYTES
        ):
            return None
        chunks: list[bytes] = []
        remaining = metadata.st_size
        while remaining > 0:
            chunk = os.read(descriptor, min(remaining, 64 * 1024))
            if not chunk:
                break
            chunks.append(chunk)
            remaining -= len(chunk)
        if remaining != 0:
            return None
        parsed = json.loads(b"".join(chunks).decode("utf-8"))
        return parsed if isinstance(parsed, dict) else None
    except (OSError, UnicodeError, json.JSONDecodeError):
        return None
    finally:
        os.close(descriptor)


def _rtl_closure_environment(
    agent_dir: Path, environment: dict[str, str]
) -> dict[str, str]:
    """Adapt isolated Pi model settings into the orchestrator process only."""

    names = {
        "model": "RTL_CLOSURE_MODEL",
        "base_url": "RTL_CLOSURE_BASE_URL",
        "api_key": "RTL_CLOSURE_API_KEY",
    }
    missing = {label for label, name in names.items() if name not in environment}
    if not missing:
        return environment.copy()

    provider_name = environment.get("RTL_CLOSURE_PROVIDER", "openai")
    models = (
        _read_private_json(agent_dir / "models.json")
        if missing & {"model", "base_url"}
        else None
    )
    provider: dict[str, object] = {}
    if isinstance(models, dict):
        providers = models.get("providers")
        if isinstance(providers, dict):
            candidate = providers.get(provider_name)
            if isinstance(candidate, dict):
                provider = candidate

    values: dict[str, str] = {}
    rows = provider.get("models")
    available: list[str] = []
    if isinstance(rows, list):
        for row in rows:
            if isinstance(row, dict) and isinstance(row.get("id"), str) and row["id"]:
                available.append(row["id"])
    if "model" in missing and available:
        values["model"] = available[0]
        settings = _read_private_json(agent_dir / "settings.json")
        if (
            isinstance(settings, dict)
            and settings.get("defaultProvider") == provider_name
            and settings.get("defaultModel") in available
        ):
            values["model"] = str(settings["defaultModel"])
    if "base_url" in missing and isinstance(provider.get("baseUrl"), str):
        values["base_url"] = str(provider["baseUrl"])
    if "api_key" in missing:
        auth = _read_private_json(agent_dir / "auth.json")
        credentials = auth.get(provider_name) if isinstance(auth, dict) else None
        if isinstance(credentials, dict) and isinstance(credentials.get("key"), str):
            values["api_key"] = str(credentials["key"])

    adapted = environment.copy()
    for label, value in values.items():
        if value:
            adapted[names[label]] = value
    return adapted


def _write_startup_record(path: Path, content: str) -> None:
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{path.name}.", suffix=".tmp", dir=path.parent
    )
    temporary = Path(temporary_name)
    try:
        os.fchmod(descriptor, 0o600)
        handle = os.fdopen(descriptor, "w", encoding="utf-8")
        descriptor = -1
        with handle:
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
        directory = os.open(path.parent, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
    finally:
        if descriptor >= 0:
            os.close(descriptor)
        if temporary.exists():
            temporary.unlink()


def main() -> int:
    if "--version" in sys.argv[1:]:
        print(f"pi-lane {__version__}")
        return 0

    if any(argument in FORWARDED_INFORMATION_ARGUMENTS for argument in sys.argv[1:]):
        configured_pi = os.environ.get("PI_LANE_PI_EXECUTABLE", "pi")
        pi_executable = shutil.which(configured_pi)
        if pi_executable is None:
            print(f"pi-lane: Pi executable is unavailable: {configured_pi}", file=sys.stderr)
            return 127
        os.execvpe(pi_executable, [pi_executable, *sys.argv[1:]], os.environ.copy())
        return 127

    state_root = _state_home() / "pi-lane"
    session_dir = _private_directory(
        Path(os.environ.get("PI_LANE_SESSION_DIR", state_root / "sessions")).expanduser()
    )
    agent_dir = _private_directory(
        Path(os.environ.get("PI_LANE_AGENT_DIR", state_root / "agent")).expanduser()
    )
    workspace = Path(os.environ.get("PI_LANE_WORKSPACE", os.getcwd())).expanduser().resolve()
    if not workspace.is_dir():
        raise RuntimeError(f"PI_LANE_WORKSPACE is not a directory: {workspace}")

    resources = resource_root()
    environment = os.environ.copy()
    environment["PI_CODING_AGENT_DIR"] = str(agent_dir)
    environment["PI_LANE_AGENT_DIR"] = str(agent_dir)
    # The monitor must use the exact installed control plane as this launcher;
    # PATH may resolve a different global executable.
    environment["PI_LANE_CONTROL_PYTHON"] = str(_control_python())
    orchestrator_environment = _rtl_closure_environment(agent_dir, environment)

    try:
        startup = subprocess.run(
            [
                sys.executable,
                "-m",
                "pi_lane.orchestrator_cli",
                "daemon",
                "ensure",
                "--json",
            ],
            cwd=workspace,
            env=orchestrator_environment,
            text=True,
            capture_output=True,
            timeout=30,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        message = f"pi-lane: orchestrator startup failed: {exc}\n"
        _write_startup_record(agent_dir / "orchestrator-start.json", "")
        _write_startup_record(agent_dir / "orchestrator-start.err", message)
        print(message, file=sys.stderr, end="")
        return 1
    _write_startup_record(agent_dir / "orchestrator-start.json", startup.stdout)
    _write_startup_record(agent_dir / "orchestrator-start.err", startup.stderr)
    if startup.returncode != 0:
        if startup.stderr:
            print(startup.stderr, file=sys.stderr, end="")
        return startup.returncode

    configured_pi = environment.get("PI_LANE_PI_EXECUTABLE", "pi")
    pi_executable = shutil.which(configured_pi)
    if pi_executable is None:
        print(f"pi-lane: Pi executable is unavailable: {configured_pi}", file=sys.stderr)
        return 127

    arguments = [
        pi_executable,
        "--no-context-files",
        "--no-extensions",
        "--extension",
        str(resources / "extensions" / "lane-monitor.ts"),
        "--no-skills",
        "--session-dir",
        str(session_dir),
        "--append-system-prompt",
        str(resources / "prompts" / "LIBRELANE_AGENT.md"),
        *sys.argv[1:],
    ]
    os.chdir(workspace)
    os.execvpe(pi_executable, arguments, environment)
    return 127


if __name__ == "__main__":
    raise SystemExit(main())
