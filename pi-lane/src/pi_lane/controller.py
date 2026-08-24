"""Background LibreLane runner and filesystem-backed status discovery."""

from __future__ import annotations

import hashlib
import json
import math
import os
import re
import shutil
import signal
import stat
import subprocess
import sys
import time
from functools import wraps
from pathlib import Path
from typing import Any, Callable, Iterable, Mapping
from urllib.parse import urlsplit

from .assurance import evaluate_eqy_assurance, open_pdk_assurance
from .classic import (
    CLASSIC_REGISTRY_VERSION,
    CLASSIC_STEP_PHASE,
    CLASSIC_STEP_IDS,
    CLASSIC_TERMINAL_STEP,
    LIBRELANE_IMAGE,
    LIBRELANE_VERSION,
    validate_classic_range,
)
from .config import (
    DesignConfig,
    OPEN_PDKS,
    capture_design_inputs,
    load_design_config,
    validate_override,
    verify_design_input_snapshot,
)
from .integrity import (
    canonical_directory,
    canonical_regular_file,
    direct_child_directory,
    file_sha256,
)
from .preflight import validate_config_in_image
from .physical_plan import (
    bind_plan_to_run,
    geometry_drift_errors,
    load_plan,
    minimum_geometry_density_qualification,
    plan_overrides,
    validate_planning_overrides,
)
from .resume import (
    build_resume_plan,
    link_chain_proof,
    validate_initial_state,
    verify_resume_chain,
)
from .run_state import (
    RUN_STATE_SCHEMA_VERSION,
    RUN_ID_RE,
    STATE_ROOT,
    atomic_write,
    commit_state,
    elapsed_seconds,
    load_state,
    make_run_id,
    managed_state_revision,
    now_iso,
    pid_identity_alive,
    pid_start_time,
    read_event_journal,
    read_terminal_event,
    resolve_run_id,
    run_lock,
    save_state,
    set_current,
)


IMAGE = os.environ.get("PI_LANE_IMAGE", LIBRELANE_IMAGE)
PDK_ROOT = (
    Path(os.environ.get("PI_LANE_PDK_ROOT", "~/.ciel-pi-lane")).expanduser().resolve()
)
TERMINAL_STATES = {"completed", "failed", "incomplete", "stopped"}
ACTIVE_STATES = {"created", "queued", "launching", "running"}
LAUNCH_REGISTRATION_GRACE_SECONDS = 30.0
FLOW_ENDPOINTS = {
    "classic": ("Verilator.Lint", CLASSIC_TERMINAL_STEP),
}
# These are intentionally loose guardrails for catching grossly oversized
# floorplans without turning ordinary placement tuning into a hard failure.
PHYSICAL_SANITY_MIN_UTILIZATION_PCT = 10.0
PHYSICAL_SANITY_MAX_FILLER_RATIO_PCT = 90.0
PHYSICAL_SANITY_AREA_TOLERANCE_PCT = 5.0
METRIC_SENTINEL_ABS = 1e20
METRIC_UNITS = {
    "timing__setup__ws": "ns",
    "timing__setup__tns": "ns",
    "timing__hold__ws": "ns",
    "timing__hold__tns": "ns",
    "timing__setup_vio__count": "count",
    "timing__hold_vio__count": "count",
    "design__max_slew_violation__count": "count",
    "design__max_cap_violation__count": "count",
    "design__instance__count__class:timing_repair_buffer": "count",
    "route__wirelength": "um",
    "global_route__wirelength": "um",
    "route__wirelength__estimated": "um",
    "design__instance__area": "um2",
    "design__instance__area__stdcell": "um2",
    "design__core__area": "um2",
    "design__instance__area__class:fill_cell": "um2",
    "design__instance__area__class:tap_cell": "um2",
    "design__instance__utilization": "ratio",
    "design__instance__utilization__stdcell": "ratio",
    "power__total": "W",
}
STEP_DIR_RE = re.compile(r"^(\d+)-(.+)$")
STEP_ID_NORMALIZE_RE = re.compile(r"[^a-z0-9]")
ANSI_ESCAPE_RE = re.compile(r"\x1b\[[0-?]*[ -/]*[@-~]")
ERROR_RE = re.compile(
    r"(?:\[ERROR|\]\s+ERROR\b|\bERROR\s*[:\[]|\bError:|Traceback \(most recent call last\))"
)
INSTALL_ERROR_RE = re.compile(
    r"(?:\berror\b|\bfailed\b|\bdenied\b|\bunauthorized\b|\bnot found\b)",
    re.IGNORECASE,
)
INSTALL_PROGRESS_RE = re.compile(r"\bnot found locally, attempting\b", re.IGNORECASE)
IMAGE_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._/:@-]{0,511}$")
IMAGE_DIGEST_RE = re.compile(r"^sha256:[0-9a-f]{64}$")
PDK_INTEGRITY_SCHEMA_VERSION = 1
PDK_REVISIONS = {
    "sky130A": ("sky130", "8afc8346a57fe1ab7934ba5a6056ea8b43078e71"),
    "gf180mcuD": ("gf180mcu", "54435919abffb937387ec956209f9cf5fd2dfbee"),
    "ihp-sg13g2": ("ihp-sg13g2", "c4b8b4e5e7a05f375cca3815d51b3a37721fbf5c"),
}
PDK_INSTALL_ROOT = STATE_ROOT / "pdk-installs"
PDK_INSTALL_CURRENT = PDK_INSTALL_ROOT / "current"
IMAGE_INSTALL_ROOT = STATE_ROOT / "image-installs"
IMAGE_INSTALL_CURRENT = IMAGE_INSTALL_ROOT / "current"
IMAGE_LOCK_SCHEMA_VERSION = 1
EQY_TOOL_PATH = Path(__file__).with_name("eqy_tool.py").resolve()
EQY_TOOL_MAKE_PATH = "/root/.nix-profile/bin/make"
EQY_TOOL_AWK_PATH = "/root/.nix-profile/bin/awk"

# Stable fields exposed as the completed Lane delivery.  They are copied into
# the terminal attestation so public status never has to trust mutable state
# pointers after completion.  Visualization evidence remains a late sidecar
# and is intentionally excluded from the sealed artifact map.
SEALED_PUBLIC_DELIVERY_FIELDS = (
    "run_id",
    "workflow_id",
    "attempt_id",
    "origin",
    "status",
    "process_status",
    "failure_kind",
    "failure",
    "delivery_gate",
    "delivery_gate_reasons",
    "flow_scope",
    "expected_final_step",
    "endpoint_reached",
    "last_completed_step",
    "completed_step_count",
    "started_step_count",
    "failed_step",
    "metrics_path",
    "qor",
    "mapped_equivalence",
    "assurance",
    "config_path",
    "config_sha256",
    "input_snapshot_dir",
    "input_manifest_path",
    "input_bundle_sha256",
    "design_dir",
    "design_name",
    "flow",
    "flow_registry_version",
    "librelane_run_dir",
    "image",
    "image_digest",
    "runtime_image_ref",
    "librelane_version",
    "pdk",
    "pdk_root",
    "pdk_revision",
    "pdk_integrity_sha256",
    "artifacts",
    "canonical_gds",
    "runtime_identity_attested",
    "runtime_environment_attestation",
    "status_detail",
    "coarse_phase",
    "errors",
    "physical_plan",
)
INTEGRITY_DEGRADATION_FIELDS = frozenset(
    {
        "status",
        "failure_kind",
        "failure",
        "delivery_gate",
        "delivery_gate_reasons",
        "status_detail",
        "coarse_phase",
    }
)

PROXY_ENV_NAMES = (
    "HTTP_PROXY",
    "HTTPS_PROXY",
    "ALL_PROXY",
    "NO_PROXY",
    "http_proxy",
    "https_proxy",
    "all_proxy",
    "no_proxy",
)

MANAGED_RUNNER_SCRIPT = r"""set +e
attempt=0
while [ ! -f "$PI_LANE_HANDOFF_FILE" ] && [ "$attempt" -lt 300 ]; do
    sleep 0.1
    attempt=$((attempt + 1))
done
if [ ! -f "$PI_LANE_HANDOFF_FILE" ]; then
    code=125
else
    "$@"
    code=$?
fi
tmp="${PI_LANE_EXIT_FILE}.tmp.$$"
printf "%s\n" "$code" > "$tmp"
mv "$tmp" "$PI_LANE_EXIT_FILE"
exit "$code"
"""


def _serialized_start(
    function: Callable[..., dict[str, Any]],
) -> Callable[..., dict[str, Any]]:
    """Serialize launch registration decisions under one controller start lock."""

    @wraps(function)
    def locked_start(*args: Any, **kwargs: Any) -> dict[str, Any]:
        STATE_ROOT.mkdir(parents=True, exist_ok=True, mode=0o700)
        with run_lock(STATE_ROOT / ".start-state.json"):
            return function(*args, **kwargs)

    return locked_start


def validate_runtime_image_ref(image: str) -> str:
    if not isinstance(image, str) or IMAGE_DIGEST_RE.fullmatch(image) is None:
        raise ValueError("runtime image reference must be a sha256 image digest")
    return image


def _inspectable_image_ref(image: str) -> str:
    if isinstance(image, str) and IMAGE_DIGEST_RE.fullmatch(image) is not None:
        return image
    return validate_image_name(image)


def image_id(image: str = IMAGE) -> str | None:
    image = _inspectable_image_ref(image)
    try:
        completed = subprocess.run(
            ["docker", "image", "inspect", image, "--format", "{{.Id}}"],
            capture_output=True,
            text=True,
            check=False,
        )
    except OSError:
        return None
    return completed.stdout.strip() or None if completed.returncode == 0 else None


