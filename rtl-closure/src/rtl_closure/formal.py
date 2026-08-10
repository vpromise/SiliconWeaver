"""Deterministic Verilator and bounded-formal verification providers."""

from __future__ import annotations

import json
import hashlib
import os
import re
import shutil
import subprocess
import time
from math import ceil
from pathlib import Path
from typing import Any

from .adaptive import behavioral_coverage, suite_hash
from .contract import content_hash
from .sandbox import _docker_base
from .spec import DesignSpec
from .runtime import VERIFY_IMAGE as VERIFY_IMAGE, verify_image_ref
from .verification import _run_container


# A Yosys process handles at most this many obligations.  Resource-pressure
# exits are retried by splitting the affected range down to the minimum size;
# the obligation manifest itself is never reduced.
FORMAL_BATCH_SIZE = 32
FORMAL_MIN_BATCH_SIZE = 8
ADAPTIVE_PROPERTY_RESERVE = 64
FORMAL_SUCCESS_PATTERN = re.compile(
    r"SAT proof finished\s*-\s*no model found:\s*SUCCESS!", re.I
)
VERILATOR_WARNING_PATTERN = re.compile(r"%Warning-([A-Z0-9_]+):")
# These warnings indicate a likely semantic or structural defect in generated
# RTL. Informational style/unused warnings remain visible but do not block it.
VERILATOR_BLOCKING_WARNINGS = frozenset(
    {
        "LATCH",
        "MULTIDRIVEN",
        "PINMISSING",
        "SELRANGE",
        "UNDRIVEN",
        "UNOPTFLAT",
        "WIDTH",
        "WIDTHCONCAT",
        "WIDTHEXPAND",
        "WIDTHTRUNC",
    }
)


def _ports(spec: DesignSpec, direction: str) -> list[Any]:
    return [port for port in spec.ports if port.direction == direction]


def _yosys_bitvector(value: Any, width: int) -> str:
    """Render a fixed-width RTLIL constant without 32-bit sign extension."""
    if width < 1:
        raise ValueError(f"Yosys bit-vector width must be positive, got {width}")
    normalized = int(value) & ((1 << width) - 1)
    return f"{width}'b{normalized:0{width}b}"


