"""Trusted scenario execution and structured RTL verification."""

from __future__ import annotations

import ast
import json
import math
import os
import random
import re
import shutil
import subprocess
import textwrap
import uuid
from pathlib import Path
from typing import Any

from .adaptive import behavioral_coverage, suite_hash
from .contract import content_hash
from .errors import ContainerInfrastructureError
from .sandbox import ORACLE_NAMESPACE_SETUP, _docker_base
from .spec import DesignSpec
from .runtime import (
    VERIFY_IMAGE as VERIFY_IMAGE,
    VERIFY_PYTHON,
    sandbox_image_ref,
    verify_image_ref,
)

RESET_DIRTY_CYCLES = 2


def independent_input_widths(spec: DesignSpec) -> dict[str, int]:
    """Return the ordered data-input contract used by cross-verification."""
    reset_port = (
        spec.reset.get("port") if isinstance(spec.reset, dict) else None
    )
    excluded = {spec.clock_port, reset_port}
    return {
        port.name: port.width
        for port in spec.ports
        if port.direction == "input" and port.name not in excluded
    }


def derive_independent_stimuli(
    spec: DesignSpec, *, num_tests: int, seed: int
) -> list[dict[str, int]]:
    """Reconstruct the exact random vectors used by cross-verification."""
    inputs = independent_input_widths(spec)
    rng = random.Random(seed)
    return [
        {
            name: rng.randint(0, (1 << width) - 1)
            for name, width in inputs.items()
        }
        for _ in range(num_tests)
    ]


def validate_independent_stimuli(
    value: Any,
    *,
    expected_stimuli: list[dict[str, int]],
    expected_inputs: dict[str, int],
) -> str | None:
    """Validate vector count, order, input identity, width, and seed replay."""
    if not isinstance(value, list) or len(value) != len(expected_stimuli):
        return "verifier stimuli do not have the frozen seed-derived length"
    for index, (observed, expected) in enumerate(zip(value, expected_stimuli)):
        if not isinstance(observed, dict) or set(observed) != set(expected):
            return f"verifier stimulus {index} has invalid input keys"
        for name, width in expected_inputs.items():
            observed_value = observed.get(name)
            if isinstance(observed_value, bool) or not isinstance(
                observed_value, int
            ):
                return f"verifier stimulus {index}.{name} is not an integer"
            if observed_value < 0 or observed_value >= (1 << width):
                return f"verifier stimulus {index}.{name} is outside its input range"
        if observed != expected:
            return f"verifier stimulus {index} does not match the frozen seed replay"
    return None


def validate_independent_transcript(
    result: dict[str, Any],
    *,
    expected_stimuli: list[dict[str, int]],
    expected_inputs: dict[str, int],
    expected_outputs: dict[str, int],
) -> str | None:
    """Validate a complete, concrete differential-verification transcript."""
    expected_num_tests = len(expected_stimuli)
    if result.get("num_tests") != expected_num_tests:
        return "verifier num_tests does not match the frozen request"
    stimulus_error = validate_independent_stimuli(
        result.get("stimuli"),
        expected_stimuli=expected_stimuli,
        expected_inputs=expected_inputs,
    )
    if stimulus_error is not None:
        return stimulus_error
    output_names = set(expected_outputs)
    if not output_names:
        return "verifier transcript has no expected outputs"
    for name in ("verilog_trace", "python_trace"):
        rows = result.get(name)
        if not isinstance(rows, list) or len(rows) != expected_num_tests:
            return f"verifier {name} is not a complete frozen-length trace"
        for index, row in enumerate(rows):
            if not isinstance(row, dict) or set(row) != output_names:
                return f"verifier {name} row {index} has invalid output keys"
            for signal, value in row.items():
                if isinstance(value, bool) or not isinstance(value, int):
                    return (
                        f"verifier {name} row {index}.{signal} is not a concrete integer"
                    )
                if value < 0 or value >= (1 << expected_outputs[signal]):
                    return (
                        f"verifier {name} row {index}.{signal} is outside its output range"
                    )
    mismatches = result.get("mismatches")
    mismatched = result.get("mismatched")
    total_checks = result.get("total_checks")
    expected_total_checks = expected_num_tests * len(output_names)
    if not isinstance(mismatches, list):
        return "verifier mismatches is not an array"
    if (
        isinstance(mismatched, bool)
        or not isinstance(mismatched, int)
        or mismatched < 0
        or mismatched != len(mismatches)
    ):
        return "verifier mismatched count does not bind the mismatch rows"
    if (
        isinstance(total_checks, bool)
        or not isinstance(total_checks, int)
        or total_checks != expected_total_checks
    ):
        return "verifier total_checks does not cover every output sample"

    verilog_trace = result["verilog_trace"]
    python_trace = result["python_trace"]
    mismatch_keys: set[tuple[int, str]] = set()
    for mismatch in mismatches:
        if not isinstance(mismatch, dict) or set(mismatch) != {
            "test",
            "signal",
            "width",
            "verilog",
            "python",
            "inputs",
        }:
            return "verifier mismatch row has an invalid shape"
        test_index = mismatch.get("test")
        signal = mismatch.get("signal")
        key = (test_index, signal)
        if (
            isinstance(test_index, bool)
            or not isinstance(test_index, int)
            or not 0 <= test_index < expected_num_tests
            or not isinstance(signal, str)
            or signal not in output_names
            or isinstance(mismatch.get("width"), bool)
            or mismatch.get("width") != expected_outputs[signal]
            or mismatch.get("inputs") != expected_stimuli[test_index]
            or mismatch.get("verilog") != verilog_trace[test_index][signal]
            or mismatch.get("python") != python_trace[test_index][signal]
            or key in mismatch_keys
        ):
            return "verifier mismatch row is not bound to its exact replay sample"
        mismatch_keys.add(key)

    derived_mismatch_keys: set[tuple[int, str]] = set()
    for test_index, (verilog_row, python_row) in enumerate(
        zip(verilog_trace, python_trace)
    ):
        for signal, width in expected_outputs.items():
            mask = (1 << width) - 1
            if (verilog_row[signal] & mask) != (python_row[signal] & mask):
                derived_mismatch_keys.add((test_index, signal))
    if mismatch_keys != derived_mismatch_keys:
        return "verifier mismatch rows omit or invent trace differences"

    expected_rate = 1.0 - (mismatched / expected_total_checks)
    match_rate = result.get("match_rate")
    if (
        isinstance(match_rate, bool)
        or not isinstance(match_rate, (int, float))
        or not math.isclose(float(match_rate), expected_rate, abs_tol=1e-12)
    ):
        return "verifier match_rate does not bind the check counts"
    return None


