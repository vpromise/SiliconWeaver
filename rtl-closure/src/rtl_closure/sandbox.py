"""No-network execution for model-generated RTL and Python."""

from __future__ import annotations

import json
import ast
import hashlib
import os
import random
import re
import subprocess
import tempfile
import textwrap
import uuid
from pathlib import Path
from typing import Any

from .errors import ContainerInfrastructureError
from .logic_value import from_binary, from_integer, matches
from .runtime import SANDBOX_IMAGE as SANDBOX_IMAGE, sandbox_image_ref
FORBIDDEN_RTL = (
    (re.compile(r"`\s*include\b", re.IGNORECASE), "preprocessor include"),
    (re.compile(r"\binitial\b", re.IGNORECASE), "initial block"),
    (
        re.compile(
            r"(?:\b(?:input|output|inout)\b[^;]*?\b(?:reg|logic|bit)\b|"
            r"(?:\A|;)\s*(?:reg|logic|bit)\b)"
            r"[^;{}]*?(?<![=!<>])=(?!=)",
            re.IGNORECASE,
        ),
        "state declaration initializer",
    ),
    (
        re.compile(r"\b(force|release|fork|join|wait)\b", re.IGNORECASE),
        "non-synthesizable control",
    ),
    (re.compile(r"\b(import|export)\s+\"?DPI", re.IGNORECASE), "DPI"),
    (
        re.compile(
            r"\$(?:system|fopen|fclose|fread|fwrite|"
            r"readmem[hb]?|writemem[hb]?|display|write|monitor|finish|stop|"
            r"dump\w*|random|urandom(?:_range)?)\b",
            re.IGNORECASE,
        ),
        "unsafe system task or function",
    ),
    # A numeric delay is never synthesizable.  ``#(...)`` is deliberately not
    # matched because it is also the legal parameter-list syntax of a module.
    (re.compile(r"#\s*(?:\d|[A-Za-z_])"), "delay control"),
)

_VARIABLE_DATA_TYPE = re.compile(
    r"\b(?:reg|logic|bit|integer|int|byte|shortint|longint|time)\b",
    re.IGNORECASE,
)
_PORT_DIRECTION = re.compile(r"\b(?:input|output|inout)\b", re.IGNORECASE)
_PARAMETER_DECLARATION = re.compile(r"\b(?:localparam|parameter)\b", re.IGNORECASE)
_SIMPLE_ASSIGNMENT = re.compile(r"(?<![!<>=+\-*/%&|^])=(?!=)")
_SYSTEM_IDENTIFIER = re.compile(r"\$[A-Za-z_][A-Za-z0-9_$]*")
_MODULE_DECLARATION = re.compile(
    r"\bmodule\s+(?P<name>[A-Za-z_$][A-Za-z0-9_$]*)"
)
_ENDMODULE_LABEL = re.compile(
    r"\bendmodule\s*:\s*(?P<label>[A-Za-z_$][A-Za-z0-9_$]*)"
)
_ALLOWED_RTL_SYSTEM_FUNCTIONS = {
    "$bits",
    "$clog2",
    "$dimensions",
    "$high",
    "$left",
    "$low",
    "$right",
    "$signed",
    "$size",
    "$unpacked_dimensions",
    "$unsigned",
}


def _mask_rtl_comments_and_strings(code: str) -> str:
    """Preserve offsets while removing text that cannot contain HDL constructs."""

    chars = list(code)
    index = 0
    state = "code"
    while index < len(chars):
        if state == "code":
            if code.startswith("//", index):
                chars[index] = chars[index + 1] = " "
                index += 2
                state = "line_comment"
                continue
            if code.startswith("/*", index):
                chars[index] = chars[index + 1] = " "
                index += 2
                state = "block_comment"
                continue
            if chars[index] == '"':
                chars[index] = " "
                index += 1
                state = "string"
                continue
        elif state == "line_comment":
            if chars[index] == "\n":
                state = "code"
            else:
                chars[index] = " "
        elif state == "block_comment":
            if code.startswith("*/", index):
                chars[index] = chars[index + 1] = " "
                index += 2
                state = "code"
                continue
            if chars[index] != "\n":
                chars[index] = " "
        else:
            if chars[index] == "\\":
                chars[index] = " "
                if index + 1 < len(chars):
                    chars[index + 1] = " "
                    index += 2
                    continue
            elif chars[index] == '"':
                chars[index] = " "
                state = "code"
            elif chars[index] != "\n":
                chars[index] = " "
        index += 1
    return "".join(chars)


def _rtl_module_declarations(code: str) -> list[re.Match[str]]:
    return list(_MODULE_DECLARATION.finditer(_mask_rtl_comments_and_strings(code)))


def _rename_module_declaration(
    code: str, declaration: re.Match[str], target: str
) -> str:
    """Rename one real declaration and its matching optional end label."""

    original = declaration.group("name")
    replacements = [(declaration.start("name"), declaration.end("name"), target)]
    masked = _mask_rtl_comments_and_strings(code)
    closing = next(
        (
            match
            for match in _ENDMODULE_LABEL.finditer(masked, declaration.end())
            if match.group("label") == original
        ),
        None,
    )
    if closing is not None:
        replacements.append((closing.start("label"), closing.end("label"), target))
    result = code
    for start, end, replacement in sorted(replacements, reverse=True):
        result = result[:start] + replacement + result[end:]
    return result


def _enum_member_ranges(code: str) -> list[tuple[int, int]]:
    """Return enum member-list ranges so enumerator values are not initializers."""

    ranges: list[tuple[int, int]] = []
    for enum in re.finditer(r"\benum\b", code, re.IGNORECASE):
        start = code.find("{", enum.end())
        if start < 0:
            continue
        depth = 0
        for index in range(start, len(code)):
            if code[index] == "{":
                depth += 1
            elif code[index] == "}":
                depth -= 1
                if depth == 0:
                    ranges.append((start, index + 1))
                    break
    return ranges


def _has_state_declaration_initializer(code: str) -> bool:
    """Detect declaration assignments while allowing net declaration assigns.

    This is an early, fail-closed check.  ``yosys_check`` independently checks
    the parsed netlist's ``init`` attributes, including user-defined types that
    cannot be classified reliably from tokens alone.
    """

    enum_ranges = _enum_member_ranges(code)
    statement_start = 0
    for assignment in _SIMPLE_ASSIGNMENT.finditer(code):
        if any(start <= assignment.start() < end for start, end in enum_ranges):
            continue
        statement_start = code.rfind(";", 0, assignment.start()) + 1
        prefix = code[statement_start : assignment.start()]
        parameter = list(_PARAMETER_DECLARATION.finditer(prefix))
        data_types = list(_VARIABLE_DATA_TYPE.finditer(prefix))
        directions = list(_PORT_DIRECTION.finditer(prefix))
        declaration_markers = data_types + directions
        if not declaration_markers:
            continue
        for_headers = list(re.finditer(r"\bfor\s*\(", prefix, re.IGNORECASE))
        if for_headers and ")" not in prefix[for_headers[-1].end() :]:
            # A SystemVerilog loop-local variable is elaboration/control state,
            # not a persistent hardware state initializer. Yosys still parses
            # and synthesizes the candidate in the mandatory downstream check.
            continue
        last_declaration = max(match.start() for match in declaration_markers)
        if parameter and max(match.start() for match in parameter) < last_declaration:
            # ``parameter integer WIDTH = ...`` is a constant declaration.
            continue
        if parameter and not data_types and not directions:
            continue
        return True
    return False


def _docker_base(workdir: Path, *, read_only_mount: bool) -> list[str]:
    mount_mode = "ro" if read_only_mount else "rw"
    return [
        "docker",
        "run",
        "--rm",
        "--network",
        "none",
        "--read-only",
        "--cap-drop",
        "ALL",
        "--security-opt",
        "no-new-privileges",
        "--pids-limit",
        "64",
        "--memory",
        "512m",
        "--cpus",
        "1",
        "--user",
        f"{os.getuid()}:{os.getgid()}",
        "--tmpfs",
        "/tmp:rw,nosuid,nodev,size=64m",
        "-e",
        "HOME=/tmp",
        "-e",
        "PYTHONDONTWRITEBYTECODE=1",
        "-v",
        f"{workdir}:/work:{mount_mode}",
        "-w",
        "/work",
    ]


def check_rtl_policy(code: str) -> None:
    if len(code.encode("utf-8")) > 2 * 1024 * 1024:
        raise RuntimeError("generated RTL exceeds the 2 MiB safety limit")
    scanned = _mask_rtl_comments_and_strings(code)
    for pattern, label in FORBIDDEN_RTL:
        if pattern.search(scanned):
            raise RuntimeError(f"generated RTL contains forbidden {label}")
    unsupported = sorted(
        {
            match.group(0)
            for match in _SYSTEM_IDENTIFIER.finditer(scanned)
            if match.group(0).lower() not in _ALLOWED_RTL_SYSTEM_FUNCTIONS
        }
    )
    if unsupported:
        raise RuntimeError(
            "generated RTL contains forbidden unsafe system task or function: "
            + ", ".join(unsupported[:16])
        )
    if _has_state_declaration_initializer(scanned):
        raise RuntimeError("generated RTL contains forbidden state declaration initializer")


