"""Trusted adaptive counterexample suites and behavioral coverage summaries."""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

from .contract import content_hash, expand_cycles
from .spec import DesignSpec


def suite_hash(plan: dict[str, Any], expected_trace: dict[str, Any]) -> str:
    return content_hash(
        {
            "testplan_sha256": content_hash(plan),
            "expected_trace_sha256": content_hash(expected_trace),
        }
    )


def behavioral_coverage(
    contract: dict[str, Any],
    plan: dict[str, Any],
    result: dict[str, Any],
) -> dict[str, Any]:
    """Summarize evidence without treating scenario labels as condition hits.

    A scenario's ``requirement_ids`` are planning associations, not executable
    coverage predicates.  Consequently this report keeps association, observed
    I/O, and explicit condition-hit evidence separate.  A producer that has a
    machine-evaluated predicate may add a requirement ID to a trace row's
    ``requirement_hits`` (or ``coverage_hits``) field.
    """
    requirements = {
        row["id"]: {
            "scenario_ids": [],
            "associated_scenario_ids": [],
            "scenario_associated_cycles": 0,
            "io_observation_cycles": 0,
            "input_observation_cycles": 0,
            "output_check_cycles": 0,
            "condition_hit_cycles": 0,
            "condition_hit_evidence": [],
            "mismatches": 0,
            "status": "uncovered",
            "coverage_basis": "none",
        }
        for row in contract.get("requirements", [])
    }
    requirement_signals = {
        row["id"]: {
            str(signal)
            for signal in row.get("signals", [])
            if isinstance(signal, str)
        }
        for row in contract.get("requirements", [])
        if row.get("id") in requirements
    }
    port_directions = {
        row["name"]: row.get("direction")
        for row in contract.get("ports", [])
        if isinstance(row, dict) and isinstance(row.get("name"), str)
    }
    scenarios = plan.get("scenarios", [])
    for scenario in scenarios:
        for requirement_id in scenario.get("requirement_ids", []):
            if requirement_id in requirements:
                requirements[requirement_id]["scenario_ids"].append(scenario.get("id"))
                requirements[requirement_id]["associated_scenario_ids"].append(
                    scenario.get("id")
                )

    traces = result.get("actual_trace", [])
    scenario_requirements = {
        (index, scenario.get("id")): scenario.get("requirement_ids", [])
        for index, scenario in enumerate(scenarios)
    }
    input_values: dict[str, set[int]] = {}
    input_toggles: dict[str, int] = {}
    output_values: dict[str, set[int]] = {}
    output_toggles: dict[str, int] = {}
    previous_inputs: dict[str, int] = {}
    previous_outputs: dict[str, int] = {}
    previous_scenario: tuple[Any, Any] | None = None
    scenario_by_key = {
        (index, scenario.get("id")): scenario
        for index, scenario in enumerate(scenarios)
    }
    for trace in traces:
        key = (trace.get("scenario_index"), trace.get("scenario_id"))
        if key != previous_scenario:
            scenario = scenario_by_key.get(key) or {}
            if scenario.get("reset_before", True):
                previous_inputs.clear()
                previous_outputs.clear()
            previous_scenario = key
        inputs = trace.get("inputs") or {}
        actual = trace.get("actual") or {}
        expected = trace.get("expected") or {}
        explicit_hits: set[str] = set()
        for field in ("requirement_hits", "coverage_hits"):
            raw_hits = trace.get(field)
            if isinstance(raw_hits, list):
                explicit_hits.update(
                    str(value) for value in raw_hits if isinstance(value, str)
                )
            elif isinstance(raw_hits, dict):
                explicit_hits.update(
                    str(value)
                    for value, hit in raw_hits.items()
                    if isinstance(value, str) and hit is True
                )
        for requirement_id in scenario_requirements.get(key, []):
            if requirement_id in requirements:
                row = requirements[requirement_id]
                row["scenario_associated_cycles"] += 1
                declared = requirement_signals.get(requirement_id, set())
                observed_inputs = {
                    name
                    for name in declared
                    if port_directions.get(name) == "input" and name in inputs
                }
                checked_outputs = {
                    name
                    for name in declared
                    if port_directions.get(name) == "output"
                    and name in expected
                    and name in actual
                }
                if observed_inputs:
                    row["input_observation_cycles"] += 1
                if checked_outputs:
                    row["output_check_cycles"] += 1
                if observed_inputs or checked_outputs:
                    row["io_observation_cycles"] += 1
                if requirement_id in explicit_hits:
                    row["condition_hit_cycles"] += 1
                    if len(row["condition_hit_evidence"]) < 16:
                        row["condition_hit_evidence"].append(
                            {
                                "scenario_index": trace.get("scenario_index"),
                                "scenario_id": trace.get("scenario_id"),
                                "cycle": trace.get("cycle"),
                                "phase": trace.get("phase"),
                                "inputs": {
                                    name: inputs.get(name)
                                    for name in sorted(observed_inputs)
                                },
                                "outputs": {
                                    name: actual.get(name)
                                    for name in sorted(checked_outputs)
                                },
                            }
                        )
        for name, raw in inputs.items():
            if raw is None:
                continue
            value = int(raw)
            input_values.setdefault(name, set()).add(value)
            if name in previous_inputs and previous_inputs[name] != value:
                input_toggles[name] = input_toggles.get(name, 0) + 1
            previous_inputs[name] = value
        for name, raw in actual.items():
            if raw is None:
                continue
            value = int(raw)
            output_values.setdefault(name, set()).add(value)
            if name in previous_outputs and previous_outputs[name] != value:
                output_toggles[name] = output_toggles.get(name, 0) + 1
            previous_outputs[name] = value

    clusters: dict[str, dict[str, Any]] = {}
    for mismatch in result.get("mismatches", []):
        requirement_ids = mismatch.get("requirement_ids", [])
        for requirement_id in requirement_ids:
            if requirement_id in requirements:
                requirements[requirement_id]["mismatches"] += 1
        key = content_hash(
            {
                "failure_class": mismatch.get("failure_class"),
                "signal": mismatch.get("signal"),
                "requirement_ids": sorted(requirement_ids),
            }
        )[:16]
        row = clusters.setdefault(
            key,
            {
                "cluster_id": key,
                "failure_class": mismatch.get("failure_class"),
                "signal": mismatch.get("signal"),
                "requirement_ids": sorted(requirement_ids),
                "count": 0,
                "first_cycle": mismatch.get("cycle"),
            },
        )
        row["count"] += 1

    for row in requirements.values():
        if row["mismatches"]:
            row["status"] = "failing"
            row["coverage_basis"] = "checked_io_mismatch"
        elif row["condition_hit_cycles"]:
            row["status"] = "passing"
            row["coverage_basis"] = "explicit_condition_hit"
        elif row["io_observation_cycles"]:
            row["status"] = "observed"
            row["coverage_basis"] = "io_observation_without_condition_predicate"
        elif row["scenario_associated_cycles"]:
            row["status"] = "associated_only"
            row["coverage_basis"] = "scenario_association_only"
        row["scenario_ids"] = sorted(set(row["scenario_ids"]))
        row["associated_scenario_ids"] = sorted(
            set(row["associated_scenario_ids"])
        )

    port_widths = {
        row["name"]: int(row["width"])
        for row in contract.get("ports", [])
        if row.get("direction") == "input"
    }
    payload_evidence = _wraparound_payload_evidence(
        contract, scenarios, traces
    )
    quality_warnings = [
        {
            "code": "repeated_payload_wraparound",
            "scenario_index": row["scenario_index"],
            "scenario_id": row["scenario_id"],
            "detail": (
                "wraparound scenario uses only one payload value; address/order "
                "aliasing defects may remain undetected"
            ),
        }
        for row in payload_evidence
        if row["status"] == "insufficient_repeated_payload"
    ]
    return {
        "schema_version": 2,
        "requirement_coverage_semantics": {
            "scenario_association_is_coverage": False,
            "observed_means": "declared requirement signals were sampled as concrete I/O",
            "passing_requires": "an explicit machine-evaluated requirement hit",
        },
        "requirements": requirements,
        "inputs": {
            name: {
                "distinct_values": len(values),
                "zero_hit": 0 in values,
                "max_hit": ((1 << port_widths.get(name, 1)) - 1) in values,
                "toggles": input_toggles.get(name, 0),
            }
            for name, values in sorted(input_values.items())
        },
        "outputs": {
            name: {
                "distinct_values": len(values),
                "toggles": output_toggles.get(name, 0),
            }
            for name, values in sorted(output_values.items())
        },
        "failure_clusters": sorted(clusters.values(), key=lambda row: row["cluster_id"]),
        "wraparound_payload_evidence": payload_evidence,
        "quality_warnings": quality_warnings,
        "observed_trace_rows": len(traces),
        "passed_check_count": len(result.get("passed_check_ids", [])),
        "mismatch_count": int(result.get("mismatched") or 0),
    }


