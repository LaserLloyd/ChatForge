"""Free space on the volume the model library lives on, and folder sizes.

Adapted from StudioForge src/studioforge/core/diskspace.py (MIT, LaserLloyd).
Queue accounting is reduced to the one number the downloader passes in.

Nothing here raises. A volume that cannot be measured reports zeroes with the
reason attached, because "I could not tell" must never render as "you are about
to run out".
"""

from __future__ import annotations

import logging
import os
import shutil
import stat
import time
from pathlib import Path
from typing import Any, Final

log = logging.getLogger(__name__)

__all__ = [
    "DISK_USAGE_TTL_S",
    "clear_cache",
    "disk_report",
    "folder_size",
    "free_bytes",
]

#: Cache lifetime for one ``shutil.disk_usage`` call; the settings UI polls.
DISK_USAGE_TTL_S: Final = 2.0

#: ``{path: (monotonic stamp, (total, used, free))}``.
_CACHE: dict[str, tuple[float, tuple[int, int, int]]] = {}


def clear_cache() -> None:
    """Drop cached ``disk_usage`` results (tests, and after a download finishes)."""
    _CACHE.clear()


# Adapted from StudioForge src/studioforge/core/diskspace.py (MIT, LaserLloyd).
def _existing_ancestor(path: Path) -> Path:
    """The nearest ancestor of *path* that exists (the models dir may not yet)."""
    probe = path
    while not probe.exists():
        parent = probe.parent
        if parent == probe:
            break
        probe = parent
    return probe


def _drive_label(path: Path) -> str:
    """``C:`` on Windows, the mount point elsewhere."""
    if path.drive:
        return path.drive
    probe = path
    while not os.path.ismount(probe):
        parent = probe.parent
        if parent == probe:
            break
        probe = parent
    return str(probe)


# Adapted from StudioForge src/studioforge/core/diskspace.py (MIT, LaserLloyd).
def _usage(path: Path) -> tuple[int, int, int]:
    """``(total, used, free)`` for the volume holding *path*, cached briefly."""
    key = str(path)
    now = time.monotonic()
    cached = _CACHE.get(key)
    if cached is not None and now - cached[0] < DISK_USAGE_TTL_S:
        return cached[1]
    total, used, free = shutil.disk_usage(path)
    value = (int(total), int(used), int(free))
    _CACHE[key] = (now, value)
    return value


# Adapted from StudioForge src/studioforge/core/diskspace.py (MIT, LaserLloyd).
def disk_report(path: Path | str, queued_remaining_bytes: int = 0) -> dict[str, Any]:
    """Free space where downloads land, before and after the queue drains.

    ``free_after_queue_bytes`` is signed: a negative value says by how much the
    queue overruns the disk.
    """
    queued = max(0, int(queued_remaining_bytes or 0))
    try:
        resolved = Path(path).expanduser().resolve()
    except OSError:  # pragma: no cover - resolve(strict=False) is near-total
        resolved = Path(path)
    base = _existing_ancestor(resolved)
    report: dict[str, Any] = {
        "path": str(resolved),
        "drive": _drive_label(base),
        "total_bytes": 0,
        "free_bytes": 0,
        "queued_bytes": queued,
        "free_after_queue_bytes": 0,
        "error": None,
    }
    try:
        total, _used, free = _usage(base)
    except (OSError, ValueError) as exc:
        report["error"] = f"{type(exc).__name__}: {exc}"
        log.debug("diskspace.unavailable path=%s error=%s", base, exc)
        return report
    report["total_bytes"] = total
    report["free_bytes"] = free
    report["free_after_queue_bytes"] = free - queued
    return report


def free_bytes(path: Path | str) -> int | None:
    """Free bytes on the volume holding *path*, or ``None`` when unknown."""
    report = disk_report(path, 0)
    if report["error"] is not None:
        return None
    return int(report["free_bytes"])


def folder_size(path: Path | str, *, skip_dot_dirs: bool = False) -> int:
    """Total size of regular files under *path* (0 when it does not exist).

    Symlinks and junctions are not followed, so a link to another drive is not
    counted as this folder's usage. ``skip_dot_dirs`` ignores tool caches such
    as ``.cache/huggingface``.
    """
    root = Path(path)
    total = 0
    try:
        if root.is_file():
            return root.stat().st_size
    except OSError:
        return 0
    for dirpath, dirnames, filenames in os.walk(root, followlinks=False):
        if skip_dot_dirs:
            dirnames[:] = [d for d in dirnames if not d.startswith(".")]
        for name in filenames:
            try:
                st = os.lstat(os.path.join(dirpath, name))
            except OSError:
                continue
            if stat.S_ISREG(st.st_mode):
                total += st.st_size
    return total
