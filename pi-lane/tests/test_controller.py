from __future__ import annotations

import hashlib
import json
import os
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from pi_lane.config import capture_design_inputs, load_design_config, validate_override
from pi_lane.controller import (
    PDK_REVISIONS,
    _physical_sanity,
    _image_lock_digest,
    _record_image_lock,
    _eqy_runtime_inputs,
    _expected_eqy_gate_tcl,
    _expected_eqy_script,
    _terminal_auxiliary_projection,
    _terminal_durable_projection,
    _idempotent_run,
    _install_errors,
    _materialize_design_snapshot,
    _materialize_eqy_tool,
    _spawn_visualizer,
    _verify_design_snapshot,
    build_docker_command,
    build_pdk_install_command,
    build_visualizer_command,
    discover_artifacts,
    discover_steps,
    discover_visualization,
    installed_pdk_revision,
    load_image_lock,
    list_runs,
    pdk_installed,
    pdk_integrity_report,
    pdk_ready,
    pdk_proxy_environment,
    pdk_proxy_uses_loopback,
    refresh_image_install,
    image_install_status,
    refresh_pdk_install,
    refresh_status,
    reconcile_status,
    require_locked_image,
    run_event_report,
    stop_image_install,
    stop_pdk_install,
    start_visualization,
    start_image_install,
    start_pdk_install,
    start_run,
    summarize_qor,
    validate_image_name,
    validate_pdk_root,
)
from pi_lane.classic import (
    CLASSIC_REGISTRY_VERSION,
    LIBRELANE_IMAGE,
    LIBRELANE_VERSION,
)
from pi_lane.run_state import (
    commit_state,
    elapsed_seconds,
    read_event_journal,
    resolve_run_id,
)
from pi_lane.integrity import file_sha256
from pi_lane.visualizer import build_render_command, preview_kind


def complete_metrics(
    *,
    core_area: float,
    total_area: float,
    standard_cell_area: float,
    filler_area: float,
    tap_area: float,
    utilization: float,
    setup_violations: int = 0,
    setup_wns: float = 0.0,
    setup_tns: float = 0.0,
    max_slew_violations: int = 0,
    max_cap_violations: int = 0,
) -> dict[str, float | int]:
    return {
        "timing__setup_vio__count": setup_violations,
        "timing__setup__ws": setup_wns,
        "timing__setup__tns": setup_tns,
        "timing__hold_vio__count": 0,
        "timing__hold__ws": 0.0,
        "timing__hold__tns": 0.0,
        "design__max_slew_violation__count": max_slew_violations,
        "design__max_cap_violation__count": max_cap_violations,
        "route__drc_errors": 0,
        "magic__drc_error__count": 0,
        "klayout__drc_error__count": 0,
        "design__lvs_error__count": 0,
        "design__xor_difference__count": 0,
        "design__instance__area": total_area,
        "design__instance__area__stdcell": standard_cell_area,
        "design__core__area": core_area,
        "design__instance__area__class:fill_cell": filler_area,
        "design__instance__area__class:tap_cell": tap_area,
        "design__instance__utilization__stdcell": utilization,
    }


def write_completed_step(run_dir: Path, ordinal: int, step_id: str) -> None:
    step = run_dir / f"{ordinal:02d}-{step_id.lower().replace('.', '-')}"
    step.mkdir(parents=True, exist_ok=True)
    (step / "config.json").write_text(
        json.dumps({"meta": {"step": step_id}}), encoding="utf-8"
    )
    (step / "state_out.json").write_text("{}\n", encoding="utf-8")


def write_valid_gds(path: Path) -> None:
    records = [
        b"\x00\x06\x00\x02\x00\x03",
        b"\x00\x1c\x01\x02" + b"\x00" * 24,
        b"\x00\x08\x02\x06LIB\x00",
        b"\x00\x14\x03\x05" + b"\x00" * 16,
        b"\x00\x1c\x05\x02" + b"\x00" * 24,
        b"\x00\x08\x06\x06TOP\x00",
        b"\x00\x04\x07\x00",
        b"\x00\x04\x04\x00",
    ]
    path.write_bytes(b"".join(records))


def locked_image_identity(digest: str | None = None) -> dict[str, object]:
    digest = digest or "sha256:" + "d" * 64
    record: dict[str, object] = {
        "schema_version": 1,
        "revision": 1,
        "image": LIBRELANE_IMAGE,
        "image_digest": digest,
        "librelane_version": LIBRELANE_VERSION,
        "install_run_id": "image-install-1",
        "locked_at": "2026-08-05T00:00:00+08:00",
        "previous_lock_sha256": None,
    }
    record["lock_sha256"] = _image_lock_digest(record)
    return {**record, "lock_path": "/state/image-installs/image-lock.json"}


def completed_run_state(root: Path, *, to: str | None = None) -> dict[str, object]:
    config_path = root / "config.yaml"
    config_path.write_text("DESIGN_NAME: delivery_test\nPDK: sky130A\n")
    run_dir = root / "runs" / "PI_LANE_delivery-test"
    run_dir.mkdir(parents=True, exist_ok=True)
    exit_file = root / "exit_code"
    exit_file.write_text("0\n", encoding="utf-8")
    build_log = root / "build.log"
    build_log.write_text("", encoding="utf-8")
    config = load_design_config(config_path)
    input_manifest, captured = capture_design_inputs(config)
    input_snapshot_dir = root / "input-snapshot"
    _materialize_design_snapshot(
        input_snapshot_dir, manifest=input_manifest, captured=captured
    )
    input_manifest_path = root / "input-manifest.json"
    input_manifest_path.write_text(
        json.dumps(input_manifest, sort_keys=True), encoding="utf-8"
    )
    image_digest = "sha256:" + "d" * 64
    pdk_root = root / "pdk"
    pdk_root.mkdir(exist_ok=True)
    runtime_binding = {
        "image": LIBRELANE_IMAGE,
        "image_digest": image_digest,
        "runtime_image_ref": image_digest,
    }
    image_lock = locked_image_identity(image_digest)
    return {
        "schema_version": 6,
        "state_revision": 0,
        "run_id": "delivery-test",
        "workflow_id": "workflow-delivery-test",
        "attempt_id": "attempt-delivery-test",
        "origin": {"component": "test"},
        "controller_dir": str(root),
        "controller_events_file": str(root / "controller-events.jsonl"),
        "state_file": str(root / "state.json"),
        "build_log": str(build_log),
        "exit_file": str(exit_file),
        "pid": None,
        "status": "completed",
        "started_epoch": exit_file.stat().st_mtime - 5,
        "librelane_run_dir": str(run_dir),
        "design_dir": str(root),
        "config_path": str(config_path),
        "config_sha256": file_sha256(config_path),
        "input_snapshot_dir": str(input_snapshot_dir),
        "input_manifest_path": str(input_manifest_path),
        "input_manifest": input_manifest,
        "input_bundle_sha256": input_manifest["bundle_sha256"],
        "design_name": "delivery_test",
        "pdk": "sky130A",
        "pdk_revision": PDK_REVISIONS["sky130A"][1],
        "pdk_integrity_sha256": "c" * 64,
        "pdk_root": str(pdk_root),
        "flow": "Classic",
        "flow_registry_version": CLASSIC_REGISTRY_VERSION,
        "image": LIBRELANE_IMAGE,
        "image_digest": image_digest,
        "runtime_image_ref": image_digest,
        "image_lock": {
            key: value for key, value in image_lock.items() if key != "lock_path"
        },
        "image_lock_path": image_lock["lock_path"],
        "image_lock_sha256": image_lock["lock_sha256"],
        "librelane_version": LIBRELANE_VERSION,
        "container_identity": runtime_binding,
        "config_preflight": runtime_binding,
        "runtime_environment_attestation": {
            **runtime_binding,
            "image_lock_sha256": image_lock["lock_sha256"],
            "image_lock_revision": image_lock["revision"],
            "expected_librelane_version": LIBRELANE_VERSION,
            "container_runtime_binding": runtime_binding,
            "preflight_runtime_binding": runtime_binding,
            "pdk": "sky130A",
            "pdk_root": str(pdk_root),
            "pdk_revision": PDK_REVISIONS["sky130A"][1],
            "pdk_integrity_sha256": "c" * 64,
            "verified": True,
            "errors": [],
        },
        "overrides": [],
        "from": None,
        "to": to,
        "stop_requested": False,
    }


def recapture_run_inputs(state: dict[str, object]) -> None:
    config = load_design_config(Path(str(state["config_path"])))
    manifest, captured = capture_design_inputs(config)
    controller_dir = Path(str(state["controller_dir"]))
    snapshot = controller_dir / f"input-snapshot-{manifest['bundle_sha256'][:12]}"
    _materialize_design_snapshot(snapshot, manifest=manifest, captured=captured)
    manifest_path = (
        controller_dir / f"input-manifest-{manifest['bundle_sha256'][:12]}.json"
    )
    manifest_path.write_text(json.dumps(manifest, sort_keys=True), encoding="utf-8")
    state.update(
        {
            "config_sha256": config.sha256,
            "input_snapshot_dir": str(snapshot),
            "input_manifest_path": str(manifest_path),
            "input_manifest": manifest,
            "input_bundle_sha256": manifest["bundle_sha256"],
        }
    )


def seal_completed_delivery(
    root: Path,
) -> tuple[dict[str, object], Path, Path]:
    state = completed_run_state(root)
    state["status"] = "running"
    run_dir = Path(str(state["librelane_run_dir"]))
    write_completed_step(run_dir, 80, "Misc.ReportManufacturability")
    final = run_dir / "final"
    gds = final / "gds" / "delivery_test.gds"
    gds.parent.mkdir(parents=True)
    write_valid_gds(gds)
    metrics = final / "metrics.json"
    metrics.write_text(
        json.dumps(
            complete_metrics(
                core_area=1000.0,
                total_area=1000.0,
                standard_cell_area=400.0,
                filler_area=600.0,
                tap_area=20.0,
                utilization=0.4,
            )
        ),
        encoding="utf-8",
    )
    state = commit_state(
        state,
        expected_revision=0,
        events=({"type": "run_started"},),
    )
    with patch(
        "pi_lane.controller._runtime_environment_attestation",
        return_value=(dict(state["runtime_environment_attestation"]), []),
    ):
        sealed = reconcile_status(
            state=state, integrity_audit_id="test-initial-delivery-audit"
        )
    return sealed, gds, metrics


