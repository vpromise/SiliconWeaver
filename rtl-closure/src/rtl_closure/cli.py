"""CLI for independent natural-language RTL generation into LibreLane."""

from __future__ import annotations

import argparse
import importlib.util
import json
import os
import shutil
import subprocess
import sys
from contextlib import redirect_stdout
from pathlib import Path

from . import __version__
from .backend import load_backend_config, make_backends
from .pipeline import (
    DESIGN_ROOT,
    STATE_ROOT,
    build_report,
    format_report,
    format_state,
    list_states,
    load_state,
    reconcile_state,
    report_state,
    retry_lane,
    start_pipeline,
    stop_pipeline,
    tail,
    worker,
)
from .sandbox import SANDBOX_IMAGE
from .runtime import VERIFY_PYTHON
from .spec import compute_intent_binding, load_spec
from .verification import VERIFY_IMAGE


def cmd_validate(args: argparse.Namespace) -> int:
    spec = load_spec(Path(args.spec))
    print(
        json.dumps(
            {
                "valid": True,
                "design": spec.name,
                "pdk": spec.pdk,
                "ports": len(spec.ports),
                "clock": {
                    "port": spec.clock_port,
                    "period_ns": spec.clock_period_ns,
                },
                "flow": spec.flow["name"],
                "floorplan_sizing": spec.flow["floorplan_sizing"],
                "to": spec.flow["to"],
                "mapped_equivalence_mode": spec.flow["mapped_equivalence_mode"],
                "architecture": spec.architecture,
                "model_profile": spec.generation["profile"],
                "model": spec.generation.get("model"),
                "model_source": (
                    "spec" if "model" in spec.generation else "RTL_CLOSURE_MODEL"
                ),
                "reasoning_effort": spec.generation["reasoning_effort"],
                "intent_closure": spec.intent_closure,
                "verification": {
                    "max_repair_rounds": spec.verification["max_repair_rounds"],
                    "repair_candidates": spec.verification["repair_candidates"],
                    "directed_requirements": len(spec.verification["requirements"]),
                    "directed_tests": len(spec.verification["directed_tests"]),
                    "random_regression_cycles_per_seed": spec.verification[
                        "random_regression_cycles"
                    ],
                    "random_regression_seed_count": 2,
                    "oracle_coverage_target_pct": spec.verification[
                        "oracle_coverage_target_pct"
                    ],
                    "oracle_line_coverage_mode": spec.verification[
                        "oracle_line_coverage_mode"
                    ],
                    "oracle_diversity_mode": spec.verification[
                        "oracle_diversity_mode"
                    ],
                    "oracle_min_distinct_sources": spec.verification[
                        "oracle_min_distinct_sources"
                    ],
                    "adaptive_tests_per_round": spec.verification[
                        "adaptive_tests_per_round"
                    ],
                    "internal_trace_signals": spec.verification[
                        "internal_trace_signals"
                    ],
                    "formal_mode": spec.verification["formal_mode"],
                    "formal_depth": spec.verification["formal_depth"],
                    "formal_timeout_seconds": spec.verification[
                        "formal_timeout_seconds"
                    ],
                    "formal_total_timeout_seconds": spec.verification[
                        "formal_total_timeout_seconds"
                    ],
                    "universal_mode": spec.verification["universal_mode"],
                    "universal_property_count": len(
                        spec.verification["universal_properties"]
                    ),
                    "universal_induction_depth": spec.verification[
                        "universal_induction_depth"
                    ],
                },
            },
            indent=2,
        )
    )
    return 0


def cmd_intent_hash(args: argparse.Namespace) -> int:
    path = Path(args.spec)
    if path.stat().st_size > 1024 * 1024:
        raise RuntimeError("specification exceeds the 1 MiB size limit")
    raw = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(raw, dict):
        raise RuntimeError("specification root must be a JSON object")
    print(compute_intent_binding(raw))
    return 0


def cmd_start(args: argparse.Namespace) -> int:
    if args.json:
        # Keep machine-readable stdout atomic even if a lower layer adds an
        # informational print in the future.  Diagnostics belong on stderr.
        with redirect_stdout(sys.stderr):
            state = start_pipeline(
                Path(args.spec),
                generate_only=args.generate_only,
                workflow_id=getattr(args, "workflow_id", None),
                idempotency_key=getattr(args, "idempotency_key", None),
            )
            report = build_report(state)
        print(json.dumps(report, indent=2))
    else:
        state = start_pipeline(
            Path(args.spec),
            generate_only=args.generate_only,
            workflow_id=getattr(args, "workflow_id", None),
            idempotency_key=getattr(args, "idempotency_key", None),
        )
        print(format_report(build_report(state)))
    return 0


def cmd_status(args: argparse.Namespace) -> int:
    state = load_state(args.run_id)
    print(json.dumps(state, indent=2) if args.json else format_state(state))
    # Query success is distinct from the workflow verdict. Machine callers must
    # always be able to read a terminal failure snapshot without treating it as
    # a malformed/failed CLI request.
    return 0