def image_librelane_version(image: str = IMAGE) -> str | None:
    """Read the package version from the fixed image without network access."""
    image = _inspectable_image_ref(image)
    try:
        completed = subprocess.run(
            [
                "docker",
                "run",
                "--rm",
                "--network",
                "none",
                "--entrypoint",
                "python3",
                image,
                "-c",
                "import librelane; print(librelane.__version__)",
            ],
            capture_output=True,
            text=True,
            check=False,
            timeout=30,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    value = completed.stdout.strip()
    return value if completed.returncode == 0 and value else None


def pdk_installed(pdk: str, pdk_root: Path = PDK_ROOT) -> bool:
    root = pdk_root.resolve()
    pdk_path = pdk_root / pdk
    try:
        pdk_path.resolve().relative_to(root)
    except (OSError, ValueError):
        return False
    tech_root = pdk_path / "libs.tech"
    return any(
        (tech_root / layout / "config.tcl").is_file()
        for layout in ("librelane", "openlane")
    )


def _pdk_version_path(pdk: str, pdk_root: Path, revision: str) -> Path:
    family, _ = PDK_REVISIONS[pdk]
    return pdk_root / "ciel" / family / "versions" / revision / pdk


def _pdk_integrity_manifest_path(pdk: str, pdk_root: Path, revision: str) -> Path:
    return pdk_root / ".pi-lane-integrity" / f"{pdk}-{revision}.json"


def _pdk_tree_digest(version_path: Path) -> tuple[str, int, int]:
    """Hash all PDK regular-file contents and relative paths without following links."""
    root = canonical_directory(version_path, label="PDK version directory")
    digest = hashlib.sha256()
    file_count = 0
    total_bytes = 0
    for directory, names, files in os.walk(root, topdown=True, followlinks=False):
        directory_path = Path(directory)
        for name in sorted(names):
            child = directory_path / name
            mode = os.lstat(child).st_mode
            if stat.S_ISLNK(mode) or not stat.S_ISDIR(mode):
                raise ValueError(
                    f"PDK contains an unsupported directory entry: {child}"
                )
        names.sort()
        for name in sorted(files):
            child = directory_path / name
            mode = os.lstat(child).st_mode
            if not stat.S_ISREG(mode):
                raise ValueError(f"PDK contains a non-regular file: {child}")
            relative = child.relative_to(root).as_posix().encode("utf-8")
            digest.update(len(relative).to_bytes(4, "big"))
            digest.update(relative)
            size = child.stat().st_size
            digest.update(size.to_bytes(8, "big"))
            with child.open("rb") as stream:
                for chunk in iter(lambda: stream.read(1024 * 1024), b""):
                    digest.update(chunk)
            file_count += 1
            total_bytes += size
    if file_count == 0:
        raise ValueError("PDK version directory contains no regular files")
    return digest.hexdigest(), file_count, total_bytes


def _pdk_bootstrap_is_safe(version_path: Path, revision: str) -> bool:
    try:
        root = canonical_directory(version_path, label="PDK version directory")
        sources = canonical_regular_file(
            root / "SOURCES",
            root=root,
            label="PDK SOURCES",
            minimum_size=1,
            maximum_size=64 * 1024,
        )
        if revision not in sources.read_text(encoding="utf-8", errors="strict").split():
            return False
        for directory, names, files in os.walk(root, followlinks=False):
            for name in [*names, *files]:
                mode = os.lstat(Path(directory) / name).st_mode
                if stat.S_ISLNK(mode) or mode & 0o022:
                    return False
    except (OSError, UnicodeError, ValueError):
        return False
    return True


def pdk_integrity_report(
    pdk: str,
    pdk_root: Path = PDK_ROOT,
    *,
    allow_bootstrap: bool = False,
    verify_content: bool = True,
) -> dict[str, Any]:
    report: dict[str, Any] = {
        "schema_version": PDK_INTEGRITY_SCHEMA_VERSION,
        "pdk": pdk,
        "ready": False,
        "bootstrapped": False,
        "tree_sha256": None,
        "content_verified": False,
        "manifest": None,
        "error": None,
    }
    if pdk not in PDK_REVISIONS:
        report["error"] = "unsupported PDK"
        return report
    root = pdk_root.expanduser().resolve()
    revision = installed_pdk_revision(pdk, root)
    expected_revision = PDK_REVISIONS[pdk][1]
    if revision != expected_revision or not pdk_installed(pdk, root):
        report["error"] = "PDK is not enabled at the pinned revision"
        return report
    version_path = _pdk_version_path(pdk, root, revision)
    manifest_path = _pdk_integrity_manifest_path(pdk, root, revision)
    report["manifest"] = str(manifest_path)
    try:
        if manifest_path.exists() and manifest_path.is_symlink():
            raise ValueError("PDK integrity manifest must not be a symlink")
        if not manifest_path.exists():
            if not allow_bootstrap or not _pdk_bootstrap_is_safe(
                version_path, revision
            ):
                raise ValueError(
                    "PDK integrity manifest is missing and safe bootstrap attestation failed"
                )
            tree_sha256, file_count, total_bytes = _pdk_tree_digest(version_path)
            manifest = {
                "schema_version": PDK_INTEGRITY_SCHEMA_VERSION,
                "pdk": pdk,
                "revision": revision,
                "version_path": str(version_path.resolve(strict=True)),
                "tree_sha256": tree_sha256,
                "file_count": file_count,
                "total_bytes": total_bytes,
            }
            manifest_path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
            atomic_write(
                manifest_path,
                json.dumps(manifest, indent=2, sort_keys=True) + "\n",
            )
            report["bootstrapped"] = True
        manifest_file = canonical_regular_file(
            manifest_path,
            root=root,
            label="PDK integrity manifest",
            minimum_size=1,
            maximum_size=1024 * 1024,
        )
        manifest = json.loads(manifest_file.read_text(encoding="utf-8"))
        if not isinstance(manifest, dict):
            raise ValueError("PDK integrity manifest is not an object")
        expected_identity = {
            "schema_version": PDK_INTEGRITY_SCHEMA_VERSION,
            "pdk": pdk,
            "revision": revision,
            "version_path": str(version_path.resolve(strict=True)),
        }
        for key, expected in expected_identity.items():
            if manifest.get(key) != expected:
                raise ValueError(f"PDK integrity manifest {key} mismatch")
        recorded_sha256 = manifest.get("tree_sha256")
        file_count = manifest.get("file_count")
        total_bytes = manifest.get("total_bytes")
        if (
            not isinstance(recorded_sha256, str)
            or re.fullmatch(r"[0-9a-f]{64}", recorded_sha256) is None
            or isinstance(file_count, bool)
            or not isinstance(file_count, int)
            or file_count < 1
            or isinstance(total_bytes, bool)
            or not isinstance(total_bytes, int)
            or total_bytes < 1
        ):
            raise ValueError("PDK integrity manifest measurements are malformed")
        if verify_content:
            actual_sha256, actual_file_count, actual_total_bytes = _pdk_tree_digest(
                version_path
            )
            if (
                recorded_sha256 != actual_sha256
                or file_count != actual_file_count
                or total_bytes != actual_total_bytes
            ):
                raise ValueError(
                    "PDK content changed after integrity manifest creation"
                )
        report.update(
            {
                "ready": True,
                "tree_sha256": recorded_sha256,
                "file_count": file_count,
                "total_bytes": total_bytes,
                "content_verified": verify_content,
            }
        )
    except (OSError, UnicodeError, ValueError, json.JSONDecodeError) as exc:
        report["error"] = str(exc)
    return report


def installed_pdk_revision(pdk: str, pdk_root: Path = PDK_ROOT) -> str | None:
    """Return the Ciel revision only when the enabled PDK path matches it exactly."""
    if pdk not in PDK_REVISIONS:
        return None
    family, _ = PDK_REVISIONS[pdk]
    root = pdk_root.expanduser().resolve()
    current = root / "ciel" / family / "current"
    try:
        revision = current.read_text(encoding="ascii").strip()
    except (OSError, UnicodeError):
        return None
    if not re.fullmatch(r"[0-9a-f]{40}", revision):
        return None
    expected_path = root / "ciel" / family / "versions" / revision / pdk
    try:
        enabled_path = (root / pdk).resolve(strict=True)
        version_path = expected_path.resolve(strict=True)
        enabled_path.relative_to(root)
    except (OSError, ValueError):
        return None
    return revision if enabled_path == version_path else None


def pdk_ready(pdk: str, pdk_root: Path = PDK_ROOT) -> bool:
    return bool(pdk_integrity_report(pdk, pdk_root, allow_bootstrap=True)["ready"])


def validate_image_name(image: str) -> str:
    if not isinstance(image, str) or not IMAGE_RE.fullmatch(image):
        raise ValueError("image must be a valid OCI image reference")
    if image != LIBRELANE_IMAGE:
        raise ValueError(
            f"pi-lane Classic registry requires the fixed LibreLane image {LIBRELANE_IMAGE}"
        )
    return image


def _image_lock_path() -> Path:
    return IMAGE_INSTALL_ROOT / "image-lock.json"


def _image_lock_digest(record: Mapping[str, Any]) -> str:
    payload = dict(record)
    payload.pop("lock_sha256", None)
    encoded = json.dumps(
        payload, sort_keys=True, separators=(",", ":"), ensure_ascii=True
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _validate_image_lock(record: Any) -> dict[str, Any]:
    if not isinstance(record, Mapping):
        raise RuntimeError("LibreLane image lock is not a JSON object")
    value = dict(record)
    revision = value.get("revision")
    if (
        value.get("schema_version") != IMAGE_LOCK_SCHEMA_VERSION
        or isinstance(revision, bool)
        or not isinstance(revision, int)
        or revision < 1
    ):
        raise RuntimeError("LibreLane image lock schema or revision is malformed")
    try:
        validate_image_name(value.get("image"))
    except ValueError as exc:
        raise RuntimeError(
            f"LibreLane image lock identity is malformed: {exc}"
        ) from exc
    if (
        not isinstance(value.get("image_digest"), str)
        or IMAGE_DIGEST_RE.fullmatch(value["image_digest"]) is None
        or value.get("librelane_version") != LIBRELANE_VERSION
        or not isinstance(value.get("install_run_id"), str)
        or RUN_ID_RE.fullmatch(value["install_run_id"]) is None
    ):
        raise RuntimeError("LibreLane image lock content is malformed")
    previous = value.get("previous_lock_sha256")
    if previous is not None and (
        not isinstance(previous, str) or re.fullmatch(r"[0-9a-f]{64}", previous) is None
    ):
        raise RuntimeError("LibreLane image lock predecessor is malformed")
    expected_hash = _image_lock_digest(value)
    if value.get("lock_sha256") != expected_hash:
        raise RuntimeError("LibreLane image lock digest does not match its content")
    return value


def load_image_lock(*, required: bool = True) -> dict[str, Any] | None:
    path = _image_lock_path()
    try:
        metadata = path.lstat()
    except FileNotFoundError:
        if required:
            raise RuntimeError(
                "LibreLane image is not locked; run lane-agent image-install"
            )
        return None
    if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISREG(metadata.st_mode):
        raise RuntimeError("LibreLane image lock must be a regular non-symlink file")
    if metadata.st_uid != os.geteuid() or stat.S_IMODE(metadata.st_mode) & 0o077:
        raise RuntimeError("LibreLane image lock must be owner-only")
    try:
        record = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise RuntimeError(f"LibreLane image lock cannot be read: {exc}") from exc
    return _validate_image_lock(record)


def _record_image_lock(state: Mapping[str, Any]) -> dict[str, Any]:
    with run_lock(IMAGE_INSTALL_ROOT / ".image-lock-state.json"):
        return _record_image_lock_unlocked(state)


def _record_image_lock_unlocked(state: Mapping[str, Any]) -> dict[str, Any]:
    if state.get("status") != "completed":
        raise RuntimeError(
            "only a completed image installation can update the image lock"
        )
    image = validate_image_name(str(state.get("image") or ""))
    expected_digest = state.get("expected_digest")
    if expected_digest is not None and (
        not isinstance(expected_digest, str)
        or IMAGE_DIGEST_RE.fullmatch(expected_digest) is None
    ):
        raise RuntimeError("image installation expected digest is malformed")
    observed_digest = image_id(image)
    if observed_digest is None or observed_digest != state.get("image_digest"):
        raise RuntimeError("installed image identity changed before lock attestation")
    observed_version = image_librelane_version(observed_digest)
    if observed_version != LIBRELANE_VERSION:
        raise RuntimeError(
            f"LibreLane image version mismatch: expected {LIBRELANE_VERSION}, "
            f"found {observed_version or 'unavailable'}"
        )
    if expected_digest is not None and observed_digest != expected_digest:
        raise RuntimeError(
            "installed image digest does not match --expected-digest: "
            f"expected {expected_digest}, found {observed_digest}"
        )

    previous = load_image_lock(required=False)
    if previous is not None and (
        previous["image"] == image
        and previous["image_digest"] == observed_digest
        and previous["librelane_version"] == observed_version
    ):
        return previous
    replace_lock = state.get("replace_lock") is True
    if previous is not None and not replace_lock:
        raise RuntimeError(
            "installed image digest differs from the persistent lock; approve an exact "
            "replacement with lane-agent image-install --replace-lock "
            f"--expected-digest {observed_digest}"
        )
    if previous is not None and expected_digest != observed_digest:
        raise RuntimeError(
            "replacing the image lock requires --expected-digest to match the installed digest"
        )

    record: dict[str, Any] = {
        "schema_version": IMAGE_LOCK_SCHEMA_VERSION,
        "revision": int(previous["revision"]) + 1 if previous is not None else 1,
        "image": image,
        "image_digest": observed_digest,
        "librelane_version": observed_version,
        "install_run_id": state["run_id"],
        "locked_at": now_iso(),
        "previous_lock_sha256": (
            previous["lock_sha256"] if previous is not None else None
        ),
    }
    record["lock_sha256"] = _image_lock_digest(record)
    atomic_write(
        _image_lock_path(),
        json.dumps(record, indent=2, sort_keys=True) + "\n",
        mode=0o400,
    )
    persisted = load_image_lock(required=True)
    if persisted != record:
        raise RuntimeError(
            "persisted LibreLane image lock did not verify after writing"
        )
    return record


def require_locked_image(image: str = IMAGE) -> dict[str, Any]:
    image = validate_image_name(image)
    lock = load_image_lock(required=True)
    assert lock is not None
    if lock["image"] != image:
        raise RuntimeError(
            "requested LibreLane image does not match the persistent lock"
        )
    observed_digest = image_id(image)
    if observed_digest is None:
        raise RuntimeError(
            f"LibreLane image is unavailable: {image}; run lane-agent image-install"
        )
    if observed_digest != lock["image_digest"]:
        raise RuntimeError(
            "installed LibreLane image digest differs from the persistent lock; "
            "run lane-agent image-install and explicitly approve any replacement"
        )
    observed_version = image_librelane_version(observed_digest)
    if observed_version != lock["librelane_version"]:
        raise RuntimeError("installed LibreLane image version differs from its lock")
    return {
        **lock,
        "lock_path": str(_image_lock_path()),
    }


def image_lock_report(image: str = IMAGE) -> dict[str, Any]:
    try:
        identity = require_locked_image(image)
    except (RuntimeError, ValueError) as exc:
        return {
            "ready": False,
            "path": str(_image_lock_path()),
            "error": str(exc),
        }
    return {"ready": True, "path": str(_image_lock_path()), **identity}


def validate_pdk_root(pdk_root: Path) -> Path:
    resolved = pdk_root.expanduser().resolve()
    forbidden = {Path("/"), Path.home().resolve(), (Path.home() / ".ciel").resolve()}
    if resolved in forbidden:
        raise ValueError("PDK root must be a dedicated Pi Lane directory")
    return resolved


def pdk_proxy_environment(
    source: Mapping[str, str] | None = None,
) -> dict[str, str]:
    values = os.environ if source is None else source
    proxies: dict[str, str] = {}
    for name in PROXY_ENV_NAMES:
        value = values.get(name)
        if not value:
            continue
        if name.lower() == "no_proxy":
            proxies[name] = value
            continue
        if urlsplit(value).scheme.lower() in {"http", "https", "socks5"}:
            proxies[name] = value
    return proxies


def pdk_proxy_uses_loopback(proxy_env: Mapping[str, str]) -> bool:
    for name, value in proxy_env.items():
        if name.lower() == "no_proxy":
            continue
        parsed = urlsplit(value if "://" in value else f"//{value}")
        if parsed.hostname in {"localhost", "127.0.0.1", "::1"}:
            return True
    return False


def _managed_install_id(root: Path, label: str) -> str:
    stamp = time.strftime("%Y%m%d-%H%M%S")
    slug = re.sub(r"[^A-Za-z0-9_.-]+", "-", label).strip("-").lower()
    base = f"{stamp}-{slug}"
    run_id = base
    index = 2
    while (root / run_id).exists():
        run_id = f"{base}-{index}"
        index += 1
    return run_id


def _install_errors(log_path: Path, *, failed: bool) -> list[str]:
    lines = [
        ANSI_ESCAPE_RE.sub("", line).strip()
        for line in _tail_lines(log_path, 80)
        if ANSI_ESCAPE_RE.sub("", line).strip()
    ]
    errors = [
        line
        for line in lines
        if INSTALL_ERROR_RE.search(line) and not INSTALL_PROGRESS_RE.search(line)
    ]
    if failed and not errors:
        errors = lines[-8:]
    return errors[-8:]


def _load_image_install(run_id: str | None = None) -> dict[str, Any]:
    if run_id is None:
        if not IMAGE_INSTALL_CURRENT.is_file():
            raise RuntimeError("no managed image installation exists")
        run_id = IMAGE_INSTALL_CURRENT.read_text(encoding="utf-8").strip()
    run_id = resolve_run_id(run_id)
    path = IMAGE_INSTALL_ROOT / run_id / "state.json"
    if not path.is_file():
        raise RuntimeError(f"image installation does not exist: {run_id}")
    return json.loads(path.read_text(encoding="utf-8"))


def refresh_image_install(state: dict[str, Any]) -> dict[str, Any]:
    state = dict(state)
    expected_start = state.get("pid_start_time")
    running = pid_identity_alive(state.get("pid"), expected_start)
    exit_code = _read_exit_code(Path(state["exit_file"]))
    if (
        state.get("status") == "launching"
        and state.get("pid") is None
        and exit_code is None
        and elapsed_seconds(state) < 35
    ):
        state["is_running"] = False
        state["elapsed_seconds"] = elapsed_seconds(state)
        return state
    if (
        state.get("status") in {"completed", "failed", "stopped"}
        and state.get("installation_attested") is True
        and exit_code is not None
        and not running
    ):
        state["is_running"] = False
        state["elapsed_seconds"] = elapsed_seconds(state)
        return state
    installed_digest = image_id(state["image"])
    installed_version = (
        image_librelane_version(state["image"])
        if installed_digest is not None
        else None
    )
    if state.get("stop_requested") and not running:
        status = "stopped"
    elif exit_code is not None:
        status = (
            "completed"
            if exit_code == 0
            and installed_digest
            and installed_version == LIBRELANE_VERSION
            else "failed"
        )
    elif running:
        status = "running"
    else:
        status = "failed"
    _freeze_elapsed(state, status, Path(state["exit_file"]))
    state.update(
        {
            "status": status,
            "is_running": running,
            "exit_code": exit_code,
            "installed": installed_digest is not None,
            "image_digest": installed_digest,
            "librelane_version": installed_version,
            "elapsed_seconds": elapsed_seconds(state),
            "errors": _install_errors(
                Path(state["build_log"]), failed=status == "failed"
            ),
        }
    )
    if status in {"completed", "failed", "stopped"}:
        state["installation_attested"] = True
    return state


def _attest_image_install_lock(state: dict[str, Any]) -> dict[str, Any]:
    if state.get("status") != "completed" or state.get("image_lock_attested") is True:
        return state
    try:
        lock = _record_image_lock(state)
    except (RuntimeError, ValueError, KeyError) as exc:
        message = f"image lock attestation failed: {exc}"
        state["status"] = "failed"
        state["image_lock_attested"] = False
        state["image_lock_error"] = message
        state["errors"] = [*(state.get("errors") or []), message][-8:]
        return state
    state.update(
        {
            "image_lock_attested": True,
            "image_lock_path": str(_image_lock_path()),
            "image_lock_sha256": lock["lock_sha256"],
            "image_lock_revision": lock["revision"],
        }
    )
    return state


def _record_install_launch_failure(
    state: dict[str, Any], *, message: str, exit_code: int = 125
) -> None:
    """Best-effort terminal evidence when an installer cannot be registered."""
    try:
        atomic_write(Path(state["exit_file"]), f"{exit_code}\n")
    except (KeyError, OSError):
        pass
    state.update(
        {
            "status": "failed",
            "is_running": False,
            "exit_code": exit_code,
            "finished_at": now_iso(),
            "finished_epoch": time.time(),
            "elapsed_seconds": elapsed_seconds(state),
            "errors": [message],
            "installation_attested": True,
        }
    )
    try:
        save_state(state)
    except Exception:
        pass


@_serialized_start
def start_image_install(
    image: str = IMAGE,
    *,
    expected_digest: str | None = None,
    replace_lock: bool = False,
) -> dict[str, Any]:
    image = validate_image_name(image)
    if expected_digest is not None and (
        not isinstance(expected_digest, str)
        or IMAGE_DIGEST_RE.fullmatch(expected_digest) is None
    ):
        raise ValueError("expected image digest must use sha256:<64 lowercase hex>")
    if replace_lock and expected_digest is None:
        raise ValueError("--replace-lock requires --expected-digest")
    if IMAGE_INSTALL_CURRENT.is_file():
        try:
            active = refresh_image_install(_load_image_install())
            if active["status"] in {"launching", "running"}:
                raise RuntimeError(
                    f"an image installation is already active: {active['run_id']}"
                )
        except (OSError, ValueError, KeyError, json.JSONDecodeError):
            pass
    run_id = _managed_install_id(IMAGE_INSTALL_ROOT, "image-install")
    controller_dir = IMAGE_INSTALL_ROOT / run_id
    controller_dir.mkdir(parents=True, mode=0o700)
    exit_file = controller_dir / "exit_code"
    handoff_file = controller_dir / "launch_handoff"
    build_log = controller_dir / "install.log"
    state_file = controller_dir / "state.json"
    state: dict[str, Any] = {
        "schema_version": 1,
        "kind": "image_install",
        "run_id": run_id,
        "state_file": str(state_file),
        "build_log": str(build_log),
        "exit_file": str(exit_file),
        "handoff_file": str(handoff_file),
        "pid": None,
        "pid_start_time": None,
        "status": "launching",
        "image": image,
        "image_digest": image_id(image),
        "expected_digest": expected_digest,
        "replace_lock": replace_lock,
        "image_lock_attested": False,
        "started_at": now_iso(),
        "started_epoch": time.time(),
        "stop_requested": False,
    }
    save_state(state)
    atomic_write(IMAGE_INSTALL_CURRENT, run_id + "\n")
    output = build_log.open("ab", buffering=0)
    process: subprocess.Popen[bytes] | None = None
    try:
        process = subprocess.Popen(
            [
                "/bin/bash",
                "-c",
                MANAGED_RUNNER_SCRIPT,
                "pi-lane-image",
                "docker",
                "pull",
                image,
            ],
            env={
                "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
                "PI_LANE_EXIT_FILE": str(exit_file),
                "PI_LANE_HANDOFF_FILE": str(handoff_file),
            },
            stdin=subprocess.DEVNULL,
            stdout=output,
            stderr=subprocess.STDOUT,
            start_new_session=True,
        )
        start_time = pid_start_time(process.pid)
        if start_time is None or start_time <= 0:
            raise RuntimeError("image installer process start time is unavailable")
        state.update(
            {
                "pid": process.pid,
                "pid_start_time": start_time,
                "status": "running",
                "registered_at": now_iso(),
            }
        )
        save_state(state)
        atomic_write(handoff_file, "ready\n")
    except Exception as exc:
        if process is not None:
            _terminate_spawned_process(process.pid)
        message = f"image installer registration failed: {exc}"
        try:
            output.write((message + "\n").encode("utf-8", errors="replace"))
        except OSError:
            pass
        _record_install_launch_failure(state, message=message)
        raise
    finally:
        output.close()
    refreshed = _attest_image_install_lock(refresh_image_install(state))
    if refreshed != state and refreshed.get("status") in {
        "completed",
        "failed",
        "stopped",
    }:
        save_state(refreshed)
    return refreshed


def image_install_status(run_id: str | None = None) -> dict[str, Any]:
    original = _load_image_install(run_id)
    refreshed = refresh_image_install(original)
    refreshed = _attest_image_install_lock(refreshed)
    if refreshed != original and refreshed.get("status") in {
        "completed",
        "failed",
        "stopped",
    }:
        save_state(refreshed)
    return refreshed


def stop_image_install(run_id: str | None = None) -> dict[str, Any]:
    state = refresh_image_install(_load_image_install(run_id))
    if state.get("status") in {"completed", "failed", "stopped"}:
        return state
    expected_start = state.get("pid_start_time")
    if not isinstance(expected_start, int) or isinstance(expected_start, bool):
        raise RuntimeError(
            "refusing to stop an image install without a persisted PID identity"
        )
    state["stop_requested"] = True
    if pid_identity_alive(state.get("pid"), expected_start):
        try:
            os.killpg(int(state["pid"]), signal.SIGTERM)
        except ProcessLookupError:
            pass
    state["status"] = "stopped"
    save_state(state)
    return refresh_image_install(state)


def build_pdk_install_command(
    pdk: str,
    *,
    pdk_root: Path,
    run_id: str,
    image: str = IMAGE,
    runtime_image_ref: str,
    proxy_env: Mapping[str, str] | None = None,
) -> tuple[list[str], str]:
    image = validate_image_name(image)
    runtime_image_ref = validate_runtime_image_ref(runtime_image_ref)
    if pdk not in PDK_REVISIONS:
        raise ValueError(f"unsupported PDK: {pdk}")
    family, revision = PDK_REVISIONS[pdk]
    container_name = f"pi-lane-pdk-{run_id}"
    proxies = pdk_proxy_environment(proxy_env)
    command = [
        "docker",
        "run",
        "--rm",
        "--init",
        "--name",
        container_name,
        "--label",
        f"io.pi-lane.pdk-install-id={run_id}",
        "--label",
        f"io.pi-lane.image-tag={image}",
        "--label",
        f"io.pi-lane.image-digest={runtime_image_ref}",
        "--read-only",
        "--cap-drop",
        "ALL",
        "--security-opt",
        "no-new-privileges",
        "--user",
        f"{os.getuid()}:{os.getgid()}",
        "--tmpfs",
        "/tmp:rw,nosuid,nodev,size=2g",
    ]
    if pdk_proxy_uses_loopback(proxies):
        command.extend(["--network", "host"])
    for name in proxies:
        command.extend(["-e", name])
    command.extend(
        [
            "-e",
            "HOME=/tmp",
            "-v",
            f"{pdk_root}:{pdk_root}:rw",
            runtime_image_ref,
            "ciel",
            "enable",
            "--pdk",
            family,
            "--pdk-root",
            str(pdk_root),
            revision,
        ]
    )
    return command, container_name


def _load_pdk_install(run_id: str | None = None) -> dict[str, Any]:
    if run_id is None:
        if not PDK_INSTALL_CURRENT.is_file():
            raise RuntimeError("no managed PDK installation exists")
        run_id = PDK_INSTALL_CURRENT.read_text(encoding="utf-8").strip()
    run_id = resolve_run_id(run_id)
    path = PDK_INSTALL_ROOT / run_id / "state.json"
    if not path.is_file():
        raise RuntimeError(f"PDK installation does not exist: {run_id}")
    return json.loads(path.read_text(encoding="utf-8"))


def refresh_pdk_install(state: dict[str, Any]) -> dict[str, Any]:
    state = dict(state)
    expected_start = state.get("pid_start_time")
    running = pid_identity_alive(state.get("pid"), expected_start)
    exit_code = _read_exit_code(Path(state["exit_file"]))
    if (
        state.get("status") == "launching"
        and state.get("pid") is None
        and exit_code is None
        and elapsed_seconds(state) < 35
    ):
        state["is_running"] = False
        state["elapsed_seconds"] = elapsed_seconds(state)
        return state
    if (
        state.get("status") in {"completed", "failed", "stopped"}
        and state.get("installation_attested") is True
        and exit_code is not None
        and not running
    ):
        state["is_running"] = False
        state["elapsed_seconds"] = elapsed_seconds(state)
        return state
    full_verify = exit_code == 0 and state.get("installation_attested") is not True
    integrity = pdk_integrity_report(
        state["pdk"],
        Path(state["pdk_root"]),
        allow_bootstrap=full_verify,
        verify_content=full_verify,
    )
    installed = bool(integrity["ready"])
    if state.get("stop_requested") and not running:
        status = "stopped"
    elif exit_code is not None:
        status = "completed" if exit_code == 0 and installed else "failed"
    elif running:
        status = "running"
    else:
        status = "failed"
    _freeze_elapsed(state, status, Path(state["exit_file"]))
    state.update(
        {
            "status": status,
            "is_running": running,
            "exit_code": exit_code,
            "installed": installed,
            "integrity": integrity,
            "elapsed_seconds": elapsed_seconds(state),
            "errors": _install_errors(
                Path(state["build_log"]), failed=status == "failed"
            ),
        }
    )
    if status in {"completed", "failed", "stopped"}:
        state["installation_attested"] = True
    return state


@_serialized_start
def start_pdk_install(
    pdk: str,
    *,
    pdk_root: Path = PDK_ROOT,
    image: str = IMAGE,
) -> dict[str, Any]:
    image = validate_image_name(image)
    if pdk not in PDK_REVISIONS:
        raise ValueError(f"unsupported PDK: {pdk}")
    image_lock = require_locked_image(image)
    image_digest = str(image_lock["image_digest"])
    image_version = str(image_lock["librelane_version"])
    if PDK_INSTALL_CURRENT.is_file():
        try:
            active = refresh_pdk_install(_load_pdk_install())
            if active["status"] in {"launching", "running"}:
                raise RuntimeError(
                    f"a PDK installation is already active: {active['run_id']}"
                )
        except (OSError, ValueError, KeyError, json.JSONDecodeError):
            pass

    pdk_root = validate_pdk_root(pdk_root)
    pdk_root.mkdir(parents=True, exist_ok=True, mode=0o700)
    run_id = _managed_install_id(PDK_INSTALL_ROOT, f"pdk-{pdk}")
    controller_dir = PDK_INSTALL_ROOT / run_id
    controller_dir.mkdir(parents=True, mode=0o700)
    exit_file = controller_dir / "exit_code"
    handoff_file = controller_dir / "launch_handoff"
    build_log = controller_dir / "install.log"
    state_file = controller_dir / "state.json"
    proxy_env = pdk_proxy_environment()
    command, container_name = build_pdk_install_command(
        pdk,
        pdk_root=pdk_root,
        run_id=run_id,
        image=image,
        runtime_image_ref=image_digest,
        proxy_env=proxy_env,
    )
    family, revision = PDK_REVISIONS[pdk]
    state: dict[str, Any] = {
        "schema_version": 1,
        "kind": "pdk_install",
        "run_id": run_id,
        "state_file": str(state_file),
        "build_log": str(build_log),
        "exit_file": str(exit_file),
        "handoff_file": str(handoff_file),
        "pid": None,
        "pid_start_time": None,
        "container_name": container_name,
        "status": "launching",
        "pdk": pdk,
        "pdk_family": family,
        "revision": revision,
        "pdk_root": str(pdk_root),
        "image": image,
        "image_digest": image_digest,
        "runtime_image_ref": image_digest,
        "image_lock_path": image_lock["lock_path"],
        "image_lock_sha256": image_lock["lock_sha256"],
        "image_lock_revision": image_lock["revision"],
        "librelane_version": image_version,
        "network_mode": "host" if pdk_proxy_uses_loopback(proxy_env) else "bridge",
        "proxy_environment": sorted(proxy_env),
        "started_at": now_iso(),
        "started_epoch": time.time(),
        "stop_requested": False,
    }
    save_state(state)
    atomic_write(PDK_INSTALL_CURRENT, run_id + "\n")
    output = build_log.open("ab", buffering=0)
    process_env = {
        "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
        "PI_LANE_EXIT_FILE": str(exit_file),
        "PI_LANE_HANDOFF_FILE": str(handoff_file),
        **proxy_env,
    }
    process: subprocess.Popen[bytes] | None = None
    try:
        process = subprocess.Popen(
            ["/bin/bash", "-c", MANAGED_RUNNER_SCRIPT, "pi-lane-pdk", *command],
            env=process_env,
            stdin=subprocess.DEVNULL,
            stdout=output,
            stderr=subprocess.STDOUT,
            start_new_session=True,
        )
        start_time = pid_start_time(process.pid)
        if start_time is None or start_time <= 0:
            raise RuntimeError("PDK installer process start time is unavailable")
        state.update(
            {
                "pid": process.pid,
                "pid_start_time": start_time,
                "status": "running",
                "registered_at": now_iso(),
            }
        )
        save_state(state)
        atomic_write(handoff_file, "ready\n")
    except Exception as exc:
        if process is not None:
            _terminate_spawned_process(process.pid, container_name=container_name)
        message = f"PDK installer registration failed: {exc}"
        try:
            output.write((message + "\n").encode("utf-8", errors="replace"))
        except OSError:
            pass
        _record_install_launch_failure(state, message=message)
        raise
    finally:
        output.close()
    return refresh_pdk_install(state)


def pdk_install_status(run_id: str | None = None) -> dict[str, Any]:
    original = _load_pdk_install(run_id)
    refreshed = refresh_pdk_install(original)
    if refreshed != original and refreshed.get("status") in {
        "completed",
        "failed",
        "stopped",
    }:
        save_state(refreshed)
    return refreshed


def stop_pdk_install(run_id: str | None = None) -> dict[str, Any]:
    state = refresh_pdk_install(_load_pdk_install(run_id))
    if state.get("status") in {"completed", "failed", "stopped"}:
        return state
    expected_start = state.get("pid_start_time")
    if not isinstance(expected_start, int) or isinstance(expected_start, bool):
        raise RuntimeError(
            "refusing to stop a PDK install without a persisted PID identity"
        )
    state["stop_requested"] = True
    if state.get("container_name"):
        subprocess.run(
            ["docker", "stop", "--time", "8", str(state["container_name"])],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            check=False,
        )
    if pid_identity_alive(state.get("pid"), expected_start):
        try:
            os.killpg(int(state["pid"]), signal.SIGTERM)
        except ProcessLookupError:
            pass
    state["status"] = "stopped"
    save_state(state)
    return refresh_pdk_install(state)


def active_duplicate(
    config: DesignConfig, *, input_bundle_sha256: str | None = None
) -> dict[str, Any] | None:
    if not STATE_ROOT.is_dir():
        return None
    for state_file in STATE_ROOT.glob("*/state.json"):
        try:
            state = refresh_status(json.loads(state_file.read_text(encoding="utf-8")))
        except (OSError, RuntimeError, ValueError, KeyError, json.JSONDecodeError):
            continue
        if (
            state.get("status") == "running"
            and state.get("config_sha256") == config.sha256
            and state.get("pdk") == config.pdk
            and (
                input_bundle_sha256 is None
                or state.get("input_bundle_sha256") == input_bundle_sha256
            )
        ):
            return state
    return None


def _expected_eqy_gate_tcl() -> str:
    return """set run_dir $::env(PI_LANE_RUN_DIR)
set formal_models [glob -nocomplain [file join $run_dir *-yosys-eqy formal_pdk.v]]
set mapped_netlists [glob -nocomplain [file join $run_dir *-openroad-fillinsertion *.nl.v]]
if {[llength $formal_models] != 1} {
    error "expected exactly one Yosys.EQY formal_pdk.v in current run"
}
if {[llength $mapped_netlists] != 1} {
    error "expected exactly one post-FillInsertion mapped netlist in current run"
}
yosys read_verilog -formal -sv [lindex $formal_models 0] [lindex $mapped_netlists 0]
"""


def _expected_eqy_script(design_name: str) -> str:
    return f"""[gold]
read_verilog -formal -sv src/{design_name}.sv

[gate]
tcl eqy/{design_name}-gate.tcl

[script]
hierarchy -top {design_name}
proc
prep -top {design_name} -flatten
memory -nomap
memory_map
async2sync

[partition {design_name}]
name whole_design .*

[strategy sat]
use sat
depth 16

[strategy pdr]
use sby
engine abc pdr -rfi
"""


def _eqy_runtime_inputs(config: DesignConfig) -> dict[str, Any] | None:
    if config.raw.get("RUN_EQY", False) is not True:
        return None
    script_reference = config.raw.get("EQY_SCRIPT")
    if not isinstance(script_reference, str) or not script_reference.startswith(
        "dir::"
    ):
        raise ValueError("RUN_EQY requires a design-relative EQY_SCRIPT")
    relative_script = script_reference.removeprefix("dir::")
    if not relative_script or Path(relative_script).is_absolute():
        raise ValueError("EQY_SCRIPT must be a non-empty design-relative path")
    script_path = canonical_regular_file(
        config.design_dir / relative_script,
        root=config.design_dir,
        label="EQY script",
        minimum_size=1,
        maximum_size=1024 * 1024,
    )
    expected_script_sha256 = hashlib.sha256(
        _expected_eqy_script(config.design_name).encode("utf-8")
    ).hexdigest()
    if file_sha256(script_path) != expected_script_sha256:
        raise ValueError("EQY_SCRIPT does not match the controller-reviewed template")
    gate_tcl_path = canonical_regular_file(
        script_path.with_name(f"{config.design_name}-gate.tcl"),
        root=config.design_dir,
        label="EQY gate Tcl",
        minimum_size=1,
        maximum_size=1024 * 1024,
    )
    expected_gate_tcl_sha256 = hashlib.sha256(
        _expected_eqy_gate_tcl().encode("utf-8")
    ).hexdigest()
    if file_sha256(gate_tcl_path) != expected_gate_tcl_sha256:
        raise ValueError("EQY gate Tcl does not match the controller-reviewed template")
    tool_path = canonical_regular_file(
        EQY_TOOL_PATH,
        root=EQY_TOOL_PATH.parent,
        label="restricted EQY tool",
        minimum_size=1,
        maximum_size=1024 * 1024,
    )
    return {
        "script_path": script_path,
        "script_sha256": expected_script_sha256,
        "gate_tcl_path": gate_tcl_path,
        "gate_tcl_sha256": expected_gate_tcl_sha256,
        "tool_path": tool_path,
        "tool_sha256": file_sha256(tool_path),
    }


def _materialize_eqy_tool(runtime: Mapping[str, Any], controller_dir: Path) -> Path:
    """Create the executable run-local EQY launcher from packaged source bytes."""

    source = canonical_regular_file(
        Path(str(runtime["tool_path"])),
        root=Path(str(runtime["tool_path"])).parent,
        label="packaged restricted EQY tool",
        minimum_size=1,
        maximum_size=1024 * 1024,
    )
    expected_sha256 = runtime.get("tool_sha256")
    payload = source.read_bytes()
    if (
        not isinstance(expected_sha256, str)
        or not re.fullmatch(r"[0-9a-f]{64}", expected_sha256)
        or hashlib.sha256(payload).hexdigest() != expected_sha256
    ):
        raise ValueError("packaged restricted EQY tool changed before materialization")
    target = controller_dir / "eqy-tool"
    descriptor: int | None = None
    created = False
    try:
        descriptor = os.open(
            target,
            os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0),
            0o555,
        )
        created = True
        remaining = memoryview(payload)
        while remaining:
            written = os.write(descriptor, remaining)
            if written <= 0:
                raise OSError("short write while materializing restricted EQY tool")
            remaining = remaining[written:]
        os.fchmod(descriptor, 0o555)
        os.fsync(descriptor)
        os.close(descriptor)
        descriptor = None
        directory = os.open(controller_dir, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
        materialized = canonical_regular_file(
            target,
            root=controller_dir,
            label="run-local restricted EQY tool",
            minimum_size=1,
            maximum_size=1024 * 1024,
        )
        if (
            not os.access(materialized, os.X_OK)
            or file_sha256(materialized) != expected_sha256
        ):
            raise ValueError(
                "run-local restricted EQY tool is not executable or changed"
            )
        return materialized
    except BaseException:
        if descriptor is not None:
            os.close(descriptor)
        if created:
            target.unlink(missing_ok=True)
        raise


def build_docker_command(
    config: DesignConfig,
    *,
    run_id: str,
    tag: str,
    pdk_root: Path,
    overrides: Iterable[str],
    frm: str | None,
    to: str | None,
    jobs: int | None,
    image: str = IMAGE,
    runtime_image_ref: str,
    initial_state: Path | None = None,
    eqy_tool_path: Path | None = None,
    workflow_id: str | None = None,
    attempt_id: str | None = None,
    input_snapshot_dir: Path | None = None,
    pinned_file_mounts: Mapping[Path, Path] | None = None,
) -> tuple[list[str], str]:
    image = validate_image_name(image)
    runtime_image_ref = validate_runtime_image_ref(runtime_image_ref)
    if (
        not isinstance(run_id, str)
        or RUN_ID_RE.fullmatch(run_id) is None
        or tag != f"PI_LANE_{run_id}"
    ):
        raise ValueError("container run tag must be the exact PI_LANE_<run-id> path")
    container_name = f"pi-lane-{run_id}"
    command = [
        "docker",
        "run",
        "--rm",
        "--init",
        "--name",
        container_name,
        "--label",
        f"io.pi-lane.run-id={run_id}",
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
        "/tmp:rw,nosuid,nodev,size=2g",
        "-e",
        "HOME=/tmp",
        "-e",
        f"PI_LANE_RUN_DIR={config.design_dir / 'runs' / tag}",
        "-e",
        f"PDK_ROOT={pdk_root}",
        "-v",
        f"{input_snapshot_dir or config.design_dir}:{config.design_dir}:ro",
        "-v",
        f"{config.design_dir / 'runs' / tag}:{config.design_dir / 'runs' / tag}:rw",
        "-v",
        f"{pdk_root}:{pdk_root}:ro",
        "-w",
        str(config.design_dir),
    ]
    if workflow_id is not None:
        command.extend(["--label", f"io.pi-lane.workflow-id={workflow_id}"])
    if attempt_id is not None:
        command.extend(["--label", f"io.pi-lane.attempt-id={attempt_id}"])
    if eqy_tool_path is not None:
        command.extend(
            [
                "-v",
                f"{eqy_tool_path}:{EQY_TOOL_MAKE_PATH}:ro",
                "-v",
                f"{eqy_tool_path}:{EQY_TOOL_AWK_PATH}:ro",
            ]
        )
    for source, target in sorted(
        (pinned_file_mounts or {}).items(), key=lambda item: str(item[1])
    ):
        command.extend(["-v", f"{source}:{target}:ro"])
    command.extend(
        [
            runtime_image_ref,
            "python3",
            "-m",
            "librelane",
            "--manual-pdk",
            "--pdk-root",
            str(pdk_root),
            "--pdk",
            config.pdk,
            "--flow",
            config.flow,
            "--run-tag",
            tag,
            "--hide-progress-bar",
        ]
    )
    if jobs is not None:
        command.extend(["--jobs", str(jobs)])
    if initial_state is not None:
        command.extend(["--with-initial-state", str(initial_state)])
    if frm:
        command.extend(["--from", frm])
    if to:
        command.extend(["--to", to])
    for override in overrides:
        command.extend(["--override-config", override])
    command.append(str(config.path))
    return command, container_name


TRACKING_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:/-]{0,254}$")


def _validate_tracking_id(value: str | None, *, label: str) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str) or not TRACKING_ID_RE.fullmatch(value):
        raise ValueError(f"{label} is malformed")
    return value



