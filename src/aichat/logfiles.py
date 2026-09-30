"""Size-bounded log files that stay safe when another process holds the file.

# Adapted from StudioForge src/studioforge/logfiles.py (MIT, LaserLloyd)

On Windows a file another process has open cannot be renamed. The stdlib
``RotatingFileHandler`` shifts ``.1 -> .2 -> ...`` *before* renaming the live file,
so each failed attempt pushed every backup one step out and deleted the oldest.
Here the live file is renamed first, to a timestamped name; a failed rename is not an
error (keep appending, retry after ``retry_s``); backups are pruned oldest-first only
after a new one exists.

Standard library only.
"""

from __future__ import annotations

import logging
import logging.handlers
import os
import re
import time
from pathlib import Path

MB = 1 << 20

DEFAULT_MAX_BYTES = 5 * MB
DEFAULT_BACKUP_COUNT = 3

#: How long a handler waits before retrying a rotation that could not rename the live file.
DEFAULT_RETRY_S = 300.0

_STAMP = "%Y%m%d-%H%M%S"


def backup_pattern(path: Path) -> re.Pattern[str]:
    """Names of ``path``'s rotated copies: ``<stem>.<YYYYmmdd-HHMMSS>[_N]<suffix>``."""
    stem, suffix = re.escape(path.stem), re.escape(path.suffix)
    return re.compile(rf"^{stem}\.(\d{{8}}-\d{{6}})(?:_(\d+))?{suffix}$")


def _backup_keys(path: Path) -> list[tuple[tuple[str, int], Path]]:
    """``((stamp, n), backup)`` for every rotated copy, oldest first."""
    pattern = backup_pattern(path)
    found: list[tuple[tuple[str, int], Path]] = []
    try:
        entries = [entry for entry in os.scandir(path.parent) if entry.is_file()]
    except OSError:
        return []
    for entry in entries:
        match = pattern.match(entry.name)
        if match:
            found.append(((match.group(1), int(match.group(2) or 0)), path.parent / entry.name))
    return sorted(found)


def backups_of(path: Path) -> list[Path]:
    """The rotated copies of ``path``, oldest first. ``[]`` when none can be listed."""
    return [backup for _key, backup in _backup_keys(path)]


def _backup_name(path: Path) -> Path:
    """A new backup name that sorts after every existing one."""
    stamp = time.strftime(_STAMP, time.localtime())
    same_second = [n for (seen, n), _backup in _backup_keys(path) if seen == stamp]
    if not same_second:
        return path.with_name(f"{path.stem}.{stamp}{path.suffix}")
    return path.with_name(f"{path.stem}.{stamp}_{max(same_second) + 1}{path.suffix}")


def prune_backups(path: Path, keep: int) -> list[Path]:
    """Delete all but the newest ``keep`` backups of ``path``; returns what went. Never raises."""
    removed: list[Path] = []
    backups = backups_of(path)
    for old in backups[: max(0, len(backups) - max(1, keep))]:
        try:
            old.unlink()
        except OSError:
            continue
        removed.append(old)
    return removed


def rotate(path: Path, *, backup_count: int) -> Path:
    """Rename ``path`` to a new timestamped backup, then prune; returns the backup.

    Raises :class:`OSError` when the rename fails, having changed nothing.
    """
    dest = _backup_name(path)
    path.rename(dest)  # the only step that can fail on a shared file
    prune_backups(path, backup_count)
    return dest


def rotate_if_large(path: Path, *, max_bytes: int, backup_count: int) -> Path | None:
    """Rotate ``path`` when it has reached ``max_bytes``; for a file nobody holds.

    ``None`` when nothing was done (smaller, missing, ``max_bytes`` 0, or rename failed).
    Used before spawning OVMS, whose console log the child holds open while it runs.
    """
    if max_bytes <= 0:
        return None
    try:
        if path.stat().st_size < max_bytes:
            return None
        return rotate(path, backup_count=backup_count)
    except OSError:
        return None


class SafeRotatingFileHandler(logging.handlers.RotatingFileHandler):
    """A size-rotating handler for the ONE process that owns a log file.

    ``max_bytes <= 0`` never rotates; the handler is then a plain append.
    """

    def __init__(
        self,
        filename: str | os.PathLike[str],
        *,
        max_bytes: int = DEFAULT_MAX_BYTES,
        backup_count: int = DEFAULT_BACKUP_COUNT,
        encoding: str = "utf-8",
        retry_s: float = DEFAULT_RETRY_S,
    ) -> None:
        super().__init__(
            filename,
            mode="a",
            maxBytes=max(0, int(max_bytes)),
            backupCount=max(1, int(backup_count)),
            encoding=encoding,
        )
        self.retry_s = float(retry_s)
        self._retry_at = 0.0
        #: Why the last rotation could not happen, or ``None``.
        self.last_rotation_error: str | None = None
        #: Rotations this handler has performed.
        self.rotations = 0

    def shouldRollover(self, record: logging.LogRecord) -> bool:  # noqa: N802 - stdlib name
        if self.maxBytes <= 0:
            return False
        if self._retry_at and time.monotonic() < self._retry_at:
            return False
        if self.stream is None:
            self.stream = self._open()
        try:
            # The size on disk, not our own write position: other processes may append too.
            return os.fstat(self.stream.fileno()).st_size >= self.maxBytes
        except (OSError, ValueError):
            return False

    def doRollover(self) -> None:  # noqa: N802 - stdlib name
        path = Path(self.baseFilename)
        moved = self._moved_under_us()
        if self.stream:
            self.stream.close()
            self.stream = None  # type: ignore[assignment,unused-ignore]
        if moved:
            # Someone else rotated it already: follow them to the new file.
            self.stream = self._open()
            return
        try:
            backup = rotate(path, backup_count=self.backupCount)
        except OSError as exc:
            first = self.last_rotation_error is None
            self._retry_at = time.monotonic() + self.retry_s
            self.last_rotation_error = f"{type(exc).__name__}: {exc}"
            self.stream = self._open()
            if first:
                self._note(
                    logging.WARNING,
                    f"log rotation deferred ({self.last_rotation_error}): another "
                    f"process holds this file; still appending to it, and retrying "
                    f"every {self.retry_s:.0f} s",
                )
            return
        self._retry_at = 0.0
        self.last_rotation_error = None
        self.rotations += 1
        self.stream = self._open()
        self._note(logging.INFO, f"log rotated; the previous file is {backup.name}")

    def _moved_under_us(self) -> bool:
        """Whether the path no longer names the file our stream writes to."""
        if self.stream is None:
            return False
        try:
            mine = os.fstat(self.stream.fileno())
        except (OSError, ValueError):
            return False
        try:
            current = Path(self.baseFilename).stat()
        except FileNotFoundError:
            return True
        except OSError:
            return False
        return (mine.st_dev, mine.st_ino) != (current.st_dev, current.st_ino)

    def _note(self, level: int, text: str) -> None:
        """One line written straight into the file (not via logging: we hold the handler lock)."""
        if self.stream is None:
            return
        record = logging.LogRecord("aichat.logfiles", level, __file__, 0, text, None, None)
        try:
            self.stream.write(self.format(record) + self.terminator)
            self.stream.flush()
        except Exception:  # noqa: BLE001 - a note is never worth a failed record
            pass