def normalize_top_module(code: str, design_name: str) -> str:
    declarations = _rtl_module_declarations(code)
    names = [match.group("name") for match in declarations]
    if design_name in names:
        return code
    candidates = [name for name in ("top_module", "TopModule") if name in names]
    if len(candidates) == 1:
        old = candidates[0]
    elif len(names) == 1:
        old = names[0]
    else:
        raise RuntimeError(f"cannot identify generated top module; declarations: {names}")
    declaration = next(
        match for match in declarations if match.group("name") == old
    )
    return _rename_module_declaration(code, declaration, design_name)


def parse_contract_ports(verilog_code: str) -> tuple[list[tuple[str, int]], list[tuple[str, int]]]:
    """Parse the constant-width ANSI declarations emitted by render_interface."""
    code = re.sub(r"//[^\n]*", "", verilog_code)
    code = re.sub(r"/\*[\s\S]*?\*/", "", code)
    inputs: list[tuple[str, int]] = []
    outputs: list[tuple[str, int]] = []
    pattern = re.compile(
        r"\b(input|output)\s+(?:wire|reg|logic)?\s*(?:signed\s+)?"
        r"(?:\[(\d+)\s*:\s*(\d+)\]\s*)?([A-Za-z_][A-Za-z0-9_]*)"
    )
    for match in pattern.finditer(code):
        direction, msb, lsb, name = match.groups()
        width = abs(int(msb) - int(lsb)) + 1 if msb is not None else 1
        (inputs if direction == "input" else outputs).append((name, width))
    return inputs, outputs


def _top_module_region(rtl: str, top_name: str) -> str:
    code = re.sub(r"//[^\n]*", lambda match: " " * len(match.group(0)), rtl)
    code = re.sub(
        r"/\*[\s\S]*?\*/",
        lambda match: "".join("\n" if char == "\n" else " " for char in match.group(0)),
        code,
    )
    start_match = re.search(r"\bmodule\s+" + re.escape(top_name) + r"\b", code)
    if not start_match:
        raise RuntimeError(f"generated RTL does not define top module {top_name}")
    end_match = re.search(r"\bendmodule\b", code[start_match.end() :])
    if not end_match:
        raise RuntimeError(f"generated RTL top module {top_name} has no endmodule")
    end = start_match.end() + end_match.end()
    return rtl[start_match.start() : end]


def _matching_paren(code: str, start: int) -> int:
    depth = 0
    for index in range(start, len(code)):
        if code[index] == "(":
            depth += 1
        elif code[index] == ")":
            depth -= 1
            if depth == 0:
                return index
    raise RuntimeError("generated RTL has an unterminated module header")


def _top_module_header(rtl: str, top_name: str) -> tuple[str, str]:
    region = _top_module_region(rtl, top_name)
    match = re.search(r"\bmodule\s+" + re.escape(top_name) + r"\b", region)
    assert match is not None
    index = match.end()
    while index < len(region) and region[index].isspace():
        index += 1
    if index < len(region) and region[index] == "#":
        parameter_start = region.find("(", index + 1)
        if parameter_start < 0:
            raise RuntimeError("generated RTL has an invalid parameter list")
        index = _matching_paren(region, parameter_start) + 1
    port_start = region.find("(", index)
    if port_start < 0:
        raise RuntimeError("generated RTL top module has no ANSI port list")
    port_end = _matching_paren(region, port_start)
    return region[port_start + 1 : port_end], region[match.end() : port_start]


def _constant_expression(expression: str, parameters: dict[str, int]) -> int:
    operators = {
        ast.Add: lambda a, b: a + b,
        ast.Sub: lambda a, b: a - b,
        ast.Mult: lambda a, b: a * b,
        ast.FloorDiv: lambda a, b: a // b,
        ast.LShift: lambda a, b: a << b,
        ast.RShift: lambda a, b: a >> b,
        ast.BitOr: lambda a, b: a | b,
        ast.BitAnd: lambda a, b: a & b,
    }

    def visit(node):
        if isinstance(node, ast.Expression):
            return visit(node.body)
        if isinstance(node, ast.Constant) and isinstance(node.value, int):
            return node.value
        if isinstance(node, ast.Name) and node.id in parameters:
            return parameters[node.id]
        if isinstance(node, ast.UnaryOp) and isinstance(node.op, (ast.UAdd, ast.USub)):
            value = visit(node.operand)
            return value if isinstance(node.op, ast.UAdd) else -value
        if isinstance(node, ast.BinOp) and type(node.op) in operators:
            return operators[type(node.op)](visit(node.left), visit(node.right))
        raise ValueError(f"unsupported constant expression: {expression}")

    return int(visit(ast.parse(expression.strip(), mode="eval")))


def _split_port_segments(header: str) -> list[str]:
    rows: list[str] = []
    start = 0
    depths = {"(": 0, "[": 0, "{": 0}
    closers = {")": "(", "]": "[", "}": "{"}
    for index, character in enumerate(header):
        if character in depths:
            depths[character] += 1
        elif character in closers:
            opener = closers[character]
            depths[opener] = max(0, depths[opener] - 1)
        elif character == "," and not any(depths.values()):
            rows.append(header[start:index].strip())
            start = index + 1
    rows.append(header[start:].strip())
    return [row for row in rows if row]


def check_exact_interface(rtl: str, expected_ports, top_name: str) -> None:
    """Reject top-level port drift before any generated RTL is published."""
    header, _prefix = _top_module_header(rtl, top_name)
    region = _top_module_region(rtl, top_name)
    scanned_region = re.sub(r"//[^\n]*", "", region)
    scanned_region = re.sub(r"/\*[\s\S]*?\*/", "", scanned_region)
    parameters: dict[str, int] = {}
    for match in re.finditer(
        r"\bparameter(?:\s+(?:integer|int))?\s+([A-Za-z_]\w*)\s*=\s*([^,;\)]+)",
        scanned_region,
    ):
        try:
            parameters[match.group(1)] = _constant_expression(match.group(2), parameters)
        except (SyntaxError, ValueError, ZeroDivisionError):
            continue

    def parse_declaration(
        declaration_text: str,
        *,
        inherited_direction: str | None = None,
        inherited_width: int = 1,
        inherited_signed: bool = False,
    ) -> tuple[str | None, int, bool, list[str]]:
        declaration = re.match(r"^(input|output)\b", declaration_text)
        direction = inherited_direction
        width = inherited_width
        signed = inherited_signed
        remainder = declaration_text.strip()
        if declaration:
            direction = declaration.group(1)
            width = 1
            signed = False
            remainder = remainder[declaration.end() :].strip()
            while True:
                qualifier = re.match(r"^(wire|reg|logic|signed|unsigned)\b", remainder)
                if qualifier is None:
                    break
                if qualifier.group(1) == "signed":
                    signed = True
                elif qualifier.group(1) == "unsigned":
                    signed = False
                remainder = remainder[qualifier.end() :].strip()
            range_match = re.match(r"^\[([^:\]]+):([^\]]+)\]", remainder)
            if range_match:
                try:
                    msb = _constant_expression(range_match.group(1), parameters)
                    lsb = _constant_expression(range_match.group(2), parameters)
                except (SyntaxError, ValueError, ZeroDivisionError) as exc:
                    raise RuntimeError(
                        f"cannot resolve top-level port width in {declaration_text}: {exc}"
                    ) from exc
                width = abs(msb - lsb) + 1
                remainder = remainder[range_match.end() :].strip()
        names = [name.strip() for name in remainder.split(",")]
        if not names or any(
            re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", name) is None for name in names
        ):
            raise RuntimeError(
                f"cannot parse top-level port declaration: {declaration_text}"
            )
        return direction, width, signed, names

    segments = _split_port_segments(header)
    non_ansi_names = [
        segment.strip()
        for segment in segments
        if re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", segment.strip())
    ]
    if len(non_ansi_names) == len(segments):
        declarations: dict[str, tuple[str, int, bool]] = {}
        for match in re.finditer(r"\b(input|output)\b([^;]*);", scanned_region):
            direction, width, signed, names = parse_declaration(
                match.group(1) + match.group(2)
            )
            assert direction is not None
            for name in names:
                if name in declarations:
                    raise RuntimeError(f"duplicate top-level port declaration: {name}")
                declarations[name] = (direction, width, signed)
        missing = [name for name in non_ansi_names if name not in declarations]
        extra = sorted(set(declarations) - set(non_ansi_names))
        if missing or extra:
            raise RuntimeError(
                "cannot resolve non-ANSI top-level port declarations: "
                f"missing={missing}, extra={extra}"
            )
        actual = [
            (declarations[name][0], name, declarations[name][1], declarations[name][2])
            for name in non_ansi_names
        ]
    else:
        actual = []
        direction: str | None = None
        signed = False
        width = 1
        for segment in segments:
            direction, width, signed, names = parse_declaration(
                segment,
                inherited_direction=direction,
                inherited_width=width,
                inherited_signed=signed,
            )
            if direction is None:
                raise RuntimeError(
                    "generated RTL top module mixes ANSI and non-ANSI port syntax"
                )
            if len(names) != 1:
                raise RuntimeError(f"cannot parse top-level port declaration: {segment}")
            actual.append((direction, names[0], width, signed))
    expected = [
        (port.direction, port.name, port.width, port.signed) for port in expected_ports
    ]
    if actual != expected:
        raise RuntimeError(
            "generated RTL top-level interface drifted from the frozen contract: "
            f"expected={expected}, actual={actual}"
        )