def cmd_report(args: argparse.Namespace) -> int:
    report = report_state(
        args.run_id,
        log_lines=args.log_lines,
        compact=bool(getattr(args, "compact", False)),
    )
    print(json.dumps(report, indent=2) if args.json else format_report(report))
    return 0


def cmd_list(args: argparse.Namespace) -> int:
    states = list_states(active_only=args.active, limit=args.limit)
    if args.json:
        print(json.dumps(states, indent=2))
    else:
        for state in states:
            design = state.get("design_slug") or state.get("design_name") or "-"
            print(
                f"{state['run_id']}  {state['status']:<9}  "
                f"{state.get('pdk', '-')}/{design}  phase={state.get('phase', '-')}"
            )
    return 0


def cmd_reconcile(args: argparse.Namespace) -> int:
    state = reconcile_state(
        args.run_id, integrity_audit_id=args.integrity_audit_id
    )
    print(json.dumps(state, indent=2) if args.json else format_state(state))
    return 0


def cmd_logs(args: argparse.Namespace) -> int:
    state = load_state(args.run_id)
    print(tail(Path(state["pipeline_log"]), args.lines))
    if state.get("lane_run_id") and args.lane:
        subprocess.run(
            [
                "lane-agent",
                "logs",
                "--run-id",
                state["lane_run_id"],
                "--lines",
                str(args.lines),
            ],
            check=False,
        )
    return 0


def cmd_stop(args: argparse.Namespace) -> int:
    state = stop_pipeline(None if getattr(args, "current", False) else args.run_id)
    if getattr(args, "json", False):
        print(json.dumps(build_report(state), indent=2))
    else:
        print(format_state(state))
    return 0


def cmd_retry(args: argparse.Namespace) -> int:
    state = retry_lane(
        None if getattr(args, "current", False) else args.run_id,
        idempotency_key=getattr(args, "idempotency_key", None),
    )
    print(json.dumps(state, indent=2) if args.json else format_state(state))
    return 0


