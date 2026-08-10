"""Pinned-container configuration preflight without executing Flow Steps."""

from __future__ import annotations

import json
import os
import re
import subprocess
from pathlib import Path
from typing import Any, Callable

from .classic import CLASSIC_STEP_IDS
from .config import DesignConfig
from .integrity import file_sha256


PREFLIGHT_PREFIX = "PI_LANE_PREFLIGHT="
IMAGE_DIGEST_RE = re.compile(r"^sha256:[0-9a-f]{64}$")


def build_preflight_command(
    config: DesignConfig,
    *,
    image: str,
    runtime_image_ref: str,
    pdk_root: Path,
    overrides: tuple[str, ...] = (),
    input_snapshot_dir: Path | None = None,
) -> list[str]:
    if IMAGE_DIGEST_RE.fullmatch(runtime_image_ref) is None:
        raise ValueError("runtime image reference must be a sha256 image digest")
    script = """
import json
from librelane.flows import Flow

flow_type = Flow.factory.get("Classic")
if flow_type is None:
    raise RuntimeError("Classic flow is not registered")
flow = flow_type(
    CONFIG_PATH,
    pdk=PDK,
    pdk_root=PDK_ROOT,
    design_dir=DESIGN_DIR,
    config_override_strings=OVERRIDES,
)
step_ids = [step.id for step in flow.Steps]
print("PI_LANE_PREFLIGHT=" + json.dumps({
    "valid": True,
    "flow": flow.name,
    "design_name": str(flow.config["DESIGN_NAME"]),
    "pdk": str(flow.config["PDK"]),
    "step_count": len(step_ids),
    "step_ids": step_ids,
    "first_step": step_ids[0] if step_ids else None,
    "last_step": step_ids[-1] if step_ids else None,
}, sort_keys=True))
"""
    bootstrap = (
        f"CONFIG_PATH={str(config.path)!r};"
        f"PDK={config.pdk!r};"
        f"PDK_ROOT={str(pdk_root)!r};"
        f"DESIGN_DIR={str(config.design_dir)!r};"
        f"OVERRIDES={list(overrides)!r};"
        + script
    )
    return [
        "docker",
        "run",
        "--rm",
        "--init",
        "--label",
        f"io.pi-lane.image-tag={image}",
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
        "/tmp:rw,nosuid,nodev,size=256m",
        "-e",
        "HOME=/tmp",
        "-v",
        f"{input_snapshot_dir or config.design_dir}:{config.design_dir}:ro",
        "-v",
        f"{pdk_root}:{pdk_root}:ro",
        "-w",
        str(config.design_dir),
        "--entrypoint",
        "python3",
        runtime_image_ref,
        "-c",
        bootstrap,
    ]


def validate_config_in_image(
    config: DesignConfig,
    *,
    image: str,
    image_digest: str,
    pdk_root: Path,
    overrides: tuple[str, ...] = (),
    input_snapshot_dir: Path | None = None,
    timeout: int = 60,
    runner: Callable[..., subprocess.CompletedProcess[str]] = subprocess.run,
) -> dict[str, Any]:
    command = build_preflight_command(
        config,
        image=image,
        runtime_image_ref=image_digest,
        pdk_root=pdk_root,
        overrides=overrides,
        input_snapshot_dir=input_snapshot_dir,
    )
    try:
        completed = runner(
            command,
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
        )
    except subprocess.TimeoutExpired as exc:
        raise RuntimeError(f"LibreLane configuration preflight exceeded {timeout} seconds") from exc
    except OSError as exc:
        raise RuntimeError(f"LibreLane configuration preflight could not start: {exc}") from exc
    if completed.returncode != 0:
        detail = (completed.stderr or completed.stdout).strip()
        raise RuntimeError(
            "LibreLane configuration preflight failed in the pinned container: "
            + detail[-4000:]
        )
    matches = [
        line[len(PREFLIGHT_PREFIX) :]
        for line in completed.stdout.splitlines()
        if line.startswith(PREFLIGHT_PREFIX)
    ]
    if len(matches) != 1:
        raise RuntimeError("LibreLane configuration preflight returned an ambiguous verdict")
    try:
        verdict = json.loads(matches[0])
    except json.JSONDecodeError as exc:
        raise RuntimeError("LibreLane configuration preflight returned invalid JSON") from exc
    if (
        not isinstance(verdict, dict)
        or verdict.get("valid") is not True
        or str(verdict.get("design_name")) != config.design_name
        or str(verdict.get("pdk")) != config.pdk
        or str(verdict.get("flow")).casefold() != "classic"
        or verdict.get("step_count") != len(CLASSIC_STEP_IDS)
        or verdict.get("step_ids") != list(CLASSIC_STEP_IDS)
        or verdict.get("first_step") != CLASSIC_STEP_IDS[0]
        or verdict.get("last_step") != CLASSIC_STEP_IDS[-1]
    ):
        raise RuntimeError("LibreLane configuration preflight identity is invalid")
    observed_config = config.path
    if input_snapshot_dir is not None:
        observed_config = input_snapshot_dir / config.path.relative_to(config.design_dir)
    observed_config_sha256 = file_sha256(observed_config)
    if observed_config_sha256 != config.sha256:
        raise RuntimeError("LibreLane configuration changed during preflight")
    return {
        "schema_version": 1,
        "status": "pass",
        "provider": "pinned_librelane_config_loader",
        "image": image,
        "image_digest": image_digest,
        "runtime_image_ref": image_digest,
        "config_path": str(config.path),
        "config_sha256": observed_config_sha256,
        "design_dir": str(config.design_dir),
        "input_snapshot_dir": (
            str(input_snapshot_dir) if input_snapshot_dir is not None else None
        ),
        "pdk_root": str(pdk_root),
        "overrides": list(overrides),
        **verdict,
    }
