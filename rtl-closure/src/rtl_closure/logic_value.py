"""Canonical four-state values for independent RTL verification evidence."""

from __future__ import annotations

from typing import Any


LOGIC_VALUE_FIELDS = frozenset(
    {"width", "value", "known_mask", "z_mask", "compare_mask"}
)


def width_mask(width: int) -> int:
    if isinstance(width, bool) or not isinstance(width, int) or width <= 0:
        raise ValueError("logic value width must be a positive integer")
    return (1 << width) - 1


def from_integer(value: int | None, width: int) -> dict[str, int]:
    """Encode a Python oracle integer, or an explicitly unknown value."""

    mask = width_mask(width)
    if value is None:
        return {
            "width": width,
            "value": 0,
            "known_mask": 0,
            "z_mask": 0,
            "compare_mask": mask,
        }
    if isinstance(value, bool) or not isinstance(value, int) or not 0 <= value <= mask:
        raise ValueError("logic integer does not fit its declared width")
    return {
        "width": width,
        "value": value,
        "known_mask": mask,
        "z_mask": 0,
        "compare_mask": mask,
    }


def from_binary(text: str, width: int) -> dict[str, int]:
    """Encode a simulator `%b` output while preserving X and Z bits."""

    mask = width_mask(width)
    if not isinstance(text, str) or not text or len(text) > width:
        raise ValueError("simulator logic value has an invalid width")
    digits = text.lower().rjust(width, "0")
    if any(character not in "01xz" for character in digits):
        raise ValueError("simulator logic value contains an invalid digit")
    value = 0
    known_mask = 0
    z_mask = 0
    for offset, character in enumerate(reversed(digits)):
        bit = 1 << offset
        if character in "01":
            known_mask |= bit
            if character == "1":
                value |= bit
        elif character == "z":
            z_mask |= bit
    return {
        "width": width,
        "value": value,
        "known_mask": known_mask,
        "z_mask": z_mask,
        "compare_mask": mask,
    }


def validate(value: Any, width: int) -> str | None:
    """Return a precise validation error for a transcript logic value."""

    try:
        mask = width_mask(width)
    except ValueError as error:
        return str(error)
    if not isinstance(value, dict) or set(value) != LOGIC_VALUE_FIELDS:
        return "logic value does not have the required v2 fields"
    if value.get("width") != width:
        return "logic value width does not match its signal contract"
    for field in ("value", "known_mask", "z_mask", "compare_mask"):
        number = value.get(field)
        if isinstance(number, bool) or not isinstance(number, int) or not 0 <= number <= mask:
            return f"logic value {field} is outside its signal width"
    known_mask = value["known_mask"]
    z_mask = value["z_mask"]
    compare_mask = value["compare_mask"]
    if known_mask & z_mask:
        return "logic value known_mask and z_mask overlap"
    if value["value"] & ~known_mask:
        return "logic value has non-canonical X/Z value bits"
    if compare_mask != mask:
        return "logic value compare_mask is not bound by the frozen contract"
    return None


def matches(left: dict[str, int], right: dict[str, int]) -> bool:
    """Compare only contract-selected bits, with X and Z as explicit states."""

    compare_mask = left["compare_mask"]
    if compare_mask != right["compare_mask"]:
        return False
    if ((left["known_mask"] ^ right["known_mask"]) & compare_mask) != 0:
        return False
    if ((left["z_mask"] ^ right["z_mask"]) & compare_mask) != 0:
        return False
    known_bits = left["known_mask"] & compare_mask
    return ((left["value"] ^ right["value"]) & known_bits) == 0


def compared_bit_count(value: dict[str, int]) -> int:
    return int(value["compare_mask"]).bit_count()
