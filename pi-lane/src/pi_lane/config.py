"""Safe parsing of LibreLane design configurations and overrides."""

from __future__ import annotations

import hashlib
import json
import os
import re
import stat
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml

from .integrity import canonical_directory, canonical_regular_file, file_sha256


OPEN_PDKS = {
    "sky130A": "sky130",
    "gf180mcuD": "gf180mcu",
    "ihp-sg13g2": "ihp-sg13g2",
}
KEY_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
PROTECTED_OVERRIDE_KEYS = frozenset(
    {
        "DESIGN_NAME",
        "PDK",
        "PDK_ROOT",
        "STD_CELL_LIBRARY",
        "DEFAULT_CORNER",
        "VERILOG_FILES",
        "VHDL_FILES",
        "SDC_FILE",
        "SIGNOFF_SDC_FILE",
        "BASE_SDC_FILE",
        "FLOW",
        "FLOW_NAME",
        "STEPS",
        "SUBSTITUTING_STEPS",
    }
)
PROTECTED_OVERRIDE_PREFIXES = (
    "RUN_",
    "ERROR_ON_",
    "PDK_",
    "VERILOG_",
    "VHDL_",
    "SDC_",
    "SYNTH_",
    "CELL_",
    "TECH_",
    "LIB_",
)
FILE_INPUT_KEYS = frozenset(
    {
        "VERILOG_FILES",
        "VHDL_FILES",
        "SDC_FILE",
        "SIGNOFF_SDC_FILE",
        "BASE_SDC_FILE",
        "EQY_SCRIPT",
    }
)


@dataclass(frozen=True)
class DesignConfig:
    path: Path
    design_dir: Path
    design_name: str
    pdk: str
    flow: str
    sha256: str
    raw: dict[str, Any]
    source_bytes: bytes


def _read_regular_input(
    path: Path, *, root: Path, maximum_bytes: int
) -> tuple[Path, bytes]:
    """Open one in-tree regular file without following a last-moment symlink."""
    resolved_root = root.resolve(strict=True)
    resolved = path.resolve(strict=True)
    resolved.relative_to(resolved_root)
    flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
    descriptor = os.open(resolved, flags)
    try:
        metadata = os.fstat(descriptor)
        if not stat.S_ISREG(metadata.st_mode):
            raise ValueError(f"configuration input is not a regular file: {resolved}")
        if metadata.st_size > maximum_bytes:
            raise ValueError(
                f"configuration input exceeds {maximum_bytes // (1024 * 1024)} MiB: {resolved}"
            )
        # Linux exposes the object actually opened, closing the resolve/open race.
        actual = Path(os.readlink(f"/proc/self/fd/{descriptor}")).resolve(strict=True)
        actual.relative_to(resolved_root)
        if actual != resolved:
            raise ValueError(f"configuration input changed while being opened: {resolved}")
        content = os.read(descriptor, maximum_bytes + 1)
        chunks = [content]
        consumed = len(content)
        while content and consumed <= maximum_bytes:
            content = os.read(descriptor, min(1024 * 1024, maximum_bytes + 1 - consumed))
            if content:
                chunks.append(content)
                consumed += len(content)
        if consumed > maximum_bytes:
            raise ValueError(
                f"configuration input exceeds {maximum_bytes // (1024 * 1024)} MiB: {resolved}"
            )
        return resolved, b"".join(chunks)
    finally:
        os.close(descriptor)


def _input_strings(value: Any) -> list[str]:
    if isinstance(value, str):
        return [value]
    if isinstance(value, list):
        result: list[str] = []
        for item in value:
            result.extend(_input_strings(item))
        return result
    if isinstance(value, dict):
        result = []
        for item in value.values():
            result.extend(_input_strings(item))
        return result
    return []


