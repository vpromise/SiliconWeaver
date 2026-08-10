"""Structured failure packets, temporal hints, and conservative RTL slicing."""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from typing import Any


IDENTIFIER = re.compile(r"\b[A-Za-z_][A-Za-z0-9_$]*\b")
BLOCK_START = re.compile(r"\b(always(?:_ff|_comb|_latch)?|assign)\b")
KEYWORDS = {
    "always",
    "always_ff",
    "always_comb",
    "always_latch",
    "and",
    "assign",
    "begin",
    "case",
    "casex",
    "casez",
    "default",
    "else",
    "end",
    "endcase",
    "for",
    "forever",
    "function",
    "if",
    "input",
    "integer",
    "logic",
    "module",
    "negedge",
    "or",
    "output",
    "parameter",
    "posedge",
    "reg",
    "signed",
    "wire",
}


@dataclass(frozen=True)
class RTLBlock:
    block_id: str
    kind: str
    start: int
    end: int
    start_line: int
    end_line: int
    text: str
    writes: tuple[str, ...]
    reads: tuple[str, ...]

    def public(self) -> dict[str, Any]:
        return {
            "block_id": self.block_id,
            "kind": self.kind,
            "start_line": self.start_line,
            "end_line": self.end_line,
            "writes": list(self.writes),
            "reads": list(self.reads),
            "text": self.text,
        }


def _mask_noncode(code: str) -> str:
    chars = list(code)
    index = 0
    state = "code"
    quote = ""
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
            # Verilog uses a single quote in sized/base literals (8'hff), so
            # only a double quote starts a string.
            if chars[index] == '"':
                quote = chars[index]
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
        elif state == "string":
            if chars[index] == "\\":
                chars[index] = " "
                if index + 1 < len(chars):
                    chars[index + 1] = " "
                    index += 2
                    continue
            elif chars[index] == quote:
                chars[index] = " "
                state = "code"
            elif chars[index] != "\n":
                chars[index] = " "
        index += 1
    return "".join(chars)


def _semicolon(masked: str, start: int) -> int:
    round_depth = square_depth = brace_depth = 0
    for index in range(start, len(masked)):
        character = masked[index]
        if character == "(":
            round_depth += 1
        elif character == ")":
            round_depth = max(0, round_depth - 1)
        elif character == "[":
            square_depth += 1
        elif character == "]":
            square_depth = max(0, square_depth - 1)
        elif character == "{":
            brace_depth += 1
        elif character == "}":
            brace_depth = max(0, brace_depth - 1)
        elif character == ";" and not (round_depth or square_depth or brace_depth):
            return index + 1
    return len(masked)


def _always_end(masked: str, start: int) -> int:
    tokens = list(re.finditer(r"\bbegin\b|\bend\b|;", masked[start:]))
    first_begin = next((token for token in tokens if token.group(0) == "begin"), None)
    first_semicolon = next((token for token in tokens if token.group(0) == ";"), None)
    case_start = re.search(r"\bcase(?:x|z)?\b", masked[start:])
    if case_start is not None and (
        first_begin is None or case_start.start() < first_begin.start()
    ):
        depth = 0
        for token in re.finditer(
            r"\bcase(?:x|z)?\b|\bendcase\b", masked[start + case_start.start() :]
        ):
            if token.group(0).startswith("case"):
                depth += 1
            else:
                depth -= 1
                if depth == 0:
                    return start + case_start.start() + token.end()
        return len(masked)
    if first_begin is None or (
        first_semicolon is not None and first_semicolon.start() < first_begin.start()
    ):
        end = _semicolon(masked, start)
        else_match = re.match(r"\s*else\b", masked[end:])
        return _always_end(masked, end + else_match.end()) if else_match else end
    depth = 0
    for token in tokens[tokens.index(first_begin) :]:
        if token.group(0) == "begin":
            depth += 1
        elif token.group(0) == "end":
            depth -= 1
            if depth == 0:
                end = start + token.end()
                tail = masked[end:]
                else_match = re.match(r"\s*else\b", tail)
                if else_match:
                    # Conservatively include an else statement attached to the
                    # outer always.  Most generated RTL uses a begin/end body,
                    # so this branch is deliberately bounded.
                    return _always_end(masked, end + else_match.end())
                return end
    return len(masked)