def cmd_doctor(args: argparse.Namespace) -> int:
    STATE_ROOT.mkdir(parents=True, exist_ok=True, mode=0o700)
    DESIGN_ROOT.mkdir(parents=True, exist_ok=True, mode=0o700)
    checks = {
        "rtl_closure_python": importlib.util.find_spec("rtl_closure") is not None,
        "docker": shutil.which("docker") is not None,
        "lane_agent": shutil.which("lane-agent") is not None,
        "state_dir": STATE_ROOT.is_dir(),
        "design_root": DESIGN_ROOT.is_dir(),
    }
    if checks["docker"]:
        image = subprocess.run(
            ["docker", "image", "inspect", SANDBOX_IMAGE],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            check=False,
        )
        checks["sandbox_image"] = image.returncode == 0
        verify_image = subprocess.run(
            ["docker", "image", "inspect", VERIFY_IMAGE],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            check=False,
        )
        checks["verify_image"] = verify_image.returncode == 0
        if checks["verify_image"]:
            runtime = subprocess.run(
                [
                    "docker",
                    "run",
                    "--rm",
                    "--network",
                    "none",
                    "--read-only",
                    "--entrypoint",
                    VERIFY_PYTHON,
                    VERIFY_IMAGE,
                    "-c",
                    "import cocotb; print(cocotb.__version__)",
                ],
                capture_output=True,
                text=True,
                timeout=30,
                check=False,
            )
            checks["cocotb_runtime"] = (
                runtime.returncode == 0 and runtime.stdout.strip() == "2.0.1"
            )
            tool_commands = {
                "verilator_runtime": ["verilator", "--version"],
                "yosys_formal_runtime": ["yosys", "-Q", "-p", "help sat"],
                "sby_runtime": ["sby", "--version"],
            }
            for key, command in tool_commands.items():
                probe = subprocess.run(
                    [
                        "docker",
                        "run",
                        "--rm",
                        "--network",
                        "none",
                        "--read-only",
                        "--entrypoint",
                        command[0],
                        VERIFY_IMAGE,
                        *command[1:],
                    ],
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                    timeout=30,
                    check=False,
                )
                checks[key] = probe.returncode == 0
        else:
            checks["cocotb_runtime"] = False
            checks["verilator_runtime"] = False
            checks["yosys_formal_runtime"] = False
            checks["sby_runtime"] = False
    else:
        checks["sandbox_image"] = False
        checks["verify_image"] = False
        checks["cocotb_runtime"] = False
        checks["verilator_runtime"] = False
        checks["yosys_formal_runtime"] = False
        checks["sby_runtime"] = False
    try:
        config = load_backend_config({})
        checks["model_config"] = bool(
            config["model"] and config["base_url"] and config["api_key"]
        )
        public_model = config["model"]
        public_url = config["public_base_url"]
    except Exception:
        checks["model_config"] = False
        public_model = "-"
        public_url = "-"
    if args.api and checks["model_config"]:
        v_backend, _, _ = make_backends(
            {"profile": "api", "reasoning_effort": "medium"}
        )
        response = v_backend.generate(
            "Reply with exactly: RTL_CLOSURE_OK",
            n=1,
            temperature=0,
            max_tokens=32,
        )[0]
        checks["api"] = bool(response.strip())
    for name, ok in checks.items():
        print(f"{name}: {'ok' if ok else 'missing'}")
    print(f"model: {public_model}")
    print(f"base_url: {public_url}")
    print(f"sandbox_image: {SANDBOX_IMAGE}")
    print(f"verify_image: {VERIFY_IMAGE}")
    return 0 if all(checks.values()) else 1


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    commands = parser.add_subparsers(dest="command", required=True)

    validate = commands.add_parser("validate", help="Validate a design specification")
    validate.add_argument("--spec", required=True)
    validate.set_defaults(func=cmd_validate)

    intent_hash = commands.add_parser(
        "intent-hash",
        help="Compute the digest a user must confirm for required intent closure",
    )
    intent_hash.add_argument("--spec", required=True)
    intent_hash.set_defaults(func=cmd_intent_hash)

    start = commands.add_parser("start", help="Start generation in the background")
    start.add_argument("--spec", required=True)
    start.add_argument("--generate-only", action="store_true")
    start.add_argument("--workflow-id")
    start.add_argument("--idempotency-key")
    start.add_argument("--json", action="store_true")
    start.set_defaults(func=cmd_start)

    status = commands.add_parser("status", help="Show one pipeline status snapshot")
    status.add_argument("--run-id")
    status.add_argument("--json", action="store_true")
    status.set_defaults(func=cmd_status)

    report = commands.add_parser(
        "report", help="Summarize generation, LibreLane progress, QoR, and errors"
    )
    report.add_argument("--run-id")
    report.add_argument("--log-lines", type=int, default=20)
    report.add_argument(
        "--compact", action="store_true", help="Emit a bounded monitor-oriented report"
    )
    report.add_argument("--json", action="store_true")
    report.set_defaults(func=cmd_report)

    listing = commands.add_parser("list", help="List persisted pipeline runs")
    listing.add_argument("--active", action="store_true")
    listing.add_argument("--limit", type=int, default=100)
    listing.add_argument("--json", action="store_true")
    listing.set_defaults(func=cmd_list)

    reconcile = commands.add_parser(
        "reconcile", help="Explicitly reconcile the child Lane run into parent state"
    )
    reconcile.add_argument("--run-id")
    reconcile.add_argument("--integrity-audit-id")
    reconcile.add_argument("--json", action="store_true")
    reconcile.set_defaults(func=cmd_reconcile)

    logs = commands.add_parser("logs", help="Show generation and optional Lane logs")
    logs.add_argument("--run-id")
    logs.add_argument("--lines", type=int, default=80)
    logs.add_argument("--lane", action="store_true")
    logs.set_defaults(func=cmd_logs)

    stop = commands.add_parser("stop", help="Stop generation or its Lane run")
    stop_target = stop.add_mutually_exclusive_group(required=True)
    stop_target.add_argument("--run-id")
    stop_target.add_argument("--current", action="store_true")
    stop.add_argument("--json", action="store_true")
    stop.set_defaults(func=cmd_stop)

    retry = commands.add_parser("retry-lane", help="Restart only the published Lane design")
    retry_target = retry.add_mutually_exclusive_group(required=True)
    retry_target.add_argument("--run-id")
    retry_target.add_argument("--current", action="store_true")
    retry.add_argument("--idempotency-key")
    retry.add_argument("--json", action="store_true")
    retry.set_defaults(func=cmd_retry)

    doctor = commands.add_parser("doctor", help="Check generation prerequisites")
    doctor.add_argument("--api", action="store_true")
    doctor.set_defaults(func=cmd_doctor)

    internal = commands.add_parser("_worker", help=argparse.SUPPRESS)
    internal.add_argument("--run-id", required=True)
    internal.add_argument("--launch-generation", required=True, type=int)

    def run_worker(args: argparse.Namespace) -> int:
        launch_token = os.environ.pop("RTL_CLOSURE_WORKER_LAUNCH_TOKEN", "")
        if not launch_token:
            raise RuntimeError("pipeline worker launch token is missing")
        return worker(args.run_id, args.launch_generation, launch_token)

    internal.set_defaults(func=run_worker)
    return parser


def main() -> int:
    parser = build_parser()
    args = parser.parse_args()
    try:
        return int(args.func(args))
    except KeyboardInterrupt:
        return 130
    except Exception as exc:
        print(f"rtl-closure: error: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