def design_input_files(config: DesignConfig) -> tuple[Path, ...]:
    """Resolve every file input that must remain immutable during a Lane run."""
    candidates = {config.path}
    for key, value in config.raw.items():
        strings = _input_strings(value)
        if key not in FILE_INPUT_KEYS:
            strings = [item for item in strings if item.startswith("dir::")]
        for encoded in strings:
            if "\x00" in encoded or any(character in encoded for character in "\r\n"):
                raise ValueError(f"configuration input {encoded!r} is malformed")
            raw_path = encoded[5:] if encoded.startswith("dir::") else encoded
            path = Path(raw_path)
            candidate = path if path.is_absolute() else config.design_dir / path
            if candidate.is_symlink():
                raise ValueError(f"configuration input must not be a symlink: {candidate}")
            try:
                resolved = candidate.resolve(strict=True)
                resolved.relative_to(config.design_dir)
            except (OSError, ValueError) as exc:
                raise ValueError(
                    f"configuration input must be a regular file inside the design directory: {candidate}"
                ) from exc
            if not resolved.is_file():
                raise ValueError(f"configuration input is not a regular file: {resolved}")
            candidates.add(resolved)
    if config.raw.get("RUN_EQY") is True:
        script_reference = config.raw.get("EQY_SCRIPT")
        if isinstance(script_reference, str) and script_reference.startswith("dir::"):
            script_path = config.design_dir / script_reference.removeprefix("dir::")
            gate_path = script_path.with_name(f"{config.design_name}-gate.tcl")
            if gate_path.is_symlink():
                raise ValueError(f"configuration input must not be a symlink: {gate_path}")
            try:
                resolved_gate = gate_path.resolve(strict=True)
                resolved_gate.relative_to(config.design_dir)
            except (OSError, ValueError) as exc:
                raise ValueError(
                    f"configuration input must be a regular file inside the design directory: {gate_path}"
                ) from exc
            if not resolved_gate.is_file():
                raise ValueError(f"configuration input is not a regular file: {resolved_gate}")
            candidates.add(resolved_gate)
    return tuple(sorted(candidates, key=lambda path: path.relative_to(config.design_dir).as_posix()))


def capture_design_inputs(
    config: DesignConfig,
) -> tuple[dict[str, Any], dict[str, bytes]]:
    """Read each design input once and return the manifest plus captured bytes."""
    files: list[dict[str, Any]] = []
    captured: dict[str, bytes] = {}
    total_bytes = 0
    for path in design_input_files(config):
        relative = path.relative_to(config.design_dir).as_posix()
        if relative == "runs" or relative.startswith("runs/"):
            raise ValueError("configuration inputs must not be stored under the run output directory")
        if path == config.path:
            content = config.source_bytes
        else:
            _, content = _read_regular_input(
                path, root=config.design_dir, maximum_bytes=64 * 1024 * 1024
            )
        total_bytes += len(content)
        if total_bytes > 256 * 1024 * 1024:
            raise ValueError("configuration inputs exceed the 256 MiB snapshot limit")
        captured[relative] = content
        files.append(
            {
                "path": relative,
                "size_bytes": len(content),
                "sha256": hashlib.sha256(content).hexdigest(),
            }
        )
    bundle_bytes = json.dumps(
        files, sort_keys=True, separators=(",", ":"), ensure_ascii=True
    ).encode("ascii")
    manifest = {
        "schema_version": 2,
        "files": files,
        "bundle_sha256": hashlib.sha256(bundle_bytes).hexdigest(),
    }
    return manifest, captured


def design_input_manifest(config: DesignConfig) -> dict[str, Any]:
    return capture_design_inputs(config)[0]


def verify_design_input_snapshot(state: Any) -> list[str]:
    """Verify a schema-6 run's complete immutable design-input bundle."""

    errors: list[str] = []
    try:
        if not isinstance(state, dict):
            raise ValueError("managed state is not an object")
        controller_root = canonical_directory(
            Path(str(state["controller_dir"])), label="Lane controller directory"
        )
        snapshot_root = canonical_directory(
            Path(str(state["input_snapshot_dir"])), label="design input snapshot"
        )
        snapshot_root.relative_to(controller_root)
        manifest_file = canonical_regular_file(
            Path(str(state["input_manifest_path"])),
            root=controller_root,
            label="design input manifest",
            minimum_size=2,
            maximum_size=4 * 1024 * 1024,
        )
        manifest = json.loads(manifest_file.read_text(encoding="utf-8"))
        if not isinstance(manifest, dict) or manifest.get("schema_version") != 2:
            raise ValueError("design input manifest schema is incompatible")
        rows = manifest.get("files")
        if not isinstance(rows, list) or not rows:
            raise ValueError("design input manifest contains no files")
        if len(rows) > 4096:
            raise ValueError("design input manifest contains too many files")
        normalized: list[dict[str, Any]] = []
        seen_paths: set[str] = set()
        total_bytes = 0
        for row in rows:
            if not isinstance(row, dict):
                raise ValueError("design input manifest row is malformed")
            relative = row.get("path")
            if not isinstance(relative, str) or not relative or Path(relative).is_absolute():
                raise ValueError("design input manifest path is malformed")
            if relative in seen_paths:
                raise ValueError(f"design input manifest contains a duplicate path: {relative}")
            seen_paths.add(relative)
            candidate = canonical_regular_file(
                snapshot_root / relative,
                root=snapshot_root,
                label="snapshotted design input",
                minimum_size=0,
                maximum_size=64 * 1024 * 1024,
            )
            size = candidate.stat().st_size
            total_bytes += size
            if total_bytes > 256 * 1024 * 1024:
                raise ValueError("design input snapshot exceeds the 256 MiB limit")
            digest = file_sha256(candidate)
            if row.get("size_bytes") != size or row.get("sha256") != digest:
                raise ValueError(f"snapshotted design input changed: {relative}")
            normalized.append({"path": relative, "size_bytes": size, "sha256": digest})
        normalized.sort(key=lambda row: str(row["path"]))
        bundle = hashlib.sha256(
            json.dumps(
                normalized, sort_keys=True, separators=(",", ":"), ensure_ascii=True
            ).encode("ascii")
        ).hexdigest()
        if manifest.get("bundle_sha256") != bundle or state.get("input_bundle_sha256") != bundle:
            raise ValueError("design input bundle identity mismatch")
    except (KeyError, OSError, UnicodeError, ValueError, json.JSONDecodeError) as exc:
        errors.append(f"design input snapshot is invalid: {exc}")
    return errors