def _matching_open_brace(code: str, close: int) -> int | None:
    depth = 0
    for index in range(close, -1, -1):
        if code[index] == "}":
            depth += 1
        elif code[index] == "{":
            depth -= 1
            if depth == 0:
                return index
    return None


def _assignment_lvalue(
    masked: str, operator_start: int
) -> tuple[int, tuple[str, ...]] | None:
    """Return a statement LHS, rejecting ``<=`` inside an expression.

    SystemVerilog uses the same token for nonblocking assignment and the
    less-than-or-equal operator.  A legal assignment LHS must begin at a
    statement boundary; this keeps a comparison in ``if (a <= b)`` out of the
    repair authorization write-set.
    """

    end = operator_start
    while end > 0 and masked[end - 1].isspace():
        end -= 1
    if end <= 0:
        return None
    if masked[end - 1] == "}":
        start = _matching_open_brace(masked, end - 1)
        if start is None:
            return None
        names: list[str] = []
        for part in masked[start + 1 : end - 1].split(","):
            match = re.match(r"\s*([A-Za-z_][A-Za-z0-9_$]*)", part)
            if match:
                names.append(match.group(1))
        if not names:
            return None
    else:
        match = re.search(
            r"([A-Za-z_][A-Za-z0-9_$]*)"
            r"(?:\s*(?:\[[^\[\]]+\]|\.\s*[A-Za-z_][A-Za-z0-9_$]*))*\s*$",
            masked[:end],
        )
        if match is None:
            return None
        start = match.start(1)
        names = [match.group(1)]
    boundary = masked[:start].rstrip()
    if boundary and not (
        boundary[-1] in ";:)"
        or re.search(r"\b(?:assign|begin|else|end)\s*$", boundary)
    ):
        return None
    return start, tuple(names)


def _read_write_sets(text: str) -> tuple[tuple[str, ...], tuple[str, ...]]:
    masked = _mask_noncode(text)
    operators = re.finditer(r"<=|(?<![!<>=+\-*/%&|^])=(?!=)", masked)
    assignments: list[tuple[int, tuple[str, ...]]] = []
    for operator in operators:
        lvalue = _assignment_lvalue(masked, operator.start())
        if lvalue is not None:
            assignments.append(lvalue)
    writes = {name for _, names in assignments for name in names}
    # Blank only the base identifier on each lvalue.  An indexed lvalue such
    # as mem[idx] therefore records mem as written while conservatively
    # retaining idx as a read dependency.  A signal written and read in the
    # same block (q <= ...; y <= q) also remains in the read set.
    read_source = list(masked)
    for start, names in assignments:
        cursor = start
        for name in names:
            match = re.search(r"\b" + re.escape(name) + r"\b", masked[cursor:])
            if match is None:
                continue
            name_start = cursor + match.start()
            name_end = cursor + match.end()
            read_source[name_start:name_end] = " " * (name_end - name_start)
            cursor = name_end
    identifiers = {
        name for name in IDENTIFIER.findall("".join(read_source)) if name not in KEYWORDS
    }
    reads = identifiers
    return tuple(sorted(writes)), tuple(sorted(reads))


def _control_signature(text: str, kind: str) -> tuple[str, str | None]:
    """Bind a local edit to the original procedural event control."""

    if kind == "assign":
        return kind, None
    masked = _mask_noncode(text).strip()
    header = re.match(r"always(?:_ff|_comb|_latch)?\b", masked)
    if header is None:
        return kind, None
    remainder = masked[header.end() :].lstrip()
    if not remainder.startswith("@"):
        return kind, None
    remainder = remainder[1:].lstrip()
    if remainder.startswith("*"):
        return kind, "*"
    if not remainder.startswith("("):
        return kind, "invalid"
    depth = 0
    for index, character in enumerate(remainder):
        if character == "(":
            depth += 1
        elif character == ")":
            depth -= 1
            if depth == 0:
                return kind, re.sub(r"\s+", "", remainder[1:index])
    return kind, "invalid"