def _build_contract_testbench(
    inputs: list[tuple[str, int]],
    outputs: list[tuple[str, int]],
    stimuli: list[dict[str, int]],
    clock_port: str | None,
    resets: list[tuple[str, int, bool]],
) -> str:
    lines = ["`timescale 1ns/1ps", "module stim_tb();"]
    for name, width in inputs:
        bits = f"[{width - 1}:0] " if width > 1 else ""
        lines.append(f"  reg {bits}{name};")
    for name, width in outputs:
        bits = f"[{width - 1}:0] " if width > 1 else ""
        lines.append(f"  wire {bits}{name};")
    connections = ", ".join(f".{name}({name})" for name, _ in inputs + outputs)
    lines.append(f"  TopModule dut ({connections});")
    lines.append(f"  initial {clock_port} = 0;")
    lines.append(f"  always #5 {clock_port} = ~{clock_port};")
    lines.append("  initial begin")
    for name, _ in inputs:
        if name != clock_port:
            lines.append(f"    {name} = 0;")
    for name, _, active_low in resets:
        lines.append(f"    {name} = {0 if active_low else 1};")
    if resets:
        for _ in range(3):
            lines.append(f"    @(posedge {clock_port}); #1;")
        for name, _, active_low in resets:
            lines.append(f"    {name} = {1 if active_low else 0};")
        lines.append(f"    @(posedge {clock_port}); #1;")
    for index, stimulus in enumerate(stimuli):
        assignments = " ".join(f"{name} = 'h{value:x};" for name, value in stimulus.items())
        lines.append(f"    {assignments}")
        lines.append(f"    @(posedge {clock_port}); #1;")
        formats = " ".join(f"{name}=%b" for name, _ in outputs)
        arguments = ", ".join(name for name, _ in outputs)
        lines.append(f'    $display("TEST_{index} {formats}", {arguments});')
    lines.extend(
        [
            "    $finish;",
            "  end",
            '  initial begin #100000 $display("TIMEOUT"); $finish; end',
            "endmodule",
        ]
    )
    return "\n".join(lines)


def _build_combinational_testbench(
    inputs: list[tuple[str, int]],
    outputs: list[tuple[str, int]],
    stimuli: list[dict[str, int]],
) -> str:
    """Build a no-clock differential harness for stateless RTL."""
    lines = ["`timescale 1ns/1ps", "module stim_tb();"]
    for name, width in inputs:
        bits = f"[{width - 1}:0] " if width > 1 else ""
        lines.append(f"  reg {bits}{name};")
    for name, width in outputs:
        bits = f"[{width - 1}:0] " if width > 1 else ""
        lines.append(f"  wire {bits}{name};")
    connections = ", ".join(f".{name}({name})" for name, _ in inputs + outputs)
    lines.append(f"  TopModule dut ({connections});")
    lines.append("  initial begin")
    for index, stimulus in enumerate(stimuli):
        for name, value in stimulus.items():
            lines.append(f"    {name} = 'h{value:x};")
        lines.append("    #1;")
        formats = " ".join(f"{name}=%b" for name, _ in outputs)
        arguments = ", ".join(name for name, _ in outputs)
        lines.append(f'    $display("TEST_{index} {formats}", {arguments});')
    lines.extend(["    $finish;", "  end", "endmodule"])
    return "\n".join(lines)


def _run(command: list[str], timeout: int) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        command,
        capture_output=True,
        text=True,
        timeout=timeout,
        errors="replace",
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
        except subprocess.TimeoutExpired as exc:
            raise ContainerInfrastructureError(
                f"Docker timed out while running {purpose}", provider=purpose
            ) from exc
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
        _force_remove_container(name)


def simulate_verilog(
    dut_sv,
    inputs,
    outputs,
    stimuli,
    has_clk,
    resets,
    timeout=30,
    clock_port="clk",
):
    if not has_clk:
        return None, "sv_contract_has_no_clock"
    tb = _build_contract_testbench(inputs, outputs, stimuli, clock_port, resets)
    with tempfile.TemporaryDirectory(prefix="rtl-closure-sv-") as temp:
        work = Path(temp)
        (work / "dut.sv").write_text(dut_sv, encoding="utf-8")
        (work / "tb.sv").write_text(tb, encoding="utf-8")
        compile_command = _docker_base(work, read_only_mount=False) + [
            "--entrypoint",
            "iverilog",
            sandbox_image_ref(),
            "-g2012",
            "-o",
            "sim.vvp",
            "-s",
            "stim_tb",
            "dut.sv",
            "tb.sv",
        ]
        compiled = _run_container(compile_command, timeout, purpose="sv-compile")
        if compiled.returncode != 0:
            detail = (compiled.stdout + "\n" + compiled.stderr).strip()[:2000]
            return None, f"sv_compile_fail: {detail}"
        run_command = _docker_base(work, read_only_mount=False) + [
            "--entrypoint",
            "vvp",
            sandbox_image_ref(),
            "-n",
            "sim.vvp",
        ]
        executed = _run_container(run_command, timeout, purpose="sv-sim")
        if executed.returncode != 0:
            return None, f"sv_sim_fail: {executed.stderr.strip()[:1500]}"
        if "TIMEOUT" in executed.stdout:
            return None, "sv_sim_hang"
        widths = dict(outputs)
        results: list[dict[str, str]] = [{} for _ in stimuli]
        for line in executed.stdout.splitlines():
            match = re.match(r"TEST_(\d+)\s+(.*)", line.strip())
            if not match:
                continue
            index = int(match.group(1))
            if index >= len(results):
                continue
            for pair in re.finditer(r"(\w+)=([01xXzZ]+)", match.group(2)):
                results[index][pair.group(1)] = pair.group(2)
        for index, row in enumerate(results):
            if set(row) != set(widths):
                return None, f"sv_result_missing_output: sample {index}"
            try:
                results[index] = {
                    name: from_binary(row[name], widths[name]) for name in sorted(widths)
                }
            except ValueError as error:
                return None, f"sv_result_parse_fail: sample {index}: {error}"
        return results, None


ORACLE_NAMESPACE_SETUP = """namespace = {"__name__": "frozen_oracle", "__builtins__": {
    "__build_class__": __build_class__,
    "abs": abs, "all": all, "any": any, "bool": bool, "dict": dict,
    "enumerate": enumerate, "int": int, "len": len, "list": list,
    "max": max, "min": min, "range": range, "reversed": reversed,
    "set": set, "sum": sum, "tuple": tuple, "zip": zip,
}}"""


PYTHON_HARNESS = textwrap.dedent(
    """
    import json
    import sys

    __ORACLE_NAMESPACE_SETUP__
    exec(open(sys.argv[1], encoding="utf-8").read(), namespace)
    if "TopModule" not in namespace:
        print("PY_ERR: no TopModule class", file=sys.stderr)
        raise SystemExit(2)
    dut = namespace["TopModule"]()
    stimuli = json.load(open(sys.argv[2], encoding="utf-8"))
    resets = json.load(open(sys.argv[3], encoding="utf-8"))
    outputs = json.load(open(sys.argv[4], encoding="utf-8"))
    has_clk = bool(int(sys.argv[5]))

    if has_clk and resets:
        for _ in range(3):
            values = {name: (0 if active_low else 1) for name, active_low in resets}
            for key in (stimuli[0] if stimuli else {}):
                values.setdefault(key, 0)
            dut.eval(values)
        values = {name: (1 if active_low else 0) for name, active_low in resets}
        for key in (stimuli[0] if stimuli else {}):
            values.setdefault(key, 0)
        dut.eval(values)

    results = []
    for stimulus in stimuli:
        values = dict(stimulus)
        for name, active_low in resets:
            values.setdefault(name, 1 if active_low else 0)
        row = dut.eval(values)
        if not isinstance(row, dict):
            raise TypeError("eval() must return a dict")
        results.append({name: (None if row.get(name) is None else int(row[name])) for name in outputs})
    print("PY_RESULTS " + json.dumps(results))
    """
).lstrip().replace("__ORACLE_NAMESPACE_SETUP__", ORACLE_NAMESPACE_SETUP)


