"""Frozen behavioral contract and declaration-only verification scenarios."""

from __future__ import annotations

import hashlib
import json
import random
from pathlib import Path
from typing import Any

from .spec import DesignSpec, Port, validate_spec


CONTRACT_SCHEMA_VERSION = 2
PLAN_SCHEMA_VERSION = 1


def canonical_json(value: Any) -> str:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )


def content_hash(value: Any) -> str:
    payload = value if isinstance(value, str) else canonical_json(value)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _extract_json(response: str) -> dict[str, Any]:
    decoder = json.JSONDecoder()
    for index, character in enumerate(response):
        if character != "{":
            continue
        try:
            value, _ = decoder.raw_decode(response[index:])
        except json.JSONDecodeError:
            continue
        if isinstance(value, dict):
            return value
    raise ValueError("planner response did not contain a JSON object")


def _port_rows(spec: DesignSpec) -> list[dict[str, Any]]:
    return [
        {
            "name": port.name,
            "direction": port.direction,
            "width": port.width,
            "signed": port.signed,
        }
        for port in spec.ports
    ]


def build_contract(spec: DesignSpec, requirements: list[dict[str, Any]]) -> dict[str, Any]:
    return {
        "schema_version": CONTRACT_SCHEMA_VERSION,
        "design": spec.name,
        "behavior": spec.description,
        "ports": _port_rows(spec),
        "clock": {
            "port": spec.clock_port,
            "period_ns": spec.clock_period_ns,
            "sample_phase": "post_rising_edge",
        },
        "reset": spec.reset,
        "requirements": requirements,
        "intent_closure": spec.intent_closure,
    }


def _fallback_requirements(spec: DesignSpec) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = [
        {
            "id": "REQ_BEHAVIOR",
            "text": spec.description,
            "kind": "functional",
            "signals": [port.name for port in spec.ports],
        }
    ]
    if spec.reset:
        rows.append(
            {
                "id": "REQ_RESET",
                "text": (
                    f"When {spec.reset['port']} is asserted, all state and outputs "
                    "must take the reset values described by the behavioral contract."
                ),
                "kind": "reset",
                "signals": [spec.reset["port"]]
                + [port.name for port in spec.ports if port.direction == "output"],
            }
        )
    return rows


def _data_inputs(spec: DesignSpec) -> tuple[Port, ...]:
    reset_port = spec.reset["port"] if spec.reset else None
    return tuple(
        port
        for port in spec.ports
        if port.direction == "input"
        and port.name not in {spec.clock_port, reset_port}
    )


def _pattern(port: Port, value: int) -> int:
    return value & ((1 << port.width) - 1)