def extract_rtl_blocks(rtl: str) -> list[RTLBlock]:
    masked = _mask_noncode(rtl)
    rows: list[RTLBlock] = []
    occupied_until = -1
    for match in BLOCK_START.finditer(masked):
        if match.start() < occupied_until:
            continue
        kind = match.group(1)
        end = _semicolon(masked, match.end()) if kind == "assign" else _always_end(masked, match.end())
        if end <= match.start():
            continue
        text = rtl[match.start() : end]
        writes, reads = _read_write_sets(text)
        block_id = f"B{len(rows) + 1:03d}"
        rows.append(
            RTLBlock(
                block_id=block_id,
                kind=kind,
                start=match.start(),
                end=end,
                start_line=rtl.count("\n", 0, match.start()) + 1,
                end_line=rtl.count("\n", 0, end) + 1,
                text=text,
                writes=writes,
                reads=reads,
            )
        )
        occupied_until = end
    return rows


def dependency_slice(
    blocks: list[RTLBlock], failing_signal: str | None, *, depth: int = 3
) -> list[RTLBlock]:
    if not blocks:
        return []
    if not failing_signal:
        return []
    frontier = {failing_signal}
    selected: list[RTLBlock] = []
    selected_ids: set[str] = set()
    for _ in range(depth):
        drivers = [block for block in blocks if set(block.writes) & frontier]
        if not drivers:
            break
        next_frontier: set[str] = set()
        for block in drivers:
            if block.block_id not in selected_ids:
                selected.append(block)
                selected_ids.add(block.block_id)
            next_frontier.update(block.reads)
        frontier = next_frontier
    return selected


def _internal_state_delta(
    trace: list[dict[str, Any]], ordinal: int
) -> list[dict[str, Any]]:
    before = next(
        (
            row
            for row in reversed(trace)
            if int(row.get("ordinal", -1)) < ordinal and row.get("internal")
        ),
        {},
    )
    current = next(
        (
            row
            for row in trace
            if int(row.get("ordinal", -1)) == ordinal and row.get("internal")
        ),
        {},
    )
    previous_state = before.get("internal", {})
    current_state = current.get("internal", {})
    return [
        {
            "signal": signal,
            "before": previous_state.get(signal),
            "at_failure": current_state.get(signal),
        }
        for signal in sorted(set(previous_state) | set(current_state))
        if previous_state.get(signal) != current_state.get(signal)
    ]


def _temporal_hint(
    result: dict[str, Any], signal: str | None, scenario_id: str | None
) -> dict[str, Any]:
    if not signal:
        return {"classification": "unknown", "current": 0.0, "lag1": 0.0, "lead1": 0.0}
    rows = [
        row
        for row in result.get("actual_trace", [])
        if signal in row.get("expected", {}) and signal in row.get("actual", {})
        and row.get("phase", "post_rising_edge") == "post_rising_edge"
        and (scenario_id is None or row.get("scenario_id") == scenario_id)
    ]
    if not rows:
        return {"classification": "unknown", "current": 0.0, "lag1": 0.0, "lead1": 0.0}

    def rate(offset: int) -> float:
        matched = total = 0
        for index, row in enumerate(rows):
            expected_index = index + offset
            if not 0 <= expected_index < len(rows):
                continue
            actual = row["actual"].get(signal)
            expected = rows[expected_index]["expected"].get(signal)
            if actual is None:
                continue
            total += 1
            matched += int(actual == expected)
        return matched / total if total else 0.0

    current = rate(0)
    # actual[t] == expected[t-1] means the DUT is one sampled cycle late.
    lag1 = rate(-1)
    lead1 = rate(1)
    classification = "value_mismatch"
    best_shift = max(lag1, lead1)
    if best_shift >= 0.75 and best_shift >= current + 0.2:
        classification = "off_by_one_late" if lag1 >= lead1 else "off_by_one_early"
    return {
        "classification": classification,
        "current": round(current, 6),
        "lag1": round(lag1, 6),
        "lead1": round(lead1, 6),
    }


