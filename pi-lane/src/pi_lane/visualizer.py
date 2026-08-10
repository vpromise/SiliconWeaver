"""Persist notebook-style LibreLane previews without blocking the main flow."""

from __future__ import annotations

import argparse
import json
import math
import os
import re
import shutil
import struct
import subprocess
import time
import zlib
from pathlib import Path
from typing import Any, Iterable

from .classic import LIBRELANE_IMAGE, LIBRELANE_VERSION
from .integrity import (
    canonical_directory,
    canonical_regular_file,
    direct_child_directory,
    file_sha256,
)
from .run_state import atomic_write, now_iso, pid_identity_alive


STEP_DIR_RE = re.compile(r"^(\d+)-(.+)$")
SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
IMAGE_DIGEST_RE = re.compile(r"^sha256:[0-9a-f]{64}$")
PNG_SIGNATURE = b"\x89PNG\r\n\x1a\n"
DEFAULT_RENDER_TIMEOUT_SECONDS = 300.0
MIN_RENDER_TIMEOUT_SECONDS = 10.0
MAX_RENDER_TIMEOUT_SECONDS = 3600.0
CONTAINER_STOP_GRACE_SECONDS = 5
CONTAINER_CLEANUP_TIMEOUT_SECONDS = 15.0
CONTAINER_DESIGN_DIR = Path("/pi-lane/design")
CONTAINER_VISUALIZATION_ROOT = Path("/pi-lane/visualizations")
RENDER_SCRIPT = """
import sys
from pathlib import Path
from librelane.flows import Flow
from librelane.steps import Step

config_path, state_path, pdk_root, pdk, output_dir, overrides_json = sys.argv[1:]
flow_type = Flow.factory.get("Classic")
if flow_type is None:
    raise RuntimeError("Classic flow is not registered")
target = Step.factory.get("KLayout.Render")
if target is None:
    raise RuntimeError("KLayout.Render step is not registered")
flow = flow_type(
    config_path,
    pdk_root=pdk_root,
    pdk=pdk,
    design_dir=str(Path(config_path).parent),
    config_override_strings=__import__("json").loads(overrides_json),
)
target.load(flow.config, state_path, pdk_root=pdk_root).start(step_dir=output_dir)
""".strip()