def fallback_directed_tests(
    spec: DesignSpec, requirement_ids: list[str] | str | None = None
) -> list[dict[str, Any]]:
    if requirement_ids is None:
        source_requirements = (
            spec.verification["requirements"] or _fallback_requirements(spec)
        )
        requirement_ids = [requirement["id"] for requirement in source_requirements]
    elif isinstance(requirement_ids, str):
        requirement_ids = [requirement_ids]
    inputs = _data_inputs(spec)
    if not inputs:
        return [
            {
                "id": "TEST_IDLE",
                "description": "Observe the design after reset for four cycles.",
                "requirement_ids": list(requirement_ids),
                "reset_before": True,
                "cycles": [{"drive": {}, "repeat": 4}],
            }
        ]

    zero = {port.name: 0 for port in inputs}
    maximum = {port.name: _pattern(port, -1) for port in inputs}
    alternating = {
        port.name: _pattern(port, int("10" * ((port.width + 1) // 2), 2))
        for port in inputs
    }
    cycles: list[dict[str, Any]] = [
        {"drive": zero, "repeat": 2},
        {"drive": maximum, "repeat": 2},
        {"drive": alternating, "repeat": 2},
    ]
    for port in inputs[:16]:
        drive = dict(zero)
        drive[port.name] = 1
        cycles.append({"drive": drive, "repeat": 1})
    return [
        {
            "id": "TEST_BOUNDARIES",
            "description": "Exercise zero, maximum, alternating, and one-hot input values.",
            "requirement_ids": list(requirement_ids),
            "reset_before": True,
            "cycles": cycles,
        }
    ]


def _random_tests(
    spec: DesignSpec,
    requirement_ids: list[str],
    existing_ids: set[str],
) -> list[dict[str, Any]]:
    count = spec.verification["random_regression_cycles"]
    inputs = _data_inputs(spec)
    if count == 0 or not inputs:
        return []
    test_count = 2
    lengths = [count] * test_count
    tests: list[dict[str, Any]] = []
    for suite, length in enumerate(lengths):
        rng = random.Random(spec.generation["seed"] + (1009, 2027)[suite])
        cycles = []
        for _ in range(length):
            cycles.append(
                {
                    "drive": {
                        port.name: rng.randrange(1 << port.width) for port in inputs
                    },
                    "repeat": 1,
                }
            )
        suffix = suite + 1
        test_id = f"RANDOM_REGRESSION_{suffix}"
        while test_id in existing_ids:
            suffix += 1
            test_id = f"RANDOM_REGRESSION_{suffix}"
        existing_ids.add(test_id)
        tests.append(
            {
                "id": test_id,
                "description": "Frozen independent pseudo-random regression sequence.",
                "requirement_ids": list(requirement_ids),
                "reset_before": True,
                "cycles": cycles,
                "source": "system_random",
            }
        )
    return tests


def _planner_prompt(spec: DesignSpec) -> str:
    supplied_requirements = spec.verification["requirements"]
    return f"""Turn the following hardware behavior into atomic requirements and
directed input scenarios. Return one JSON object and no prose.

Design: {spec.name}
Behavior: {spec.description}
Clock: {spec.clock_port}, sampled immediately after each rising edge
Reset: {json.dumps(spec.reset, sort_keys=True)}
Ports: {json.dumps(_port_rows(spec), sort_keys=True)}
Authoritative requirements already supplied by the user (preserve them exactly
when non-empty): {json.dumps(supplied_requirements, sort_keys=True)}

Required JSON shape:
{{
  "requirements": [
    {{"id":"REQ_NAME","text":"...","kind":"functional|reset|protocol|latency|invariant","signals":["port"],"latency_cycles":0}}
  ],
  "directed_tests": [
    {{"id":"TEST_NAME","description":"...","requirement_ids":["REQ_NAME"],"reset_before":true,
      "cycles":[{{"drive":{{"input_port":0}},"repeat":1}}]}}
  ]
}}

Rules:
- Scenarios contain inputs only, never expected outputs or executable code.
- Never drive the clock. Values must fit declared widths.
- Cover reset, enable/hold, operation start, busy/ready behavior, completion
  latency, back-to-back operations, priorities, overflow, and boundary values
  whenever those concepts exist in the behavior.
- A repeat expands identical consecutive cycles. Keep the total expanded cycle
  count at or below {spec.verification['directed_test_budget']}.
- Every test references at least one requirement.
"""


def _validated_verification(
    spec: DesignSpec,
    requirements: list[dict[str, Any]],
    directed_tests: list[dict[str, Any]],
) -> dict[str, Any]:
    raw = json.loads(json.dumps(spec.raw))
    verification = dict(spec.verification)
    verification["requirements"] = requirements
    verification["directed_tests"] = directed_tests
    raw["verification"] = verification
    validated = validate_spec(raw).verification
    if not validated["requirements"]:
        raise ValueError("planned verification must contain at least one requirement")
    if not validated["directed_tests"]:
        raise ValueError("planned verification must contain at least one directed test")
    expanded = sum(
        cycle["repeat"]
        for test in validated["directed_tests"]
        for cycle in test["cycles"]
    )
    if expanded > validated["directed_test_budget"]:
        raise ValueError(
            "planned directed tests exceed verification.directed_test_budget "
            f"({expanded} > {validated['directed_test_budget']})"
        )
    return validated


def plan_contract(spec: DesignSpec, backend) -> tuple[dict[str, Any], dict[str, Any], dict[str, Any]]:
    """Build a validated contract and frozen scenario plan.

    The planner is advisory.  Invalid model output is never executed and falls
    back to deterministic boundary scenarios.
    """
    supplied_requirements = spec.verification["requirements"]
    supplied_tests = spec.verification["directed_tests"]
    metadata: dict[str, Any] = {"source": "user"}
    requirements = supplied_requirements
    directed_tests = supplied_tests
    if not requirements or not directed_tests:
        try:
            response = backend.generate(
                _planner_prompt(spec),
                n=1,
                temperature=0.2,
                max_tokens=6000,
                system_prompt=(
                    "You are a hardware verification architect. Produce only the "
                    "requested declaration-only JSON; never produce Python, RTL, or prose."
                ),
            )[0]
            planned = _extract_json(response)
            planned_requirements = planned.get("requirements")
            planned_tests = planned.get("directed_tests")
            if supplied_requirements:
                planned_requirements = supplied_requirements
            if supplied_tests:
                planned_tests = supplied_tests
            validated = _validated_verification(
                spec, planned_requirements, planned_tests
            )
            requirements = validated["requirements"]
            directed_tests = validated["directed_tests"]
            metadata = {
                "source": "model" if not supplied_requirements and not supplied_tests else "mixed",
                "raw_response_sha256": content_hash(response),
            }
        except Exception as exc:
            requirements = supplied_requirements or _fallback_requirements(spec)
            directed_tests = supplied_tests or fallback_directed_tests(
                spec, [requirement["id"] for requirement in requirements]
            )
            validated = _validated_verification(spec, requirements, directed_tests)
            requirements = validated["requirements"]
            directed_tests = validated["directed_tests"]
            metadata = {"source": "fallback", "warning": str(exc)[:1000]}

    contract = build_contract(spec, requirements)
    scenarios = json.loads(json.dumps(directed_tests))
    requirement_ids = [requirement["id"] for requirement in requirements]
    scenario_ids = {scenario["id"] for scenario in scenarios}
    scenarios.extend(_random_tests(spec, requirement_ids, scenario_ids))
    plan = {
        "schema_version": PLAN_SCHEMA_VERSION,
        "design": spec.name,
        "clock": {
            "port": spec.clock_port,
            "period_ns": spec.clock_period_ns,
            "sample_phase": "post_rising_edge",
        },
        "reset": spec.reset,
        "reset_cycles": 3,
        "scenarios": scenarios,
    }
    metadata.update(
        {
            "contract_sha256": content_hash(contract),
            "testplan_sha256": content_hash(plan),
            "requirement_count": len(requirements),
            "directed_scenario_count": len(directed_tests),
            "total_scenario_count": len(scenarios),
            "intent_closure": spec.intent_closure,
        }
    )
    return contract, plan, metadata


def expand_cycles(scenario: dict[str, Any]) -> list[dict[str, int]]:
    rows: list[dict[str, int]] = []
    current: dict[str, int] = {}
    for cycle in scenario["cycles"]:
        current.update(cycle.get("drive", {}))
        for _ in range(cycle.get("repeat", 1)):
            rows.append(dict(current))
    return rows


def verify_frozen(path: Path, expected_hash: str) -> None:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise RuntimeError(f"frozen artifact is unreadable: {path}: {exc}") from exc
    actual = content_hash(value)
    if actual != expected_hash:
        raise RuntimeError(
            f"frozen artifact changed during repair: {path.name}; "
            f"expected {expected_hash}, got {actual}"
        )
