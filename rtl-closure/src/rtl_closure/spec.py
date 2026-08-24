"""Structured natural-language design contract and LibreLane rendering."""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml

from .universal import UniversalPropertyError, normalize_universal_properties


IDENT_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
OPEN_PDKS = {"sky130A", "gf180mcuD", "ihp-sg13g2"}
ROOT_FIELDS = {
    "schema_version",
    "design",
    "flow",
    "generation",
    "verification",
    "intent_closure",
}
INTENT_CLOSURE_FIELDS = {
    "mode",
    "user_confirmed",
    "unresolved_questions",
    "assumptions",
    "confirmed_sha256",
    "binding_sha256",
    "closure_satisfied",
}
DESIGN_FIELDS = {
    "name",
    "description",
    "pdk",
    "scl",
    "ports",
    "clock",
    "reset",
    "architecture",
}
ARCHITECTURE_FIELDS = {
    "required_modules",
    "forbidden_operators",
    "min_sequential_bits",
    "max_sequential_bits",
    "max_synth_cells",
}
PORT_FIELDS = {"name", "direction", "width", "signed"}
CLOCK_FIELDS = {"port", "period_ns"}
RESET_FIELDS = {"port", "active_low", "asynchronous"}
FLOW_FIELDS = {
    "name",
    "to",
    "mapped_equivalence_mode",
    "floorplan_sizing",
    "core_utilization",
    "place_density_pct",
    "die_area",
    "core_area",
    "jobs",
    "physical_planning_mode",
    "physical_planning_policy",
    "physical_planning_policy_sha256",
    "reference_geometry_lock",
    "reference_geometry_lock_sha256",
}
GENERATION_FIELDS = {
    "profile",
    "model",
    "n",
    "max_turns",
    "num_verify_tests",
    "independent_verify_tests",
    "temperature",
    "seed",
    "reasoning_effort",
}
VERIFICATION_FIELDS = {
    "requirements",
    "directed_tests",
    "max_repair_rounds",
    "repair_candidates",
    "stagnation_limit",
    "trace_window_cycles",
    "max_counterexamples",
    "directed_test_budget",
    "random_regression_cycles",
    "oracle_coverage_target_pct",
    "oracle_coverage_rounds",
    "oracle_coverage_cycle_budget",
    "oracle_line_coverage_mode",
    "oracle_diversity_mode",
    "oracle_min_distinct_sources",
    "adaptive_tests_per_round",
    "adaptive_cycle_budget",
    "internal_trace_signals",
    "formal_mode",
    "formal_depth",
    "formal_timeout_seconds",
    "formal_total_timeout_seconds",
    "verilator_lint",
    "universal_mode",
    "universal_properties",
    "universal_induction_depth",
    "universal_timeout_seconds",
}
REQUIREMENT_FIELDS = {"id", "text", "kind", "signals", "latency_cycles"}
DIRECTED_TEST_FIELDS = {
    "id",
    "description",
    "requirement_ids",
    "reset_before",
    "cycles",
}
CYCLE_FIELDS = {"drive", "repeat"}
class SpecError(ValueError):
    """Raised when a design specification is incomplete or unsafe."""


@dataclass(frozen=True)
class Port:
    name: str
    direction: str
    width: int
    signed: bool = False

    @property
    def declaration(self) -> str:
        signed = " signed" if self.signed else ""
        width = f" [{self.width - 1}:0]" if self.width > 1 else ""
        return f"{self.direction} wire{signed}{width} {self.name}"


@dataclass(frozen=True)
class DesignSpec:
    raw: dict[str, Any]
    name: str
    description: str
    pdk: str
    scl: str | None
    ports: tuple[Port, ...]
    clock_port: str
    clock_period_ns: float
    reset: dict[str, Any] | None
    architecture: dict[str, Any]
    flow: dict[str, Any]
    generation: dict[str, Any]
    verification: dict[str, Any]
    intent_closure: dict[str, Any]

    @property
    def design_slug_prefix(self) -> str:
        normalized = self.name.lower()
        if normalized.startswith("rtl_closure_"):
            return normalized
        return f"rtl_closure_{normalized}"