class PiLaneTests(unittest.TestCase):
    def test_list_runs_active_filter_uses_fresh_status_without_touching_current(self):
        with tempfile.TemporaryDirectory() as temp:
            state_root = Path(temp)
            statuses = {
                "active-running": "running",
                "active-queued": "queued",
                "invalid-unknown": "unknown",
                "done-completed": "completed",
                "done-failed": "failed",
                "done-incomplete": "incomplete",
                "done-stopped": "stopped",
            }
            for run_id in statuses:
                run_root = state_root / run_id
                run_root.mkdir()
                (run_root / "state.json").write_text(
                    json.dumps({"run_id": run_id, "status": "running"}),
                    encoding="utf-8",
                )
            mismatched = state_root / "mismatched"
            mismatched.mkdir()
            (mismatched / "state.json").write_text(
                json.dumps({"run_id": "different", "status": "running"}),
                encoding="utf-8",
            )
            current = state_root / "current"
            current.write_text("done-completed\n", encoding="ascii")
            current_before = current.read_bytes()
            current_mtime_before = current.stat().st_mtime_ns

            def fresh_status(state: dict[str, object]) -> dict[str, object]:
                return {**state, "status": statuses[str(state["run_id"])]}

            with (
                patch("pi_lane.controller.STATE_ROOT", state_root),
                patch("pi_lane.controller.refresh_status", side_effect=fresh_status),
            ):
                all_runs = list_runs()
                active_runs = list_runs(active_only=True)
                limited_runs = list_runs(active_only=True, limit=1)

            self.assertEqual({row["run_id"] for row in all_runs}, set(statuses))
            self.assertEqual(
                {row["run_id"] for row in active_runs},
                {"active-running", "active-queued"},
            )
            self.assertEqual(len(limited_runs), 1)
            with self.assertRaisesRegex(ValueError, "limit must be"):
                list_runs(active_only=True, limit=0)
            self.assertEqual(current.read_bytes(), current_before)
            self.assertEqual(current.stat().st_mtime_ns, current_mtime_before)

    def test_install_status_projections_are_pure_reads(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            exit_file = root / "exit_code"
            exit_file.write_text("0\n", encoding="ascii")
            log = root / "install.log"
            log.write_text("done\n", encoding="utf-8")
            state_file = root / "state.json"
            common = {
                "run_id": "install-run",
                "state_file": str(state_file),
                "build_log": str(log),
                "exit_file": str(exit_file),
                "pid": 999999,
                "status": "running",
                "started_epoch": 1.0,
                "stop_requested": False,
                "state_revision": 3,
            }

            image_state = {
                **common,
                "kind": "image_install",
                "image": LIBRELANE_IMAGE,
            }
            state_file.write_text(json.dumps(image_state), encoding="utf-8")
            before = state_file.read_bytes()
            with (
                patch("pi_lane.controller.pid_identity_alive", return_value=False),
                patch("pi_lane.controller.image_id", return_value="sha256:image"),
                patch(
                    "pi_lane.controller.image_librelane_version",
                    return_value=LIBRELANE_VERSION,
                ),
            ):
                image = refresh_image_install(image_state)
            self.assertEqual(state_file.read_bytes(), before)
            self.assertEqual(image["status"], "completed")

            pdk_revision = PDK_REVISIONS["sky130A"][1]
            pdk_state = {
                **common,
                "kind": "pdk_install",
                "pdk": "sky130A",
                "pdk_root": str(root / "pdk"),
                "revision": pdk_revision,
            }
            state_file.write_text(json.dumps(pdk_state), encoding="utf-8")
            before = state_file.read_bytes()
            with (
                patch("pi_lane.controller.pid_identity_alive", return_value=False),
                patch(
                    "pi_lane.controller.pdk_integrity_report",
                    return_value={"ready": True},
                ),
            ):
                pdk = refresh_pdk_install(pdk_state)
            self.assertEqual(state_file.read_bytes(), before)
            self.assertEqual(pdk["status"], "completed")
            self.assertEqual(pdk["revision"], pdk_revision)

    def test_start_idempotency_key_returns_only_the_matching_request(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            state_root = root / "state-root"
            run_root = state_root / "existing"
            run_root.mkdir(parents=True)
            state = completed_run_state(run_root)
            state.update(
                {
                    "idempotency_key": "request-1",
                    "request_fingerprint": "a" * 64,
                }
            )
            (run_root / "state.json").write_text(json.dumps(state), encoding="utf-8")
            with patch("pi_lane.controller.STATE_ROOT", state_root):
                same = _idempotent_run("request-1", "a" * 64)
                with self.assertRaisesRegex(RuntimeError, "different Lane request"):
                    _idempotent_run("request-1", "b" * 64)
        self.assertEqual(same["run_id"], "delivery-test")

    def test_event_report_v3_uses_controller_journal(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            state = completed_run_state(root)
            state.update(
                {
                    "schema_version": 6,
                    "state_revision": 0,
                    "workflow_id": "workflow-1",
                    "attempt_id": "attempt-1",
                    "origin": {"component": "test"},
                    "controller_dir": str(root),
                    "controller_events_file": str(root / "controller-events.jsonl"),
                }
            )
            commit_state(
                state,
                expected_revision=0,
                events=({"type": "run_started", "dedupe_key": "run_started"},),
            )
            with patch("pi_lane.controller.load_state", return_value=state):
                report = run_event_report("delivery-test")
        self.assertEqual(report["schema_version"], 3)
        self.assertEqual(report["contract"], "pi-lane.events.v3")
        self.assertTrue(report["authoritative"])
        self.assertTrue(report["visualization_independent"])
        self.assertEqual(report["events"][0]["type"], "run_started")
        self.assertEqual(report["snapshot"]["state_revision"], 1)
        self.assertNotIn("revision", report["snapshot"])

    def test_status_projection_is_a_pure_read(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            state = completed_run_state(root)
            state_path = Path(str(state["state_file"]))
            state_path.write_text(json.dumps(state, sort_keys=True), encoding="utf-8")
            before = state_path.read_bytes()
            projected = refresh_status(state)
            after = state_path.read_bytes()
        self.assertEqual(before, after)
        self.assertEqual(projected["process_status"], "completed")

    def test_recent_launch_registration_is_not_reconciled_as_a_dead_worker(self):
        state = {
            "schema_version": 6,
            "state_revision": 0,
            "run_id": "launching-run",
            "status": "launching",
            "pid": None,
            "exit_file": "/does/not/exist",
            "launch_requested_epoch": 100.0,
        }
        with patch("pi_lane.controller.time.time", return_value=105.0):
            projected = refresh_status(state)
        self.assertEqual(projected, state)

    def test_reconcile_writes_authoritative_events_and_one_terminal(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            reconciled, gds, metrics = seal_completed_delivery(root)
            sealed_evidence = {
                key: reconciled.get(key)
                for key in (
                    "process_status",
                    "qor",
                    "canonical_gds",
                    "runtime_environment_attestation",
                    "delivery_attestation",
                )
            }
            gds.unlink()
            metrics.write_text(json.dumps({"route__drc_errors": 99}), encoding="utf-8")
            again = reconcile_status(state=reconciled)
            replay = read_event_journal(again, limit=100)
        terminal = [event for event in replay["events"] if event["terminal"]]
        step_events = [
            event for event in replay["events"] if event["type"] == "step_completed"
        ]
        self.assertEqual(reconciled["status"], "completed")
        self.assertEqual(len(terminal), 1)
        self.assertEqual(terminal[0]["type"], "run_completed")
        self.assertEqual(again["terminal_event_id"], terminal[0]["event_id"])
        self.assertEqual(
            {key: again.get(key) for key in sealed_evidence}, sealed_evidence
        )
        self.assertEqual(again["status"], "incomplete")
        self.assertEqual(again["delivery_gate"], "incomplete")
        self.assertEqual(again["failure_kind"], "release_integrity")
        self.assertEqual(again["release_integrity"]["status"], "degraded")
        self.assertTrue(
            any(
                event["type"] == "delivery_integrity_degraded"
                for event in replay["events"]
            )
        )
        self.assertNotIn("event", step_events[0])
        self.assertEqual(step_events[0]["step"], "Misc.ReportManufacturability")
        self.assertEqual(step_events[0]["ordinal"], 80)


    def test_completed_delivery_degrades_after_same_size_valid_gds_replacement(self):
        with tempfile.TemporaryDirectory() as temp:
            sealed, gds, _ = seal_completed_delivery(Path(temp))
            original = gds.read_bytes()
            replacement = original.replace(b"TOP", b"ALT")
            self.assertNotEqual(replacement, original)
            self.assertEqual(len(replacement), len(original))
            gds.write_bytes(replacement)

            audited = reconcile_status(
                state=sealed, integrity_audit_id="test-gds-replacement-audit"
            )

        self.assertEqual(audited["status"], "incomplete")
        self.assertEqual(audited["delivery_gate"], "incomplete")
        self.assertEqual(audited["failure_kind"], "release_integrity")
        self.assertEqual(audited["release_integrity"]["status"], "degraded")
        self.assertEqual(
            audited["release_integrity"]["audit_id"],
            "test-gds-replacement-audit",
        )
        self.assertIn(
            "delivery artifact content",
            "\n".join(audited["release_integrity"]["errors"]),
        )

    def test_completed_delivery_degrades_after_final_metrics_change(self):
        with tempfile.TemporaryDirectory() as temp:
            sealed, _, metrics = seal_completed_delivery(Path(temp))
            metrics.write_text(
                json.dumps(
                    complete_metrics(
                        core_area=1000.0,
                        total_area=1000.0,
                        standard_cell_area=400.0,
                        filler_area=600.0,
                        tap_area=21.0,
                        utilization=0.4,
                    )
                ),
                encoding="utf-8",
            )

            audited = reconcile_status(
                state=sealed, integrity_audit_id="test-metrics-change-audit"
            )

        self.assertEqual(audited["status"], "incomplete")
        self.assertEqual(audited["failure_kind"], "release_integrity")
        self.assertEqual(audited["release_integrity"]["status"], "degraded")
        self.assertIn(
            "bound QoR evidence changed",
            "\n".join(audited["release_integrity"]["errors"]),
        )

    def test_public_terminal_delivery_is_restored_from_sealed_evidence(self):
        with tempfile.TemporaryDirectory() as temp:
            sealed, _, _ = seal_completed_delivery(Path(temp))
            expected = {
                key: sealed[key]
                for key in (
                    "metrics_path",
                    "canonical_gds",
                    "artifacts",
                    "assurance",
                )
            }
            state_path = Path(str(sealed["state_file"]))
            tampered = json.loads(state_path.read_text(encoding="utf-8"))
            tampered.update(
                {
                    "metrics_path": "/tmp/forged-metrics.json",
                    "canonical_gds": ["/tmp/forged.gds"],
                    "artifacts": {
                        "metrics": "/tmp/forged-metrics.json",
                        "canonical_gds": ["/tmp/forged.gds"],
                    },
                    "assurance": {
                        "open_pdk": False,
                        "foundry_signoff": True,
                        "tapeout_guarantee": True,
                    },
                }
            )
            state_path.write_text(json.dumps(tampered), encoding="utf-8")

            audited = reconcile_status(
                state=tampered, integrity_audit_id="test-public-state-tamper"
            )
            persisted = json.loads(state_path.read_text(encoding="utf-8"))
            public_read = refresh_status(persisted)

        self.assertEqual(audited["status"], "incomplete")
        self.assertEqual(audited["release_integrity"]["status"], "degraded")
        self.assertIn(
            "persisted public delivery projection changed",
            "\n".join(audited["release_integrity"]["errors"]),
        )
        for key, value in expected.items():
            self.assertEqual(audited[key], value)
            self.assertEqual(public_read[key], value)

    def test_repeated_degraded_audit_issues_a_fresh_receipt(self):
        with tempfile.TemporaryDirectory() as temp:
            sealed, gds, _ = seal_completed_delivery(Path(temp))
            gds.write_bytes(gds.read_bytes().replace(b"TOP", b"ALT"))
            first = reconcile_status(
                state=sealed, integrity_audit_id="test-degraded-audit-first"
            )
            with patch(
                "pi_lane.controller.now_iso",
                return_value="2026-08-03T12:34:56+08:00",
            ):
                second = reconcile_status(
                    state=first,
                    integrity_audit_id="test-degraded-audit-second",
                )

        self.assertEqual(first["release_integrity"]["status"], "degraded")
        self.assertEqual(second["status"], "incomplete")
        self.assertEqual(second["release_integrity"]["status"], "degraded")
        self.assertEqual(
            second["release_integrity"]["audit_id"],
            "test-degraded-audit-second",
        )
        self.assertEqual(
            second["release_integrity"]["checked_at"],
            "2026-08-03T12:34:56+08:00",
        )

    def test_malformed_attestation_degrades_with_a_null_bound_hash(self):
        with tempfile.TemporaryDirectory() as temp:
            sealed, _, _ = seal_completed_delivery(Path(temp))
            sealed["delivery_attestation"]["attestation_sha256"] = "f" * 64
            Path(str(sealed["state_file"])).write_text(
                json.dumps(sealed), encoding="utf-8"
            )
            first = reconcile_status(
                state=sealed,
                integrity_audit_id="test-malformed-attestation-first",
            )
            second = reconcile_status(
                state=first,
                integrity_audit_id="test-malformed-attestation-second",
            )

        self.assertEqual(first["status"], "incomplete")
        self.assertEqual(first["release_integrity"]["status"], "degraded")
        self.assertIsNone(first["release_integrity"]["attestation_sha256"])
        self.assertEqual(
            second["release_integrity"]["audit_id"],
            "test-malformed-attestation-second",
        )
        self.assertIsNone(second["release_integrity"]["attestation_sha256"])

    def test_transient_metrics_read_error_is_unknown_without_degrading_completion(
        self,
    ):
        with tempfile.TemporaryDirectory() as temp:
            sealed, _, metrics = seal_completed_delivery(Path(temp))
            original_read_text = Path.read_text

            def flaky_read_text(path: Path, *args, **kwargs):
                if path == metrics:
                    raise OSError("temporary metrics I/O failure")
                return original_read_text(path, *args, **kwargs)

            with patch.object(Path, "read_text", flaky_read_text):
                audited = reconcile_status(
                    state=sealed,
                    integrity_audit_id="test-transient-metrics-io-audit",
                )

        self.assertEqual(audited["status"], "completed")
        self.assertEqual(audited["delivery_gate"], "pass")
        self.assertEqual(audited["release_integrity"]["status"], "unknown")
        self.assertEqual(
            audited["release_integrity"]["audit_id"],
            "test-transient-metrics-io-audit",
        )
        self.assertIn(
            "temporary metrics I/O failure",
            "\n".join(audited["release_integrity"]["errors"]),
        )

    def test_transient_delivery_hash_error_is_unknown_without_degrading_completion(
        self,
    ):
        with tempfile.TemporaryDirectory() as temp:
            sealed, _, _ = seal_completed_delivery(Path(temp))
            with patch(
                "pi_lane.controller.file_sha256",
                side_effect=OSError("temporary artifact read failure"),
            ):
                audited = reconcile_status(
                    state=sealed, integrity_audit_id="test-transient-io-audit"
                )

        self.assertEqual(audited["status"], "completed")
        self.assertEqual(audited["delivery_gate"], "pass")
        self.assertEqual(audited["release_integrity"]["status"], "unknown")
        self.assertEqual(
            audited["release_integrity"]["audit_id"], "test-transient-io-audit"
        )
        self.assertIn(
            "temporary artifact read failure",
            "\n".join(audited["release_integrity"]["errors"]),
        )

    def test_process_failure_has_structured_non_rtl_failure(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            state = completed_run_state(root)
            Path(str(state["exit_file"])).write_text("17\n", encoding="utf-8")
            projected = refresh_status(state)
        self.assertEqual(projected["status"], "failed")
        self.assertEqual(projected["failure"]["code"], "LANE_PROCESS_FAILED")
        self.assertEqual(projected["failure"]["domain"], "librelane")
        self.assertFalse(projected["failure"]["repairable_by_rtl"])
        self.assertIn("caused_by_event_id", projected["failure"])

    def test_only_gds_files_can_satisfy_the_canonical_delivery_gate(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            state = completed_run_state(root)
            run_dir = Path(str(state["librelane_run_dir"]))
            write_completed_step(run_dir, 80, "Misc.ReportManufacturability")
            final = run_dir / "final"
            (final / "gds").mkdir(parents=True)
            (final / "gds" / "not-a-gds.txt").write_bytes(b"not gds")
            (final / "metrics.json").write_text(
                json.dumps(
                    complete_metrics(
                        core_area=1000.0,
                        total_area=1000.0,
                        standard_cell_area=400.0,
                        filler_area=600.0,
                        tap_area=20.0,
                        utilization=0.4,
                    )
                ),
                encoding="utf-8",
            )
            with patch("pi_lane.controller.save_state"):
                refreshed = refresh_status(state)
        self.assertEqual(refreshed["canonical_gds"], [])
        self.assertEqual(refreshed["delivery_gate"], "incomplete")

    def test_elapsed_time_freezes_at_finished_epoch(self):
        self.assertEqual(
            elapsed_seconds({"started_epoch": 100.0, "finished_epoch": 142.0}), 42
        )

    def test_config_requires_open_pdk(self):
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "config.yaml"
            path.write_text("DESIGN_NAME: demo\nPDK: commercial\n")
            with self.assertRaises(ValueError):
                load_design_config(path)

    def test_tcl_is_rejected(self):
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "config.tcl"
            path.write_text("set ::env(DESIGN_NAME) demo\n")
            with self.assertRaises(ValueError):
                load_design_config(path, pdk_override="sky130A")

    def test_override_is_json(self):
        self.assertEqual(validate_override("FP_CORE_UTIL=40"), "FP_CORE_UTIL=40")
        self.assertEqual(validate_override('MODE="fast"'), 'MODE="fast"')
        with self.assertRaises(ValueError):
            validate_override("MODE=fast")
        for protected in (
            'DESIGN_NAME="other"',
            'PDK="gf180mcuD"',
            'VERILOG_FILES=["/tmp/evil.v"]',
            'SDC_FILE="/tmp/evil.sdc"',
            "RUN_EQY=false",
            "ERROR_ON_LINTER_ERRORS=false",
            'FLOW="Other"',
        ):
            with self.subTest(protected=protected), self.assertRaises(ValueError):
                validate_override(protected)

    def test_docker_command_has_no_network_and_no_home_mount(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            config_path = root / "config.yaml"
            config_path.write_text("DESIGN_NAME: demo\nPDK: sky130A\n")
            config = load_design_config(config_path)
            command, _ = build_docker_command(
                config,
                run_id="run",
                tag="PI_LANE_run",
                pdk_root=root / "pdk",
                overrides=[],
                frm=None,
                to=None,
                jobs=2,
                runtime_image_ref="sha256:" + "d" * 64,
                input_snapshot_dir=root / "snapshot",
            )
        self.assertIn("none", command)
        self.assertNotIn(str(Path.home()), command)
        self.assertIn("--read-only", command)
        self.assertIn("sha256:" + "d" * 64, command)
        self.assertNotIn(LIBRELANE_IMAGE, command)
        self.assertIn(f"io.pi-lane.image-tag={LIBRELANE_IMAGE}", command)
        self.assertIn(
            "io.pi-lane.image-digest=sha256:" + "d" * 64,
            command,
        )
        self.assertIn(f"{root / 'snapshot'}:{root}:ro", command)
        self.assertIn(
            f"{root / 'runs' / 'PI_LANE_run'}:{root / 'runs' / 'PI_LANE_run'}:rw",
            command,
        )
        self.assertNotIn(f"{root / 'runs'}:{root / 'runs'}:rw", command)



    def test_design_inputs_are_captured_once_and_attested(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            rtl = root / "rtl" / "demo.v"
            rtl.parent.mkdir()
            rtl.write_text("module demo; endmodule\n")
            sdc = root / "demo.sdc"
            sdc.write_text("create_clock -period 10 clk\n")
            config_path = root / "config.yaml"
            config_path.write_text(
                "DESIGN_NAME: demo\n"
                "PDK: sky130A\n"
                "VERILOG_FILES: [dir::rtl/demo.v]\n"
                "SDC_FILE: dir::demo.sdc\n"
            )
            config = load_design_config(config_path)
            original_config = config_path.read_bytes()
            config_path.write_text("DESIGN_NAME: replaced\nPDK: sky130A\n")
            manifest, captured = capture_design_inputs(config)
            self.assertEqual(captured["config.yaml"], original_config)
            controller = root / "controller"
            controller.mkdir()
            snapshot = controller / "input-snapshot"
            _materialize_design_snapshot(snapshot, manifest=manifest, captured=captured)
            manifest_path = controller / "input-manifest.json"
            manifest_path.write_text(json.dumps(manifest))
            state = {
                "controller_dir": str(controller),
                "input_snapshot_dir": str(snapshot),
                "input_manifest_path": str(manifest_path),
                "input_bundle_sha256": manifest["bundle_sha256"],
            }
            rtl.write_text("module demo; wire changed; endmodule\n")
            self.assertEqual(_verify_design_snapshot(state), [])
            snap_rtl = snapshot / "rtl" / "demo.v"
            snap_rtl.chmod(0o600)
            snap_rtl.write_text("tampered\n")
            self.assertIn("changed", "\n".join(_verify_design_snapshot(state)))

    def test_design_input_capture_rejects_files_outside_design_directory(self):
        with (
            tempfile.TemporaryDirectory() as temp,
            tempfile.TemporaryDirectory() as other,
        ):
            root = Path(temp)
            external = Path(other) / "external.v"
            external.write_text("module external; endmodule\n")
            config_path = root / "config.yaml"
            config_path.write_text(
                f"DESIGN_NAME: demo\nPDK: sky130A\nVERILOG_FILES: [{external}]\n"
            )
            with self.assertRaisesRegex(ValueError, "inside the design directory"):
                capture_design_inputs(load_design_config(config_path))

    def test_required_eqy_inputs_are_hashed_and_mounted(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            script = root / "eqy" / "demo.eqy"
            script.parent.mkdir()
            script.write_text(_expected_eqy_script("demo"), encoding="utf-8")
            script.with_name("demo-gate.tcl").write_text(
                _expected_eqy_gate_tcl(), encoding="utf-8"
            )
            digest = file_sha256(script)
            config_path = root / "config.yaml"
            config_path.write_text(
                "meta:\n"
                "  flow: Classic\n"
                "DESIGN_NAME: demo\n"
                "PDK: sky130A\n"
                "RUN_EQY: true\n"
                "EQY_SCRIPT: dir::eqy/demo.eqy\n",
                encoding="utf-8",
            )
            config = load_design_config(config_path)
            manifest, captured = capture_design_inputs(config)
            self.assertIn("eqy/demo-gate.tcl", captured)
            self.assertIn(
                "eqy/demo-gate.tcl",
                {row["path"] for row in manifest["files"]},
            )
            packaged_tool = root / "packaged-eqy-tool.py"
            packaged_tool.write_bytes(
                Path(str(_eqy_runtime_inputs(config)["tool_path"])).read_bytes()
            )
            packaged_tool.chmod(0o644)
            with patch("pi_lane.controller.EQY_TOOL_PATH", packaged_tool):
                runtime = _eqy_runtime_inputs(config)
            self.assertIsNotNone(runtime)
            self.assertFalse(os.access(packaged_tool, os.X_OK))
            controller_dir = root / "controller"
            controller_dir.mkdir(mode=0o700)
            materialized_tool = _materialize_eqy_tool(runtime, controller_dir)
            command, _ = build_docker_command(
                config,
                run_id="run",
                tag="PI_LANE_run",
                pdk_root=root / "pdk",
                overrides=[],
                frm=None,
                to=None,
                jobs=2,
                runtime_image_ref="sha256:" + "d" * 64,
                eqy_tool_path=materialized_tool,
            )
            self.assertEqual(runtime["script_sha256"], digest)
            self.assertEqual(materialized_tool.stat().st_mode & 0o777, 0o555)
            self.assertEqual(file_sha256(materialized_tool), runtime["tool_sha256"])
            self.assertIn(
                f"{materialized_tool}:/root/.nix-profile/bin/make:ro", command
            )
            self.assertIn(f"{materialized_tool}:/root/.nix-profile/bin/awk:ro", command)

    def test_docker_command_rejects_a_noncanonical_run_mount_tag(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            config_path = root / "config.yaml"
            config_path.write_text("DESIGN_NAME: demo\nPDK: sky130A\n")
            config = load_design_config(config_path)
            with self.assertRaisesRegex(ValueError, "exact PI_LANE"):
                build_docker_command(
                    config,
                    run_id="run",
                    tag="../../runs",
                    pdk_root=root / "pdk",
                    overrides=[],
                    frm=None,
                    to=None,
                    jobs=None,
                    runtime_image_ref="sha256:" + "d" * 64,
                )

    def test_materialized_eqy_tool_never_replaces_an_existing_path(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            source = root / "source.py"
            source.write_text("#!/usr/bin/env python3\n", encoding="utf-8")
            source.chmod(0o644)
            runtime = {
                "tool_path": source,
                "tool_sha256": file_sha256(source),
            }

            existing_dir = root / "existing"
            existing_dir.mkdir()
            existing = existing_dir / "eqy-tool"
            existing.write_text("do not replace\n", encoding="utf-8")
            with self.assertRaises(FileExistsError):
                _materialize_eqy_tool(runtime, existing_dir)
            self.assertEqual(existing.read_text(encoding="utf-8"), "do not replace\n")

            symlink_dir = root / "symlink"
            symlink_dir.mkdir()
            outside = root / "outside"
            outside.write_text("do not replace\n", encoding="utf-8")
            (symlink_dir / "eqy-tool").symlink_to(outside)
            with self.assertRaises(FileExistsError):
                _materialize_eqy_tool(runtime, symlink_dir)
            self.assertEqual(outside.read_text(encoding="utf-8"), "do not replace\n")

    def test_start_run_wires_identity_to_visualizer_not_flow_builder(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            config_path = root / "config.yaml"
            config_path.write_text("DESIGN_NAME: demo\nPDK: sky130A\n")
            pdk_root = root / "pdk"
            pdk_root.mkdir()
            state_root = root / "state"
            with (
                patch("pi_lane.controller.STATE_ROOT", state_root),
                patch("pi_lane.controller.make_run_id", return_value="wired-run"),
                patch(
                    "pi_lane.controller.require_locked_image",
                    return_value=locked_image_identity(),
                ),
                patch("pi_lane.controller.image_id", return_value="sha256:" + "d" * 64),
                patch(
                    "pi_lane.controller.image_librelane_version", return_value="3.0.5"
                ),
                patch(
                    "pi_lane.controller.installed_pdk_revision", return_value="b" * 40
                ),
                patch(
                    "pi_lane.controller.pdk_integrity_report",
                    return_value={
                        "ready": True,
                        "tree_sha256": "c" * 64,
                        "manifest": str(root / "manifest.json"),
                    },
                ),
                patch("pi_lane.controller.active_duplicate", return_value=None),
                patch(
                    "pi_lane.controller.validate_config_in_image",
                    return_value={
                        "schema_version": 1,
                        "status": "pass",
                        "provider": "pinned_librelane_config_loader",
                        "step_count": 80,
                    },
                ) as config_preflight,
                patch(
                    "pi_lane.controller.subprocess.Popen",
                    return_value=SimpleNamespace(pid=1234),
                ),
                patch("pi_lane.controller.pid_start_time", return_value=2468),
                patch(
                    "pi_lane.controller._spawn_visualizer",
                    return_value=(5678, 1357),
                ),
                patch("pi_lane.controller.set_current"),
                patch(
                    "pi_lane.controller.refresh_status", side_effect=lambda state: state
                ),
                patch(
                    "pi_lane.controller.build_visualizer_command",
                    wraps=build_visualizer_command,
                ) as visual_builder,
            ):
                state = start_run(
                    config_path,
                    pdk_root=pdk_root,
                    workflow_id="workflow-1",
                    attempt_id="attempt-1",
                    idempotency_key="request-1",
                    origin="rtl-closure",
                )
            visual_args = visual_builder.call_args.kwargs
            self.assertEqual(visual_args["config_sha256"], state["config_sha256"])
            self.assertEqual(visual_args["pdk_integrity_sha256"], "c" * 64)
            self.assertEqual(visual_args["image_digest"], "sha256:" + "d" * 64)
            self.assertEqual(visual_args["runtime_image_ref"], "sha256:" + "d" * 64)
            self.assertEqual(visual_args["librelane_version"], "3.0.5")
            self.assertEqual(visual_args["main_pid"], 1234)
            self.assertEqual(visual_args["main_pid_start_time"], 2468)
            self.assertEqual(state["visualizer_pid"], 5678)
            self.assertEqual(state["visualizer_pid_start_time"], 1357)
            self.assertEqual(state["workflow_id"], "workflow-1")
            self.assertEqual(state["attempt_id"], "attempt-1")
            self.assertEqual(state["idempotency_key"], "request-1")
            self.assertEqual(state["origin"], {"component": "rtl-closure"})
            self.assertEqual(state["container_identity"]["run_id"], "wired-run")
            self.assertEqual(state["config_preflight"]["status"], "pass")
            self.assertEqual(config_preflight.call_args.kwargs["overrides"], ())

    def test_start_run_registers_main_and_visualizer_before_reconcile_can_advance(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            config_path = root / "config.yaml"
            config_path.write_text("DESIGN_NAME: demo\nPDK: sky130A\n")
            pdk_root = root / "pdk"
            pdk_root.mkdir()
            state_root = root / "state"

            def reconcile_during_visualizer_spawn(*args: object, **kwargs: object):
                observed = reconcile_status("registration-race")
                self.assertEqual(observed["status"], "launching")
                return 5678, 1357

            with (
                patch("pi_lane.controller.STATE_ROOT", state_root),
                patch("pi_lane.run_state.STATE_ROOT", state_root),
                patch(
                    "pi_lane.controller.make_run_id", return_value="registration-race"
                ),
                patch(
                    "pi_lane.controller.require_locked_image",
                    return_value=locked_image_identity(),
                ),
                patch("pi_lane.controller.image_id", return_value="sha256:" + "d" * 64),
                patch(
                    "pi_lane.controller.image_librelane_version",
                    return_value=LIBRELANE_VERSION,
                ),
                patch(
                    "pi_lane.controller.installed_pdk_revision",
                    return_value=PDK_REVISIONS["sky130A"][1],
                ),
                patch(
                    "pi_lane.controller.pdk_integrity_report",
                    return_value={
                        "ready": True,
                        "tree_sha256": "c" * 64,
                        "manifest": str(root / "manifest.json"),
                    },
                ),
                patch("pi_lane.controller.active_duplicate", return_value=None),
                patch(
                    "pi_lane.controller.validate_config_in_image",
                    return_value={"status": "pass"},
                ),
                patch(
                    "pi_lane.controller.subprocess.Popen",
                    return_value=SimpleNamespace(pid=1234),
                ),
                patch("pi_lane.controller.pid_start_time", return_value=2468),
                patch("pi_lane.controller.pid_identity_alive", return_value=True),
                patch(
                    "pi_lane.controller._spawn_visualizer",
                    side_effect=reconcile_during_visualizer_spawn,
                ),
                patch("pi_lane.controller.set_current"),
            ):
                state = start_run(config_path, pdk_root=pdk_root)

            self.assertEqual(state["status"], "running")
            self.assertEqual(state["pid"], 1234)
            self.assertEqual(state["visualizer_pid"], 5678)
            saved = json.loads(
                (state_root / "registration-race" / "state.json").read_text()
            )
            self.assertEqual(saved["pid"], 1234)
            self.assertEqual(saved["visualizer_pid"], 5678)

    def test_start_run_fails_closed_and_cleans_up_without_main_pid_identity(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            config_path = root / "config.yaml"
            config_path.write_text("DESIGN_NAME: demo\nPDK: sky130A\n")
            pdk_root = root / "pdk"
            pdk_root.mkdir()
            state_root = root / "state"
            with (
                patch("pi_lane.controller.STATE_ROOT", state_root),
                patch(
                    "pi_lane.controller.make_run_id", return_value="identity-failure"
                ),
                patch(
                    "pi_lane.controller.require_locked_image",
                    return_value=locked_image_identity(),
                ),
                patch(
                    "pi_lane.controller.image_id",
                    return_value="sha256:" + "d" * 64,
                ),
                patch(
                    "pi_lane.controller.image_librelane_version",
                    return_value=LIBRELANE_VERSION,
                ),
                patch(
                    "pi_lane.controller.installed_pdk_revision",
                    return_value=PDK_REVISIONS["sky130A"][1],
                ),
                patch(
                    "pi_lane.controller.pdk_integrity_report",
                    return_value={
                        "ready": True,
                        "tree_sha256": "c" * 64,
                        "manifest": str(root / "manifest.json"),
                    },
                ),
                patch("pi_lane.controller.active_duplicate", return_value=None),
                patch(
                    "pi_lane.controller.validate_config_in_image",
                    return_value={"status": "pass"},
                ),
                patch(
                    "pi_lane.controller.subprocess.Popen",
                    return_value=SimpleNamespace(pid=1234),
                ),
                patch("pi_lane.controller.pid_start_time", return_value=None),
                patch("pi_lane.controller._terminate_spawned_process") as terminate,
                patch("pi_lane.controller._spawn_visualizer") as spawn,
                patch("pi_lane.controller.set_current"),
            ):
                with self.assertRaisesRegex(RuntimeError, "start time is unavailable"):
                    start_run(config_path, pdk_root=pdk_root)
            terminate.assert_called_once_with(
                1234,
                container_name="pi-lane-identity-failure",
            )
            spawn.assert_not_called()
            failed = json.loads(
                (state_root / "identity-failure" / "state.json").read_text(
                    encoding="utf-8"
                )
            )
            self.assertEqual(failed["status"], "failed")
            self.assertEqual(failed["failure"]["code"], "LANE_WORKER_IDENTITY_FAILED")

    def test_start_run_rejects_non_classic_flow_before_runtime_checks(self):
        with tempfile.TemporaryDirectory() as temp:
            config_path = Path(temp) / "config.yaml"
            config_path.write_text(
                "DESIGN_NAME: demo\nPDK: sky130A\nmeta:\n  flow: Other\n"
            )
            with self.assertRaisesRegex(ValueError, "only the pinned.*Classic"):
                start_run(config_path)

    def test_start_run_rejects_a_changed_resume_input_bundle_before_runtime_checks(
        self,
    ):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            rtl = root / "demo.sv"
            rtl.write_text("module demo; endmodule\n", encoding="utf-8")
            config_path = root / "config.yaml"
            config_path.write_text(
                "DESIGN_NAME: demo\nPDK: sky130A\nVERILOG_FILES: [dir::demo.sv]\n",
                encoding="utf-8",
            )
            with self.assertRaisesRegex(ValueError, "design inputs changed"):
                start_run(
                    config_path,
                    expected_input_bundle_sha256="a" * 64,
                )

    def test_step_discovery_uses_step_metadata(self):
        with tempfile.TemporaryDirectory() as temp:
            run = Path(temp)
            step = run / "01-openroad-floorplan"
            step.mkdir()
            (step / "config.json").write_text(
                json.dumps({"meta": {"step": "OpenROAD.Floorplan"}})
            )
            (step / "state_out.json").write_text("{}")
            found = discover_steps(run)
        self.assertEqual(found[0]["id"], "OpenROAD.Floorplan")
        self.assertTrue(found[0]["completed"])

    def test_step_discovery_preserves_repeated_classic_step_identity(self):
        with tempfile.TemporaryDirectory() as temp:
            run = Path(temp)
            step = run / "31-openroad-stamidpnr-3"
            step.mkdir()
            # LibreLane's metadata intentionally omits SequentialFlow's
            # normalized duplicate suffix, so the directory is authoritative
            # for known Classic registry identities.
            (step / "config.json").write_text(
                json.dumps({"meta": {"step": "OpenROAD.STAMidPNR"}})
            )
            (step / "state_out.json").write_text("{}")
            found = discover_steps(run)
        self.assertEqual(found[0]["id"], "OpenROAD.STAMidPNR-3")
        self.assertTrue(found[0]["completed"])

    def test_step_discovery_rejects_symlinked_steps_and_state(self):
        with (
            tempfile.TemporaryDirectory() as temp,
            tempfile.TemporaryDirectory() as other,
        ):
            run = Path(temp)
            external_step = Path(other) / "01-external"
            external_step.mkdir()
            (run / "01-external").symlink_to(external_step, target_is_directory=True)
            self.assertEqual(discover_steps(run), [])

            real_step = run / "02-openroad-floorplan"
            real_step.mkdir()
            (real_step / "config.json").write_text(
                json.dumps({"meta": {"step": "OpenROAD.Floorplan"}})
            )
            external_state = Path(other) / "state_out.json"
            external_state.write_text("{}\n")
            (real_step / "state_out.json").symlink_to(external_state)
            found = discover_steps(run)
            self.assertFalse(found[0]["completed"])
            self.assertIsNone(found[0]["state_out"])

    def test_notebook_preview_boundary_requires_an_updated_def(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            changed = root / "13-openroad-floorplan"
            changed.mkdir()
            (changed / "config.json").write_text(
                json.dumps({"meta": {"step": "OpenROAD.Floorplan"}})
            )
            (changed / "state_in.json").write_text(json.dumps({"def": None}))
            (changed / "state_out.json").write_text(
                json.dumps({"def": str(changed / "demo.def")})
            )
            self.assertEqual(preview_kind(changed), "render")

            unchanged = root / "14-openroad-stamidpnr"
            unchanged.mkdir()
            (unchanged / "config.json").write_text(
                json.dumps({"meta": {"step": "OpenROAD.STAMidPNR"}})
            )
            for name in ("state_in.json", "state_out.json"):
                (unchanged / name).write_text(json.dumps({"def": "/demo.def"}))
            self.assertIsNone(preview_kind(unchanged))

    def test_visualizer_containers_remain_offline_and_read_only(self):
        root = Path("/tmp/pi-lane-test")
        command = build_render_command(
            runtime_image_ref="sha256:" + "d" * 64,
            container_name="pi-lane-viz-run-13",
            design_dir=root / "design",
            input_snapshot_dir=root / "snapshot",
            run_dir=root / "design" / "runs" / "run",
            visualization_root=root / "design" / "runs" / "run" / "visualizations",
            pdk_root=root / "pdk",
            config_path=root / "design" / "config.yaml",
            state_path=root / "design" / "runs" / "run" / "state_out.json",
            pdk="sky130A",
            output_dir=root / "design" / "visualizations" / "13-floorplan",
            overrides=["FP_CORE_UTIL=40"],
        )
        self.assertIn("none", command)
        self.assertIn("--read-only", command)
        self.assertIn("sha256:" + "d" * 64, command)
        self.assertNotIn(LIBRELANE_IMAGE, command)
        self.assertIn(f"io.pi-lane.image-tag={LIBRELANE_IMAGE}", command)
        self.assertIn(
            "io.pi-lane.image-digest=sha256:" + "d" * 64,
            command,
        )
        self.assertIn(f"{root / 'snapshot'}:/pi-lane/design:ro", command)
        self.assertIn(
            f"{root / 'design' / 'runs' / 'run'}:{root / 'design' / 'runs' / 'run'}:ro",
            command,
        )
        self.assertIn(
            f"{root / 'design' / 'runs' / 'run' / 'visualizations'}:"
            "/pi-lane/visualizations:rw",
            command,
        )
        self.assertIn("/pi-lane/design/config.yaml", command)
        self.assertIn("/pi-lane/visualizations/13-floorplan", command)
        self.assertNotIn(
            f"{root / 'snapshot'}:{root / 'design'}:ro",
            command,
        )

    def test_visualizer_worker_records_the_managed_inputs(self):
        command = build_visualizer_command(
            run_id="run",
            run_dir=Path("/design/runs/run"),
            design_dir=Path("/design"),
            design_name="demo",
            config_path=Path("/design/config.yaml"),
            input_snapshot_dir=Path("/state/input-snapshot"),
            input_bundle_sha256="e" * 64,
            pdk="sky130A",
            pdk_root=Path("/pdk"),
            image="ghcr.io/librelane/librelane:3.0.5",
            runtime_image_ref="sha256:" + "d" * 64,
            config_sha256="a" * 64,
            pdk_revision="b" * 40,
            pdk_integrity_sha256="c" * 64,
            image_digest="sha256:" + "d" * 64,
            librelane_version="3.0.5",
            main_exit_file=Path("/state/exit"),
            main_pid=1234,
            main_pid_start_time=2468,
            visualizer_exit_file=Path("/state/viz-exit"),
            overrides=["FP_CORE_UTIL=40"],
        )
        self.assertIn("pi_lane.visualizer", command)
        self.assertIn("/design/runs/run", command)
        self.assertIn("FP_CORE_UTIL=40", command)
        self.assertNotIn("--retry-failed", command)

        retry_command = build_visualizer_command(
            run_id="run",
            run_dir=Path("/design/runs/run"),
            design_dir=Path("/design"),
            design_name="demo",
            config_path=Path("/design/config.yaml"),
            input_snapshot_dir=Path("/state/input-snapshot"),
            input_bundle_sha256="e" * 64,
            pdk="sky130A",
            pdk_root=Path("/pdk"),
            image="ghcr.io/librelane/librelane:3.0.5",
            runtime_image_ref="sha256:" + "d" * 64,
            config_sha256="a" * 64,
            pdk_revision="b" * 40,
            pdk_integrity_sha256="c" * 64,
            image_digest="sha256:" + "d" * 64,
            librelane_version="3.0.5",
            main_exit_file=Path("/state/exit"),
            main_pid=1234,
            main_pid_start_time=2468,
            visualizer_exit_file=Path("/state/viz-exit"),
            overrides=[],
            retry_failed=True,
        )
        self.assertIn("--retry-failed", retry_command)

    def test_completed_visualization_is_idempotent_without_explicit_retry(self):
        state = {
            "run_id": "run",
            "visualization": {
                "status": "completed_with_errors",
                "is_running": False,
            },
        }
        with (
            patch("pi_lane.controller.load_state", return_value=state),
            patch("pi_lane.controller.refresh_status", return_value=state),
            patch("pi_lane.controller._spawn_visualizer") as spawn,
        ):
            self.assertIs(start_visualization("run"), state)
        spawn.assert_not_called()

    def test_pdk_install_is_pinned_and_isolated(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            command, _ = build_pdk_install_command(
                "sky130A",
                pdk_root=root,
                run_id="run",
                runtime_image_ref="sha256:" + "d" * 64,
                proxy_env={},
            )
            self.assertIn(PDK_REVISIONS["sky130A"][1], command)
            self.assertIn(f"{root}:{root}:rw", command)
            self.assertIn("io.pi-lane.pdk-install-id=run", command)
            self.assertNotIn("--network", command)
            self.assertFalse(pdk_installed("sky130A", root))
            family, revision = PDK_REVISIONS["sky130A"]
            version = root / "ciel" / family / "versions" / revision / "sky130A"
            marker = version / "libs.tech" / "librelane" / "config.tcl"
            marker.parent.mkdir(parents=True)
            marker.write_text("# open PDK marker\n")
            (version / "SOURCES").write_text(
                f"open_pdks {revision}\n", encoding="utf-8"
            )
            version.chmod(0o755)
            for path in version.rglob("*"):
                path.chmod(0o755 if path.is_dir() else 0o444)
            current = root / "ciel" / family / "current"
            current.write_text(revision, encoding="ascii")
            (root / "sky130A").symlink_to(version, target_is_directory=True)
            self.assertTrue(pdk_installed("sky130A", root))
            self.assertEqual(installed_pdk_revision("sky130A", root), revision)
            self.assertTrue(pdk_ready("sky130A", root))
            marker.chmod(0o644)
            marker.write_text("# modified after attestation\n", encoding="utf-8")
            report = pdk_integrity_report("sky130A", root)
            self.assertFalse(report["ready"])
            self.assertIn("content changed", report["error"])

    def test_pdk_status_can_reuse_a_persisted_integrity_attestation(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            family, revision = PDK_REVISIONS["sky130A"]
            version = root / "ciel" / family / "versions" / revision / "sky130A"
            marker = version / "libs.tech" / "librelane" / "config.tcl"
            marker.parent.mkdir(parents=True)
            marker.write_text("# marker\n")
            (version / "SOURCES").write_text(f"open_pdks {revision}\n")
            for path in [version, *version.rglob("*")]:
                path.chmod(0o755 if path.is_dir() else 0o444)
            (root / "ciel" / family / "current").write_text(revision)
            (root / "sky130A").symlink_to(version, target_is_directory=True)
            self.assertTrue(
                pdk_integrity_report("sky130A", root, allow_bootstrap=True)["ready"]
            )
            with patch(
                "pi_lane.controller._pdk_tree_digest",
                side_effect=AssertionError("status polling must not hash the PDK tree"),
            ):
                cached = pdk_integrity_report("sky130A", root, verify_content=False)
            self.assertTrue(cached["ready"])
            self.assertFalse(cached["content_verified"])

    def test_install_stop_is_terminal_idempotent_and_requires_pid_identity(self):
        terminal = {
            "status": "completed",
            "run_id": "install-1",
            "pid": 123,
            "pid_start_time": 456,
        }
        with (
            patch("pi_lane.controller._load_image_install", return_value=terminal),
            patch("pi_lane.controller.refresh_image_install", return_value=terminal),
            patch("pi_lane.controller.os.killpg") as kill,
        ):
            self.assertIs(stop_image_install("install-1"), terminal)
        kill.assert_not_called()

        active = {"status": "running", "run_id": "install-2", "pid": 123}
        with (
            patch("pi_lane.controller._load_pdk_install", return_value=active),
            patch("pi_lane.controller.refresh_pdk_install", return_value=active),
        ):
            with self.assertRaisesRegex(RuntimeError, "persisted PID identity"):
                stop_pdk_install("install-2")

    def test_image_installer_is_serialized_and_cleans_up_missing_pid_identity(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            state_root = root / "state"
            install_root = root / "image-installs"
            current = install_root / "current"
            with (
                patch("pi_lane.controller.STATE_ROOT", state_root),
                patch("pi_lane.controller.IMAGE_INSTALL_ROOT", install_root),
                patch("pi_lane.controller.IMAGE_INSTALL_CURRENT", current),
                patch(
                    "pi_lane.controller._managed_install_id",
                    return_value="image-install",
                ),
                patch(
                    "pi_lane.controller.subprocess.Popen",
                    return_value=SimpleNamespace(pid=1234),
                ),
                patch(
                    "pi_lane.controller.image_id",
                    return_value="sha256:" + "d" * 64,
                ),
                patch("pi_lane.controller.pid_start_time", return_value=None),
                patch("pi_lane.controller._terminate_spawned_process") as terminate,
                patch("pi_lane.controller.run_lock") as start_lock,
            ):
                with self.assertRaisesRegex(RuntimeError, "start time is unavailable"):
                    start_image_install()
            start_lock.assert_called_once_with(state_root / ".start-state.json")
            terminate.assert_called_once_with(1234)
            failed = json.loads(
                (install_root / "image-install" / "state.json").read_text(
                    encoding="utf-8"
                )
            )
            self.assertEqual(failed["status"], "failed")
            self.assertEqual(failed["exit_code"], 125)
            self.assertIn("start time is unavailable", failed["errors"][0])

    def test_image_lock_is_tofu_pinned_and_replacement_requires_exact_digest(self):
        with tempfile.TemporaryDirectory() as temp:
            install_root = Path(temp) / "image-installs"
            first_digest = "sha256:" + "a" * 64
            second_digest = "sha256:" + "b" * 64
            first_state = {
                "status": "completed",
                "run_id": "image-install-1",
                "image": LIBRELANE_IMAGE,
                "image_digest": first_digest,
                "expected_digest": None,
                "replace_lock": False,
            }
            with (
                patch("pi_lane.controller.IMAGE_INSTALL_ROOT", install_root),
                patch("pi_lane.controller.image_id", return_value=first_digest),
                patch(
                    "pi_lane.controller.image_librelane_version",
                    return_value=LIBRELANE_VERSION,
                ),
            ):
                first = _record_image_lock(first_state)
                self.assertEqual(load_image_lock(), first)
                self.assertEqual(require_locked_image()["image_digest"], first_digest)

            replacement = {
                **first_state,
                "run_id": "image-install-2",
                "image_digest": second_digest,
            }
            with (
                patch("pi_lane.controller.IMAGE_INSTALL_ROOT", install_root),
                patch("pi_lane.controller.image_id", return_value=second_digest),
                patch(
                    "pi_lane.controller.image_librelane_version",
                    return_value=LIBRELANE_VERSION,
                ),
            ):
                with self.assertRaisesRegex(RuntimeError, "persistent lock"):
                    _record_image_lock(replacement)
                with self.assertRaisesRegex(
                    RuntimeError, "differs from the persistent"
                ):
                    require_locked_image()

                replacement.update(
                    {"replace_lock": True, "expected_digest": second_digest}
                )
                second = _record_image_lock(replacement)
                self.assertEqual(second["revision"], 2)
                self.assertEqual(second["previous_lock_sha256"], first["lock_sha256"])
                self.assertEqual(load_image_lock(), second)

    def test_image_lock_missing_and_ambiguous_replacement_fail_closed(self):
        with tempfile.TemporaryDirectory() as temp:
            install_root = Path(temp) / "image-installs"
            with patch("pi_lane.controller.IMAGE_INSTALL_ROOT", install_root):
                with self.assertRaisesRegex(RuntimeError, "not locked"):
                    require_locked_image()
            with self.assertRaisesRegex(ValueError, "requires --expected-digest"):
                start_image_install(replace_lock=True)

    def test_completed_image_status_persists_the_image_lock(self):
        with tempfile.TemporaryDirectory() as temp:
            install_root = Path(temp) / "image-installs"
            run_dir = install_root / "image-install-1"
            run_dir.mkdir(parents=True)
            digest = "sha256:" + "a" * 64
            exit_file = run_dir / "exit_code"
            exit_file.write_text("0\n", encoding="ascii")
            build_log = run_dir / "install.log"
            build_log.write_text("completed\n", encoding="utf-8")
            state = {
                "kind": "image_install",
                "run_id": "image-install-1",
                "state_file": str(run_dir / "state.json"),
                "build_log": str(build_log),
                "exit_file": str(exit_file),
                "pid": 999999,
                "pid_start_time": 1,
                "status": "completed",
                "installation_attested": True,
                "image": LIBRELANE_IMAGE,
                "image_digest": digest,
                "librelane_version": LIBRELANE_VERSION,
                "started_epoch": 1.0,
                "stop_requested": False,
            }
            Path(state["state_file"]).write_text(json.dumps(state), encoding="utf-8")
            with (
                patch("pi_lane.controller.IMAGE_INSTALL_ROOT", install_root),
                patch(
                    "pi_lane.controller.IMAGE_INSTALL_CURRENT", install_root / "current"
                ),
                patch("pi_lane.controller.image_id", return_value=digest),
                patch(
                    "pi_lane.controller.image_librelane_version",
                    return_value=LIBRELANE_VERSION,
                ),
                patch("pi_lane.controller.pid_identity_alive", return_value=False),
            ):
                persisted = image_install_status("image-install-1")
                lock = load_image_lock(required=True)

            self.assertTrue(persisted["image_lock_attested"])
            self.assertEqual(lock["image_digest"], digest)

    def test_pdk_installer_registration_failure_terminates_process_and_container(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            state_root = root / "state"
            install_root = root / "pdk-installs"
            current = install_root / "current"
            pdk_root = root / "pdk"
            with (
                patch("pi_lane.controller.STATE_ROOT", state_root),
                patch("pi_lane.controller.PDK_INSTALL_ROOT", install_root),
                patch("pi_lane.controller.PDK_INSTALL_CURRENT", current),
                patch(
                    "pi_lane.controller._managed_install_id",
                    return_value="pdk-install",
                ),
                patch(
                    "pi_lane.controller.require_locked_image",
                    return_value=locked_image_identity(),
                ),
                patch(
                    "pi_lane.controller.image_id",
                    return_value="sha256:" + "d" * 64,
                ),
                patch(
                    "pi_lane.controller.image_librelane_version",
                    return_value=LIBRELANE_VERSION,
                ),
                patch(
                    "pi_lane.controller.subprocess.Popen",
                    return_value=SimpleNamespace(pid=2345),
                ),
                patch("pi_lane.controller.pid_start_time", return_value=6789),
                patch(
                    "pi_lane.controller.save_state",
                    side_effect=[None, RuntimeError("state registration failed")],
                ),
                patch("pi_lane.controller._terminate_spawned_process") as terminate,
                patch("pi_lane.controller.run_lock") as start_lock,
            ):
                with self.assertRaisesRegex(RuntimeError, "registration failed"):
                    start_pdk_install("sky130A", pdk_root=pdk_root)
            start_lock.assert_called_once_with(state_root / ".start-state.json")
            terminate.assert_called_once_with(
                2345,
                container_name="pi-lane-pdk-pdk-install",
            )
            self.assertEqual(
                (install_root / "pdk-install" / "exit_code").read_text(
                    encoding="utf-8"
                ),
                "125\n",
            )
            self.assertIn(
                "state registration failed",
                (install_root / "pdk-install" / "install.log").read_text(
                    encoding="utf-8"
                ),
            )

    def test_manual_visualization_save_failure_terminates_new_sidecar(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            design_dir = root / "design"
            design_dir.mkdir()
            config_path = design_dir / "config.yaml"
            config_path.write_text("DESIGN_NAME: demo\nPDK: sky130A\n")
            pdk_root = root / "pdk"
            pdk_root.mkdir()
            controller_dir = root / "controller"
            controller_dir.mkdir()
            (controller_dir / "input-snapshot").mkdir()
            digest = "sha256:" + "d" * 64
            state = {
                "schema_version": 6,
                "run_id": "manual-visualization",
                "status": "completed",
                "visualization": {"status": "unavailable", "is_running": False},
                "flow_registry_version": CLASSIC_REGISTRY_VERSION,
                "image": LIBRELANE_IMAGE,
                "image_digest": digest,
                "runtime_image_ref": digest,
                "librelane_version": LIBRELANE_VERSION,
                "design_dir": str(design_dir),
                "design_name": "demo",
                "config_path": str(config_path),
                "config_sha256": file_sha256(config_path),
                "pdk": "sky130A",
                "pdk_root": str(pdk_root),
                "pdk_revision": PDK_REVISIONS["sky130A"][1],
                "pdk_integrity_sha256": "c" * 64,
                "controller_dir": str(controller_dir),
                "input_snapshot_dir": str(controller_dir / "input-snapshot"),
                "input_bundle_sha256": "e" * 64,
                "librelane_run_dir": str(
                    design_dir / "runs" / "PI_LANE_manual-visualization"
                ),
                "exit_file": str(controller_dir / "exit_code"),
                "pid": 1234,
                "pid_start_time": 5678,
                "overrides": [],
            }
            with (
                patch("pi_lane.controller.STATE_ROOT", root / "state"),
                patch("pi_lane.controller.load_state", return_value=state),
                patch("pi_lane.controller.refresh_status", return_value=state),
                patch("pi_lane.controller.image_id", return_value=digest),
                patch(
                    "pi_lane.controller.image_librelane_version",
                    return_value=LIBRELANE_VERSION,
                ),
                patch(
                    "pi_lane.controller.pdk_integrity_report",
                    return_value={"ready": True, "tree_sha256": "c" * 64},
                ),
                patch("pi_lane.controller._verify_design_snapshot", return_value=[]),
                patch(
                    "pi_lane.controller.installed_pdk_revision",
                    return_value=PDK_REVISIONS["sky130A"][1],
                ),
                patch(
                    "pi_lane.controller._spawn_visualizer",
                    return_value=(3456, 7890),
                ),
                patch(
                    "pi_lane.controller.build_visualizer_command",
                    wraps=build_visualizer_command,
                ) as visual_builder,
                patch(
                    "pi_lane.controller.save_state",
                    side_effect=RuntimeError("CAS failed"),
                ) as save,
                patch("pi_lane.controller._terminate_spawned_process") as terminate,
            ):
                with self.assertRaisesRegex(RuntimeError, "CAS failed"):
                    start_visualization("manual-visualization")
            terminate.assert_called_once_with(3456)
            registered = save.call_args.args[0]
            self.assertEqual(
                visual_builder.call_args.kwargs["input_snapshot_dir"],
                controller_dir / "input-snapshot",
            )
            self.assertEqual(
                visual_builder.call_args.kwargs["input_bundle_sha256"],
                "e" * 64,
            )
            self.assertEqual(
                visual_builder.call_args.kwargs["runtime_image_ref"], digest
            )
            self.assertEqual(registered["runtime_image_ref"], digest)

    def test_visualizer_spawn_cleans_up_when_start_time_cannot_be_read(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            with (
                patch(
                    "pi_lane.controller.subprocess.Popen",
                    return_value=SimpleNamespace(pid=4567),
                ),
                patch("pi_lane.controller.pid_start_time", return_value=None),
                patch("pi_lane.controller._terminate_spawned_process") as terminate,
            ):
                spawned = _spawn_visualizer(
                    ["visualizer"],
                    cwd=root,
                    log_path=root / "visualizer.log",
                )
            self.assertIsNone(spawned)
            terminate.assert_called_once_with(4567)

    def test_pdk_integrity_bootstrap_rejects_internal_symlinks(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            family, revision = PDK_REVISIONS["sky130A"]
            version = root / "ciel" / family / "versions" / revision / "sky130A"
            marker = version / "libs.tech" / "librelane" / "config.tcl"
            marker.parent.mkdir(parents=True)
            marker.write_text("# marker\n")
            (version / "SOURCES").write_text(f"open_pdks {revision}\n")
            (version / "escape").symlink_to(Path("/tmp"), target_is_directory=True)
            (root / "ciel" / family / "current").write_text(revision)
            (root / "sky130A").symlink_to(version, target_is_directory=True)
            report = pdk_integrity_report("sky130A", root, allow_bootstrap=True)
            self.assertFalse(report["ready"])
            self.assertIn("bootstrap", report["error"])

    def test_openlane_compatible_pdk_layout_is_installed(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            marker = root / "sky130A" / "libs.tech" / "openlane" / "config.tcl"
            marker.parent.mkdir(parents=True)
            marker.write_text("# LibreLane-compatible legacy marker\n")
            self.assertTrue(pdk_installed("sky130A", root))

    def test_pdk_symlink_must_remain_inside_dedicated_root(self):
        with (
            tempfile.TemporaryDirectory() as temp,
            tempfile.TemporaryDirectory() as other,
        ):
            root = Path(temp)
            external = Path(other)
            marker = external / "libs.tech" / "openlane" / "config.tcl"
            marker.parent.mkdir(parents=True)
            marker.write_text("# must not be accepted\n")
            (root / "sky130A").symlink_to(external, target_is_directory=True)
            self.assertFalse(pdk_installed("sky130A", root))

    def test_loopback_proxy_is_inherited_without_exposing_value(self):
        proxy = {
            "HTTP_PROXY": "http://127.0.0.1:10808/",
            "HTTPS_PROXY": "http://127.0.0.1:10808/",
            "ALL_PROXY": "socks://127.0.0.1:10808/",
            "NO_PROXY": "localhost,127.0.0.0/8,::1",
        }
        command, _ = build_pdk_install_command(
            "sky130A",
            pdk_root=Path("/tmp/pdk"),
            run_id="run",
            runtime_image_ref="sha256:" + "d" * 64,
            proxy_env=proxy,
        )
        self.assertTrue(pdk_proxy_uses_loopback(proxy))
        self.assertIn("host", command)
        self.assertIn("HTTPS_PROXY", command)
        self.assertNotIn(proxy["HTTPS_PROXY"], command)
        filtered = pdk_proxy_environment(proxy)
        self.assertNotIn("ALL_PROXY", filtered)
        self.assertEqual(
            filtered,
            {name: value for name, value in proxy.items() if name != "ALL_PROXY"},
        )

    def test_remote_proxy_keeps_pdk_installer_on_bridge(self):
        proxy = {"HTTPS_PROXY": "http://proxy.example:8080"}
        command, _ = build_pdk_install_command(
            "sky130A",
            pdk_root=Path("/tmp/pdk"),
            run_id="run",
            runtime_image_ref="sha256:" + "d" * 64,
            proxy_env=proxy,
        )
        self.assertFalse(pdk_proxy_uses_loopback(proxy))
        self.assertNotIn("--network", command)

    def test_missing_qor_is_unknown(self):
        summary = summarize_qor({"route__drc_errors": 0}, "final")
        self.assertEqual(summary["checks"]["route_drc"], "pass")
        self.assertEqual(summary["checks"]["lvs"], "unknown")
        self.assertEqual(summary["gate"], "unknown")
        self.assertEqual(summary["physical_sanity"]["gate"], "unknown")
        self.assertTrue(
            all(
                check["status"] == "unknown"
                for check in summary["physical_sanity"]["checks"].values()
            )
        )

    def test_fractional_negative_and_nonfinite_qor_counts_are_unknown(self):
        for value in (0.5, -1, float("nan"), float("inf"), True, "0"):
            with self.subTest(value=value):
                result = summarize_qor({"route__drc_errors": value}, "final")
                self.assertEqual(result["checks"]["route_drc"], "unknown")

    def test_partial_metrics_never_claim_terminal_signoff(self):
        metrics = complete_metrics(
            core_area=1000.0,
            total_area=1000.0,
            standard_cell_area=400.0,
            filler_area=600.0,
            tap_area=20.0,
            utilization=0.4,
        )
        result = summarize_qor(
            metrics,
            "partial",
            observed_at_step="OpenROAD.DetailedPlacement",
            terminal_maturity=False,
        )
        self.assertEqual(result["maturity"], "partial")
        self.assertEqual(result["observed_signoff"]["route_drc"], "pass")
        self.assertEqual(result["checks"]["route_drc"], "unknown")
        self.assertEqual(result["signoff_gate"], "unknown")

    def test_tool_sentinel_metric_is_excluded_from_qor_value(self):
        result = summarize_qor(
            {
                "timing__setup__ws": 1e39,
                "power__total": float("inf"),
            },
            "partial",
        )
        self.assertIsNone(result["setup_ws_ns"])
        self.assertIsNone(result["power_w"])
        self.assertEqual(
            result["metric_evidence"]["timing__setup__ws"]["validity"],
            "tool_sentinel",
        )
        self.assertEqual(
            result["metric_evidence"]["power__total"]["validity"],
            "tool_sentinel",
        )

    def test_qor_reports_real_areas_and_rejects_sparse_floorplan(self):
        summary = summarize_qor(
            complete_metrics(
                core_area=66333.6,
                total_area=66333.6,
                standard_cell_area=3462.07,
                filler_area=62871.5,
                tap_area=1201.15,
                utilization=0.0521918,
            ),
            "final",
        )
        self.assertAlmostEqual(summary["logic_area_um2"], 2260.92)
        self.assertAlmostEqual(summary["standard_cell_area_um2"], 3462.07)
        self.assertAlmostEqual(summary["core_area_um2"], 66333.6)
        self.assertAlmostEqual(summary["filler_area_um2"], 62871.5)
        self.assertAlmostEqual(summary["tap_area_um2"], 1201.15)
        self.assertAlmostEqual(summary["total_instance_area_um2"], 66333.6)
        self.assertAlmostEqual(summary["utilization_pct"], 5.21918)
        self.assertAlmostEqual(summary["filler_ratio_pct"], 94.780775, places=5)
        self.assertEqual(summary["signoff_gate"], "pass")
        self.assertEqual(summary["physical_sanity"]["gate"], "fail")
        self.assertEqual(
            summary["physical_sanity"]["checks"]["minimum_utilization"]["status"],
            "fail",
        )
        self.assertEqual(
            summary["physical_sanity"]["checks"]["maximum_filler_ratio"]["status"],
            "fail",
        )
        self.assertEqual(
            summary["physical_sanity"]["checks"]["area_consistency"]["status"],
            "pass",
        )
        self.assertEqual(summary["gate"], "fail")
        self.assertTrue(
            any("minimum_utilization" in reason for reason in summary["gate_reasons"])
        )

    def test_proven_minimum_geometry_qualifies_only_raw_density_failures(self):
        result = _physical_sanity(
            total_instance_area_um2=66333.6,
            standard_cell_area_um2=3462.07,
            core_area_um2=66333.6,
            filler_area_um2=62871.5,
            tap_area_um2=1201.15,
            utilization_pct=5.21918,
            utilization_metric="design__instance__utilization__stdcell",
            minimum_geometry_qualification={
                "gate": "pass",
                "classification": "minimum_geometry_dominated",
                "reasons": ["sealed minimum candidate"],
                "integrity_checks": {
                    "plan": "pass",
                    "search": "pass",
                    "geometry": "pass",
                },
            },
        )
        self.assertEqual(result["gate"], "pass")
        self.assertEqual(result["density"]["raw_threshold_gate"], "fail")
        self.assertEqual(result["density"]["qualification_gate"], "pass")
        self.assertEqual(
            result["checks"]["minimum_utilization"]["status"], "fail"
        )

    def test_compact_design_passes_signoff_and_physical_sanity(self):
        summary = summarize_qor(
            complete_metrics(
                core_area=1000.0,
                total_area=1000.0,
                standard_cell_area=400.0,
                filler_area=600.0,
                tap_area=20.0,
                utilization=0.4,
            ),
            "final",
        )
        self.assertEqual(summary["signoff_gate"], "pass")
        self.assertEqual(summary["physical_sanity"]["gate"], "pass")
        self.assertEqual(summary["gate"], "pass")
        self.assertEqual(summary["gate_reasons"], [])

    def test_signoff_failure_fails_composite_gate_even_when_area_is_sane(self):
        summary = summarize_qor(
            complete_metrics(
                core_area=66333.6,
                total_area=66333.6,
                standard_cell_area=16784.8,
                filler_area=49548.8,
                tap_area=1201.15,
                utilization=0.253037,
                setup_violations=48,
                setup_wns=-2.1,
                setup_tns=-7.9,
                max_slew_violations=41,
                max_cap_violations=5,
            ),
            "final",
        )
        self.assertEqual(summary["signoff_gate"], "fail")
        self.assertEqual(summary["physical_sanity"]["gate"], "pass")
        self.assertEqual(summary["gate"], "fail")
        self.assertTrue(
            any("signoff.setup_timing" in reason for reason in summary["gate_reasons"])
        )
        self.assertEqual(
            summary["timing_diagnostics"]["classification"], "timing_structural"
        )
        self.assertEqual(summary["max_slew_violation_count"], 41)
        self.assertEqual(summary["max_cap_violation_count"], 5)

    def test_marginal_timing_and_electrical_failures_are_distinguished(self):
        marginal = complete_metrics(
            core_area=1000.0,
            total_area=1000.0,
            standard_cell_area=400.0,
            filler_area=600.0,
            tap_area=20.0,
            utilization=0.4,
            setup_violations=1,
            setup_wns=-0.1016,
            setup_tns=-0.1016,
        )
        marginal["timing__setup__ws__corner:max_ss"] = -0.1016
        marginal["timing__setup__tns__corner:max_ss"] = -0.1016
        marginal["timing__setup_vio__count__corner:max_ss"] = 1
        marginal_summary = summarize_qor(marginal, "final")
        self.assertEqual(
            marginal_summary["timing_diagnostics"]["classification"],
            "timing_marginal",
        )
        electrical = complete_metrics(
            core_area=1000.0,
            total_area=1000.0,
            standard_cell_area=400.0,
            filler_area=600.0,
            tap_area=20.0,
            utilization=0.4,
            max_slew_violations=2,
        )
        electrical_summary = summarize_qor(electrical, "final")
        self.assertEqual(electrical_summary["checks"]["max_slew"], "fail")
        self.assertEqual(
            electrical_summary["timing_diagnostics"]["classification"],
            "electrical_constraint",
        )

    def test_full_delivery_requires_endpoint_nonempty_gds_and_qor_pass(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            state = completed_run_state(root)
            run_dir = Path(str(state["librelane_run_dir"]))
            write_completed_step(run_dir, 76, "Misc.ReportManufacturability")
            final = run_dir / "final"
            (final / "gds").mkdir(parents=True)
            write_valid_gds(final / "gds" / "delivery_test.gds")
            (final / "metrics.json").write_text(
                json.dumps(
                    complete_metrics(
                        core_area=1000.0,
                        total_area=1000.0,
                        standard_cell_area=400.0,
                        filler_area=600.0,
                        tap_area=20.0,
                        utilization=0.4,
                    )
                ),
                encoding="utf-8",
            )
            with patch("pi_lane.controller.save_state"):
                refreshed = refresh_status(state)

        self.assertEqual(refreshed["process_status"], "completed")
        self.assertEqual(refreshed["flow_scope"], "full")
        self.assertTrue(refreshed["endpoint_reached"])
        self.assertEqual(refreshed["delivery_gate"], "pass")
        self.assertEqual(refreshed["delivery_gate_reasons"], [])
        self.assertEqual(refreshed["status"], "completed")
        self.assertEqual(len(refreshed["canonical_gds"]), 1)

    def test_schema_v5_delivery_rechecks_digest_and_complete_pdk_identity(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            state = completed_run_state(root)
            digest = "sha256:" + "d" * 64
            pdk_root = root / "pdk"
            state.update(
                {
                    "schema_version": 6,
                    "runtime_image_ref": digest,
                    "pdk_root": str(pdk_root),
                    "runtime_environment_attestation": None,
                    "container_identity": {
                        "image": LIBRELANE_IMAGE,
                        "image_digest": digest,
                        "runtime_image_ref": digest,
                    },
                    "config_preflight": {
                        "image": LIBRELANE_IMAGE,
                        "image_digest": digest,
                        "runtime_image_ref": digest,
                    },
                }
            )
            run_dir = Path(str(state["librelane_run_dir"]))
            write_completed_step(run_dir, 80, "Misc.ReportManufacturability")
            final = run_dir / "final"
            (final / "gds").mkdir(parents=True)
            write_valid_gds(final / "gds" / "delivery_test.gds")
            (final / "metrics.json").write_text(
                json.dumps(
                    complete_metrics(
                        core_area=1000.0,
                        total_area=1000.0,
                        standard_cell_area=400.0,
                        filler_area=600.0,
                        tap_area=20.0,
                        utilization=0.4,
                    )
                ),
                encoding="utf-8",
            )
            valid_pdk = {
                "ready": True,
                "content_verified": True,
                "tree_sha256": "c" * 64,
                "file_count": 123,
                "total_bytes": 456,
                "error": None,
            }
            with (
                patch("pi_lane.controller._verify_design_snapshot", return_value=[]),
                patch(
                    "pi_lane.controller.image_id", return_value=digest
                ) as image_id_mock,
                patch(
                    "pi_lane.controller.image_librelane_version",
                    return_value=LIBRELANE_VERSION,
                ) as image_version_mock,
                patch(
                    "pi_lane.controller.installed_pdk_revision",
                    return_value=PDK_REVISIONS["sky130A"][1],
                ),
                patch(
                    "pi_lane.controller.pdk_integrity_report",
                    return_value=valid_pdk,
                ) as pdk_report_mock,
            ):
                accepted = refresh_status(state)
            self.assertEqual(accepted["delivery_gate"], "pass")
            self.assertTrue(accepted["runtime_identity_attested"])
            self.assertTrue(accepted["runtime_environment_attestation"]["verified"])
            image_id_mock.assert_called_once_with(digest)
            image_version_mock.assert_called_once_with(digest)
            pdk_report_mock.assert_called_once_with(
                "sky130A",
                pdk_root,
                allow_bootstrap=False,
                verify_content=True,
            )

            for label, image_observed, pdk_observed, revision_observed in (
                (
                    "image digest",
                    "sha256:" + "e" * 64,
                    valid_pdk,
                    PDK_REVISIONS["sky130A"][1],
                ),
                (
                    "PDK tree",
                    digest,
                    {**valid_pdk, "tree_sha256": "e" * 64},
                    PDK_REVISIONS["sky130A"][1],
                ),
                ("PDK revision", digest, valid_pdk, "f" * 40),
            ):
                with (
                    self.subTest(label=label),
                    patch(
                        "pi_lane.controller._verify_design_snapshot", return_value=[]
                    ),
                    patch("pi_lane.controller.image_id", return_value=image_observed),
                    patch(
                        "pi_lane.controller.image_librelane_version",
                        return_value=LIBRELANE_VERSION,
                    ),
                    patch(
                        "pi_lane.controller.installed_pdk_revision",
                        return_value=revision_observed,
                    ),
                    patch(
                        "pi_lane.controller.pdk_integrity_report",
                        return_value=pdk_observed,
                    ),
                ):
                    rejected = refresh_status(state)
                self.assertEqual(rejected["delivery_gate"], "fail")
                self.assertFalse(rejected["runtime_identity_attested"])
                self.assertFalse(
                    rejected["runtime_environment_attestation"]["verified"]
                )

    def test_full_delivery_fails_if_snapshotted_configuration_changes(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            state = completed_run_state(root)
            run_dir = Path(str(state["librelane_run_dir"]))
            write_completed_step(run_dir, 80, "Misc.ReportManufacturability")
            final = run_dir / "final"
            (final / "gds").mkdir(parents=True)
            write_valid_gds(final / "gds" / "delivery_test.gds")
            (final / "metrics.json").write_text(
                json.dumps(
                    complete_metrics(
                        core_area=1000.0,
                        total_area=1000.0,
                        standard_cell_area=400.0,
                        filler_area=600.0,
                        tap_area=20.0,
                        utilization=0.4,
                    )
                )
            )
            snapshot_root = Path(str(state["input_snapshot_dir"]))
            snapshot_config = snapshot_root / "config.yaml"
            snapshot_root.chmod(0o700)
            snapshot_config.chmod(0o600)
            snapshot_config.write_text(
                "DESIGN_NAME: delivery_test\nPDK: sky130A\nCLOCK_PERIOD: 99\n"
            )
            with patch("pi_lane.controller.save_state"):
                refreshed = refresh_status(state)
            self.assertEqual(refreshed["delivery_gate"], "fail")
            self.assertIn(
                "snapshotted design input changed",
                "\n".join(refreshed["delivery_gate_reasons"]),
            )

    def test_full_delivery_with_unknown_qor_is_incomplete(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            state = completed_run_state(root)
            run_dir = Path(str(state["librelane_run_dir"]))
            write_completed_step(run_dir, 76, "Misc.ReportManufacturability")
            final = run_dir / "final"
            (final / "gds").mkdir(parents=True)
            write_valid_gds(final / "gds" / "delivery_test.gds")
            (final / "metrics.json").write_text(
                json.dumps({"route__drc_errors": 0}), encoding="utf-8"
            )
            with patch("pi_lane.controller.save_state"):
                refreshed = refresh_status(state)

        self.assertEqual(refreshed["process_status"], "completed")
        self.assertEqual(refreshed["qor"]["gate"], "unknown")
        self.assertEqual(refreshed["delivery_gate"], "incomplete")
        self.assertEqual(refreshed["status"], "incomplete")
        self.assertTrue(
            any(
                "QoR gate is unknown" in reason
                for reason in refreshed["delivery_gate_reasons"]
            )
        )

    def test_full_delivery_rejects_missing_endpoint_and_empty_gds(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            state = completed_run_state(root)
            run_dir = Path(str(state["librelane_run_dir"]))
            write_completed_step(run_dir, 75, "Checker.HoldViolations")
            final = run_dir / "final"
            (final / "gds").mkdir(parents=True)
            (final / "gds" / "delivery_test.gds").write_bytes(b"")
            (final / "metrics.json").write_text(
                json.dumps(
                    complete_metrics(
                        core_area=1000.0,
                        total_area=1000.0,
                        standard_cell_area=400.0,
                        filler_area=600.0,
                        tap_area=20.0,
                        utilization=0.4,
                    )
                ),
                encoding="utf-8",
            )
            with patch("pi_lane.controller.save_state"):
                refreshed = refresh_status(state)

        self.assertEqual(refreshed["delivery_gate"], "incomplete")
        self.assertEqual(refreshed["status"], "incomplete")
        self.assertFalse(refreshed["endpoint_reached"])
        self.assertEqual(refreshed["canonical_gds"], [])
        reasons = "\n".join(refreshed["delivery_gate_reasons"])
        self.assertIn("expected final Step", reasons)
        self.assertIn("canonical GDS", reasons)

    def test_partial_to_is_not_a_full_delivery(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            state = completed_run_state(root, to="OpenROAD.GlobalRouting")
            run_dir = Path(str(state["librelane_run_dir"]))
            write_completed_step(run_dir, 39, "OpenROAD.GlobalRouting")
            with patch("pi_lane.controller.save_state"):
                refreshed = refresh_status(state)

        self.assertEqual(refreshed["process_status"], "completed")
        self.assertEqual(refreshed["flow_scope"], "partial")
        self.assertEqual(refreshed["delivery_gate"], "not_applicable")
        self.assertEqual(refreshed["status"], "completed")
        self.assertIn("partial", refreshed["status_detail"])

    def test_partial_terminal_checkpoint_retains_latest_state(self):
        checkpoint = "/design/runs/PI_LANE_partial/12-openroad-staprepnr/state_out.json"
        projected = {
            "status": "completed",
            "flow_scope": "partial",
            "artifacts": {
                "latest_state": checkpoint,
                "visualizations": {"latest": None},
            },
        }
        durable = _terminal_durable_projection({}, projected)
        self.assertEqual(durable["artifacts"]["latest_state"], checkpoint)

        reconciled = _terminal_auxiliary_projection(
            durable,
            {
                "artifacts": {"visualizations": {"latest": "new.png"}},
                "visualization": {"status": "completed"},
            },
        )
        self.assertEqual(reconciled["artifacts"]["latest_state"], checkpoint)
        self.assertEqual(reconciled["artifacts"]["visualizations"]["latest"], "new.png")

    def test_design_snapshot_can_create_parent_probe_mountpoint(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            snapshot = root / "snapshot"
            content = b"DESIGN_NAME: demo\nPDK: sky130A\n"
            _materialize_design_snapshot(
                snapshot,
                manifest={
                    "files": [
                        {
                            "path": "config.yaml",
                            "sha256": hashlib.sha256(content).hexdigest(),
                        }
                    ]
                },
                captured={"config.yaml": content},
                run_tag="PI_LANE_probe",
                extra_run_tags=("PI_LANE_baseline",),
            )
            self.assertTrue((snapshot / "runs" / "PI_LANE_baseline").is_dir())
            self.assertTrue((snapshot / "runs" / "PI_LANE_probe").is_dir())

    def test_validated_resume_chain_can_complete_a_full_delivery(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            state = completed_run_state(root)
            resume_snapshot = root / "resume-input.state"
            resume_snapshot.write_text('{"def":"parent.def"}\n', encoding="utf-8")
            state.update(
                {
                    "from": "OpenROAD.GlobalRouting",
                    "initial_state": str(root / "parent" / "state_out.json"),
                    "initial_state_sha256": file_sha256(resume_snapshot),
                    "initial_state_snapshot": str(resume_snapshot),
                    "resume": {
                        "validated": True,
                        "chain_covers_from_start": True,
                        "root_run_id": "root-run",
                        "chain_depth": 1,
                        "input_bundle_sha256": state["input_bundle_sha256"],
                    },
                }
            )
            run_dir = Path(str(state["librelane_run_dir"]))
            write_completed_step(run_dir, 80, "Misc.ReportManufacturability")
            final = run_dir / "final"
            (final / "gds").mkdir(parents=True)
            write_valid_gds(final / "gds" / "delivery_test.gds")
            (final / "metrics.json").write_text(
                json.dumps(
                    complete_metrics(
                        core_area=1000.0,
                        total_area=1000.0,
                        standard_cell_area=400.0,
                        filler_area=600.0,
                        tap_area=20.0,
                        utilization=0.4,
                    )
                ),
                encoding="utf-8",
            )
            with (
                patch("pi_lane.controller.save_state"),
                patch(
                    "pi_lane.controller.verify_resume_chain",
                    return_value={
                        "root_run_id": "root-run",
                        "chain_depth": 1,
                        "chain_proof_sha256": "e" * 64,
                    },
                ),
            ):
                refreshed = refresh_status(state)
        self.assertEqual(refreshed["flow_scope"], "full")
        self.assertTrue(refreshed["resumed_delivery"])
        self.assertEqual(refreshed["delivery_gate"], "pass")
        self.assertEqual(refreshed["status"], "completed")

    def test_requested_mapped_equivalence_is_part_of_delivery_gate(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            state = completed_run_state(root)
            state["run_eqy_requested"] = True
            script = root / "eqy" / "delivery_test.eqy"
            script.parent.mkdir()
            script.write_text(_expected_eqy_script("delivery_test"), encoding="utf-8")
            gate_tcl = script.with_name("delivery_test-gate.tcl")
            gate_tcl.write_text(_expected_eqy_gate_tcl(), encoding="utf-8")
            script_sha256 = file_sha256(script)
            config_path = Path(str(state["config_path"]))
            config_path.write_text(
                "meta:\n"
                "  flow: Classic\n"
                "DESIGN_NAME: delivery_test\n"
                "PDK: sky130A\n"
                "RUN_EQY: true\n"
                "EQY_SCRIPT: dir::eqy/delivery_test.eqy\n",
                encoding="utf-8",
            )
            state["config_sha256"] = file_sha256(config_path)
            state["eqy_script_sha256"] = script_sha256
            state["eqy_gate_tcl_sha256"] = file_sha256(gate_tcl)
            recapture_run_inputs(state)
            run_dir = Path(str(state["librelane_run_dir"]))
            eqy = run_dir / "75-yosys-eqy"
            eqy.mkdir(parents=True)
            (eqy / "config.json").write_text(
                json.dumps(
                    {
                        "meta": {"step": "Yosys.EQY"},
                        "PDK": "sky130A",
                        "EQY_SCRIPT": str(script),
                    }
                ),
                encoding="utf-8",
            )
            (eqy / "state_in.json").write_text(
                json.dumps({"netlist": "delivery_test.v"}), encoding="utf-8"
            )
            (eqy / "state_out.json").write_text(
                json.dumps({"netlist": "delivery_test.nl.v"}), encoding="utf-8"
            )
            (eqy / "eqy.log").write_text(
                "Successfully proved designs equivalent\nDONE (PASS, rc=0)\n",
                encoding="utf-8",
            )
            (eqy / "scratch").mkdir()
            (eqy / "scratch" / "PASS").write_text(
                "Elapsed clock time: 0:00:01\n", encoding="utf-8"
            )
            write_completed_step(run_dir, 80, "Misc.ReportManufacturability")
            final = run_dir / "final"
            (final / "gds").mkdir(parents=True)
            write_valid_gds(final / "gds" / "delivery_test.gds")
            (final / "metrics.json").write_text(
                json.dumps(
                    complete_metrics(
                        core_area=1000.0,
                        total_area=1000.0,
                        standard_cell_area=400.0,
                        filler_area=600.0,
                        tap_area=20.0,
                        utilization=0.4,
                    )
                ),
                encoding="utf-8",
            )
            with patch("pi_lane.controller.save_state"):
                refreshed = refresh_status(state)
        self.assertEqual(refreshed["mapped_equivalence"]["gate"], "pass")
        self.assertEqual(refreshed["delivery_gate"], "pass")
        self.assertFalse(refreshed["assurance"]["foundry_signoff"])
        self.assertFalse(refreshed["assurance"]["tapeout_guarantee"])

    def test_exit_zero_with_failed_qor_becomes_failed_state(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            state = completed_run_state(root)
            state["design_name"] = "sparse_design"
            run_dir = Path(str(state["librelane_run_dir"]))
            final_dir = run_dir / "final"
            final_dir.mkdir(parents=True)
            (final_dir / "metrics.json").write_text(
                json.dumps(
                    complete_metrics(
                        core_area=66333.6,
                        total_area=66333.6,
                        standard_cell_area=3462.07,
                        filler_area=62871.5,
                        tap_area=1201.15,
                        utilization=0.0521918,
                    )
                )
            )
            with patch("pi_lane.controller.save_state"):
                refreshed = refresh_status(state)
        self.assertEqual(refreshed["exit_code"], 0)
        self.assertEqual(refreshed["process_status"], "completed")
        self.assertEqual(refreshed["status"], "failed")
        self.assertEqual(refreshed["failure_kind"], "qor")
        self.assertEqual(refreshed["coarse_phase"], "qor_gate")
        self.assertEqual(refreshed["qor"]["gate"], "fail")
        self.assertTrue(
            any(error.startswith("[QoR ERROR]") for error in refreshed["errors"])
        )

    def test_state_ids_and_shared_pdk_root_are_rejected(self):
        with self.assertRaises(RuntimeError):
            resolve_run_id("../../other-state")
        with self.assertRaises(ValueError):
            validate_pdk_root(Path.home() / ".ciel")
        with self.assertRaises(ValueError):
            validate_image_name("--privileged")
        self.assertEqual(
            validate_image_name("ghcr.io/librelane/librelane:3.0.5"),
            "ghcr.io/librelane/librelane:3.0.5",
        )

    def test_timestamped_rich_error_is_preserved_in_failed_lane_state(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            state = completed_run_state(root)
            Path(str(state["exit_file"])).write_text("1\n")
            Path(str(state["build_log"])).write_text(
                "\x1b[31m[12:34:56] ERROR [PDN-0185] grid construction failed\x1b[0m\n"
            )
            state["status"] = "running"
            refreshed = refresh_status(state)
        self.assertEqual(refreshed["process_status"], "failed")
        self.assertIn(
            "[12:34:56] ERROR [PDN-0185] grid construction failed",
            refreshed["errors"],
        )
        for image in (
            "ghcr.io/librelane/librelane:latest",
            "ghcr.io/librelane/librelane:3.0.4",
            "docker.io/example/librelane:3.0.5",
        ):
            with self.subTest(image=image), self.assertRaises(ValueError):
                validate_image_name(image)

    def test_malformed_and_symlinked_gds_and_metrics_are_not_delivery_evidence(self):
        with (
            tempfile.TemporaryDirectory() as temp,
            tempfile.TemporaryDirectory() as other,
        ):
            run = Path(temp) / "run"
            final = run / "final"
            gds_dir = final / "gds"
            gds_dir.mkdir(parents=True)
            (gds_dir / "truncated.gds").write_bytes(
                b"\x00\x06\x00\x02\x00\x03" + b"\x00\x04\x04\x00"
            )
            outside_gds = Path(other) / "outside.gds"
            write_valid_gds(outside_gds)
            (gds_dir / "linked.gds").symlink_to(outside_gds)
            outside_metrics = Path(other) / "metrics.json"
            outside_metrics.write_text(
                json.dumps(
                    complete_metrics(
                        core_area=1000.0,
                        total_area=1000.0,
                        standard_cell_area=400.0,
                        filler_area=600.0,
                        tap_area=20.0,
                        utilization=0.4,
                    )
                )
            )
            (final / "metrics.json").symlink_to(outside_metrics)
            artifacts = discover_artifacts(run, [], {})
            self.assertEqual(artifacts["canonical_gds"], [])
            self.assertIsNone(artifacts["metrics"])

    def test_dead_visualizer_with_running_manifest_is_failed(self):
        with tempfile.TemporaryDirectory() as temp:
            run = Path(temp) / "run"
            visual = run / "visualizations"
            visual.mkdir(parents=True)
            (visual / "manifest.json").write_text(
                json.dumps({"schema_version": 3, "status": "running"})
            )
            state = {
                "librelane_run_dir": str(run),
                "visualization_dir": str(visual),
                "visualizer_pid": 12345,
            }
            with patch("pi_lane.controller._visualizer_pid_alive", return_value=False):
                result = discover_visualization(state)
            self.assertEqual(result["status"], "failed")
            self.assertTrue(
                any("terminal manifest" in item for item in result["errors"])
            )

    def test_visualization_manifest_schema_two_is_explicitly_rejected(self):
        with tempfile.TemporaryDirectory() as temp:
            run = Path(temp) / "run"
            visual = run / "visualizations"
            visual.mkdir(parents=True)
            (visual / "manifest.json").write_text(
                json.dumps({"schema_version": 2, "status": "completed"}),
                encoding="utf-8",
            )
            state = {
                "librelane_run_dir": str(run),
                "visualization_dir": str(visual),
                "visualizer_pid": None,
                "visualizer_launch_attempted": True,
            }

            result = discover_visualization(state)

        self.assertEqual(result["status"], "failed")
        self.assertIn("schema is unsupported", "\n".join(result["errors"]))

    def test_unattempted_visualizer_is_starting_until_launch_registration(self):
        with tempfile.TemporaryDirectory() as temp:
            run = Path(temp) / "run"
            run.mkdir()
            state = {
                "run_id": "run",
                "librelane_run_dir": str(run),
                "status": "running",
                "visualizer_pid": None,
                "visualizer_pid_start_time": None,
                "visualizer_launch_attempted": False,
            }
            pending = discover_visualization(state)
            self.assertEqual(pending["status"], "starting")
            self.assertEqual(pending["errors"], [])
            state["visualizer_launch_attempted"] = True
            failed = discover_visualization(state)
            self.assertEqual(failed["status"], "failed")
            self.assertTrue(
                any("terminal manifest" in item for item in failed["errors"])
            )

    def test_malformed_visualization_counter_fails_closed(self):
        with tempfile.TemporaryDirectory() as temp:
            run = Path(temp) / "run"
            visual = run / "visualizations"
            visual.mkdir(parents=True)
            state = {
                "schema_version": 6,
                "run_id": "counter-test",
                "librelane_run_dir": str(run),
                "design_dir": str(Path(temp)),
                "design_name": "demo",
                "config_path": str(Path(temp) / "config.yaml"),
                "config_sha256": "a" * 64,
                "input_snapshot_dir": str(Path(temp) / "input-snapshot"),
                "input_bundle_sha256": "e" * 64,
                "pdk": "sky130A",
                "pdk_revision": "b" * 40,
                "pdk_integrity_sha256": "c" * 64,
                "pdk_root": str(Path(temp) / "pdk"),
                "image": LIBRELANE_IMAGE,
                "image_digest": "sha256:" + "d" * 64,
                "runtime_image_ref": "sha256:" + "d" * 64,
                "librelane_version": LIBRELANE_VERSION,
                "pid": 111,
                "pid_start_time": 222,
                "overrides": [],
                "visualization_dir": str(visual),
                "visualizer_pid": 12345,
                "visualizer_launch_attempted": True,
            }
            identity = {
                "run_id": state["run_id"],
                "run_dir": state["librelane_run_dir"],
                "design_dir": state["design_dir"],
                "design_name": state["design_name"],
                "config_path": state["config_path"],
                "config_sha256": state["config_sha256"],
                "input_snapshot_dir": state["input_snapshot_dir"],
                "input_bundle_sha256": state["input_bundle_sha256"],
                "pdk": state["pdk"],
                "pdk_revision": state["pdk_revision"],
                "pdk_integrity_sha256": state["pdk_integrity_sha256"],
                "pdk_root": state["pdk_root"],
                "image": state["image"],
                "image_digest": state["image_digest"],
                "runtime_image_ref": state["runtime_image_ref"],
                "librelane_version": state["librelane_version"],
                "main_pid": state["pid"],
                "main_pid_start_time": state["pid_start_time"],
                "overrides": [],
            }
            (visual / "manifest.json").write_text(
                json.dumps(
                    {
                        "schema_version": 3,
                        **identity,
                        "status": "completed",
                        "completed_step_count": "not-an-integer",
                        "visualization_count": 1,
                        "failed_visualization_count": 0,
                    }
                )
            )

            with patch("pi_lane.controller._visualizer_pid_alive", return_value=False):
                result = discover_visualization(state)

        self.assertEqual(result["status"], "failed")
        self.assertEqual(result["completed_step_count"], 0)
        self.assertTrue(
            any("non-negative integer" in item for item in result["errors"])
        )

    def test_install_errors_are_extracted_from_log(self):
        with tempfile.TemporaryDirectory() as temp:
            log = Path(temp) / "install.log"
            log.write_text(
                "pulling layer\nError response from daemon: denied\nretrying\n"
            )
            errors = _install_errors(log, failed=True)
        self.assertEqual(errors, ["Error response from daemon: denied"])

    def test_expected_pdk_download_message_is_not_an_error(self):
        with tempfile.TemporaryDirectory() as temp:
            log = Path(temp) / "install.log"
            log.write_text("Version abc not found locally, attempting to download\n")
            errors = _install_errors(log, failed=False)
        self.assertEqual(errors, [])


if __name__ == "__main__":
    unittest.main()
