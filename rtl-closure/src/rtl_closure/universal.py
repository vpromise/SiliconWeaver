"""Safe, declarative, port-level temporal properties for generated RTL.

This module intentionally accepts data, not SystemVerilog snippets.  A property
is an ``always`` implication over current and previous clock-edge samples.  Each
property is compiled into a private formal harness and proved independently so
that failures remain attributable to one requirement and one counterexample.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import subprocess
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence

from .sandbox import _docker_base, _run_container
from .runtime import VERIFY_IMAGE as VERIFY_IMAGE, verify_image_ref


_VERILOG_ID = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")
_REPORT_ID = re.compile(r"[A-Za-z][A-Za-z0-9_.:-]{0,127}")
_PASS_PATTERN = re.compile(
    r"Temporal induction proof successful|SAT temporal induction proof finished.*SUCCESS|"
    r"Induction step proven:\s*SUCCESS",
    re.IGNORECASE,
)
_FAIL_PATTERN = re.compile(
    r"proof did fail|model found:\s*FAIL|temporal induction.*failed",
    re.IGNORECASE,
)
_MAX_PROPERTIES = 64
_MAX_EXPR_NODES = 256
_MAX_EXPR_DEPTH = 32
_MAX_WIDTH = 4096


class UniversalPropertyError(ValueError):
    """Raised when an untrusted property does not match the safe DSL."""


@dataclass(frozen=True)
class _Port:
    name: str
    direction: str
    width: int
    signed: bool


@dataclass(frozen=True)
class _Expr:
    op: str
    width: int
    value: Any = None
    args: tuple["_Expr", ...] = ()


@dataclass(frozen=True)
class _Property:
    property_id: str
    requirement_ids: tuple[str, ...]
    antecedent: _Expr
    consequent: _Expr


def _sha256_text(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _canonical_hash(value: Any) -> str:
    encoded = json.dumps(
        value, sort_keys=True, separators=(",", ":"), ensure_ascii=True
    )
    return _sha256_text(encoded)


def _write_json_atomic(path: Path, value: Any) -> None:
    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    temporary.write_text(
        json.dumps(value, indent=2, ensure_ascii=False) + "\n", encoding="utf-8"
    )
    os.replace(temporary, path)


def _strict_object(
    value: Any,
    *,
    label: str,
    required: set[str],
    allowed: set[str],
) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise UniversalPropertyError(f"{label} must be an object")
    keys = set(value)
    missing = required - keys
    unknown = keys - allowed
    if missing:
        raise UniversalPropertyError(
            f"{label} is missing required field(s): {', '.join(sorted(missing))}"
        )
    if unknown:
        raise UniversalPropertyError(
            f"{label} has unsupported field(s): {', '.join(sorted(unknown))}"
        )
    return value


def _normalize_ports(raw_ports: Iterable[Any]) -> tuple[_Port, ...]:
    ports: list[_Port] = []
    seen: set[str] = set()
    for index, raw in enumerate(raw_ports):
        if isinstance(raw, Mapping):
            name = raw.get("name")
            direction = raw.get("direction")
            width = raw.get("width")
            signed = raw.get("signed", False)
        else:
            name = getattr(raw, "name", None)
            direction = getattr(raw, "direction", None)
            width = getattr(raw, "width", None)
            signed = getattr(raw, "signed", False)
        label = f"ports[{index}]"
        if not isinstance(name, str) or not _VERILOG_ID.fullmatch(name):
            raise UniversalPropertyError(f"{label}.name must be a Verilog identifier")
        if name in seen:
            raise UniversalPropertyError(f"duplicate port {name}")
        if direction not in {"input", "output"}:
            raise UniversalPropertyError(f"{label}.direction must be input or output")
        if isinstance(width, bool) or not isinstance(width, int) or not 1 <= width <= _MAX_WIDTH:
            raise UniversalPropertyError(
                f"{label}.width must be an integer from 1 to {_MAX_WIDTH}"
            )
        if not isinstance(signed, bool):
            raise UniversalPropertyError(f"{label}.signed must be boolean")
        ports.append(_Port(name, direction, width, signed))
        seen.add(name)
    if not ports:
        raise UniversalPropertyError("ports must not be empty")
    return tuple(ports)


def _normalize_expr(
    raw: Any,
    ports: Mapping[str, _Port],
    *,
    label: str,
    depth: int,
    node_count: list[int],
) -> _Expr:
    if depth > _MAX_EXPR_DEPTH:
        raise UniversalPropertyError(
            f"{label} exceeds the maximum expression depth {_MAX_EXPR_DEPTH}"
        )
    node_count[0] += 1
    if node_count[0] > _MAX_EXPR_NODES:
        raise UniversalPropertyError(
            f"property exceeds the maximum {_MAX_EXPR_NODES} expression nodes"
        )
    if not isinstance(raw, Mapping):
        raise UniversalPropertyError(f"{label} must be an expression object")

    if "port" in raw:
        row = _strict_object(
            raw,
            label=label,
            required={"port"},
            allowed={"port", "at"},
        )
        name = row["port"]
        at = row.get("at", "current")
        if not isinstance(name, str) or name not in ports:
            raise UniversalPropertyError(f"{label} references unknown port {name!r}")
        if at not in {"current", "previous"}:
            raise UniversalPropertyError(
                f"{label}.at must be current or previous"
            )
        return _Expr("port", ports[name].width, (name, at))

    if "const" in raw:
        row = _strict_object(
            raw,
            label=label,
            required={"const", "width"},
            allowed={"const", "width"},
        )
        value = row["const"]
        width = row["width"]
        if isinstance(width, bool) or not isinstance(width, int) or not 1 <= width <= _MAX_WIDTH:
            raise UniversalPropertyError(
                f"{label}.width must be an integer from 1 to {_MAX_WIDTH}"
            )
        if isinstance(value, bool) or not isinstance(value, int) or value < 0:
            raise UniversalPropertyError(f"{label}.const must be a non-negative integer")
        if value.bit_length() > width:
            raise UniversalPropertyError(
                f"{label}.const does not fit in its declared width {width}"
            )
        return _Expr("const", width, value)

    row = _strict_object(
        raw,
        label=label,
        required={"op"},
        allowed={"op", "arg", "args", "left", "right"},
    )
    op = row["op"]
    if not isinstance(op, str):
        raise UniversalPropertyError(f"{label}.op must be a string")

    if op == "not":
        if set(row) != {"op", "arg"}:
            raise UniversalPropertyError(f"{label} not requires exactly op and arg")
        arg = _normalize_expr(
            row["arg"], ports, label=f"{label}.arg", depth=depth + 1, node_count=node_count
        )
        if arg.width != 1:
            raise UniversalPropertyError(f"{label} not operand must be one bit")
        return _Expr(op, 1, args=(arg,))

    if op in {"and", "or"}:
        if set(row) != {"op", "args"} or not isinstance(row["args"], Sequence) or isinstance(
            row["args"], (str, bytes)
        ):
            raise UniversalPropertyError(f"{label} {op} requires an args array")
        if not 2 <= len(row["args"]) <= 32:
            raise UniversalPropertyError(f"{label}.args must contain from 2 to 32 expressions")
        args = tuple(
            _normalize_expr(
                item,
                ports,
                label=f"{label}.args[{index}]",
                depth=depth + 1,
                node_count=node_count,
            )
            for index, item in enumerate(row["args"])
        )
        if any(arg.width != 1 for arg in args):
            raise UniversalPropertyError(f"{label} boolean operands must be one bit")
        return _Expr(op, 1, args=args)

    binary_ops = {
        "eq",
        "ne",
        "ult",
        "ule",
        "ugt",
        "uge",
        "bit_and",
        "bit_or",
        "bit_xor",
        "add",
        "sub",
    }
    if op not in binary_ops:
        raise UniversalPropertyError(f"{label} uses unsupported operator {op!r}")
    if set(row) != {"op", "left", "right"}:
        raise UniversalPropertyError(
            f"{label} {op} requires exactly op, left, and right"
        )
    left = _normalize_expr(
        row["left"], ports, label=f"{label}.left", depth=depth + 1, node_count=node_count
    )
    right = _normalize_expr(
        row["right"], ports, label=f"{label}.right", depth=depth + 1, node_count=node_count
    )
    if left.width != right.width:
        raise UniversalPropertyError(
            f"{label} operand widths differ ({left.width} and {right.width})"
        )
    result_width = 1 if op in {"eq", "ne", "ult", "ule", "ugt", "uge"} else left.width
    return _Expr(op, result_width, args=(left, right))


def _normalize_properties(
    raw_properties: Sequence[Any], ports: Mapping[str, _Port]
) -> tuple[_Property, ...]:
    if isinstance(raw_properties, (str, bytes)) or not isinstance(raw_properties, Sequence):
        raise UniversalPropertyError("properties must be an array")
    if len(raw_properties) > _MAX_PROPERTIES:
        raise UniversalPropertyError(f"properties may contain at most {_MAX_PROPERTIES} rows")
    properties: list[_Property] = []
    seen: set[str] = set()
    for index, raw in enumerate(raw_properties):
        label = f"properties[{index}]"
        row = _strict_object(
            raw,
            label=label,
            required={"id", "kind", "antecedent", "consequent"},
            allowed={"id", "kind", "antecedent", "consequent", "requirement_ids"},
        )
        property_id = row["id"]
        if not isinstance(property_id, str) or not _REPORT_ID.fullmatch(property_id):
            raise UniversalPropertyError(
                f"{label}.id must match {_REPORT_ID.pattern}"
            )
        if property_id in seen:
            raise UniversalPropertyError(f"duplicate property id {property_id}")
        if row["kind"] != "always_implication":
            raise UniversalPropertyError(
                f"{label}.kind must be always_implication"
            )
        requirement_ids = row.get("requirement_ids", [])
        if isinstance(requirement_ids, (str, bytes)) or not isinstance(
            requirement_ids, Sequence
        ):
            raise UniversalPropertyError(f"{label}.requirement_ids must be an array")
        normalized_requirements: list[str] = []
        for req_index, requirement_id in enumerate(requirement_ids):
            if not isinstance(requirement_id, str) or not _REPORT_ID.fullmatch(
                requirement_id
            ):
                raise UniversalPropertyError(
                    f"{label}.requirement_ids[{req_index}] has an invalid id"
                )
            if requirement_id not in normalized_requirements:
                normalized_requirements.append(requirement_id)
        node_count = [0]
        antecedent = _normalize_expr(
            row["antecedent"],
            ports,
            label=f"{label}.antecedent",
            depth=0,
            node_count=node_count,
        )
        consequent = _normalize_expr(
            row["consequent"],
            ports,
            label=f"{label}.consequent",
            depth=0,
            node_count=node_count,
        )
        if antecedent.width != 1 or consequent.width != 1:
            raise UniversalPropertyError(
                f"{label} antecedent and consequent must each be one bit"
            )
        properties.append(
            _Property(
                property_id,
                tuple(normalized_requirements),
                antecedent,
                consequent,
            )
        )
        seen.add(property_id)
    return tuple(properties)


def _expr_json(expr: _Expr) -> dict[str, Any]:
    if expr.op == "port":
        name, at = expr.value
        return {"port": name, "at": at}
    if expr.op == "const":
        return {"const": expr.value, "width": expr.width}
    if expr.op == "not":
        return {"op": expr.op, "arg": _expr_json(expr.args[0])}
    if expr.op in {"and", "or"}:
        return {"op": expr.op, "args": [_expr_json(arg) for arg in expr.args]}
    return {
        "op": expr.op,
        "left": _expr_json(expr.args[0]),
        "right": _expr_json(expr.args[1]),
    }


def _property_json(prop: _Property) -> dict[str, Any]:
    return {
        "id": prop.property_id,
        "kind": "always_implication",
        "requirement_ids": list(prop.requirement_ids),
        "antecedent": _expr_json(prop.antecedent),
        "consequent": _expr_json(prop.consequent),
    }


def normalize_universal_properties(
    ports: Iterable[Any],
    properties: Sequence[Any],
    *,
    requirement_ids: Iterable[str] | None = None,
    forbidden_ports: Iterable[str] = (),
) -> list[dict[str, Any]]:
    """Validate untrusted DSL rows and return their canonical JSON form."""

    normalized_ports = _normalize_ports(ports)
    port_map = {port.name: port for port in normalized_ports}
    normalized = _normalize_properties(properties, port_map)
    known_requirements = set(requirement_ids) if requirement_ids is not None else None
    blocked = set(forbidden_ports)
    for prop in normalized:
        if known_requirements is not None:
            unknown_requirements = sorted(set(prop.requirement_ids) - known_requirements)
            if unknown_requirements:
                raise UniversalPropertyError(
                    f"property {prop.property_id} references unknown requirement(s): "
                    + ", ".join(unknown_requirements)
                )
        referenced = {
            str(expr.value[0])
            for root in (prop.antecedent, prop.consequent)
            for expr in _walk_expr(root)
            if expr.op == "port"
        }
        forbidden_references = sorted(referenced & blocked)
        if forbidden_references:
            raise UniversalPropertyError(
                f"property {prop.property_id} references harness-controlled port(s): "
                + ", ".join(forbidden_references)
            )
    return [_property_json(prop) for prop in normalized]


def _signal_name(port_name: str, at: str) -> str:
    prefix = "__cm_prev_" if at == "previous" else "__cm_port_"
    return prefix + port_name


def _emit_expr(expr: _Expr) -> str:
    if expr.op == "port":
        return _signal_name(*expr.value)
    if expr.op == "const":
        return f"{expr.width}'d{expr.value}"
    if expr.op == "not":
        return f"(!({_emit_expr(expr.args[0])}))"
    if expr.op in {"and", "or"}:
        operator = " && " if expr.op == "and" else " || "
        return "(" + operator.join(f"({_emit_expr(arg)})" for arg in expr.args) + ")"
    operators = {
        "eq": "==",
        "ne": "!=",
        "ult": "<",
        "ule": "<=",
        "ugt": ">",
        "uge": ">=",
        "bit_and": "&",
        "bit_or": "|",
        "bit_xor": "^",
        "add": "+",
        "sub": "-",
    }
    left = _emit_expr(expr.args[0])
    right = _emit_expr(expr.args[1])
    if expr.op in {"ult", "ule", "ugt", "uge"}:
        left = f"$unsigned({left})"
        right = f"$unsigned({right})"
    return f"(({left}) {operators[expr.op]} ({right}))"


def _width_decl(width: int) -> str:
    return f" [{width - 1}:0]" if width > 1 else ""


def _build_harness(
    *,
    top: str,
    ports: tuple[_Port, ...],
    clock_port: str,
    reset: Mapping[str, Any] | None,
    prop: _Property,
    property_index: int,
) -> str:
    wrapper = f"__rtl_closure_universal_{property_index:04d}"
    reset_port = str(reset["port"]) if reset else None
    previous_ports = {
        str(expr.value[0])
        for root in (prop.antecedent, prop.consequent)
        for expr in _walk_expr(root)
        if expr.op == "port" and expr.value[1] == "previous"
    }
    lines = ["`default_nettype none", f"module {wrapper};"]
    lines.extend(
        [
            "  reg __cm_clock = 1'b0;",
            "  always @($global_clock) __cm_clock <= !__cm_clock;",
        ]
    )
    if reset:
        lines.extend(
            [
                "  reg [1:0] __cm_reset_edges = 2'd0;",
                "  always @(posedge __cm_clock) begin",
                "    if (__cm_reset_edges < 2) __cm_reset_edges <= __cm_reset_edges + 1'b1;",
                "  end",
                "  wire __cm_reset_active = (__cm_reset_edges < 2);",
            ]
        )
    else:
        lines.append("  wire __cm_reset_active = 1'b0;")

    for port in ports:
        width = _width_decl(port.width)
        if port.name == clock_port:
            lines.append(f"  wire{width} __cm_port_{port.name} = __cm_clock;")
        elif port.name == reset_port:
            inactive = "!__cm_reset_active" if bool(reset["active_low"]) else "__cm_reset_active"
            lines.append(f"  wire{width} __cm_port_{port.name} = {inactive};")
        elif port.direction == "input":
            lines.append(f"  (* anyseq *) wire{width} __cm_port_{port.name};")
        else:
            lines.append(f"  wire{width} __cm_port_{port.name};")
    connections = ", ".join(
        f".{port.name}(__cm_port_{port.name})" for port in ports
    )
    lines.append(f"  {top} __cm_dut ({connections});")
    for port in ports:
        if port.name in previous_ports:
            lines.append(f"  reg{_width_decl(port.width)} __cm_prev_{port.name};")
    implication = f"(!({_emit_expr(prop.antecedent)})) || ({_emit_expr(prop.consequent)})"
    lines.extend(
        [
            "  reg __cm_past_valid = 1'b0;",
            f"  wire __cm_property_holds = {implication};",
            "  always @(posedge __cm_clock) begin",
            "    if (__cm_reset_active) begin",
            "      __cm_past_valid <= 1'b0;",
            "    end else begin",
            "      if (__cm_past_valid) assert(__cm_property_holds);",
            "      __cm_past_valid <= 1'b1;",
            "    end",
        ]
    )
    for port in ports:
        if port.name in previous_ports:
            lines.append(f"    __cm_prev_{port.name} <= __cm_port_{port.name};")
    lines.extend(["  end", "endmodule", "`default_nettype wire", ""])
    return "\n".join(lines)


def _walk_expr(expr: _Expr):
    yield expr
    for arg in expr.args:
        yield from _walk_expr(arg)


def _yosys_script(property_index: int, induction_depth: int, sat_timeout: int) -> str:
    wrapper = f"__rtl_closure_universal_{property_index:04d}"
    property_file = f"property-{property_index:04d}.sv"
    counterexample_file = f"property-{property_index:04d}-cex.json"
    return "\n".join(
        [
            f"read_verilog -formal -sv dut.sv {property_file}",
            f"hierarchy -check -top {wrapper}",
            "proc",
            "async2sync",
            "flatten",
            "memory_map",
            "opt_clean",
            f"select -module {wrapper}",
            (
                "sat -enable_undef -set-init-def -set-def-formal "
                f"-seq {induction_depth} -tempinduct -prove-asserts -verify "
                f"-timeout {sat_timeout} -show-public -dump_json {counterexample_file}"
            ),
            "",
        ]
    )


def render_universal_execution_inputs(
    *,
    top: str,
    ports: Iterable[Any],
    clock_port: str,
    reset: Mapping[str, Any] | None,
    property_value: Mapping[str, Any],
    property_index: int,
    induction_depth: int,
    timeout: int,
    requirement_ids: Iterable[str] | None = None,
) -> dict[str, str]:
    """Reconstruct the exact harness and Yosys script for release validation."""

    if isinstance(property_index, bool) or not isinstance(property_index, int) or property_index < 1:
        raise UniversalPropertyError("property_index must be a positive integer")
    if (
        isinstance(induction_depth, bool)
        or not isinstance(induction_depth, int)
        or not 2 <= induction_depth <= 64
    ):
        raise UniversalPropertyError("induction_depth must be an integer from 2 to 64")
    if isinstance(timeout, bool) or not isinstance(timeout, int) or not 1 <= timeout <= 3600:
        raise UniversalPropertyError("timeout must be an integer from 1 to 3600")
    normalized_ports = _normalize_ports(ports)
    port_map = {port.name: port for port in normalized_ports}
    if clock_port not in port_map:
        raise UniversalPropertyError(f"clock_port references unknown port {clock_port!r}")
    normalized_reset = _normalize_reset(reset, port_map)
    canonical = normalize_universal_properties(
        normalized_ports,
        [property_value],
        requirement_ids=requirement_ids,
        forbidden_ports={
            clock_port,
            *([normalized_reset["port"]] if normalized_reset else []),
        },
    )
    prop = _normalize_properties(canonical, port_map)[0]
    return {
        "harness": _build_harness(
            top=top,
            ports=normalized_ports,
            clock_port=clock_port,
            reset=normalized_reset,
            prop=prop,
            property_index=property_index,
        ),
        "script": _yosys_script(
            property_index,
            induction_depth,
            max(1, timeout - 5),
        ),
    }


def classify_universal_execution_log(returncode: int | None, log: str) -> str:
    """Classify a persisted Yosys transcript using the runtime verdict rules."""

    if isinstance(returncode, bool) or (
        returncode is not None and not isinstance(returncode, int)
    ):
        raise UniversalPropertyError("universal execution returncode must be integer or null")
    if not isinstance(log, str):
        raise UniversalPropertyError("universal execution log must be text")
    if returncode == 0 and _PASS_PATTERN.search(log):
        return "pass"
    if _FAIL_PATTERN.search(log):
        return "fail"
    if "timeout" in log.lower() or "exceeded" in log.lower():
        return "unknown"
    return "error"


def _normalize_reset(
    reset: Mapping[str, Any] | None, ports: Mapping[str, _Port]
) -> dict[str, Any] | None:
    if reset is None:
        return None
    if not isinstance(reset, Mapping):
        raise UniversalPropertyError("reset must be an object or null")
    unknown = set(reset) - {"port", "active_low", "asynchronous"}
    if unknown:
        raise UniversalPropertyError(
            f"reset has unsupported field(s): {', '.join(sorted(unknown))}"
        )
    if "port" not in reset or "active_low" not in reset:
        raise UniversalPropertyError("reset requires port and active_low")
    port_name = reset["port"]
    if not isinstance(port_name, str) or port_name not in ports:
        raise UniversalPropertyError(f"reset references unknown port {port_name!r}")
    port = ports[port_name]
    if port.direction != "input" or port.width != 1:
        raise UniversalPropertyError("reset port must be a one-bit input")
    if not isinstance(reset["active_low"], bool):
        raise UniversalPropertyError("reset.active_low must be boolean")
    asynchronous = reset.get("asynchronous", False)
    if not isinstance(asynchronous, bool):
        raise UniversalPropertyError("reset.asynchronous must be boolean")
    return {
        "port": port_name,
        "active_low": reset["active_low"],
        "asynchronous": asynchronous,
        "asserted_edges": 2,
    }


def _counterexample(path: Path, log: str, *, persistent: bool) -> dict[str, Any]:
    result: dict[str, Any] = {
        "available": False,
        "detail": log[-4000:],
    }
    if persistent:
        result["artifact"] = str(path)
    try:
        if path.stat().st_size > 4 * 1024 * 1024:
            result["detail"] = "counterexample wave exceeds the 4 MiB report limit"
            return result
        wave = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return result
    signals = []
    if isinstance(wave, Mapping):
        for row in wave.get("signal", [])[:128]:
            if not isinstance(row, Mapping):
                continue
            data = row.get("data")
            signals.append(
                {
                    "name": str(row.get("name", "")),
                    "trace": list(data[-32:]) if isinstance(data, list) else [],
                }
            )
    result.update({"available": True, "signals": signals})
    return result


def universal_proof_scope(
    *, induction_depth: int, reset: Mapping[str, Any] | None
) -> dict[str, Any]:
    """Return the exact claim boundary of the declarative proof provider.

    This is public so release validation can reconstruct the claim instead of
    trusting a report supplied by an earlier process.
    """

    normalized_reset = (
        {
            "port": reset["port"],
            "active_low": reset["active_low"],
            "asynchronous": reset.get("asynchronous", False),
            "asserted_edges": 2,
        }
        if reset
        else None
    )
    return {
        "claim": "listed_port_level_safety_properties_only",
        "property_class": "port_level_always_implication",
        "quantification": "all defined values of every non-clock input at every sampled cycle",
        "time_horizon": "unbounded when temporal induction succeeds",
        "proof_method": "Yosys SAT temporal induction",
        "induction_depth": induction_depth,
        "sampling": "port values immediately before each rising clock edge",
        "previous": "the preceding post-reset rising-edge sample",
        "warmup_cycles": 1,
        "initial_dut_state": (
            "arbitrary defined state followed by two asserted reset edges"
            if normalized_reset
            else "arbitrary defined state"
        ),
        "reset": normalized_reset,
        "capabilities": {
            "safety_properties": True,
            "liveness_properties": False,
            "eventuality_properties": False,
            "internal_state_references": False,
            "memory_index_references": False,
            "arbitrary_hdl_expressions": False,
            "supported_property_kinds": ["always_implication"],
            "supported_signal_visibility": ["declared_top_level_ports"],
            "supported_sample_offsets": ["current", "previous"],
            "supported_boolean_operators": ["not", "and", "or"],
            "supported_relational_operators": ["eq", "ne", "ult", "ule", "ugt", "uge"],
            "supported_bitvector_operators": ["bit_and", "bit_or", "bit_xor", "add", "sub"],
        },
        "limitations": [
            {
                "code": "top_level_ports_only",
                "description": "Only declared top-level ports may be referenced.",
            },
            {
                "code": "no_liveness_or_eventuality",
                "description": "Liveness and eventuality properties are not supported.",
            },
            {
                "code": "no_internal_state_or_memory_index",
                "description": "Internal state and memory-index references are not supported.",
            },
            {
                "code": "no_arbitrary_hdl_expressions",
                "description": "Only the allowlisted declarative operators are accepted.",
            },
            {
                "code": "unconstrained_environment",
                "description": (
                    "Inputs are unconstrained except for clock and the deterministic reset prefix."
                ),
            },
            {
                "code": "requirement_traceability_not_semantic_completeness",
                "description": (
                    "A requirement ID is a traceability link; proving linked properties does "
                    "not establish that they fully encode the natural-language requirement."
                ),
            },
        ],
    }


def universal_requirement_coverage(
    properties: Sequence[Mapping[str, Any]],
    requirement_ids: Iterable[str] | None = None,
) -> dict[str, Any]:
    """Recompute requirement-to-property traceability from observed rows.

    ``covered`` means that at least one listed property carries the requirement
    ID.  It deliberately does not mean that the natural-language requirement is
    semantically complete.  ``fully_proven`` is stricter but still only refers
    to the linked property rows.
    """

    mapped_statuses: dict[str, list[str]] = {}
    for row in properties:
        if not isinstance(row, Mapping):
            continue
        status = str(row.get("status") or "unknown")
        references = row.get("requirement_ids") or []
        if isinstance(references, (str, bytes)) or not isinstance(references, Sequence):
            continue
        for requirement_id in references:
            if not isinstance(requirement_id, str) or not _REPORT_ID.fullmatch(
                requirement_id
            ):
                continue
            mapped_statuses.setdefault(requirement_id, []).append(status)

    mapped_ids = sorted(mapped_statuses)
    explicit_universe = requirement_ids is not None
    declared_ids: list[str] = []
    if explicit_universe:
        seen: set[str] = set()
        for requirement_id in requirement_ids or ():
            if not isinstance(requirement_id, str) or not _REPORT_ID.fullmatch(
                requirement_id
            ):
                raise UniversalPropertyError(
                    "requirement_ids contains an invalid requirement id"
                )
            if requirement_id not in seen:
                declared_ids.append(requirement_id)
                seen.add(requirement_id)
        declared_ids.sort()
    else:
        declared_ids = list(mapped_ids)

    declared_set = set(declared_ids)
    covered_ids = sorted(declared_set & set(mapped_ids))
    uncovered_ids = sorted(declared_set - set(mapped_ids))
    fully_proven_ids = sorted(
        requirement_id
        for requirement_id in covered_ids
        if mapped_statuses[requirement_id]
        and all(status == "pass" for status in mapped_statuses[requirement_id])
    )
    failed_ids = sorted(
        requirement_id
        for requirement_id in covered_ids
        if "fail" in mapped_statuses[requirement_id]
    )
    inconclusive_ids = sorted(
        requirement_id
        for requirement_id in covered_ids
        if any(status in {"unknown", "error"} for status in mapped_statuses[requirement_id])
    )
    return {
        "requirement_universe_source": (
            "verification.requirements"
            if explicit_universe
            else "property_references_only"
        ),
        "assessment_complete_against_spec": explicit_universe,
        "declared_requirement_ids": declared_ids,
        "covered_requirement_ids": covered_ids,
        "uncovered_requirement_ids": uncovered_ids,
        "fully_proven_requirement_ids": fully_proven_ids,
        "failed_requirement_ids": failed_ids,
        "inconclusive_requirement_ids": inconclusive_ids,
        "all_declared_requirements_mapped": (
            not uncovered_ids if explicit_universe else None
        ),
        "semantic_completeness_claimed": False,
        "coverage_semantics": (
            "covered means referenced by at least one listed universal property; "
            "it does not mean the natural-language requirement is fully specified"
        ),
    }


def bind_universal_requirement_coverage(
    report: Mapping[str, Any], requirement_ids: Iterable[str]
) -> dict[str, Any]:
    """Return a report bound to the complete requirement universe in the spec."""

    bound = json.loads(json.dumps(report))
    rows = bound.get("properties")
    bound["requirement_coverage"] = universal_requirement_coverage(
        rows if isinstance(rows, list) else [], requirement_ids
    )
    return bound


def run_universal_properties(
    rtl: str,
    top: str,
    ports: Iterable[Any],
    clock_port: str,
    reset: Mapping[str, Any] | None,
    properties: Sequence[Any],
    artifact_dir_or_none: str | Path | None = None,
    timeout: int = 120,
    *,
    induction_depth: int = 8,
    requirement_ids: Iterable[str] | None = None,
) -> dict[str, Any]:
    """Prove safe declarative properties for all symbolic input traces.

    ``timeout`` is one shared deadline for the complete property set.  A
    passing result is an unbounded temporal-induction proof, not a finite
    test-vector replay.  Persistent runs checkpoint conclusive property rows;
    an identical retry only executes previously inconclusive rows.
    """

    if not isinstance(rtl, str) or not rtl.strip():
        raise UniversalPropertyError("rtl must be non-empty text")
    if not isinstance(top, str) or not _VERILOG_ID.fullmatch(top):
        raise UniversalPropertyError("top must be a Verilog identifier")
    if not isinstance(clock_port, str) or not _VERILOG_ID.fullmatch(clock_port):
        raise UniversalPropertyError("clock_port must be a Verilog identifier")
    if isinstance(timeout, bool) or not isinstance(timeout, int) or not 1 <= timeout <= 3600:
        raise UniversalPropertyError("timeout must be an integer from 1 to 3600")
    if (
        isinstance(induction_depth, bool)
        or not isinstance(induction_depth, int)
        or not 2 <= induction_depth <= 64
    ):
        raise UniversalPropertyError("induction_depth must be an integer from 2 to 64")

    normalized_ports = _normalize_ports(ports)
    port_map = {port.name: port for port in normalized_ports}
    if clock_port not in port_map:
        raise UniversalPropertyError(f"clock_port references unknown port {clock_port!r}")
    clock = port_map[clock_port]
    if clock.direction != "input" or clock.width != 1:
        raise UniversalPropertyError("clock_port must be a one-bit input")
    normalized_reset = _normalize_reset(reset, port_map)
    declared_requirement_ids = (
        tuple(requirement_ids) if requirement_ids is not None else None
    )
    property_json = normalize_universal_properties(
        normalized_ports,
        properties,
        requirement_ids=declared_requirement_ids,
        forbidden_ports={
            clock_port,
            *([normalized_reset["port"]] if normalized_reset else []),
        },
    )
    normalized_properties = _normalize_properties(property_json, port_map)
    rtl_hash = _sha256_text(rtl)
    scope = universal_proof_scope(
        induction_depth=induction_depth, reset=normalized_reset
    )
    report: dict[str, Any] = {
        "schema_version": 2,
        "provider": "yosys_sat_universal_properties",
        "authoritative": True,
        "authoritative_for": "listed_port_level_safety_properties_only",
        "complete_behavioral_proof": False,
        "complete_requirement_proof": False,
        "top": top,
        "clock_port": clock_port,
        "scope": scope,
        "rtl_sha256": rtl_hash,
        "properties_sha256": _canonical_hash(property_json),
        "property_count": len(normalized_properties),
        "verify_image": verify_image_ref(),
        "global_timeout_seconds": timeout,
        "timeout_scope": "complete_property_set",
        "properties": [],
    }
    if not normalized_properties:
        report.update(
            {
                "status": "not_requested",
                "universal_proof": False,
                "passed_property_count": 0,
                "passed_property_ids": [],
                "failed_property_ids": [],
                "unknown_property_ids": [],
                "error_property_ids": [],
                "global_deadline_exhausted": False,
                "requirement_coverage": universal_requirement_coverage(
                    [], declared_requirement_ids
                ),
                "detail": "no universal properties were supplied",
            }
        )
        if artifact_dir_or_none is not None:
            artifact_dir = Path(artifact_dir_or_none)
            artifact_dir.mkdir(parents=True, exist_ok=True)
            (artifact_dir / "universal-properties.json").write_text(
                json.dumps(report, indent=2, ensure_ascii=False) + "\n",
                encoding="utf-8",
            )
        return report

    persistent = artifact_dir_or_none is not None
    temporary: tempfile.TemporaryDirectory[str] | None = None
    if persistent:
        artifact_dir = Path(artifact_dir_or_none)
        artifact_dir.mkdir(parents=True, exist_ok=True)
        work = artifact_dir / "universal-properties"
        work.mkdir(parents=True, exist_ok=True)
    else:
        temporary = tempfile.TemporaryDirectory(prefix="rtl-closure-universal-")
        work = Path(temporary.name)
    (work / "dut.sv").write_text(rtl + "\n", encoding="utf-8")

    execution_identity = {
        "schema_version": 1,
        "rtl_sha256": rtl_hash,
        "properties_sha256": report["properties_sha256"],
        "top": top,
        "ports": [
            {
                "name": port.name,
                "direction": port.direction,
                "width": port.width,
                "signed": port.signed,
            }
            for port in normalized_ports
        ],
        "clock_port": clock_port,
        "reset": normalized_reset,
        "induction_depth": induction_depth,
        "verify_image": verify_image_ref(),
    }
    execution_identity_sha256 = _canonical_hash(execution_identity)
    report["execution_identity_sha256"] = execution_identity_sha256
    checkpoint_path = (
        Path(artifact_dir_or_none) / "universal-checkpoint.json"
        if persistent
        else None
    )
    completed_rows: dict[str, dict[str, Any]] = {}
    if checkpoint_path is not None and checkpoint_path.is_file():
        try:
            checkpoint = json.loads(checkpoint_path.read_text(encoding="utf-8"))
            payload = dict(checkpoint) if isinstance(checkpoint, dict) else {}
            digest = payload.pop("checkpoint_sha256", None)
            rows = checkpoint.get("completed") if isinstance(checkpoint, dict) else None
            if (
                isinstance(checkpoint, dict)
                and checkpoint.get("schema_version") == 1
                and digest == _canonical_hash(payload)
                and checkpoint.get("execution_identity_sha256")
                == execution_identity_sha256
                and isinstance(rows, list)
            ):
                for row in rows:
                    if (
                        isinstance(row, dict)
                        and isinstance(row.get("id"), str)
                        and row.get("status") in {"pass", "fail"}
                        and row.get("rtl_sha256") == rtl_hash
                    ):
                        completed_rows[row["id"]] = row
        except (OSError, ValueError, TypeError):
            completed_rows = {}

    def persist_checkpoint() -> None:
        if checkpoint_path is None:
            return
        checkpoint = {
            "schema_version": 1,
            "execution_identity_sha256": execution_identity_sha256,
            "completed": [completed_rows[key] for key in sorted(completed_rows)],
        }
        checkpoint["checkpoint_sha256"] = _canonical_hash(checkpoint)
        _write_json_atomic(checkpoint_path, checkpoint)

    deadline = time.monotonic() + timeout
    deadline_exhausted = False
    try:
        for index, prop in enumerate(normalized_properties, 1):
            normalized = _property_json(prop)
            property_sha256 = _canonical_hash(normalized)
            cached = completed_rows.get(prop.property_id)
            if cached is not None and cached.get("property_sha256") == property_sha256:
                resumed = json.loads(json.dumps(cached))
                resumed["resumed_from_checkpoint"] = True
                report["properties"].append(resumed)
                continue
            harness = _build_harness(
                top=top,
                ports=normalized_ports,
                clock_port=clock_port,
                reset=normalized_reset,
                prop=prop,
                property_index=index,
            )
            script = _yosys_script(index, induction_depth, max(1, timeout - 5))
            harness_path = work / f"property-{index:04d}.sv"
            script_path = work / f"property-{index:04d}.ys"
            log_path = work / f"property-{index:04d}.log"
            cex_path = work / f"property-{index:04d}-cex.json"
            harness_path.write_text(harness, encoding="utf-8")
            script_path.write_text(script, encoding="utf-8")
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                deadline_exhausted = True
                detail = (
                    f"universal property set exceeded its shared {timeout}-second deadline"
                )
                skipped = {
                    "id": prop.property_id,
                    "kind": "always_implication",
                    "requirement_ids": list(prop.requirement_ids),
                    "rtl_sha256": rtl_hash,
                    "property_sha256": property_sha256,
                    "universal_proof": False,
                    "counterexample": None,
                    "status": "unknown",
                    "detail": detail,
                    "executed": False,
                }
                if persistent:
                    log_path.write_text(detail + "\n", encoding="utf-8")
                    skipped["artifacts"] = {
                        "dut": str(work / "dut.sv"),
                        "harness": str(harness_path),
                        "script": str(script_path),
                        "log": str(log_path),
                    }
                report["properties"].append(skipped)
                continue
            property_timeout = max(0.001, min(float(timeout), remaining))
            # Keep the script deterministic for release-evidence reconstruction.
            # The outer container timeout enforces the shorter remaining global
            # budget even though the SAT command retains the configured limit.
            try:
                cex_path.unlink()
            except FileNotFoundError:
                pass
            command = _docker_base(work, read_only_mount=False) + [
                "--entrypoint",
                "yosys",
                verify_image_ref(),
                "-Q",
                "-s",
                script_path.name,
            ]
            result: dict[str, Any] = {
                "id": prop.property_id,
                "kind": "always_implication",
                "requirement_ids": list(prop.requirement_ids),
                "rtl_sha256": rtl_hash,
                "property_sha256": property_sha256,
                "universal_proof": False,
                "counterexample": None,
                "executed": True,
            }
            try:
                completed = _run_container(
                    command, property_timeout, purpose=f"universal-{index:04d}"
                )
            except subprocess.TimeoutExpired:
                deadline_exhausted = True
                result.update(
                    {
                        "status": "unknown",
                        "detail": (
                            f"universal proof exhausted the shared {timeout}-second deadline"
                        ),
                    }
                )
                log = result["detail"]
            except OSError as exc:
                result.update({"status": "error", "detail": str(exc)})
                log = result["detail"]
            else:
                log = (completed.stdout + "\n" + completed.stderr).strip()
                result["returncode"] = completed.returncode
                execution_status = classify_universal_execution_log(
                    completed.returncode, log
                )
                if execution_status == "pass":
                    result.update(
                        {
                            "status": "pass",
                            "universal_proof": True,
                            "detail": (
                                "temporal induction proved the property for all "
                                "symbolic input traces"
                            ),
                        }
                    )
                elif execution_status == "fail":
                    result.update(
                        {
                            "status": "fail",
                            "detail": log[-4000:],
                            "counterexample": _counterexample(
                                cex_path, log, persistent=persistent
                            ),
                        }
                    )
                elif execution_status == "unknown":
                    result.update(
                        {
                            "status": "unknown",
                            "detail": "Yosys SAT did not complete the temporal induction proof",
                        }
                    )
                else:
                    result.update(
                        {
                            "status": "error",
                            "detail": log[-4000:] or "Yosys returned no proof verdict",
                        }
                    )
            if persistent:
                log_path.write_text(log + "\n", encoding="utf-8")
                result["artifacts"] = {
                    "dut": str(work / "dut.sv"),
                    "harness": str(harness_path),
                    "script": str(script_path),
                    "log": str(log_path),
                }
                if result["status"] == "fail" and cex_path.exists():
                    result["artifacts"]["counterexample"] = str(cex_path)
            report["properties"].append(result)
            if result["status"] in {"pass", "fail"}:
                completed_rows[prop.property_id] = json.loads(json.dumps(result))
                persist_checkpoint()
    finally:
        if temporary is not None:
            temporary.cleanup()

    by_status = {
        status: [row["id"] for row in report["properties"] if row["status"] == status]
        for status in ("pass", "fail", "unknown", "error")
    }
    # A concrete counterexample is a conclusive verdict for the conjunction,
    # even when another property times out.  Repair that failure first; a
    # later candidate will still be blocked if the remaining proof is
    # inconclusive.
    if by_status["fail"]:
        overall_status = "fail"
    elif by_status["error"]:
        overall_status = "error"
    elif by_status["unknown"]:
        overall_status = "unknown"
    else:
        overall_status = "pass"
    if overall_status == "pass":
        detail = (
            f"proved all {len(normalized_properties)} listed port-level safety "
            "properties for every symbolic input trace; this is not a claim of "
            "complete natural-language requirement coverage"
        )
    elif overall_status == "fail":
        detail = "counterexample(s) found for: " + ", ".join(by_status["fail"])
    elif overall_status == "unknown":
        detail = "proof timed out or remained unknown for: " + ", ".join(
            by_status["unknown"]
        )
    else:
        detail = "proof provider error for: " + ", ".join(by_status["error"])
    report.update(
        {
            "status": overall_status,
            "universal_proof": overall_status == "pass",
            "detail": detail,
            "passed_property_count": len(by_status["pass"]),
            "passed_property_ids": by_status["pass"],
            "failed_property_ids": by_status["fail"],
            "unknown_property_ids": by_status["unknown"],
            "error_property_ids": by_status["error"],
            "global_deadline_exhausted": deadline_exhausted,
            "requirement_coverage": universal_requirement_coverage(
                report["properties"], declared_requirement_ids
            ),
        }
    )
    if persistent:
        (Path(artifact_dir_or_none) / "universal-properties.json").write_text(
            json.dumps(report, indent=2, ensure_ascii=False) + "\n",
            encoding="utf-8",
        )
    return report


def merge_universal_result(
    simulation: Mapping[str, Any],
    report: Mapping[str, Any],
    *,
    required: bool,
) -> dict[str, Any]:
    """Merge property verdicts without counting inconclusive rows as checks."""

    result = json.loads(json.dumps(simulation))
    result["universal_properties"] = json.loads(json.dumps(report))
    passed_ids = list(result.get("passed_check_ids") or [])
    mismatches = list(result.get("mismatches") or [])
    base_checks = int(result.get("total_checks") or 0)
    checked = 0
    universal_failures: list[dict[str, Any]] = []
    for row in report.get("properties", []):
        if not isinstance(row, Mapping) or row.get("status") not in {"pass", "fail"}:
            continue
        ordinal = base_checks + checked
        checked += 1
        check_id = f"universal:{row.get('id', 'unknown')}"
        if row.get("status") == "pass":
            if check_id not in passed_ids:
                passed_ids.append(check_id)
            continue
        mismatch = {
            "check_id": check_id,
            "ordinal": ordinal,
            "scenario_index": None,
            "scenario_id": None,
            "requirement_ids": list(row.get("requirement_ids") or []),
            "cycle": None,
            "phase": "universal_formal",
            "signal": row.get("id"),
            "expected": "property holds for every symbolic input trace",
            "actual": row.get("counterexample") or row.get("detail"),
            "inputs": {},
            "failure_class": "universal_counterexample",
            "property_id": row.get("id"),
        }
        mismatches.append(mismatch)
        universal_failures.append(mismatch)

    result["total_checks"] = base_checks + checked
    result["passed_check_ids"] = passed_ids
    result["mismatches"] = mismatches
    result["mismatched"] = int(result.get("mismatched") or 0) + len(
        universal_failures
    )
    if universal_failures:
        result["passed"] = False
        result["source"] = "universal_formal"
        result["failure_class"] = "universal_counterexample"
        first = universal_failures[0]["ordinal"]
        previous = result.get("first_failure_ordinal")
        result["first_failure_ordinal"] = (
            first if previous is None else min(int(previous), first)
        )
        result["detail"] = (
            f"{len(universal_failures)} universal property counterexample(s)"
        )

    status = report.get("status")
    inconclusive = status in {"unknown", "error", "not_requested"}
    if required and inconclusive and not universal_failures:
        ordinal = int(result["total_checks"])
        mismatch = {
            "check_id": "universal:PROVIDER_CONCLUSIVE",
            "ordinal": ordinal,
            "scenario_index": None,
            "scenario_id": None,
            "requirement_ids": [],
            "cycle": None,
            "phase": "universal_formal",
            "signal": None,
            "expected": "all required universal properties proven",
            "actual": status,
            "inputs": {},
            "failure_class": "universal_inconclusive",
        }
        result["mismatches"].append(mismatch)
        result["total_checks"] = ordinal + 1
        result["mismatched"] = int(result["mismatched"]) + 1
        result["passed"] = False
        result["source"] = "universal_formal"
        result["failure_class"] = "universal_inconclusive"
        result["first_failure_ordinal"] = ordinal
        result["detail"] = str(report.get("detail") or status)
    return result


__all__ = [
    "bind_universal_requirement_coverage",
    "UniversalPropertyError",
    "normalize_universal_properties",
    "merge_universal_result",
    "run_universal_properties",
    "universal_proof_scope",
    "universal_requirement_coverage",
]