def oracle_executable_lines(code: str) -> list[int]:
    """Return executable statement lines inside TopModule methods."""
    tree = ast.parse(code)
    lines: set[int] = set()
    for node in tree.body:
        if not isinstance(node, ast.ClassDef) or node.name != "TopModule":
            continue
        for method in node.body:
            if not isinstance(method, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            for child in ast.walk(method):
                if isinstance(child, ast.stmt) and not isinstance(
                    child, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)
                ):
                    lines.add(child.lineno)
    return sorted(lines)


def check_oracle_policy(code: str) -> None:
    if len(code.encode("utf-8")) > 1024 * 1024:
        raise RuntimeError("generated Python oracle exceeds the 1 MiB safety limit")
    try:
        tree = ast.parse(code)
    except SyntaxError as exc:
        raise RuntimeError(f"Python oracle has invalid syntax: {exc}") from exc
    forbidden_nodes = (ast.Import, ast.ImportFrom, ast.Global, ast.Nonlocal)
    forbidden_calls = {
        "breakpoint",
        "compile",
        "eval",
        "exec",
        "getattr",
        "globals",
        "input",
        "locals",
        "open",
        "setattr",
        "vars",
        "__import__",
    }
    for node in ast.walk(tree):
        if isinstance(node, forbidden_nodes):
            raise RuntimeError(
                f"Python oracle contains forbidden {type(node).__name__}"
            )
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name):
            if node.func.id in forbidden_calls:
                raise RuntimeError(
                    f"Python oracle calls forbidden function {node.func.id}"
                )
        if (
            isinstance(node, ast.Name)
            and isinstance(node.ctx, ast.Load)
            and node.id in forbidden_calls
        ):
            raise RuntimeError(
                f"Python oracle references forbidden function {node.id}"
            )
        if isinstance(node, ast.Attribute) and node.attr.startswith("__"):
            raise RuntimeError("Python oracle accesses a forbidden dunder attribute")
    classes = [
        node
        for node in tree.body
        if isinstance(node, ast.ClassDef) and node.name == "TopModule"
    ]
    if len(classes) != 1:
        raise RuntimeError("Python oracle must define exactly one TopModule class")
    methods = {
        node.name
        for node in classes[0].body
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
    }
    if "eval" not in methods:
        raise RuntimeError("Python oracle TopModule must define eval(self, inputs)")


def _validate_output_sample(
    value: Any,
    output_widths: dict[str, int],
    label: str,
) -> None:
    if not isinstance(value, dict) or set(value) != set(output_widths):
        raise RuntimeError(
            f"{label} must contain exactly these outputs: {sorted(output_widths)}"
        )
    for name, width in output_widths.items():
        sample = value[name]
        if isinstance(sample, bool) or not isinstance(sample, int):
            raise RuntimeError(f"{label}.{name} must be an integer")
        if not 0 <= sample < (1 << width):
            raise RuntimeError(f"{label}.{name} does not fit its declared width")