def _formal_feedback_summary(value: Any) -> dict[str, Any] | None:
    """Keep formal diagnostics useful to repair without embedding full waveforms."""
    if not isinstance(value, dict):
        return None
    summary_keys = (
        "status",
        "provider",
        "authoritative",
        "detail",
        "property_count",
        "depth",
        "suite_sha256",
        "property_map_sha256",
        "engine",
        "verify_image",
        "rtl_sha256",
        "artifact",
    )
    summary = {key: value.get(key) for key in summary_keys if key in value}
    raw_counterexamples = value.get("counterexamples")
    if isinstance(raw_counterexamples, list):
        fields = (
            "property_index",
            "property_id",
            "scenario_index",
            "scenario_id",
            "requirement_ids",
            "cycle",
            "phase",
            "signal",
            "expected",
            "actual",
            "inputs",
            "artifact",
        )
        summary["counterexample_count"] = len(raw_counterexamples)
        summary["counterexamples"] = [
            {key: row.get(key) for key in fields if key in row}
            for row in raw_counterexamples[:32]
            if isinstance(row, dict)
        ]
        summary["counterexamples_truncated"] = len(raw_counterexamples) > 32
    return summary


def _lint_feedback_summary(value: Any) -> dict[str, Any] | None:
    if not isinstance(value, dict):
        return None
    return {
        key: value.get(key)
        for key in (
            "status",
            "provider",
            "authoritative",
            "detail",
            "warning_counts",
            "quality_gate",
            "rtl_sha256",
            "artifact",
        )
        if key in value
    }


def build_failure_packet(
    result: dict[str, Any],
    rtl: str,
    *,
    trace_window_cycles: int = 4,
) -> dict[str, Any]:
    mismatch = (result.get("mismatches") or [None])[0]
    mismatch = mismatch if isinstance(mismatch, dict) else {}
    signal = mismatch.get("signal")
    blocks = extract_rtl_blocks(rtl)
    suspect = dependency_slice(blocks, signal)
    ordinal = mismatch.get("ordinal", result.get("first_failure_ordinal", 0))
    ordinal = int(ordinal or 0)
    trace = [
        row
        for row in result.get("actual_trace", [])
        if abs(int(row.get("ordinal", -1000000)) - ordinal) <= trace_window_cycles
    ]
    temporal = _temporal_hint(result, signal, mismatch.get("scenario_id"))
    failure_class = result.get("failure_class") or mismatch.get("failure_class") or "verification_error"
    if failure_class == "value_mismatch" and temporal["classification"].startswith("off_by_one"):
        failure_class = temporal["classification"]
    requirement_ids = mismatch.get("requirement_ids") or []
    slice_hash = hashlib.sha256(
        "\n".join(block.text for block in suspect).encode("utf-8")
    ).hexdigest()
    fingerprint_payload = {
        "failure_class": failure_class,
        "requirements": requirement_ids,
        "scenario": mismatch.get("scenario_id"),
        "signal": signal,
        "cycle": mismatch.get("cycle"),
        "slice_sha256": slice_hash,
    }
    fingerprint = hashlib.sha256(
        json.dumps(fingerprint_payload, sort_keys=True).encode("utf-8")
    ).hexdigest()
    mismatch_cluster = [
        {
            key: row.get(key)
            for key in (
                "check_id",
                "signal",
                "expected",
                "actual",
                "actual_bits",
                "cycle",
                "phase",
                "requirement_ids",
            )
        }
        for row in (result.get("mismatches") or [])
        if int(row.get("ordinal", -1)) == ordinal
    ][:32]
    return {
        "schema_version": 2,
        "source": result.get("source", "simulation"),
        "failure_class": failure_class,
        "detail": result.get("detail"),
        "fingerprint": fingerprint,
        "scenario_id": mismatch.get("scenario_id"),
        "scenario_index": mismatch.get("scenario_index"),
        "requirement_ids": requirement_ids,
        "first_failure_cycle": mismatch.get("cycle"),
        "first_failure_ordinal": ordinal,
        "signal": signal,
        "expected": mismatch.get("expected"),
        "actual": mismatch.get("actual"),
        "actual_bits": mismatch.get("actual_bits"),
        "inputs": mismatch.get("inputs"),
        "temporal_hint": temporal,
        "trace_window": trace,
        "internal_state_delta": _internal_state_delta(trace, ordinal),
        "mismatch_cluster": mismatch_cluster,
        "coverage": result.get("coverage"),
        "suite_sha256": result.get("suite_sha256"),
        "formal": _formal_feedback_summary(result.get("formal")),
        "verilator_lint": _lint_feedback_summary(result.get("verilator_lint")),
        "suspect_blocks": [block.public() for block in suspect],
        "slice_sha256": slice_hash,
    }


