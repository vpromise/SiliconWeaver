"""Command-line interface for isolated managed LibreLane runs."""

from __future__ import annotations

import argparse
import json
import shutil
import sys
from pathlib import Path

from . import __version__
from .classic import CLASSIC_REGISTRY_VERSION, CLASSIC_STEP_PHASES
from .config import OPEN_PDKS
from .controller import (
    IMAGE,
    PDK_ROOT,
    PDK_REVISIONS,
    format_status,
    image_install_status,
    image_id,
    image_librelane_version,
    list_runs,
    installed_pdk_revision,
    pdk_integrity_report,
    pdk_install_status,
    refresh_status,
    reconcile_status,
    resume_run,
    run_event_report,
    start_image_install,
    start_pdk_install,
    start_run,
    start_visualization,
    stop_pdk_install,
    stop_image_install,
    stop_run,
    tail_logs,
)
from .run_state import STATE_ROOT, load_state


def doctor_report(
    *, image: str, pdk_root: Path, target_pdk: str | None
) -> dict[str, object]:
    STATE_ROOT.mkdir(parents=True, exist_ok=True, mode=0o700)
    digest = image_id(image)
    image_version = image_librelane_version(image) if digest is not None else None
    checks = {
        "docker": shutil.which("docker") is not None,
        "image": digest is not None and image_version == "3.0.5",
        "state_dir": STATE_ROOT.is_dir(),
        "pdk_root": pdk_root.is_dir(),
    }
    installed_pdks: dict[str, bool] = {}
    pdk_revisions: dict[str, str | None] = {}
    pdk_integrity: dict[str, object] = {}
    for pdk in OPEN_PDKS:
        integrity = pdk_integrity_report(pdk, pdk_root, allow_bootstrap=True)
        installed_pdks[pdk] = bool(integrity["ready"])
        pdk_integrity[pdk] = integrity
        pdk_revisions[pdk] = installed_pdk_revision(pdk, pdk_root)
    readiness_errors: list[str] = []
    for name in ("docker", "image", "state_dir", "pdk_root"):
        if not checks[name]:
            readiness_errors.append(f"required runtime check is missing: {name}")
    if target_pdk is None:
        readiness_errors.append("target PDK is unspecified; pass --pdk")
    elif target_pdk not in OPEN_PDKS:
        readiness_errors.append(f"unsupported target PDK: {target_pdk}")
    elif not installed_pdks[target_pdk]:
        checks[f"pdk_{target_pdk}"] = False
        readiness_errors.append(
            f"target PDK {target_pdk} is missing or is not enabled at pinned "
            f"revision {PDK_REVISIONS[target_pdk][1]} under {pdk_root}"
        )
    else:
        checks[f"pdk_{target_pdk}"] = True
    return {
        "schema_version": 1,
        "ready": not readiness_errors,
        "target_pdk": target_pdk,
        "checks": checks,
        "installed_pdks": installed_pdks,
        "pdk_revisions": pdk_revisions,
        "pdk_integrity": pdk_integrity,
        "expected_pdk_revisions": {pdk: PDK_REVISIONS[pdk][1] for pdk in OPEN_PDKS},
        "image_name": image,
        "image_id": digest,
        "librelane_version": image_version,
        "pdk_root": str(pdk_root),
        "readiness_errors": readiness_errors,
    }


def cmd_doctor(args: argparse.Namespace) -> int:
    report = doctor_report(
        image=args.image,
        pdk_root=args.pdk_root,
        target_pdk=args.pdk,
    )
    if args.json:
        print(json.dumps(report, indent=2))
        return 0 if report["ready"] else 1
    checks = report["checks"]
    assert isinstance(checks, dict)
    for name, ok in checks.items():
        print(f"{name}: {'ok' if ok else 'missing'}")
    print(f"image_name: {report['image_name']}")
    print(f"image_id: {report['image_id'] or '-'}")
    print(f"pdk_root_path: {report['pdk_root']}")
    print(f"target_pdk: {report['target_pdk'] or '-'}")
    print(f"ready: {'yes' if report['ready'] else 'no'}")
    for reason in report["readiness_errors"]:
        print(f"readiness_error: {reason}")
    return 0 if report["ready"] else 1


def cmd_start(args: argparse.Namespace) -> int:
    state = start_run(
        Path(args.config),
        pdk=args.pdk,
        flow=args.flow,
        pdk_root=args.pdk_root,
        overrides=args.overrides,
        frm=args.frm,
        to=args.to,
        jobs=args.jobs,
        allow_concurrent=args.allow_concurrent,
        image=args.image,
        workflow_id=args.workflow_id,
        attempt_id=args.attempt_id,
        idempotency_key=args.idempotency_key,
        origin=args.origin,
    )
    print(json.dumps(state, indent=2) if args.json else format_status(state))
    return 0