def validate_expected_trace(
    spec: DesignSpec,
    plan: dict[str, Any],
    expected_trace: dict[str, Any],
    *,
    expected_oracle_sha256: str | None = None,
) -> dict[str, int]:
    """Validate that every planned sample has one unambiguous oracle result."""
    if not isinstance(plan, dict) or not isinstance(expected_trace, dict):
        raise RuntimeError("test plan and expected trace must be JSON objects")
    clock = plan.get("clock")
    if not isinstance(clock, dict) or clock.get("port") != spec.clock_port:
        raise RuntimeError("expected trace test plan has the wrong clock")
    if clock.get("sample_phase") != "post_rising_edge":
        raise RuntimeError("expected trace test plan has an unsupported sample phase")
    if clock.get("period_ns") != spec.clock_period_ns:
        raise RuntimeError("expected trace test plan has the wrong clock period")
    if plan.get("reset") != spec.reset:
        raise RuntimeError("expected trace test plan has the wrong reset contract")

    claimed_plan_hash = expected_trace.get("testplan_sha256")
    actual_plan_hash = content_hash(plan)
    if claimed_plan_hash != actual_plan_hash:
        raise RuntimeError(
            "expected trace testplan_sha256 does not match the supplied test plan"
        )
    claimed_oracle_hash = expected_trace.get("oracle_sha256")
    if not isinstance(claimed_oracle_hash, str) or not re.fullmatch(
        r"[0-9a-f]{64}", claimed_oracle_hash
    ):
        raise RuntimeError("expected trace has an invalid oracle_sha256")
    if (
        expected_oracle_sha256 is not None
        and claimed_oracle_hash != expected_oracle_sha256
    ):
        raise RuntimeError("expected trace was generated by a different Python oracle")

    scenarios = plan.get("scenarios")
    if not isinstance(scenarios, list) or not scenarios:
        raise RuntimeError("expected trace test plan must contain scenarios")
    rows = expected_trace.get("trace")
    if not isinstance(rows, list) or not rows:
        raise RuntimeError("expected trace contains no functional samples")
    setup_rows = expected_trace.get("setup_observations", [])
    if not isinstance(setup_rows, list):
        raise RuntimeError("expected trace setup_observations must be an array")

    data_ports = [
        port
        for port in spec.ports
        if port.direction == "input" and port.name != spec.clock_port
    ]
    input_masks = {port.name: (1 << port.width) - 1 for port in data_ports}
    output_widths = {
        port.name: port.width for port in spec.ports if port.direction == "output"
    }
    if not output_widths:
        raise RuntimeError("expected trace contract has no output ports")
    reset = spec.reset
    reset_cycles = plan.get("reset_cycles", 3)
    if (
        isinstance(reset_cycles, bool)
        or not isinstance(reset_cycles, int)
        or not 1 <= reset_cycles <= 10_000
    ):
        raise RuntimeError("expected trace test plan reset_cycles is invalid")

    state = {port.name: 0 for port in data_ports}
    functional_schedule: list[dict[str, Any]] = []
    setup_schedule: list[dict[str, Any]] = []
    ordinal = 0
    for scenario_index, scenario in enumerate(scenarios):
        if not isinstance(scenario, dict):
            raise RuntimeError(f"test plan scenario {scenario_index} is not an object")
        scenario_id = scenario.get("id")
        requirement_ids = scenario.get("requirement_ids", [])
        if not isinstance(scenario_id, str) or not isinstance(requirement_ids, list):
            raise RuntimeError(f"test plan scenario {scenario_index} has invalid identity")
        if scenario.get("reset_before", True) and reset:
            state = {port.name: 0 for port in data_ports}
            asserted = 0 if reset["active_low"] else 1
            state[reset["port"]] = asserted
            state[reset["port"]] = 1 - asserted
            setup_schedule.append(
                {
                    "scenario_index": scenario_index,
                    "scenario_id": scenario_id,
                    "requirement_ids": requirement_ids,
                    "inputs": dict(state),
                }
            )
        cycles = scenario.get("cycles")
        if not isinstance(cycles, list) or not cycles:
            raise RuntimeError(f"test plan scenario {scenario_id} has no cycles")
        cycle_index = 0
        for source_cycle in cycles:
            if not isinstance(source_cycle, dict):
                raise RuntimeError(f"test plan scenario {scenario_id} has an invalid cycle")
            drive = source_cycle.get("drive", {})
            if not isinstance(drive, dict):
                raise RuntimeError(f"test plan scenario {scenario_id} drive is invalid")
            for name, raw_value in drive.items():
                if name not in input_masks:
                    raise RuntimeError(
                        f"test plan scenario {scenario_id} drives unsupported input {name}"
                    )
                if isinstance(raw_value, bool) or not isinstance(raw_value, int):
                    raise RuntimeError(
                        f"test plan scenario {scenario_id} drive for {name} is invalid"
                    )
                if not 0 <= raw_value <= input_masks[name]:
                    raise RuntimeError(
                        f"test plan scenario {scenario_id} drive for {name} is out of range"
                    )
                state[name] = raw_value
            repeat = source_cycle.get("repeat", 1)
            if isinstance(repeat, bool) or not isinstance(repeat, int) or repeat < 1:
                raise RuntimeError(f"test plan scenario {scenario_id} repeat is invalid")
            for _ in range(repeat):
                functional_schedule.append(
                    {
                        "ordinal": ordinal,
                        "scenario_index": scenario_index,
                        "scenario_id": scenario_id,
                        "requirement_ids": requirement_ids,
                        "cycle": cycle_index,
                        "inputs": dict(state),
                    }
                )
                ordinal += 1
                cycle_index += 1

    if len(rows) != len(functional_schedule):
        raise RuntimeError(
            "expected trace functional sample count does not match the test plan: "
            f"expected {len(functional_schedule)}, got {len(rows)}"
        )
    identity_fields = (
        "ordinal",
        "scenario_index",
        "scenario_id",
        "requirement_ids",
        "cycle",
        "inputs",
    )
    for position, (row, scheduled) in enumerate(zip(rows, functional_schedule)):
        if not isinstance(row, dict):
            raise RuntimeError(f"expected trace row {position} is not an object")
        for field in identity_fields:
            if row.get(field) != scheduled[field]:
                raise RuntimeError(
                    f"expected trace row {position} has incorrect {field}"
                )
        _validate_output_sample(
            row.get("expected"), output_widths, f"expected trace row {position}.expected"
        )

    if len(setup_rows) != len(setup_schedule):
        raise RuntimeError(
            "expected trace reset setup count does not match the test plan: "
            f"expected {len(setup_schedule)}, got {len(setup_rows)}"
        )
    setup_samples = 0
    for position, (row, scheduled) in enumerate(zip(setup_rows, setup_schedule)):
        if not isinstance(row, dict):
            raise RuntimeError(f"reset setup row {position} is not an object")
        for field in ("scenario_index", "scenario_id", "requirement_ids", "inputs"):
            if row.get(field) != scheduled[field]:
                raise RuntimeError(f"reset setup row {position} has incorrect {field}")
        _validate_output_sample(
            row.get("prime_asserted_edge"),
            output_widths,
            f"reset setup row {position}.prime_asserted_edge",
        )
        _validate_output_sample(
            row.get("prime_deasserted_edge"),
            output_widths,
            f"reset setup row {position}.prime_deasserted_edge",
        )
        dirty_edges = row.get("dirty_edges")
        if not isinstance(dirty_edges, list) or len(dirty_edges) != RESET_DIRTY_CYCLES:
            raise RuntimeError(
                f"reset setup row {position} has the wrong dirty probe edge count"
            )
        for edge_index, sample in enumerate(dirty_edges):
            _validate_output_sample(
                sample,
                output_widths,
                f"reset setup row {position}.dirty_edges[{edge_index}]",
            )
        asserted_edges = row.get("asserted_edges")
        if not isinstance(asserted_edges, list) or len(asserted_edges) != reset_cycles:
            raise RuntimeError(
                f"reset setup row {position} has the wrong asserted edge count"
            )
        for edge_index, sample in enumerate(asserted_edges):
            _validate_output_sample(
                sample,
                output_widths,
                f"reset setup row {position}.asserted_edges[{edge_index}]",
            )
        _validate_output_sample(
            row.get("deasserted_edge"),
            output_widths,
            f"reset setup row {position}.deasserted_edge",
        )
        asynchronous = row.get("asynchronous_assertion")
        if reset and reset.get("asynchronous"):
            _validate_output_sample(
                asynchronous,
                output_widths,
                f"reset setup row {position}.asynchronous_assertion",
            )
            if asynchronous != asserted_edges[0]:
                raise RuntimeError(
                    f"reset setup row {position} asynchronous sample is inconsistent"
                )
            setup_samples += 1
        elif asynchronous is not None:
            raise RuntimeError(
                f"reset setup row {position} has an unexpected asynchronous sample"
            )
        setup_samples += 2 + len(dirty_edges) + len(asserted_edges) + 1

    sample_count = len(functional_schedule) + setup_samples
    return {
        "functional_sample_count": len(functional_schedule),
        "setup_sample_count": setup_samples,
        "sample_count": sample_count,
        "expected_check_count": sample_count * len(output_widths),
    }