def simulate_python(
    dut_py,
    inputs,
    outputs,
    stimuli,
    has_clk,
    resets,
    timeout=30,
):
    del inputs
    with tempfile.TemporaryDirectory(prefix="rtl-closure-py-") as temp:
        work = Path(temp)
        (work / "harness.py").write_text(PYTHON_HARNESS, encoding="utf-8")
        (work / "user.py").write_text(dut_py, encoding="utf-8")
        (work / "stims.json").write_text(json.dumps(stimuli), encoding="utf-8")
        (work / "resets.json").write_text(
            json.dumps([[name, active_low] for name, _, active_low in resets]),
            encoding="utf-8",
        )
        (work / "outputs.json").write_text(
            json.dumps([name for name, _ in outputs]), encoding="utf-8"
        )
        command = _docker_base(work, read_only_mount=True) + [
            "--entrypoint",
            "python3",
            sandbox_image_ref(),
            "harness.py",
            "user.py",
            "stims.json",
            "resets.json",
            "outputs.json",
            "1" if has_clk else "0",
        ]
        completed = _run_container(command, timeout, purpose="py-oracle")
        if completed.returncode != 0:
            return None, f"py_exec_fail: {completed.stderr.strip()[:1500]}"
        for line in completed.stdout.splitlines():
            if line.startswith("PY_RESULTS "):
                try:
                    return json.loads(line[len("PY_RESULTS ") :]), None
                except json.JSONDecodeError as exc:
                    return None, f"py_parse_fail: {exc}"
        return None, "py_no_results"


def contract_cross_verify(
    ref_sv: str,
    dut_sv: str,
    dut_py: str,
    *,
    clock_port: str,
    reset: dict | None,
    num_tests: int = 30,
    seed: int = 42,
) -> dict:
    inputs, outputs = parse_contract_ports(ref_sv)
    input_map = dict(inputs)
    if not outputs:
        return {
            "match_rate": 0.0,
            "mismatches": [],
            "sv_error": None,
            "py_error": None,
            "num_tests": 0,
            "has_clk": False,
            "meta_error": "no output ports parsed",
        }
    has_clk = clock_port is not None
    if has_clk and input_map.get(clock_port) != 1:
        return {
            "match_rate": 0.0,
            "mismatches": [],
            "sv_error": None,
            "py_error": None,
            "num_tests": 0,
            "has_clk": False,
            "meta_error": "contract clock is not a one-bit input",
        }
    resets: list[tuple[str, int, bool]] = []
    if reset:
        resets.append((reset["port"], input_map[reset["port"]], reset["active_low"]))
    excluded = {name for name in [clock_port, *(name for name, _, _ in resets)] if name}
    rng = random.Random(seed)
    stimuli = [
        {
            name: rng.randint(0, (1 << width) - 1)
            for name, width in inputs
            if name not in excluded
        }
        for _ in range(num_tests)
    ]
    reference_declarations = _rtl_module_declarations(ref_sv)
    reference_top = (
        reference_declarations[0].group("name") if reference_declarations else None
    )
    canonical_rtl = _normalize_verifier_module_name(
        dut_sv,
        source_name=reference_top,
    )
    sv_out, sv_error = simulate_verilog(
        canonical_rtl,
        inputs,
        outputs,
        stimuli,
        has_clk,
        resets,
        clock_port=clock_port or "clk",
    )
    if sv_error:
        return {
            "match_rate": 0.0,
            "mismatches": [],
            "sv_error": sv_error,
            "py_error": None,
            "num_tests": num_tests,
            "has_clk": has_clk,
            "stimuli": stimuli,
        }
    py_out, py_error = simulate_python(
        dut_py, inputs, outputs, stimuli, has_clk, resets
    )
    if py_error:
        return {
            "match_rate": 0.0,
            "mismatches": [],
            "sv_error": None,
            "py_error": py_error,
            "num_tests": num_tests,
            "has_clk": has_clk,
            "stimuli": stimuli,
            "verilog_trace": sv_out,
        }
    mismatches = []
    total = 0
    mismatched = 0
    compared_bits = 0
    masked_bits = 0
    widths = dict(outputs)
    verilog_trace: list[dict[str, dict[str, int]]] = []
    python_trace: list[dict[str, dict[str, int]]] = []
    for index, (sv_row, py_row) in enumerate(zip(sv_out, py_out)):
        verilog_sample: dict[str, dict[str, int]] = {}
        python_sample: dict[str, dict[str, int]] = {}
        for name, width in outputs:
            total += 1
            sv_value = sv_row.get(name)
            py_value = py_row.get(name)
            try:
                sv_logic = (
                    sv_value
                    if isinstance(sv_value, dict)
                    else from_integer(sv_value, width)
                )
                py_logic = (
                    py_value
                    if isinstance(py_value, dict)
                    else from_integer(py_value, width)
                )
            except ValueError as error:
                return {
                    "transcript_schema_version": 2,
                    "match_rate": 0.0,
                    "mismatches": [],
                    "sv_error": None,
                    "py_error": None,
                    "num_tests": num_tests,
                    "has_clk": has_clk,
                    "stimuli": stimuli,
                    "meta_error": f"logic_value_encoding_error: {error}",
                }
            verilog_sample[name] = sv_logic
            python_sample[name] = py_logic
            compared_bits += int(sv_logic["compare_mask"]).bit_count()
            masked_bits += width - int(sv_logic["compare_mask"]).bit_count()
            if not matches(sv_logic, py_logic):
                mismatched += 1
                mismatches.append(
                    {
                        "test": index,
                        "signal": name,
                        "width": widths[name],
                        "verilog": sv_logic,
                        "python": py_logic,
                        "inputs": stimuli[index],
                    }
                )
        verilog_trace.append(verilog_sample)
        python_trace.append(python_sample)
    rate = 1.0 - (mismatched / total) if total else 0.0
    return {
        "transcript_schema_version": 2,
        "match_rate": rate,
        "mismatches": mismatches,
        "sv_error": None,
        "py_error": None,
        "num_tests": num_tests,
        "total_checks": total,
        "mismatched": mismatched,
        "compared_bits": compared_bits,
        "masked_bits": masked_bits,
        "has_clk": has_clk,
        "stimuli": stimuli,
        "verilog_trace": verilog_trace,
        "python_trace": python_trace,
    }


def _normalize_verifier_module_name(
    rtl: str,
    *,
    source_name: str | None = None,
    target: str = "TopModule",
) -> str:
    """Rename the contract top, never a preceding helper or comment token."""

    declarations = _rtl_module_declarations(rtl)
    if not declarations:
        return rtl
    selected = next(
        (
            declaration
            for declaration in declarations
            if source_name and declaration.group("name") == source_name
        ),
        None,
    )
    if selected is None:
        target_declaration = next(
            (
                declaration
                for declaration in declarations
                if declaration.group("name") == target
            ),
            None,
        )
        if target_declaration is not None:
            return rtl
        common = [
            declaration
            for declaration in declarations
            if declaration.group("name") in {"top_module", "TopModule"}
        ]
        if len(common) == 1:
            selected = common[0]
        elif len(declarations) == 1:
            selected = declarations[0]
        else:
            return rtl
    if selected.group("name") == target:
        return rtl
    return _rename_module_declaration(rtl, selected, target)


def make_contract_verifier(*, clock_port: str, reset: dict | None):
    """Bind immutable interface semantics to the local inference verifier."""

    def verifier(
        ref_sv: str,
        dut_sv: str,
        dut_py: str,
        num_tests: int = 30,
        seed: int = 42,
    ) -> dict:
        return contract_cross_verify(
            ref_sv,
            dut_sv,
            dut_py,
            clock_port=clock_port,
            reset=reset,
            num_tests=num_tests,
            seed=seed,
        )

    return verifier