def cmd_resume(args: argparse.Namespace) -> int:
    state = resume_run(
        args.run_id,
        frm=args.frm,
        to=args.to,
        allow_concurrent=args.allow_concurrent,
    )
    print(json.dumps(state, indent=2) if args.json else format_status(state))
    return 0


def cmd_steps(args: argparse.Namespace) -> int:
    rows = [
        {"ordinal": index, "id": step_id, "phase": phase}
        for index, (step_id, phase) in enumerate(CLASSIC_STEP_PHASES, 1)
    ]
    if args.json:
        print(
            json.dumps(
                {
                    "flow": "Classic",
                    "registry_version": CLASSIC_REGISTRY_VERSION,
                    "step_count": len(rows),
                    "steps": rows,
                },
                indent=2,
            )
        )
    else:
        print(f"Classic Step registry: {CLASSIC_REGISTRY_VERSION}")
        for row in rows:
            print(f"{row['ordinal']:02d}  {row['phase']:<10}  {row['id']}")
    return 0


def cmd_pdk_install(args: argparse.Namespace) -> int:
    state = start_pdk_install(
        args.pdk,
        pdk_root=args.pdk_root,
        image=args.image,
    )
    print(json.dumps(state, indent=2))
    return 0


def cmd_image_install(args: argparse.Namespace) -> int:
    print(json.dumps(start_image_install(args.image), indent=2))
    return 0


def cmd_image_status(args: argparse.Namespace) -> int:
    state = image_install_status(args.run_id)
    print(json.dumps(state, indent=2))
    return 1 if state["status"] == "failed" else 0


def cmd_image_stop(args: argparse.Namespace) -> int:
    print(json.dumps(stop_image_install(args.run_id), indent=2))
    return 0


def cmd_pdk_status(args: argparse.Namespace) -> int:
    state = pdk_install_status(args.run_id)
    print(json.dumps(state, indent=2))
    return 1 if state["status"] == "failed" else 0


def cmd_pdk_stop(args: argparse.Namespace) -> int:
    print(json.dumps(stop_pdk_install(args.run_id), indent=2))
    return 0


def cmd_status(args: argparse.Namespace) -> int:
    state = refresh_status(load_state(args.run_id))
    print(json.dumps(state, indent=2) if args.json else format_status(state))
    return 0


def cmd_reconcile(args: argparse.Namespace) -> int:
    state = reconcile_status(
        args.run_id, integrity_audit_id=args.integrity_audit_id
    )
    print(json.dumps(state, indent=2) if args.json else format_status(state))
    return 0


def cmd_logs(args: argparse.Namespace) -> int:
    print(tail_logs(args.run_id, args.lines))
    return 0


def cmd_events(args: argparse.Namespace) -> int:
    report = run_event_report(
        args.run_id,
        after_sequence=args.after_sequence,
        limit=args.limit,
        generation=args.generation,
        cursor_sha256=args.cursor_sha256,
    )
    print(json.dumps(report, indent=2))
    return 0


def cmd_list(args: argparse.Namespace) -> int:
    states = list_runs(active_only=args.active, limit=getattr(args, "limit", None))
    if args.json:
        print(json.dumps(states, indent=2))
    else:
        for state in states:
            print(
                f"{state['run_id']}  {state['status']:<9}  "
                f"{state['pdk']}/{state['design_name']}  "
                f"step={state.get('active_step') or state.get('last_completed_step') or '-'}"
            )
    return 0


def cmd_artifacts(args: argparse.Namespace) -> int:
    state = refresh_status(load_state(args.run_id))
    print(json.dumps(state.get("artifacts", {}), indent=2))
    return 0


def cmd_visualize(args: argparse.Namespace) -> int:
    state = start_visualization(args.run_id, retry_failed=args.retry_failed)
    print(json.dumps(state.get("visualization", {}), indent=2))
    return 0


