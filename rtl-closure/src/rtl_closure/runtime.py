"""Immutable identities for trusted container runtimes."""

from __future__ import annotations

import json
import os
import re
import subprocess
from typing import Any

from .errors import NonRepairableInfrastructureError


VERIFY_IMAGE_VERSION = "0.4.0"
DEFAULT_VERIFY_IMAGE = f"rtl-closure/verify:{VERIFY_IMAGE_VERSION}"
VERIFY_IMAGE = os.environ.get("RTL_CLOSURE_VERIFY_IMAGE", DEFAULT_VERIFY_IMAGE)
VERIFY_ROOT = "/opt/rtl-closure-verify"
VERIFY_PYTHON = f"{VERIFY_ROOT}/bin/python"
SANDBOX_IMAGE = os.environ.get(
    "RTL_CLOSURE_SANDBOX_IMAGE",
    os.environ.get("PI_LANE_IMAGE", "ghcr.io/librelane/librelane:3.0.5"),
)
_IMAGE_ID_RE = re.compile(r"^sha256:[0-9a-f]{64}$")
_REPO_DIGEST_RE = re.compile(r"^.+@sha256:[0-9a-f]{64}$")
_bound_verify_image: str | None = None
_bound_sandbox_image: str | None = None


def _image_inspection_error(
    message: str, *, role: str, retryable: bool
) -> NonRepairableInfrastructureError:
    return NonRepairableInfrastructureError(
        message,
        gate="image_inspect",
        code=f"{role}_image_inspect",
        provider="docker",
        retryable=retryable,
    )


def _resolve_image(reference: str, *, role: str) -> dict[str, Any]:
    try:
        completed = subprocess.run(
            ["docker", "image", "inspect", reference],
            capture_output=True,
            text=True,
            errors="replace",
            timeout=30,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise _image_inspection_error(
            f"trusted {role} image could not be inspected: {reference}: {exc}",
            role=role,
            retryable=True,
        ) from exc
    if completed.returncode != 0:
        detail = (completed.stderr or completed.stdout).strip()
        raise _image_inspection_error(
            f"trusted {role} image is unavailable: {reference}"
            + (f": {detail}" if detail else ""),
            role=role,
            retryable=True,
        )
    if len(completed.stdout.encode("utf-8", errors="replace")) > 4 * 1024 * 1024:
        raise _image_inspection_error(
            f"trusted {role} image inspection output is too large",
            role=role,
            retryable=False,
        )
    try:
        rows = json.loads(completed.stdout)
    except json.JSONDecodeError as exc:
        raise _image_inspection_error(
            f"trusted {role} image inspection returned invalid JSON",
            role=role,
            retryable=False,
        ) from exc
    if not isinstance(rows, list) or len(rows) != 1 or not isinstance(rows[0], dict):
        raise _image_inspection_error(
            f"trusted {role} image inspection returned an invalid record",
            role=role,
            retryable=False,
        )
    image_id = rows[0].get("Id")
    repo_digests = rows[0].get("RepoDigests") or []
    if not isinstance(image_id, str) or not _IMAGE_ID_RE.fullmatch(image_id):
        raise _image_inspection_error(
            f"trusted {role} image has an invalid immutable image ID",
            role=role,
            retryable=False,
        )
    if not isinstance(repo_digests, list) or any(
        not isinstance(value, str) or not _REPO_DIGEST_RE.fullmatch(value)
        for value in repo_digests
    ):
        raise _image_inspection_error(
            f"trusted {role} image has invalid repository digests",
            role=role,
            retryable=False,
        )
    return {
        "schema_version": 1,
        "configured_reference": reference,
        "image_id": image_id,
        "immutable_reference": image_id,
        "repo_digests": sorted(set(repo_digests)),
    }


def resolve_verify_image(reference: str = VERIFY_IMAGE) -> dict[str, Any]:
    """Resolve the verification runtime to a validated immutable image ID."""
    return _resolve_image(reference, role="verification")


def resolve_sandbox_image(reference: str = SANDBOX_IMAGE) -> dict[str, Any]:
    """Resolve the general sandbox runtime to a validated immutable image ID."""
    return _resolve_image(reference, role="sandbox")


def _validate_image_identity(
    identity: Any, *, configured_reference: str, role: str
) -> str:
    if not isinstance(identity, dict):
        raise RuntimeError(f"trusted {role} image identity is invalid")
    immutable = identity.get("immutable_reference")
    repo_digests = identity.get("repo_digests")
    if (
        identity.get("schema_version") != 1
        or identity.get("configured_reference") != configured_reference
        or not isinstance(immutable, str)
        or not _IMAGE_ID_RE.fullmatch(immutable)
        or identity.get("image_id") != immutable
        or not isinstance(repo_digests, list)
        or any(
            not isinstance(value, str) or not _REPO_DIGEST_RE.fullmatch(value)
            for value in repo_digests
        )
    ):
        raise RuntimeError(f"trusted {role} image identity is invalid")
    return immutable


def validate_verify_image_identity(identity: Any) -> str:
    """Validate a persisted verification image identity without changing state."""
    return _validate_image_identity(
        identity, configured_reference=VERIFY_IMAGE, role="verification"
    )


def validate_sandbox_image_identity(identity: Any) -> str:
    """Validate a persisted sandbox image identity without changing state."""
    return _validate_image_identity(
        identity, configured_reference=SANDBOX_IMAGE, role="sandbox"
    )


def bind_verify_image(identity: dict[str, Any]) -> str:
    """Bind this process to one previously resolved verification image."""
    global _bound_verify_image
    immutable = validate_verify_image_identity(identity)
    _bound_verify_image = immutable
    return immutable


def bind_sandbox_image(identity: dict[str, Any]) -> str:
    """Bind this process to one previously resolved sandbox image."""
    global _bound_sandbox_image
    immutable = validate_sandbox_image_identity(identity)
    _bound_sandbox_image = immutable
    return immutable


def verify_image_ref() -> str:
    """Return the immutable bound ID, or the configured tag outside a run."""
    return _bound_verify_image or VERIFY_IMAGE


def sandbox_image_ref() -> str:
    """Return the immutable bound ID, or the configured tag outside a run."""
    return _bound_sandbox_image or SANDBOX_IMAGE