REFERENCE_HARNESS = textwrap.dedent(
    r"""
    import json
    import sys

    __ORACLE_NAMESPACE_SETUP__
    oracle_path = sys.argv[1]
    oracle_source = open(oracle_path, encoding="utf-8").read()
    executed_lines = set()

    def trace_oracle(frame, event, arg):
        del arg
        if event == "line" and frame.f_code.co_filename == oracle_path:
            executed_lines.add(frame.f_lineno)
        return trace_oracle

    sys.settrace(trace_oracle)
    exec(compile(oracle_source, oracle_path, "exec"), namespace)
    top = namespace.get("TopModule")
    if top is None:
        raise RuntimeError("oracle does not define TopModule")
    plan = json.load(open(sys.argv[2], encoding="utf-8"))
    ports = json.load(open(sys.argv[3], encoding="utf-8"))
    outputs = {row["name"]: int(row["width"]) for row in ports if row["direction"] == "output"}
    clock = plan["clock"]["port"]
    reset = plan.get("reset")
    reset_port = reset["port"] if reset else None
    data_ports = [row for row in ports if row["direction"] == "input" and row["name"] != clock]
    masks = {row["name"]: (1 << int(row["width"])) - 1 for row in data_ports}
    reset_dirty_cycles = 2

    def normalize(row):
        if not isinstance(row, dict):
            raise TypeError("TopModule.eval() must return a dict")
        expected = {}
        for name, width in outputs.items():
            if name not in row or row[name] is None:
                raise ValueError("oracle omitted output " + name)
            expected[name] = int(row[name]) & ((1 << width) - 1)
        return expected

    dut = top()
    state = {row["name"]: 0 for row in data_ports}
    trace = []
    setup_observations = []
    ordinal = 0
    for scenario_index, scenario in enumerate(plan["scenarios"]):
        if scenario.get("reset_before", True) and reset:
            state = {row["name"]: 0 for row in data_ports}
            asserted = 0 if reset["active_low"] else 1
            deasserted = 1 - asserted
            state[reset_port] = asserted
            prime_asserted_edge = normalize(dut.eval(dict(state)))
            state[reset_port] = deasserted
            prime_deasserted_edge = normalize(dut.eval(dict(state)))
            for row in data_ports:
                if row["name"] != reset_port:
                    state[row["name"]] = masks[row["name"]]
            dirty_edges = [
                normalize(dut.eval(dict(state))) for _ in range(reset_dirty_cycles)
            ]
            state = {row["name"]: 0 for row in data_ports}
            state[reset_port] = asserted
            asserted_edges = []
            for _ in range(int(plan.get("reset_cycles", 3))):
                asserted_edges.append(normalize(dut.eval(dict(state))))
            state[reset_port] = deasserted
            deasserted_edge = normalize(dut.eval(dict(state)))
            setup_observations.append({
                "scenario_index": scenario_index,
                "scenario_id": scenario["id"],
                "requirement_ids": scenario.get("requirement_ids", []),
                "inputs": dict(state),
                "prime_asserted_edge": prime_asserted_edge,
                "prime_deasserted_edge": prime_deasserted_edge,
                "dirty_edges": dirty_edges,
                "asynchronous_assertion": asserted_edges[0]
                    if reset.get("asynchronous") and asserted_edges else None,
                "asserted_edges": asserted_edges,
                "deasserted_edge": deasserted_edge,
            })
        cycle_index = 0
        for source_cycle in scenario["cycles"]:
            for name, value in source_cycle.get("drive", {}).items():
                state[name] = int(value) & masks[name]
            for _ in range(int(source_cycle.get("repeat", 1))):
                expected = normalize(dut.eval(dict(state)))
                trace.append({
                    "ordinal": ordinal,
                    "scenario_index": scenario_index,
                    "scenario_id": scenario["id"],
                    "requirement_ids": scenario.get("requirement_ids", []),
                    "cycle": cycle_index,
                    "inputs": dict(state),
                    "expected": expected,
                })
                ordinal += 1
                cycle_index += 1
    sys.settrace(None)
    print("REFERENCE_RESULTS " + json.dumps({
        "trace": trace,
        "setup_observations": setup_observations,
        "oracle_executed_lines": sorted(executed_lines),
    }, sort_keys=True))
    """
).lstrip().replace("__ORACLE_NAMESPACE_SETUP__", ORACLE_NAMESPACE_SETUP)


