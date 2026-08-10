"""Small fail-closed helpers for filesystem-backed controller evidence."""

from __future__ import annotations

import hashlib
import os
import stat
from pathlib import Path


def file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def canonical_directory(path: Path, *, label: str) -> Path:
    """Return an existing directory only when no path component is a symlink."""
    candidate = path.expanduser()
    if not candidate.is_absolute():
        candidate = candidate.absolute()
    try:
        resolved = candidate.resolve(strict=True)
        mode = os.lstat(candidate).st_mode
    except (OSError, RuntimeError, ValueError) as exc:
        raise ValueError(f"{label} is unavailable: {exc}") from exc
    if candidate != resolved or not stat.S_ISDIR(mode):
        raise ValueError(f"{label} must be a canonical non-symlink directory")
    return resolved


def canonical_regular_file(
    path: Path,
    *,
    root: Path,
    label: str,
    minimum_size: int = 0,
    maximum_size: int | None = None,
) -> Path:
    """Validate a regular file and every parent component below ``root``."""
    trusted_root = canonical_directory(root, label=f"{label} root")
    candidate = path.expanduser()
    if not candidate.is_absolute():
        candidate = candidate.absolute()
    try:
        resolved = candidate.resolve(strict=True)
        resolved.relative_to(trusted_root)
        mode = os.lstat(candidate).st_mode
        size = os.lstat(candidate).st_size
    except (OSError, RuntimeError, ValueError) as exc:
        raise ValueError(f"{label} is unavailable or escapes its root: {exc}") from exc
    if candidate != resolved or not stat.S_ISREG(mode):
        raise ValueError(f"{label} must be a canonical non-symlink regular file")
    if size < minimum_size:
        raise ValueError(f"{label} is smaller than the required minimum")
    if maximum_size is not None and size > maximum_size:
        raise ValueError(f"{label} exceeds the size limit")
    return resolved


def direct_child_directory(path: Path, *, root: Path, label: str) -> Path:
    trusted_root = canonical_directory(root, label=f"{label} root")
    resolved = canonical_directory(path, label=label)
    if resolved.parent != trusted_root:
        raise ValueError(f"{label} must be a direct child of its root")
    return resolved