def _load_json(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
        return value if isinstance(value, dict) else {}
    except (OSError, json.JSONDecodeError):
        return {}


def _safe_json(path: Path, root: Path, label: str) -> dict[str, Any]:
    try:
        safe = canonical_regular_file(
            path,
            root=root,
            label=label,
            minimum_size=2,
            maximum_size=64 * 1024 * 1024,
        )
    except ValueError:
        return {}
    return _load_json(safe)


def _step_id(step_dir: Path) -> str:
    raw = _safe_json(step_dir / "config.json", step_dir, "Step config")
    meta = raw.get("meta") or {}
    if isinstance(meta, dict) and isinstance(meta.get("step"), str):
        return meta["step"]
    match = STEP_DIR_RE.match(step_dir.name)
    return match.group(2) if match else step_dir.name


def discover_step_dirs(run_dir: Path) -> list[tuple[int, Path]]:
    try:
        trusted_run = canonical_directory(run_dir, label="LibreLane run directory")
    except ValueError:
        return []
    result: list[tuple[int, Path]] = []
    ordinals: set[int] = set()
    for path in trusted_run.iterdir():
        match = STEP_DIR_RE.match(path.name)
        if match is None:
            continue
        try:
            safe = direct_child_directory(
                path, root=trusted_run, label="Step directory"
            )
        except ValueError as exc:
            raise ValueError(f"untrusted Step directory {path}: {exc}") from exc
        ordinal = int(match.group(1))
        if ordinal in ordinals:
            raise ValueError(f"duplicate Step ordinal in run directory: {ordinal}")
        ordinals.add(ordinal)
        result.append((ordinal, safe))
    return sorted(result)


def preview_kind(step_dir: Path) -> str | None:
    """Return the same layout-preview boundary used by LibreLane's notebook."""
    try:
        canonical_regular_file(
            step_dir / "state_out.json",
            root=step_dir,
            label="Step state_out",
            minimum_size=2,
            maximum_size=64 * 1024 * 1024,
        )
    except ValueError:
        return None
    step_id = _step_id(step_dir)
    state_in = _safe_json(step_dir / "state_in.json", step_dir, "Step state_in")
    state_out = _safe_json(step_dir / "state_out.json", step_dir, "Step state_out")
    if step_id == "KLayout.Render" and state_out.get("render"):
        return "existing"
    if step_id == "KLayout.StreamOut" and state_out.get("def"):
        return "render"
    if (
        step_id.startswith("OpenROAD.")
        and state_out.get("def")
        and state_out.get("def") != state_in.get("def")
    ):
        return "render"
    return None


def step_execution_record(ordinal: int, step_dir: Path) -> dict[str, Any]:
    safe_step = canonical_directory(step_dir, label="Step directory")
    logs: list[str] = []
    for path in safe_step.glob("*.log"):
        try:
            logs.append(
                str(
                    canonical_regular_file(
                        path,
                        root=safe_step,
                        label="Step log",
                        maximum_size=1024 * 1024 * 1024,
                    )
                )
            )
        except ValueError:
            continue
    logs.sort()
    commands = step_dir / "COMMANDS"
    runtime = step_dir / "runtime.txt"
    state_out = step_dir / "state_out.json"

    def safe_optional(path: Path, label: str) -> str | None:
        try:
            return str(
                canonical_regular_file(
                    path,
                    root=safe_step,
                    label=label,
                    maximum_size=64 * 1024 * 1024,
                )
            )
        except ValueError:
            return None

    safe_state_out = safe_optional(state_out, "Step state_out")
    return {
        "ordinal": ordinal,
        "id": _step_id(step_dir),
        "status": "completed" if safe_state_out is not None else "running",
        "directory": str(safe_step),
        "commands": safe_optional(commands, "Step commands"),
        "logs": logs,
        "runtime": safe_optional(runtime, "Step runtime"),
        "state_out": safe_state_out,
        "completed_at": (
            time.strftime(
                "%Y-%m-%dT%H:%M:%S%z", time.localtime(state_out.stat().st_mtime)
            )
            if safe_state_out is not None
            else None
        ),
        "preview_kind": preview_kind(step_dir),
        "visualization_status": "pending"
        if preview_kind(step_dir)
        else "not_applicable",
        "image": None,
        "visualization_directory": None,
        "visualization_error": None,
    }


def build_render_command(
    *,
    runtime_image_ref: str,
    container_name: str,
    design_dir: Path,
    input_snapshot_dir: Path,
    run_dir: Path,
    visualization_root: Path,
    pdk_root: Path,
    config_path: Path,
    state_path: Path,
    pdk: str,
    output_dir: Path,
    overrides: Iterable[str],
) -> list[str]:
    try:
        relative_config = config_path.resolve().relative_to(design_dir.resolve())
    except ValueError as exc:
        raise ValueError(
            "visualization config must be inside the design directory"
        ) from exc
    container_config = CONTAINER_DESIGN_DIR / relative_config
    container_output = CONTAINER_VISUALIZATION_ROOT / output_dir.name
    return [
        "docker",
        "run",
        "--rm",
        "--init",
        "--name",
        container_name,
        "--label",
        f"io.pi-lane.image-tag={LIBRELANE_IMAGE}",
        "--label",
        f"io.pi-lane.image-digest={runtime_image_ref}",
        "--network",
        "none",
        "--read-only",
        "--cap-drop",
        "ALL",
        "--security-opt",
        "no-new-privileges",
        "--user",
        f"{os.getuid()}:{os.getgid()}",
        "--tmpfs",
        "/tmp:rw,nosuid,nodev,size=2g",
        "-e",
        "HOME=/tmp",
        "-e",
        f"PDK_ROOT={pdk_root}",
        "-v",
        f"{input_snapshot_dir}:{CONTAINER_DESIGN_DIR}:ro",
        "-v",
        f"{run_dir}:{run_dir}:ro",
        "-v",
        f"{visualization_root}:{CONTAINER_VISUALIZATION_ROOT}:rw",
        "-v",
        f"{pdk_root}:{pdk_root}:ro",
        "-w",
        str(CONTAINER_DESIGN_DIR),
        runtime_image_ref,
        "python3",
        "-c",
        RENDER_SCRIPT,
        str(container_config),
        str(state_path),
        str(pdk_root),
        pdk,
        str(container_output),
        json.dumps(list(overrides), separators=(",", ":")),
    ]


def _append_event(timeline: Path, event: dict[str, Any]) -> None:
    timeline.parent.mkdir(parents=True, exist_ok=True)
    with timeline.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(event, sort_keys=True) + "\n")


def _write_manifest(path: Path, manifest: dict[str, Any]) -> None:
    manifest["updated_at"] = now_iso()
    atomic_write(path, json.dumps(manifest, indent=2, sort_keys=True) + "\n")


def _sync_manifest(
    manifest_path: Path,
    manifest: dict[str, Any],
    records: dict[int, dict[str, Any]],
) -> None:
    manifest["steps"] = [records[key] for key in sorted(records)]
    manifest["completed_step_count"] = sum(
        record.get("status") == "completed" for record in records.values()
    )
    manifest["visualization_count"] = sum(
        record.get("visualization_status") == "completed" for record in records.values()
    )
    manifest["failed_visualization_count"] = sum(
        record.get("visualization_status") == "failed" for record in records.values()
    )
    manifest["errors"] = [
        f"{record['id']}: {record.get('visualization_error') or 'visualization failed'}"
        for record in records.values()
        if record.get("visualization_status") == "failed"
    ]
    images = [
        record["image"]
        for record in records.values()
        if record.get("visualization_status") == "completed" and record.get("image")
    ]
    manifest["images"] = images
    manifest["latest_image"] = images[-1] if images else None
    _write_manifest(manifest_path, manifest)