_WRAPAROUND_PATTERN = re.compile(r"(?:wrap(?:around)?|rollover|circular)", re.I)
_PAYLOAD_PATTERN = re.compile(
    r"(?:^|_)(?:data(?:_in)?|din|payload|word|value)(?:$|_)", re.I
)


def _wraparound_payload_evidence(
    contract: dict[str, Any],
    scenarios: list[dict[str, Any]],
    traces: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """Audit whether wraparound tests use distinguishable payload values."""
    payload_ports = {
        row["name"]
        for row in contract.get("ports", [])
        if isinstance(row, dict)
        and row.get("direction") == "input"
        and int(row.get("width") or 0) > 1
        and _PAYLOAD_PATTERN.search(str(row.get("name") or ""))
    }
    evidence: list[dict[str, Any]] = []
    for scenario_index, scenario in enumerate(scenarios):
        identity = " ".join(
            str(scenario.get(field) or "") for field in ("id", "description")
        )
        if not _WRAPAROUND_PATTERN.search(identity):
            continue
        rows = [
            trace
            for trace in traces
            if trace.get("scenario_index") == scenario_index
            and trace.get("scenario_id") == scenario.get("id")
            and trace.get("phase") in {None, "post_rising_edge"}
        ]
        signal_rows: dict[str, dict[str, Any]] = {}
        for signal in sorted(payload_ports):
            values = [
                int(trace["inputs"][signal])
                for trace in rows
                if isinstance(trace.get("inputs"), dict)
                and trace["inputs"].get(signal) is not None
            ]
            if not values:
                continue
            distinct = sorted(set(values))
            signal_rows[signal] = {
                "sample_count": len(values),
                "distinct_value_count": len(distinct),
                "distinct_values_preview": distinct[:16],
                "transition_count": sum(
                    left != right for left, right in zip(values, values[1:])
                ),
                "sequence_sha256": content_hash(values),
            }
        minimum_distinct = 2
        repeated_only = bool(signal_rows) and all(
            row["distinct_value_count"] < minimum_distinct
            for row in signal_rows.values()
        )
        if not signal_rows:
            status = "no_payload_signal_evidence"
        elif repeated_only:
            status = "insufficient_repeated_payload"
        else:
            status = "payload_diversity_observed"
        evidence.append(
            {
                "scenario_index": scenario_index,
                "scenario_id": scenario.get("id"),
                "minimum_distinct_values": minimum_distinct,
                "status": status,
                "payload_signals": signal_rows,
            }
        )
    return evidence


def _scenario_fingerprint(scenario: dict[str, Any]) -> str:
    return content_hash(
        {
            "reset_before": scenario.get("reset_before", True),
            "requirement_ids": sorted(scenario.get("requirement_ids", [])),
            "cycles": scenario.get("cycles", []),
        }
    )


def derive_counterexample_scenarios(
    spec: DesignSpec,
    plan: dict[str, Any],
    packet: dict[str, Any],
    *,
    round_index: int,
) -> list[dict[str, Any]]:
    """Deterministically mutate the concrete prefix that reached a mismatch."""
    limit = spec.verification["adaptive_tests_per_round"]
    budget = spec.verification["adaptive_cycle_budget"]
    if limit <= 0 or budget <= 0 or packet.get("first_failure_cycle") is None:
        return []
    source = next(
        (
            scenario
            for scenario in plan.get("scenarios", [])
            if scenario.get("id") == packet.get("scenario_id")
        ),
        None,
    )
    if source is None:
        return []
    # A newly appended adaptive scenario must be independently replayable.
    # Mutating a resetless or state-continuing scenario would silently discard
    # the state established by earlier scenarios and validate a different trace.
    if spec.reset is None or source.get("reset_before", True) is not True:
        return []
    expanded = expand_cycles(source)
    failure_cycle = int(packet["first_failure_cycle"])
    observed_failure_cycle = failure_cycle
    reset_port = spec.reset["port"] if spec.reset else None
    data_ports = [
        port
        for port in spec.ports
        if port.direction == "input"
        and port.name not in {spec.clock_port, reset_port}
    ]
    if failure_cycle < 0:
        # Controller-owned reset/dirty probes precede scenario cycle zero.  A
        # probe counterexample still yields a useful replay scenario by
        # materializing its concrete non-reset inputs as cycle zero.
        packet_inputs = packet.get("inputs") or {}
        if not isinstance(packet_inputs, dict):
            return []
        prefix = [
            {
                port.name: int(packet_inputs.get(port.name, 0))
                & ((1 << port.width) - 1)
                for port in data_ports
            }
        ]
        failure_cycle = 0
    elif failure_cycle >= len(expanded):
        return []
    else:
        prefix = expanded[: failure_cycle + 1]
    if not prefix or len(prefix) > budget:
        return []

    current = dict(prefix[-1])
    variants: list[tuple[str, list[dict[str, int]]]] = []
    variants.append(("HOLD", prefix + [dict(current)]))
    if len(prefix) >= 2:
        variants.append(("PULSE", prefix[:-1] + [dict(current), dict(prefix[-2]), dict(current)]))
    for port in data_ports:
        base = int(current.get(port.name, 0))
        mask = (1 << port.width) - 1
        for label, value in (
            ("ZERO", 0),
            ("MAX", mask),
            ("BITFLIP", (base ^ 1) & mask),
        ):
            if value == base:
                continue
            mutation = dict(current)
            mutation[port.name] = value
            variants.append((f"{port.name}_{label}", prefix[:-1] + [mutation]))

    existing = {_scenario_fingerprint(scenario) for scenario in plan.get("scenarios", [])}
    existing_ids = {
        str(scenario.get("id")) for scenario in plan.get("scenarios", [])
    }
    additions: list[dict[str, Any]] = []
    used_cycles = 0
    for label, rows in variants:
        if len(additions) >= limit:
            break
        if used_cycles + len(rows) > budget:
            continue
        base_id = f"ADAPT_R{round_index:02d}_{len(additions) + 1:02d}_{label}"[:128]
        scenario_id = base_id
        suffix = 2
        while scenario_id in existing_ids:
            rendered_suffix = f"_{suffix}"
            scenario_id = base_id[: 128 - len(rendered_suffix)] + rendered_suffix
            suffix += 1
        scenario = {
            "id": scenario_id,
            "description": (
                f"Controller-derived mutation around {packet.get('scenario_id')} "
                f"cycle {observed_failure_cycle}."
            ),
            "requirement_ids": packet.get("requirement_ids")
            or source.get("requirement_ids", []),
            "reset_before": True,
            "cycles": [{"drive": row, "repeat": 1} for row in rows],
            "source": "counterexample_mutation",
            "parent_fingerprint": packet.get("fingerprint"),
        }
        fingerprint = _scenario_fingerprint(scenario)
        if fingerprint in existing:
            continue
        existing.add(fingerprint)
        existing_ids.add(scenario_id)
        additions.append(scenario)
        used_cycles += len(rows)
    return additions


def build_suite_epoch(
    *,
    epoch: int,
    parent_epoch_sha256: str | None,
    parent_suite_sha256: str,
    additions: list[dict[str, Any]],
    plan: dict[str, Any],
    expected_trace: dict[str, Any],
    oracle_quorum: dict[str, Any],
) -> dict[str, Any]:
    if isinstance(epoch, bool) or not isinstance(epoch, int) or epoch < 1:
        raise ValueError("adaptive suite epoch number is invalid")
    if not additions:
        raise ValueError("adaptive suite epoch must add at least one scenario")
    if not isinstance(oracle_quorum, dict):
        raise ValueError("adaptive suite epoch must include oracle quorum evidence")
    scenarios = plan.get("scenarios")
    if not isinstance(scenarios, list) or len(scenarios) < len(additions):
        raise ValueError("adaptive suite epoch has an invalid test plan")
    if scenarios[-len(additions) :] != additions:
        raise ValueError("adaptive suite additions are not the test plan suffix")
    scenario_ids = [scenario.get("id") for scenario in scenarios]
    if any(not isinstance(value, str) for value in scenario_ids) or len(
        scenario_ids
    ) != len(set(scenario_ids)):
        raise ValueError("adaptive suite test plan contains duplicate scenario IDs")
    row = {
        "schema_version": 2,
        "epoch": epoch,
        "parent_epoch_sha256": parent_epoch_sha256,
        "parent_suite_sha256": parent_suite_sha256,
        "additions_sha256": content_hash(additions),
        "testplan_sha256": content_hash(plan),
        "expected_trace_sha256": content_hash(expected_trace),
        "oracle_quorum_sha256": content_hash(oracle_quorum),
        "suite_sha256": suite_hash(plan, expected_trace),
        "scenario_ids": [scenario["id"] for scenario in additions],
        "additions": additions,
    }
    row["epoch_sha256"] = content_hash(row)
    return row


def _read_epoch_json(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise RuntimeError(f"adaptive suite artifact is unreadable: {path}: {exc}") from exc
    if not isinstance(value, dict):
        raise RuntimeError(f"adaptive suite artifact is not an object: {path}")
    return value


def verify_epoch_chain(
    epochs: list[dict[str, Any]],
    base_plan: dict[str, Any],
    base_expected_trace: dict[str, Any],
    *,
    epoch_artifact_root: Path | None = None,
    spec: DesignSpec | None = None,
    oracle_sha256: str | None = None,
    selected_oracle: str | None = None,
    origin_bundle: dict[str, Any] | None = None,
) -> str:
    """Validate an append-only suite chain rooted in trusted base artifacts."""
    if not isinstance(epochs, list):
        raise RuntimeError("adaptive suite epochs must be an array")
    if not isinstance(base_plan, dict) or not isinstance(base_expected_trace, dict):
        raise RuntimeError("adaptive suite trusted base artifacts are invalid")
    if epochs and epoch_artifact_root is None:
        raise RuntimeError("adaptive suite epoch artifacts are required for verification")
    if spec is not None:
        from .verification import validate_expected_trace

        validate_expected_trace(
            spec,
            base_plan,
            base_expected_trace,
            expected_oracle_sha256=oracle_sha256,
        )
        if selected_oracle is None or origin_bundle is None:
            raise RuntimeError(
                "adaptive suite verification requires selected oracle and origin bundle"
            )
    trusted_oracle_sha256 = base_expected_trace.get("oracle_sha256")
    if oracle_sha256 is not None and trusted_oracle_sha256 != oracle_sha256:
        raise RuntimeError("adaptive suite base trace was generated by another oracle")

    active_plan = json.loads(json.dumps(base_plan))
    previous_trace = json.loads(json.dumps(base_expected_trace))
    previous_suite = suite_hash(active_plan, previous_trace)
    previous_epoch_sha256: str | None = None
    existing_ids = [str(row.get("id")) for row in active_plan.get("scenarios", [])]
    if len(existing_ids) != len(set(existing_ids)):
        raise RuntimeError("adaptive suite base plan contains duplicate scenario IDs")
    existing_fingerprints = {
        _scenario_fingerprint(scenario)
        for scenario in active_plan.get("scenarios", [])
    }
    required = {
        "schema_version",
        "epoch",
        "parent_epoch_sha256",
        "parent_suite_sha256",
        "additions_sha256",
        "testplan_sha256",
        "expected_trace_sha256",
        "oracle_quorum_sha256",
        "suite_sha256",
        "scenario_ids",
        "additions",
        "epoch_sha256",
    }
    for index, epoch in enumerate(epochs, 1):
        if not isinstance(epoch, dict) or set(epoch) != required:
            raise RuntimeError("adaptive suite epoch has an invalid schema")
        if epoch.get("schema_version") != 2 or epoch.get("epoch") != index:
            raise RuntimeError("adaptive suite epoch chain is discontinuous")
        if (
            epoch.get("parent_epoch_sha256") != previous_epoch_sha256
            or epoch.get("parent_suite_sha256") != previous_suite
        ):
            raise RuntimeError("adaptive suite epoch chain is discontinuous")
        additions = epoch.get("additions")
        if not isinstance(additions, list) or not additions:
            raise RuntimeError("adaptive suite epoch has no additions")
        if epoch.get("additions_sha256") != content_hash(additions):
            raise RuntimeError("adaptive suite additions hash is invalid")
        scenario_ids = []
        for scenario in additions:
            if not isinstance(scenario, dict) or not isinstance(scenario.get("id"), str):
                raise RuntimeError("adaptive suite addition is invalid")
            scenario_id = scenario["id"]
            fingerprint = _scenario_fingerprint(scenario)
            if scenario_id in existing_ids or fingerprint in existing_fingerprints:
                raise RuntimeError("adaptive suite addition is not unique")
            existing_ids.append(scenario_id)
            existing_fingerprints.add(fingerprint)
            scenario_ids.append(scenario_id)
        if epoch.get("scenario_ids") != scenario_ids:
            raise RuntimeError("adaptive suite scenario IDs do not match additions")
        active_plan["scenarios"].extend(json.loads(json.dumps(additions)))
        if epoch.get("testplan_sha256") != content_hash(active_plan):
            raise RuntimeError("adaptive suite test plan is not append-only")

        epoch_dir = Path(epoch_artifact_root) / f"epoch-{index:02d}"
        artifact_plan = _read_epoch_json(epoch_dir / "testplan.json")
        artifact_trace = _read_epoch_json(epoch_dir / "expected-trace.json")
        artifact_quorum = _read_epoch_json(epoch_dir / "oracle-quorum.json")
        artifact_epoch = _read_epoch_json(epoch_dir / "epoch.json")
        if artifact_plan != active_plan or artifact_epoch != epoch:
            raise RuntimeError("adaptive suite epoch artifacts disagree with the chain")
        if epoch.get("expected_trace_sha256") != content_hash(artifact_trace):
            raise RuntimeError("adaptive suite expected trace hash is invalid")
        if epoch.get("oracle_quorum_sha256") != content_hash(artifact_quorum):
            raise RuntimeError("adaptive suite oracle quorum hash is invalid")
        if epoch.get("suite_sha256") != suite_hash(active_plan, artifact_trace):
            raise RuntimeError("adaptive suite hash is invalid")
        if trusted_oracle_sha256 is not None and artifact_trace.get(
            "oracle_sha256"
        ) != trusted_oracle_sha256:
            raise RuntimeError("adaptive suite epoch changed the frozen oracle")
        if spec is not None:
            validate_expected_trace(
                spec,
                active_plan,
                artifact_trace,
                expected_oracle_sha256=oracle_sha256,
            )
            from .oracle import validate_oracle_quorum_evidence

            validate_oracle_quorum_evidence(
                artifact_quorum,
                active_plan,
                selected_oracle,
                artifact_trace,
                origin_bundle,
            )
        old_trace = previous_trace.get("trace", [])
        old_setup = previous_trace.get("setup_observations", [])
        if artifact_trace.get("trace", [])[: len(old_trace)] != old_trace or artifact_trace.get(
            "setup_observations", []
        )[: len(old_setup)] != old_setup:
            raise RuntimeError("adaptive suite changed historical expected behavior")
        payload = dict(epoch)
        claimed = payload.pop("epoch_sha256", None)
        if claimed != content_hash(payload):
            raise RuntimeError("adaptive suite epoch hash is invalid")
        previous_epoch_sha256 = claimed
        previous_suite = epoch["suite_sha256"]
        previous_trace = artifact_trace
    return previous_suite
