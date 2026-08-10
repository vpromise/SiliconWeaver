"""Defensive extraction of generated Python and SystemVerilog source."""

from __future__ import annotations

import ast
import re
from collections.abc import Callable, Iterable


_ANSWER_RE = re.compile(
    r"<answer\b[^>]*>(.*?)(?:</answer\s*>|\Z)", re.IGNORECASE | re.DOTALL
)
_FENCE_OPEN_RE = re.compile(
    r"^[ \t]*(`{3,}|~{3,})[ \t]*([^\r\n]*)\r?\n", re.MULTILINE
)
_MODULE_DECL_RE = re.compile(
    r"^[ \t]*(?:extern[ \t]+)?module(?:[ \t]+(?:automatic|static))?"
    r"[ \t\r\n]+[A-Za-z_$][A-Za-z0-9_$]*\b"
    r"(?=[ \t\r\n]*(?:#\s*\(|\(|;))",
    re.MULTILINE,
)
_ENDMODULE_RE = re.compile(
    r"^[ \t]*endmodule(?:[ \t]*:[ \t]*[A-Za-z_$][A-Za-z0-9_$]*)?[ \t]*$",
    re.MULTILINE,
)


def _fenced_blocks(text: str) -> list[tuple[str, str]]:
    """Return fenced blocks, including a final unterminated model fence."""

    blocks: list[tuple[str, str]] = []
    offset = 0
    while match := _FENCE_OPEN_RE.search(text, offset):
        marker = match.group(1)
        info = match.group(2).strip()
        language = info.split(maxsplit=1)[0].casefold() if info else ""
        close = re.compile(
            rf"^[ \t]*{re.escape(marker[0])}{{{len(marker)},}}[ \t]*$",
            re.MULTILINE,
        ).search(text, match.end())
        end = close.start() if close else len(text)
        blocks.append((language, text[match.end() : end].strip()))
        if close is None:
            break
        offset = close.end()
    return blocks


def _extract_code(
    response: str,
    *,
    languages: Iterable[str],
    validator: Callable[[str], bool],
) -> tuple[str, bool]:
    if not isinstance(response, str) or not response.strip():
        return "", False
    normalized = response.replace("\r\n", "\n").replace("\r", "\n").lstrip("\ufeff")
    answers = [match.group(1) for match in _ANSWER_RE.finditer(normalized)]
    scopes = [*reversed(answers), normalized]
    preferred = {language.casefold() for language in languages}

    for scope in scopes:
        blocks = _fenced_blocks(scope)
        for language, code in reversed(blocks):
            if language in preferred and validator(code):
                return code, True
        for _language, code in reversed(blocks):
            if validator(code):
                return code, True
        raw = scope.strip()
        if validator(raw):
            return raw, False
    return "", False


def _valid_python(code: str) -> bool:
    if not code:
        return False
    try:
        ast.parse(code)
    except (SyntaxError, ValueError, TypeError):
        return False
    return True


def _valid_verilog(code: str) -> bool:
    if not code:
        return False
    masked = _mask_verilog_noncode(code)
    return bool(_MODULE_DECL_RE.search(masked) and _ENDMODULE_RE.search(masked))


def _mask_verilog_noncode(code: str) -> str:
    """Mask comments and strings while preserving source offsets."""

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


def _bare_verilog(scope: str) -> str:
    """Trim prose surrounding a complete, unfenced Verilog response."""

    masked = _mask_verilog_noncode(scope)
    modules = list(_MODULE_DECL_RE.finditer(masked))
    endings = list(_ENDMODULE_RE.finditer(masked))
    if not modules or not endings or endings[-1].end() <= modules[0].start():
        return ""
    candidate = scope[modules[0].start() : endings[-1].end()].strip()
    if len(modules) != len(endings):
        return ""
    return candidate


def extract_python(response: str) -> str:
    """Extract the last syntactically valid Python block from a model response."""

    return _extract_code(
        response,
        languages=("python", "py", "python3"),
        validator=_valid_python,
    )[0]


def extract_verilog(response: str) -> str:
    """Extract the last complete Verilog/SystemVerilog module from a response."""

    extracted, fenced = _extract_code(
        response,
        languages=("verilog", "systemverilog", "sv"),
        validator=_valid_verilog,
    )
    return extracted if fenced else _bare_verilog(extracted)