def _identity_fields(
    args: argparse.Namespace, *, run_dir: Path, design_dir: Path
) -> dict[str, Any]:
    return {
        "run_id": args.run_id,
        "run_dir": str(run_dir),
        "design_dir": str(design_dir),
        "design_name": args.design_name,
        "config_path": str(args.config),
        "config_sha256": args.config_sha256,
        "input_snapshot_dir": str(args.input_snapshot_dir),
        "input_bundle_sha256": args.input_bundle_sha256,
        "pdk": args.pdk,
        "pdk_revision": args.pdk_revision,
        "pdk_integrity_sha256": args.pdk_integrity_sha256,
        "pdk_root": str(args.pdk_root),
        "image": args.image,
        "image_digest": args.image_digest,
        "runtime_image_ref": args.runtime_image_ref,
        "librelane_version": args.librelane_version,
        "main_pid": args.main_pid,
        "main_pid_start_time": args.main_pid_start_time,
        "overrides": list(args.overrides),
    }


def _load_retry_manifest(
    path: Path, *, expected_identity: dict[str, Any], visualization_root: Path
) -> dict[str, Any]:
    try:
        safe_path = canonical_regular_file(
            path,
            root=visualization_root,
            label="visualization manifest",
            minimum_size=2,
            maximum_size=64 * 1024 * 1024,
        )
    except ValueError as exc:
        raise ValueError(str(exc)) from exc
    try:
        raw = json.loads(safe_path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise ValueError(f"visualization manifest does not exist: {path}") from exc
    except OSError as exc:
        raise ValueError(f"cannot read visualization manifest {path}: {exc}") from exc
    except json.JSONDecodeError as exc:
        raise ValueError(f"visualization manifest is not valid JSON: {path}") from exc

    if not isinstance(raw, dict):
        raise ValueError("visualization manifest must be a JSON object")
    if raw.get("schema_version") != 3:
        raise ValueError("visualization manifest schema_version must be 3 for retry")
    for key, expected in expected_identity.items():
        if raw.get(key) != expected:
            raise ValueError(
                f"visualization manifest {key} mismatch: "
                f"expected {expected!r}, found {raw.get(key)!r}"
            )
    if raw.get("status") != "completed_with_errors":
        raise ValueError(
            "visualization retry requires manifest status completed_with_errors"
        )

    steps = raw.get("steps")
    if not isinstance(steps, list):
        raise ValueError("visualization manifest steps must be a list")
    ordinals: set[int] = set()
    for index, record in enumerate(steps):
        if not isinstance(record, dict):
            raise ValueError(f"visualization manifest step {index} must be an object")
        ordinal = record.get("ordinal")
        if type(ordinal) is not int or ordinal < 0:
            raise ValueError(
                f"visualization manifest step {index} has an invalid ordinal"
            )
        if ordinal in ordinals:
            raise ValueError(f"visualization manifest has duplicate ordinal {ordinal}")
        ordinals.add(ordinal)
        if not isinstance(record.get("id"), str) or not record["id"]:
            raise ValueError(f"visualization manifest step {ordinal} has an invalid id")
        attempts = record.get("attempts")
        if attempts is not None and not isinstance(attempts, list):
            raise ValueError(
                f"visualization manifest step {ordinal} attempts must be a list"
            )
    retry_count = raw.get("retry_count")
    if retry_count is not None and (type(retry_count) is not int or retry_count < 0):
        raise ValueError(
            "visualization manifest retry_count must be a non-negative integer"
        )
    return raw


def _merge_retry_records(
    manifest: dict[str, Any], run_dir: Path
) -> dict[int, dict[str, Any]]:
    records = {record["ordinal"]: record for record in manifest["steps"]}
    current_steps = dict(discover_step_dirs(run_dir))
    missing = sorted(set(records) - set(current_steps))
    if missing:
        raise ValueError(
            "visualization manifest references missing current Step ordinals: "
            + ", ".join(str(value) for value in missing)
        )
    execution_fields = (
        "status",
        "directory",
        "commands",
        "logs",
        "runtime",
        "state_out",
        "completed_at",
    )
    for ordinal, step_dir in sorted(current_steps.items()):
        current = step_execution_record(ordinal, step_dir)
        record = records.get(ordinal)
        if record is None:
            records[ordinal] = current
            continue
        if record["id"] != current["id"]:
            raise ValueError(
                "visualization manifest step mismatch at ordinal "
                f"{ordinal}: {record['id']!r} != {current['id']!r}"
            )
        for field in execution_fields:
            record[field] = current[field]
        if current["preview_kind"] is not None or not record.get("preview_kind"):
            record["preview_kind"] = current["preview_kind"]

    for record in records.values():
        record.setdefault("attempts", [])
        if "visualization_status" not in record:
            record["visualization_status"] = (
                "pending" if record.get("preview_kind") else "not_applicable"
            )
        record.setdefault("image", None)
        record.setdefault("visualization_directory", None)
        record.setdefault("visualization_error", None)
    return records


def _valid_png(path: Path, *, root: Path) -> Path | None:
    try:
        safe = canonical_regular_file(
            path,
            root=root,
            label="visualization PNG",
            minimum_size=45,
            maximum_size=256 * 1024 * 1024,
        )
        data = safe.read_bytes()
    except (OSError, ValueError):
        return None
    if safe.parent != canonical_directory(root, label="visualization PNG root"):
        return None
    if not data.startswith(PNG_SIGNATURE):
        return None
    offset = len(PNG_SIGNATURE)
    seen_ihdr = False
    seen_idat = False
    seen_iend = False
    while offset + 12 <= len(data):
        length = struct.unpack(">I", data[offset : offset + 4])[0]
        end = offset + 12 + length
        if end > len(data):
            return None
        kind = data[offset + 4 : offset + 8]
        payload = data[offset + 8 : offset + 8 + length]
        expected_crc = struct.unpack(">I", data[offset + 8 + length : end])[0]
        if zlib.crc32(kind + payload) & 0xFFFFFFFF != expected_crc:
            return None
        if not seen_ihdr:
            if kind != b"IHDR" or length != 13:
                return None
            width, height = struct.unpack(">II", payload[:8])
            if width == 0 or height == 0:
                return None
            seen_ihdr = True
        elif kind == b"IHDR":
            return None
        if kind == b"IDAT":
            seen_idat = True
        if kind == b"IEND":
            if length != 0 or end != len(data):
                return None
            seen_iend = True
            break
        offset = end
    return safe if seen_ihdr and seen_idat and seen_iend else None


def _has_readable_image(record: dict[str, Any], visualization_root: Path) -> bool:
    value = record.get("image")
    if not isinstance(value, str) or not value:
        return False
    output_value = record.get("visualization_directory")
    if not isinstance(output_value, str) or not output_value:
        return False
    try:
        output_dir = direct_child_directory(
            Path(output_value),
            root=visualization_root,
            label="visualization Step directory",
        )
    except ValueError:
        return False
    return _valid_png(Path(value), root=output_dir) is not None


def _retry_candidate(record: dict[str, Any], visualization_root: Path) -> bool:
    if record.get("status") != "completed" or not record.get("preview_kind"):
        return False
    status = record.get("visualization_status")
    if status == "completed":
        return not _has_readable_image(record, visualization_root)
    return status in {None, "missing", "pending", "failed"}


def _existing_image(step_dir: Path) -> Path | None:
    state_out = _safe_json(step_dir / "state_out.json", step_dir, "Step state_out")
    value = state_out.get("render")
    if isinstance(value, str):
        return _valid_png(Path(value), root=step_dir)
    return None


def _clear_stale_pngs(output_dir: Path) -> None:
    for path in output_dir.iterdir():
        if path.suffix.casefold() != ".png":
            continue
        if path.is_symlink() or path.is_file():
            path.unlink()
            continue
        raise RuntimeError(f"stale PNG path is not a regular file: {path}")


def _render_timeout_seconds(value: str) -> float:
    try:
        timeout = float(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError("render timeout must be a number") from exc
    if not math.isfinite(timeout):
        raise argparse.ArgumentTypeError("render timeout must be finite")
    if not MIN_RENDER_TIMEOUT_SECONDS <= timeout <= MAX_RENDER_TIMEOUT_SECONDS:
        raise argparse.ArgumentTypeError(
            "render timeout must be between "
            f"{MIN_RENDER_TIMEOUT_SECONDS:g} and {MAX_RENDER_TIMEOUT_SECONDS:g} seconds"
        )
    return timeout


def _cleanup_render_container(container_name: str) -> None:
    """Best-effort cleanup for a render container after every render attempt."""
    commands = (
        [
            "docker",
            "stop",
            "--time",
            str(CONTAINER_STOP_GRACE_SECONDS),
            container_name,
        ],
        ["docker", "rm", "-f", container_name],
    )
    for command in commands:
        try:
            subprocess.run(
                command,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                check=False,
                timeout=CONTAINER_CLEANUP_TIMEOUT_SECONDS,
            )
        except (OSError, subprocess.TimeoutExpired):
            pass


def _render_failure_detail(path: Path) -> str | None:
    try:
        with path.open("rb") as handle:
            handle.seek(0, os.SEEK_END)
            size = handle.tell()
            handle.seek(max(0, size - 16 * 1024))
            tail = handle.read().decode("utf-8", errors="replace")
    except OSError:
        return None
    detail = " | ".join(line.strip() for line in tail.splitlines()[-8:] if line.strip())
    return detail[:1000] or None


def _render_step(
    record: dict[str, Any],
    *,
    run_id: str,
    runtime_image_ref: str,
    design_dir: Path,
    input_snapshot_dir: Path,
    run_dir: Path,
    visualization_root: Path,
    pdk_root: Path,
    config_path: Path,
    pdk: str,
    overrides: list[str],
    current_container_file: Path,
    render_timeout_seconds: float,
) -> None:
    current_container_file.unlink(missing_ok=True)
    expected_run_dir = design_dir / "runs" / f"PI_LANE_{run_id}"
    source_dir = direct_child_directory(
        Path(record["directory"]),
        root=expected_run_dir,
        label="source Step directory",
    )
    output_dir = visualization_root / source_dir.name
    if output_dir.exists():
        output_dir = direct_child_directory(
            output_dir,
            root=visualization_root,
            label="visualization Step directory",
        )
    else:
        output_dir.mkdir(mode=0o700)
        output_dir = direct_child_directory(
            output_dir,
            root=visualization_root,
            label="visualization Step directory",
        )
    _clear_stale_pngs(output_dir)
    record["visualization_directory"] = str(output_dir)

    if record["preview_kind"] == "existing":
        source_image = _existing_image(source_dir)
        if source_image is None:
            raise RuntimeError("KLayout.Render completed without a readable PNG")
        target = output_dir / source_image.name
        shutil.copy2(source_image, target)
        validated = _valid_png(target, root=output_dir)
        if validated is None:
            raise RuntimeError("copied KLayout.Render artifact is not a valid PNG")
        record["image"] = str(validated)
        record["visualization_status"] = "completed"
        return

    container_name = f"pi-lane-viz-{run_id}-{record['ordinal']}"
    atomic_write(current_container_file, container_name + "\n")
    render_log = output_dir / "sidecar.log"
    record["render_log"] = str(render_log)
    if render_log.exists():
        try:
            safe_render_log = canonical_regular_file(
                render_log,
                root=output_dir,
                label="visualization render log",
                maximum_size=1024 * 1024 * 1024,
            )
        except ValueError as exc:
            raise RuntimeError(str(exc)) from exc
        if safe_render_log.parent != output_dir:
            raise RuntimeError("visualization render log is not a direct child")
    try:
        command = build_render_command(
            runtime_image_ref=runtime_image_ref,
            container_name=container_name,
            design_dir=design_dir,
            input_snapshot_dir=input_snapshot_dir,
            run_dir=run_dir,
            visualization_root=visualization_root,
            pdk_root=pdk_root,
            config_path=config_path,
            state_path=Path(record["state_out"]),
            pdk=pdk,
            output_dir=output_dir,
            overrides=overrides,
        )
        with render_log.open("ab", buffering=0) as output:
            try:
                completed = subprocess.run(
                    command,
                    stdin=subprocess.DEVNULL,
                    stdout=output,
                    stderr=subprocess.STDOUT,
                    check=False,
                    timeout=render_timeout_seconds,
                )
            except subprocess.TimeoutExpired as exc:
                raise RuntimeError(
                    f"KLayout.Render timed out after {render_timeout_seconds:g} seconds"
                ) from exc
        if completed.returncode != 0:
            detail = _render_failure_detail(render_log)
            suffix = f": {detail}" if detail else ""
            raise RuntimeError(
                f"KLayout.Render exited with code {completed.returncode}{suffix}"
            )
        candidates = sorted(
            safe
            for candidate in output_dir.iterdir()
            if candidate.suffix.casefold() == ".png"
            and (safe := _valid_png(candidate, root=output_dir)) is not None
        )
        if not candidates:
            raise RuntimeError("KLayout.Render did not produce a fresh valid PNG")
        record["image"] = str(candidates[-1])
        record["visualization_status"] = "completed"
    finally:
        _cleanup_render_container(container_name)
        current_container_file.unlink(missing_ok=True)


def _retry_failed_visualizations(
    args: argparse.Namespace,
    *,
    run_dir: Path,
    design_dir: Path,
    visualization_root: Path,
    manifest_path: Path,
    timeline_path: Path,
    current_container_file: Path,
) -> int:
    # Validate the complete persisted identity before appending events or rewriting state.
    manifest = _load_retry_manifest(
        manifest_path,
        expected_identity=_identity_fields(
            args, run_dir=run_dir, design_dir=design_dir
        ),
        visualization_root=visualization_root,
    )
    if timeline_path.exists():
        canonical_regular_file(
            timeline_path,
            root=visualization_root,
            label="visualization timeline",
            maximum_size=64 * 1024 * 1024,
        )
    records = _merge_retry_records(manifest, run_dir)

    manifest["status"] = "running"
    manifest["last_retry_started_at"] = now_iso()
    manifest["retry_count"] = (manifest.get("retry_count") or 0) + 1
    _sync_manifest(manifest_path, manifest, records)

    for ordinal in sorted(records):
        record = records[ordinal]
        if not _retry_candidate(record, visualization_root):
            continue

        previous_error = record.get("visualization_error")
        attempts = record["attempts"]
        attempt_number = len(attempts) + 1
        attempt: dict[str, Any] = {
            "attempt": attempt_number,
            "started_at": now_iso(),
            "previous_error": previous_error,
            "result": "running",
        }
        attempts.append(attempt)
        record["previous_error"] = previous_error
        record["visualization_status"] = "pending"
        record["visualization_error"] = None
        record["image"] = None
        _append_event(
            timeline_path,
            {
                "timestamp": attempt["started_at"],
                "event": "retry_started",
                "ordinal": ordinal,
                "step": record["id"],
                "attempt": attempt_number,
                "previous_error": previous_error,
            },
        )
        _sync_manifest(manifest_path, manifest, records)

        try:
            _render_step(
                record,
                run_id=args.run_id,
                runtime_image_ref=args.runtime_image_ref,
                design_dir=design_dir,
                input_snapshot_dir=args.input_snapshot_dir,
                run_dir=run_dir,
                visualization_root=visualization_root,
                pdk_root=args.pdk_root.resolve(),
                config_path=args.config,
                pdk=args.pdk,
                overrides=args.overrides,
                current_container_file=current_container_file,
                render_timeout_seconds=args.render_timeout_seconds,
            )
            if not _has_readable_image(record, visualization_root):
                raise RuntimeError(
                    "render attempt did not publish a valid in-scope PNG"
                )
            record["visualization_error"] = None
            attempt.update(
                {
                    "finished_at": now_iso(),
                    "result": "completed",
                    "image": record["image"],
                }
            )
            _append_event(
                timeline_path,
                {
                    "timestamp": attempt["finished_at"],
                    "event": "retry_completed",
                    "ordinal": ordinal,
                    "step": record["id"],
                    "attempt": attempt_number,
                    "image": record["image"],
                },
            )
        except Exception as exc:
            record["visualization_status"] = "failed"
            record["visualization_error"] = str(exc)
            attempt.update(
                {
                    "finished_at": now_iso(),
                    "result": "failed",
                    "error": record["visualization_error"],
                }
            )
            _append_event(
                timeline_path,
                {
                    "timestamp": attempt["finished_at"],
                    "event": "retry_failed",
                    "ordinal": ordinal,
                    "step": record["id"],
                    "attempt": attempt_number,
                    "previous_error": previous_error,
                    "error": record["visualization_error"],
                },
            )
        _sync_manifest(manifest_path, manifest, records)

    manifest["status"] = (
        "completed_with_errors"
        if any(
            record.get("visualization_status") == "failed"
            for record in records.values()
        )
        else "completed"
    )
    manifest["last_retry_finished_at"] = now_iso()
    _sync_manifest(manifest_path, manifest, records)
    return 1 if manifest["status"] == "completed_with_errors" else 0


def run_visualizer(args: argparse.Namespace) -> int:
    try:
        design_dir = canonical_directory(args.design_dir, label="design directory")
        input_snapshot_dir = canonical_directory(
            args.input_snapshot_dir, label="design input snapshot"
        )
        logical_config = args.config.expanduser()
        if not logical_config.is_absolute():
            logical_config = logical_config.absolute()
        config_relative = logical_config.relative_to(design_dir)
        config_path = canonical_regular_file(
            input_snapshot_dir / config_relative,
            root=input_snapshot_dir,
            label="LibreLane config",
            minimum_size=2,
            maximum_size=16 * 1024 * 1024,
        )
        pdk_root = canonical_directory(args.pdk_root, label="PDK root")
    except ValueError as exc:
        print(f"visualizer: identity rejected: {exc}", flush=True)
        return 1
    if args.image != LIBRELANE_IMAGE or args.librelane_version != LIBRELANE_VERSION:
        print(
            "visualizer: identity rejected: unverified LibreLane image identity",
            flush=True,
        )
        return 1
    for label, value in (
        ("config SHA-256", args.config_sha256),
        ("PDK integrity SHA-256", args.pdk_integrity_sha256),
        ("input bundle SHA-256", args.input_bundle_sha256),
    ):
        if not isinstance(value, str) or SHA256_RE.fullmatch(value) is None:
            print(f"visualizer: identity rejected: invalid {label}", flush=True)
            return 1
    if (
        not isinstance(args.image_digest, str)
        or IMAGE_DIGEST_RE.fullmatch(args.image_digest) is None
    ):
        print("visualizer: identity rejected: invalid image digest", flush=True)
        return 1
    if (
        not isinstance(args.runtime_image_ref, str)
        or IMAGE_DIGEST_RE.fullmatch(args.runtime_image_ref) is None
        or args.runtime_image_ref != args.image_digest
    ):
        print(
            "visualizer: identity rejected: runtime image digest mismatch", flush=True
        )
        return 1
    if (
        isinstance(args.main_pid, bool)
        or not isinstance(args.main_pid, int)
        or args.main_pid <= 0
        or isinstance(args.main_pid_start_time, bool)
        or not isinstance(args.main_pid_start_time, int)
        or args.main_pid_start_time <= 0
    ):
        print(
            "visualizer: identity rejected: invalid main process identity", flush=True
        )
        return 1
    if file_sha256(config_path) != args.config_sha256:
        print("visualizer: identity rejected: config content changed", flush=True)
        return 1
    if not isinstance(args.pdk_revision, str) or not args.pdk_revision:
        print("visualizer: identity rejected: missing PDK revision", flush=True)
        return 1
    expected_run = design_dir / "runs" / f"PI_LANE_{args.run_id}"
    supplied_run = args.run_dir.expanduser()
    if not supplied_run.is_absolute():
        supplied_run = supplied_run.absolute()
    if supplied_run != expected_run:
        print(
            "visualizer: identity rejected: run directory does not match run_id",
            flush=True,
        )
        return 1
    run_dir = expected_run
    args.config = logical_config
    args.input_snapshot_dir = input_snapshot_dir
    args.pdk_root = pdk_root
    while not run_dir.is_dir():
        if args.main_exit_file.is_file():
            print(
                f"visualizer: LibreLane run directory was not created: {run_dir}",
                flush=True,
            )
            return 1
        if not pid_identity_alive(args.main_pid, args.main_pid_start_time):
            print(
                "visualizer: LibreLane worker exited without an exit record",
                flush=True,
            )
            return 1
        time.sleep(args.poll_interval)
    try:
        runs_root = canonical_directory(design_dir / "runs", label="runs directory")
        run_dir = direct_child_directory(
            run_dir, root=runs_root, label="LibreLane run directory"
        )
    except ValueError as exc:
        print(f"visualizer: identity rejected: {exc}", flush=True)
        return 1
    visualization_root = run_dir / "visualizations"
    manifest_path = visualization_root / "manifest.json"
    timeline_path = visualization_root / "timeline.jsonl"
    current_container_file = visualization_root / "current_container"
    if getattr(args, "retry_failed", False):
        try:
            visualization_root = direct_child_directory(
                visualization_root,
                root=run_dir,
                label="visualization directory",
            )
            return _retry_failed_visualizations(
                args,
                run_dir=run_dir,
                design_dir=design_dir,
                visualization_root=visualization_root,
                manifest_path=manifest_path,
                timeline_path=timeline_path,
                current_container_file=current_container_file,
            )
        except (OSError, ValueError) as exc:
            print(f"visualizer: retry rejected: {exc}", flush=True)
            return 1
        finally:
            current_container_file.unlink(missing_ok=True)

    if visualization_root.exists():
        try:
            visualization_root = direct_child_directory(
                visualization_root, root=run_dir, label="visualization directory"
            )
        except ValueError as exc:
            print(f"visualizer: identity rejected: {exc}", flush=True)
            return 1
        if manifest_path.exists() or timeline_path.exists():
            print(
                "visualizer: existing visualization journal requires an explicit retry",
                flush=True,
            )
            return 1
    else:
        visualization_root.mkdir(mode=0o700)
    try:
        visualization_root = direct_child_directory(
            visualization_root, root=run_dir, label="visualization directory"
        )
    except ValueError as exc:
        print(f"visualizer: identity rejected: {exc}", flush=True)
        return 1
    identity = _identity_fields(args, run_dir=run_dir, design_dir=design_dir)
    manifest: dict[str, Any] = {
        "schema_version": 3,
        **identity,
        "status": "running",
        "directory": str(visualization_root),
        "manifest": str(manifest_path),
        "timeline": str(timeline_path),
        "started_at": now_iso(),
        "steps": [],
        "errors": [],
    }
    _write_manifest(manifest_path, manifest)
    records: dict[int, dict[str, Any]] = {}

    try:
        while True:
            discovered = discover_step_dirs(run_dir)
            for ordinal, step_dir in discovered:
                record = records.get(ordinal)
                if record is None:
                    record = step_execution_record(ordinal, step_dir)
                    records[ordinal] = record
                    _append_event(
                        timeline_path,
                        {
                            "timestamp": now_iso(),
                            "event": "step_started",
                            "ordinal": ordinal,
                            "step": record["id"],
                            "directory": record["directory"],
                            "commands": record["commands"],
                            "logs": record["logs"],
                        },
                    )
                if (
                    record["status"] != "completed"
                    and (step_dir / "state_out.json").is_file()
                ):
                    record.update(step_execution_record(ordinal, step_dir))
                if record["status"] != "completed" or record.get("completion_recorded"):
                    continue
                record["completion_recorded"] = True
                _append_event(
                    timeline_path,
                    {
                        "timestamp": now_iso(),
                        "event": "step_completed",
                        "ordinal": ordinal,
                        "step": record["id"],
                        "state_out": record["state_out"],
                        "commands": record["commands"],
                        "logs": record["logs"],
                    },
                )
                if record["preview_kind"]:
                    _append_event(
                        timeline_path,
                        {
                            "timestamp": now_iso(),
                            "event": "visualization_started",
                            "ordinal": ordinal,
                            "step": record["id"],
                        },
                    )
                    try:
                        _render_step(
                            record,
                            run_id=args.run_id,
                            runtime_image_ref=args.runtime_image_ref,
                            design_dir=design_dir,
                            input_snapshot_dir=input_snapshot_dir,
                            run_dir=run_dir,
                            visualization_root=visualization_root,
                            pdk_root=args.pdk_root.resolve(),
                            config_path=args.config,
                            pdk=args.pdk,
                            overrides=args.overrides,
                            current_container_file=current_container_file,
                            render_timeout_seconds=args.render_timeout_seconds,
                        )
                        _append_event(
                            timeline_path,
                            {
                                "timestamp": now_iso(),
                                "event": "visualization_completed",
                                "ordinal": ordinal,
                                "step": record["id"],
                                "image": record["image"],
                            },
                        )
                    except Exception as exc:
                        record["visualization_status"] = "failed"
                        record["visualization_error"] = str(exc)
                        manifest["errors"].append(
                            f"{record['id']}: {record['visualization_error']}"
                        )
                        _append_event(
                            timeline_path,
                            {
                                "timestamp": now_iso(),
                                "event": "visualization_failed",
                                "ordinal": ordinal,
                                "step": record["id"],
                                "error": record["visualization_error"],
                            },
                        )
                _sync_manifest(manifest_path, manifest, records)

            _sync_manifest(manifest_path, manifest, records)

            if args.main_exit_file.is_file():
                for record in records.values():
                    if record["status"] != "completed":
                        record["status"] = "incomplete"
                _sync_manifest(manifest_path, manifest, records)
                break
            if not pid_identity_alive(args.main_pid, args.main_pid_start_time):
                raise RuntimeError(
                    "LibreLane worker exited or its PID was reused without an exit record"
                )
            time.sleep(args.poll_interval)

        manifest["status"] = (
            "completed" if not manifest["errors"] else "completed_with_errors"
        )
        manifest["finished_at"] = now_iso()
        _write_manifest(manifest_path, manifest)
        return 0 if not manifest["errors"] else 1
    except Exception as exc:
        manifest["status"] = "failed"
        manifest["errors"].append(str(exc))
        manifest["finished_at"] = now_iso()
        _write_manifest(manifest_path, manifest)
        return 1
    finally:
        current_container_file.unlink(missing_ok=True)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run-id", required=True)
    parser.add_argument("--run-dir", type=Path, required=True)
    parser.add_argument("--design-dir", type=Path, required=True)
    parser.add_argument("--design-name", required=True)
    parser.add_argument("--config", type=Path, required=True)
    parser.add_argument("--input-snapshot-dir", type=Path, required=True)
    parser.add_argument("--input-bundle-sha256", required=True)
    parser.add_argument("--pdk", required=True)
    parser.add_argument("--pdk-root", type=Path, required=True)
    parser.add_argument("--image", required=True)
    parser.add_argument("--runtime-image-ref", required=True)
    parser.add_argument("--config-sha256", required=True)
    parser.add_argument("--pdk-revision", required=True)
    parser.add_argument("--pdk-integrity-sha256", required=True)
    parser.add_argument("--image-digest", required=True)
    parser.add_argument("--librelane-version", required=True)
    parser.add_argument("--main-exit-file", type=Path, required=True)
    parser.add_argument("--main-pid", type=int, required=True)
    parser.add_argument("--main-pid-start-time", type=int, required=True)
    parser.add_argument("--exit-file", type=Path, required=True)
    parser.add_argument(
        "--retry-failed",
        action="store_true",
        help="retry only failed, missing, or pending previews from an existing manifest",
    )
    parser.add_argument("--override", dest="overrides", action="append", default=[])
    parser.add_argument("--poll-interval", type=float, default=1.0)
    parser.add_argument(
        "--render-timeout-seconds",
        type=_render_timeout_seconds,
        default=DEFAULT_RENDER_TIMEOUT_SECONDS,
        help=(
            "maximum time for one KLayout.Render sidecar "
            f"({MIN_RENDER_TIMEOUT_SECONDS:g}-"
            f"{MAX_RENDER_TIMEOUT_SECONDS:g} seconds)"
        ),
    )
    return parser


def main() -> int:
    args = build_parser().parse_args()
    code = run_visualizer(args)
    atomic_write(args.exit_file, f"{code}\n")
    return code


if __name__ == "__main__":
    raise SystemExit(main())