def cmd_stop(args: argparse.Namespace) -> int:
    state = stop_run(args.run_id)
    print(json.dumps(state, indent=2) if args.json else format_status(state))
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    commands = parser.add_subparsers(dest="command", required=True)

    doctor = commands.add_parser("doctor", help="Check the isolated LibreLane runtime")
    doctor.add_argument("--image", default=IMAGE)
    doctor.add_argument("--pdk-root", type=Path, default=PDK_ROOT)
    doctor.add_argument("--pdk", choices=tuple(OPEN_PDKS))
    doctor.add_argument("--json", action="store_true")
    doctor.set_defaults(func=cmd_doctor)

    image_install = commands.add_parser(
        "image-install", help="Pull the LibreLane image in the background"
    )
    image_install.add_argument("--image", default=IMAGE)
    image_install.set_defaults(func=cmd_image_install)

    image_status = commands.add_parser(
        "image-status", help="Show one managed image installation snapshot"
    )
    image_status.add_argument("--run-id")
    image_status.set_defaults(func=cmd_image_status)

    image_stop = commands.add_parser(
        "image-stop", help="Stop a managed image installation"
    )
    image_stop.add_argument("--run-id")
    image_stop.set_defaults(func=cmd_image_stop)

    pdk_install = commands.add_parser(
        "pdk-install", help="Install a pinned open PDK in the background"
    )
    pdk_install.add_argument("--pdk", required=True, choices=tuple(OPEN_PDKS))
    pdk_install.add_argument("--pdk-root", type=Path, default=PDK_ROOT)
    pdk_install.add_argument("--image", default=IMAGE)
    pdk_install.set_defaults(func=cmd_pdk_install)

    pdk_status = commands.add_parser(
        "pdk-status", help="Show one managed PDK installation snapshot"
    )
    pdk_status.add_argument("--run-id")
    pdk_status.set_defaults(func=cmd_pdk_status)

    pdk_stop = commands.add_parser("pdk-stop", help="Stop a managed PDK installation")
    pdk_stop.add_argument("--run-id")
    pdk_stop.set_defaults(func=cmd_pdk_stop)

    start = commands.add_parser("start", help="Start LibreLane in the background")
    start.add_argument("--config", required=True)
    start.add_argument("--pdk", choices=tuple(OPEN_PDKS))
    start.add_argument("--flow")
    start.add_argument("--pdk-root", type=Path, default=PDK_ROOT)
    start.add_argument("--image", default=IMAGE)
    start.add_argument("--set", dest="overrides", action="append", default=[])
    start.add_argument("--from", dest="frm")
    start.add_argument("--to")
    start.add_argument("--jobs", type=int)
    start.add_argument("--allow-concurrent", action="store_true")
    start.add_argument("--workflow-id")
    start.add_argument("--attempt-id")
    start.add_argument("--idempotency-key")
    start.add_argument("--origin", default="lane-agent")
    start.add_argument("--json", action="store_true")
    start.set_defaults(func=cmd_start)

    resume = commands.add_parser(
        "resume", help="Create a new Classic run from a validated prior State"
    )
    resume.add_argument("--run-id", required=True)
    resume.add_argument("--from", dest="frm")
    resume.add_argument("--to")
    resume.add_argument("--allow-concurrent", action="store_true")
    resume.add_argument("--json", action="store_true")
    resume.set_defaults(func=cmd_resume)

    steps = commands.add_parser(
        "steps", help="List exact Step IDs for the pinned Classic registry"
    )
    steps.add_argument("--json", action="store_true")
    steps.set_defaults(func=cmd_steps)

    status = commands.add_parser("status", help="Show one current status snapshot")
    status.add_argument("--run-id")
    status.add_argument("--json", action="store_true")
    status.set_defaults(func=cmd_status)

    reconcile = commands.add_parser(
        "reconcile", help="Persist current process/Step evidence and transition events"
    )
    reconcile.add_argument("--run-id")
    reconcile.add_argument("--integrity-audit-id")
    reconcile.add_argument("--json", action="store_true")
    reconcile.set_defaults(func=cmd_reconcile)

    logs = commands.add_parser("logs", help="Show the active Step or build log tail")
    logs.add_argument("--run-id")
    logs.add_argument("--lines", type=int, default=80)
    logs.set_defaults(func=cmd_logs)

    events = commands.add_parser(
        "events", help="Replay durable LibreLane Step events and the current snapshot"
    )
    events.add_argument("--run-id")
    events.add_argument("--after-sequence", type=int, default=0)
    events.add_argument("--limit", type=int, default=256)
    events.add_argument("--generation")
    events.add_argument("--cursor-sha256")
    events.set_defaults(func=cmd_events)

    list_command = commands.add_parser("list", help="List isolated LibreLane runs")
    list_command.add_argument(
        "--active",
        action="store_true",
        help="show only non-terminal managed runs",
    )
    list_command.add_argument("--limit", type=int)
    list_command.add_argument("--json", action="store_true")
    list_command.set_defaults(func=cmd_list)

    artifacts = commands.add_parser("artifacts", help="Show generated LibreLane artifacts")
    artifacts.add_argument("--run-id")
    artifacts.set_defaults(func=cmd_artifacts)

    visualize = commands.add_parser(
        "visualize", help="Start or backfill notebook-style Step visualizations"
    )
    visualize.add_argument("--run-id")
    visualize.add_argument(
        "--retry-failed",
        action="store_true",
        help="retry failed or missing previews from a completed visualization run",
    )
    visualize.set_defaults(func=cmd_visualize)

    stop = commands.add_parser("stop", help="Stop a managed LibreLane run")
    stop.add_argument("--run-id")
    stop.add_argument("--json", action="store_true")
    stop.set_defaults(func=cmd_stop)
    return parser


def main() -> int:
    parser = build_parser()
    args = parser.parse_args()
    try:
        return int(args.func(args))
    except KeyboardInterrupt:
        return 130
    except Exception as exc:
        print(f"lane-agent: error: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