def run_verilator_lint(
    spec: DesignSpec,
    rtl: str,
    artifact_dir: Path,
    *,
    timeout: int = 120,
) -> dict[str, Any]:
    artifact_dir.mkdir(parents=True, exist_ok=True)
    work = artifact_dir / "verilator"
    if work.exists():
        shutil.rmtree(work)
    work.mkdir()
    (work / "dut.sv").write_text(rtl + "\n", encoding="utf-8")
    command = _docker_base(work, read_only_mount=True) + [
        "--entrypoint",
        "verilator",
        verify_image_ref(),
        "--lint-only",
        "--timing",
        "--Wall",
        "--Wno-fatal",
        "--top-module",
        spec.name,
        "dut.sv",
    ]
    try:
        completed = _run_container(command, timeout, purpose="verilator-lint")
    except subprocess.TimeoutExpired:
        report = {
            "status": "unknown",
            "provider": "verilator_lint",
            "authoritative": True,
            "detail": f"Verilator lint exceeded {timeout} seconds",
            "quality_gate": {"status": "unknown", "blocking_warning_codes": []},
        }
    except OSError as exc:
        report = {
            "status": "error",
            "provider": "verilator_lint",
            "authoritative": True,
            "detail": str(exc),
            "quality_gate": {"status": "error", "blocking_warning_codes": []},
        }
    else:
        log = (completed.stdout + "\n" + completed.stderr).strip()
        warning_counts: dict[str, int] = {}
        for code in VERILATOR_WARNING_PATTERN.findall(log):
            warning_counts[code] = warning_counts.get(code, 0) + 1
        blocking_warning_codes = sorted(
            code for code in warning_counts if code in VERILATOR_BLOCKING_WARNINGS
        )
        quality_status = "fail" if blocking_warning_codes else "pass"
        report = {
            "status": (
                "pass"
                if completed.returncode == 0 and quality_status == "pass"
                else "fail"
            ),
            "provider": "verilator_lint",
            "authoritative": True,
            "returncode": completed.returncode,
            "detail": log[-4000:],
            "warning_counts": warning_counts,
            "quality_gate": {
                "status": quality_status,
                "blocking_warning_codes": blocking_warning_codes,
                "policy": "generated_rtl_semantic_and_structural_warnings_v1",
            },
        }
        (artifact_dir / "verilator-lint.log").write_text(
            log + "\n", encoding="utf-8"
        )
    report["rtl_sha256"] = content_hash(rtl)
    (artifact_dir / "verilator-lint.json").write_text(
        json.dumps(report, indent=2, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )
    return report


def _scenario_steps(
    spec: DesignSpec,
    plan: dict[str, Any],
    expected_trace: dict[str, Any],
    scenario_index: int,
) -> list[dict[str, Any]]:
    scenario = plan["scenarios"][scenario_index]
    rows = [
        row
        for row in expected_trace.get("trace", [])
        if int(row.get("scenario_index", -1)) == scenario_index
    ]
    setup = next(
        (
            row
            for row in expected_trace.get("setup_observations", [])
            if int(row.get("scenario_index", -1)) == scenario_index
        ),
        None,
    )
    steps: list[dict[str, Any]] = []
    if scenario.get("reset_before", True) and spec.reset and setup:
        state = dict(setup.get("inputs", {}))
        asserted = 0 if spec.reset["active_low"] else 1
        state[spec.reset["port"]] = asserted
        for reset_index, expected in enumerate(setup.get("asserted_edges", [])):
            steps.append(
                {
                    "cycle": -len(setup["asserted_edges"]) + reset_index,
                    "phase": "reset_asserted",
                    "inputs": dict(state),
                    "expected": expected,
                }
            )
        state[spec.reset["port"]] = 1 - asserted
        steps.append(
            {
                "cycle": 0,
                "phase": "reset_deasserted",
                "inputs": dict(state),
                "expected": setup.get("deasserted_edge", {}),
            }
        )
    for row in rows:
        steps.append(
            {
                "cycle": row.get("cycle"),
                "phase": "post_rising_edge",
                "inputs": dict(row.get("inputs", {})),
                "expected": dict(row.get("expected", {})),
            }
        )
    return steps


def _is_adaptive_scenario(scenario: dict[str, Any]) -> bool:
    source = str(scenario.get("source") or "").strip().lower()
    return source in {"counterexample_mutation", "adaptive"} or source.startswith(
        "adaptive_"
    )


def _scenario_property_groups(
    spec: DesignSpec,
    plan: dict[str, Any],
    expected_trace: dict[str, Any],
    depth: int,
) -> tuple[list[dict[str, Any]], int, list[dict[str, Any]]]:
    """Build O(trace-size) scenario indexes without copying property prefixes."""
    output_names = [port.name for port in _ports(spec, "output")]
    trace_by_scenario: dict[int, list[dict[str, Any]]] = {}
    for row in expected_trace.get("trace", []):
        scenario_index = int(row.get("scenario_index", -1))
        trace_by_scenario.setdefault(scenario_index, []).append(row)
    setup_by_scenario = {
        int(row.get("scenario_index", -1)): row
        for row in expected_trace.get("setup_observations", [])
    }

    groups: list[dict[str, Any]] = []
    unsupported: list[dict[str, Any]] = []
    eligible_count = 0
    for scenario_index, scenario in enumerate(plan.get("scenarios", [])):
        indexed_trace = {
            # Do not normalize more cycle rows than can participate at the
            # configured formal depth. Reset setup steps may make the final
            # slice shorter, but never longer.
            "trace": trace_by_scenario.get(scenario_index, [])[:depth],
            "setup_observations": (
                [setup_by_scenario[scenario_index]]
                if scenario_index in setup_by_scenario
                else []
            ),
        }
        # _scenario_steps sees only this scenario's indexed rows, avoiding the
        # previous O(scenarios * trace-size) scan. The formal depth bounds the
        # additional step objects retained for each scenario.
        steps = _scenario_steps(spec, plan, indexed_trace, scenario_index)[:depth]
        count = sum(
            1
            for step in steps
            for signal in output_names
            if signal in step["expected"]
        )
        reason_code: str | None = None
        reason: str | None = None
        if not spec.reset:
            reason_code = "resetless_initial_state_unconstrained"
            reason = (
                "bounded trace replay cannot bind a resetless RTL's arbitrary "
                "initial sequential state to the software oracle"
            )
        elif not scenario.get("reset_before", True):
            reason_code = "continuous_state_not_isolatable"
            reason = (
                "scenario depends on state established by an earlier frozen suite "
                "member and cannot be isolated without changing its semantics"
            )
        elif not count:
            reason_code = "no_bounded_output_obligations"
            reason = "scenario produced no bounded output checkpoint obligations"
        if reason_code is not None:
            unsupported.append(
                {
                    "scenario_index": scenario_index,
                    "scenario_id": scenario.get("id"),
                    "requirement_ids": [
                        str(value)
                        for value in scenario.get("requirement_ids", [])
                        if isinstance(value, str)
                    ],
                    "reason_code": reason_code,
                    "reason": reason,
                    "excluded_obligation_count": count,
                }
            )
            continue
        eligible_count += count
        groups.append(
            {
                "scenario_index": scenario_index,
                "scenario": scenario,
                "steps": steps,
                "output_names": output_names,
                "eligible_count": count,
                "adaptive": _is_adaptive_scenario(scenario),
            }
        )
    return groups, eligible_count, unsupported


def _group_descriptors(group: dict[str, Any]):
    for step_index, step in enumerate(group["steps"]):
        for signal in group["output_names"]:
            if signal in step["expected"]:
                yield step_index, signal


def _round_robin_scenarios(
    groups: list[dict[str, Any]],
    limit: int,
    *,
    newest_first: bool,
) -> list[tuple[dict[str, Any], int, str]]:
    """Select checkpoints across scenarios instead of draining one long trace."""
    if limit <= 0 or not groups:
        return []
    ordered = sorted(
        groups,
        key=lambda group: int(group["scenario_index"]),
        reverse=newest_first,
    )
    iterators = [(group, iter(_group_descriptors(group))) for group in ordered]
    selected: list[tuple[dict[str, Any], int, str]] = []
    while len(selected) < limit:
        advanced = False
        for group, descriptors in iterators:
            try:
                step_index, signal = next(descriptors)
            except StopIteration:
                continue
            selected.append((group, step_index, signal))
            advanced = True
            if len(selected) >= limit:
                break
        if not advanced:
            break
    return selected


def _hydrate_property(
    group: dict[str, Any], step_index: int, signal: str
) -> dict[str, Any]:
    scenario_index = int(group["scenario_index"])
    scenario = group["scenario"]
    steps = group["steps"]
    step = steps[step_index]
    # Yosys SAT exposes a register's value at timestep N before applying that
    # timestep's D input. Add one observation timestep so a sampled edge at N
    # is checked against Q at N+1. Only selected properties receive this prefix.
    input_prefix = [
        {"inputs": dict(prefix_step["inputs"])}
        for prefix_step in steps[: step_index + 1]
    ]
    input_prefix.append({"inputs": dict(step["inputs"])})
    return {
        "property_id": f"TRACE_{scenario_index:03d}_{step_index + 1:03d}_{signal}",
        "scenario_index": scenario_index,
        "scenario_id": scenario.get("id"),
        "requirement_ids": scenario.get("requirement_ids", []),
        "scenario_source": scenario.get("source"),
        "adaptive": bool(group["adaptive"]),
        "cycle": step.get("cycle"),
        "phase": step.get("phase"),
        "signal": signal,
        "expected": int(step["expected"][signal]),
        "steps": input_prefix,
    }


def _canonical_obligation_payload(
    spec: DesignSpec, prop: dict[str, Any]
) -> dict[str, Any]:
    """Return only the semantics consumed by the generated Yosys command."""
    input_names = {
        port.name
        for port in _ports(spec, "input")
        if port.name != spec.clock_port
    }
    return {
        "signal": prop["signal"],
        "expected": int(prop["expected"]),
        "steps": [
            {
                "inputs": {
                    name: int(value)
                    for name, value in sorted(step["inputs"].items())
                    if name in input_names
                }
            }
            for step in prop["steps"]
        ],
    }


def _property_provenance(prop: dict[str, Any]) -> dict[str, Any]:
    return {
        "alias": prop["property_id"],
        "scenario_index": prop["scenario_index"],
        "scenario_id": prop.get("scenario_id"),
        "requirement_ids": list(prop.get("requirement_ids", [])),
        "scenario_source": prop.get("scenario_source"),
        "adaptive": bool(prop.get("adaptive")),
        "cycle": prop.get("cycle"),
        "phase": prop.get("phase"),
    }


def _deduplicate_properties(
    spec: DesignSpec, properties: list[dict[str, Any]]
) -> list[dict[str, Any]]:
    """Collapse semantically equivalent SAT obligations without losing origins."""
    by_hash: dict[str, dict[str, Any]] = {}
    unique: list[dict[str, Any]] = []
    for prop in properties:
        payload = _canonical_obligation_payload(spec, prop)
        digest = content_hash(payload)
        provenance = _property_provenance(prop)
        existing = by_hash.get(digest)
        if existing is None:
            canonical = dict(prop)
            canonical.update(
                {
                    "property_id": f"TRACE_EQ_{digest[:24].upper()}",
                    "canonical_sha256": digest,
                    "aliases": [prop["property_id"]],
                    "provenance": [provenance],
                    "steps": payload["steps"],
                }
            )
            by_hash[digest] = canonical
            unique.append(canonical)
            continue
        if _canonical_obligation_payload(spec, existing) != payload:
            raise ValueError("formal obligation hash collision")
        existing["aliases"].append(prop["property_id"])
        existing["provenance"].append(provenance)
        existing["requirement_ids"] = sorted(
            set(existing.get("requirement_ids", []))
            | set(prop.get("requirement_ids", []))
        )
        existing["adaptive"] = bool(existing.get("adaptive") or prop.get("adaptive"))
    return unique


def _select_property_rows(
    spec: DesignSpec,
    plan: dict[str, Any],
    expected_trace: dict[str, Any],
    depth: int,
    *,
    limit: int = FORMAL_BATCH_SIZE,
    adaptive_reserve: int = ADAPTIVE_PROPERTY_RESERVE,
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """Schedule and canonicalize every bounded-trace proof obligation."""
    groups, eligible_count, unsupported_scenarios = _scenario_property_groups(
        spec, plan, expected_trace, depth
    )
    adaptive = [group for group in groups if group["adaptive"]]
    adaptive_eligible = sum(group["eligible_count"] for group in adaptive)
    base_eligible = eligible_count - adaptive_eligible

    batch_size = int(limit)
    if batch_size <= 0:
        raise ValueError("formal batch size must be positive")
    selection_limit = eligible_count
    adaptive_reserve = min(max(0, int(adaptive_reserve)), selection_limit)
    descriptors = {
        int(group["scenario_index"]): list(_group_descriptors(group))
        for group in groups
    }
    cursors = {int(group["scenario_index"]): 0 for group in groups}
    selected_descriptors: list[tuple[dict[str, Any], int, str]] = []
    selected_keys: set[tuple[int, int, str]] = set()
    selected_scenarios: set[int] = set()
    selected_requirements: set[str] = set()

    def take_next(group: dict[str, Any]) -> bool:
        if len(selected_descriptors) >= selection_limit:
            return False
        scenario_index = int(group["scenario_index"])
        rows = descriptors[scenario_index]
        cursor = cursors[scenario_index]
        while cursor < len(rows):
            step_index, signal = rows[cursor]
            cursor += 1
            key = (scenario_index, step_index, signal)
            if key in selected_keys:
                continue
            cursors[scenario_index] = cursor
            selected_keys.add(key)
            selected_descriptors.append((group, step_index, signal))
            selected_scenarios.add(scenario_index)
            selected_requirements.update(
                str(value)
                for value in group["scenario"].get("requirement_ids", [])
                if isinstance(value, str)
            )
            return True
        cursors[scenario_index] = cursor
        return False

    # First represent every associated requirement. One selected checkpoint can
    # represent several requirements attached to the same scenario.
    requirement_order: list[str] = []
    for scenario in plan.get("scenarios", []):
        for value in scenario.get("requirement_ids", []):
            if isinstance(value, str) and value not in requirement_order:
                requirement_order.append(value)
    for requirement_id in requirement_order:
        if requirement_id in selected_requirements:
            continue
        candidates = [
            group
            for group in groups
            if requirement_id in group["scenario"].get("requirement_ids", [])
        ]
        candidates.sort(
            key=lambda group: (
                int(group["scenario_index"]) in selected_scenarios,
                int(group["scenario_index"]),
            )
        )
        for group in candidates:
            if take_next(group):
                break

    # Then give every eligible scenario one checkpoint before any long trace can
    # consume the cap.
    for group in sorted(groups, key=lambda row: int(row["scenario_index"])):
        if int(group["scenario_index"]) not in selected_scenarios:
            take_next(group)

    # Preserve capacity for adaptive counterexample replays. Fair scenario
    # selection may already exceed this minimum, which is intentional.
    adaptive_quota = min(adaptive_eligible, adaptive_reserve, selection_limit)
    adaptive_selected = sum(
        1 for group, _, _ in selected_descriptors if group["adaptive"]
    )
    while (
        adaptive_selected < adaptive_quota
        and len(selected_descriptors) < selection_limit
    ):
        advanced = False
        for group in sorted(
            adaptive, key=lambda row: int(row["scenario_index"]), reverse=True
        ):
            if take_next(group):
                adaptive_selected += 1
                advanced = True
                if adaptive_selected >= adaptive_quota:
                    break
        if not advanced:
            break

    # Fill remaining capacity round-robin across all scenarios.
    while len(selected_descriptors) < eligible_count:
        advanced = False
        for group in sorted(groups, key=lambda row: int(row["scenario_index"])):
            if take_next(group):
                advanced = True
                if len(selected_descriptors) >= selection_limit:
                    break
        if not advanced:
            break

    aliases = [
        _hydrate_property(group, step_index, signal)
        for group, step_index, signal in selected_descriptors
    ]
    selected = _deduplicate_properties(spec, aliases)
    adaptive_selected = sum(1 for row in selected if row["adaptive"])
    group_by_index = {int(row["scenario_index"]): row for row in groups}
    unsupported_by_index = {
        int(row["scenario_index"]): row for row in unsupported_scenarios
    }
    scenario_selection = []
    for scenario_index, scenario in enumerate(plan.get("scenarios", [])):
        group = group_by_index.get(scenario_index)
        unsupported = unsupported_by_index.get(scenario_index)
        count = int(group["eligible_count"]) if group is not None else 0
        excluded = (
            int(unsupported["excluded_obligation_count"])
            if unsupported is not None
            else 0
        )
        scenario_selection.append(
            {
                "scenario_index": scenario_index,
                "scenario_id": scenario.get("id"),
                "eligible": count,
                "selected": count,
                "excluded": excluded,
                "represented": count > 0 and unsupported is None,
                "supported": unsupported is None,
                "unsupported_reason_code": (
                    unsupported.get("reason_code") if unsupported is not None else None
                ),
            }
        )
    requirement_selection = []
    for requirement_id in requirement_order:
        eligible = sum(
            int(group["eligible_count"])
            for group in groups
            if requirement_id in group["scenario"].get("requirement_ids", [])
        )
        selected_count = eligible
        unsupported_refs = [
            row
            for row in unsupported_scenarios
            if requirement_id in row.get("requirement_ids", [])
        ]
        requirement_selection.append(
            {
                "requirement_id": requirement_id,
                "eligible": eligible,
                "selected": selected_count,
                "represented": selected_count > 0 and not unsupported_refs,
                "unsupported_scenario_ids": [
                    row.get("scenario_id") for row in unsupported_refs
                ],
            }
        )
    excluded_obligation_count = sum(
        int(row["excluded_obligation_count"]) for row in unsupported_scenarios
    )
    selection = {
        "limit": batch_size,
        "batch_size": batch_size,
        "batch_count": ceil(len(selected) / batch_size) if selected else 0,
        "adaptive_reserved_quota": adaptive_reserve,
        "raw_obligation_count": eligible_count + excluded_obligation_count,
        "deduplicated_obligation_count": eligible_count - len(selected),
        "eligible": len(selected),
        "selected": len(selected),
        "excluded": excluded_obligation_count,
        "truncated": False,
        "adaptive_alias_count": adaptive_eligible,
        "adaptive_eligible": adaptive_selected,
        "adaptive_selected": adaptive_selected,
        "adaptive_excluded": 0,
        "base_alias_count": base_eligible,
        "base_eligible": len(selected) - adaptive_selected,
        "base_selected": len(selected) - adaptive_selected,
        "frozen_scenario_count": len(plan.get("scenarios", [])),
        "supported_scenario_count": len(groups),
        "unsupported_scenario_count": len(unsupported_scenarios),
        "unsupported_scenarios": unsupported_scenarios,
        "scenario_selection": scenario_selection,
        "requirement_selection": requirement_selection,
        "unrepresented_scenario_ids": [
            row["scenario_id"]
            for row in scenario_selection
            if not row["represented"]
        ],
        "unrepresented_requirement_ids": [
            row["requirement_id"]
            for row in requirement_selection
            if not row["represented"]
        ],
        "scenario_fairness_complete": all(
            row["represented"] for row in scenario_selection
        ),
        "requirement_fairness_complete": all(
            row["represented"] for row in requirement_selection
        ),
    }
    return selected, selection


def build_formal_evidence_contract(
    spec: DesignSpec,
    plan: dict[str, Any],
    expected_trace: dict[str, Any],
    depth: int,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]], dict[str, Any]]:
    """Rebuild the deterministic obligations and their release property map."""
    properties, selection = _select_property_rows(
        spec,
        plan,
        expected_trace,
        depth,
        limit=FORMAL_BATCH_SIZE,
    )
    property_map = [
        {
            "property_index": index,
            "batch_index": ((index - 1) // FORMAL_BATCH_SIZE) + 1,
            "batch_property_index": ((index - 1) % FORMAL_BATCH_SIZE) + 1,
            **{key: value for key, value in prop.items() if key != "steps"},
        }
        for index, prop in enumerate(properties, 1)
    ]
    return properties, property_map, selection


def _yosys_script(spec: DesignSpec, properties: list[dict[str, Any]]) -> str:
    lines = [
        "read_verilog -formal -sv dut.sv",
        f"hierarchy -check -top {spec.name}",
        "proc",
        # RTL declaration initializers are not an ASIC reset guarantee.  Remove
        # their Yosys init attributes so every trace is proved from an arbitrary
        # defined state before the frozen reset prefix is applied.
        "setattr -unset init w:*",
        "chformal -remove",
        "async2sync",
        "flatten",
        "memory_map",
        "opt_clean",
        f"select -module {spec.name}",
    ]
    port_widths = {port.name: port.width for port in spec.ports}
    input_names = {
        port.name
        for port in _ports(spec, "input")
        if port.name != spec.clock_port
    }
    for index, prop in enumerate(properties, 1):
        command = ["sat", "-enable_undef", "-set-init-def"]
        for step_index, step in enumerate(prop["steps"], 1):
            for signal, value in sorted(step["inputs"].items()):
                if signal in input_names:
                    command.extend(
                        [
                            "-set-at",
                            str(step_index),
                            signal,
                            _yosys_bitvector(value, port_widths[signal]),
                        ]
                    )
        command.extend(
            [
                "-seq",
                str(len(prop["steps"])),
                "-prove-skip",
                str(max(0, len(prop["steps"]) - 1)),
                "-prove",
                prop["signal"],
                _yosys_bitvector(
                    prop["expected"], port_widths[prop["signal"]]
                ),
                "-show-inputs",
                "-show-outputs",
                "-dump_json",
                f"cex-{index:04d}.json",
            ]
        )
        lines.append(" ".join(command))
    return "\n".join(lines) + "\n"


def render_formal_batch_script(
    spec: DesignSpec, properties: list[dict[str, Any]]
) -> str:
    """Render the deterministic Yosys input used by one formal batch."""
    return _yosys_script(spec, properties)


def _counterexample(
    prop: dict[str, Any], cex_path: Path, *, property_index: int
) -> dict[str, Any]:
    actual = None
    try:
        wave = json.loads(cex_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        wave = None
    if isinstance(wave, dict):
        for signal in wave.get("signal", []):
            if str(signal.get("name", "")).lstrip("\\") != prop["signal"]:
                continue
            data = signal.get("data") or []
            if data and re.fullmatch(r"[01]+", str(data[-1])):
                actual = int(data[-1], 2)
            break
    return {
        "verified": actual is not None and actual != int(prop["expected"]),
        "property_index": property_index,
        "property_id": prop["property_id"],
        "canonical_sha256": prop.get("canonical_sha256"),
        "aliases": list(prop.get("aliases", [])),
        "provenance": list(prop.get("provenance", [])),
        "scenario_index": prop["scenario_index"],
        "scenario_id": prop["scenario_id"],
        "requirement_ids": prop["requirement_ids"],
        "cycle": prop["cycle"],
        "phase": prop["phase"],
        "signal": prop["signal"],
        "expected": prop["expected"],
        "actual": actual,
        "inputs": prop["steps"][-1]["inputs"],
        "input_trace": [dict(step["inputs"]) for step in prop["steps"]],
        "wavejson": wave,
        "artifact": str(cex_path),
    }


def _formal_error(
    code: str,
    category: str,
    message: str,
    **context: Any,
) -> dict[str, Any]:
    retryable = category in {"infrastructure", "deadline", "capacity"}
    return {
        "code": code,
        "category": category,
        "domain": "verification",
        "repairable": False,
        "repairable_by_rtl": False,
        "retryable": retryable,
        "user_action_required": category == "configuration",
        "provider": "yosys_bounded_trace",
        "message": message,
        "context": context,
        "evidence": [],
    }


def _resource_pressure(returncode: int | None, log: str) -> bool:
    """Recognize container/Yosys termination caused by memory pressure."""
    normalized = log.casefold()
    return returncode in {137, -9} or any(
        marker in normalized
        for marker in (
            "out of memory",
            "oom-kill",
            "oom killed",
            "cannot allocate memory",
            "memory cgroup out of memory",
            "killed process",
        )
    )


def _write_json_atomic(path: Path, value: Any) -> None:
    temporary = path.with_name(path.name + ".tmp")
    temporary.write_text(
        json.dumps(value, indent=2, ensure_ascii=False) + "\n", encoding="utf-8"
    )
    os.replace(temporary, path)


def run_bounded_trace_formal(
    spec: DesignSpec,
    rtl: str,
    plan: dict[str, Any],
    expected_trace: dict[str, Any],
    artifact_dir: Path,
    *,
    depth: int,
    timeout: int,
    total_timeout: int | None = None,
) -> dict[str, Any]:
    """Check every supported obligation with bounded, adaptive processes.

    ``timeout`` limits one Yosys process.  ``total_timeout`` limits the complete
    obligation manifest.  Omitting the latter preserves the historical API in
    which ``timeout`` was also the global deadline.
    """
    artifact_dir.mkdir(parents=True, exist_ok=True)
    work = artifact_dir / "formal"
    properties, property_map, property_selection = build_formal_evidence_contract(
        spec, plan, expected_trace, depth
    )
    (artifact_dir / "formal-properties.json").write_text(
        json.dumps(property_map, indent=2, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )
    manifest_payload = {
        "schema_version": 2,
        "provider": "yosys_bounded_trace",
        "engine": "yosys_internal_sat",
        "verify_image": verify_image_ref(),
        "rtl_sha256": content_hash(rtl),
        "suite_sha256": suite_hash(plan, expected_trace),
        "property_map_sha256": content_hash(property_map),
        "depth": depth,
        "initial_batch_size": FORMAL_BATCH_SIZE,
        "minimum_batch_size": FORMAL_MIN_BATCH_SIZE,
        "obligations": [
            {
                "property_index": index,
                "property_id": prop["property_id"],
                "canonical_sha256": prop["canonical_sha256"],
            }
            for index, prop in enumerate(properties, 1)
        ],
    }
    execution_identity = content_hash(manifest_payload)
    manifest = {**manifest_payload, "execution_identity_sha256": execution_identity}
    manifest_path = artifact_dir / "formal-manifest.json"
    checkpoint_path = artifact_dir / "formal-checkpoint.json"
    resume_checkpoint: dict[str, Any] | None = None
    if manifest_path.is_file() and checkpoint_path.is_file():
        try:
            previous_manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            candidate_checkpoint = json.loads(
                checkpoint_path.read_text(encoding="utf-8")
            )
        except (OSError, json.JSONDecodeError):
            previous_manifest = None
            candidate_checkpoint = None
        checkpoint_digest_valid = False
        if isinstance(candidate_checkpoint, dict):
            checkpoint_payload = dict(candidate_checkpoint)
            checkpoint_digest = checkpoint_payload.pop("checkpoint_sha256", None)
            checkpoint_digest_valid = checkpoint_digest == content_hash(
                checkpoint_payload
            )
        if (
            previous_manifest == manifest
            and isinstance(candidate_checkpoint, dict)
            and checkpoint_digest_valid
            and candidate_checkpoint.get("execution_identity_sha256")
            == execution_identity
        ):
            resume_checkpoint = candidate_checkpoint
    if resume_checkpoint is None:
        if work.exists():
            shutil.rmtree(work)
        checkpoint_path.unlink(missing_ok=True)
    work.mkdir(exist_ok=True)
    _write_json_atomic(manifest_path, manifest)
    initial_ranges = [
        (index, min(index + FORMAL_BATCH_SIZE, len(properties)), None)
        for index in range(0, len(properties), FORMAL_BATCH_SIZE)
    ]
    batch_reports: list[dict[str, Any]] = []
    reschedules: list[dict[str, Any]] = []
    passed: list[str] = []
    counterexamples: list[dict[str, Any]] = []
    errors: list[dict[str, Any]] = []
    logs: list[str] = []
    attempt_index = 0

    def reusable_batch(row: Any) -> bool:
        if not isinstance(row, dict):
            return False
        start_index = row.get("property_start_index")
        end_index = row.get("property_end_index")
        if (
            isinstance(start_index, bool)
            or not isinstance(start_index, int)
            or isinstance(end_index, bool)
            or not isinstance(end_index, int)
            or not 1 <= start_index <= end_index <= len(properties)
            or row.get("property_count") != end_index - start_index + 1
            or row.get("status") not in {"pass", "fail"}
            or row.get("infrastructure_status") is not None
        ):
            return False
        artifact = row.get("artifact")
        if not isinstance(artifact, str):
            return False
        batch_dir = Path(artifact)
        try:
            if work.resolve() not in batch_dir.resolve().parents:
                return False
            dut_text = (batch_dir / "dut.sv").read_text(encoding="utf-8")
            script_text = (batch_dir / "formal.ys").read_text(encoding="utf-8")
            log_text = (batch_dir / "formal.log").read_text(encoding="utf-8")
        except (OSError, UnicodeError, RuntimeError):
            return False
        batch = properties[start_index - 1 : end_index]
        if dut_text != rtl + "\n" or script_text != _yosys_script(spec, batch):
            return False
        success_count = len(FORMAL_SUCCESS_PATTERN.findall(log_text))
        if row["status"] == "pass" and success_count != len(batch):
            return False
        if row["status"] == "fail" and not row.get("counterexamples"):
            return False
        return row.get("success_verdict_count") == success_count

    cached_reports: list[dict[str, Any]] = []
    if resume_checkpoint is not None:
        raw_completed = resume_checkpoint.get("completed")
        if isinstance(raw_completed, list) and all(
            reusable_batch(row) for row in raw_completed
        ):
            ordered = sorted(
                (dict(row) for row in raw_completed),
                key=lambda row: int(row["property_start_index"]),
            )
            previous_end = 0
            non_overlapping = True
            for row in ordered:
                if int(row["property_start_index"]) <= previous_end:
                    non_overlapping = False
                    break
                previous_end = int(row["property_end_index"])
            if non_overlapping:
                cached_reports = ordered
        raw_reschedules = resume_checkpoint.get("reschedules")
        if isinstance(raw_reschedules, list) and all(
            isinstance(row, dict) for row in raw_reschedules
        ):
            reschedules.extend(dict(row) for row in raw_reschedules)
        next_attempt = resume_checkpoint.get("next_attempt_index")
        if isinstance(next_attempt, int) and not isinstance(next_attempt, bool):
            attempt_index = max(0, next_attempt - 1)

    def persist_checkpoint() -> None:
        reusable = [
            dict(row)
            for row in batch_reports
            if row.get("status") in {"pass", "fail"}
            and row.get("infrastructure_status") is None
        ]
        checkpoint = {
            "schema_version": 1,
            "execution_identity_sha256": execution_identity,
            "next_attempt_index": attempt_index + 1,
            "completed": sorted(
                reusable, key=lambda row: int(row["property_start_index"])
            ),
            "reschedules": reschedules,
        }
        checkpoint["checkpoint_sha256"] = content_hash(checkpoint)
        _write_json_atomic(checkpoint_path, checkpoint)

    batch_reports.extend(cached_reports)
    for row in cached_reports:
        row["resumed"] = True
        passed.extend(str(value) for value in row.get("passed_property_ids", []))
        counterexamples.extend(
            dict(value)
            for value in row.get("counterexamples", [])
            if isinstance(value, dict)
        )
        artifact = row.get("artifact")
        if isinstance(artifact, str):
            try:
                logs.append(
                    f"=== resumed {row.get('attempt_id')} "
                    f"({row['property_start_index']}-{row['property_end_index']}) ===\n"
                    + (Path(artifact) / "formal.log").read_text(encoding="utf-8")
                )
            except (OSError, UnicodeError):
                pass
    if not properties:
        if property_selection["unsupported_scenario_count"]:
            report = {
                "status": "pass",
                "provider": "yosys_bounded_trace",
                "authoritative": True,
                "detail": (
                    "no supported bounded-trace obligations were generated; "
                    f"{property_selection['unsupported_scenario_count']} frozen "
                    "scenario(s) are explicitly unsupported"
                ),
                "property_count": 0,
                "passed_property_ids": [],
                "counterexamples": [],
                "infrastructure_errors_present": False,
                "infrastructure_error_count": 0,
                "infrastructure_statuses": [],
            }
        else:
            error = _formal_error(
                "formal_configuration_no_obligations",
                "configuration",
                "no deterministic trace obligations were generated",
                depth=depth,
            )
            errors.append(error)
            report = {
                "status": "unknown",
                "provider": "yosys_bounded_trace",
                "authoritative": True,
                "detail": error["message"],
                "property_count": 0,
                "passed_property_ids": [],
                "counterexamples": [],
                "infrastructure_errors_present": False,
                "infrastructure_error_count": 0,
                "infrastructure_statuses": [],
            }
    else:
        global_timeout = timeout if total_timeout is None else total_timeout
        deadline = time.monotonic() + global_timeout
        covered = [False] * len(properties)
        for row in cached_reports:
            for index in range(
                int(row["property_start_index"]) - 1,
                int(row["property_end_index"]),
            ):
                covered[index] = True
        pending: list[tuple[int, int, str | None]] = []
        for initial_start, initial_end, _ in initial_ranges:
            cursor = initial_start
            while cursor < initial_end:
                if covered[cursor]:
                    cursor += 1
                    continue
                end = cursor + 1
                while end < initial_end and not covered[end]:
                    end += 1
                pending.append((cursor, end, None))
                cursor = end
        while pending:
            start, end, parent_attempt_id = pending.pop(0)
            batch = properties[start:end]
            start_index = start + 1
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                error = _formal_error(
                    "formal_global_deadline_exceeded",
                    "deadline",
                    f"bounded formal exceeded the {global_timeout}-second global deadline",
                    first_unexecuted_property=start_index,
                    property_count=len(properties),
                )
                errors.append(error)
                pending.insert(0, (start, end, parent_attempt_id))
                break
            attempt_index += 1
            attempt_id = f"formal-attempt-{attempt_index:04d}"
            batch_dir = work / f"batch-{attempt_index:04d}"
            if batch_dir.exists():
                shutil.rmtree(batch_dir)
            batch_dir.mkdir()
            (batch_dir / "dut.sv").write_text(rtl + "\n", encoding="utf-8")
            (batch_dir / "formal.ys").write_text(
                _yosys_script(spec, batch), encoding="utf-8"
            )
            command = _docker_base(batch_dir, read_only_mount=False) + [
                "--entrypoint",
                "yosys",
                verify_image_ref(),
                "-Q",
                "-s",
                "formal.ys",
            ]
            batch_error: dict[str, Any] | None = None
            batch_infrastructure_status: str | None = None
            batch_returncode: int | None = None
            batch_success_count = 0
            batch_passed_ids: list[str] = []
            try:
                completed = _run_container(
                    command,
                    min(float(timeout), remaining),
                    purpose=f"formal-batch-{attempt_index}",
                )
            except subprocess.TimeoutExpired:
                batch_status = "unknown"
                batch_infrastructure_status = "unknown"
                batch_log = (
                    f"batch exceeded its {min(float(timeout), remaining):.3f}s "
                    "execution deadline"
                )
                batch_error = _formal_error(
                    "formal_batch_timeout",
                    "deadline",
                    "bounded formal batch exceeded its execution deadline",
                    attempt_id=attempt_id,
                    property_start_index=start_index,
                    property_count=len(batch),
                )
            except OSError as exc:
                batch_status = "error"
                batch_infrastructure_status = "error"
                batch_log = str(exc)
                batch_error = _formal_error(
                    "formal_batch_launch_error",
                    "infrastructure",
                    str(exc),
                    attempt_id=attempt_id,
                    property_start_index=start_index,
                    property_count=len(batch),
                )
            else:
                batch_log = (completed.stdout + "\n" + completed.stderr).strip()
                batch_returncode = completed.returncode
                batch_success_count = len(FORMAL_SUCCESS_PATTERN.findall(batch_log))
                if completed.returncode != 0:
                    batch_status = "error"
                    batch_infrastructure_status = "error"
                    batch_error = _formal_error(
                        "formal_batch_execution_error",
                        "infrastructure",
                        f"Yosys batch exited with code {completed.returncode}",
                        attempt_id=attempt_id,
                        property_start_index=start_index,
                        property_count=len(batch),
                        returncode=completed.returncode,
                    )
                elif "timeout" in batch_log.lower():
                    batch_status = "unknown"
                    batch_infrastructure_status = "unknown"
                    batch_error = _formal_error(
                        "formal_batch_inconclusive",
                        "infrastructure",
                        "Yosys reported an inconclusive timeout",
                        attempt_id=attempt_id,
                        property_start_index=start_index,
                        property_count=len(batch),
                    )
                else:
                    batch_status = "pending_verdict"

            (batch_dir / "formal.log").write_text(
                batch_log + "\n", encoding="utf-8"
            )

            # A freshly emitted counterexample is a concrete behavioral failure,
            # even if Yosys then exits abnormally or exhausts the deadline.
            counterexample_rows = [
                (
                    index,
                    _counterexample(
                        batch[index - 1],
                        path,
                        property_index=start_index + index - 1,
                    ),
                )
                for path in batch_dir.glob("cex-*.json")
                if (match := re.fullmatch(r"cex-(\d+)\.json", path.name))
                if 1 <= (index := int(match.group(1))) <= len(batch)
            ]
            verified_counterexamples = [
                (index, row)
                for index, row in counterexample_rows
                if row["verified"] is True
            ]
            failed_indices = {index for index, _ in verified_counterexamples}
            if (
                not failed_indices
                and _resource_pressure(batch_returncode, batch_log)
                and len(batch) >= FORMAL_MIN_BATCH_SIZE * 2
            ):
                split = start + len(batch) // 2
                child_ranges = [
                    (start, split, attempt_id),
                    (split, end, attempt_id),
                ]
                reschedules.append(
                    {
                        "attempt_id": attempt_id,
                        "parent_attempt_id": parent_attempt_id,
                        "reason": "resource_pressure",
                        "returncode": batch_returncode,
                        "property_start_index": start_index,
                        "property_end_index": end,
                        "property_count": len(batch),
                        "child_property_counts": [split - start, end - split],
                        "artifact": str(batch_dir),
                        "log_sha256": hashlib.sha256(
                            batch_log[-4000:].encode()
                        ).hexdigest(),
                        "detail": batch_log[-4000:],
                    }
                )
                logs.append(
                    f"=== {attempt_id} rescheduled ({start_index}-{end}) ===\n"
                    f"{batch_log}"
                )
                pending[0:0] = child_ranges
                persist_checkpoint()
                continue
            if failed_indices:
                batch_status = "fail"
                if (
                    batch_returncode == 0
                    and batch_success_count != len(batch) - len(failed_indices)
                    and batch_error is None
                ):
                    batch_infrastructure_status = "error"
                    batch_error = _formal_error(
                        "formal_batch_incomplete_transcript",
                        "evidence",
                        "Yosys transcript success count does not cover every non-failing obligation",
                        attempt_id=attempt_id,
                        expected_success_count=len(batch) - len(failed_indices),
                        observed_success_count=batch_success_count,
                    )
                if batch_error is None:
                    batch_passed_ids = list(
                        prop["property_id"]
                        for local_index, prop in enumerate(batch, 1)
                        if local_index not in failed_indices
                    )
                    passed.extend(batch_passed_ids)
                counterexamples.extend(
                    row for _, row in sorted(verified_counterexamples)
                )
            elif batch_status == "pending_verdict":
                if batch_success_count == len(batch):
                    batch_status = "pass"
                    batch_passed_ids = [prop["property_id"] for prop in batch]
                    passed.extend(batch_passed_ids)
                else:
                    batch_status = "error"
                    batch_infrastructure_status = "error"
                    batch_error = _formal_error(
                        "formal_batch_missing_success_verdict",
                        "evidence",
                        "Yosys exited successfully without one explicit SAT SUCCESS verdict per obligation",
                        attempt_id=attempt_id,
                        expected_success_count=len(batch),
                        observed_success_count=batch_success_count,
                    )
            logs.append(f"=== {attempt_id} ({start_index}-{end}) ===\n{batch_log}")
            if batch_error is not None:
                errors.append(batch_error)
            batch_reports.append(
                {
                    "batch_index": 0,
                    "attempt_id": attempt_id,
                    "parent_attempt_id": parent_attempt_id,
                    "status": batch_status,
                    "property_start_index": start_index,
                    "property_end_index": start_index + len(batch) - 1,
                    "property_count": len(batch),
                    "artifact": str(batch_dir),
                    "error": batch_error,
                    "infrastructure_status": batch_infrastructure_status,
                    "returncode": batch_returncode,
                    "success_verdict_count": batch_success_count,
                    "passed_property_ids": batch_passed_ids,
                    "counterexamples": [
                        row for _, row in sorted(verified_counterexamples)
                    ],
                    "resumed": False,
                }
            )
            persist_checkpoint()
        for start, end, parent_attempt_id in pending:
            batch_reports.append(
                {
                    "batch_index": 0,
                    "attempt_id": None,
                    "parent_attempt_id": parent_attempt_id,
                    "status": "not_run",
                    "property_start_index": start + 1,
                    "property_end_index": end,
                    "property_count": end - start,
                    "artifact": None,
                    "error": errors[-1] if errors else None,
                    "infrastructure_status": "not_run",
                    "returncode": None,
                    "success_verdict_count": 0,
                }
            )

        batch_reports.sort(key=lambda row: int(row["property_start_index"]))
        for batch_index, row in enumerate(batch_reports, 1):
            row["batch_index"] = batch_index
        passed = [
            str(property_id)
            for row in batch_reports
            for property_id in row.get("passed_property_ids", [])
        ]
        counterexamples = [
            dict(counterexample)
            for row in batch_reports
            for counterexample in row.get("counterexamples", [])
            if isinstance(counterexample, dict)
        ]

        statuses = {row["status"] for row in batch_reports}
        # A concrete counterexample is always authoritative for the candidate,
        # even if another batch is inconclusive. Preserve both verdict classes.
        if "fail" in statuses:
            status = "fail"
        elif "error" in statuses:
            status = "error"
        elif statuses & {"unknown", "not_run"}:
            status = "unknown"
        else:
            status = "pass"
        infrastructure_statuses = sorted(
            {
                row["infrastructure_status"]
                for row in batch_reports
                if row.get("infrastructure_status") is not None
            }
        )
        detail = (
            f"bounded formal found {len(counterexamples)} counterexample(s)"
            + (
                f"; {len(errors)} infrastructure error(s) also occurred"
                if infrastructure_statuses
                else ""
            )
            if status == "fail"
            else errors[0]["message"]
            if errors
            else "all formal batches completed"
        )
        report = {
            "status": status,
            "provider": "yosys_bounded_trace",
            "authoritative": True,
            "detail": detail,
            "property_count": len(properties),
            "passed_property_ids": passed if status in {"pass", "fail"} else [],
            "counterexamples": counterexamples,
            "depth": depth,
            "infrastructure_errors_present": bool(errors),
            "infrastructure_error_count": len(errors),
            "infrastructure_statuses": infrastructure_statuses,
        }

    persist_checkpoint()
    (artifact_dir / "formal.log").write_text(
        ("\n".join(logs) or report["detail"]) + "\n", encoding="utf-8"
    )
    report["rtl_sha256"] = content_hash(rtl)
    report["suite_sha256"] = suite_hash(plan, expected_trace)
    report["property_map_sha256"] = content_hash(property_map)
    report["property_selection"] = property_selection
    report["eligible_property_count"] = property_selection["eligible"]
    report["selected_property_count"] = property_selection["selected"]
    report["excluded_property_count"] = property_selection["excluded"]
    report["properties_truncated"] = property_selection["truncated"]
    report["adaptive_selected_property_count"] = property_selection[
        "adaptive_selected"
    ]
    report["verification_method"] = "bounded_trace_checking"
    report["proof_scope"] = "supported_unique_frozen_input_trace_obligations"
    report["universal_proof"] = False
    report["status_semantics"] = (
        "pass means every generated, supported frozen-trace checkpoint obligation "
        "passed; evidence_complete must also be true to claim complete frozen-suite "
        "coverage, and this is not a proof over arbitrary input sequences"
    )
    report["selection_complete"] = bool(
        not property_selection["truncated"]
        and property_selection["excluded"] == 0
        and property_selection["scenario_fairness_complete"]
        and property_selection["requirement_fairness_complete"]
    )
    report["batch_size"] = FORMAL_BATCH_SIZE
    report["minimum_batch_size"] = FORMAL_MIN_BATCH_SIZE
    report["initial_batch_count"] = len(initial_ranges)
    report["batch_count"] = len(batch_reports)
    report["execution_attempt_count"] = attempt_index
    report["reschedule_count"] = len(reschedules)
    report["reschedules"] = reschedules
    report["completed_batch_count"] = sum(
        row["status"] in {"pass", "fail"}
        and row.get("infrastructure_status") is None
        for row in batch_reports
    )
    report["batches"] = batch_reports
    report["errors"] = errors
    report["error"] = errors[0] if errors else None
    report["execution_complete"] = bool(
        sum(row["property_count"] for row in batch_reports) == len(properties)
        and all(
            row["status"] in {"pass", "fail"}
            and row.get("infrastructure_status") is None
            for row in batch_reports
        )
    )
    report["evidence_complete"] = bool(
        report["selection_complete"] and report["execution_complete"]
    )
    report["completeness"] = (
        "complete_for_all_unique_bounded_trace_obligations"
        if report["evidence_complete"]
        else "incomplete_execution"
        if report["selection_complete"]
        else "incomplete_selection"
    )
    report["engine"] = "yosys_internal_sat"
    report["verify_image"] = verify_image_ref()
    report["execution_identity_sha256"] = execution_identity
    report["obligation_manifest"] = str(manifest_path)
    report["checkpoint"] = str(checkpoint_path)
    execution_entries: list[dict[str, Any]] = []
    total_execution_bytes = 0
    expected_dut = rtl + "\n"
    for batch_report in batch_reports:
        start = int(batch_report["property_start_index"]) - 1
        end = int(batch_report["property_end_index"])
        batch = properties[start:end]
        artifact = batch_report.get("artifact")
        if artifact is None:
            execution_entries.append(
                {
                    "batch_index": batch_report["batch_index"],
                    "status": batch_report["status"],
                    "executed": False,
                    "returncode": None,
                    "property_start_index": batch_report["property_start_index"],
                    "property_end_index": batch_report["property_end_index"],
                    "property_count": batch_report["property_count"],
                    "success_verdict_count": 0,
                    "dut_sha256": None,
                    "script_sha256": None,
                    "log_sha256": None,
                    "script": None,
                    "log": None,
                }
            )
            continue
        batch_dir = Path(artifact)
        dut_text = (batch_dir / "dut.sv").read_text(encoding="utf-8")
        script_text = (batch_dir / "formal.ys").read_text(encoding="utf-8")
        log_text = (batch_dir / "formal.log").read_text(encoding="utf-8")
        expected_script = render_formal_batch_script(spec, batch)
        if dut_text != expected_dut or script_text != expected_script:
            raise RuntimeError("formal execution inputs changed before evidence capture")
        total_execution_bytes += len(script_text.encode()) + len(log_text.encode())
        if total_execution_bytes > 64 * 1024 * 1024:
            raise RuntimeError("formal execution evidence exceeds the 64 MiB release limit")
        execution_entries.append(
            {
                "batch_index": batch_report["batch_index"],
                "status": batch_report["status"],
                "executed": True,
                "returncode": batch_report.get("returncode"),
                "property_start_index": batch_report["property_start_index"],
                "property_end_index": batch_report["property_end_index"],
                "property_count": batch_report["property_count"],
                "success_verdict_count": len(
                    FORMAL_SUCCESS_PATTERN.findall(log_text)
                ),
                "dut_sha256": hashlib.sha256(dut_text.encode()).hexdigest(),
                "script_sha256": hashlib.sha256(script_text.encode()).hexdigest(),
                "log_sha256": hashlib.sha256(log_text.encode()).hexdigest(),
                "script": script_text,
                "log": log_text,
            }
        )
    execution_evidence = {
        "schema_version": 2,
        "provider": "yosys_bounded_trace_execution",
        "engine": "yosys_internal_sat",
        "verify_image": verify_image_ref(),
        "rtl_sha256": content_hash(rtl),
        "suite_sha256": suite_hash(plan, expected_trace),
        "property_map_sha256": content_hash(property_map),
        "batch_size": FORMAL_BATCH_SIZE,
        "minimum_batch_size": FORMAL_MIN_BATCH_SIZE,
        "batch_count": len(batch_reports),
        "reschedule_count": len(reschedules),
        "reschedules": reschedules,
        "entries": execution_entries,
    }
    execution_evidence["evidence_sha256"] = content_hash(execution_evidence)
    report["execution_evidence_sha256"] = execution_evidence["evidence_sha256"]
    (artifact_dir / "formal-execution-evidence.json").write_text(
        json.dumps(execution_evidence, indent=2, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )
    report_path = artifact_dir / "formal-report.json"
    report["artifact"] = str(report_path)
    report_path.write_text(
        json.dumps(report, indent=2, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )
    return report


def merge_formal_result(
    simulation: dict[str, Any],
    formal: dict[str, Any],
    contract: dict[str, Any],
    plan: dict[str, Any],
    *,
    required: bool,
) -> dict[str, Any]:
    """Normalize formal feedback into the simulation result contract."""
    result = json.loads(json.dumps(simulation))
    result["formal"] = json.loads(json.dumps(formal))
    status = formal.get("status")
    result["source"] = "formal" if status == "fail" else "simulation"
    formal_passes = [
        f"formal:{property_id}"
        for property_id in formal.get("passed_property_ids", [])
    ]
    result["passed_check_ids"] = list(result.get("passed_check_ids", [])) + formal_passes
    verified_properties = (
        int(formal.get("property_count") or 0)
        if status in {"pass", "fail"}
        else 0
    )
    result["total_checks"] = int(result.get("total_checks") or 0) + verified_properties
    if status == "fail":
        simulation_checks = int(simulation.get("total_checks") or 0)
        raw_counterexamples = formal.get("counterexamples") or [{}]
        indexed_counterexamples: list[tuple[int, dict[str, Any]]] = []
        for fallback_index, raw_cex in enumerate(raw_counterexamples, 1):
            cex = raw_cex if isinstance(raw_cex, dict) else {}
            property_index = cex.get("property_index", fallback_index)
            if (
                isinstance(property_index, bool)
                or not isinstance(property_index, int)
                or property_index < 1
            ):
                property_index = fallback_index
            indexed_counterexamples.append((property_index, cex))
        indexed_counterexamples.sort(key=lambda item: item[0])

        mismatches: list[dict[str, Any]] = []
        formal_trace: list[dict[str, Any]] = []
        for property_index, cex in indexed_counterexamples:
            formal_ordinal = simulation_checks + property_index - 1
            mismatch = {
                "check_id": f"formal:{cex.get('property_id', 'unknown')}",
                "ordinal": formal_ordinal,
                "scenario_index": cex.get("scenario_index"),
                "scenario_id": cex.get("scenario_id"),
                "requirement_ids": cex.get("requirement_ids", []),
                "cycle": cex.get("cycle"),
                "phase": cex.get("phase", "formal"),
                "signal": cex.get("signal"),
                "expected": cex.get("expected"),
                "actual": cex.get("actual"),
                "inputs": cex.get("inputs", {}),
                "failure_class": "formal_counterexample",
                "property_index": property_index,
                "property_id": cex.get("property_id"),
                "aliases": cex.get("aliases", []),
                "provenance": cex.get("provenance", []),
            }
            mismatches.append(mismatch)
            formal_trace.append(
                {
                    "ordinal": formal_ordinal,
                    "scenario_index": cex.get("scenario_index"),
                    "scenario_id": cex.get("scenario_id"),
                    "cycle": cex.get("cycle"),
                    "phase": cex.get("phase", "formal"),
                    "inputs": cex.get("inputs", {}),
                    "expected": {cex.get("signal"): cex.get("expected")}
                    if cex.get("signal")
                    else {},
                    "actual": {cex.get("signal"): cex.get("actual")}
                    if cex.get("signal")
                    else {},
                    "formal_property_index": property_index,
                    "formal_property_id": cex.get("property_id"),
                }
            )
        result.update(
            {
                "passed": False,
                "failure_class": "formal_counterexample",
                "first_failure_ordinal": mismatches[0]["ordinal"],
                "mismatched": int(result.get("mismatched") or 0)
                + len(mismatches),
                "mismatches": mismatches,
            }
        )
        result.setdefault("actual_trace", []).extend(formal_trace)
    elif status in {"unknown", "error"} and required:
        error = formal.get("error") or _formal_error(
            f"formal_provider_{status}",
            "infrastructure",
            str(formal.get("detail") or f"formal provider returned {status}"),
        )
        result["formal"]["error"] = error
        result.update(
            {
                "passed": False,
                "failure_class": f"formal_{status}",
                "detail": formal.get("detail"),
                "first_failure_ordinal": None,
                "mismatched": int(result.get("mismatched") or 0),
                "mismatches": [],
                "source": "formal",
                "non_repairable": True,
                "verification_error": error,
            }
        )
    elif status == "pass" and required and (
        formal.get("properties_truncated")
        or formal.get("selection_complete") is False
        or formal.get("execution_complete") is False
        or formal.get("evidence_complete") is False
        or (
            "batch_count" in formal
            and formal.get("completed_batch_count") != formal.get("batch_count")
        )
        or (formal.get("property_selection") or {}).get(
            "unrepresented_scenario_ids"
        )
        or (formal.get("property_selection") or {}).get(
            "unrepresented_requirement_ids"
        )
    ):
        selection = formal.get("property_selection") or {}
        error = formal.get("error") or _formal_error(
            "formal_incomplete_evidence",
            "evidence",
            "required bounded-trace verification did not completely represent "
            "the frozen suite and execute every supported eligible unique obligation",
            eligible_unique=selection.get("eligible"),
            selected_unique=selection.get("selected"),
            excluded_unique=selection.get("excluded"),
            batch_count=formal.get("batch_count"),
            completed_batch_count=formal.get("completed_batch_count"),
            unrepresented_scenarios=selection.get(
                "unrepresented_scenario_ids", []
            ),
            unrepresented_requirements=selection.get(
                "unrepresented_requirement_ids", []
            ),
            unsupported_scenarios=selection.get("unsupported_scenarios", []),
        )
        result["formal"]["error"] = error
        result.update(
            {
                "passed": False,
                "failure_class": "formal_incomplete",
                "detail": error["message"],
                "first_failure_ordinal": None,
                "mismatched": int(result.get("mismatched") or 0),
                "mismatches": list(result.get("mismatches") or []),
                "source": "formal",
                "non_repairable": True,
                "verification_error": error,
            }
        )
    result["coverage"] = behavioral_coverage(contract, plan, result)
    return result


def merge_lint_result(
    verification: dict[str, Any],
    lint: dict[str, Any],
    contract: dict[str, Any],
    plan: dict[str, Any],
    *,
    required: bool,
) -> dict[str, Any]:
    """Append the independent Verilator verdict without mutating formal evidence."""
    result = json.loads(json.dumps(verification))
    result["verilator_lint"] = lint
    status = lint.get("status")
    check_id = "lint:VERILATOR_LINT"

    if status in {"pass", "fail"}:
        lint_ordinal = int(result.get("total_checks") or 0)
        result["total_checks"] = lint_ordinal + 1
        if status == "pass":
            passed = list(result.get("passed_check_ids", []))
            if check_id not in passed:
                passed.append(check_id)
            result["passed_check_ids"] = passed
        else:
            mismatch = {
                "check_id": check_id,
                "ordinal": lint_ordinal,
                "scenario_index": None,
                "scenario_id": None,
                "requirement_ids": [],
                "cycle": None,
                "phase": "static",
                "signal": None,
                "expected": "lint clean",
                "actual": lint.get("detail"),
                "inputs": {},
                "failure_class": "verilator_lint_error",
            }
            existing = list(result.get("mismatches") or [])
            existing.append(mismatch)
            was_passing = bool(result.get("passed"))
            result["passed"] = False
            result["mismatches"] = existing
            result["mismatched"] = int(result.get("mismatched") or 0) + 1
            first = result.get("first_failure_ordinal")
            if first is None or lint_ordinal < int(first):
                result["first_failure_ordinal"] = lint_ordinal
            if was_passing:
                result["failure_class"] = "verilator_lint_error"
                result["detail"] = lint.get("detail")
                result["source"] = "verilator_lint"
    elif status in {"unknown", "error"} and required:
        result["infrastructure_failure"] = {
            "provider": "verilator_lint",
            "status": status,
            "detail": lint.get("detail"),
        }
        if result.get("passed"):
            result.update(
                {
                    "passed": False,
                    "failure_class": f"verilator_lint_{status}",
                    "detail": lint.get("detail"),
                    "first_failure_ordinal": 0,
                    "mismatched": 1,
                    "mismatches": [],
                }
            )

    result["coverage"] = behavioral_coverage(contract, plan, result)
    return result