def _start_request_fingerprint(
    config: DesignConfig,
    *,
    image: str,
    overrides: list[str],
    frm: str | None,
    to: str | None,
    jobs: int | None,
    parent_run_id: str | None,
    initial_state_sha256: str | None,
    input_bundle_sha256: str,
    physical_plan_sha256: str | None = None,
    planning_probe_sha256: str | None = None,
    visualization_enabled: bool = True,
) -> str:
    request = {
        "config_sha256": config.sha256,
        "pdk": config.pdk,
        "flow": config.flow,
        "image": image,
        "overrides": overrides,
        "from": frm,
        "to": to,
        "jobs": jobs,
        "parent_run_id": parent_run_id,
        "initial_state_sha256": initial_state_sha256,
        "input_bundle_sha256": input_bundle_sha256,
        "physical_plan_sha256": physical_plan_sha256,
        "planning_probe_sha256": planning_probe_sha256,
        "visualization_enabled": visualization_enabled,
    }
    return hashlib.sha256(
        json.dumps(request, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()


def _idempotent_run(
    idempotency_key: str | None, request_fingerprint: str
) -> dict[str, Any] | None:
    if idempotency_key is None or not STATE_ROOT.is_dir():
        return None
    for path in STATE_ROOT.glob("*/state.json"):
        try:
            state = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        if (
            not isinstance(state, dict)
            or state.get("idempotency_key") != idempotency_key
        ):
            continue
        if state.get("request_fingerprint") != request_fingerprint:
            raise RuntimeError(
                "idempotency key was already used for a different Lane request"
            )
        return refresh_status(state)
    return None


def _materialize_design_snapshot(
    snapshot_root: Path,
    *,
    manifest: Mapping[str, Any],
    captured: Mapping[str, bytes],
    run_tag: str | None = None,
    extra_run_tags: Iterable[str] = (),
) -> None:
    if snapshot_root.exists():
        raise RuntimeError(f"design input snapshot already exists: {snapshot_root}")
    snapshot_root.mkdir(parents=True, mode=0o700)
    for row in manifest.get("files") or []:
        if not isinstance(row, Mapping) or not isinstance(row.get("path"), str):
            raise RuntimeError("design input manifest is malformed")
        relative = str(row["path"])
        content = captured.get(relative)
        if content is None or hashlib.sha256(content).hexdigest() != row.get("sha256"):
            raise RuntimeError(
                f"captured design input does not match its manifest: {relative}"
            )
        destination = snapshot_root / relative
        destination.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        destination.write_bytes(content)
        destination.chmod(0o400)
    (snapshot_root / "runs").mkdir(mode=0o700)
    run_tags = list(extra_run_tags)
    if run_tag is not None:
        run_tags.append(run_tag)
    for tag in run_tags:
        if not isinstance(tag, str) or not RUN_ID_RE.fullmatch(tag):
            raise RuntimeError("design input snapshot run tag is malformed")
        (snapshot_root / "runs" / tag).mkdir(mode=0o700)
    for directory in sorted(
        (path for path in snapshot_root.rglob("*") if path.is_dir()),
        key=lambda path: len(path.parts),
        reverse=True,
    ):
        directory.chmod(0o500)
    snapshot_root.chmod(0o500)


def _verify_design_snapshot(state: Mapping[str, Any]) -> list[str]:
    errors = verify_design_input_snapshot(dict(state))
    try:
        if state.get("initial_state") is not None:
            controller_root = canonical_directory(
                Path(str(state["controller_dir"])), label="Lane controller directory"
            )
            resume_snapshot = canonical_regular_file(
                Path(str(state.get("initial_state_snapshot") or "")),
                root=controller_root,
                label="resume input snapshot",
                minimum_size=1,
                maximum_size=2 * 1024 * 1024 * 1024,
            )
            if file_sha256(resume_snapshot) != state.get("initial_state_sha256"):
                raise ValueError("resume input snapshot identity mismatch")
    except (KeyError, OSError, ValueError) as exc:
        errors.append(f"design input snapshot is invalid: {exc}")
    return errors


def build_visualizer_command(
    *,
    run_id: str,
    run_dir: Path,
    design_dir: Path,
    design_name: str,
    config_path: Path,
    input_snapshot_dir: Path,
    input_bundle_sha256: str,
    pdk: str,
    pdk_root: Path,
    image: str,
    runtime_image_ref: str,
    config_sha256: str,
    pdk_revision: str,
    pdk_integrity_sha256: str,
    image_digest: str,
    librelane_version: str,
    main_exit_file: Path,
    main_pid: int,
    main_pid_start_time: int,
    visualizer_exit_file: Path,
    overrides: Iterable[str],
    retry_failed: bool = False,
) -> list[str]:
    command = [
        sys.executable,
        "-m",
        "pi_lane.visualizer",
        "--run-id",
        run_id,
        "--run-dir",
        str(run_dir),
        "--design-dir",
        str(design_dir),
        "--design-name",
        design_name,
        "--config",
        str(config_path),
        "--input-snapshot-dir",
        str(input_snapshot_dir),
        "--input-bundle-sha256",
        input_bundle_sha256,
        "--pdk",
        pdk,
        "--pdk-root",
        str(pdk_root),
        "--image",
        image,
        "--runtime-image-ref",
        runtime_image_ref,
        "--config-sha256",
        config_sha256,
        "--pdk-revision",
        pdk_revision,
        "--pdk-integrity-sha256",
        pdk_integrity_sha256,
        "--image-digest",
        image_digest,
        "--librelane-version",
        librelane_version,
        "--main-exit-file",
        str(main_exit_file),
        "--main-pid",
        str(main_pid),
        "--main-pid-start-time",
        str(main_pid_start_time),
        "--exit-file",
        str(visualizer_exit_file),
    ]
    if retry_failed:
        command.append("--retry-failed")
    for override in overrides:
        command.extend(["--override", override])
    return command


def _terminate_spawned_process(pid: int, *, container_name: str | None = None) -> None:
    try:
        os.killpg(pid, signal.SIGTERM)
    except OSError:
        pass
    if container_name is not None:
        try:
            subprocess.run(
                ["docker", "stop", "--time", "8", container_name],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                check=False,
                timeout=15,
            )
        except (OSError, subprocess.TimeoutExpired):
            pass


def _spawn_visualizer(
    command: list[str], *, cwd: Path, log_path: Path
) -> tuple[int, int] | None:
    output = log_path.open("ab", buffering=0)
    try:
        process = subprocess.Popen(
            command,
            cwd=cwd,
            env={"PATH": os.environ.get("PATH", "/usr/bin:/bin")},
            stdin=subprocess.DEVNULL,
            stdout=output,
            stderr=subprocess.STDOUT,
            start_new_session=True,
        )
        start_time = pid_start_time(process.pid)
        if start_time is None or start_time <= 0:
            _terminate_spawned_process(process.pid)
            output.write(
                b"visualizer launch failed: process start time is unavailable\n"
            )
            return None
        return process.pid, start_time
    except OSError as exc:
        output.write(f"visualizer launch failed: {exc}\n".encode("utf-8"))
        return None
    finally:
        output.close()


@_serialized_start
def start_run(
    config_path: Path,
    *,
    pdk: str | None = None,
    flow: str | None = None,
    pdk_root: Path = PDK_ROOT,
    overrides: Iterable[str] = (),
    frm: str | None = None,
    to: str | None = None,
    jobs: int | None = None,
    allow_concurrent: bool = False,
    image: str = IMAGE,
    initial_state: Path | None = None,
    initial_state_sha256: str | None = None,
    parent_run_id: str | None = None,
    resume_root_run_id: str | None = None,
    resume_chain_depth: int | None = None,
    resume_parent_state_attestation_sha256: str | None = None,
    resume_parent_chain_proof_sha256: str | None = None,
    workflow_id: str | None = None,
    attempt_id: str | None = None,
    idempotency_key: str | None = None,
    origin: str = "lane-agent",
    expected_input_bundle_sha256: str | None = None,
    physical_plan_path: Path | None = None,
    planning_probe: Mapping[str, Any] | None = None,
    visualization_enabled: bool = True,
    claim_current: bool = True,
) -> dict[str, Any]:
    config = load_design_config(config_path, pdk_override=pdk, flow_override=flow)
    input_manifest, captured_inputs = capture_design_inputs(config)
    if expected_input_bundle_sha256 is not None:
        if re.fullmatch(r"[0-9a-f]{64}", expected_input_bundle_sha256) is None:
            raise ValueError("expected design input bundle SHA-256 is malformed")
        if input_manifest["bundle_sha256"] != expected_input_bundle_sha256:
            raise ValueError("design inputs changed since the source run")
    image = validate_image_name(image)
    if config.flow.casefold() != "classic":
        raise ValueError(
            "pi-lane executes only the pinned LibreLane 3.0.5 Classic flow registry"
        )
    run_eqy_requested = config.raw.get("RUN_EQY", False)
    if not isinstance(run_eqy_requested, bool):
        raise ValueError("RUN_EQY must be boolean")
    if run_eqy_requested and config.pdk != "sky130A":
        raise ValueError("RUN_EQY assurance is currently supported only for sky130A")
    eqy_runtime = _eqy_runtime_inputs(config) if run_eqy_requested else None
    if jobs is not None and not 1 <= jobs <= 256:
        raise ValueError("jobs must be from 1 to 256")
    frm, to = validate_classic_range(frm, to)
    if frm is not None and initial_state is None:
        raise ValueError(
            "--from requires a validated prior State; use `lane-agent resume`"
        )
    if initial_state is not None and (frm is None or parent_run_id is None):
        raise ValueError("initial State requires a parent run and an exact --from Step")
    if (
        initial_state is not None
        and planning_probe is None
        and (
            not resume_root_run_id
            or isinstance(resume_chain_depth, bool)
            or not isinstance(resume_chain_depth, int)
            or resume_chain_depth < 1
            or not isinstance(resume_parent_state_attestation_sha256, str)
            or not re.fullmatch(r"[0-9a-f]{64}", resume_parent_state_attestation_sha256)
            or not isinstance(resume_parent_chain_proof_sha256, str)
            or not re.fullmatch(r"[0-9a-f]{64}", resume_parent_chain_proof_sha256)
        )
    ):
        raise ValueError("resume chain provenance is incomplete")
    if planning_probe is not None:
        if physical_plan_path is not None:
            raise ValueError(
                "a planning probe cannot consume a completed physical plan"
            )
        if initial_state is None or frm is None or parent_run_id is None:
            raise ValueError(
                "planning probe requires a parent baseline State and --from"
            )
        required_probe = {
            "schema_version",
            "plan_id",
            "candidate_id",
            "policy_sha256",
            "parent_run_id",
            "parent_run_dir",
            "baseline_state_sha256",
        }
        if (
            set(planning_probe) != required_probe
            or planning_probe.get("schema_version") != 1
        ):
            raise ValueError("planning probe provenance is malformed")
        if planning_probe.get("parent_run_id") != parent_run_id:
            raise ValueError("planning probe parent identity is inconsistent")
        for key in ("policy_sha256", "baseline_state_sha256"):
            if (
                not isinstance(planning_probe.get(key), str)
                or re.fullmatch(r"[0-9a-f]{64}", str(planning_probe.get(key))) is None
            ):
                raise ValueError(f"planning probe {key} is malformed")
    validated_initial_state: Path | None = None
    validated_initial_state_sha256: str | None = None
    if initial_state is not None:
        validated_initial_state, validated_initial_state_sha256 = (
            validate_initial_state(initial_state, config.design_dir)
        )
        if (
            initial_state_sha256 is not None
            and validated_initial_state_sha256 != initial_state_sha256
        ):
            raise ValueError("initial State changed after resume validation")
    image_lock = require_locked_image(image)
    image_digest = str(image_lock["image_digest"])
    runtime_image_ref = validate_runtime_image_ref(image_digest)
    image_version = str(image_lock["librelane_version"])
    pdk_root = validate_pdk_root(pdk_root)
    pdk_revision = installed_pdk_revision(config.pdk, pdk_root)
    pdk_integrity = pdk_integrity_report(config.pdk, pdk_root, allow_bootstrap=True)
    if not pdk_integrity["ready"]:
        raise RuntimeError(
            f"PDK {config.pdk} at {pdk_root / config.pdk} is missing or does not "
            f"match pinned revision {PDK_REVISIONS[config.pdk][1]}; "
            "run lane-agent pdk-install for the isolated PI_LANE_PDK_ROOT; "
            f"integrity error: {pdk_integrity.get('error') or 'unknown'}"
        )
    assert pdk_revision is not None
    validated_overrides = (
        validate_planning_overrides(overrides)
        if planning_probe is not None
        else [validate_override(value) for value in overrides]
    )
    physical_plan: dict[str, Any] | None = None
    physical_plan_binding: dict[str, Any] | None = None
    if physical_plan_path is not None:
        physical_plan = load_plan(physical_plan_path)
        physical_plan_binding = bind_plan_to_run(
            physical_plan,
            config_sha256=config.sha256,
            input_bundle_sha256=str(input_manifest["bundle_sha256"]),
            pdk=config.pdk,
            pdk_revision=pdk_revision,
            pdk_integrity_sha256=str(pdk_integrity["tree_sha256"]),
            image_digest=image_digest,
        )
        validated_overrides.extend(plan_overrides(physical_plan))
    planning_probe_sha256 = (
        hashlib.sha256(
            json.dumps(planning_probe, sort_keys=True, separators=(",", ":")).encode(
                "utf-8"
            )
        ).hexdigest()
        if planning_probe is not None
        else None
    )
    workflow_id = _validate_tracking_id(workflow_id, label="workflow_id")
    attempt_id = _validate_tracking_id(attempt_id, label="attempt_id")
    idempotency_key = _validate_tracking_id(idempotency_key, label="idempotency_key")
    validated_origin = _validate_tracking_id(origin, label="origin")
    assert validated_origin is not None
    request_fingerprint = _start_request_fingerprint(
        config,
        image=image,
        overrides=validated_overrides,
        frm=frm,
        to=to,
        jobs=jobs,
        parent_run_id=parent_run_id,
        initial_state_sha256=validated_initial_state_sha256,
        input_bundle_sha256=str(input_manifest["bundle_sha256"]),
        physical_plan_sha256=(physical_plan or {}).get("plan_sha256"),
        planning_probe_sha256=planning_probe_sha256,
        visualization_enabled=visualization_enabled,
    )
    previous = _idempotent_run(idempotency_key, request_fingerprint)
    if previous is not None:
        return previous
    duplicate = active_duplicate(
        config, input_bundle_sha256=str(input_manifest["bundle_sha256"])
    )
    if duplicate and not allow_concurrent:
        raise RuntimeError(
            f"an equivalent run is already active: {duplicate['run_id']}"
        )
    run_id = make_run_id(config.design_name)
    tag = f"PI_LANE_{run_id}"
    controller_dir = STATE_ROOT / run_id
    controller_dir.mkdir(parents=True, mode=0o700)
    input_snapshot_dir = controller_dir / "input-snapshot"
    extra_run_tags: tuple[str, ...] = ()
    if planning_probe is not None:
        parent_run_dir = canonical_directory(
            Path(str(planning_probe["parent_run_dir"])),
            label="planning baseline run directory",
        )
        expected_runs_root = canonical_directory(
            config.design_dir / "runs", label="planning design runs directory"
        )
        try:
            relative_parent = parent_run_dir.relative_to(expected_runs_root)
        except ValueError as exc:
            raise ValueError(
                "planning baseline run directory must belong to the design runs directory"
            ) from exc
        if len(relative_parent.parts) != 1 or not RUN_ID_RE.fullmatch(
            relative_parent.name
        ):
            raise ValueError("planning baseline run directory identity is invalid")
        extra_run_tags = (relative_parent.name,)
    _materialize_design_snapshot(
        input_snapshot_dir,
        manifest=input_manifest,
        captured=captured_inputs,
        run_tag=tag,
        extra_run_tags=extra_run_tags,
    )
    try:
        config_preflight = validate_config_in_image(
            config,
            image=image,
            image_digest=image_digest,
            pdk_root=pdk_root,
            overrides=tuple(validated_overrides),
            input_snapshot_dir=input_snapshot_dir,
        )
        if eqy_runtime is not None:
            eqy_runtime["tool_path"] = _materialize_eqy_tool(
                eqy_runtime, controller_dir
            )
    except Exception:
        for directory in sorted(
            (path for path in controller_dir.rglob("*") if path.is_dir()),
            key=lambda path: len(path.parts),
            reverse=True,
        ):
            directory.chmod(0o700)
        shutil.rmtree(controller_dir, ignore_errors=True)
        raise

    workflow_id = workflow_id or run_id
    attempt_id = attempt_id or run_id
    resume_chain_proof_sha256 = (
        link_chain_proof(
            parent_chain_proof_sha256=resume_parent_chain_proof_sha256,
            parent_state_attestation_sha256=resume_parent_state_attestation_sha256,
            parent_run_id=parent_run_id,
            run_id=run_id,
            root_run_id=resume_root_run_id,
            chain_depth=resume_chain_depth,
            frm=frm,
            initial_state_sha256=validated_initial_state_sha256,
            input_bundle_sha256=str(input_manifest["bundle_sha256"]),
        )
        if initial_state is not None and planning_probe is None
        else None
    )
    input_manifest_path = controller_dir / "input-manifest.json"
    atomic_write(
        input_manifest_path,
        json.dumps(input_manifest, indent=2, sort_keys=True) + "\n",
    )
    pinned_file_mounts: dict[Path, Path] = {}
    resume_input_snapshot: Path | None = None
    if validated_initial_state is not None:
        resume_bytes = validated_initial_state.read_bytes()
        if hashlib.sha256(resume_bytes).hexdigest() != validated_initial_state_sha256:
            raise ValueError(
                "initial State changed while its immutable snapshot was captured"
            )
        resume_input_snapshot = controller_dir / "resume-input.state"
        resume_input_snapshot.write_bytes(resume_bytes)
        resume_input_snapshot.chmod(0o400)
        pinned_file_mounts[resume_input_snapshot] = validated_initial_state
        if planning_probe is not None:
            parent_run_dir = canonical_directory(
                Path(str(planning_probe["parent_run_dir"])),
                label="planning baseline run directory",
            )
            expected_runs_root = canonical_directory(
                config.design_dir / "runs", label="planning design runs directory"
            )
            parent_run_dir.relative_to(expected_runs_root)
            validated_initial_state.relative_to(parent_run_dir)
            pinned_file_mounts[parent_run_dir] = parent_run_dir
    physical_plan_snapshot: Path | None = None
    if physical_plan is not None and physical_plan_binding is not None:
        physical_plan_snapshot = controller_dir / "physical-plan.json"
        atomic_write(
            physical_plan_snapshot,
            json.dumps(physical_plan, indent=2, sort_keys=True) + "\n",
        )
        physical_plan_snapshot.chmod(0o400)
        physical_plan_binding = {
            **physical_plan_binding,
            "path": str(physical_plan_snapshot),
        }
    (config.design_dir / "runs").mkdir(parents=True, exist_ok=True, mode=0o700)
    lane_run_dir = config.design_dir / "runs" / tag
    lane_run_dir.mkdir(mode=0o700)
    exit_file = controller_dir / "exit_code"
    handoff_file = controller_dir / "launch_handoff"
    build_log = controller_dir / "build.log"
    state_file = controller_dir / "state.json"
    docker_command, container_name = build_docker_command(
        config,
        run_id=run_id,
        tag=tag,
        pdk_root=pdk_root,
        overrides=validated_overrides,
        frm=frm,
        to=to,
        jobs=jobs,
        image=image,
        runtime_image_ref=runtime_image_ref,
        initial_state=validated_initial_state,
        eqy_tool_path=(eqy_runtime or {}).get("tool_path"),
        workflow_id=workflow_id,
        attempt_id=attempt_id,
        input_snapshot_dir=input_snapshot_dir,
        pinned_file_mounts=pinned_file_mounts,
    )
    env = {
        "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
        "PI_LANE_EXIT_FILE": str(exit_file),
        "PI_LANE_HANDOFF_FILE": str(handoff_file),
    }
    visualizer_exit_file = controller_dir / "visualizer_exit_code"
    visualizer_log = controller_dir / "visualizer.log"
    state: dict[str, Any] = {
        "schema_version": RUN_STATE_SCHEMA_VERSION,
        "state_revision": 0,
        "run_id": run_id,
        "workflow_id": workflow_id,
        "attempt_id": attempt_id,
        "idempotency_key": idempotency_key,
        "request_fingerprint": request_fingerprint,
        "origin": {"component": validated_origin},
        "controller_dir": str(controller_dir),
        "state_file": str(state_file),
        "controller_events_file": str(controller_dir / "controller-events.jsonl"),
        "build_log": str(build_log),
        "exit_file": str(exit_file),
        "handoff_file": str(handoff_file),
        "pid": None,
        "pid_start_time": None,
        "container_name": container_name,
        "container_identity": {
            "name": container_name,
            "image": image,
            "image_digest": image_digest,
            "runtime_image_ref": runtime_image_ref,
            "run_id": run_id,
                "workflow_id": workflow_id,
            "attempt_id": attempt_id,
            "input_bundle_sha256": input_manifest["bundle_sha256"],
        },
        "status": "launching",
        "launch_requested_at": now_iso(),
        "launch_requested_epoch": time.time(),
        "config_path": str(config.path),
        "config_sha256": config.sha256,
        "input_snapshot_dir": str(input_snapshot_dir),
        "input_manifest_path": str(input_manifest_path),
        "input_manifest": input_manifest,
        "input_bundle_sha256": input_manifest["bundle_sha256"],
        "config_preflight": config_preflight,
        "design_dir": str(config.design_dir),
        "design_name": config.design_name,
        "pdk": config.pdk,
        "pdk_family": OPEN_PDKS[config.pdk],
        "pdk_revision": pdk_revision,
        "pdk_integrity_sha256": pdk_integrity["tree_sha256"],
        "pdk_integrity_manifest": pdk_integrity["manifest"],
        "pdk_root": str(pdk_root),
        "flow": config.flow,
        "flow_registry_version": (
            CLASSIC_REGISTRY_VERSION if config.flow.casefold() == "classic" else None
        ),
        "tag": tag,
        "librelane_run_dir": str(lane_run_dir),
        "image": image,
        "image_digest": image_digest,
        "runtime_image_ref": runtime_image_ref,
        "image_lock": {
            key: image_lock[key]
            for key in (
                "schema_version",
                "revision",
                "image",
                "image_digest",
                "librelane_version",
                "install_run_id",
                "locked_at",
                "previous_lock_sha256",
                "lock_sha256",
            )
        },
        "image_lock_path": image_lock["lock_path"],
        "image_lock_sha256": image_lock["lock_sha256"],
        "librelane_version": image_version,
        "overrides": validated_overrides,
        "from": frm,
        "to": to,
        "jobs": jobs,
        "run_eqy_requested": run_eqy_requested,
        "eqy_script_path": (
            str(eqy_runtime["script_path"]) if eqy_runtime is not None else None
        ),
        "eqy_script_sha256": (
            eqy_runtime["script_sha256"] if eqy_runtime is not None else None
        ),
        "eqy_gate_tcl_path": (
            str(eqy_runtime["gate_tcl_path"]) if eqy_runtime is not None else None
        ),
        "eqy_gate_tcl_sha256": (
            eqy_runtime["gate_tcl_sha256"] if eqy_runtime is not None else None
        ),
        "eqy_tool_path": (
            str(eqy_runtime["tool_path"]) if eqy_runtime is not None else None
        ),
        "eqy_tool_sha256": (
            eqy_runtime["tool_sha256"] if eqy_runtime is not None else None
        ),
        "parent_run_id": parent_run_id,
        "initial_state": (
            str(validated_initial_state)
            if validated_initial_state is not None
            else None
        ),
        "initial_state_sha256": validated_initial_state_sha256,
        "initial_state_snapshot": (
            str(resume_input_snapshot) if resume_input_snapshot is not None else None
        ),
        "resume": (
            {
                "parent_run_id": parent_run_id,
                "root_run_id": resume_root_run_id,
                "chain_depth": resume_chain_depth,
                "from": frm,
                "initial_state": str(validated_initial_state),
                "initial_state_sha256": validated_initial_state_sha256,
                "input_bundle_sha256": input_manifest["bundle_sha256"],
                "parent_state_attestation_sha256": resume_parent_state_attestation_sha256,
                "parent_chain_proof_sha256": resume_parent_chain_proof_sha256,
                "chain_proof_sha256": resume_chain_proof_sha256,
            }
            if parent_run_id is not None
            and validated_initial_state is not None
            and planning_probe is None
            else None
        ),
        "planning_probe": dict(planning_probe) if planning_probe is not None else None,
        "physical_plan": physical_plan_binding,
        "visualization_dir": str(lane_run_dir / "visualizations"),
        "visualizer_pid": None,
        "visualizer_pid_start_time": None,
        "visualizer_launch_attempted": False,
        "visualizer_log": str(visualizer_log),
        "visualizer_exit_file": str(visualizer_exit_file),
        "stop_requested": False,
        "failure": None,
    }
    state = commit_state(
        state,
        expected_revision=0,
        events=(
            {
                "type": "run_launch_requested",
                "dedupe_key": "run_launch_requested",
                "phase": "startup",
                "status": "launching",
                "message": "managed LibreLane launch durably requested",
                "data": {
                    "design_name": config.design_name,
                    "pdk": config.pdk,
                    "flow": config.flow,
                    "input_bundle_sha256": input_manifest["bundle_sha256"],
                    "container_name": container_name,
                },
            },
        ),
    )
    if claim_current:
        set_current(run_id)
    output = build_log.open("ab", buffering=0)
    try:
        process = subprocess.Popen(
            [
                "/bin/bash",
                "-c",
                MANAGED_RUNNER_SCRIPT,
                "pi-lane-run",
                *docker_command,
            ],
            cwd=config.design_dir,
            env=env,
            stdin=subprocess.DEVNULL,
            stdout=output,
            stderr=subprocess.STDOUT,
            start_new_session=True,
        )
    except OSError as exc:
        state.update(
            {
                "status": "failed",
                "finished_at": now_iso(),
                "finished_epoch": time.time(),
                "failure": {
                    "component": "lane-agent",
                    "code": "LANE_WORKER_LAUNCH_FAILED",
                    "class": "infrastructure",
                    "retryable": True,
                    "rtl_repairable": False,
                    "message": str(exc),
                },
            }
        )
        commit_state(
            state,
            expected_revision=managed_state_revision(state),
            events=(
                {
                    "type": "run_launch_failed",
                    "dedupe_key": "run_launch_failed",
                    "phase": "startup",
                    "status": "failed",
                    "message": "managed LibreLane worker could not be launched",
                    "terminal": True,
                    "data": {"error": str(exc)},
                },
            ),
        )
        raise RuntimeError(f"failed to launch LibreLane worker: {exc}") from exc
    finally:
        output.close()

    main_pid_start_time = pid_start_time(process.pid)
    if main_pid_start_time is None or main_pid_start_time <= 0:
        message = "LibreLane worker process start time is unavailable"
        _terminate_spawned_process(process.pid, container_name=container_name)
        state.update(
            {
                "status": "failed",
                "finished_at": now_iso(),
                "finished_epoch": time.time(),
                "failure": {
                    "component": "lane-agent",
                    "code": "LANE_WORKER_IDENTITY_FAILED",
                    "class": "infrastructure",
                    "retryable": True,
                    "rtl_repairable": False,
                    "message": message,
                },
            }
        )
        commit_state(
            state,
            expected_revision=managed_state_revision(state),
            events=(
                {
                    "type": "run_launch_failed",
                    "dedupe_key": "run_launch_failed",
                    "phase": "startup",
                    "status": "failed",
                    "message": message,
                    "terminal": True,
                    "data": {"error": message},
                },
            ),
        )
        raise RuntimeError(message)

    visualizer_process: tuple[int, int] | None = None
    if visualization_enabled:
        visualizer_command = build_visualizer_command(
            run_id=run_id,
            run_dir=lane_run_dir,
            design_dir=config.design_dir,
            design_name=config.design_name,
            config_path=config.path,
            input_snapshot_dir=input_snapshot_dir,
            input_bundle_sha256=str(input_manifest["bundle_sha256"]),
            pdk=config.pdk,
            pdk_root=pdk_root,
            image=image,
            runtime_image_ref=runtime_image_ref,
            config_sha256=config.sha256,
            pdk_revision=pdk_revision,
            pdk_integrity_sha256=pdk_integrity["tree_sha256"],
            image_digest=image_digest,
            librelane_version=image_version,
            main_exit_file=exit_file,
            main_pid=process.pid,
            main_pid_start_time=main_pid_start_time,
            visualizer_exit_file=visualizer_exit_file,
            overrides=validated_overrides,
        )
        visualizer_process = _spawn_visualizer(
            visualizer_command, cwd=config.design_dir, log_path=visualizer_log
        )
    state.update(
        {
            "pid": process.pid,
            "pid_start_time": main_pid_start_time,
            "status": "running",
            "started_at": now_iso(),
            "started_epoch": time.time(),
            "visualizer_launch_attempted": visualization_enabled,
            "visualizer_pid": (
                visualizer_process[0] if visualizer_process is not None else None
            ),
            "visualizer_pid_start_time": (
                visualizer_process[1] if visualizer_process is not None else None
            ),
        }
    )
    try:
        state = commit_state(
            state,
            expected_revision=managed_state_revision(state),
            events=(
                {
                    "type": "run_started",
                    "dedupe_key": "run_started",
                    "phase": "startup",
                    "status": "running",
                    "message": "managed LibreLane run started",
                    "data": {
                        "design_name": config.design_name,
                        "pdk": config.pdk,
                        "flow": config.flow,
                        "pid": process.pid,
                        "input_bundle_sha256": input_manifest["bundle_sha256"],
                        "container_name": container_name,
                    },
                },
            ),
        )
    except Exception:
        if visualizer_process is not None:
            _terminate_spawned_process(visualizer_process[0])
        _terminate_spawned_process(process.pid, container_name=container_name)
        raise

    try:
        atomic_write(handoff_file, "ready\n")
    except Exception as exc:
        _terminate_spawned_process(process.pid, container_name=container_name)
        if visualizer_process is not None:
            _terminate_spawned_process(visualizer_process[0])
        message = f"LibreLane worker registration handoff failed: {exc}"
        state.update(
            {
                "status": "failed",
                "finished_at": now_iso(),
                "finished_epoch": time.time(),
                "failure": {
                    "component": "lane-agent",
                    "code": "LANE_WORKER_HANDOFF_FAILED",
                    "class": "infrastructure",
                    "retryable": True,
                    "rtl_repairable": False,
                    "message": message,
                },
            }
        )
        commit_state(
            state,
            expected_revision=managed_state_revision(state),
            events=(
                {
                    "type": "run_launch_failed",
                    "dedupe_key": "run_launch_failed",
                    "phase": "startup",
                    "status": "failed",
                    "message": message,
                    "terminal": True,
                    "data": {"error": message},
                },
            ),
        )
        raise RuntimeError(message) from exc
    return refresh_status(state)


def _read_exit_code(path: Path) -> int | None:
    if not path.is_file():
        return None
    try:
        return int(path.read_text(encoding="utf-8").strip())
    except ValueError:
        return None


def _freeze_elapsed(state: dict[str, Any], status: str, exit_file: Path) -> None:
    if status not in TERMINAL_STATES or state.get("finished_epoch") is not None:
        return
    try:
        state["finished_epoch"] = exit_file.stat().st_mtime
    except OSError:
        state["finished_epoch"] = time.time()


def _step_id(step_dir: Path) -> str:
    # LibreLane's config metadata retains only a Step implementation name.
    # Repeated Classic Steps therefore all report e.g. OpenROAD.STAMidPNR,
    # while the normalized identity suffix (-1, -2, -3) is preserved in the
    # directory name. Prefer a directory identity only when it maps exactly
    # to this pinned Classic registry; unknown/custom Steps continue to use
    # their metadata below.
    match = STEP_DIR_RE.match(step_dir.name)
    if match:
        directory_id = STEP_ID_NORMALIZE_RE.sub("", match.group(2).lower())
        for classic_id in CLASSIC_STEP_IDS:
            if STEP_ID_NORMALIZE_RE.sub("", classic_id.lower()) == directory_id:
                return classic_id
    config_path = step_dir / "config.json"
    try:
        config_file = canonical_regular_file(
            config_path,
            root=step_dir,
            label="Step config",
            minimum_size=1,
            maximum_size=16 * 1024 * 1024,
        )
        raw = json.loads(config_file.read_text(encoding="utf-8"))
        meta = raw.get("meta") or {}
        if isinstance(meta.get("step"), str):
            return meta["step"]
    except (OSError, json.JSONDecodeError, AttributeError):
        pass
    return match.group(2) if match else step_dir.name


def discover_steps(run_dir: Path) -> list[dict[str, Any]]:
    try:
        trusted_run_dir = canonical_directory(run_dir, label="LibreLane run directory")
    except ValueError:
        return []
    found: list[tuple[int, Path]] = []
    ordinals: set[int] = set()
    for entry in trusted_run_dir.iterdir():
        match = STEP_DIR_RE.match(entry.name)
        if not match:
            continue
        try:
            step_dir = direct_child_directory(
                entry, root=trusted_run_dir, label="LibreLane Step directory"
            )
        except ValueError:
            continue
        ordinal = int(match.group(1))
        if ordinal in ordinals:
            return []
        ordinals.add(ordinal)
        found.append((ordinal, step_dir))
    steps = []
    for ordinal, path in sorted(found):
        logs: list[str] = []
        for log in sorted(path.glob("*.log")):
            try:
                logs.append(
                    str(
                        canonical_regular_file(
                            log, root=path, label="Step log", maximum_size=2**31
                        )
                    )
                )
            except ValueError:
                continue
        commands = path / "COMMANDS"
        runtime = path / "runtime.txt"
        state_out = path / "state_out.json"

        def safe_optional(candidate: Path, label: str) -> str | None:
            try:
                return str(
                    canonical_regular_file(
                        candidate, root=path, label=label, maximum_size=64 * 1024 * 1024
                    )
                )
            except ValueError:
                return None

        safe_state = safe_optional(state_out, "Step state_out")
        steps.append(
            {
                "ordinal": ordinal,
                "id": _step_id(path),
                "directory": str(path),
                "completed": safe_state is not None,
                "state_out": safe_state,
                "commands": safe_optional(commands, "Step commands"),
                "logs": logs,
                "runtime": safe_optional(runtime, "Step runtime"),
            }
        )
    return steps


def discover_visualization(state: Mapping[str, Any]) -> dict[str, Any]:
    run_dir = Path(str(state["librelane_run_dir"]))
    root = Path(str(state.get("visualization_dir") or run_dir / "visualizations"))
    manifest_path = root / "manifest.json"
    timeline_path = root / "timeline.jsonl"
    manifest: dict[str, Any] = {}
    manifest_error: str | None = None
    if manifest_path.exists():
        try:
            safe_run = canonical_directory(run_dir, label="LibreLane run directory")
            safe_root = direct_child_directory(
                root, root=safe_run, label="visualization directory"
            )
            safe_manifest = canonical_regular_file(
                manifest_path,
                root=safe_root,
                label="visualization manifest",
                minimum_size=2,
                maximum_size=64 * 1024 * 1024,
            )
            raw = json.loads(safe_manifest.read_text(encoding="utf-8"))
            if isinstance(raw, dict):
                manifest = raw
                schema_version = manifest.get("schema_version")
                if schema_version != 3:
                    manifest_error = "visualization manifest schema is unsupported"
                else:
                    expected_identity = {
                        "run_id": state.get("run_id"),
                        "run_dir": state.get("librelane_run_dir"),
                        "design_dir": state.get("design_dir"),
                        "design_name": state.get("design_name"),
                        "config_path": state.get("config_path"),
                        "config_sha256": state.get("config_sha256"),
                        "input_snapshot_dir": state.get("input_snapshot_dir"),
                        "input_bundle_sha256": state.get("input_bundle_sha256"),
                        "pdk": state.get("pdk"),
                        "pdk_revision": state.get("pdk_revision"),
                        "pdk_integrity_sha256": state.get("pdk_integrity_sha256"),
                        "pdk_root": state.get("pdk_root"),
                        "image": state.get("image"),
                        "image_digest": state.get("image_digest"),
                        "runtime_image_ref": state.get("runtime_image_ref"),
                        "librelane_version": state.get("librelane_version"),
                        "main_pid": state.get("pid"),
                        "main_pid_start_time": state.get("pid_start_time"),
                        "overrides": list(state.get("overrides") or []),
                    }
                    for key, expected in expected_identity.items():
                        if (
                            expected is None
                            or key not in manifest
                            or manifest[key] != expected
                        ):
                            manifest_error = (
                                f"visualization manifest {key} identity mismatch"
                            )
                            break
            else:
                manifest_error = "visualization manifest is not a JSON object"
        except (OSError, ValueError, json.JSONDecodeError) as exc:
            manifest_error = f"untrusted visualization manifest: {exc}"

    manifest_counters: dict[str, int] = {}
    for counter_name in (
        "completed_step_count",
        "visualization_count",
        "failed_visualization_count",
    ):
        counter_value = manifest.get(counter_name, 0)
        if (
            isinstance(counter_value, bool)
            or not isinstance(counter_value, int)
            or counter_value < 0
        ):
            if manifest_error is None:
                manifest_error = f"visualization manifest {counter_name} must be a non-negative integer"
            manifest_counters[counter_name] = 0
        else:
            manifest_counters[counter_name] = counter_value

    pid = state.get("visualizer_pid")
    pid_start = state.get("visualizer_pid_start_time")
    exit_path_value = state.get("visualizer_exit_file")
    exit_code = _read_exit_code(Path(str(exit_path_value))) if exit_path_value else None
    running = _visualizer_pid_alive(pid, pid_start, str(state.get("run_id") or ""))
    launch_attempted = (
        state.get("visualizer_launch_attempted") is True or pid is not None
    )
    manifest_status = (
        str(manifest["status"]) if isinstance(manifest.get("status"), str) else None
    )
    known_manifest_states = {
        "running",
        "completed",
        "completed_with_errors",
        "failed",
    }
    if manifest_status is not None and manifest_status not in known_manifest_states:
        manifest_error = f"visualization manifest status is invalid: {manifest_status}"
    terminal_manifest_states = {"completed", "completed_with_errors", "failed"}
    if manifest_error is not None:
        status = "failed"
    elif running:
        status = manifest_status or "starting"
    elif manifest_status in terminal_manifest_states:
        status = manifest_status
    elif launch_attempted:
        # A dead managed worker cannot leave a nonterminal manifest authoritative.
        status = "failed"
    elif exit_code is not None:
        status = "completed" if exit_code == 0 else "failed"
    else:
        status = "starting" if state.get("status") in ACTIVE_STATES else "unavailable"

    steps = manifest.get("steps")
    if not isinstance(steps, list):
        steps = []
    images = manifest.get("images")
    if not isinstance(images, list):
        images = []
    errors = manifest.get("errors")
    if not isinstance(errors, list):
        errors = []
    if manifest_error is not None:
        errors.append(manifest_error)
    if (
        status == "failed"
        and not running
        and manifest_status not in terminal_manifest_states
    ):
        errors.append("visualization sidecar exited before writing a terminal manifest")
    if status == "failed" and not errors and state.get("visualizer_log"):
        errors = _tail_lines(Path(str(state["visualizer_log"])), 8)
    return {
        "status": status,
        "is_running": running,
        "pid": pid,
        "pid_start_time": pid_start,
        "exit_code": exit_code,
        "directory": str(root),
        "manifest": str(manifest_path),
        "timeline": str(timeline_path),
        "completed_step_count": manifest_counters["completed_step_count"],
        "visualization_count": manifest_counters["visualization_count"],
        "failed_visualization_count": manifest_counters["failed_visualization_count"],
        "latest_image": manifest.get("latest_image"),
        "images": [str(path) for path in images],
        "errors": [str(error) for error in errors][-8:],
        "steps": steps,
    }


def _visualizer_pid_alive(
    pid: object, expected_start_time: object, run_id: str
) -> bool:
    if (
        isinstance(pid, bool)
        or not isinstance(pid, int)
        or isinstance(expected_start_time, bool)
        or not isinstance(expected_start_time, int)
        or not pid_identity_alive(pid, expected_start_time)
    ):
        return False
    try:
        command = Path(f"/proc/{pid}/cmdline").read_bytes().split(b"\0")
    except OSError:
        return False
    decoded = [item.decode("utf-8", errors="replace") for item in command if item]
    run_id_matches = any(
        argument == "--run-id"
        and index + 1 < len(decoded)
        and decoded[index + 1] == run_id
        for index, argument in enumerate(decoded)
    )
    return "pi_lane.visualizer" in decoded and run_id_matches


def build_step_history(
    steps: list[dict[str, Any]], visualization: Mapping[str, Any]
) -> list[dict[str, Any]]:
    visual_by_ordinal = {
        item.get("ordinal"): item
        for item in visualization.get("steps", [])
        if isinstance(item, dict)
    }
    history: list[dict[str, Any]] = []
    for step in steps:
        visual = visual_by_ordinal.get(step["ordinal"], {})
        history.append(
            {
                "ordinal": step["ordinal"],
                "id": step["id"],
                "phase": _coarse_phase(step["id"]),
                "status": "completed" if step["completed"] else "running",
                "directory": step["directory"],
                "commands": step["commands"],
                "logs": step["logs"],
                "runtime": step["runtime"],
                "state_out": step["state_out"],
                "visualization_status": visual.get(
                    "visualization_status", "not_applicable"
                ),
                "visualization": visual.get("image"),
                "visualization_error": visual.get("visualization_error"),
            }
        )
    return history


def _coarse_phase(step_id: str | None) -> str | None:
    if not step_id:
        return None
    return CLASSIC_STEP_PHASE.get(step_id, "unknown")


def _transient_evidence_error(exc: BaseException) -> bool:
    cause: BaseException | None = exc
    while cause is not None:
        if isinstance(cause, OSError):
            return not isinstance(cause, (FileNotFoundError, NotADirectoryError))
        cause = cause.__cause__
    return False


def _load_metrics(
    run_dir: Path, steps: list[dict[str, Any]]
) -> tuple[dict[str, Any], str, str | None, str | None]:
    try:
        trusted_run_dir = canonical_directory(run_dir, label="LibreLane run directory")
    except ValueError as exc:
        error = str(exc) if _transient_evidence_error(exc) else None
        return {}, "unavailable", None, error
    final_path = trusted_run_dir / "final" / "metrics.json"
    candidates: list[tuple[Path, str]] = []
    read_error: str | None = None
    try:
        candidates.append(
            (
                canonical_regular_file(
                    final_path,
                    root=trusted_run_dir,
                    label="final metrics",
                    minimum_size=1,
                    maximum_size=64 * 1024 * 1024,
                ),
                "final",
            )
        )
    except ValueError as exc:
        if _transient_evidence_error(exc):
            read_error = str(exc)
    for step in reversed(steps):
        if step["state_out"]:
            candidates.append((Path(step["state_out"]), "partial"))
            break
    for path, status in candidates:
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
            metrics = raw if status == "final" else raw.get("metrics", {})
            if isinstance(metrics, dict):
                return metrics, status, str(path), read_error
        except OSError as exc:
            if not isinstance(exc, (FileNotFoundError, NotADirectoryError)):
                read_error = f"metrics evidence could not be read: {exc}"
            continue
        except (json.JSONDecodeError, AttributeError):
            continue
    return {}, "unavailable", None, read_error


def _finite_metric(metrics: Mapping[str, Any], key: str) -> float | None:
    evidence = _metric_evidence(metrics, key, maturity="partial")
    value = evidence["value"]
    return float(value) if evidence["validity"] == "valid" and value is not None else None


def _metric_evidence(
    metrics: Mapping[str, Any],
    key: str,
    *,
    maturity: str,
    observed_at_step: str | None = None,
    source_sha256: str | None = None,
    tool_image_digest: str | None = None,
) -> dict[str, Any]:
    """Normalize tool metrics without allowing sentinels into QoR or PPA."""

    if maturity not in {"partial", "terminal"}:
        raise ValueError("metric maturity is invalid")
    raw = metrics.get(key)
    value: float | None = None
    validity = "valid"
    if raw is None or isinstance(raw, bool) or not isinstance(raw, (int, float)):
        validity = "unavailable"
    else:
        parsed = float(raw)
        if not math.isfinite(parsed) or abs(parsed) >= METRIC_SENTINEL_ABS:
            validity = "tool_sentinel"
        else:
            value = parsed
    return {
        "schema": "lane.metric/v2",
        "metric": key,
        "value": value,
        "unit": METRIC_UNITS.get(key, "count"),
        "observed_at_step": observed_at_step,
        "maturity": maturity,
        "validity": validity,
        "raw_value": None if raw is None else str(raw),
        "source_sha256": source_sha256,
        "tool_image_digest": tool_image_digest,
    }


def _physical_sanity(
    *,
    total_instance_area_um2: float | None,
    standard_cell_area_um2: float | None,
    core_area_um2: float | None,
    filler_area_um2: float | None,
    tap_area_um2: float | None,
    utilization_pct: float | None,
    utilization_metric: str,
    minimum_geometry_qualification: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    checks: dict[str, dict[str, Any]] = {}

    if utilization_pct is None:
        checks["minimum_utilization"] = {
            "status": "unknown",
            "metric": utilization_metric,
            "value_pct": None,
            "minimum_pct": PHYSICAL_SANITY_MIN_UTILIZATION_PCT,
            "reason": f"{utilization_metric} is unavailable or non-numeric",
        }
    elif utilization_pct < 0.0 or utilization_pct > 100.0:
        checks["minimum_utilization"] = {
            "status": "fail",
            "metric": utilization_metric,
            "value_pct": utilization_pct,
            "minimum_pct": PHYSICAL_SANITY_MIN_UTILIZATION_PCT,
            "reason": (
                f"standard-cell utilization {utilization_pct:.3f}% is outside "
                "the physical range 0..100%"
            ),
        }
    elif utilization_pct < PHYSICAL_SANITY_MIN_UTILIZATION_PCT:
        checks["minimum_utilization"] = {
            "status": "fail",
            "metric": utilization_metric,
            "value_pct": utilization_pct,
            "minimum_pct": PHYSICAL_SANITY_MIN_UTILIZATION_PCT,
            "reason": (
                f"standard-cell utilization {utilization_pct:.3f}% is below the "
                f"conservative minimum {PHYSICAL_SANITY_MIN_UTILIZATION_PCT:.1f}%"
            ),
        }
    else:
        checks["minimum_utilization"] = {
            "status": "pass",
            "metric": utilization_metric,
            "value_pct": utilization_pct,
            "minimum_pct": PHYSICAL_SANITY_MIN_UTILIZATION_PCT,
            "reason": (
                f"standard-cell utilization {utilization_pct:.3f}% meets the "
                f"minimum {PHYSICAL_SANITY_MIN_UTILIZATION_PCT:.1f}%"
            ),
        }

    filler_ratio_pct = None
    if (
        filler_area_um2 is not None
        and core_area_um2 is not None
        and core_area_um2 > 0.0
    ):
        filler_ratio_pct = filler_area_um2 / core_area_um2 * 100.0
    if filler_ratio_pct is None:
        checks["maximum_filler_ratio"] = {
            "status": "unknown",
            "metric": "filler_area_um2/core_area_um2",
            "value_pct": None,
            "maximum_pct": PHYSICAL_SANITY_MAX_FILLER_RATIO_PCT,
            "reason": "filler or positive core area is unavailable",
        }
    elif (
        filler_ratio_pct < 0.0
        or filler_ratio_pct > 100.0 + PHYSICAL_SANITY_AREA_TOLERANCE_PCT
    ):
        checks["maximum_filler_ratio"] = {
            "status": "fail",
            "metric": "filler_area_um2/core_area_um2",
            "value_pct": filler_ratio_pct,
            "maximum_pct": PHYSICAL_SANITY_MAX_FILLER_RATIO_PCT,
            "reason": (
                f"filler/core ratio {filler_ratio_pct:.3f}% is outside the "
                "physically reasonable range"
            ),
        }
    elif filler_ratio_pct > PHYSICAL_SANITY_MAX_FILLER_RATIO_PCT:
        checks["maximum_filler_ratio"] = {
            "status": "fail",
            "metric": "filler_area_um2/core_area_um2",
            "value_pct": filler_ratio_pct,
            "maximum_pct": PHYSICAL_SANITY_MAX_FILLER_RATIO_PCT,
            "reason": (
                f"filler/core ratio {filler_ratio_pct:.3f}% exceeds the "
                f"conservative maximum {PHYSICAL_SANITY_MAX_FILLER_RATIO_PCT:.1f}%"
            ),
        }
    else:
        checks["maximum_filler_ratio"] = {
            "status": "pass",
            "metric": "filler_area_um2/core_area_um2",
            "value_pct": filler_ratio_pct,
            "maximum_pct": PHYSICAL_SANITY_MAX_FILLER_RATIO_PCT,
            "reason": (
                f"filler/core ratio {filler_ratio_pct:.3f}% is at or below the "
                f"maximum {PHYSICAL_SANITY_MAX_FILLER_RATIO_PCT:.1f}%"
            ),
        }

    area_metrics = {
        "design__instance__area": total_instance_area_um2,
        "design__instance__area__stdcell": standard_cell_area_um2,
        "design__core__area": core_area_um2,
        "design__instance__area__class:fill_cell": filler_area_um2,
        "design__instance__area__class:tap_cell": tap_area_um2,
        utilization_metric: utilization_pct,
    }
    missing = [key for key, value in area_metrics.items() if value is None]
    if missing:
        checks["area_consistency"] = {
            "status": "unknown",
            "tolerance_pct": PHYSICAL_SANITY_AREA_TOLERANCE_PCT,
            "reason": "required metrics unavailable: " + ", ".join(missing),
        }
    else:
        assert total_instance_area_um2 is not None
        assert standard_cell_area_um2 is not None
        assert core_area_um2 is not None
        assert filler_area_um2 is not None
        assert tap_area_um2 is not None
        assert utilization_pct is not None
        tolerance = 1.0 + PHYSICAL_SANITY_AREA_TOLERANCE_PCT / 100.0
        violations: list[str] = []
        if core_area_um2 <= 0.0:
            violations.append("core area must be positive")
        for label, value in (
            ("total instance", total_instance_area_um2),
            ("standard-cell", standard_cell_area_um2),
            ("filler", filler_area_um2),
            ("tap", tap_area_um2),
        ):
            if value < 0.0:
                violations.append(f"{label} area is negative")
        if core_area_um2 > 0.0:
            if total_instance_area_um2 > core_area_um2 * tolerance:
                violations.append(
                    "total instance area exceeds core area beyond tolerance"
                )
            if standard_cell_area_um2 > core_area_um2 * tolerance:
                violations.append(
                    "standard-cell area exceeds core area beyond tolerance"
                )
            if filler_area_um2 > core_area_um2 * tolerance:
                violations.append("filler area exceeds core area beyond tolerance")
            calculated_utilization = standard_cell_area_um2 / core_area_um2 * 100.0
            utilization_tolerance = max(
                0.5,
                abs(calculated_utilization)
                * PHYSICAL_SANITY_AREA_TOLERANCE_PCT
                / 100.0,
            )
            if abs(calculated_utilization - utilization_pct) > utilization_tolerance:
                violations.append(
                    "reported utilization does not match standard-cell/core areas "
                    "within tolerance"
                )
        if tap_area_um2 > standard_cell_area_um2 * tolerance:
            violations.append(
                "tap-cell area exceeds standard-cell area beyond tolerance"
            )
        if filler_area_um2 > total_instance_area_um2 * tolerance:
            violations.append(
                "filler area exceeds total instance area beyond tolerance"
            )
        if (
            standard_cell_area_um2 + filler_area_um2
            > total_instance_area_um2 * tolerance
        ):
            violations.append(
                "standard-cell plus filler area exceeds total instance area beyond tolerance"
            )
        checks["area_consistency"] = {
            "status": "fail" if violations else "pass",
            "tolerance_pct": PHYSICAL_SANITY_AREA_TOLERANCE_PCT,
            "reason": "; ".join(violations)
            if violations
            else (
                "reported core, instance, standard-cell, filler, tap, and utilization "
                "metrics are mutually consistent"
            ),
        }

    density_checks = ("minimum_utilization", "maximum_filler_ratio")
    density_statuses = [checks[name]["status"] for name in density_checks]
    if any(status == "unknown" for status in density_statuses):
        raw_density_gate = "unknown"
    elif any(status == "fail" for status in density_statuses):
        raw_density_gate = "fail"
    else:
        raw_density_gate = "pass"
    qualification = dict(minimum_geometry_qualification or {
        "gate": "not_applicable",
        "classification": "not_applicable",
        "reasons": [],
        "integrity_checks": {},
    })
    metric_ranges_valid = (
        utilization_pct is not None
        and 0.0 <= utilization_pct <= 100.0
        and filler_ratio_pct is not None
        and 0.0 <= filler_ratio_pct <= 100.0 + PHYSICAL_SANITY_AREA_TOLERANCE_PCT
    )
    area_consistency_pass = checks["area_consistency"]["status"] == "pass"
    qualification_gate = (
        "pass"
        if raw_density_gate == "fail"
        and qualification.get("gate") == "pass"
        and metric_ranges_valid
        and area_consistency_pass
        else qualification.get("gate", "not_applicable")
    )
    density = {
        "raw_threshold_gate": raw_density_gate,
        "classification": qualification.get("classification", "not_applicable"),
        "qualification_gate": qualification_gate,
        "utilization_pct": utilization_pct,
        "filler_ratio_pct": filler_ratio_pct,
        "qualification_reasons": list(qualification.get("reasons") or []),
    }
    integrity_statuses = [
        check["status"] for name, check in checks.items() if name not in density_checks
    ]
    if any(status == "fail" for status in integrity_statuses):
        gate = "fail"
    elif any(status == "unknown" for status in integrity_statuses):
        gate = "unknown"
    elif raw_density_gate == "pass" or qualification_gate == "pass":
        gate = "pass"
    else:
        gate = "unknown" if raw_density_gate == "unknown" else "fail"
    return {
        "gate": gate,
        "checks": checks,
        "density": density,
        "minimum_geometry_qualification": qualification,
        "thresholds": {
            "minimum_utilization_pct": PHYSICAL_SANITY_MIN_UTILIZATION_PCT,
            "maximum_filler_ratio_pct": PHYSICAL_SANITY_MAX_FILLER_RATIO_PCT,
            "area_tolerance_pct": PHYSICAL_SANITY_AREA_TOLERANCE_PCT,
        },
        "failure_reasons": [
            check["reason"] for check in checks.values() if check["status"] == "fail"
        ],
    }


def summarize_qor(
    metrics: dict[str, Any],
    metric_status: str,
    *,
    observed_at_step: str | None = None,
    source_sha256: str | None = None,
    tool_image_digest: str | None = None,
    terminal_maturity: bool | None = None,
    physical_plan: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    checks_by_metric = {
        "setup_timing": "timing__setup_vio__count",
        "hold_timing": "timing__hold_vio__count",
        "max_slew": "design__max_slew_violation__count",
        "max_cap": "design__max_cap_violation__count",
        "route_drc": "route__drc_errors",
        "magic_drc": "magic__drc_error__count",
        "klayout_drc": "klayout__drc_error__count",
        "lvs": "design__lvs_error__count",
        "xor": "design__xor_difference__count",
    }
    if terminal_maturity is None:
        terminal_maturity = metric_status == "final"
    maturity = "terminal" if metric_status == "final" and terminal_maturity else "partial"
    evidence_keys = {
        *checks_by_metric.values(),
        "timing__setup__ws",
        "timing__setup__tns",
        "timing__hold__ws",
        "timing__hold__tns",
        "design__instance__area",
        "design__instance__area__stdcell",
        "design__core__area",
        "design__instance__area__class:fill_cell",
        "design__instance__area__class:tap_cell",
        "design__instance__utilization",
        "design__instance__utilization__stdcell",
        "power__total",
    }
    metric_evidence = {
        key: _metric_evidence(
            metrics,
            key,
            maturity=maturity,
            observed_at_step=observed_at_step,
            source_sha256=source_sha256,
            tool_image_digest=tool_image_digest,
        )
        for key in sorted(evidence_keys)
    }

    setup_wns = metric_evidence["timing__setup__ws"]["value"]
    setup_tns = metric_evidence["timing__setup__tns"]["value"]
    setup_violations = metric_evidence["timing__setup_vio__count"]["value"]
    max_slew_violations = metric_evidence[
        "design__max_slew_violation__count"
    ]["value"]
    max_cap_violations = metric_evidence[
        "design__max_cap_violation__count"
    ]["value"]
    corner_setup: list[dict[str, Any]] = []
    corner_prefix = "timing__setup__ws__corner:"
    for key, raw_wns in sorted(metrics.items()):
        if not isinstance(key, str) or not key.startswith(corner_prefix):
            continue
        corner = key.removeprefix(corner_prefix)
        wns = _finite_metric(metrics, key)
        tns = _finite_metric(metrics, f"timing__setup__tns__corner:{corner}")
        violations = _finite_metric(
            metrics, f"timing__setup_vio__count__corner:{corner}"
        )
        if wns is None and tns is None and violations is None:
            continue
        corner_setup.append(
            {
                "corner": corner,
                "wns_ns": wns,
                "tns_ns": tns,
                "violation_count": violations,
                "status": "fail"
                if (violations is not None and violations > 0)
                or (wns is not None and wns < 0)
                else "pass",
            }
        )
    failing_corner_count = sum(row["status"] == "fail" for row in corner_setup)
    failure_modes: list[str] = []
    if setup_violations is None or setup_wns is None:
        timing_failure_class = "timing_unavailable"
    elif setup_violations > 0:
        marginal = (
            setup_wns >= -0.2
            and setup_violations <= 1
            and failing_corner_count <= 1
        )
        timing_failure_class = "timing_marginal" if marginal else "timing_structural"
        failure_modes.append("setup_marginal" if marginal else "setup_structural")
    elif (max_slew_violations or 0) > 0 or (max_cap_violations or 0) > 0:
        timing_failure_class = "electrical_constraint"
    else:
        timing_failure_class = "pass"
    if (max_slew_violations or 0) > 0:
        failure_modes.append("max_slew")
    if (max_cap_violations or 0) > 0:
        failure_modes.append("max_capacitance")
    if timing_failure_class == "electrical_constraint" and not failure_modes:
        failure_modes.append("electrical_constraint")
    timing_diagnostics = {
        "schema": "lane.timing-diagnostic/v1",
        "classification": timing_failure_class,
        "failure_modes": failure_modes,
        "setup_wns_ns": setup_wns,
        "setup_tns_ns": setup_tns,
        "setup_violation_count": setup_violations,
        "failing_corner_count": failing_corner_count,
        "corners": corner_setup,
        "max_slew_violation_count": max_slew_violations,
        "max_cap_violation_count": max_cap_violations,
        "timing_repair_buffer_count": _finite_metric(
            metrics, "design__instance__count__class:timing_repair_buffer"
        ),
        "route_wirelength_um": next(
            (
                value
                for key in (
                    "global_route__wirelength",
                    "route__wirelength__estimated",
                    "route__wirelength",
                )
                if (value := _finite_metric(metrics, key)) is not None
            ),
            None,
        ),
    }
    observed_checks: dict[str, str] = {}
    for label, key in checks_by_metric.items():
        evidence = metric_evidence[key]
        value = evidence["value"]
        if (
            evidence["validity"] != "valid"
            or value is None
            or value < 0
            or not value.is_integer()
        ):
            observed_checks[label] = "unknown"
        else:
            observed_checks[label] = "pass" if value == 0 else "fail"
    checks = (
        observed_checks
        if maturity == "terminal"
        else {label: "unknown" for label in checks_by_metric}
    )
    if any(value == "fail" for value in checks.values()):
        signoff_gate = "fail"
    elif maturity == "terminal" and all(value == "pass" for value in checks.values()):
        signoff_gate = "pass"
    else:
        signoff_gate = "unknown"

    total_instance_area_um2 = _finite_metric(metrics, "design__instance__area")
    standard_cell_area_um2 = _finite_metric(metrics, "design__instance__area__stdcell")
    core_area_um2 = _finite_metric(metrics, "design__core__area")
    filler_area_um2 = _finite_metric(metrics, "design__instance__area__class:fill_cell")
    tap_area_um2 = _finite_metric(metrics, "design__instance__area__class:tap_cell")
    utilization_metric = (
        "design__instance__utilization__stdcell"
        if "design__instance__utilization__stdcell" in metrics
        else "design__instance__utilization"
    )
    utilization_ratio = _finite_metric(metrics, utilization_metric)
    utilization_pct = (
        utilization_ratio * 100.0 if utilization_ratio is not None else None
    )
    logic_area_um2 = (
        standard_cell_area_um2 - tap_area_um2
        if standard_cell_area_um2 is not None and tap_area_um2 is not None
        else None
    )
    logic_utilization_pct = (
        logic_area_um2 / core_area_um2 * 100.0
        if logic_area_um2 is not None
        and core_area_um2 is not None
        and core_area_um2 > 0.0
        else None
    )
    filler_ratio_pct = (
        filler_area_um2 / core_area_um2 * 100.0
        if filler_area_um2 is not None
        and core_area_um2 is not None
        and core_area_um2 > 0.0
        else None
    )
    minimum_geometry_qualification = minimum_geometry_density_qualification(
        physical_plan, metrics
    )
    physical_sanity = _physical_sanity(
        total_instance_area_um2=total_instance_area_um2,
        standard_cell_area_um2=standard_cell_area_um2,
        core_area_um2=core_area_um2,
        filler_area_um2=filler_area_um2,
        tap_area_um2=tap_area_um2,
        utilization_pct=utilization_pct,
        utilization_metric=utilization_metric,
        minimum_geometry_qualification=minimum_geometry_qualification,
    )
    if signoff_gate == "fail" or physical_sanity["gate"] == "fail":
        gate = "fail"
    elif signoff_gate == "pass" and physical_sanity["gate"] == "pass":
        gate = "pass"
    else:
        gate = "unknown"

    gate_reasons = [
        f"signoff.{label}: {metrics.get(key)!r} violation(s)"
        for label, key in checks_by_metric.items()
        if checks[label] == "fail"
    ]
    qualified_density = (
        physical_sanity.get("density", {}).get("qualification_gate") == "pass"
    )
    gate_reasons.extend(
        f"physical_sanity.{label}: {check['reason']}"
        for label, check in physical_sanity["checks"].items()
        if check["status"] == "fail"
        and not (qualified_density and label in {"minimum_utilization", "maximum_filler_ratio"})
    )
    return {
        "status": metric_status,
        "maturity": maturity,
        "gate": gate,
        "gate_reasons": gate_reasons,
        "signoff_gate": signoff_gate,
        "signoff": {
            "gate": signoff_gate,
            "checks": checks,
            "observed_checks": observed_checks,
        },
        "checks": checks,
        "observed_signoff": observed_checks,
        "metric_evidence": metric_evidence,
        "physical_sanity": physical_sanity,
        "timing_diagnostics": timing_diagnostics,
        "setup_ws_ns": metric_evidence["timing__setup__ws"]["value"],
        "setup_tns_ns": metric_evidence["timing__setup__tns"]["value"],
        "hold_ws_ns": metric_evidence["timing__hold__ws"]["value"],
        "hold_tns_ns": metric_evidence["timing__hold__tns"]["value"],
        "max_slew_violation_count": max_slew_violations,
        "max_cap_violation_count": max_cap_violations,
        "logic_area_um2": logic_area_um2,
        "standard_cell_area_um2": standard_cell_area_um2,
        "core_area_um2": core_area_um2,
        "filler_area_um2": filler_area_um2,
        "tap_area_um2": tap_area_um2,
        "total_instance_area_um2": total_instance_area_um2,
        "utilization_pct": utilization_pct,
        "logic_utilization_pct": logic_utilization_pct,
        "filler_ratio_pct": filler_ratio_pct,
        "power_w": metric_evidence["power__total"]["value"],
        "design__die__bbox": metrics.get("design__die__bbox"),
        "design__core__bbox": metrics.get("design__core__bbox"),
    }


def _flatten_paths(value: Any) -> list[str]:
    if isinstance(value, str):
        return [value]
    if isinstance(value, list):
        result: list[str] = []
        for item in value:
            result.extend(_flatten_paths(item))
        return result
    if isinstance(value, dict):
        result = []
        for item in value.values():
            result.extend(_flatten_paths(item))
        return result
    return []


def _valid_gds_stream(path: Path) -> bool:
    """Check GDSII record framing without attempting semantic signoff validation."""
    try:
        size = path.stat().st_size
        if size < 68:
            return False
        offset = 0
        records: list[tuple[int, int, int]] = []
        with path.open("rb") as stream:
            while offset < size:
                header = stream.read(4)
                if len(header) != 4:
                    return False
                length = int.from_bytes(header[:2], "big")
                record_type = header[2]
                data_type = header[3]
                if length < 4 or length % 2 or offset + length > size:
                    return False
                records.append((record_type, data_type, length))
                stream.seek(length - 4, os.SEEK_CUR)
                offset += length
        if offset != size or len(records) < 8:
            return False
        if records[0] != (0, 2, 6) or records[1] != (1, 2, 28):
            return False
        if records[2][0] != 2 or records[2][1] != 6 or records[2][2] < 6:
            return False
        if records[-1] != (4, 0, 4):
            return False
        if any(record_type == 4 for record_type, _, _ in records[:-1]):
            return False
        try:
            units_index = next(
                index for index, record in enumerate(records[3:], 3) if record[0] == 3
            )
        except StopIteration:
            return False
        if records[units_index] != (3, 5, 20):
            return False

        in_structure = False
        structure_has_name = False
        completed_structures = 0
        for record_type, data_type, length in records[units_index + 1 : -1]:
            if record_type == 5:
                if in_structure or data_type != 2 or length != 28:
                    return False
                in_structure = True
                structure_has_name = False
            elif record_type == 6:
                if (
                    not in_structure
                    or structure_has_name
                    or data_type != 6
                    or length < 6
                ):
                    return False
                structure_has_name = True
            elif record_type == 7:
                if (
                    not in_structure
                    or not structure_has_name
                    or data_type != 0
                    or length != 4
                ):
                    return False
                in_structure = False
                completed_structures += 1
        return not in_structure and completed_structures > 0
    except OSError:
        return False


def discover_artifacts(
    run_dir: Path,
    steps: list[dict[str, Any]],
    visualization: Mapping[str, Any],
) -> dict[str, Any]:
    try:
        trusted_run_dir = canonical_directory(run_dir, label="LibreLane run directory")
    except ValueError:
        trusted_run_dir = None
    final = (
        trusted_run_dir / "final" if trusted_run_dir is not None else run_dir / "final"
    )
    gds_candidates: list[Path] = []
    if trusted_run_dir is not None:
        try:
            gds_dir = direct_child_directory(
                final / "gds", root=final, label="final GDS directory"
            )
        except ValueError:
            gds_dir = None
        if gds_dir is not None:
            for candidate in sorted(gds_dir.iterdir()):
                try:
                    gds_candidates.append(
                        canonical_regular_file(
                            candidate,
                            root=gds_dir,
                            label="GDS artifact",
                            minimum_size=1,
                        )
                    )
                except ValueError:
                    continue
    canonical_gds: list[str] = []
    for path in gds_candidates:
        if path.suffix.casefold() != ".gds":
            continue
        if not _valid_gds_stream(path):
            continue
        canonical_gds.append(str(path))
    artifacts: dict[str, Any] = {
        "metrics": None,
        "gds": [str(path) for path in gds_candidates],
        "canonical_gds": canonical_gds,
        "renders": [],
        "latest_state": None,
        "latest_odb": None,
        "visualizations": {
            "directory": visualization.get("directory"),
            "manifest": visualization.get("manifest"),
            "timeline": visualization.get("timeline"),
            "images": visualization.get("images", []),
            "latest": visualization.get("latest_image"),
        },
    }
    if trusted_run_dir is not None:
        try:
            artifacts["metrics"] = str(
                canonical_regular_file(
                    final / "metrics.json",
                    root=trusted_run_dir,
                    label="final metrics",
                    minimum_size=1,
                    maximum_size=64 * 1024 * 1024,
                )
            )
        except (OSError, ValueError):
            pass
        render_dir = final / "render"
        try:
            render_root = direct_child_directory(
                render_dir, root=final, label="final render directory"
            )
        except ValueError:
            render_root = None
        if render_root is not None:
            for render in sorted(render_root.iterdir()):
                try:
                    artifacts["renders"].append(
                        str(
                            canonical_regular_file(
                                render, root=render_root, label="render artifact"
                            )
                        )
                    )
                except ValueError:
                    continue
    for step in reversed(steps):
        if not step["state_out"]:
            continue
        artifacts["latest_state"] = step["state_out"]
        try:
            raw = json.loads(Path(step["state_out"]).read_text(encoding="utf-8"))
            odbs = _flatten_paths(raw.get("odb"))
            if trusted_run_dir is not None:
                for raw_odb in reversed(odbs):
                    try:
                        artifacts["latest_odb"] = str(
                            canonical_regular_file(
                                Path(raw_odb),
                                root=trusted_run_dir,
                                label="latest ODB artifact",
                                minimum_size=1,
                            )
                        )
                        break
                    except ValueError:
                        continue
        except (OSError, json.JSONDecodeError, AttributeError):
            pass
        break
    return artifacts


def _json_copy(value: Any) -> Any:
    return json.loads(
        json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    )


def _stable_artifacts(value: Any) -> dict[str, Any] | None:
    if not isinstance(value, Mapping):
        return None
    stable = dict(value)
    stable.pop("visualizations", None)
    return _json_copy(stable)


def _public_delivery_projection(state: Mapping[str, Any]) -> dict[str, Any]:
    projection: dict[str, Any] = {}
    for key in SEALED_PUBLIC_DELIVERY_FIELDS:
        if key not in state:
            continue
        value = state[key]
        if key == "artifacts":
            value = _stable_artifacts(value)
        projection[key] = _json_copy(value)
    return projection


def _attestation_payload(attestation: Mapping[str, Any]) -> dict[str, Any]:
    payload = dict(attestation)
    claimed_sha256 = payload.pop("attestation_sha256", None)
    actual_sha256 = hashlib.sha256(
        json.dumps(
            payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")
        ).encode("utf-8")
    ).hexdigest()
    if claimed_sha256 != actual_sha256:
        raise ValueError("sealed delivery attestation hash is invalid")
    public_delivery = payload.get("public_delivery")
    if not isinstance(public_delivery, Mapping):
        raise ValueError("sealed delivery attestation has no public delivery manifest")
    required = {
        "status": "completed",
        "process_status": "completed",
        "delivery_gate": "pass",
        "flow_scope": "full",
        "endpoint_reached": True,
    }
    if any(public_delivery.get(key) != expected for key, expected in required.items()):
        raise ValueError("sealed public delivery manifest is not a completed delivery")
    return payload


def _sealed_public_delivery(
    state: Mapping[str, Any], *, verify_terminal_event: bool = False
) -> dict[str, Any]:
    attestation = state.get("delivery_attestation")
    if not isinstance(attestation, Mapping):
        raise ValueError("sealed completed state has no delivery attestation")
    payload = _attestation_payload(attestation)
    release_integrity = state.get("release_integrity")
    if isinstance(release_integrity, Mapping) and release_integrity.get(
        "attestation_sha256"
    ) != attestation.get("attestation_sha256"):
        raise ValueError("release integrity receipt is not bound to the attestation")
    if verify_terminal_event:
        terminal = read_terminal_event(state)
        event_sha256 = (
            (terminal.get("data") or {}).get("delivery_attestation_sha256")
            if isinstance(terminal, Mapping)
            else None
        )
        if event_sha256 != attestation.get("attestation_sha256"):
            raise ValueError(
                "delivery attestation does not match the sealed terminal event"
            )
    return _json_copy(payload["public_delivery"])


def _delivery_attestation(state: Mapping[str, Any]) -> dict[str, Any]:
    """Bind a successful delivery to the exact metrics and GDS file contents."""

    if state.get("status") != "completed" or state.get("delivery_gate") != "pass":
        raise ValueError("only a completed passing delivery can be attested")
    run_dir = canonical_directory(
        Path(str(state["librelane_run_dir"])), label="delivery attestation run"
    )
    artifacts = state.get("artifacts")
    if not isinstance(artifacts, Mapping):
        raise ValueError("delivery artifacts are missing")
    metrics_path = state.get("metrics_path")
    canonical_gds = state.get("canonical_gds")
    if not isinstance(metrics_path, str) or not metrics_path:
        raise ValueError("final metrics are missing from the delivery")
    if (
        not isinstance(canonical_gds, list)
        or not canonical_gds
        or len(canonical_gds) > 32
        or any(not isinstance(path, str) or not path for path in canonical_gds)
    ):
        raise ValueError("canonical GDS delivery set is missing or unbounded")
    candidates = [("metrics", metrics_path), *[("gds", path) for path in canonical_gds]]
    records: list[dict[str, Any]] = []
    seen: set[str] = set()
    for kind, raw_path in candidates:
        path = canonical_regular_file(
            Path(raw_path),
            root=run_dir,
            label=f"delivery {kind} artifact",
            minimum_size=1,
        )
        relative = path.relative_to(run_dir).as_posix()
        if relative in seen:
            raise ValueError("delivery attestation contains a duplicate artifact")
        seen.add(relative)
        records.append(
            {
                "kind": kind,
                "path": relative,
                "size_bytes": path.stat().st_size,
                "sha256": file_sha256(path),
            }
        )
    records.sort(key=lambda row: (row["kind"], row["path"]))
    payload: dict[str, Any] = {
        "schema_version": 1,
        "run_id": state.get("run_id"),
        "input_bundle_sha256": state.get("input_bundle_sha256"),
        "image_digest": state.get("image_digest"),
        "pdk_integrity_sha256": state.get("pdk_integrity_sha256"),
        "last_completed_step": state.get("last_completed_step"),
        "qor_sha256": hashlib.sha256(
            json.dumps(state.get("qor"), sort_keys=True, separators=(",", ":")).encode(
                "utf-8"
            )
        ).hexdigest(),
        "mapped_equivalence_sha256": hashlib.sha256(
            json.dumps(
                state.get("mapped_equivalence"),
                sort_keys=True,
                separators=(",", ":"),
            ).encode("utf-8")
        ).hexdigest(),
        "public_delivery": _public_delivery_projection(state),
        "files": records,
    }
    payload["attestation_sha256"] = hashlib.sha256(
        json.dumps(
            payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")
        ).encode("utf-8")
    ).hexdigest()
    return payload


def _terminal_release_integrity(
    persisted: Mapping[str, Any],
    refreshed: Mapping[str, Any],
    *,
    audit_id: str,
    evidence_read_error: str | None = None,
) -> dict[str, Any]:
    previous = persisted.get("release_integrity")
    if isinstance(previous, Mapping) and previous.get("status") == "degraded":
        return {
            **dict(previous),
            "schema_version": 1,
            "audit_id": audit_id,
            "checked_at": now_iso(),
        }
    baseline = persisted.get("delivery_attestation")
    baseline_attestation_sha256: str | None = None
    errors: list[str] = []
    status = "verified"
    sealed_public_delivery: Mapping[str, Any] | None = None
    if not isinstance(baseline, Mapping):
        status = "degraded"
        errors.append("sealed completed state has no delivery attestation")
    else:
        claimed_sha256 = baseline.get("attestation_sha256")
        try:
            payload = _attestation_payload(baseline)
        except (TypeError, ValueError) as exc:
            status = "degraded"
            errors.append(str(exc))
        else:
            baseline_attestation_sha256 = str(claimed_sha256)
            sealed_public_delivery = payload["public_delivery"]
            try:
                terminal = read_terminal_event(persisted)
            except OSError as exc:
                status = "unknown"
                errors.append(f"terminal event journal could not be read: {exc}")
            except (KeyError, RuntimeError, TypeError, ValueError) as exc:
                status = "degraded"
                errors.append(f"terminal event journal is invalid: {exc}")
            else:
                event_sha256 = (
                    (terminal.get("data") or {}).get("delivery_attestation_sha256")
                    if isinstance(terminal, Mapping)
                    else None
                )
                if event_sha256 != claimed_sha256:
                    status = "degraded"
                    errors.append(
                        "delivery attestation does not match the sealed terminal event"
                    )
    if status == "verified" and sealed_public_delivery is not None:
        observed = _public_delivery_projection(persisted)
        drifted = [
            key
            for key in sealed_public_delivery
            if observed.get(key) != sealed_public_delivery.get(key)
        ]
        if drifted:
            status = "degraded"
            errors.append(
                "persisted public delivery projection changed after completion: "
                + ", ".join(sorted(drifted)[:16])
            )
    if status == "verified" and evidence_read_error is not None:
        status = "unknown"
        errors.append(evidence_read_error)
    if status == "verified" and (
        refreshed.get("status") != "completed"
        or refreshed.get("delivery_gate") != "pass"
    ):
        status = "degraded"
        errors.extend(
            str(reason)
            for reason in (
                refreshed.get("delivery_gate_reasons")
                or ["current delivery evidence no longer passes"]
            )
        )
    elif status == "verified":
        try:
            current = _delivery_attestation(refreshed)
        except OSError as exc:
            status = "unknown"
            errors.append(f"delivery artifacts could not be read: {exc}")
        except (KeyError, TypeError, ValueError) as exc:
            status = "degraded"
            errors.append(str(exc))
        else:
            if current != baseline:
                status = "degraded"
                errors.append(
                    "delivery artifact content or bound QoR evidence changed after completion"
                )
    return {
        "schema_version": 1,
        "audit_id": audit_id,
        "status": status,
        "checked_at": now_iso(),
        "attestation_sha256": baseline_attestation_sha256,
        "errors": errors[:16],
    }


def run_event_report(
    run_id: str | None = None,
    *,
    after_sequence: int = 0,
    limit: int = 256,
    generation: str | None = None,
    cursor_sha256: str | None = None,
) -> dict[str, Any]:
    """Return authoritative controller events plus a pure current snapshot."""
    state = refresh_status(load_state(run_id))
    replay = read_event_journal(
        state,
        after_sequence=after_sequence,
        limit=limit,
        generation=generation,
        cursor_sha256=cursor_sha256,
    )
    return {
        "schema_version": 3,
        "contract": "pi-lane.events.v3",
        "run_id": state["run_id"],
        "workflow_id": state.get("workflow_id") or state["run_id"],
        "attempt_id": state.get("attempt_id") or state["run_id"],
        "origin": state.get("origin") or {"component": "lane-agent"},
        "durable": True,
        "delivery": "pull_replay",
        "event_source": "pi_lane_controller",
        "authoritative": True,
        "visualization_independent": True,
        "live_push_requires_connected_monitor": True,
        **replay,
        "snapshot": {
            "state_revision": managed_state_revision(state),
            "updated_at": state.get("updated_at"),
            "status": state.get("status"),
            "process_status": state.get("process_status"),
            "coarse_phase": state.get("coarse_phase"),
            "active_step": state.get("active_step"),
            "last_completed_step": state.get("last_completed_step"),
            "completed_step_count": state.get("completed_step_count", 0),
            "elapsed_seconds": state.get("elapsed_seconds", 0),
            "delivery_gate": state.get("delivery_gate"),
            "delivery_gate_reasons": state.get("delivery_gate_reasons") or [],
            "mapped_equivalence": state.get("mapped_equivalence"),
            "assurance": state.get("assurance"),
            "failure": state.get("failure"),
            "terminal_event_id": state.get("terminal_event_id"),
            "errors": state.get("errors") or [],
        },
    }


def _flow_delivery_scope(state: Mapping[str, Any]) -> tuple[str, str | None]:
    endpoints = FLOW_ENDPOINTS.get(str(state.get("flow", "")).lower())
    if endpoints is None:
        return ("partial" if state.get("from") or state.get("to") else "full", None)
    _, final_step = endpoints
    frm = state.get("from")
    to = state.get("to")
    resume = state.get("resume") or {}
    resume_covers_start = (
        isinstance(resume, Mapping) and resume.get("chain_verified") is True
    )
    pinned_root = (
        state.get("flow_registry_version") == CLASSIC_REGISTRY_VERSION
        and state.get("image") == LIBRELANE_IMAGE
        and state.get("librelane_version") == LIBRELANE_VERSION
        and isinstance(state.get("pdk_integrity_sha256"), str)
        and re.fullmatch(r"[0-9a-f]{64}", str(state.get("pdk_integrity_sha256")))
        is not None
    )
    starts_at_first = (
        frm is None and state.get("initial_state") is None and pinned_root
    ) or resume_covers_start
    ends_at_final = to is None or str(to).casefold() == final_step.casefold()
    return ("full" if starts_at_first and ends_at_final else "partial", final_step)


def _runtime_environment_attestation(
    state: Mapping[str, Any],
) -> tuple[dict[str, Any], list[str]]:
    """Recheck the content-addressed image and complete PDK tree once at delivery."""
    errors: list[str] = []
    image_digest = state.get("image_digest")
    runtime_image_ref = state.get("runtime_image_ref")
    if (
        not isinstance(image_digest, str)
        or IMAGE_DIGEST_RE.fullmatch(image_digest) is None
        or runtime_image_ref != image_digest
    ):
        errors.append("runtime image reference is missing or does not match its digest")
        observed_image_id = None
        observed_image_version = None
    else:
        observed_image_id = image_id(runtime_image_ref)
        observed_image_version = image_librelane_version(runtime_image_ref)
        if observed_image_id != image_digest:
            errors.append(
                "pinned runtime image is unavailable or has a different identity"
            )
        if observed_image_version != LIBRELANE_VERSION:
            errors.append(
                "pinned runtime image LibreLane version is unavailable or incompatible"
            )
    image_lock_snapshot = state.get("image_lock")
    try:
        validated_image_lock = _validate_image_lock(image_lock_snapshot)
    except RuntimeError as exc:
        validated_image_lock = None
        errors.append(f"run image lock snapshot is invalid: {exc}")
    else:
        if (
            validated_image_lock.get("image") != state.get("image")
            or validated_image_lock.get("image_digest") != image_digest
            or validated_image_lock.get("librelane_version")
            != state.get("librelane_version")
            or validated_image_lock.get("lock_sha256") != state.get("image_lock_sha256")
        ):
            errors.append("run image lock snapshot does not match the runtime identity")
    container_identity = state.get("container_identity")
    container_runtime_binding = {
        key: container_identity.get(key)
        if isinstance(container_identity, Mapping)
        else None
        for key in ("image", "image_digest", "runtime_image_ref")
    }
    if (
        not isinstance(container_identity, Mapping)
        or container_identity.get("image") != state.get("image")
        or container_identity.get("image_digest") != image_digest
        or container_identity.get("runtime_image_ref") != runtime_image_ref
    ):
        errors.append("main container runtime image binding is missing or inconsistent")
    preflight = state.get("config_preflight")
    preflight_runtime_binding = {
        key: preflight.get(key) if isinstance(preflight, Mapping) else None
        for key in ("image", "image_digest", "runtime_image_ref")
    }
    if (
        not isinstance(preflight, Mapping)
        or preflight.get("image") != state.get("image")
        or preflight.get("image_digest") != image_digest
        or preflight.get("runtime_image_ref") != runtime_image_ref
    ):
        errors.append(
            "configuration preflight runtime image binding is missing or inconsistent"
        )

    pdk_name = state.get("pdk")
    pdk_root_value = state.get("pdk_root")
    pdk_report: dict[str, Any]
    observed_revision: str | None = None
    if not isinstance(pdk_name, str) or not isinstance(pdk_root_value, str):
        pdk_report = {"ready": False, "error": "PDK identity is missing"}
        errors.append("PDK identity is missing from the managed run")
    else:
        pdk_root = Path(pdk_root_value)
        pdk_report = pdk_integrity_report(
            pdk_name,
            pdk_root,
            allow_bootstrap=False,
            verify_content=True,
        )
        observed_revision = installed_pdk_revision(pdk_name, pdk_root)
        if (
            not pdk_report.get("ready")
            or pdk_report.get("content_verified") is not True
            or pdk_report.get("tree_sha256") != state.get("pdk_integrity_sha256")
        ):
            errors.append(
                "current PDK content does not match the complete pinned tree: "
                + str(pdk_report.get("error") or "identity mismatch")
            )
        if (
            observed_revision != state.get("pdk_revision")
            or pdk_name not in PDK_REVISIONS
            or observed_revision != PDK_REVISIONS[pdk_name][1]
        ):
            errors.append("current PDK enabled revision does not match the pinned run")

    attestation = {
        "schema_version": 1,
        "verified": not errors,
        "image": state.get("image"),
        "image_digest": image_digest,
        "runtime_image_ref": runtime_image_ref,
        "image_lock_sha256": state.get("image_lock_sha256"),
        "image_lock_revision": (
            validated_image_lock.get("revision")
            if validated_image_lock is not None
            else None
        ),
        "observed_image_id": observed_image_id,
        "expected_librelane_version": state.get("librelane_version"),
        "librelane_version": observed_image_version,
        "container_runtime_binding": container_runtime_binding,
        "preflight_runtime_binding": preflight_runtime_binding,
        "pdk": pdk_name,
        "pdk_root": pdk_root_value,
        "pdk_revision": state.get("pdk_revision"),
        "observed_pdk_revision": observed_revision,
        "pdk_integrity_sha256": state.get("pdk_integrity_sha256"),
        "observed_pdk_integrity_sha256": pdk_report.get("tree_sha256"),
        "pdk_file_count": pdk_report.get("file_count"),
        "pdk_total_bytes": pdk_report.get("total_bytes"),
        "errors": errors,
    }
    return attestation, errors


def _delivery_status(
    state: Mapping[str, Any],
    *,
    process_status: str,
    completed_steps: list[dict[str, Any]],
    qor: Mapping[str, Any],
    artifacts: Mapping[str, Any],
    mapped_equivalence: Mapping[str, Any],
) -> dict[str, Any]:
    flow_scope, expected_final_step = _flow_delivery_scope(state)
    last_completed_step = completed_steps[-1]["id"] if completed_steps else None
    endpoint_reached = bool(
        expected_final_step
        and last_completed_step
        and str(last_completed_step).casefold() == expected_final_step.casefold()
    )
    canonical_gds = list(artifacts.get("canonical_gds") or [])
    identity_errors: list[str] = []
    if str(state.get("flow", "")).casefold() != "classic":
        identity_errors.append("run is not bound to the Classic flow")
    if state.get("flow_registry_version") != CLASSIC_REGISTRY_VERSION:
        identity_errors.append("Classic registry version is missing or incompatible")
    if (
        state.get("image") != LIBRELANE_IMAGE
        or state.get("librelane_version") != LIBRELANE_VERSION
    ):
        identity_errors.append("run is not bound to the pinned LibreLane image/version")
    if (
        not isinstance(state.get("image_digest"), str)
        or re.fullmatch(r"sha256:[0-9a-f]{64}", str(state.get("image_digest"))) is None
    ):
        identity_errors.append("run has no valid LibreLane image digest binding")
    if (
        not isinstance(state.get("config_sha256"), str)
        or re.fullmatch(r"[0-9a-f]{64}", str(state.get("config_sha256"))) is None
    ):
        identity_errors.append("run has no valid configuration content binding")
    if (
        not isinstance(state.get("pdk_integrity_sha256"), str)
        or re.fullmatch(r"[0-9a-f]{64}", str(state.get("pdk_integrity_sha256"))) is None
    ):
        identity_errors.append("run has no PDK content integrity binding")
    pdk_name = state.get("pdk")
    if (
        not isinstance(pdk_name, str)
        or pdk_name not in PDK_REVISIONS
        or state.get("pdk_revision") != PDK_REVISIONS[pdk_name][1]
    ):
        identity_errors.append("run has no pinned PDK revision binding")
    if process_status == "completed":
        input_bundle_attested = False
        runtime_identity_attested = False
        runtime_attestation: dict[str, Any] | None = None
        try:
            design_root = canonical_directory(
                Path(str(state["design_dir"])), label="delivery design directory"
            )
            snapshot_errors = _verify_design_snapshot(state)
            identity_errors.extend(snapshot_errors)
            input_bundle_attested = not snapshot_errors
            runs_root = canonical_directory(
                design_root / "runs", label="delivery runs directory"
            )
            managed_run = direct_child_directory(
                Path(str(state["librelane_run_dir"])),
                root=runs_root,
                label="delivery run directory",
            )
            if managed_run.name != f"PI_LANE_{state.get('run_id')}":
                identity_errors.append("delivery run directory does not match run ID")
        except (KeyError, ValueError) as exc:
            identity_errors.append(f"delivery filesystem identity is invalid: {exc}")
        persisted_runtime = state.get("runtime_environment_attestation")
        if (
            state.get("status") in TERMINAL_STATES
            and isinstance(persisted_runtime, Mapping)
            and persisted_runtime.get("image_digest") == state.get("image_digest")
            and persisted_runtime.get("runtime_image_ref")
            == state.get("runtime_image_ref")
            and persisted_runtime.get("image_lock_sha256")
            == state.get("image_lock_sha256")
            and persisted_runtime.get("image") == state.get("image")
            and persisted_runtime.get("expected_librelane_version")
            == state.get("librelane_version")
            and persisted_runtime.get("container_runtime_binding")
            == {
                key: state.get("container_identity", {}).get(key)
                if isinstance(state.get("container_identity"), Mapping)
                else None
                for key in ("image", "image_digest", "runtime_image_ref")
            }
            and persisted_runtime.get("preflight_runtime_binding")
            == {
                key: state.get("config_preflight", {}).get(key)
                if isinstance(state.get("config_preflight"), Mapping)
                else None
                for key in ("image", "image_digest", "runtime_image_ref")
            }
            and persisted_runtime.get("pdk") == state.get("pdk")
            and persisted_runtime.get("pdk_root") == state.get("pdk_root")
            and persisted_runtime.get("pdk_revision") == state.get("pdk_revision")
            and persisted_runtime.get("pdk_integrity_sha256")
            == state.get("pdk_integrity_sha256")
        ):
            runtime_attestation = dict(persisted_runtime)
            persisted_errors = runtime_attestation.get("errors")
            if not isinstance(persisted_errors, list):
                persisted_errors = [
                    "persisted runtime environment attestation is malformed"
                ]
            identity_errors.extend(str(error) for error in persisted_errors)
            runtime_identity_attested = (
                runtime_attestation.get("verified") is True and not persisted_errors
            )
        else:
            runtime_attestation, runtime_errors = _runtime_environment_attestation(
                state
            )
            identity_errors.extend(runtime_errors)
            runtime_identity_attested = not runtime_errors
        identity_errors.extend(geometry_drift_errors(state.get("physical_plan"), qor))
        physical_binding = state.get("physical_plan")
        if physical_binding is not None:
            try:
                if not isinstance(physical_binding, Mapping):
                    raise ValueError("physical plan binding is malformed")
                plan_snapshot = load_plan(Path(str(physical_binding.get("path") or "")))
                if (
                    plan_snapshot.get("plan_sha256")
                    != physical_binding.get("plan_sha256")
                    or plan_snapshot.get("geometry_lock_sha256")
                    != physical_binding.get("geometry_lock_sha256")
                    or plan_snapshot.get("config_sha256") != state.get("config_sha256")
                    or plan_snapshot.get("input_bundle_sha256")
                    != state.get("input_bundle_sha256")
                ):
                    raise ValueError("physical plan snapshot binding changed")
            except (OSError, ValueError, json.JSONDecodeError) as exc:
                identity_errors.append(f"physical plan integrity is invalid: {exc}")
    else:
        input_bundle_attested = False
        runtime_identity_attested = False
        runtime_attestation = None

    if process_status == "running":
        gate = "not_applicable"
        reasons = ["LibreLane execution is still running"]
    elif process_status == "stopped":
        gate = "not_applicable"
        reasons = ["LibreLane execution was stopped before delivery evaluation"]
    elif process_status != "completed":
        gate = "fail"
        reasons = ["LibreLane execution did not complete successfully"]
    elif identity_errors:
        gate = "fail"
        reasons = identity_errors
    elif flow_scope == "partial":
        resume_record = state.get("resume")
        if resume_record is not None and (
            not isinstance(resume_record, Mapping)
            or resume_record.get("chain_verified") is not True
        ):
            gate = "fail"
            reasons = [
                "resume chain provenance is invalid: "
                + str(
                    resume_record.get("chain_verification_error") or "unverified"
                    if isinstance(resume_record, Mapping)
                    else "malformed resume record"
                )
            ]
        else:
            gate = "not_applicable"
            reasons = [
                "the requested from/to range is partial and is not a complete RTL-to-GDS delivery"
            ]
    else:
        reasons = []
        hard_failure = False
        if expected_final_step is None:
            reasons.append(
                f"the expected final Step is not defined for flow {state.get('flow')!r}"
            )
        elif not endpoint_reached:
            reasons.append(f"expected final Step {expected_final_step} was not reached")
        if not canonical_gds:
            reasons.append(
                "no structurally valid canonical GDSII stream exists under final/gds"
            )
        if (
            mapped_equivalence.get("requested") is True
            and mapped_equivalence.get("gate") != "pass"
        ):
            reasons.extend(
                "mapped_equivalence: " + str(reason)
                for reason in mapped_equivalence.get("reasons", [])
            )
            hard_failure = mapped_equivalence.get("status") in {
                "unsupported",
                "invalid_configuration",
            }
        qor_gate = str(qor.get("gate") or "unknown")
        if qor_gate == "fail":
            reasons.extend(str(reason) for reason in (qor.get("gate_reasons") or []))
            if not qor.get("gate_reasons"):
                reasons.append("final QoR gate failed")
            gate = "fail"
        else:
            if qor_gate != "pass":
                reasons.append(f"final QoR gate is {qor_gate}, not pass")
            gate = "fail" if hard_failure else "pass" if not reasons else "incomplete"

    return {
        "flow_scope": flow_scope,
        "delivery_gate": gate,
        "delivery_gate_reasons": reasons,
        "expected_final_step": expected_final_step,
        "endpoint_reached": endpoint_reached,
        "canonical_gds": canonical_gds,
        "resumed_delivery": bool(state.get("resume")),
        "input_bundle_attested": input_bundle_attested,
        "runtime_identity_attested": runtime_identity_attested,
        "runtime_environment_attestation": runtime_attestation,
    }


def _tail_lines(
    path: Path, count: int = 80, max_bytes: int = 2 * 1024 * 1024
) -> list[str]:
    if not path.is_file():
        return []
    size = path.stat().st_size
    with path.open("rb") as handle:
        if size > max_bytes:
            handle.seek(size - max_bytes)
        return handle.read().decode("utf-8", errors="replace").splitlines()[-count:]


def _structured_failure(state: Mapping[str, Any]) -> dict[str, Any] | None:
    status = state.get("status")
    if status not in {"failed", "incomplete"}:
        return None
    kind = str(state.get("failure_kind") or "librelane_execution")
    code_by_kind = {
        "qor": "LANE_QOR_GATE_FAILED",
        "mapped_equivalence": "LANE_EQUIVALENCE_FAILED",
        "delivery": "LANE_DELIVERY_GATE_FAILED",
        "librelane_execution": "LANE_PROCESS_FAILED",
        "delivery_incomplete": "LANE_DELIVERY_INCOMPLETE",
        "release_integrity": "LANE_DELIVERY_INTEGRITY_DEGRADED",
    }
    domain_by_kind = {
        "qor": "physical_design",
        "mapped_equivalence": "verification",
        "delivery": "delivery",
        "librelane_execution": "librelane",
        "delivery_incomplete": "evidence",
        "release_integrity": "release_integrity",
    }
    errors = [str(value) for value in (state.get("errors") or []) if value]
    message = str(
        state.get("status_detail")
        or (
            errors[-1]
            if errors
            else "managed LibreLane run did not satisfy its terminal gate"
        )
    )
    evidence = []
    for key in ("build_log", "metrics_path", "state_file"):
        value = state.get(key)
        if isinstance(value, str) and value:
            evidence.append({"kind": key, "path": value})
    return {
        "code": code_by_kind.get(kind, "LANE_FAILED"),
        "domain": domain_by_kind.get(kind, "librelane"),
        "kind": kind,
        "message": message,
        "retryable": kind == "librelane_execution",
        "repairable_by_rtl": False,
        "user_action_required": kind
        in {"qor", "mapped_equivalence", "delivery", "release_integrity"},
        "owner": "unclassified" if kind == "librelane_execution" else "lane",
        "caused_by_event_id": state.get("last_event_id"),
        "evidence": evidence,
    }


def _transition_events(
    before: Mapping[str, Any], after: Mapping[str, Any]
) -> list[dict[str, Any]]:
    events: list[dict[str, Any]] = []
    before_revision = managed_state_revision(before)
    before_steps = {
        str(row.get("id"))
        for row in (before.get("step_history") or [])
        if isinstance(row, Mapping) and row.get("status") == "completed"
    }
    for row in after.get("step_history") or []:
        if not isinstance(row, Mapping) or row.get("status") != "completed":
            continue
        step_id = str(row.get("id") or "")
        if step_id and step_id not in before_steps:
            events.append(
                {
                    "type": "step_completed",
                    "dedupe_key": f"step_completed:{step_id}",
                    "phase": row.get("phase") or after.get("coarse_phase"),
                    "message": f"LibreLane Step completed: {step_id}",
                    "data": {
                        "step_id": step_id,
                        "ordinal": row.get("ordinal"),
                        "directory": row.get("directory"),
                        "commands": row.get("commands"),
                        "logs": row.get("logs") or [],
                    },
                }
            )
    if after.get("active_step") and after.get("active_step") != before.get(
        "active_step"
    ):
        active_row = next(
            (
                row
                for row in (after.get("step_history") or [])
                if isinstance(row, Mapping)
                and row.get("id") == after.get("active_step")
            ),
            {},
        )
        events.append(
            {
                "type": "step_started",
                "dedupe_key": (
                    f"step_started:{before_revision}:{after['active_step']}"
                ),
                "phase": after.get("coarse_phase"),
                "message": f"LibreLane Step active: {after['active_step']}",
                "data": {
                    "step_id": after["active_step"],
                    "ordinal": active_row.get("ordinal"),
                    "directory": active_row.get("directory"),
                    "commands": active_row.get("commands"),
                    "logs": active_row.get("logs") or [],
                },
            }
        )
    changed = {
        key: {"from": before.get(key), "to": after.get(key)}
        for key in ("status", "process_status", "coarse_phase", "delivery_gate")
        if before.get(key) != after.get(key)
    }
    if changed:
        events.append(
            {
                "type": "status_changed",
                "dedupe_key": (
                    f"status_changed:{before_revision}:"
                    f"{after.get('status')}:{after.get('coarse_phase')}:"
                    f"{after.get('delivery_gate')}"
                ),
                "phase": after.get("coarse_phase"),
                "status": after.get("status"),
                "severity": "error" if after.get("status") == "failed" else "info",
                "message": "managed LibreLane status changed",
                "data": {"changes": changed},
                "failure": after.get("failure"),
            }
        )
    before_integrity = before.get("release_integrity")
    after_integrity = after.get("release_integrity")
    if (
        isinstance(after_integrity, Mapping)
        and after_integrity.get("status") == "degraded"
        and (
            not isinstance(before_integrity, Mapping)
            or before_integrity.get("status") != "degraded"
        )
    ):
        events.append(
            {
                "type": "delivery_integrity_degraded",
                "dedupe_key": "delivery_integrity_degraded:"
                + str(after_integrity.get("attestation_sha256") or "missing"),
                "phase": "release_integrity",
                "status": after.get("status"),
                "severity": "error",
                "message": "sealed LibreLane delivery evidence changed after completion",
                "data": {
                    "audit_id": after_integrity.get("audit_id"),
                    "attestation_sha256": after_integrity.get("attestation_sha256"),
                    "errors": after_integrity.get("errors") or [],
                },
                "failure": after.get("failure"),
            }
        )
    before_visualization = before.get("visualization") or {}
    after_visualization = after.get("visualization") or {}
    visualization_fields = {
        key: after_visualization.get(key)
        for key in ("status", "visualization_count", "latest_image", "errors")
    }
    if isinstance(before_visualization, Mapping) and any(
        before_visualization.get(key) != value
        for key, value in visualization_fields.items()
    ):
        visualization_errors = after_visualization.get("errors") or []
        events.append(
            {
                "type": "visualization_status_changed",
                "dedupe_key": f"visualization:{before_revision}:"
                f"{after_visualization.get('status')}:"
                f"{after_visualization.get('visualization_count')}",
                "phase": after.get("coarse_phase") or "visualization",
                "severity": "warning" if visualization_errors else "info",
                "message": "LibreLane visualization status changed",
                "data": visualization_fields,
            }
        )
    before_visual_steps = {
        row.get("ordinal"): row
        for row in before_visualization.get("steps", [])
        if isinstance(row, Mapping)
    }
    for row in after_visualization.get("steps", []):
        if not isinstance(row, Mapping):
            continue
        ordinal = row.get("ordinal")
        visual_status = row.get("visualization_status")
        previous = before_visual_steps.get(ordinal) or {}
        if visual_status == previous.get("visualization_status"):
            continue
        if visual_status not in {"completed", "failed"}:
            continue
        events.append(
            {
                "type": f"visualization_{visual_status}",
                "dedupe_key": f"visualization:{ordinal}:{visual_status}",
                "phase": after.get("coarse_phase") or "visualization",
                "severity": "warning" if visual_status == "failed" else "info",
                "message": f"LibreLane visualization {visual_status}",
                "data": {
                    "ordinal": ordinal,
                    "step_id": row.get("id") or row.get("step"),
                    "directory": row.get("directory"),
                    "commands": row.get("commands"),
                    "logs": row.get("logs") or [],
                    "image": row.get("image"),
                    "error": row.get("visualization_error") or row.get("error"),
                    "attempt": row.get("attempt"),
                },
            }
        )
    status = after.get("status")
    if status in TERMINAL_STATES and not after.get("terminal_event_id"):
        event_type = {
            "completed": "run_completed",
            "failed": "run_failed",
            "incomplete": "run_incomplete",
            "stopped": "run_stopped",
        }[str(status)]
        events.append(
            {
                "type": event_type,
                "dedupe_key": "terminal",
                "phase": after.get("coarse_phase") or "terminal",
                "status": status,
                "severity": "error" if status in {"failed", "incomplete"} else "info",
                "message": after.get("status_detail")
                or f"managed LibreLane run {status}",
                "data": {
                    "exit_code": after.get("exit_code"),
                    "delivery_gate": after.get("delivery_gate"),
                    "last_completed_step": after.get("last_completed_step"),
                    "delivery_attestation_sha256": (
                        (after.get("delivery_attestation") or {}).get(
                            "attestation_sha256"
                        )
                        if isinstance(after.get("delivery_attestation"), Mapping)
                        else None
                    ),
                },
                "failure": after.get("failure"),
                "terminal": True,
            }
        )
    return events


def _terminal_auxiliary_projection(
    persisted: Mapping[str, Any], refreshed: Mapping[str, Any]
) -> dict[str, Any]:
    """Keep a sealed run outcome fixed while admitting late sidecar evidence."""

    projected = dict(persisted)
    partial_checkpoint = persisted.get("flow_scope") == "partial"
    try:
        sealed_public_delivery = _sealed_public_delivery(persisted)
    except (KeyError, RuntimeError, TypeError, ValueError):
        sealed_public_delivery = None
        if not partial_checkpoint and (
            persisted.get("status") == "completed"
            or persisted.get("failure_kind") == "release_integrity"
        ):
            projected["metrics_path"] = None
            projected["canonical_gds"] = []
            projected["qor"] = None
            projected["mapped_equivalence"] = None
            projected["assurance"] = None
            projected["artifacts"] = {}
    else:
        prior_integrity = persisted.get("release_integrity")
        preserve_degradation = (
            isinstance(prior_integrity, Mapping)
            and prior_integrity.get("status") == "degraded"
        )
        for key in SEALED_PUBLIC_DELIVERY_FIELDS:
            if preserve_degradation and key in INTEGRITY_DEGRADATION_FIELDS:
                continue
            if key in sealed_public_delivery:
                projected[key] = _json_copy(sealed_public_delivery[key])
            else:
                projected.pop(key, None)
    projected["visualization"] = refreshed.get("visualization")

    persisted_artifacts = projected.get("artifacts")
    refreshed_artifacts = refreshed.get("artifacts")
    artifacts = (
        dict(persisted_artifacts) if isinstance(persisted_artifacts, Mapping) else {}
    )
    if isinstance(refreshed_artifacts, Mapping):
        artifacts["visualizations"] = refreshed_artifacts.get("visualizations")
    if artifacts:
        projected["artifacts"] = artifacts

    persisted_history = persisted.get("step_history")
    refreshed_history = refreshed.get("step_history")
    if isinstance(persisted_history, list) and isinstance(refreshed_history, list):
        refreshed_by_ordinal = {
            row.get("ordinal"): row
            for row in refreshed_history
            if isinstance(row, Mapping)
        }
        history: list[Any] = []
        for persisted_row in persisted_history:
            if not isinstance(persisted_row, Mapping):
                history.append(persisted_row)
                continue
            row = dict(persisted_row)
            fresh = refreshed_by_ordinal.get(row.get("ordinal"))
            if isinstance(fresh, Mapping) and fresh.get("id") == row.get("id"):
                for key in (
                    "visualization_status",
                    "visualization",
                    "visualization_error",
                ):
                    row[key] = fresh.get(key)
            history.append(row)
        projected["step_history"] = history
    return projected


def _terminal_durable_projection(
    persisted: Mapping[str, Any], projected: Mapping[str, Any]
) -> dict[str, Any]:
    """Persist verdicts while retaining the immutable raw terminal snapshot."""

    durable = dict(projected)
    # A deliberately bounded Classic run is a controller checkpoint, not a
    # delivery.  It must retain its latest State so a validated child probe can
    # resume from it.  Full deliveries still use the sealed public projection
    # below, which prevents late filesystem changes from rewriting GDS/QoR.
    if projected.get("flow_scope") == "partial":
        return durable
    for key in SEALED_PUBLIC_DELIVERY_FIELDS:
        if key in INTEGRITY_DEGRADATION_FIELDS:
            continue
        if key == "artifacts":
            persisted_artifacts = persisted.get("artifacts")
            projected_artifacts = projected.get("artifacts")
            artifacts = (
                dict(persisted_artifacts)
                if isinstance(persisted_artifacts, Mapping)
                else {}
            )
            if isinstance(projected_artifacts, Mapping):
                artifacts["visualizations"] = projected_artifacts.get("visualizations")
            if artifacts:
                durable["artifacts"] = artifacts
            else:
                durable.pop("artifacts", None)
            continue
        if key in persisted:
            durable[key] = persisted[key]
        else:
            durable.pop(key, None)
    return durable


def refresh_status(
    state: dict[str, Any], *, integrity_audit_id: str | None = None
) -> dict[str, Any]:
    """Compute a fresh snapshot without mutating the persistent run record."""
    state = dict(state)
    if state.get("schema_version") != RUN_STATE_SCHEMA_VERSION:
        raise RuntimeError(
            f"managed run state schema is unsupported; expected {RUN_STATE_SCHEMA_VERSION}"
        )
    if "revision" in state or "state_revision" not in state:
        raise RuntimeError(
            "managed run state requires state_revision and rejects legacy revision"
        )
    managed_state_revision(state)
    audit_id = _validate_tracking_id(integrity_audit_id, label="integrity audit id")
    persisted_terminal = (
        dict(state)
        if state.get("status") in TERMINAL_STATES
        and isinstance(state.get("terminal_event_id"), str)
        and bool(state.get("terminal_event_id"))
        else None
    )
    exit_code = _read_exit_code(Path(state["exit_file"]))
    launch_epoch = state.get("launch_requested_epoch")
    if (
        state.get("status") == "launching"
        and state.get("pid") is None
        and exit_code is None
        and isinstance(launch_epoch, (int, float))
        and not isinstance(launch_epoch, bool)
        and 0 <= time.time() - float(launch_epoch) <= LAUNCH_REGISTRATION_GRACE_SECONDS
    ):
        # Keep the durable launch request byte-for-byte stable so a concurrent
        # reconcile cannot consume the registration writer's CAS revision.
        return state
    running = pid_identity_alive(state.get("pid"), state.get("pid_start_time"))
    if state.get("stop_requested") and not running:
        process_status = "stopped"
    elif exit_code is not None:
        process_status = "completed" if exit_code == 0 else "failed"
    elif running:
        process_status = "running"
    elif state.get("status") in TERMINAL_STATES:
        process_status = str(state.get("process_status") or state["status"])
    else:
        process_status = "failed"
    run_dir = Path(state["librelane_run_dir"])
    steps = discover_steps(run_dir)
    incomplete = [step for step in steps if not step["completed"]]
    completed = [step for step in steps if step["completed"]]
    active_step = incomplete[-1]["id"] if incomplete else None
    if active_step is None and process_status == "running" and completed:
        active_step = completed[-1]["id"]
    metrics, metric_status, metric_path, metrics_read_error = _load_metrics(
        run_dir, steps
    )
    metric_source_sha256 = None
    if isinstance(metric_path, str):
        try:
            metric_source_sha256 = file_sha256(
                canonical_regular_file(
                    Path(metric_path),
                    root=run_dir,
                    label="QoR metrics evidence",
                    minimum_size=1,
                    maximum_size=64 * 1024 * 1024,
                )
            )
        except (OSError, ValueError):
            pass
    last_completed_step = completed[-1]["id"] if completed else None
    physical_plan_for_qor: Mapping[str, Any] | None = None
    physical_binding = state.get("physical_plan")
    if isinstance(physical_binding, Mapping) and isinstance(
        physical_binding.get("path"), str
    ):
        try:
            physical_plan_for_qor = load_plan(Path(str(physical_binding["path"])))
        except (OSError, ValueError, json.JSONDecodeError):
            # Delivery identity validation below remains the authoritative
            # failure path; QoR qualification must fail closed without a plan.
            physical_plan_for_qor = None
    qor = summarize_qor(
        metrics,
        metric_status,
        observed_at_step=last_completed_step,
        source_sha256=metric_source_sha256,
        tool_image_digest=state.get("image_digest"),
        terminal_maturity=(
            process_status == "completed"
            and last_completed_step == CLASSIC_TERMINAL_STEP
        ),
        physical_plan=physical_plan_for_qor,
    )
    visualization = discover_visualization(state)
    artifacts = discover_artifacts(run_dir, steps, visualization)
    if isinstance(state.get("resume"), Mapping):
        resume_status = dict(state["resume"])
        try:
            chain_proof = verify_resume_chain(state, load_state)
            resume_status.update(
                {
                    "chain_verified": True,
                    "chain_verification_error": None,
                    "verified_root_run_id": chain_proof["root_run_id"],
                    "verified_chain_depth": chain_proof["chain_depth"],
                    "verified_chain_proof_sha256": chain_proof["chain_proof_sha256"],
                }
            )
        except (
            OSError,
            RuntimeError,
            ValueError,
            KeyError,
            json.JSONDecodeError,
        ) as exc:
            resume_status.update(
                {
                    "chain_verified": False,
                    "chain_verification_error": str(exc),
                }
            )
        state["resume"] = resume_status
    mapped_equivalence = evaluate_eqy_assurance(
        {
            "RUN_EQY": state.get("run_eqy_requested", False),
            "EQY_SCRIPT_SHA256": state.get("eqy_script_sha256"),
            "EQY_GATE_TCL_SHA256": state.get("eqy_gate_tcl_sha256"),
        },
        str(state["pdk"]),
        steps,
        run_id=str(state["run_id"]),
        run_dir=run_dir,
        design_dir=state.get("design_dir"),
        input_snapshot_dir=state.get("input_snapshot_dir"),
    )
    if (
        process_status == "running"
        and mapped_equivalence.get("requested") is True
        and mapped_equivalence.get("status") == "incomplete"
    ):
        mapped_equivalence = {
            **mapped_equivalence,
            "status": "pending",
            "gate": "not_applicable",
            "reasons": ["Yosys.EQY evidence is pending while LibreLane is running"],
        }
    assurance = open_pdk_assurance(str(state["pdk"]))
    # State is persisted as JSON; keep the in-memory projection JSON-stable so
    # terminal reconciliation does not mistake tuple-to-list encoding for drift.
    assurance["limitations"] = list(assurance.get("limitations") or ())
    delivery = _delivery_status(
        state,
        process_status=process_status,
        completed_steps=completed,
        qor=qor,
        artifacts=artifacts,
        mapped_equivalence=mapped_equivalence,
    )
    delivery_failed = (
        process_status == "completed" and delivery["delivery_gate"] == "fail"
    )
    qor_failed = delivery_failed and qor.get("gate") == "fail"
    mapped_equivalence_failed = (
        delivery_failed
        and mapped_equivalence.get("requested") is True
        and mapped_equivalence.get("gate") == "fail"
    )
    delivery_incomplete = (
        process_status == "completed"
        and delivery["flow_scope"] == "full"
        and delivery["delivery_gate"] == "incomplete"
    )
    if delivery_failed:
        status = "failed"
    elif delivery_incomplete:
        status = "incomplete"
    else:
        status = process_status
    _freeze_elapsed(state, status, Path(state["exit_file"]))
    step_history = build_step_history(steps, visualization)
    errors: list[str] = []
    for path in (run_dir / "error.log", Path(state["build_log"])):
        for line in _tail_lines(path):
            clean = ANSI_ESCAPE_RE.sub("", line)
            if ERROR_RE.search(clean):
                stripped = clean.strip()
                if stripped and stripped not in errors:
                    errors.append(stripped)
    if process_status == "failed" and not errors:
        fallback = [
            ANSI_ESCAPE_RE.sub("", line).strip()
            for line in _tail_lines(Path(state["build_log"]), 12)
            if ANSI_ESCAPE_RE.sub("", line).strip()
        ]
        errors.extend(fallback[-8:])
    if delivery_failed:
        reasons = delivery.get("delivery_gate_reasons") or [
            "final delivery gate failed"
        ]
        for reason in reasons:
            prefix = "QoR" if qor_failed else "DELIVERY"
            message = f"[{prefix} ERROR] {reason}"
            if message not in errors:
                errors.append(message)
    status_detail = None
    if qor_failed:
        status_detail = (
            "LibreLane exited with code 0, but the final QoR gate failed; "
            "inspect qor.gate_reasons and qor.physical_sanity"
        )
    elif delivery_failed:
        status_detail = "; ".join(delivery["delivery_gate_reasons"])
    elif delivery_incomplete:
        status_detail = "; ".join(delivery["delivery_gate_reasons"])
    elif process_status == "failed":
        status_detail = (
            f"LibreLane process exited with code {exit_code}"
            if exit_code is not None
            else "LibreLane worker disappeared without recording an exit code"
        )
    elif process_status == "completed" and delivery["flow_scope"] == "partial":
        status_detail = delivery["delivery_gate_reasons"][0]
    state.update(
        {
            "status": status,
            "process_status": process_status,
            "failure_kind": (
                "qor"
                if qor_failed
                else "mapped_equivalence"
                if mapped_equivalence_failed
                else "delivery"
                if delivery_failed
                else "delivery_incomplete"
                if delivery_incomplete
                else "librelane_execution"
                if process_status == "failed"
                else None
            ),
            "status_detail": status_detail,
            "is_running": running,
            "exit_code": exit_code,
            "elapsed_seconds": elapsed_seconds(state),
            "active_step": active_step,
            "coarse_phase": (
                "qor_gate"
                if qor_failed
                else "mapped_equivalence_gate"
                if mapped_equivalence_failed
                else "delivery_gate"
                if delivery_failed
                else "delivery_gate"
                if delivery_incomplete
                else "partial_complete"
                if process_status == "completed" and delivery["flow_scope"] == "partial"
                else _coarse_phase(active_step)
            ),
            "last_completed_step": last_completed_step,
            "completed_step_count": len(completed),
            "started_step_count": len(steps),
            "failed_step": incomplete[-1]["id"]
            if status == "failed" and incomplete
            else None,
            "metrics_path": metric_path,
            "qor": qor,
            "step_history": step_history,
            "visualization": visualization,
            "artifacts": artifacts,
            "mapped_equivalence": mapped_equivalence,
            "assurance": assurance,
            "errors": errors[-8:],
            **delivery,
        }
    )
    state["failure"] = _structured_failure(state)
    if persisted_terminal is not None:
        projected = _terminal_auxiliary_projection(persisted_terminal, state)
        if audit_id is None:
            return projected
        integrity = _terminal_release_integrity(
            persisted_terminal,
            state,
            audit_id=audit_id,
            evidence_read_error=metrics_read_error,
        )
        projected["release_integrity"] = integrity
        if integrity["status"] == "degraded" and projected.get("status") == "completed":
            projected.update(
                {
                    "status": "incomplete",
                    "failure_kind": "release_integrity",
                    "delivery_gate": "incomplete",
                    "delivery_gate_reasons": list(integrity["errors"]),
                    "status_detail": "; ".join(integrity["errors"]),
                    "coarse_phase": "release_integrity",
                }
            )
            projected["failure"] = _structured_failure(projected)
        return projected
    if state.get("status") == "completed" and state.get("delivery_gate") == "pass":
        audit_id = audit_id or f"lane-audit-{time.time_ns()}"
        try:
            state["delivery_attestation"] = _delivery_attestation(state)
        except (OSError, KeyError, TypeError, ValueError) as exc:
            state.update(
                {
                    "status": "incomplete",
                    "failure_kind": "release_integrity",
                    "delivery_gate": "incomplete",
                    "delivery_gate_reasons": [
                        f"delivery attestation could not be created: {exc}"
                    ],
                    "status_detail": f"delivery attestation could not be created: {exc}",
                    "coarse_phase": "release_integrity",
                }
            )
            state["failure"] = _structured_failure(state)
        else:
            state["release_integrity"] = {
                "schema_version": 1,
                "audit_id": audit_id,
                "status": "verified",
                "checked_at": now_iso(),
                "attestation_sha256": state["delivery_attestation"][
                    "attestation_sha256"
                ],
                "errors": [],
            }
    return state


def reconcile_status(
    run_id: str | None = None,
    *,
    state: Mapping[str, Any] | None = None,
    integrity_audit_id: str | None = None,
) -> dict[str, Any]:
    """Explicitly project filesystem/process evidence into state and events."""
    initial = dict(state) if state is not None else load_state(run_id)
    state_file = Path(str(initial["state_file"]))
    with run_lock(state_file):
        current_raw = json.loads(state_file.read_text(encoding="utf-8"))
        if not isinstance(current_raw, dict):
            raise RuntimeError("managed state is not a JSON object")
        current_revision = managed_state_revision(current_raw)
        audit_id = integrity_audit_id or f"lane-audit-{time.time_ns()}"
        projected = refresh_status(current_raw, integrity_audit_id=audit_id)
        events = _transition_events(current_raw, projected)
        durable_projected = (
            _terminal_durable_projection(current_raw, projected)
            if current_raw.get("status") in TERMINAL_STATES
            and isinstance(current_raw.get("terminal_event_id"), str)
            and bool(current_raw.get("terminal_event_id"))
            else projected
        )
        comparable_current = {
            key: value
            for key, value in current_raw.items()
            if key not in {"state_revision", "updated_at"}
        }
        comparable_projected = {
            key: value
            for key, value in durable_projected.items()
            if key not in {"state_revision", "updated_at"}
        }
        if not events and comparable_current == comparable_projected:
            return projected
        committed = commit_state(
            durable_projected,
            expected_revision=current_revision,
            events=events,
            locked=True,
        )
        result = dict(projected)
        for key in (
            "state_revision",
            "updated_at",
            "event_sequence",
            "last_event_id",
            "last_event_sha256",
            "terminal_event_id",
            "terminal_event_type",
        ):
            if key in committed:
                result[key] = committed[key]
        return result


def resume_run(
    source_run_id: str,
    *,
    frm: str | None = None,
    to: str | None = None,
    allow_concurrent: bool = False,
) -> dict[str, Any]:
    """Create a new run from one verified, immutable parent Step checkpoint."""
    source = refresh_status(load_state(source_run_id))
    snapshot_errors = _verify_design_snapshot(source)
    if snapshot_errors:
        raise ValueError("; ".join(snapshot_errors))
    source_input_bundle = source.get("input_bundle_sha256")
    if (
        not isinstance(source_input_bundle, str)
        or re.fullmatch(r"[0-9a-f]{64}", source_input_bundle) is None
    ):
        raise ValueError("resume source has no valid design input bundle identity")
    config = load_design_config(
        Path(source["config_path"]),
        pdk_override=str(source["pdk"]),
        flow_override=str(source["flow"]),
    )
    image = validate_image_name(str(source["image"]))
    current_image_digest = str(require_locked_image(image)["image_digest"])
    plan = build_resume_plan(
        source,
        config,
        current_image_digest=current_image_digest,
        current_pdk_revision=installed_pdk_revision(
            config.pdk, Path(source["pdk_root"])
        ),
        current_pdk_integrity_sha256=pdk_integrity_report(
            config.pdk, Path(source["pdk_root"]), allow_bootstrap=False
        ).get("tree_sha256"),
        state_loader=load_state,
        requested_from=frm,
        requested_to=to,
    )
    return start_run(
        config.path,
        pdk=config.pdk,
        flow=config.flow,
        pdk_root=Path(source["pdk_root"]),
        overrides=tuple(source.get("overrides") or ()),
        frm=plan.frm,
        to=plan.to,
        jobs=source.get("jobs"),
        allow_concurrent=allow_concurrent,
        image=image,
        initial_state=plan.initial_state,
        initial_state_sha256=plan.initial_state_sha256,
        parent_run_id=plan.parent_run_id,
        resume_root_run_id=plan.root_run_id,
        resume_chain_depth=plan.chain_depth,
        resume_parent_state_attestation_sha256=plan.parent_state_attestation_sha256,
        resume_parent_chain_proof_sha256=plan.parent_chain_proof_sha256,
        workflow_id=str(source.get("workflow_id") or plan.root_run_id),
        origin="lane-agent-resume",
        expected_input_bundle_sha256=source_input_bundle,
    )


def start_planning_probe(
    source_run_id: str,
    *,
    plan_id: str,
    candidate_id: str,
    policy_sha256: str,
    overrides: Iterable[str],
    to: str,
    jobs: int | None = None,
) -> dict[str, Any]:
    """Start one controller-owned geometry probe from a frozen synthesis baseline."""

    source = reconcile_status(source_run_id)
    if source.get("process_status") != "completed":
        raise ValueError("physical planning baseline is not complete")
    if source.get("last_completed_step") != "OpenROAD.STAPrePNR":
        raise ValueError("physical planning baseline must end at OpenROAD.STAPrePNR")
    if (
        source.get("planning_probe") is not None
        or source.get("physical_plan") is not None
    ):
        raise ValueError(
            "physical planning baseline has an incompatible execution identity"
        )
    if (source.get("origin") or {}).get("component") != "physical-planner-baseline":
        raise ValueError("physical planning baseline origin is invalid")
    artifacts = source.get("artifacts")
    checkpoint = (
        artifacts.get("latest_state") if isinstance(artifacts, Mapping) else None
    )
    if not isinstance(checkpoint, str):
        raise ValueError("physical planning baseline has no State checkpoint")
    initial_state, state_sha256 = validate_initial_state(
        Path(checkpoint), Path(str(source["design_dir"]))
    )
    probe = {
        "schema_version": 1,
        "plan_id": plan_id,
        "candidate_id": candidate_id,
        "policy_sha256": policy_sha256,
        "parent_run_id": source_run_id,
        "parent_run_dir": str(source["librelane_run_dir"]),
        "baseline_state_sha256": state_sha256,
    }
    return start_run(
        Path(str(source["config_path"])),
        pdk=str(source["pdk"]),
        flow=str(source["flow"]),
        pdk_root=Path(str(source["pdk_root"])),
        overrides=overrides,
        frm="OpenROAD.Floorplan",
        to=to,
        jobs=jobs if jobs is not None else source.get("jobs"),
        image=str(source["image"]),
        initial_state=initial_state,
        initial_state_sha256=state_sha256,
        parent_run_id=source_run_id,
        workflow_id=plan_id,
        attempt_id=candidate_id,
        origin="physical-planner-probe",
        expected_input_bundle_sha256=str(source["input_bundle_sha256"]),
        planning_probe=probe,
        visualization_enabled=False,
        claim_current=False,
    )


@_serialized_start
def start_visualization(
    run_id: str | None = None, *, retry_failed: bool = False
) -> dict[str, Any]:
    state = refresh_status(load_state(run_id))
    visualization = state.get("visualization") or {}
    if visualization.get("is_running") and retry_failed:
        raise RuntimeError("cannot retry visualizations while the sidecar is running")
    if visualization.get("is_running"):
        return state
    visualization_status = visualization.get("status")
    if visualization_status == "completed":
        return state
    if visualization_status == "completed_with_errors" and not retry_failed:
        return state
    if retry_failed and visualization_status != "completed_with_errors":
        raise RuntimeError(
            "--retry-failed requires visualization status completed_with_errors"
        )

    if state.get("flow_registry_version") != CLASSIC_REGISTRY_VERSION:
        raise RuntimeError(
            "visualization requires the pinned Classic registry identity"
        )
    validate_image_name(str(state.get("image") or ""))
    runtime_image_ref = validate_runtime_image_ref(
        str(state.get("runtime_image_ref") or state.get("image_digest") or "")
    )
    current_image_digest = image_id(runtime_image_ref)
    current_image_version = image_librelane_version(runtime_image_ref)
    if (
        runtime_image_ref != state.get("image_digest")
        or current_image_digest != state.get("image_digest")
        or current_image_version != LIBRELANE_VERSION
        or current_image_version != state.get("librelane_version")
    ):
        raise RuntimeError("visualization image identity changed since the run started")
    canonical_directory(Path(str(state["design_dir"])), label="design directory")
    snapshot_errors = _verify_design_snapshot(state)
    if snapshot_errors:
        raise RuntimeError("; ".join(snapshot_errors))
    input_snapshot_dir = canonical_directory(
        Path(str(state["input_snapshot_dir"])), label="design input snapshot"
    )
    input_bundle_sha256 = str(state["input_bundle_sha256"])
    pdk_report = pdk_integrity_report(
        str(state["pdk"]), Path(str(state["pdk_root"])), allow_bootstrap=False
    )
    if (
        not pdk_report.get("ready")
        or pdk_report.get("tree_sha256") != state.get("pdk_integrity_sha256")
        or installed_pdk_revision(str(state["pdk"]), Path(str(state["pdk_root"])))
        != state.get("pdk_revision")
    ):
        raise RuntimeError("visualization PDK identity changed since the run started")

    controller_dir = Path(state["controller_dir"])
    visualizer_exit_file = controller_dir / "visualizer_exit_code"
    visualizer_log = controller_dir / "visualizer.log"
    visualizer_exit_file.unlink(missing_ok=True)
    main_pid = state.get("pid")
    main_pid_start_time = state.get("pid_start_time")
    if (
        isinstance(main_pid, bool)
        or not isinstance(main_pid, int)
        or main_pid <= 0
        or isinstance(main_pid_start_time, bool)
        or not isinstance(main_pid_start_time, int)
        or main_pid_start_time <= 0
    ):
        raise RuntimeError("visualization requires a persisted main process identity")
    command = build_visualizer_command(
        run_id=state["run_id"],
        run_dir=Path(state["librelane_run_dir"]),
        design_dir=Path(state["design_dir"]),
        design_name=state["design_name"],
        config_path=Path(state["config_path"]),
        input_snapshot_dir=input_snapshot_dir,
        input_bundle_sha256=input_bundle_sha256,
        pdk=state["pdk"],
        pdk_root=Path(state["pdk_root"]),
        image=state["image"],
        runtime_image_ref=runtime_image_ref,
        config_sha256=state["config_sha256"],
        pdk_revision=state["pdk_revision"],
        pdk_integrity_sha256=state["pdk_integrity_sha256"],
        image_digest=state["image_digest"],
        librelane_version=state["librelane_version"],
        main_exit_file=Path(state["exit_file"]),
        main_pid=main_pid,
        main_pid_start_time=main_pid_start_time,
        visualizer_exit_file=visualizer_exit_file,
        overrides=state.get("overrides") or [],
        retry_failed=retry_failed,
    )
    spawned = _spawn_visualizer(
        command,
        cwd=Path(state["design_dir"]),
        log_path=visualizer_log,
    )
    if spawned is None:
        raise RuntimeError("visualization sidecar could not establish a PID identity")
    visualizer_pid, visualizer_pid_start_time = spawned
    state.update(
        {
            "schema_version": RUN_STATE_SCHEMA_VERSION,
            "visualization_dir": str(
                Path(state["librelane_run_dir"]) / "visualizations"
            ),
            "visualizer_pid": visualizer_pid,
            "visualizer_pid_start_time": visualizer_pid_start_time,
            "visualizer_launch_attempted": True,
            "visualizer_log": str(visualizer_log),
            "visualizer_exit_file": str(visualizer_exit_file),
        }
    )
    try:
        save_state(state)
    except Exception:
        _terminate_spawned_process(visualizer_pid)
        raise
    return refresh_status(state)


def list_runs(
    *, active_only: bool = False, limit: int | None = None
) -> list[dict[str, Any]]:
    if limit is not None and not 1 <= limit <= 1000:
        raise ValueError("limit must be from 1 to 1000")
    states = []
    if not STATE_ROOT.is_dir():
        return states
    for path in sorted(STATE_ROOT.glob("*/state.json"), reverse=True):
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
            if not isinstance(raw, dict):
                continue
            run_id = raw.get("run_id")
            if (
                not isinstance(run_id, str)
                or not RUN_ID_RE.fullmatch(run_id)
                or run_id != path.parent.name
            ):
                continue
            state = refresh_status(raw)
            if state.get("run_id") != run_id:
                continue
            if active_only and state.get("status") not in ACTIVE_STATES:
                continue
            states.append(state)
        except (
            OSError,
            RuntimeError,
            TypeError,
            ValueError,
            KeyError,
            json.JSONDecodeError,
        ):
            continue
    return states[:limit]


def tail_logs(run_id: str | None, lines: int) -> str:
    state = refresh_status(load_state(run_id))
    candidates = [Path(state["build_log"])]
    active = state.get("active_step") or state.get("last_completed_step")
    if active:
        for step in reversed(discover_steps(Path(state["librelane_run_dir"]))):
            if step["id"] == active:
                logs = sorted(
                    (Path(value) for value in step.get("logs") or []),
                    key=lambda p: p.stat().st_mtime,
                )
                if logs:
                    candidates.insert(0, logs[-1])
                break
    for candidate in candidates:
        content = _tail_lines(candidate, lines)
        if content:
            return "\n".join(content)
    return ""


def stop_run(run_id: str | None) -> dict[str, Any]:
    state = refresh_status(load_state(run_id))
    if state.get("status") in TERMINAL_STATES:
        return state
    state["stop_requested"] = True
    commit_state(
        state,
        expected_revision=managed_state_revision(state),
        events=(
            {
                "type": "stop_requested",
                "dedupe_key": "stop_requested",
                "phase": state.get("coarse_phase") or "control",
                "status": state.get("status"),
                "message": "managed LibreLane stop requested",
                "data": {"requested_by": "lane-agent"},
            },
        ),
    )
    container = state.get("container_name")
    if container:
        subprocess.run(
            ["docker", "stop", "--time", "8", str(container)],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            check=False,
        )
    pid = state.get("pid")
    if pid_identity_alive(pid, state.get("pid_start_time")):
        try:
            os.killpg(int(pid), signal.SIGTERM)
        except ProcessLookupError:
            pass
    visualization_dir = state.get("visualization_dir")
    visualizer_container = (
        Path(str(visualization_dir)) / "current_container"
        if visualization_dir
        else None
    )
    if visualizer_container is not None and visualizer_container.is_file():
        name = visualizer_container.read_text(encoding="utf-8").strip()
        if re.fullmatch(r"pi-lane-viz-[A-Za-z0-9_.-]+-\d+", name):
            subprocess.run(
                ["docker", "stop", "--time", "8", name],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                check=False,
            )
    if _visualizer_pid_alive(
        state.get("visualizer_pid"),
        state.get("visualizer_pid_start_time"),
        str(state.get("run_id") or ""),
    ):
        try:
            os.killpg(int(state["visualizer_pid"]), signal.SIGTERM)
        except ProcessLookupError:
            pass
    return reconcile_status(state=state)


def format_status(state: dict[str, Any]) -> str:
    qor = state.get("qor") or {}
    physical_sanity = qor.get("physical_sanity") or {}
    mapped_equivalence = state.get("mapped_equivalence") or {}
    assurance = state.get("assurance") or {}
    lines = [
        f"run_id: {state['run_id']}",
        f"design: {state['design_name']}",
        f"pdk: {state['pdk']}",
        f"flow: {state['flow']}",
        f"status: {state['status']}",
        f"process_status: {state.get('process_status', 'unknown')}",
        f"flow_scope: {state.get('flow_scope', 'unknown')}",
        f"delivery_gate: {state.get('delivery_gate', 'not_applicable')}",
        f"coarse_phase: {state.get('coarse_phase') or '-'}",
        f"active_step: {state.get('active_step') or '-'}",
        f"last_completed_step: {state.get('last_completed_step') or '-'}",
        f"completed_steps: {state.get('completed_step_count', 0)}",
        f"elapsed_seconds: {state.get('elapsed_seconds', 0)}",
        f"qor_gate: {qor.get('gate', 'unknown')}",
        f"signoff_gate: {qor.get('signoff_gate', 'unknown')}",
        f"physical_sanity_gate: {physical_sanity.get('gate', 'unknown')}",
        f"mapped_equivalence_status: {mapped_equivalence.get('status', 'not_requested')}",
        f"mapped_equivalence_gate: {mapped_equivalence.get('gate', 'not_applicable')}",
        f"foundry_signoff: {assurance.get('foundry_signoff', False)}",
        f"tapeout_guarantee: {assurance.get('tapeout_guarantee', False)}",
        f"visualization_status: {(state.get('visualization') or {}).get('status', 'unavailable')}",
        f"visualizations: {(state.get('visualization') or {}).get('visualization_count', 0)}",
        f"latest_visualization: {(state.get('visualization') or {}).get('latest_image') or '-'}",
        f"metrics: {state.get('metrics_path') or '-'}",
        f"build_log: {state['build_log']}",
    ]
    if state.get("parent_run_id"):
        lines.append(f"parent_run_id: {state['parent_run_id']}")
    if state.get("status_detail"):
        lines.append(f"status_detail: {state['status_detail']}")
    for reason in state.get("delivery_gate_reasons") or []:
        lines.append(f"delivery_reason: {reason}")
    if state.get("errors"):
        lines.append("errors:")
        lines.extend(f"  - {line}" for line in state["errors"])
    return "\n".join(lines)
