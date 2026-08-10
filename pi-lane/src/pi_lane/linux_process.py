"""Linux process identity helpers shared by managed workers and leases."""

from __future__ import annotations

from pathlib import Path


def proc_identity(pid: int | None) -> tuple[str, int] | None:
    """Return ``(state, start_ticks)`` from procfs without splitting ``comm``."""

    if isinstance(pid, bool) or not isinstance(pid, int) or pid <= 0:
        return None
    try:
        raw = Path(f"/proc/{pid}/stat").read_text(encoding="utf-8")
        closing = raw.rfind(")")
        if closing <= 0:
            return None
        fields = raw[closing + 2 :].split()
        if len(fields) <= 19 or not fields[0]:
            return None
        start_ticks = int(fields[19])
        if start_ticks <= 0:
            return None
        return fields[0], start_ticks
    except (OSError, UnicodeError, ValueError):
        return None