def yosys_check(rtl: str, top: str) -> str:
    check_rtl_policy(rtl)
    with tempfile.TemporaryDirectory(prefix="rtl-closure-yosys-") as temp:
        work = Path(temp)
        (work / "design.sv").write_text(rtl, encoding="utf-8")
        script = (
            f"read_verilog -sv /work/design.sv; hierarchy -check -top {top}; "
            "proc; write_json /work/rtl-policy.json; opt; check -assert"
        )
        command = _docker_base(work, read_only_mount=False) + [
            "--entrypoint",
            "yosys",
            sandbox_image_ref(),
            "-p",
            script,
        ]
        completed = _run_container(command, 120, purpose="yosys")
        output = (completed.stdout + "\n" + completed.stderr).strip()
        if completed.returncode != 0:
            raise RuntimeError(f"Yosys validation failed: {output[-3000:]}")
        try:
            netlist = json.loads((work / "rtl-policy.json").read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise RuntimeError(f"Yosys policy artifact is invalid: {exc}") from exc
        initialized: list[str] = []
        modules = netlist.get("modules") if isinstance(netlist, dict) else None
        if not isinstance(modules, dict):
            raise RuntimeError("Yosys policy artifact contains no modules")
        for module_name, module in modules.items():
            if not isinstance(module, dict):
                continue
            for section in ("netnames", "memories"):
                objects = module.get(section) or {}
                if not isinstance(objects, dict):
                    continue
                for object_name, metadata in objects.items():
                    attributes = metadata.get("attributes") if isinstance(metadata, dict) else None
                    if isinstance(attributes, dict) and "init" in attributes:
                        initialized.append(f"{module_name}.{object_name}")
            cells = module.get("cells") or {}
            if isinstance(cells, dict):
                for cell_name, metadata in cells.items():
                    cell_type = metadata.get("type") if isinstance(metadata, dict) else None
                    if isinstance(cell_type, str) and cell_type.lstrip("\\").startswith("$meminit"):
                        initialized.append(f"{module_name}.{cell_name}")
        if initialized:
            raise RuntimeError(
                "generated RTL contains forbidden synthesized initialization: "
                + ", ".join(sorted(initialized)[:16])
            )
        return output[-3000:]


def _equivalence_module(netlist: dict, name: str) -> dict:
    modules = netlist.get("modules") if isinstance(netlist, dict) else None
    if not isinstance(modules, dict):
        raise RuntimeError("Yosys equivalence preparation produced no modules")
    for key in (name, "\\" + name):
        value = modules.get(key)
        if isinstance(value, dict):
            return value
    raise RuntimeError(f"Yosys equivalence preparation omitted module {name}")


def _observable_equivalence_metadata(
    gold_netlist: dict, gate_netlist: dict
) -> tuple[list[str], int, list[str]]:
    """Identify observable outputs and shared internal proof-support names."""

    gold = _equivalence_module(gold_netlist, "gold")
    gate = _equivalence_module(gate_netlist, "gate")
    gold_ports = gold.get("ports") or {}
    gate_ports = gate.get("ports") or {}
    if not isinstance(gold_ports, dict) or not isinstance(gate_ports, dict):
        raise RuntimeError("Yosys equivalence preparation produced invalid ports")
    if set(gold_ports) != set(gate_ports):
        raise RuntimeError("generic synthesis changed the top-level port set")
    output_ports: list[str] = []
    output_bits = 0
    for name in sorted(gold_ports):
        gold_port = gold_ports[name]
        gate_port = gate_ports[name]
        if not isinstance(gold_port, dict) or not isinstance(gate_port, dict):
            raise RuntimeError("Yosys equivalence preparation produced invalid port metadata")
        gold_direction = gold_port.get("direction")
        if gate_port.get("direction") != gold_direction:
            raise RuntimeError(f"generic synthesis changed port direction: {name}")
        gold_bits = gold_port.get("bits")
        gate_bits = gate_port.get("bits")
        if not isinstance(gold_bits, list) or not isinstance(gate_bits, list):
            raise RuntimeError("Yosys equivalence preparation produced invalid port bits")
        if len(gold_bits) != len(gate_bits):
            raise RuntimeError(f"generic synthesis changed port width: {name}")
        if gold_direction == "output":
            output_ports.append(name)
            output_bits += len(gold_bits)
    if output_bits <= 0:
        raise RuntimeError("synthesis equivalence requires at least one output bit")

    port_names = set(gold_ports)
    shared_public_names: set[str] = set()
    for section in ("netnames", "cells"):
        gold_names = gold.get(section) or {}
        gate_names = gate.get(section) or {}
        if not isinstance(gold_names, dict) or not isinstance(gate_names, dict):
            raise RuntimeError(
                f"Yosys equivalence preparation produced invalid {section} metadata"
            )
        shared_public_names.update(set(gold_names).intersection(gate_names))
    supporting_internal_names = sorted(
        name
        for name in shared_public_names - port_names
        if not name.startswith("$") and not name.startswith("\\$")
    )
    if any(
        not name or any(character.isspace() for character in name)
        for name in supporting_internal_names
    ):
        raise RuntimeError(
            "observable equivalence cannot safely encode an internal escaped identifier"
        )
    return supporting_internal_names, output_bits, output_ports


def _equivalence_port_evidence(netlist: dict, name: str) -> dict:
    """Extract a fail-closed port/dependency view from Yosys JSON evidence."""

    module = _equivalence_module(netlist, name)
    ports = module.get("ports")
    if not isinstance(ports, dict):
        raise RuntimeError("Yosys equivalence preparation produced invalid ports")
    inputs: dict[str, list[int | str]] = {}
    outputs: dict[str, list[int | str]] = {}
    for port_name, port in sorted(ports.items()):
        if not isinstance(port_name, str) or not isinstance(port, dict):
            raise RuntimeError("Yosys equivalence preparation produced invalid port metadata")
        direction = port.get("direction")
        bits = port.get("bits")
        if direction not in {"input", "output"} or not isinstance(bits, list) or not bits:
            raise RuntimeError("Yosys equivalence preparation produced unsupported ports")
        if any(isinstance(bit, (dict, list, bool)) for bit in bits):
            raise RuntimeError("Yosys equivalence preparation produced invalid port bits")
        target = inputs if direction == "input" else outputs
        target[port_name] = list(bits)
    if not outputs:
        raise RuntimeError("synthesis equivalence requires at least one output port")

    reverse_dependencies: dict[int | str, set[int | str]] = {}
    cells = module.get("cells")
    if not isinstance(cells, dict):
        raise RuntimeError("Yosys equivalence preparation produced invalid cells")
    for cell in cells.values():
        if not isinstance(cell, dict):
            raise RuntimeError("Yosys equivalence preparation produced invalid cell metadata")
        directions = cell.get("port_directions")
        connections = cell.get("connections")
        if not isinstance(directions, dict) or not isinstance(connections, dict):
            raise RuntimeError("Yosys equivalence preparation lacks cell port directions")
        inputs_to_cell: set[int | str] = set()
        outputs_from_cell: set[int | str] = set()
        for port_name, direction in directions.items():
            bits = connections.get(port_name)
            if not isinstance(port_name, str) or not isinstance(bits, list):
                raise RuntimeError("Yosys equivalence preparation has malformed connections")
            valid_bits = {
                bit for bit in bits if not isinstance(bit, (dict, list, bool))
            }
            if direction in {"input", "inout"}:
                inputs_to_cell.update(valid_bits)
            if direction in {"output", "inout"}:
                outputs_from_cell.update(valid_bits)
        for output_bit in outputs_from_cell:
            reverse_dependencies.setdefault(output_bit, set()).update(inputs_to_cell)

    output_bits = {bit for bits in outputs.values() for bit in bits}

    def reaches_input(input_bits: list[int | str]) -> bool:
        frontier = list(output_bits)
        visited: set[int | str] = set()
        sources = set(input_bits)
        while frontier:
            bit = frontier.pop()
            if bit in visited:
                continue
            visited.add(bit)
            if bit in sources:
                return True
            frontier.extend(reverse_dependencies.get(bit, ()))
        return False

    return {
        "inputs": {port: len(bits) for port, bits in inputs.items()},
        "outputs": {port: len(bits) for port, bits in outputs.items()},
        "observable_input_dependency": {
            port: reaches_input(bits) for port, bits in inputs.items()
        },
    }


def _prepare_observable_equivalence(
    work: Path,
    *,
    top: str,
    timeout: int,
) -> tuple[list[str], int, list[str], str, dict]:
    script = " ".join(
        (
            "read_verilog -sv /work/design.sv;",
            f"hierarchy -check -top {top};",
            "proc; memory; flatten; async2sync; opt_clean;",
            f"rename {top} gold;",
            "write_json /work/gold.json;",
            "design -reset;",
            "read_verilog -sv /work/design.sv;",
            f"synth -flatten -top {top}; async2sync; opt_clean;",
            f"rename {top} gate;",
            "write_json /work/gate.json;",
        )
    )
    command = _docker_base(work, read_only_mount=False) + [
        "--entrypoint",
        "yosys",
        sandbox_image_ref(),
        "-p",
        script,
    ]
    completed = _run_container(command, timeout, purpose="synth-equiv-prepare")
    output = (completed.stdout + "\n" + completed.stderr).strip()
    if completed.returncode != 0:
        raise RuntimeError(
            "Yosys synthesis equivalence preparation failed: " + output[-5000:]
        )
    try:
        gold = json.loads((work / "gold.json").read_text(encoding="utf-8"))
        gate = json.loads((work / "gate.json").read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise RuntimeError(
            f"Yosys synthesis equivalence preparation evidence is unreadable: {exc}"
        ) from exc
    supporting_names, output_bits, output_ports = _observable_equivalence_metadata(
        gold, gate
    )
    canonical_gold = json.dumps(gold, ensure_ascii=True, separators=(",", ":"), sort_keys=True)
    canonical_gate = json.dumps(gate, ensure_ascii=True, separators=(",", ":"), sort_keys=True)
    evidence = {
        "schema_version": 1,
        "top": top,
        "preparation_script": script,
        "preparation_script_sha256": hashlib.sha256(script.encode()).hexdigest(),
        "preparation_log": output,
        "preparation_log_sha256": hashlib.sha256(output.encode()).hexdigest(),
        "gold_netlist_sha256": hashlib.sha256(canonical_gold.encode()).hexdigest(),
        "gate_netlist_sha256": hashlib.sha256(canonical_gate.encode()).hexdigest(),
        "interfaces": {
            "gold": _equivalence_port_evidence(gold, "gold"),
            "gate": _equivalence_port_evidence(gate, "gate"),
        },
    }
    return supporting_names, output_bits, output_ports, output, evidence


def _equivalence_inconclusive(
    rtl: str,
    *,
    top: str,
    clock_port: str | None,
    reset: dict | None,
    output_ports: list[str],
    observable_output_bits: int,
    supporting_names: list[str],
    reason_code: str,
    preparation: dict | None,
    reset_evidence: dict | None = None,
) -> dict:
    return {
        "schema_version": 4,
        "status": "inconclusive",
        "verdict": "inconclusive",
        "reason_code": reason_code,
        "provider": "yosys_generic_synthesis_equivalence",
        "scope": "observable_outputs_rtl_vs_generic_synthesized_netlist",
        "top": top,
        "clock_port": clock_port,
        "reset": reset,
        "reset_base_case": {
            "status": "inconclusive",
            "asserted_samples": 0,
            "post_reset_samples": 0,
            "sampling": "rising_clock_edge",
        },
        "sequential_induction_depth": 8,
        "comparison_semantics": {
            "points": "top_level_output_bits_only",
            "observable_output_bits": observable_output_bits,
            "output_ports": output_ports,
            "supporting_internal_match_names": len(supporting_names),
            "internal_state": "supporting_inductive_relation_not_a_release_obligation",
            "reachable_state_base_proven": False,
        },
        "equivalence_cells": None,
        "proven_cells": None,
        "rtl_sha256": hashlib.sha256(rtl.encode("utf-8")).hexdigest(),
        "sandbox_image": sandbox_image_ref(),
        "preparation": preparation,
        "reset_observability": reset_evidence,
        "proof_evidence": None,
    }


def _read_equivalence_counterexample(path: Path) -> tuple[dict[str, Any], str]:
    """Load one bounded Yosys SAT dump without trusting a partial artifact."""

    try:
        raw = path.read_bytes()
    except OSError as exc:
        raise RuntimeError(
            f"Yosys equivalence counterexample artifact is unavailable: {exc}"
        ) from exc
    if not raw or len(raw) > 1024 * 1024:
        raise RuntimeError("Yosys equivalence counterexample exceeds its evidence bound")
    try:
        value = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise RuntimeError(
            f"Yosys equivalence counterexample artifact is invalid JSON: {exc}"
        ) from exc
    if not isinstance(value, dict) or not value:
        raise RuntimeError("Yosys equivalence counterexample is not a non-empty object")
    return value, hashlib.sha256(raw).hexdigest()


def _equivalence_counterexample(
    rtl: str,
    *,
    top: str,
    clock_port: str | None,
    reset: dict | None,
    output_ports: list[str],
    observable_output_bits: int,
    supporting_names: list[str],
    preparation: dict | None,
    reset_evidence: dict | None,
    induction_script: str,
    induction_log: str,
    replay_script: str,
    replay_log: str,
    sat_dump: dict[str, Any],
    sat_dump_sha256: str,
) -> dict:
    """Return only a replayable, observable miter counterexample verdict."""

    report = _equivalence_inconclusive(
        rtl,
        top=top,
        clock_port=clock_port,
        reset=reset,
        output_ports=output_ports,
        observable_output_bits=observable_output_bits,
        supporting_names=supporting_names,
        reason_code="observable_miter_counterexample",
        preparation=preparation,
        reset_evidence=reset_evidence,
    )
    report.update(
        status="counterexample",
        verdict="counterexample",
        scope=(
            "post_reset_observable_outputs_rtl_vs_generic_synthesized_netlist"
            if reset is not None
            else "observable_outputs_rtl_vs_generic_synthesized_netlist"
        ),
        proof_evidence={
            "induction_script": induction_script,
            "induction_script_sha256": hashlib.sha256(
                induction_script.encode()
            ).hexdigest(),
            "induction_log": induction_log,
            "induction_log_sha256": hashlib.sha256(induction_log.encode()).hexdigest(),
            "replay_script": replay_script,
            "replay_script_sha256": hashlib.sha256(replay_script.encode()).hexdigest(),
            "replay_log": replay_log,
            "replay_log_sha256": hashlib.sha256(replay_log.encode()).hexdigest(),
            "reset_schedule_bound_to_miter": reset is not None,
        },
        counterexample={
            "format": "yosys-sat-dump-json-v1",
            "scope": "observable_equiv_miter",
            "replay_status": "miter_observable_mismatch",
            "artifact_sha256": sat_dump_sha256,
            "artifact": sat_dump,
        },
    )
    return report


def run_synthesis_equivalence(
    rtl: str,
    top: str,
    *,
    clock_port: str | None = None,
    reset: dict | None = None,
    sequential_depth: int = 8,
    timeout: int = 180,
) -> tuple[dict, str]:
    """Check the RTL against its generic Yosys synthesis result.

    Both sides originate from independently parsed copies of the source.  The
    gold side is lowered only far enough for equivalence checking; the gate
    side runs the complete generic ``synth`` pass.  Reset-bearing designs must
    first pass a bounded reset base case before weak temporal induction is
    accepted. Only top-level output bits are observable comparison points;
    synthesized internal state may be legally re-encoded. This is deliberately
    synthesis equivalence rather than sign-off LEC: it does not compare a
    PDK-mapped post-layout netlist.
    """

    if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", top):
        raise ValueError("top must be a Verilog identifier")
    if clock_port is not None and not re.fullmatch(
        r"[A-Za-z_][A-Za-z0-9_]*", clock_port
    ):
        raise ValueError("clock_port must be a Verilog identifier")
    if isinstance(sequential_depth, bool) or not 1 <= sequential_depth <= 64:
        raise ValueError("sequential_depth must be from 1 to 64")

    normalized_reset = None
    if reset is not None:
        if not isinstance(reset, dict) or set(reset) != {
            "port",
            "active_low",
            "asynchronous",
        }:
            raise ValueError(
                "reset must contain port, active_low, and asynchronous"
            )
        reset_port = reset["port"]
        if not isinstance(reset_port, str) or not re.fullmatch(
            r"[A-Za-z_][A-Za-z0-9_]*", reset_port
        ):
            raise ValueError("reset.port must be a Verilog identifier")
        if not isinstance(reset["active_low"], bool) or not isinstance(
            reset["asynchronous"], bool
        ):
            raise ValueError("reset polarity and timing fields must be boolean")
        if clock_port is None:
            raise ValueError("clock_port is required when reset is supplied")
        if reset_port == clock_port:
            raise ValueError("reset.port must differ from clock_port")
        normalized_reset = {
            "port": reset_port,
            "active_low": reset["active_low"],
            "asynchronous": reset["asynchronous"],
        }

    base_steps = sequential_depth + 2
    reset_base_script: tuple[str, ...] = ()
    reset_schedule = ""

    with tempfile.TemporaryDirectory(prefix="rtl-closure-equiv-") as temp:
        work = Path(temp)
        (work / "design.sv").write_text(rtl, encoding="utf-8")
        prepared = _prepare_observable_equivalence(work, top=top, timeout=timeout)
        if len(prepared) == 4:
            supporting_names, observable_output_bits, output_ports, preparation_log = prepared
            preparation_evidence = None
        else:
            (
                supporting_names,
                observable_output_bits,
                output_ports,
                preparation_log,
                preparation_evidence,
            ) = prepared

        reset_evidence: dict[str, Any] | None = None
        if normalized_reset is not None:
            if not isinstance(preparation_evidence, dict):
                return _equivalence_inconclusive(
                    rtl,
                    top=top,
                    clock_port=clock_port,
                    reset=normalized_reset,
                    output_ports=output_ports,
                    observable_output_bits=observable_output_bits,
                    supporting_names=supporting_names,
                    reason_code="reset_preparation_evidence_missing",
                    preparation=preparation_evidence,
                ), preparation_log
            interfaces = preparation_evidence.get("interfaces")
            gold = interfaces.get("gold") if isinstance(interfaces, dict) else None
            gate = interfaces.get("gate") if isinstance(interfaces, dict) else None
            if not isinstance(gold, dict) or not isinstance(gate, dict):
                return _equivalence_inconclusive(
                    rtl,
                    top=top,
                    clock_port=clock_port,
                    reset=normalized_reset,
                    output_ports=output_ports,
                    observable_output_bits=observable_output_bits,
                    supporting_names=supporting_names,
                    reason_code="reset_interface_evidence_invalid",
                    preparation=preparation_evidence,
                ), preparation_log
            gold_inputs = gold.get("inputs")
            gate_inputs = gate.get("inputs")
            gold_dependencies = gold.get("observable_input_dependency")
            gate_dependencies = gate.get("observable_input_dependency")
            reset_port = normalized_reset["port"]
            clock_name = clock_port
            if (
                not isinstance(gold_inputs, dict)
                or not isinstance(gate_inputs, dict)
                or not isinstance(gold_dependencies, dict)
                or not isinstance(gate_dependencies, dict)
                or reset_port not in gold_inputs
                or reset_port not in gate_inputs
                or (clock_name is not None and (clock_name not in gold_inputs or clock_name not in gate_inputs))
            ):
                return _equivalence_inconclusive(
                    rtl,
                    top=top,
                    clock_port=clock_port,
                    reset=normalized_reset,
                    output_ports=output_ports,
                    observable_output_bits=observable_output_bits,
                    supporting_names=supporting_names,
                    reason_code="reset_or_clock_port_missing_from_miter",
                    preparation=preparation_evidence,
                ), preparation_log
            reset_evidence = {
                "port": reset_port,
                "gold_present": True,
                "gate_present": True,
                "gold_observable_dependency": gold_dependencies.get(reset_port),
                "gate_observable_dependency": gate_dependencies.get(reset_port),
            }
            if reset_evidence["gold_observable_dependency"] is not True or reset_evidence["gate_observable_dependency"] is not True:
                return _equivalence_inconclusive(
                    rtl,
                    top=top,
                    clock_port=clock_port,
                    reset=normalized_reset,
                    output_ports=output_ports,
                    observable_output_bits=observable_output_bits,
                    supporting_names=supporting_names,
                    reason_code="reset_observable_dependency_unproven",
                    preparation=preparation_evidence,
                    reset_evidence=reset_evidence,
                ), preparation_log
            asserted = 0 if normalized_reset["active_low"] else 1
            deasserted = 1 - asserted
            reset_schedule = " ".join(
                f"-set-at {step} {reset_port} "
                f"{asserted if step <= 2 else deasserted}"
                for step in range(1, base_steps + 1)
            )
            reset_base_script = (
                "design -save equiv_induction_design;",
                "equiv_miter -assert equiv_reset_base @observable_equiv;",
                "prep -top equiv_reset_base;",
                (
                    f"sat -verify -seq {base_steps} -set-init-def -set-def-inputs "
                    f"{reset_schedule} -prove-skip 2 -prove-asserts;"
                ),
                "design -load equiv_induction_design;",
                "hierarchy -check -top equiv;",
                (
                    "select -set observable_equiv equiv/o:* %ci1 "
                    "equiv/t:$equiv %i;"
                ),
            )
        script_parts = (
            "read_verilog -sv /work/design.sv;",
            f"hierarchy -check -top {top};",
            "proc; memory; flatten; async2sync; opt_clean;",
            f"rename {top} gold;",
            "design -stash gold_design;",
            "design -reset;",
            "read_verilog -sv /work/design.sv;",
            (
                "synth -flatten -encfile /tmp/rtl-closure-fsm.enc "
                f"-top {top}; async2sync; opt_clean;"
            ),
            f"rename {top} gate;",
            "design -stash gate_design;",
            "design -reset;",
            "design -copy-from gold_design -as gold gold;",
            "design -copy-from gate_design -as gate gate;",
            (
                "equiv_make -encfile /tmp/rtl-closure-fsm.enc "
                "gold gate equiv;"
            ),
            "hierarchy -check -top equiv;",
            (
                "select -set observable_equiv equiv/o:* %ci1 "
                "equiv/t:$equiv %i;"
            ),
            f"select -assert-count {observable_output_bits} @observable_equiv;",
            "equiv_simple;",
            *reset_base_script,
            f"equiv_induct -seq {sequential_depth};",
            (
                "select -set observable_equiv equiv/o:* %ci1 "
                "equiv/t:$equiv %i;"
            ),
            "equiv_status -assert @observable_equiv;",
        )
        script = " ".join(script_parts)
        command = _docker_base(work, read_only_mount=True) + [
            "--entrypoint",
            "yosys",
            sandbox_image_ref(),
            "-p",
            script,
        ]
        completed = _run_container(command, timeout, purpose="synth-equiv")
        output = (completed.stdout + "\n" + completed.stderr).strip()
        if completed.returncode != 0:
            # ``equiv_induct`` failing is not itself a behavioral mismatch. Run
            # a separate bounded observable-miter SAT query and require its
            # captured model before making the stronger counterexample claim.
            try:
                prefix_end = script_parts.index("equiv_simple;") + 1
            except ValueError as exc:
                raise RuntimeError("equivalence proof script lost its miter setup") from exc
            counterexample_path = work / "equivalence-cex.json"
            replay_steps = base_steps if normalized_reset is not None else sequential_depth
            replay_schedule = (" " + reset_schedule) if reset_schedule else ""
            replay_script = " ".join(
                (
                    *script_parts[:prefix_end],
                    "equiv_miter -assert equiv_counterexample @observable_equiv;",
                    "prep -top equiv_counterexample;",
                    (
                        f"sat -seq {replay_steps} -set-init-def -set-def-inputs"
                        f"{replay_schedule} -max 1 "
                        "-dump_json /work/equivalence-cex.json -prove-asserts;"
                    ),
                )
            )
            replay_command = _docker_base(work, read_only_mount=False) + [
                "--entrypoint",
                "yosys",
                sandbox_image_ref(),
                "-p",
                replay_script,
            ]
            replay_completed = _run_container(
                replay_command, timeout, purpose="synth-equiv-cex"
            )
            replay_output = (
                replay_completed.stdout + "\n" + replay_completed.stderr
            ).strip()
            try:
                sat_dump, sat_dump_sha256 = _read_equivalence_counterexample(
                    counterexample_path
                )
            except RuntimeError:
                sat_dump = None
                sat_dump_sha256 = None
            if replay_completed.returncode != 0 and sat_dump is not None:
                return (
                    _equivalence_counterexample(
                        rtl,
                        top=top,
                        clock_port=clock_port,
                        reset=normalized_reset,
                        output_ports=output_ports,
                        observable_output_bits=observable_output_bits,
                        supporting_names=supporting_names,
                        preparation=preparation_evidence,
                        reset_evidence=reset_evidence,
                        induction_script=script,
                        induction_log=output,
                        replay_script=replay_script,
                        replay_log=replay_output,
                        sat_dump=sat_dump,
                        sat_dump_sha256=str(sat_dump_sha256),
                    ),
                    preparation_log + "\n" + output + "\n" + replay_output,
                )
            report = _equivalence_inconclusive(
                rtl,
                top=top,
                clock_port=clock_port,
                reset=normalized_reset,
                output_ports=output_ports,
                observable_output_bits=observable_output_bits,
                supporting_names=supporting_names,
                reason_code="equivalence_proof_failed_without_replayable_counterexample",
                preparation=preparation_evidence,
                reset_evidence=reset_evidence,
            )
            report["proof_evidence"] = {
                "induction_script": script,
                "induction_script_sha256": hashlib.sha256(script.encode()).hexdigest(),
                "induction_log": output,
                "induction_log_sha256": hashlib.sha256(output.encode()).hexdigest(),
                "replay_script": replay_script,
                "replay_script_sha256": hashlib.sha256(replay_script.encode()).hexdigest(),
                "replay_log": replay_output,
                "replay_log_sha256": hashlib.sha256(replay_output.encode()).hexdigest(),
                "reset_schedule_bound_to_miter": normalized_reset is not None,
            }
            return report, preparation_log + "\n" + output + "\n" + replay_output

    found = re.findall(r"Found\s+(\d+)\s+\$equiv cells", output)
    proven = re.findall(r"Number of proven \$equiv cells:\s+(\d+)", output)
    equivalence_cells = int(found[-1]) if found else None
    proven_cells = (
        int(proven[-1])
        if proven
        else equivalence_cells
        if equivalence_cells is not None
        and "Equivalence successfully proven!" in output
        else None
    )
    if (
        equivalence_cells is None
        or equivalence_cells <= 0
        or proven_cells != equivalence_cells
        or equivalence_cells != observable_output_bits
    ):
        raise RuntimeError(
            "Yosys synthesis equivalence returned incomplete proof evidence"
        )
    base_status = "not_requested"
    if normalized_reset is not None:
        if not re.search(
            r"SAT proof finished\s*-\s*no model found:\s*SUCCESS", output, re.I
        ):
            raise RuntimeError(
                "Yosys synthesis equivalence reset base proof is missing"
            )
        base_status = "pass"
    report = {
        "schema_version": 4,
        "status": "pass",
        "verdict": "proved",
        "reason_code": None,
        "provider": "yosys_generic_synthesis_equivalence",
        "scope": (
            "post_reset_observable_outputs_rtl_vs_generic_synthesized_netlist"
            if normalized_reset is not None
            else "observable_outputs_rtl_vs_generic_synthesized_netlist"
        ),
        "top": top,
        "clock_port": clock_port,
        "reset": normalized_reset,
        "reset_base_case": {
            "status": base_status,
            "asserted_samples": 2 if normalized_reset is not None else 0,
            "post_reset_samples": (
                sequential_depth if normalized_reset is not None else 0
            ),
            "sampling": "rising_clock_edge",
        },
        "sequential_induction_depth": sequential_depth,
        "comparison_semantics": {
            "points": "top_level_output_bits_only",
            "observable_output_bits": observable_output_bits,
            "output_ports": output_ports,
            "supporting_internal_match_names": len(supporting_names),
            "internal_state": (
                "supporting_inductive_relation_not_a_release_obligation"
            ),
            "reachable_state_base_proven": normalized_reset is not None,
        },
        "equivalence_cells": equivalence_cells,
        "proven_cells": proven_cells,
        "rtl_sha256": hashlib.sha256(rtl.encode("utf-8")).hexdigest(),
        "sandbox_image": sandbox_image_ref(),
        "preparation": preparation_evidence,
        "reset_observability": reset_evidence,
        "proof_evidence": {
            "script": script,
            "script_sha256": hashlib.sha256(script.encode()).hexdigest(),
            "log": output,
            "log_sha256": hashlib.sha256(output.encode()).hexdigest(),
            "reset_schedule_bound_to_miter": normalized_reset is not None,
        },
    }
    return report, preparation_log + "\n" + output


_ARCHITECTURE_OPERATOR_CELLS = {
    "multiply": {"$mul"},
    "divide": {"$div", "$divfloor"},
    "modulo": {"$mod", "$modfloor"},
}


def _module_matches(module_key: str, requested: str) -> bool:
    plain = module_key.lstrip("\\")
    if plain == requested:
        return True
    if not module_key.startswith("$paramod"):
        return False
    return bool(
        re.search(r"\\" + re.escape(requested) + r"(?:\\|$)", module_key)
        or re.search(r"\\" + re.escape(requested) + r"$", module_key)
    )


def _find_module(modules: dict, requested: str) -> str | None:
    for module_key in modules:
        if _module_matches(module_key, requested):
            return module_key
    return None


def _evaluate_architecture_netlists(
    hierarchy: dict,
    synthesized: dict,
    *,
    top: str,
    constraints: dict,
    rtl_sha256: str,
) -> dict:
    hierarchy_modules = hierarchy.get("modules") or {}
    synth_modules = synthesized.get("modules") or {}
    top_key = _find_module(hierarchy_modules, top)
    synth_top_key = _find_module(synth_modules, top)
    if top_key is None or synth_top_key is None:
        raise RuntimeError("Yosys architecture audit did not preserve the requested top")

    reachable: set[str] = set()
    raw_instance_counts: dict[str, int] = {}
    operator_counts = {
        operator: 0 for operator in _ARCHITECTURE_OPERATOR_CELLS
    }

    def visit(module_key: str, multiplicity: int, stack: tuple[str, ...]) -> None:
        if module_key in stack:
            raise RuntimeError("recursive module hierarchy is not supported")
        reachable.add(module_key)
        cells = (hierarchy_modules[module_key] or {}).get("cells") or {}
        for cell in cells.values():
            cell_type = str((cell or {}).get("type", ""))
            child_key = cell_type if cell_type in hierarchy_modules else None
            if child_key is not None:
                raw_instance_counts[child_key] = (
                    raw_instance_counts.get(child_key, 0) + multiplicity
                )
                visit(child_key, multiplicity, stack + (module_key,))
                continue
            for operator, types in _ARCHITECTURE_OPERATOR_CELLS.items():
                if cell_type in types:
                    operator_counts[operator] += multiplicity

    visit(top_key, 1, ())

    synth_cells = (synth_modules[synth_top_key] or {}).get("cells") or {}
    sequential_bits = 0
    for cell in synth_cells.values():
        cell_type = str((cell or {}).get("type", "")).upper()
        if "DFF" not in cell_type and "LATCH" not in cell_type:
            continue
        connections = (cell or {}).get("connections") or {}
        q_bits = connections.get("Q") or connections.get("Y") or []
        sequential_bits += len(q_bits) if isinstance(q_bits, list) else 1

    checks: list[dict] = []
    for required in constraints.get("required_modules", []):
        matched_keys = [
            module_key
            for module_key in reachable
            if _module_matches(module_key, required)
        ]
        instances = sum(
            count
            for module_key, count in raw_instance_counts.items()
            if _module_matches(module_key, required)
        )
        if required == top:
            instances = max(instances, 1)
        checks.append(
            {
                "id": f"required_module:{required}",
                "kind": "required_reachable_module",
                "status": "pass" if matched_keys and instances >= 1 else "fail",
                "expected": {"reachable": True, "minimum_instances": 1},
                "actual": {
                    "reachable": bool(matched_keys),
                    "instances": instances,
                    "module_keys": sorted(matched_keys),
                },
            }
        )
    for operator in constraints.get("forbidden_operators", []):
        count = operator_counts[operator]
        checks.append(
            {
                "id": f"forbidden_operator:{operator}",
                "kind": "forbidden_operator",
                "status": "pass" if count == 0 else "fail",
                "expected": 0,
                "actual": count,
            }
        )
    for field, kind, actual, predicate in (
        (
            "min_sequential_bits",
            "minimum_sequential_bits",
            sequential_bits,
            lambda observed, limit: observed >= limit,
        ),
        (
            "max_sequential_bits",
            "maximum_sequential_bits",
            sequential_bits,
            lambda observed, limit: observed <= limit,
        ),
        (
            "max_synth_cells",
            "maximum_generic_synth_cells",
            len(synth_cells),
            lambda observed, limit: observed <= limit,
        ),
    ):
        if field not in constraints:
            continue
        limit = constraints[field]
        checks.append(
            {
                "id": field,
                "kind": kind,
                "status": "pass" if predicate(actual, limit) else "fail",
                "expected": limit,
                "actual": actual,
            }
        )

    canonical_constraints = json.dumps(
        constraints,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    return {
        "schema_version": 1,
        "status": "pass" if all(row["status"] == "pass" for row in checks) else "fail",
        "provider": "yosys_structural_architecture_audit",
        "top": top,
        "rtl_sha256": rtl_sha256,
        "constraints_sha256": hashlib.sha256(
            canonical_constraints.encode("utf-8")
        ).hexdigest(),
        "constraints": constraints,
        "observed": {
            "reachable_module_keys": sorted(reachable),
            "module_instance_counts": {
                key: raw_instance_counts[key] for key in sorted(raw_instance_counts)
            },
            "operator_cells": operator_counts,
            "generic_synth_cells": len(synth_cells),
            "sequential_bits": sequential_bits,
        },
        "checks": checks,
    }


def run_architecture_audit(
    rtl: str,
    top: str,
    constraints: dict,
    *,
    timeout: int = 180,
) -> dict:
    """Evaluate explicit structural constraints against Yosys netlists."""

    rtl_sha256 = hashlib.sha256(rtl.encode("utf-8")).hexdigest()
    canonical_constraints = json.dumps(
        constraints,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    constraints_sha256 = hashlib.sha256(
        canonical_constraints.encode("utf-8")
    ).hexdigest()
    if not constraints:
        return {
            "schema_version": 1,
            "status": "not_requested",
            "provider": "yosys_structural_architecture_audit",
            "top": top,
            "rtl_sha256": rtl_sha256,
            "constraints_sha256": constraints_sha256,
            "constraints": {},
            "observed": None,
            "checks": [],
            "sandbox_image": sandbox_image_ref(),
        }
    if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", top):
        raise ValueError("top must be a Verilog identifier")

    with tempfile.TemporaryDirectory(prefix="rtl-closure-architecture-") as temp:
        work = Path(temp)
        (work / "design.sv").write_text(rtl, encoding="utf-8")
        script = " ".join(
            (
                "read_verilog -sv /work/design.sv;",
                f"hierarchy -check -top {top};",
                "proc; opt_clean;",
                "write_json /work/hierarchy.json;",
                "design -reset;",
                "read_verilog -sv /work/design.sv;",
                f"synth -flatten -top {top};",
                "write_json /work/synthesized.json;",
            )
        )
        command = _docker_base(work, read_only_mount=False) + [
            "--entrypoint",
            "yosys",
            sandbox_image_ref(),
            "-p",
            script,
        ]
        completed = _run_container(command, timeout, purpose="architecture")
        output = (completed.stdout + "\n" + completed.stderr).strip()
        if completed.returncode != 0:
            raise RuntimeError("Yosys architecture audit failed: " + output[-5000:])
        try:
            hierarchy = json.loads((work / "hierarchy.json").read_text(encoding="utf-8"))
            synthesized = json.loads(
                (work / "synthesized.json").read_text(encoding="utf-8")
            )
        except (OSError, json.JSONDecodeError) as exc:
            raise RuntimeError(
                f"Yosys architecture audit produced unreadable netlists: {exc}"
            ) from exc

    report = _evaluate_architecture_netlists(
        hierarchy,
        synthesized,
        top=top,
        constraints=constraints,
        rtl_sha256=rtl_sha256,
    )
    report["sandbox_image"] = sandbox_image_ref()
    report["yosys_log_tail"] = output[-3000:]
    return report