def _object(value: Any, label: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise SpecError(f"{label} must be a JSON object")
    return value


def _identifier(value: Any, label: str) -> str:
    if (
        not isinstance(value, str)
        or len(value) > 128
        or not IDENT_RE.fullmatch(value)
    ):
        raise SpecError(f"{label} must be a Verilog identifier of at most 128 characters")
    return value


def _bounded_number(value: Any, label: str, low: float, high: float) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise SpecError(f"{label} must be numeric")
    number = float(value)
    if not low <= number <= high:
        raise SpecError(f"{label} must be between {low} and {high}")
    return number


def _area(value: Any, label: str) -> list[float]:
    if not isinstance(value, list) or len(value) != 4:
        raise SpecError(f"{label} must be [x1, y1, x2, y2]")
    area = [
        _bounded_number(item, f"{label}[{index}]", 0, 1_000_000)
        for index, item in enumerate(value)
    ]
    if area[2] <= area[0] or area[3] <= area[1]:
        raise SpecError(f"{label} upper-right coordinates must exceed lower-left coordinates")
    return area


def _reject_secrets(value: Any, path: str = "spec") -> None:
    if isinstance(value, dict):
        for key, child in value.items():
            lowered = str(key).lower()
            if any(token in lowered for token in ("api_key", "apikey", "secret", "password", "token")):
                raise SpecError(f"secret-bearing field is forbidden: {path}.{key}")
            _reject_secrets(child, f"{path}.{key}")
    elif isinstance(value, list):
        for index, child in enumerate(value):
            _reject_secrets(child, f"{path}[{index}]")


def _reject_unknown(value: dict[str, Any], allowed: set[str], label: str) -> None:
    unknown = sorted(set(value) - allowed)
    if unknown:
        raise SpecError(f"unsupported {label} field(s): {', '.join(unknown)}")


def _short_text(value: Any, label: str, *, minimum: int = 1) -> str:
    if not isinstance(value, str):
        raise SpecError(f"{label} must be a string")
    rendered = value.strip()
    if not minimum <= len(rendered) <= 4096:
        raise SpecError(f"{label} must contain {minimum} to 4096 characters")
    return rendered


def _canonical_hash(value: Any) -> str:
    payload = json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _intent_binding_payload(
    *,
    name: str,
    description: str,
    ports: tuple[Port, ...],
    clock_port: str,
    clock_period_ns: float,
    reset: dict[str, Any] | None,
    architecture: dict[str, Any],
    requirements: list[dict[str, Any]],
    assumptions: list[str],
) -> dict[str, Any]:
    """Return the exact user-review surface covered by an intent signature."""
    return {
        "description": description,
        "interface": {
            "design": name,
            "ports": [
                {
                    "name": port.name,
                    "direction": port.direction,
                    "width": port.width,
                    "signed": port.signed,
                }
                for port in ports
            ],
            "clock": {"port": clock_port, "period_ns": clock_period_ns},
            "reset": reset,
        },
        "architecture": architecture,
        "requirements": requirements,
        "assumptions": assumptions,
    }


def _text_array(value: Any, label: str, *, maximum: int = 64) -> list[str]:
    if not isinstance(value, list) or len(value) > maximum:
        raise SpecError(f"{label} must be an array of at most {maximum} strings")
    return [
        _short_text(item, f"{label}[{index}]") for index, item in enumerate(value)
    ]


def _intent_closure_contract(
    value: Any,
    *,
    name: str,
    description: str,
    ports: tuple[Port, ...],
    clock_port: str,
    clock_period_ns: float,
    reset: dict[str, Any] | None,
    architecture: dict[str, Any],
    requirements: list[dict[str, Any]],
) -> dict[str, Any]:
    closure = dict(_object(value, "intent_closure"))
    _reject_unknown(closure, INTENT_CLOSURE_FIELDS, "intent_closure")
    mode = closure.get("mode", "advisory")
    if mode not in {"off", "advisory", "required"}:
        raise SpecError("intent_closure.mode must be off, advisory, or required")
    user_confirmed = closure.get("user_confirmed", False)
    if not isinstance(user_confirmed, bool):
        raise SpecError("intent_closure.user_confirmed must be boolean")
    unresolved = _text_array(
        closure.get("unresolved_questions", []),
        "intent_closure.unresolved_questions",
    )
    assumptions = _text_array(
        closure.get("assumptions", []),
        "intent_closure.assumptions",
    )
    confirmed_sha256 = closure.get("confirmed_sha256")
    if confirmed_sha256 is not None and (
        not isinstance(confirmed_sha256, str)
        or not re.fullmatch(r"[0-9a-f]{64}", confirmed_sha256)
    ):
        raise SpecError(
            "intent_closure.confirmed_sha256 must be a lowercase SHA-256 digest"
        )
    payload = _intent_binding_payload(
        name=name,
        description=description,
        ports=ports,
        clock_port=clock_port,
        clock_period_ns=clock_period_ns,
        reset=reset,
        architecture=architecture,
        requirements=requirements,
        assumptions=assumptions,
    )
    binding_sha256 = _canonical_hash(payload)
    closure_satisfied = (
        user_confirmed
        and not unresolved
        and confirmed_sha256 == binding_sha256
        and bool(requirements)
    )
    if mode == "required" and not requirements:
        raise SpecError(
            "intent_closure required needs explicit verification.requirements; "
            "model-planned requirements cannot be covered by prior user confirmation"
        )
    if mode == "required" and unresolved:
        raise SpecError(
            "intent_closure required needs unresolved_questions to be empty"
        )
    if mode == "required" and not user_confirmed:
        raise SpecError("intent_closure required needs user_confirmed=true")
    if mode == "required" and confirmed_sha256 != binding_sha256:
        raise SpecError(
            "intent_closure.confirmed_sha256 does not bind the current description, "
            "interface, normalized architecture, requirements, and assumptions; "
            f"expected {binding_sha256}"
        )
    return {
        "mode": mode,
        "user_confirmed": user_confirmed,
        "unresolved_questions": unresolved,
        "assumptions": assumptions,
        "confirmed_sha256": confirmed_sha256,
        "binding_sha256": binding_sha256,
        "closure_satisfied": closure_satisfied,
    }


def _architecture_contract(value: Any) -> dict[str, Any]:
    architecture = dict(_object(value, "design.architecture"))
    _reject_unknown(architecture, ARCHITECTURE_FIELDS, "design.architecture")

    modules = architecture.get("required_modules", [])
    if not isinstance(modules, list) or len(modules) > 64:
        raise SpecError(
            "design.architecture.required_modules must be an array of at most 64 module names"
        )
    normalized_modules = [
        _identifier(name, "design.architecture.required_modules[]")
        for name in modules
    ]
    if len(set(normalized_modules)) != len(normalized_modules):
        raise SpecError("design.architecture.required_modules contains duplicates")

    operators = architecture.get("forbidden_operators", [])
    allowed_operators = {"multiply", "divide", "modulo"}
    if not isinstance(operators, list) or len(operators) > len(allowed_operators):
        raise SpecError(
            "design.architecture.forbidden_operators must be an array"
        )
    if any(operator not in allowed_operators for operator in operators):
        raise SpecError(
            "design.architecture.forbidden_operators accepts only multiply, divide, and modulo"
        )
    if len(set(operators)) != len(operators):
        raise SpecError("design.architecture.forbidden_operators contains duplicates")

    normalized: dict[str, Any] = {}
    if normalized_modules:
        normalized["required_modules"] = normalized_modules
    if operators:
        normalized["forbidden_operators"] = list(operators)
    for field, low, high in (
        ("min_sequential_bits", 0, 1_000_000),
        ("max_sequential_bits", 0, 1_000_000),
        ("max_synth_cells", 1, 10_000_000),
    ):
        if field not in architecture:
            continue
        number = architecture[field]
        if isinstance(number, bool) or not isinstance(number, int) or not low <= number <= high:
            raise SpecError(
                f"design.architecture.{field} must be an integer from {low} to {high}"
            )
        normalized[field] = number
    if normalized.get("min_sequential_bits", 0) > normalized.get(
        "max_sequential_bits", 1_000_000
    ):
        raise SpecError(
            "design.architecture.min_sequential_bits must not exceed max_sequential_bits"
        )
    if (
        "min_sequential_bits" in normalized
        and "max_sequential_bits" in normalized
        and normalized["min_sequential_bits"] == normalized["max_sequential_bits"]
    ):
        raise SpecError(
            "design.architecture synthesized sequential-bit bounds must describe "
            "a range; exact post-synthesis counts are tool-transformation dependent"
        )
    return normalized


def _verification_contract(
    value: Any,
    *,
    ports: tuple[Port, ...],
    clock_port: str,
    reset: dict[str, Any] | None,
) -> dict[str, Any]:
    verification = dict(_object(value, "verification"))
    _reject_unknown(verification, VERIFICATION_FIELDS, "verification")
    defaults = {
        "max_repair_rounds": 6,
        "repair_candidates": 3,
        "stagnation_limit": 2,
        "trace_window_cycles": 4,
        "max_counterexamples": 64,
        "directed_test_budget": 32,
        "random_regression_cycles": 300,
        "oracle_coverage_target_pct": 85,
        "oracle_coverage_rounds": 2,
        "oracle_coverage_cycle_budget": 128,
        "oracle_line_coverage_mode": "advisory",
        "oracle_diversity_mode": "advisory",
        "oracle_min_distinct_sources": 2,
        "adaptive_tests_per_round": 4,
        "adaptive_cycle_budget": 64,
        "internal_trace_signals": 32,
        "formal_mode": "advisory",
        "formal_depth": 32,
        "formal_timeout_seconds": 120,
        "formal_total_timeout_seconds": 3600,
        "verilator_lint": True,
        "universal_mode": "off",
        "universal_properties": [],
        "universal_induction_depth": 8,
        "universal_timeout_seconds": 120,
    }
    for key, default in defaults.items():
        verification.setdefault(key, default)
    bounds = {
        "max_repair_rounds": (0, 20),
        "repair_candidates": (1, 8),
        "stagnation_limit": (1, 8),
        "trace_window_cycles": (1, 16),
        "max_counterexamples": (1, 256),
        "directed_test_budget": (1, 256),
        "random_regression_cycles": (0, 1024),
        "oracle_coverage_target_pct": (0, 100),
        "oracle_coverage_rounds": (0, 8),
        "oracle_coverage_cycle_budget": (0, 1024),
        "oracle_min_distinct_sources": (1, 10),
        "adaptive_tests_per_round": (0, 16),
        "adaptive_cycle_budget": (0, 1024),
        "internal_trace_signals": (0, 128),
        "formal_depth": (1, 256),
        "formal_timeout_seconds": (10, 900),
        "formal_total_timeout_seconds": (30, 14_400),
        "universal_induction_depth": (2, 64),
        "universal_timeout_seconds": (10, 900),
    }
    for field, (low, high) in bounds.items():
        item = verification[field]
        if isinstance(item, bool) or not isinstance(item, int) or not low <= item <= high:
            raise SpecError(
                f"verification.{field} must be an integer from {low} to {high}"
            )

    if verification["formal_mode"] not in {"off", "advisory", "required"}:
        raise SpecError(
            "verification.formal_mode must be off, advisory, or required"
        )
    if verification["universal_mode"] not in {"off", "advisory", "required"}:
        raise SpecError(
            "verification.universal_mode must be off, advisory, or required"
        )
    if verification["oracle_line_coverage_mode"] not in {
        "off",
        "advisory",
        "required",
    }:
        raise SpecError(
            "verification.oracle_line_coverage_mode must be off, advisory, or required"
        )
    if verification["oracle_diversity_mode"] not in {
        "off",
        "advisory",
        "required",
    }:
        raise SpecError(
            "verification.oracle_diversity_mode must be off, advisory, or required"
        )
    if (
        verification["oracle_diversity_mode"] == "required"
        and verification["oracle_min_distinct_sources"] < 2
    ):
        raise SpecError(
            "verification.oracle_diversity_mode required needs "
            "oracle_min_distinct_sources of at least 2"
        )
    if not isinstance(verification["verilator_lint"], bool):
        raise SpecError("verification.verilator_lint must be boolean")

    port_map = {port.name: port for port in ports}
    requirements_value = verification.get("requirements", [])
    if not isinstance(requirements_value, list) or len(requirements_value) > 128:
        raise SpecError("verification.requirements must be an array of at most 128 items")
    requirements: list[dict[str, Any]] = []
    requirement_ids: set[str] = set()
    for index, item_value in enumerate(requirements_value):
        item = _object(item_value, f"verification.requirements[{index}]")
        _reject_unknown(item, REQUIREMENT_FIELDS, f"verification.requirements[{index}]")
        requirement_id = _identifier(
            item.get("id"), f"verification.requirements[{index}].id"
        )
        if requirement_id in requirement_ids:
            raise SpecError(f"duplicate verification requirement id: {requirement_id}")
        requirement_ids.add(requirement_id)
        requirement = {
            "id": requirement_id,
            "text": _short_text(
                item.get("text"), f"verification.requirements[{index}].text"
            ),
            "kind": item.get("kind", "functional"),
            "signals": item.get("signals", []),
        }
        if requirement["kind"] not in {
            "functional",
            "reset",
            "protocol",
            "latency",
            "invariant",
        }:
            raise SpecError(
                f"verification.requirements[{index}].kind is unsupported"
            )
        if not isinstance(requirement["signals"], list) or len(requirement["signals"]) > 64:
            raise SpecError(
                f"verification.requirements[{index}].signals must be an array"
            )
        normalized_signals: list[str] = []
        for signal in requirement["signals"]:
            signal_name = _identifier(
                signal, f"verification.requirements[{index}].signals[]"
            )
            if signal_name not in port_map:
                raise SpecError(f"requirement {requirement_id} references unknown signal {signal_name}")
            normalized_signals.append(signal_name)
        requirement["signals"] = normalized_signals
        if "latency_cycles" in item:
            latency = item["latency_cycles"]
            if isinstance(latency, bool) or not isinstance(latency, int) or not 0 <= latency <= 100_000:
                raise SpecError(
                    f"verification.requirements[{index}].latency_cycles must be from 0 to 100000"
                )
            requirement["latency_cycles"] = latency
        requirements.append(requirement)

    tests_value = verification.get("directed_tests", [])
    if not isinstance(tests_value, list) or len(tests_value) > 256:
        raise SpecError("verification.directed_tests must be an array of at most 256 items")
    input_map = {
        port.name: port
        for port in ports
        if port.direction == "input" and port.name != clock_port
    }
    tests: list[dict[str, Any]] = []
    test_ids: set[str] = set()
    expanded_cycles = 0
    for index, test_value in enumerate(tests_value):
        test = _object(test_value, f"verification.directed_tests[{index}]")
        _reject_unknown(test, DIRECTED_TEST_FIELDS, f"verification.directed_tests[{index}]")
        test_id = _identifier(test.get("id"), f"verification.directed_tests[{index}].id")
        if test_id in test_ids:
            raise SpecError(f"duplicate directed test id: {test_id}")
        test_ids.add(test_id)
        refs = test.get("requirement_ids", [])
        if not isinstance(refs, list) or len(refs) > 32:
            raise SpecError(f"directed test {test_id} requirement_ids must be an array")
        normalized_refs: list[str] = []
        for requirement_id in refs:
            requirement_id = _identifier(
                requirement_id, f"directed test {test_id} requirement_ids[]"
            )
            if requirement_id not in requirement_ids:
                raise SpecError(
                    f"directed test {test_id} references unknown requirement {requirement_id}"
                )
            normalized_refs.append(requirement_id)
        cycles_value = test.get("cycles")
        if not isinstance(cycles_value, list) or not cycles_value:
            raise SpecError(f"directed test {test_id} cycles must be a non-empty array")
        cycles: list[dict[str, Any]] = []
        for cycle_index, cycle_value in enumerate(cycles_value):
            cycle = _object(
                cycle_value,
                f"verification.directed_tests[{index}].cycles[{cycle_index}]",
            )
            _reject_unknown(
                cycle,
                CYCLE_FIELDS,
                f"verification.directed_tests[{index}].cycles[{cycle_index}]",
            )
            drive = _object(
                cycle.get("drive", {}),
                f"verification.directed_tests[{index}].cycles[{cycle_index}].drive",
            )
            normalized_drive: dict[str, int] = {}
            for signal, raw_value in drive.items():
                if signal not in input_map:
                    raise SpecError(
                        f"directed test {test_id} drives unknown, output, or clock signal {signal}"
                    )
                if isinstance(raw_value, bool) or not isinstance(raw_value, int):
                    raise SpecError(f"directed test {test_id} value for {signal} must be an integer")
                width = input_map[signal].width
                low = -(1 << (width - 1)) if input_map[signal].signed else 0
                high = (1 << (width - 1)) - 1 if input_map[signal].signed else (1 << width) - 1
                if not low <= raw_value <= high:
                    raise SpecError(
                        f"directed test {test_id} value for {signal} does not fit its declared width"
                    )
                normalized_drive[signal] = raw_value & ((1 << width) - 1)
            repeat = cycle.get("repeat", 1)
            if isinstance(repeat, bool) or not isinstance(repeat, int) or not 1 <= repeat <= 10_000:
                raise SpecError(f"directed test {test_id} repeat must be from 1 to 10000")
            expanded_cycles += repeat
            cycles.append({"drive": normalized_drive, "repeat": repeat})
        tests.append(
            {
                "id": test_id,
                "description": _short_text(
                    test.get("description", test_id),
                    f"verification.directed_tests[{index}].description",
                ),
                "requirement_ids": normalized_refs,
                "reset_before": test.get("reset_before", True),
                "cycles": cycles,
            }
        )
        if not isinstance(tests[-1]["reset_before"], bool):
            raise SpecError(f"directed test {test_id} reset_before must be boolean")
    if expanded_cycles > 100_000:
        raise SpecError("verification directed tests exceed 100000 expanded cycles")
    if tests and not requirements:
        raise SpecError(
            "verification.directed_tests require at least one verification requirement"
        )
    if tests:
        for test in tests:
            if not test["requirement_ids"]:
                raise SpecError(
                    f"directed test {test['id']} must reference at least one requirement"
                )
        covered_requirements = {
            requirement_id
            for test in tests
            for requirement_id in test["requirement_ids"]
        }
        uncovered_requirements = sorted(requirement_ids - covered_requirements)
        if uncovered_requirements:
            raise SpecError(
                "verification requirements are not covered by a directed test: "
                + ", ".join(uncovered_requirements)
            )
    verification["requirements"] = requirements
    verification["directed_tests"] = tests
    try:
        universal_properties = normalize_universal_properties(
            ports,
            verification["universal_properties"],
            requirement_ids=requirement_ids,
            forbidden_ports={
                clock_port,
                *([reset["port"]] if reset else []),
            },
        )
    except UniversalPropertyError as exc:
        raise SpecError(f"verification.universal_properties: {exc}") from exc
    if universal_properties and not requirements:
        raise SpecError(
            "verification.universal_properties require verification requirements"
        )
    if any(not row["requirement_ids"] for row in universal_properties):
        raise SpecError(
            "each verification universal property must reference at least one requirement"
        )
    if verification["universal_mode"] == "off" and universal_properties:
        raise SpecError(
            "verification.universal_mode must be advisory or required when properties are supplied"
        )
    if verification["universal_mode"] == "required" and not universal_properties:
        raise SpecError(
            "verification.universal_mode required needs at least one universal property"
        )
    verification["universal_properties"] = universal_properties
    return verification


def validate_spec(raw: dict[str, Any]) -> DesignSpec:
    _reject_secrets(raw)
    _reject_unknown(raw, ROOT_FIELDS, "top-level")
    if raw.get("schema_version") != 1:
        raise SpecError("schema_version must be 1")

    design = _object(raw.get("design"), "design")
    _reject_unknown(design, DESIGN_FIELDS, "design")
    name = _identifier(design.get("name"), "design.name")
    description = design.get("description")
    if not isinstance(description, str) or len(description.strip()) < 20:
        raise SpecError("design.description must contain an explicit behavioral specification")
    pdk = design.get("pdk", "sky130A")
    if pdk not in OPEN_PDKS:
        raise SpecError(f"design.pdk must be one of: {', '.join(sorted(OPEN_PDKS))}")
    scl = design.get("scl")
    if scl is not None and (not isinstance(scl, str) or not re.fullmatch(r"[A-Za-z0-9_.-]+", scl)):
        raise SpecError("design.scl contains unsupported characters")

    rows = design.get("ports")
    if not isinstance(rows, list) or not rows:
        raise SpecError("design.ports must be a non-empty array")
    ports: list[Port] = []
    seen: set[str] = set()
    for index, row_value in enumerate(rows):
        row = _object(row_value, f"design.ports[{index}]")
        _reject_unknown(row, PORT_FIELDS, f"design.ports[{index}]")
        port_name = _identifier(row.get("name"), f"design.ports[{index}].name")
        if port_name in seen:
            raise SpecError(f"duplicate port: {port_name}")
        seen.add(port_name)
        direction = row.get("direction")
        if direction not in {"input", "output"}:
            raise SpecError(f"port {port_name} direction must be input or output")
        width = row.get("width", 1)
        if isinstance(width, bool) or not isinstance(width, int) or not 1 <= width <= 4096:
            raise SpecError(f"port {port_name} width must be an integer from 1 to 4096")
        signed = row.get("signed", False)
        if not isinstance(signed, bool):
            raise SpecError(f"port {port_name} signed must be boolean")
        ports.append(Port(port_name, direction, width, signed))

    port_map = {port.name: port for port in ports}
    clock = _object(design.get("clock"), "design.clock")
    _reject_unknown(clock, CLOCK_FIELDS, "design.clock")
    clock_port = _identifier(clock.get("port"), "design.clock.port")
    clock_period = _bounded_number(
        clock.get("period_ns"), "design.clock.period_ns", 0.1, 100_000
    )
    if clock_port not in port_map or port_map[clock_port].direction != "input" or port_map[clock_port].width != 1:
        raise SpecError("design.clock.port must name a one-bit input port")

    reset_value = design.get("reset")
    reset: dict[str, Any] | None = None
    if reset_value is not None:
        reset = _object(reset_value, "design.reset")
        _reject_unknown(reset, RESET_FIELDS, "design.reset")
        reset_port = _identifier(reset.get("port"), "design.reset.port")
        if reset_port not in port_map or port_map[reset_port].direction != "input" or port_map[reset_port].width != 1:
            raise SpecError("design.reset.port must name a one-bit input port")
        if reset_port == clock_port:
            raise SpecError("design.reset.port must differ from design.clock.port")
        for field in ("active_low", "asynchronous"):
            if not isinstance(reset.get(field), bool):
                raise SpecError(f"design.reset.{field} must be boolean")

    architecture = _architecture_contract(design.get("architecture", {}))

    flow = dict(_object(raw.get("flow", {}), "flow"))
    _reject_unknown(flow, FLOW_FIELDS, "flow")
    flow.setdefault("name", "Classic")
    flow.setdefault("to", None)
    flow.setdefault("mapped_equivalence_mode", "off")
    flow.setdefault("core_utilization", 40)
    flow.setdefault("place_density_pct", 60)
    flow.setdefault("jobs", None)
    flow.setdefault("physical_planning_mode", "off")
    has_die_area = "die_area" in flow
    has_core_area = "core_area" in flow
    sizing = flow.get("floorplan_sizing", "relative")
    if sizing not in {"absolute", "relative"}:
        raise SpecError("flow.floorplan_sizing must be relative or absolute")
    flow["floorplan_sizing"] = sizing
    if not isinstance(flow["name"], str) or not re.fullmatch(r"[A-Za-z][A-Za-z0-9_]*", flow["name"]):
        raise SpecError("flow.name contains unsupported characters")
    if flow["name"] != "Classic":
        raise SpecError(
            "flow.name must be Classic; rtl-closure supports only LibreLane Classic"
        )
    if flow["to"] is not None and (
        not isinstance(flow["to"], str)
        or not re.fullmatch(r"[A-Za-z0-9_.-]{1,128}", flow["to"])
    ):
        raise SpecError("flow.to must be a LibreLane Step ID")
    if flow["mapped_equivalence_mode"] not in {"off", "required"}:
        raise SpecError("flow.mapped_equivalence_mode must be off or required")
    if flow["mapped_equivalence_mode"] == "required" and pdk != "sky130A":
        raise SpecError(
            "flow.mapped_equivalence_mode required is supported only for sky130A"
        )
    flow["core_utilization"] = _bounded_number(
        flow["core_utilization"], "flow.core_utilization", 10, 80
    )
    flow["place_density_pct"] = _bounded_number(
        flow["place_density_pct"], "flow.place_density_pct", 20, 95
    )
    if sizing == "absolute":
        if not has_die_area or not has_core_area:
            raise SpecError(
                "absolute floorplan sizing requires both flow.die_area and flow.core_area"
            )
        flow["die_area"] = _area(flow["die_area"], "flow.die_area")
        flow["core_area"] = _area(flow["core_area"], "flow.core_area")
        die = flow["die_area"]
        core = flow["core_area"]
        if not (
            die[0] <= core[0] < core[2] <= die[2]
            and die[1] <= core[1] < core[3] <= die[3]
        ):
            raise SpecError("flow.core_area must be contained within flow.die_area")
    elif has_die_area or has_core_area:
        raise SpecError(
            "flow.die_area and flow.core_area are only valid with absolute floorplan sizing"
        )
    if flow["jobs"] is not None and (
        isinstance(flow["jobs"], bool)
        or not isinstance(flow["jobs"], int)
        or not 1 <= flow["jobs"] <= 256
    ):
        raise SpecError("flow.jobs must be an integer from 1 to 256")
    if flow["physical_planning_mode"] not in {
        "auto_safe",
        "reference_locked",
        "off",
    }:
        raise SpecError(
            "flow.physical_planning_mode must be auto_safe, reference_locked, or off"
        )
    planning_policy = flow.get("physical_planning_policy")
    if flow["physical_planning_mode"] != "off" and planning_policy is not None:
        if not isinstance(planning_policy, str) or not planning_policy:
            raise SpecError("flow.physical_planning_policy must be a path")
        candidate = Path(planning_policy).expanduser()
        try:
            resolved = candidate.resolve(strict=True)
        except OSError as exc:
            raise SpecError("flow.physical_planning_policy does not exist") from exc
        if not candidate.is_absolute() or candidate.is_symlink() or not resolved.is_file():
            raise SpecError(
                "flow.physical_planning_policy must be an absolute regular file"
            )
        if resolved.stat().st_size > 1024 * 1024:
            raise SpecError("flow.physical_planning_policy exceeds 1 MiB")
        flow["physical_planning_policy"] = str(resolved)
        observed_digest = hashlib.sha256(resolved.read_bytes()).hexdigest()
        configured_digest = flow.get("physical_planning_policy_sha256")
        if configured_digest is not None and configured_digest != observed_digest:
            raise SpecError(
                "flow.physical_planning_policy_sha256 does not match the file"
            )
        flow["physical_planning_policy_sha256"] = observed_digest
    elif flow["physical_planning_mode"] == "off" and planning_policy is not None:
        raise SpecError("flow.physical_planning_policy is invalid when planning is off")
    elif (
        flow["physical_planning_mode"] == "off"
        and flow.get("physical_planning_policy_sha256") is not None
    ):
        raise SpecError(
            "flow.physical_planning_policy_sha256 is invalid when planning is off"
        )
    elif planning_policy is None and flow.get("physical_planning_policy_sha256") is not None:
        raise SpecError(
            "flow.physical_planning_policy_sha256 requires a policy file"
        )
    reference_geometry_lock = flow.get("reference_geometry_lock")
    if flow["physical_planning_mode"] == "reference_locked":
        if not isinstance(reference_geometry_lock, str) or not reference_geometry_lock:
            raise SpecError(
                "reference_locked physical planning requires flow.reference_geometry_lock"
            )
        candidate = Path(reference_geometry_lock).expanduser()
        try:
            resolved = candidate.resolve(strict=True)
        except OSError as exc:
            raise SpecError("flow.reference_geometry_lock does not exist") from exc
        if not candidate.is_absolute() or candidate.is_symlink() or not resolved.is_file():
            raise SpecError("flow.reference_geometry_lock must be an absolute regular file")
        if resolved.stat().st_size > 1024 * 1024:
            raise SpecError("flow.reference_geometry_lock exceeds 1 MiB")
        flow["reference_geometry_lock"] = str(resolved)
        observed_digest = hashlib.sha256(resolved.read_bytes()).hexdigest()
        configured_digest = flow.get("reference_geometry_lock_sha256")
        if configured_digest is not None and configured_digest != observed_digest:
            raise SpecError("flow.reference_geometry_lock_sha256 does not match the file")
        flow["reference_geometry_lock_sha256"] = observed_digest
    elif reference_geometry_lock is not None:
        raise SpecError(
            "flow.reference_geometry_lock is valid only in reference_locked mode"
        )
    elif flow.get("reference_geometry_lock_sha256") is not None:
        raise SpecError(
            "flow.reference_geometry_lock_sha256 is valid only in reference_locked mode"
        )

    generation = dict(_object(raw.get("generation", {}), "generation"))
    _reject_unknown(generation, GENERATION_FIELDS, "generation")
    generation.setdefault("n", 3)
    generation.setdefault("max_turns", 3)
    generation.setdefault("num_verify_tests", 100)
    generation.setdefault("independent_verify_tests", 300)
    generation.setdefault("temperature", 0.4)
    generation.setdefault("seed", 42)
    generation.setdefault("reasoning_effort", "xhigh")
    generation.setdefault("profile", "api")
    if generation["profile"] != "api":
        raise SpecError("rtl-closure supports only the isolated api profile")
    if "model" in generation and (
        not isinstance(generation["model"], str)
        or not 1 <= len(generation["model"]) <= 256
        or any(character in generation["model"] for character in "\r\n\0")
    ):
        raise SpecError("generation.model must be a non-empty model identifier")
    for field, low, high in (
        # Two independently generated Python candidates are the minimum
        # trusted oracle quorum.  The reviewer is an audit, not a second vote.
        ("n", 2, 10),
        ("max_turns", 1, 8),
        ("num_verify_tests", 10, 2000),
        ("independent_verify_tests", 30, 5000),
    ):
        value = generation[field]
        if isinstance(value, bool) or not isinstance(value, int) or not low <= value <= high:
            raise SpecError(f"generation.{field} must be an integer from {low} to {high}")
    generation["temperature"] = _bounded_number(
        generation["temperature"], "generation.temperature", 0, 2
    )
    if isinstance(generation["seed"], bool) or not isinstance(generation["seed"], int):
        raise SpecError("generation.seed must be an integer")
    if generation["reasoning_effort"] not in {"low", "medium", "high", "xhigh", "max"}:
        raise SpecError(
            "generation.reasoning_effort must be low, medium, high, xhigh, or max"
        )

    verification = _verification_contract(
        raw["verification"] if "verification" in raw else {},
        ports=tuple(ports),
        clock_port=clock_port,
        reset=reset,
    )
    if verification["oracle_min_distinct_sources"] > generation["n"]:
        raise SpecError(
            "verification.oracle_min_distinct_sources must not exceed generation.n"
        )
    intent_closure = _intent_closure_contract(
        raw.get("intent_closure", {}),
        name=name,
        description=description.strip(),
        ports=tuple(ports),
        clock_port=clock_port,
        clock_period_ns=clock_period,
        reset=reset,
        architecture=architecture,
        requirements=verification["requirements"],
    )

    normalized = json.loads(json.dumps(raw))
    normalized["design"]["architecture"] = architecture
    normalized["flow"] = flow
    normalized["generation"] = generation
    normalized["verification"] = verification
    normalized["intent_closure"] = intent_closure
    return DesignSpec(
        raw=normalized,
        name=name,
        description=description.strip(),
        pdk=pdk,
        scl=scl,
        ports=tuple(ports),
        clock_port=clock_port,
        clock_period_ns=clock_period,
        reset=reset,
        architecture=architecture,
        flow=flow,
        generation=generation,
        verification=verification,
        intent_closure=intent_closure,
    )


def compute_intent_binding(raw: dict[str, Any]) -> str:
    """Compute the confirmation digest without enforcing an existing signature."""
    candidate = json.loads(json.dumps(raw))
    existing = candidate.get("intent_closure")
    assumptions = existing.get("assumptions", []) if isinstance(existing, dict) else []
    unresolved = (
        existing.get("unresolved_questions", []) if isinstance(existing, dict) else []
    )
    candidate["intent_closure"] = {
        "mode": "advisory",
        "user_confirmed": False,
        "unresolved_questions": unresolved,
        "assumptions": assumptions,
        "confirmed_sha256": None,
    }
    return validate_spec(candidate).intent_closure["binding_sha256"]


def load_spec(path: Path) -> DesignSpec:
    try:
        if path.stat().st_size > 1024 * 1024:
            raise SpecError("specification exceeds the 1 MiB size limit")
        raw = json.loads(path.read_text(encoding="utf-8"))
    except SpecError:
        raise
    except (OSError, json.JSONDecodeError) as exc:
        raise SpecError(f"cannot read specification {path}: {exc}") from exc
    if not isinstance(raw, dict):
        raise SpecError("specification root must be a JSON object")
    return validate_spec(raw)


def render_interface(spec: DesignSpec) -> str:
    declarations = ",\n".join(f"  {port.declaration}" for port in spec.ports)
    return f"module {spec.name}(\n{declarations}\n);\nendmodule\n"


def render_question(spec: DesignSpec) -> str:
    interface = "\n".join(
        f"- {port.direction} {port.name}: {port.width} bit{'s' if port.width != 1 else ''}"
        + (", signed" if port.signed else "")
        for port in spec.ports
    )
    reset_text = "No reset signal is defined."
    if spec.reset:
        reset_text = (
            f"Reset port {spec.reset['port']} is "
            f"{'active-low' if spec.reset['active_low'] else 'active-high'} and "
            f"{'asynchronous' if spec.reset['asynchronous'] else 'synchronous'}."
        )
    architecture_lines: list[str] = []
    if spec.architecture.get("required_modules"):
        architecture_lines.append(
            "- Reachable RTL hierarchy must contain these modules: "
            + ", ".join(spec.architecture["required_modules"])
        )
    if spec.architecture.get("forbidden_operators"):
        architecture_lines.append(
            "- Do not infer these operators: "
            + ", ".join(spec.architecture["forbidden_operators"])
        )
    for key, label in (
        ("min_sequential_bits", "Minimum synthesized sequential bits"),
        ("max_sequential_bits", "Maximum synthesized sequential bits"),
        ("max_synth_cells", "Maximum generic synthesized cell count"),
    ):
        if key in spec.architecture:
            architecture_lines.append(f"- {label}: {spec.architecture[key]}")
    architecture_text = ""
    if architecture_lines:
        architecture_text = (
            "\n\nArchitecture constraints (machine-checked before publication):\n"
            + "\n".join(architecture_lines)
        )
    universal_text = ""
    if spec.verification.get("universal_properties"):
        universal_text = (
            "\n\nUniversal port properties (machine-checked with symbolic inputs):\n"
            + json.dumps(
                spec.verification["universal_properties"],
                indent=2,
                ensure_ascii=False,
            )
        )
    return f"""Design a synthesizable hardware module named {spec.name}.

The interface is fixed and must match exactly:
{interface}

Clock port {spec.clock_port} has a target period of {spec.clock_period_ns:g} ns. {reset_text}

Behavioral specification:
{spec.description}
{architecture_text}
{universal_text}

Preserve exact port names, directions, widths, and signedness. Do not use initial
blocks, delays, force/release, DPI, file I/O, system tasks, or vendor primitives."""


def render_sdc(spec: DesignSpec) -> str:
    return f"""set clk_name core_clock
set clk_port_name {spec.clock_port}
set clk_period {spec.clock_period_ns:g}
set clk_io_pct 0.2

set clk_port [get_ports $clk_port_name]
create_clock -name $clk_name -period $clk_period $clk_port
set non_clock_inputs [all_inputs -no_clocks]
set_input_delay [expr $clk_period * $clk_io_pct] -clock $clk_name $non_clock_inputs
set_output_delay [expr $clk_period * $clk_io_pct] -clock $clk_name [all_outputs]
"""


def render_eqy_gate_tcl() -> str:
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


def render_eqy_script_for_name(design_name: str) -> str:
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


def render_eqy_script(spec: DesignSpec) -> str:
    if spec.flow["mapped_equivalence_mode"] != "required":
        raise ValueError("EQY script rendering requires mapped equivalence")
    return render_eqy_script_for_name(spec.name)


def render_config(spec: DesignSpec) -> str:
    mapped_equivalence_required = (
        spec.flow["mapped_equivalence_mode"] == "required"
    )
    config: dict[str, Any] = {
        "meta": {"version": 2, "flow": spec.flow["name"]},
        "PDK": spec.pdk,
        "DESIGN_NAME": spec.name,
        "VERILOG_FILES": [f"dir::src/{spec.name}.sv"],
        "CLOCK_PORT": spec.clock_port,
        "CLOCK_PERIOD": spec.clock_period_ns,
        "PNR_SDC_FILE": "dir::constraints.sdc",
        "SIGNOFF_SDC_FILE": "dir::constraints.sdc",
        "IO_PIN_ORDER_CFG": "dir::pin_order.cfg",
        "FP_SIZING": spec.flow["floorplan_sizing"],
        "FP_CORE_UTIL": spec.flow["core_utilization"],
        "PL_TARGET_DENSITY_PCT": spec.flow["place_density_pct"],
        "RUN_EQY": mapped_equivalence_required,
    }
    if mapped_equivalence_required:
        config["EQY_SCRIPT"] = f"dir::eqy/{spec.name}.eqy"
    if spec.flow["floorplan_sizing"] == "absolute":
        config["DIE_AREA"] = spec.flow["die_area"]
        config["CORE_AREA"] = spec.flow["core_area"]
    if spec.scl:
        config["STD_CELL_LIBRARY"] = spec.scl
    return yaml.safe_dump(config, sort_keys=False)


def render_pin_order(spec: DesignSpec) -> str:
    """Render deterministic I/O placement with contiguous wide-bus splitting."""

    side_pins: dict[str, list[str]] = {side: [] for side in "NSEW"}
    side_load = {side: 0 for side in "NSEW"}

    control_names = [spec.clock_port]
    if spec.reset is not None:
        control_names.append(spec.reset["port"])
    control_set = set(control_names)
    port_map = {port.name: port for port in spec.ports}
    for name in control_names:
        port = port_map[name]
        side_pins["S"].append(name)
        side_load["S"] += port.width

    for port in spec.ports:
        if port.name in control_set:
            continue
        first, second = (
            ("W", "S") if port.direction == "input" else ("E", "N")
        )
        numerator = side_load[second] + port.width - side_load[first]
        first_count = max(0, min(port.width, (numerator + 1) // 2))
        shares = ((first, 0, first_count), (second, first_count, port.width))
        for side, start, stop in shares:
            count = stop - start
            if count <= 0:
                continue
            if port.width == 1:
                side_pins[side].append(port.name)
            elif count == port.width:
                side_pins[side].append(rf"{port.name}\[\d+\]")
            else:
                side_pins[side].extend(
                    rf"{port.name}\[{index}\]" for index in range(start, stop)
                )
            side_load[side] += count

    lines = ["@bus_major"]
    for side in "NSEW":
        pins = side_pins[side]
        if not pins:
            continue
        lines.extend(("", f"#{side}"))
        lines.extend(pins)
    return "\n".join(lines) + "\n"