COCOTB_DRIVER = textwrap.dedent(
    r"""
    import json
    import os
    from pathlib import Path

    import cocotb
    from cocotb.clock import Clock
    from cocotb.triggers import FallingEdge, ReadOnly, RisingEdge, Timer


    def _read_unsigned(handle):
        bits = str(handle.value).lower()
        if "x" in bits or "z" in bits:
            return None, bits
        try:
            return int(handle.value), bits
        except (ValueError, TypeError, OverflowError):
            return None, bits


    @cocotb.test()
    async def verify_frozen_plan(dut):
        root = Path(os.environ["RTL_CLOSURE_WORK"])
        plan = json.loads((root / "testplan.json").read_text())
        expected_data = json.loads((root / "expected-trace.json").read_text())
        expected_rows = expected_data["trace"]
        ports = json.loads((root / "ports.json").read_text())
        debug_names = json.loads((root / "debug-signals.json").read_text())
        clock_name = plan["clock"]["port"]
        clock = getattr(dut, clock_name)
        reset = plan.get("reset")
        output_ports = [row for row in ports if row["direction"] == "output"]
        input_ports = [
            row for row in ports
            if row["direction"] == "input" and row["name"] != clock_name
        ]
        debug_handles = {}
        unavailable_debug_signals = []
        for name in debug_names:
            try:
                debug_handles[name] = getattr(dut, name)
            except AttributeError:
                unavailable_debug_signals.append(name)
        state = {row["name"]: 0 for row in input_ports}
        if reset:
            state[reset["port"]] = 1 if reset["active_low"] else 0
        for name, value in state.items():
            getattr(dut, name).value = value
        clock.value = 0
        clock_task = cocotb.start_soon(
            Clock(clock, plan["clock"]["period_ns"], unit="ns").start(
                start_high=False
            )
        )
        await Timer(1, unit="ps")

        expected_by_scenario = {}
        for row in expected_rows:
            expected_by_scenario.setdefault(row["scenario_index"], []).append(row)
        setup_by_scenario = {
            row["scenario_index"]: row
            for row in expected_data.get("setup_observations", [])
        }
        mismatches = []
        actual_trace = []
        passed_check_ids = []
        total_checks = 0
        ordinal = 0
        initial_low_phase = True

        async def drive_at_low():
            nonlocal initial_low_phase
            if initial_low_phase:
                initial_low_phase = False
            else:
                await FallingEdge(clock)
            for name, value in state.items():
                getattr(dut, name).value = value

        async def observe(scenario_index, scenario, expected, phase, cycle):
            nonlocal total_checks, ordinal
            await ReadOnly()
            actual = {}
            actual_bits = {}
            internal = {}
            internal_bits = {}
            for port in output_ports:
                signal = port["name"]
                value, bits = _read_unsigned(getattr(dut, signal))
                actual[signal] = value
                actual_bits[signal] = bits
                expected_value = int(expected[signal]) & ((1 << int(port["width"])) - 1)
                check_id = (
                    f"{scenario_index}:{scenario['id']}:{phase}:{cycle}:{signal}"
                )
                total_checks += 1
                if value == expected_value:
                    passed_check_ids.append(check_id)
                elif len(mismatches) < 256:
                    failure_class = (
                        "unknown_value" if value is None
                        else "reset_mismatch" if phase.startswith("reset_")
                        else "value_mismatch"
                    )
                    mismatches.append({
                        "check_id": check_id,
                        "ordinal": ordinal,
                        "scenario_index": scenario_index,
                        "scenario_id": scenario["id"],
                        "requirement_ids": scenario.get("requirement_ids", []),
                        "cycle": cycle,
                        "phase": phase,
                        "signal": signal,
                        "expected": expected_value,
                        "actual": value,
                        "actual_bits": bits,
                        "inputs": dict(state),
                        "failure_class": failure_class,
                    })
            for signal, handle in debug_handles.items():
                value, bits = _read_unsigned(handle)
                internal[signal] = value
                internal_bits[signal] = bits
            actual_trace.append({
                "ordinal": ordinal,
                "scenario_index": scenario_index,
                "scenario_id": scenario["id"],
                "cycle": cycle,
                "phase": phase,
                "inputs": dict(state),
                "expected": expected,
                "actual": actual,
                "actual_bits": actual_bits,
                "internal": internal,
                "internal_bits": internal_bits,
            })
            ordinal += 1

        try:
            for scenario_index, scenario in enumerate(plan["scenarios"]):
                rows = expected_by_scenario.get(scenario_index, [])
                if scenario.get("reset_before", True) and reset:
                    setup = setup_by_scenario[scenario_index]
                    state = {row["name"]: 0 for row in input_ports}
                    asserted = 0 if reset["active_low"] else 1
                    deasserted = 1 - asserted
                    state[reset["port"]] = asserted
                    await drive_at_low()
                    await RisingEdge(clock)
                    await observe(
                        scenario_index,
                        scenario,
                        setup["prime_asserted_edge"],
                        "reset_prime_asserted",
                        -int(plan.get("reset_cycles", 3))
                        - len(setup["dirty_edges"])
                        - 3,
                    )
                    state[reset["port"]] = deasserted
                    await drive_at_low()
                    await RisingEdge(clock)
                    await observe(
                        scenario_index,
                        scenario,
                        setup["prime_deasserted_edge"],
                        "reset_prime_deasserted",
                        -int(plan.get("reset_cycles", 3))
                        - len(setup["dirty_edges"])
                        - 2,
                    )
                    for port in input_ports:
                        if port["name"] != reset["port"]:
                            state[port["name"]] = (1 << int(port["width"])) - 1
                    for dirty_index, expected in enumerate(setup["dirty_edges"]):
                        await drive_at_low()
                        await RisingEdge(clock)
                        await observe(
                            scenario_index,
                            scenario,
                            expected,
                            "probe_dirty",
                            -int(plan.get("reset_cycles", 3))
                            - len(setup["dirty_edges"])
                            - 1
                            + dirty_index,
                        )
                    state = {row["name"]: 0 for row in input_ports}
                    state[reset["port"]] = asserted
                    await drive_at_low()
                    if reset.get("asynchronous"):
                        await Timer(plan["clock"]["period_ns"] / 4, unit="ns")
                        await observe(
                            scenario_index,
                            scenario,
                            setup["asynchronous_assertion"],
                            "reset_async_assertion",
                            -int(plan.get("reset_cycles", 3)) - 1,
                        )
                    for reset_index, expected in enumerate(setup["asserted_edges"]):
                        await RisingEdge(clock)
                        await observe(
                            scenario_index,
                            scenario,
                            expected,
                            "reset_asserted",
                            -len(setup["asserted_edges"]) + reset_index,
                        )
                    state[reset["port"]] = deasserted
                    await drive_at_low()
                    await RisingEdge(clock)
                    await observe(
                        scenario_index,
                        scenario,
                        setup["deasserted_edge"],
                        "reset_deasserted",
                        0,
                    )
                for row in rows:
                    state.update(row["inputs"])
                    await drive_at_low()
                    await RisingEdge(clock)
                    await observe(
                        scenario_index,
                        scenario,
                        row["expected"],
                        "post_rising_edge",
                        row["cycle"],
                    )
        finally:
            clock_task.cancel()
        result = {
            "passed": not mismatches,
            "failure_class": None if not mismatches else mismatches[0]["failure_class"],
            "total_checks": total_checks,
            "mismatched": total_checks - len(passed_check_ids),
            "first_failure_ordinal": None if not mismatches else mismatches[0]["ordinal"],
            "mismatches": mismatches,
            "passed_check_ids": passed_check_ids,
            "actual_trace": actual_trace,
            "observed_internal_signals": sorted(debug_handles),
            "unavailable_internal_signals": sorted(unavailable_debug_signals),
        }
        (root / "verification-result.json").write_text(
            json.dumps(result, indent=2, sort_keys=True) + "\n"
        )
        assert not mismatches, (
            f"{len(mismatches)} sampled mismatches; first={mismatches[0] if mismatches else None}"
        )
    """
).lstrip()