def counterexample_from_packet(packet: dict[str, Any]) -> dict[str, Any]:
    return {
        "fingerprint": packet["fingerprint"],
        "failure_class": packet["failure_class"],
        "scenario_id": packet.get("scenario_id"),
        "requirement_ids": packet.get("requirement_ids", []),
        "first_failure_cycle": packet.get("first_failure_cycle"),
        "signal": packet.get("signal"),
        "expected": packet.get("expected"),
        "actual": packet.get("actual"),
        "inputs": packet.get("inputs"),
        "trace_window": packet.get("trace_window", []),
        "internal_state_delta": packet.get("internal_state_delta", []),
        "mismatch_cluster": packet.get("mismatch_cluster", []),
        "source": packet.get("source", "simulation"),
        "suite_sha256": packet.get("suite_sha256"),
    }


def apply_block_edits(
    rtl: str,
    edits: list[dict[str, Any]],
    authorized_blocks: list[RTLBlock],
) -> str:
    by_id = {block.block_id: block for block in authorized_blocks}
    replacements: list[tuple[RTLBlock, str]] = []
    seen: set[str] = set()
    for edit in edits:
        if not isinstance(edit, dict) or set(edit) != {"block_id", "replacement"}:
            raise ValueError("each repair edit must contain only block_id and replacement")
        block_id = edit["block_id"]
        replacement = edit["replacement"]
        if block_id in seen or block_id not in by_id:
            raise ValueError(f"repair edit is duplicated or unauthorized: {block_id}")
        if not isinstance(replacement, str) or not replacement.strip():
            raise ValueError(f"repair edit {block_id} has an empty replacement")
        original = by_id[block_id]
        stripped = replacement.strip()
        parsed = extract_rtl_blocks(stripped)
        if len(parsed) != 1 or parsed[0].kind != original.kind:
            raise ValueError(
                f"repair edit {block_id} must contain exactly one {original.kind} block"
            )
        if parsed[0].start != 0 or parsed[0].end != len(stripped):
            raise ValueError(
                f"repair edit {block_id} contains code outside the authorized block"
            )
        if not set(parsed[0].writes) <= set(original.writes):
            raise ValueError(
                f"repair edit {block_id} expands write-set from {original.writes} "
                f"to {parsed[0].writes}"
            )
        if _control_signature(parsed[0].text, parsed[0].kind) != _control_signature(
            original.text, original.kind
        ):
            raise ValueError(
                f"repair edit {block_id} changes the authorized event control"
            )
        seen.add(block_id)
        replacements.append((original, stripped))
    if not replacements:
        raise ValueError("repair proposal contains no edits")
    candidate = rtl
    for block, replacement in sorted(replacements, key=lambda item: item[0].start, reverse=True):
        candidate = candidate[: block.start] + replacement + candidate[block.end :]
    return candidate