def load_design_config(
    path: Path,
    *,
    pdk_override: str | None = None,
    flow_override: str | None = None,
) -> DesignConfig:
    resolved = path.expanduser().resolve()
    if not resolved.is_file():
        raise FileNotFoundError(f"configuration not found: {resolved}")
    suffix = resolved.suffix.lower()
    if suffix not in {".json", ".yaml", ".yml"}:
        raise ValueError("pi-lane accepts only JSON or YAML configurations")
    try:
        _, source_bytes = _read_regular_input(
            resolved, root=resolved.parent, maximum_bytes=16 * 1024 * 1024
        )
        text = source_bytes.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise ValueError(f"configuration is not valid UTF-8: {exc}") from exc
    try:
        raw = json.loads(text) if suffix == ".json" else yaml.safe_load(text)
    except (json.JSONDecodeError, yaml.YAMLError) as exc:
        raise ValueError(f"invalid configuration: {exc}") from exc
    if not isinstance(raw, dict):
        raise ValueError("configuration root must be an object")

    design_name = raw.get("DESIGN_NAME")
    if (
        not isinstance(design_name, str)
        or len(design_name) > 128
        or not KEY_RE.fullmatch(design_name)
    ):
        raise ValueError("DESIGN_NAME must be a valid identifier of at most 128 characters")
    config_pdk = raw.get("PDK")
    if config_pdk is not None and not isinstance(config_pdk, str):
        raise ValueError("PDK must be a string")
    if config_pdk and pdk_override and config_pdk != pdk_override:
        raise ValueError(
            f"configuration PDK '{config_pdk}' conflicts with --pdk '{pdk_override}'"
        )
    pdk = config_pdk or pdk_override
    if pdk not in OPEN_PDKS:
        allowed = ", ".join(sorted(OPEN_PDKS))
        raise ValueError(f"PDK must be one of the reviewed open PDKs: {allowed}")

    meta = raw.get("meta") or {}
    if not isinstance(meta, dict):
        raise ValueError("meta must be an object")
    config_flow = meta.get("flow")
    if config_flow is not None and not isinstance(config_flow, str):
        raise ValueError("pi-lane requires meta.flow to be a registered flow name")
    if config_flow and flow_override and config_flow.lower() != flow_override.lower():
        raise ValueError(
            f"configuration flow '{config_flow}' conflicts with --flow '{flow_override}'"
        )
    flow = config_flow or flow_override or "Classic"
    if len(flow) > 64 or not re.fullmatch(r"[A-Za-z][A-Za-z0-9_]*", flow):
        raise ValueError("flow contains unsupported characters")

    return DesignConfig(
        path=resolved,
        design_dir=resolved.parent,
        design_name=design_name,
        pdk=pdk,
        flow=flow,
        sha256=hashlib.sha256(source_bytes).hexdigest(),
        raw=raw,
        source_bytes=source_bytes,
    )


def validate_override(value: str) -> str:
    if "=" not in value or any(character in value for character in "\r\n\0"):
        raise ValueError(f"invalid configuration override: {value!r}")
    key, encoded = value.split("=", 1)
    if not KEY_RE.fullmatch(key):
        raise ValueError(f"invalid configuration key: {key!r}")
    normalized = key.upper()
    if normalized in PROTECTED_OVERRIDE_KEYS or normalized.startswith(
        PROTECTED_OVERRIDE_PREFIXES
    ):
        raise ValueError(
            f"configuration override {key!r} is controller-managed and cannot be changed"
        )
    try:
        parsed = json.loads(encoded)
    except json.JSONDecodeError as exc:
        raise ValueError(
            f"override {key} must contain a JSON value; quote string values"
        ) from exc
    return f"{key}={json.dumps(parsed, separators=(',', ':'))}"