COCOTB_RUNNER = textwrap.dedent(
    r"""
    import os
    import sys
    from pathlib import Path

    from cocotb_tools.runner import get_runner

    root = Path("/work")
    os.environ["RTL_CLOSURE_WORK"] = str(root)
    os.environ["COCOTB_RESULTS_FILE"] = str(root / "results.xml")
    simulator = os.environ.get("RTL_CLOSURE_SIMULATOR", "icarus")
    runner = get_runner(simulator)
    try:
        runner.build(
            sources=[root / "dut.sv"],
            hdl_toplevel=os.environ["RTL_CLOSURE_TOP"],
            build_dir=root / "sim-build",
            build_args=["-g2012"],
            always=True,
            waves=True,
        )
        runner.test(
            hdl_toplevel=os.environ["RTL_CLOSURE_TOP"],
            test_module="cocotb_driver",
            test_dir=root,
            build_dir=root / "sim-build",
            waves=True,
        )
    except BaseException as exc:
        print(f"COCOTB_RUNNER_ERROR: {type(exc).__name__}: {exc}", file=sys.stderr)
        raise
    """
).lstrip()


def _run(command: list[str], timeout: int) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        command,
        capture_output=True,
        text=True,
        errors="replace",
        timeout=timeout,
        check=False,
    )


