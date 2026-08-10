from __future__ import annotations

import json
import subprocess
from pathlib import Path
from unittest.mock import patch

import pytest

from rtl_closure import runtime
from rtl_closure.errors import NonRepairableInfrastructureError


def test_verifier_python_path_matches_image_layout():
    assert runtime.VERIFY_PYTHON == "/opt/rtl-closure-verify/bin/python"
    dockerfile = (
        Path(__file__).parents[1] / "docker" / "verify.Dockerfile"
    ).read_text(encoding="utf-8")
    assert f"python3 -m venv {runtime.VERIFY_ROOT}" in dockerfile
    assert (
        f'org.opencontainers.image.version="{runtime.VERIFY_IMAGE_VERSION}"'
        in dockerfile
    )
    assert runtime.DEFAULT_VERIFY_IMAGE == "rtl-closure/verify:0.4.0"


def test_resolve_and_bind_verification_image_uses_immutable_id():
    image_id = "sha256:" + "a" * 64
    response = subprocess.CompletedProcess(
        args=[],
        returncode=0,
        stdout=json.dumps(
            [
                {
                    "Id": image_id,
                    "RepoDigests": ["example.invalid/verify@sha256:" + "b" * 64],
                }
            ]
        ),
        stderr="",
    )
    with patch("rtl_closure.runtime.subprocess.run", return_value=response) as runner, patch(
        "rtl_closure.runtime._bound_verify_image", None
    ):
        identity = runtime.resolve_verify_image()
        assert runtime.bind_verify_image(identity) == image_id
        assert runtime.verify_image_ref() == image_id
    assert runner.call_args.args[0] == [
        "docker",
        "image",
        "inspect",
        runtime.VERIFY_IMAGE,
    ]


def test_resolve_verification_image_rejects_mutable_or_malformed_identity():
    response = subprocess.CompletedProcess(
        args=[],
        returncode=0,
        stdout=json.dumps([{"Id": "not-an-image-id", "RepoDigests": []}]),
        stderr="",
    )
    with patch("rtl_closure.runtime.subprocess.run", return_value=response):
        with pytest.raises(NonRepairableInfrastructureError, match="invalid immutable image ID") as raised:
            runtime.resolve_verify_image()
    assert raised.value.retryable is False
    assert raised.value.repairable_by_rtl is False


def test_image_inspect_timeout_is_typed_retryable_infrastructure_failure():
    timeout = subprocess.TimeoutExpired(["docker", "image", "inspect"], 30)
    with patch("rtl_closure.runtime.subprocess.run", side_effect=timeout):
        with pytest.raises(NonRepairableInfrastructureError) as raised:
            runtime.resolve_verify_image()
    assert raised.value.failure_gate == "image_inspect"
    assert raised.value.failure_code == "infrastructure/verification_image_inspect"
    assert raised.value.provider == "docker"
    assert raised.value.retryable is True
    assert raised.value.repairable_by_rtl is False


def test_resolve_and_bind_sandbox_image_uses_immutable_id():
    image_id = "sha256:" + "c" * 64
    response = subprocess.CompletedProcess(
        args=[],
        returncode=0,
        stdout=json.dumps(
            [
                {
                    "Id": image_id,
                    "RepoDigests": ["example.invalid/sandbox@sha256:" + "d" * 64],
                }
            ]
        ),
        stderr="",
    )
    with patch("rtl_closure.runtime.subprocess.run", return_value=response) as runner, patch(
        "rtl_closure.runtime._bound_sandbox_image", None
    ):
        identity = runtime.resolve_sandbox_image()
        assert runtime.bind_sandbox_image(identity) == image_id
        assert runtime.sandbox_image_ref() == image_id
    assert runner.call_args.args[0] == [
        "docker",
        "image",
        "inspect",
        runtime.SANDBOX_IMAGE,
    ]


def test_sandbox_identity_cannot_be_rebound_to_another_configured_tag():
    identity = {
        "schema_version": 1,
        "configured_reference": "different.invalid/sandbox:latest",
        "image_id": "sha256:" + "e" * 64,
        "immutable_reference": "sha256:" + "e" * 64,
        "repo_digests": [],
    }
    with pytest.raises(RuntimeError, match="sandbox image identity is invalid"):
        runtime.validate_sandbox_image_identity(identity)