def _force_remove_container(name: str) -> None:
    try:
        subprocess.run(
            ["docker", "rm", "-f", name],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=15,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        pass


def _run_container(
    command: list[str], timeout: int, *, purpose: str
) -> subprocess.CompletedProcess[str]:
    if command[:2] != ["docker", "run"]:
        raise ValueError("container command must start with docker run")
    name = f"rtl-closure-{purpose}-{os.getpid()}-{uuid.uuid4().hex[:12]}"
    named_command = command[:2] + ["--name", name] + command[2:]
    try:
        try:
            completed = _run(named_command, timeout)
        except OSError as exc:
            raise ContainerInfrastructureError(
                f"Docker could not launch {purpose}: {exc}", provider=purpose
            ) from exc
        runtime_log = (completed.stdout + "\n" + completed.stderr).lower()
        if completed.returncode in {125, 126, 127, 137} or any(
            marker in runtime_log
            for marker in (
                "cannot connect to the docker daemon",
                "error during connect",
                "docker: error response from daemon",
                "no space left on device",
            )
        ):
            detail = (completed.stderr or completed.stdout).strip()[-2000:]
            raise ContainerInfrastructureError(
                f"Docker failed while running {purpose} (exit {completed.returncode}): {detail}",
                provider=purpose,
            )
        return completed
    finally:
        # Killing the docker CLI on timeout does not stop the daemon-owned
        # container.  Explicit removal prevents hung simulations accumulating.
        _force_remove_container(name)


def generate_expected_trace(
    spec: DesignSpec,
    python_model: str,
    plan: dict[str, Any],
    artifact_dir: Path,
) -> dict[str, Any]:
    check_oracle_policy(python_model)
    work = artifact_dir / "oracle-execution"
    work.mkdir(parents=True, exist_ok=True)
    ports = [
        {
            "name": port.name,
            "direction": port.direction,
            "width": port.width,
            "signed": port.signed,
        }
        for port in spec.ports
    ]
    (work / "oracle.py").write_text(python_model, encoding="utf-8")
    (work / "harness.py").write_text(REFERENCE_HARNESS, encoding="utf-8")
    (work / "testplan.json").write_text(json.dumps(plan), encoding="utf-8")
    (work / "ports.json").write_text(json.dumps(ports), encoding="utf-8")
    command = _docker_base(work, read_only_mount=True) + [
        "-e",
        "PYTHONHASHSEED=0",
        "--entrypoint",
        "python3",
        sandbox_image_ref(),
        "harness.py",
        "oracle.py",
        "testplan.json",
        "ports.json",
    ]
    try:
        completed = _run_container(command, 120, purpose="oracle")
    except subprocess.TimeoutExpired as exc:
        raise ContainerInfrastructureError(
            "Python oracle execution exceeded 120 seconds",
            provider="oracle",
        ) from exc
    log = (completed.stdout + "\n" + completed.stderr).strip()
    (artifact_dir / "oracle-execution.log").write_text(log + "\n", encoding="utf-8")
    if completed.returncode != 0:
        raise RuntimeError(f"Python oracle execution failed: {log[-2500:]}")
    for line in completed.stdout.splitlines():
        if not line.startswith("REFERENCE_RESULTS "):
            continue
        try:
            result = json.loads(line[len("REFERENCE_RESULTS ") :])
        except json.JSONDecodeError as exc:
            raise RuntimeError(f"Python oracle returned invalid trace JSON: {exc}") from exc
        if not isinstance(result.get("trace"), list) or not result["trace"]:
            raise RuntimeError("Python oracle produced an empty expected trace")
        executable = oracle_executable_lines(python_model)
        executed = sorted(set(result.pop("oracle_executed_lines", [])) & set(executable))
        missing = sorted(set(executable) - set(executed))
        line_coverage = {
            "metric": "oracle_line_coverage",
            "coverage_type": "python_source_line",
            "scope": "TopModule executable statement lines",
            "executable_lines": executable,
            "executed_lines": executed,
            "missing_lines": missing,
            "covered": len(executed),
            "total": len(executable),
            "percent": round(100.0 * len(executed) / len(executable), 3)
            if executable
            else 100.0,
        }
        result["oracle_line_coverage"] = dict(line_coverage)
        result["oracle_sha256"] = content_hash(python_model)
        result["testplan_sha256"] = content_hash(plan)
        validate_expected_trace(
            spec,
            plan,
            result,
            expected_oracle_sha256=content_hash(python_model),
        )
        return result
    raise RuntimeError("Python oracle execution returned no expected trace")


def _failure_result(failure_class: str, detail: str) -> dict[str, Any]:
    return {
        "passed": False,
        "failure_class": failure_class,
        "detail": detail[-4000:],
        "total_checks": 0,
        "mismatched": 1,
        "first_failure_ordinal": 0,
        "mismatches": [],
        "passed_check_ids": [],
        "actual_trace": [],
    }


def verify_rtl(
    spec: DesignSpec,
    rtl: str,
    plan: dict[str, Any],
    expected_trace: dict[str, Any],
    artifact_dir: Path,
    *,
    timeout: int = 180,
    debug_signals: list[str] | None = None,
    simulator: str = "icarus",
) -> dict[str, Any]:
    trace_summary = validate_expected_trace(spec, plan, expected_trace)
    work = artifact_dir / "cocotb"
    if work.is_symlink():
        raise RuntimeError(f"verification work directory must not be a symlink: {work}")
    if work.exists():
        shutil.rmtree(work)
    work.mkdir(parents=True, exist_ok=True)
    ports = [
        {
            "name": port.name,
            "direction": port.direction,
            "width": port.width,
            "signed": port.signed,
        }
        for port in spec.ports
    ]
    files = {
        # Testbench timing is a verification concern.  Supplying a local
        # timescale avoids inheriting Icarus' 1-second default without changing
        # the RTL that is later sent to synthesis.
        "dut.sv": "`timescale 1ns/1ps\n" + rtl,
        "testplan.json": json.dumps(plan, indent=2),
        "expected-trace.json": json.dumps(expected_trace, indent=2),
        "ports.json": json.dumps(ports, indent=2),
        "debug-signals.json": json.dumps(sorted(set(debug_signals or [])), indent=2),
        "cocotb_driver.py": COCOTB_DRIVER,
        "runner.py": COCOTB_RUNNER,
    }
    for name, content in files.items():
        (work / name).write_text(content + ("\n" if not content.endswith("\n") else ""), encoding="utf-8")
    command = _docker_base(work, read_only_mount=False) + [
        "-e",
        f"RTL_CLOSURE_TOP={spec.name}",
        "-e",
        f"RTL_CLOSURE_SIMULATOR={simulator}",
        "--entrypoint",
        VERIFY_PYTHON,
        verify_image_ref(),
        "runner.py",
    ]
    try:
        completed = _run_container(command, timeout, purpose="verify")
    except subprocess.TimeoutExpired as exc:
        raise ContainerInfrastructureError(
            f"Cocotb verification exceeded {timeout} seconds",
            provider="verify",
        ) from exc
    log = (completed.stdout + "\n" + completed.stderr).strip()
    (artifact_dir / "cocotb.log").write_text(log + "\n", encoding="utf-8")
    result_path = work / "verification-result.json"
    parsed_runner_result = False
    if result_path.is_file():
        try:
            result = json.loads(result_path.read_text(encoding="utf-8"))
            if not isinstance(result, dict):
                raise ValueError("verification result root must be a JSON object")
            parsed_runner_result = True
        except (json.JSONDecodeError, ValueError) as exc:
            result = _failure_result("result_parse_error", str(exc))
    else:
        failure_class = "compile_error" if "iverilog" in log.lower() else "simulation_error"
        result = _failure_result(failure_class, log)
    if parsed_runner_result:
        try:
            total_checks = result.get("total_checks")
            passed_ids = result.get("passed_check_ids")
            actual_trace = result.get("actual_trace")
            mismatched = result.get("mismatched")
            if total_checks != trace_summary["expected_check_count"]:
                raise ValueError(
                    "verification result check count does not cover the complete trace"
                )
            if not isinstance(passed_ids, list) or len(passed_ids) != len(set(passed_ids)):
                raise ValueError("verification result has invalid passed check IDs")
            if not isinstance(actual_trace, list) or len(actual_trace) != trace_summary[
                "sample_count"
            ]:
                raise ValueError(
                    "verification result actual trace does not cover every expected sample"
                )
            if mismatched != total_checks - len(passed_ids):
                raise ValueError("verification result mismatch count is inconsistent")
            if result.get("passed") is True and (mismatched != 0 or result.get("mismatches")):
                raise ValueError("verification result claims pass despite mismatches")
        except (TypeError, ValueError) as exc:
            result = _failure_result("result_parse_error", str(exc))
    if completed.returncode != 0 and result.get("passed"):
        result = _failure_result(
            "simulation_error",
            f"Cocotb runner exited with code {completed.returncode}: {log}",
        )
    result["returncode"] = completed.returncode
    result["rtl_sha256"] = content_hash(rtl)
    result["suite_sha256"] = suite_hash(plan, expected_trace)
    result["verify_image"] = verify_image_ref()
    result["coverage"] = behavioral_coverage(
        {
            "requirements": spec.verification.get("requirements", []),
            "ports": [
                {
                    "name": port.name,
                    "direction": port.direction,
                    "width": port.width,
                }
                for port in spec.ports
            ],
        },
        plan,
        result,
    )
    result["artifacts"] = {
        "directory": str(artifact_dir),
        "log": str(artifact_dir / "cocotb.log"),
        "waveform": next(
            (str(path) for path in work.rglob("*.*") if path.suffix in {".fst", ".vcd"}),
            None,
        ),
        "simulator": simulator,
    }
    (artifact_dir / "verification-result.json").write_text(
        json.dumps(result, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    return result
