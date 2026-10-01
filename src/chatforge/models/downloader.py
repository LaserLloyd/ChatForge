"""Resumable, multi-file downloads of OpenVINO model repos into ``models\\<pub>\\<repo>``.

Adapted from StudioForge src/studioforge/core/downloader.py (MIT, LaserLloyd).
Kept: the ``.part`` + ``Range`` protocol with the 206 check, restart on a 200,
416 recovery, sha256 against the LFS oid, the exclusive ``.part`` lock held for
the whole transfer, on-disk verification before publish, transient-only retries
with jittered backoff, and cancellation through task cancellation. Cut: the
SQLite store (``downloads.json`` instead, written atomically at 1 Hz), the
planner and fit verdicts, GGUF, mmproj, quarantine, sibling-writer waits,
secondary-instance gating and adopt-time re-hashing (adoption is by size).

A **group** is one repo. Its files download sequentially, each into
``<dest>.part`` and then renamed onto ``<dest>``. When every file is complete the
sidecar ``.chatforge-model.json`` is written (``source: "chatforge-downloader"``).

Groups that were queued or running when the app last exited come back as
``paused`` and wait for an explicit :meth:`Downloader.resume` (PLAN WS4 step 3).
"""

from __future__ import annotations

import asyncio
import contextlib
import errno
import hashlib
import json
import logging
import os
import random
import re
import sys
import time
import uuid
from collections import deque
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Final, Literal, cast

import httpx

from chatforge.models import diskspace
from chatforge.models.hf_search import (
    DEFAULT_HF_ENDPOINT,
    HfSearch,
    ModelError,
    RepoFile,
    file_url,
    safe_relpath,
    validate_repo_id,
)
from chatforge.models.registry import _is_under, model_slug, write_sidecar

log = logging.getLogger(__name__)

__all__ = [
    "DOWNLOAD_MAX_ATTEMPTS",
    "FREE_SPACE_MARGIN_BYTES",
    "ChecksumMismatchError",
    "DownloadProgress",
    "DownloadStatus",
    "Downloader",
    "PartFileLockedError",
    "PublishFailedError",
]

DownloadStatus = Literal["queued", "running", "paused", "completed", "failed", "canceled"]

#: Statuses whose bytes are still coming (the disk-space estimate).
_PENDING: Final[tuple[DownloadStatus, ...]] = ("queued", "running", "paused")

#: Progress callback rate (4 Hz) and ``downloads.json`` rate (1 Hz).
_EMIT_INTERVAL_S: Final = 0.25
_PERSIST_INTERVAL_S: Final = 1.0
_SPEED_WINDOW_S: Final = 5.0
_REHASH_CHUNK: Final = 4 * 1024 * 1024
_STATE_SCHEMA: Final = 1

#: PLAN WS4: refuse unless free space >= total + 2 GB.
FREE_SPACE_MARGIN_BYTES: Final = 2 * 1024**3

_CONTENT_RANGE_RE: Final = re.compile(
    r"\Abytes\s+(?P<start>\d+)-(?P<end>\d+)/(?P<total>\d+|\*)\Z", re.I
)

# --- retry policy (StudioForge) -----------------------------------------------
DOWNLOAD_MAX_ATTEMPTS: Final = 5
_RETRY_BASE_S: Final = 2.0
_RETRY_CAP_S: Final = 60.0
_RETRY_JITTER: Final = (0.8, 1.2)

#: Byte offset of the exclusive lock taken on a ``.part``: past the end of any
#: real file, so the lock never overlaps data another handle might read.
_PART_LOCK_OFFSET: Final = 1 << 62
_PART_OPEN_FLAGS: Final = os.O_RDWR | os.O_CREAT | getattr(os, "O_BINARY", 0)

#: Errno values that mean "this will not get better by waiting".
_FATAL_ERRNOS: Final = frozenset(
    {
        getattr(errno, name)
        for name in ("ENOSPC", "EDQUOT", "EFBIG", "EROFS", "ENAMETOOLONG")
        if hasattr(errno, name)
    }
)


def _part_path(dest: Path) -> Path:
    return dest.with_name(dest.name + ".part")


class _RangeUnsatisfiable(Exception):
    """HTTP 416: the ``.part`` is at or past the object size. Restart clean."""


class ChecksumMismatchError(ModelError):
    """The downloaded bytes do not match the sha256 HuggingFace published."""

    def __init__(self, message: str, **kwargs: Any) -> None:
        super().__init__(message, code="checksum_mismatch", **kwargs)


class PartFileLockedError(ModelError):
    """Somebody else is already writing this ``.part``. Deliberately not retryable."""

    def __init__(self, message: str, **kwargs: Any) -> None:
        super().__init__(message, code="part_file_locked", **kwargs)


class PublishFailedError(ModelError):
    """A verified partial could not be renamed onto its destination; it is kept."""

    def __init__(self, message: str, **kwargs: Any) -> None:
        super().__init__(message, code="publish_failed", **kwargs)


# Adapted from StudioForge src/studioforge/core/downloader.py (MIT, LaserLloyd).
@dataclass
class DownloadProgress:
    """Snapshot of one file's transfer, as pushed to the UI."""

    id: str
    group_id: str
    repo_id: str
    filename: str
    status: DownloadStatus
    downloaded_bytes: int
    total_bytes: int
    speed_bps: float
    eta_s: float | None
    error: str | None
    attempt: int = 0
    max_attempts: int = DOWNLOAD_MAX_ATTEMPTS
    next_retry_at: float | None = None
    last_error: str | None = None
    part_bytes: int = 0

    @property
    def percent(self) -> float:
        if self.total_bytes <= 0:
            return 0.0
        return min(100.0, 100.0 * self.downloaded_bytes / self.total_bytes)

    @property
    def retry_in_s(self) -> float | None:
        if self.next_retry_at is None:
            return None
        return max(0.0, self.next_retry_at - time.time())

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "group_id": self.group_id,
            "repo_id": self.repo_id,
            "filename": self.filename,
            "status": self.status,
            "downloaded_bytes": self.downloaded_bytes,
            "total_bytes": self.total_bytes,
            "percent": self.percent,
            "speed_bps": self.speed_bps,
            "eta_s": self.eta_s,
            "error": self.error,
            "attempt": self.attempt,
            "max_attempts": self.max_attempts,
            "next_retry_at": self.next_retry_at,
            "retry_in_s": self.retry_in_s,
            "last_error": self.last_error,
            "part_bytes": self.part_bytes,
        }


# Adapted from StudioForge src/studioforge/core/downloader.py (MIT, LaserLloyd).
@dataclass
class _FileState:
    """Durable row plus the runtime bookkeeping that never hits ``downloads.json``."""

    id: str
    group_id: str
    repo_id: str
    filename: str
    dest: Path
    status: DownloadStatus
    total_bytes: int = 0
    downloaded_bytes: int = 0
    sha256: str | None = None
    error: str | None = None
    created_at: float = field(default_factory=time.time)
    samples: deque[tuple[float, int]] = field(default_factory=deque)
    attempt: int = 0
    next_retry_at: float | None = None
    last_error: str | None = None
    part_bytes: int = 0

    def observe(self, now: float) -> None:
        """Record a speed sample and drop everything outside the window."""
        self.samples.append((now, self.downloaded_bytes))
        cutoff = now - _SPEED_WINDOW_S
        while len(self.samples) > 2 and self.samples[0][0] < cutoff:
            self.samples.popleft()

    @property
    def speed_bps(self) -> float:
        if len(self.samples) < 2:
            return 0.0
        (t0, b0), (t1, b1) = self.samples[0], self.samples[-1]
        span = t1 - t0
        if span <= 0:
            return 0.0
        return max(0.0, (b1 - b0) / span)

    @property
    def eta_s(self) -> float | None:
        speed = self.speed_bps
        if speed <= 0 or self.total_bytes <= 0:
            return None
        return max(0, self.total_bytes - self.downloaded_bytes) / speed

    def snapshot(self) -> DownloadProgress:
        return DownloadProgress(
            id=self.id,
            group_id=self.group_id,
            repo_id=self.repo_id,
            filename=self.filename,
            status=self.status,
            downloaded_bytes=self.downloaded_bytes,
            total_bytes=self.total_bytes,
            speed_bps=self.speed_bps,
            eta_s=self.eta_s,
            error=self.error,
            attempt=self.attempt,
            next_retry_at=self.next_retry_at,
            last_error=self.last_error,
            part_bytes=self.part_bytes,
        )

    def row(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "filename": self.filename,
            "status": self.status,
            "total_bytes": self.total_bytes,
            "downloaded_bytes": self.downloaded_bytes,
            "sha256": self.sha256,
            "error": self.error,
            "created_at": self.created_at,
        }


@dataclass
class _Group:
    id: str
    repo_id: str
    files: list[str]
    revision: str | None = None
    license: str | None = None
    created_at: float = field(default_factory=time.time)
    error: str | None = None


# Adapted from StudioForge src/studioforge/core/downloader.py (MIT, LaserLloyd).
class _PartFile:
    """Exclusive owner of one ``.part`` for the whole life of a transfer.

    Opened once and locked once; every read, write and the size check that
    proves completion go through this one handle. The OS lock is released by
    the kernel however the holder dies, so a crash leaves a resumable partial.
    Deleting (``discard``) and renaming (``publish``) happen after close,
    because Windows refuses both on an open file.
    """

    def __init__(self, path: Path) -> None:
        self.path = path
        self._fd: int | None = None
        self._discard = False

    def __enter__(self) -> _PartFile:
        fd = os.open(self.path, _PART_OPEN_FLAGS, 0o644)
        try:
            _lock_part_fd(fd)
        except OSError as exc:
            os.close(fd)
            raise PartFileLockedError(
                f"{self.path.name}: another process is writing this file "
                f"({type(exc).__name__}: {exc}). Two writers would interleave into one "
                "corrupt file, so this transfer is refused. Close the other ChatForge and "
                "resume.",
                details={"path": str(self.path)},
            ) from exc
        self._fd = fd
        return self

    def __exit__(self, *_exc: object) -> None:
        self.close()

    def close(self) -> None:
        fd, self._fd = self._fd, None
        if fd is not None:
            with contextlib.suppress(OSError):
                os.close(fd)
        if self._discard:
            self._discard = False
            self.path.unlink(missing_ok=True)

    @property
    def fd(self) -> int:
        if self._fd is None:  # pragma: no cover - programming error
            raise RuntimeError("the .part file is not open")
        return self._fd

    @property
    def size(self) -> int:
        return os.fstat(self.fd).st_size

    def discard(self) -> None:
        """Mark the partial as garbage; it is deleted when the handle closes."""
        self._discard = True

    def truncate_to_zero(self) -> None:
        os.ftruncate(self.fd, 0)
        os.lseek(self.fd, 0, os.SEEK_SET)

    def seek_to(self, offset: int) -> None:
        os.lseek(self.fd, offset, os.SEEK_SET)

    def write(self, chunk: bytes) -> None:
        """Write ALL of ``chunk``, looping on short writes."""
        view = memoryview(chunk)
        while view:
            written = os.write(self.fd, view)
            if written <= 0:
                raise OSError(f"os.write wrote {written} bytes to {self.path}")
            view = view[written:]

    def rehash(self, hasher: Any) -> int:
        """Feed the existing bytes into ``hasher``; return how many there were."""
        os.lseek(self.fd, 0, os.SEEK_SET)
        size = 0
        while True:
            block = os.read(self.fd, _REHASH_CHUNK)
            if not block:
                break
            hasher.update(block)
            size += len(block)
        return size

    def sync(self) -> None:
        os.fsync(self.fd)

    def publish(self, dest: Path) -> None:
        """Close the handle, then rename the verified partial onto ``dest``."""
        self._discard = False
        self.close()
        self.path.replace(dest)


# Adapted from StudioForge src/studioforge/core/downloader.py (MIT, LaserLloyd).
def _lock_part_fd(fd: int) -> None:
    """Exclusive, non-blocking OS lock on ``fd``. Raises ``OSError`` if taken."""
    if sys.platform == "win32":
        import msvcrt

        os.lseek(fd, _PART_LOCK_OFFSET, os.SEEK_SET)
        try:
            msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)
        finally:
            os.lseek(fd, 0, os.SEEK_SET)
    else:
        import fcntl

        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)


# Adapted from StudioForge src/studioforge/core/downloader.py (MIT, LaserLloyd).
def _retry_after_s(exc: BaseException) -> float | None:
    """``Retry-After`` the upstream asked for, when it named one."""
    if isinstance(exc, ModelError):
        value = exc.details.get("retry_after_s")
        if isinstance(value, int | float) and value >= 0:
            return float(value)
    return None


# Adapted from StudioForge src/studioforge/core/downloader.py (MIT, LaserLloyd).
def _is_transient(exc: BaseException) -> bool:
    """Transport faults, 5xx, 429 and momentary OS blocks retry; answers do not."""
    if isinstance(exc, PartFileLockedError | ChecksumMismatchError | PublishFailedError):
        return False
    if isinstance(exc, httpx.TransportError):
        return True
    if isinstance(exc, ModelError):
        status = exc.details.get("status")
        return isinstance(status, int) and (status == 429 or status >= 500)
    if isinstance(exc, OSError):
        return exc.errno not in _FATAL_ERRNOS
    return False


# Adapted from StudioForge src/studioforge/core/downloader.py (MIT, LaserLloyd).
def _backoff_delay(attempt: int, *, retry_after: float | None = None) -> float:
    """Jittered exponential backoff for ``attempt`` (1-based), in seconds."""
    delay = min(_RETRY_CAP_S, _RETRY_BASE_S * (2 ** max(0, attempt - 1)))
    if retry_after is not None:
        delay = max(delay, min(_RETRY_CAP_S, retry_after))
    return delay * random.uniform(*_RETRY_JITTER)  # noqa: S311 - jitter, not crypto


# Adapted from StudioForge src/studioforge/core/downloader.py (MIT, LaserLloyd).
def _describe(exc: BaseException) -> str:
    """One-line rendering of a failure, for the state field and the UI."""
    if isinstance(exc, ModelError):
        return exc.message
    return f"{type(exc).__name__}: {exc}"


# Adapted from StudioForge src/studioforge/core/downloader.py (MIT, LaserLloyd).
def _file_sha256(path: Path) -> str:
    """sha256 of a file on disk. Blocking; call it from a worker thread."""
    hasher = hashlib.sha256()
    with path.open("rb") as handle:
        while block := handle.read(_REHASH_CHUNK):
            hasher.update(block)
    return hasher.hexdigest()


def _stat_size(path: Path) -> int:
    try:
        return path.stat().st_size
    except OSError:
        return 0


# Adapted from StudioForge src/studioforge/core/downloader.py (MIT, LaserLloyd).
def _parse_retry_after(response: httpx.Response) -> float | None:
    """``Retry-After`` in delta-seconds, when the server sent a parseable one."""
    raw = response.headers.get("Retry-After")
    if not raw:
        return None
    try:
        value = float(raw.strip())
    except ValueError:
        return None
    return value if value >= 0 else None


# Adapted from StudioForge src/studioforge/core/downloader.py (MIT, LaserLloyd).
def _range_honoured(response: httpx.Response, have: int) -> bool:
    """A 206 whose ``Content-Range`` starts exactly at ``have``."""
    if response.status_code != 206:
        return False
    header = response.headers.get("Content-Range")
    if not header:
        return False
    match = _CONTENT_RANGE_RE.match(header.strip())
    if match is None:
        return False
    return int(match.group("start")) == have


# Adapted from StudioForge src/studioforge/core/downloader.py (MIT, LaserLloyd).
def _total_size(response: httpx.Response, have: int, fallback: int) -> int:
    """Object size in bytes, preferring ``Content-Range`` over ``Content-Length``."""
    header = response.headers.get("Content-Range")
    if header:
        match = _CONTENT_RANGE_RE.match(header.strip())
        if match is not None and match.group("total") != "*":
            return int(match.group("total"))
    length = response.headers.get("Content-Length")
    if length is not None:
        with contextlib.suppress(ValueError):
            return have + int(length)
    return fallback


class Downloader:
    """Resumable repo downloads with progress, cancel and ``downloads.json`` state."""

    def __init__(
        self,
        models_dir: Path,
        downloads_file: Path,
        *,
        hf: HfSearch,
        client: httpx.AsyncClient | None = None,
        endpoint: str = DEFAULT_HF_ENDPOINT,
        token: str | None = None,
        free_bytes: Callable[[Path], int | None] | None = None,
        sleep: Callable[[float], Awaitable[Any]] | None = None,
        clock: Callable[[], float] = time.monotonic,
        chunk_bytes: int | None = None,
    ) -> None:
        self.models_dir = Path(models_dir)
        self.downloads_file = Path(downloads_file)
        self._hf = hf
        self._endpoint = endpoint.rstrip("/")
        self._token = token
        self._owns_client = client is None
        self._client = client or httpx.AsyncClient(
            # No overall timeout: a 2 GB file can legitimately stream for a long
            # time. The read timeout is what detects a dead connection.
            timeout=httpx.Timeout(None, connect=30.0, read=120.0),
            follow_redirects=True,
        )
        self._free_bytes = free_bytes or diskspace.free_bytes
        self._sleep = sleep or asyncio.sleep
        self._clock = clock
        self._chunk_bytes = chunk_bytes
        self._files: dict[str, _FileState] = {}
        self._groups: dict[str, _Group] = {}
        self._tasks: dict[str, asyncio.Task[None]] = {}
        self._intent: dict[str, str] = {}
        self._subscribers: list[Callable[[dict[str, Any]], None]] = []
        #: One repo at a time: files share a disk and a link, racing them buys nothing.
        self._semaphore = asyncio.Semaphore(1)
        self._last_persist = float("-inf")
        self._started = False

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    async def start(self) -> None:
        """Load ``downloads.json``. Anything pending comes back as ``paused``."""
        if self._started:
            return
        self._load_state()
        self._started = True
        changed = False
        for state in self._files.values():
            if state.status in ("queued", "running"):
                state.status = "paused"
                changed = True
        if changed:
            self._persist(force=True)

    async def aclose(self) -> None:
        """Stop in-flight transfers (kept resumable as ``paused``) and close the client."""
        for gid in list(self._tasks):
            self._intent[gid] = "stop"
        tasks = list(self._tasks.values())
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        self._persist(force=True)
        if self._owns_client:
            await self._client.aclose()

    # ------------------------------------------------------------------
    # Subscriptions
    # ------------------------------------------------------------------

    # Adapted from StudioForge src/studioforge/core/downloader.py (MIT, LaserLloyd).
    def subscribe(self, cb: Callable[[dict[str, Any]], None]) -> Callable[[], None]:
        """Register a progress callback; returns its unsubscribe function.

        Each call receives ``DownloadProgress.to_dict()`` for one file plus
        ``group_status``, ``group_downloaded_bytes``, ``group_total_bytes`` and
        ``group_percent``.
        """
        self._subscribers.append(cb)

        def unsubscribe() -> None:
            with contextlib.suppress(ValueError):
                self._subscribers.remove(cb)

        return unsubscribe

    # Adapted from StudioForge src/studioforge/core/downloader.py (MIT, LaserLloyd).
    def _emit(self, state: _FileState) -> None:
        """Fan a snapshot out. A raising subscriber is logged and dropped."""
        if not self._subscribers:
            return
        payload = state.snapshot().to_dict()
        summary = self._group_summary(state.group_id)
        payload.update(
            group_status=summary["status"],
            group_downloaded_bytes=summary["downloaded_bytes"],
            group_total_bytes=summary["total_bytes"],
            group_percent=summary["percent"],
        )
        for callback in list(self._subscribers):
            try:
                callback(payload)
            except Exception as exc:  # noqa: BLE001 - a UI hiccup must not cost a download
                log.warning("downloader.subscriber_failed error=%s", _describe(exc))
                with contextlib.suppress(ValueError):
                    self._subscribers.remove(callback)

    # ------------------------------------------------------------------
    # Queries
    # ------------------------------------------------------------------

    # Adapted from StudioForge src/studioforge/core/downloader.py (MIT, LaserLloyd).
    def group_status(self, group_id: str) -> DownloadStatus:
        """Status of a whole repo download, derived from its files."""
        group = self._groups.get(group_id)
        statuses = [self._files[i].status for i in group.files] if group else []
        if not statuses:
            return "queued"
        if "failed" in statuses:
            return "failed"
        if "running" in statuses:
            return "running"
        if all(s == "completed" for s in statuses):
            return "completed"
        if "queued" in statuses:
            return "queued"
        if "canceled" in statuses:
            return "canceled"
        return "paused"

    def _group_summary(self, group_id: str) -> dict[str, Any]:
        group = self._groups[group_id]
        states = [self._files[i] for i in group.files]
        total = sum(s.total_bytes for s in states)
        done = sum(s.downloaded_bytes for s in states)
        current = next((s for s in states if s.status == "running"), None)
        error = next((s.error for s in states if s.error), None) or group.error
        speed = current.speed_bps if current else 0.0
        eta = (max(0, total - done) / speed) if speed > 0 and total > 0 else None
        return {
            "group_id": group.id,
            "repo_id": group.repo_id,
            "revision": group.revision,
            "status": self.group_status(group_id),
            "downloaded_bytes": done,
            "total_bytes": total,
            "percent": min(100.0, 100.0 * done / total) if total > 0 else 0.0,
            "speed_bps": speed,
            "eta_s": eta,
            "error": error,
            "current_file": current.filename if current else None,
            "files_done": sum(1 for s in states if s.status == "completed"),
            "files_total": len(states),
            "created_at": group.created_at,
        }

    def all(self) -> list[dict[str, Any]]:
        """One dict per repo download (with its ``files``), oldest first."""
        out: list[dict[str, Any]] = []
        for group in sorted(self._groups.values(), key=lambda g: g.created_at):
            summary = self._group_summary(group.id)
            summary["files"] = [self._files[i].snapshot().to_dict() for i in group.files]
            out.append(summary)
        return out

    def get(self, group_id: str) -> dict[str, Any] | None:
        return next((g for g in self.all() if g["group_id"] == group_id), None)

    def is_downloading(self, repo_id: str) -> bool:
        """Whether a repo has a queued, running or paused download (delete refuses it)."""
        gid = model_slug(repo_id)
        return gid in self._groups and self.group_status(gid) in _PENDING

    def _remaining_bytes(self, ids: list[str]) -> int:
        return sum(
            max(0, self._files[i].total_bytes - _stat_size(_part_path(self._files[i].dest)))
            for i in ids
            if self._files[i].status in _PENDING
        )

    # ------------------------------------------------------------------
    # Destination paths
    # ------------------------------------------------------------------

    def _dest_for(self, repo_id: str, rel: str) -> Path:
        """``models_dir/<pub>/<repo>/<rel>``, validated and confined (PLAN §7 item 7)."""
        repo = validate_repo_id(repo_id)
        safe_relpath(rel)
        publisher, _, name = repo.partition("/")
        dest = self.models_dir.joinpath(publisher, name, *rel.split("/"))
        self._assert_dest(dest)
        return dest

    def _assert_dest(self, dest: Path) -> None:
        if not _is_under(dest, self.models_dir):
            raise ModelError(
                f"refusing to write {dest}: outside the models directory", code="path_escape"
            )

    # ------------------------------------------------------------------
    # Enqueue / control
    # ------------------------------------------------------------------

    async def enqueue(self, repo_id: str) -> str:
        """Queue a repo download and return its group id (the model slug).

        Files already on disk with the listed size are marked completed without
        traffic. Refuses (``insufficient_disk``) unless free space covers what
        is left to fetch, across the whole queue, plus 2 GiB.
        """
        repo = validate_repo_id(repo_id)
        gid = model_slug(repo)
        if gid in self._groups:
            status = self.group_status(gid)
            if status in ("queued", "running"):
                return gid
            if status in ("paused", "failed"):
                await self.resume(gid)
                return gid
            self._drop_group(gid)

        meta = await self._hf.repo_meta(repo)
        listing = await self._hf.repo_files(repo)
        wanted = [f for f in listing if not any(p.startswith(".") for p in f.path.split("/"))]
        if not wanted:
            raise ModelError(f"{repo} has no files to download", code="empty_repo")

        base_time = time.time()
        states: list[_FileState] = []
        for index, info in enumerate(wanted):
            dest = self._dest_for(repo, info.path)
            state = _FileState(
                id=f"{gid}:{info.path}",
                group_id=gid,
                repo_id=repo,
                filename=info.path,
                dest=dest,
                status="queued",
                total_bytes=info.size,
                sha256=info.sha256,
                created_at=base_time + index * 1e-3,
            )
            if not self._adopt_complete(state):
                state.downloaded_bytes = _stat_size(_part_path(dest))
            states.append(state)

        needed = sum(
            max(0, s.total_bytes - s.downloaded_bytes) for s in states if s.status in _PENDING
        )
        others = [i for g in self._groups.values() for i in g.files]
        self._refuse_if_disk_cannot_hold(needed + self._remaining_bytes(others))

        for state in states:
            self._files[state.id] = state
        self._groups[gid] = _Group(
            id=gid,
            repo_id=repo,
            files=[s.id for s in states],
            revision=meta.sha,
            license=meta.license,
            created_at=base_time,
        )
        self._persist(force=True)
        for state in states:
            self._emit(state)
        self._launch(gid)
        log.info("downloader.enqueued repo=%s files=%d bytes=%d", repo, len(states), needed)
        return gid

    def _refuse_if_disk_cannot_hold(self, remaining: int) -> None:
        if remaining <= 0:
            return
        free = self._free_bytes(self.models_dir)
        if free is None:
            return  # unknown is not "full"; never refuse on a failed measurement
        need = remaining + FREE_SPACE_MARGIN_BYTES
        if free < need:
            gib = 1024**3
            raise ModelError(
                f"Not enough disk space: {free / gib:.1f} GiB free, "
                f"{need / gib:.1f} GiB needed (download plus 2 GiB headroom).",
                code="insufficient_disk",
                hint="Free some space, or delete a model you no longer use.",
                details={"free_bytes": free, "needed_bytes": need},
            )

    def _adopt_complete(self, state: _FileState) -> bool:
        """Mark an already-present file completed if its size matches (no re-hash)."""
        size = _stat_size(state.dest) if state.dest.is_file() else 0
        if size <= 0:
            return False
        if state.total_bytes > 0 and size != state.total_bytes:
            return False
        state.downloaded_bytes = size
        if state.total_bytes <= 0:
            state.total_bytes = size
        state.status = "completed"
        state.error = None
        return True

    def _launch(self, group_id: str) -> None:
        if group_id in self._tasks:
            return
        self._intent.pop(group_id, None)
        self._tasks[group_id] = asyncio.create_task(
            self._run_group(group_id), name=f"download:{group_id}"
        )

    async def resume(self, gid: str) -> None:
        """Re-queue a paused, failed or canceled group and restart its transfer."""
        group = self._groups.get(gid)
        if group is None:
            raise ModelError(f"unknown download: {gid}", code="not_found")
        if gid in self._tasks:
            return
        states = [self._files[i] for i in group.files]
        revive = [s for s in states if s.status in ("paused", "failed", "canceled")]
        needed = sum(max(0, s.total_bytes - _stat_size(_part_path(s.dest))) for s in revive)
        others = [i for g in self._groups.values() if g.id != gid for i in g.files]
        self._refuse_if_disk_cannot_hold(needed + self._remaining_bytes(others))
        group.error = None
        for state in revive:
            state.error = None
            state.attempt = 0
            state.next_retry_at = None
            state.part_bytes = 0
            self._set_status(state, "queued")
        self._launch(gid)

    async def pause(self, gid: str) -> None:
        """Stop a group, keeping the ``.part`` files so resume is cheap."""
        await self._interrupt(gid, "pause")

    async def cancel(self, gid: str, *, delete_partial: bool = True) -> None:
        """Stop a group for good, deleting its partial files unless told not to."""
        await self._interrupt(gid, "cancel" if delete_partial else "cancel-keep")

    def dismiss(self, gid: str) -> None:
        """Forget a finished (completed, failed or canceled) group."""
        if gid in self._tasks:
            raise ModelError("that download is still running; cancel it first", code="busy")
        if gid in self._groups:
            self._drop_group(gid)
            self._persist(force=True)

    async def join(self, gid: str) -> None:
        """Wait until the group's task (if any) has finished."""
        task = self._tasks.get(gid)
        if task is not None:
            await asyncio.gather(task, return_exceptions=True)

    def _drop_group(self, gid: str) -> None:
        group = self._groups.pop(gid, None)
        if group is not None:
            for file_id in group.files:
                self._files.pop(file_id, None)

    # Adapted from StudioForge src/studioforge/core/downloader.py (MIT, LaserLloyd).
    async def _interrupt(self, gid: str, intent: str) -> None:
        if gid not in self._groups:
            raise ModelError(f"unknown download: {gid}", code="not_found")
        task = self._tasks.get(gid)
        self._intent[gid] = intent
        if task is not None:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
        else:
            self._intent.pop(gid, None)
            self._finalize_interrupted(gid, intent)

    # Adapted from StudioForge src/studioforge/core/downloader.py (MIT, LaserLloyd).
    def _finalize_interrupted(self, gid: str, intent: str) -> None:
        targets: dict[str, DownloadStatus] = {
            "pause": "paused",
            "cancel": "canceled",
            "cancel-keep": "canceled",
            "stop": "paused",
        }
        target = targets.get(intent, "paused")
        group = self._groups.get(gid)
        for file_id in group.files if group else []:
            state = self._files[file_id]
            if state.status == "completed":
                continue
            if intent == "cancel":
                with contextlib.suppress(OSError):
                    _part_path(state.dest).unlink(missing_ok=True)
                state.downloaded_bytes = 0
            state.next_retry_at = None
            self._set_status(state, target)

    # ------------------------------------------------------------------
    # Transfer
    # ------------------------------------------------------------------

    # Adapted from StudioForge src/studioforge/core/downloader.py (MIT, LaserLloyd).
    async def _run_group(self, gid: str) -> None:
        """Download one repo's files sequentially, one repo at a time."""
        try:
            async with self._semaphore:
                group = self._groups[gid]
                for file_id in group.files:
                    state = self._files[file_id]
                    if state.status in ("completed", "canceled", "paused"):
                        continue
                    await self._download_file(state)
                    if state.status == "failed":
                        break
                if self.group_status(gid) == "completed":
                    self._on_group_complete(group)
        except asyncio.CancelledError:
            intent = self._intent.pop(gid, "cancel")
            self._finalize_interrupted(gid, intent)
            raise
        except Exception as exc:  # noqa: BLE001 - the task must not die silently
            log.error("downloader.group_task_failed group=%s error=%s", gid, _describe(exc))
            group = self._groups.get(gid)
            for file_id in group.files if group else []:
                stranded = self._files.get(file_id)
                if stranded is not None and stranded.status in ("queued", "running"):
                    with contextlib.suppress(Exception):
                        self._fail(stranded, f"download task failed: {_describe(exc)}")
        finally:
            self._tasks.pop(gid, None)
            self._persist(force=True)

    def _on_group_complete(self, group: _Group) -> None:
        """Write the sidecar once every file is on disk."""
        states = [self._files[i] for i in group.files]
        publisher, _, name = group.repo_id.partition("/")
        model_dir = self.models_dir / publisher / name
        try:
            write_sidecar(
                model_dir,
                repo_id=group.repo_id,
                files=[RepoFile(s.filename, s.total_bytes, s.sha256) for s in states],
                source="chatforge-downloader",
                revision=group.revision,
                license=group.license,
                models_dir=self.models_dir,
            )
        except (OSError, ModelError) as exc:
            group.error = f"downloaded, but the sidecar could not be written: {_describe(exc)}"
            log.error("downloader.sidecar_failed repo=%s error=%s", group.repo_id, exc)
        diskspace.clear_cache()
        log.info("downloader.group_completed repo=%s", group.repo_id)
        if states:
            self._emit(states[-1])

    async def _download_file(self, state: _FileState) -> None:
        """Own the ``.part``, then transfer it -- with retries -- under that lock."""
        dest = state.dest
        part = _part_path(dest)
        try:
            self._assert_dest(dest)  # before the first byte is written
            dest.parent.mkdir(parents=True, exist_ok=True)
            if self._adopt_complete(state):
                self._set_status(state, "completed")
                return
            with _PartFile(part) as part_file:
                await self._transfer_with_retries(state, part_file)
        except asyncio.CancelledError:
            raise
        except ModelError as exc:
            self._fail(state, exc.message)
        except OSError as exc:
            self._fail(state, self._describe_os_error(state, exc))
        except Exception as exc:  # noqa: BLE001 - reported on the row, never raised
            self._fail(state, _describe(exc))

    # Adapted from StudioForge src/studioforge/core/downloader.py (MIT, LaserLloyd).
    async def _transfer_with_retries(self, state: _FileState, part_file: _PartFile) -> None:
        """Retry transient failures with jittered backoff, resuming each time.

        The backoff is an awaitable sleep inside the group's task, so pause and
        cancel interrupt it immediately.
        """
        attempt = 0
        while True:
            attempt += 1
            state.attempt = attempt
            state.next_retry_at = None
            try:
                await self._transfer_once(state, part_file)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                if attempt >= DOWNLOAD_MAX_ATTEMPTS or not _is_transient(exc):
                    raise
                delay = _backoff_delay(attempt, retry_after=_retry_after_s(exc))
                state.last_error = _describe(exc)
                state.next_retry_at = time.time() + delay
                log.warning(
                    "downloader.retrying id=%s attempt=%d delay_s=%.1f error=%s",
                    state.id,
                    attempt,
                    delay,
                    state.last_error,
                )
                self._emit(state)
                await self._sleep(delay)
                state.next_retry_at = None
                self._emit(state)
                continue
            state.attempt = 0
            state.next_retry_at = None
            return

    # Adapted from StudioForge src/studioforge/core/downloader.py (MIT, LaserLloyd).
    async def _transfer_once(self, state: _FileState, part_file: _PartFile) -> None:
        """One attempt, including the "the server keeps saying 416" fallback."""
        for allow_resume in (True, False):
            try:
                await self._transfer(state, part_file, allow_resume=allow_resume)
                return
            except _RangeUnsatisfiable:
                log.warning("downloader.range_unsatisfiable id=%s", state.id)
                part_file.truncate_to_zero()
                state.downloaded_bytes = 0
        raise ModelError(
            "the server kept answering HTTP 416 even without a Range request",
            code="upstream_error",
        )

    # Adapted from StudioForge src/studioforge/core/downloader.py (MIT, LaserLloyd).
    async def _transfer(
        self, state: _FileState, part_file: _PartFile, *, allow_resume: bool
    ) -> None:
        have = 0
        hasher = hashlib.sha256()
        if allow_resume and part_file.size > 0:
            # The stream hash must cover the bytes already on disk, so they are
            # re-read, off the event loop.
            have = await asyncio.to_thread(part_file.rehash, hasher)
            if have > 0 and state.total_bytes > 0 and have == state.total_bytes:
                # A crash landed between the last write and the rename: prove the
                # partial from the hash just computed and publish without a request.
                if not state.sha256 or hasher.hexdigest() == state.sha256.lower():
                    self._set_status(state, "running")
                    self._finish(state, part_file, have, hasher)
                    return
                part_file.truncate_to_zero()
                have = 0
                hasher = hashlib.sha256()

        group = self._groups.get(state.group_id)
        revision = (group.revision if group else None) or "main"
        url = file_url(state.repo_id, state.filename, endpoint=self._endpoint, revision=revision)
        headers = self._headers()
        if have > 0:
            headers["Range"] = f"bytes={have}-"

        state.downloaded_bytes = have
        state.samples.clear()
        self._set_status(state, "running")

        async with self._client.stream("GET", url, headers=headers) as response:
            if response.status_code == 416:
                raise _RangeUnsatisfiable
            if response.status_code >= 400:
                await response.aread()
                raise self._http_error(state, response)

            resumed = have > 0 and _range_honoured(response, have)
            if have > 0 and not resumed:
                # 200 (or a 206 starting elsewhere) to a Range request: the server
                # is sending the whole object. Appending would corrupt the file.
                log.warning(
                    "downloader.range_ignored id=%s status=%d", state.id, response.status_code
                )
                have = 0
                hasher = hashlib.sha256()
                state.downloaded_bytes = 0

            declared = state.total_bytes
            total = _total_size(response, have, declared)
            if declared > 0 and total > 0 and total != declared:
                part_file.discard()
                raise ModelError(
                    f"{state.filename}: size mismatch, the repository lists {declared} bytes "
                    f"but the server is sending {total}; refusing to write it",
                    code="size_mismatch",
                    details={"declared_bytes": declared, "server_bytes": total},
                )
            if total > 0:
                state.total_bytes = total

            written = have
            now = self._clock()
            state.observe(now)
            last_emit = now

            if resumed:
                part_file.seek_to(have)
            else:
                part_file.truncate_to_zero()

            async for chunk in response.aiter_bytes(self._chunk_bytes):
                if not chunk:
                    continue
                part_file.write(chunk)
                hasher.update(chunk)
                written += len(chunk)
                state.downloaded_bytes = written

                now = self._clock()
                if now - last_emit >= _EMIT_INTERVAL_S:
                    state.observe(now)
                    self._emit(state)
                    last_emit = now
                self._persist()

        await asyncio.to_thread(part_file.sync)
        self._finish(state, part_file, written, hasher)

    # Adapted from StudioForge src/studioforge/core/downloader.py (MIT, LaserLloyd).
    def _finish(self, state: _FileState, part_file: _PartFile, written: int, hasher: Any) -> None:
        """Verify on disk, publish, and mark complete."""
        dest = state.dest
        self._verify(state, part_file, written, hasher)
        try:
            part_file.publish(dest)
        except OSError as exc:
            raise PublishFailedError(
                f"{state.filename}: downloaded and verified, but it could not be moved into "
                f"place ({type(exc).__name__}: {exc}). If the model is loaded, unload it, then "
                "Resume; the verified partial is kept.",
                details={"path": str(dest), "errno": exc.errno},
            ) from exc
        state.downloaded_bytes = written
        state.part_bytes = 0
        if state.total_bytes <= 0:
            state.total_bytes = written
        state.observe(self._clock())
        self._set_status(state, "completed")
        log.info(
            "downloader.completed id=%s bytes=%d verified=%s",
            state.id,
            written,
            "sha256" if state.sha256 else "length-only",
        )

    def _http_error(self, state: _FileState, response: httpx.Response) -> ModelError:
        status = response.status_code
        details = {
            "status": status,
            "repo_id": state.repo_id,
            "retry_after_s": _parse_retry_after(response),
        }
        if status in (401, 403):
            return ModelError(
                f"HTTP {status} downloading {state.filename} from {state.repo_id}: the "
                "repository is gated or private.",
                code="gated_repo",
                hint="Open the model page on huggingface.co and accept its licence.",
                details=details,
            )
        return ModelError(
            f"HTTP {status} downloading {state.filename} from {state.repo_id}",
            code="http_error",
            details=details,
        )

    # Adapted from StudioForge src/studioforge/core/downloader.py (MIT, LaserLloyd).
    def _verify(self, state: _FileState, part_file: _PartFile, written: int, hasher: Any) -> None:
        """Prove the file is complete *on disk* (fstat after fsync), then its sha256."""
        if state.total_bytes > 0 and written != state.total_bytes:
            part_file.discard()
            raise ModelError(
                f"{state.filename}: transfer ended at {written} bytes but "
                f"{state.total_bytes} were expected; the partial file was discarded",
                code="size_mismatch",
                details={"expected_bytes": state.total_bytes, "actual_bytes": written},
            )
        on_disk = part_file.size
        if on_disk != written:
            part_file.discard()
            raise ModelError(
                f"{state.filename}: {written} bytes were streamed but the partial file holds "
                f"{on_disk}; something else wrote to it, so it was discarded.",
                code="size_mismatch",
                details={"streamed_bytes": written, "on_disk_bytes": on_disk},
            )
        if not state.sha256:
            return
        actual = hasher.hexdigest()
        if actual != state.sha256.lower():
            part_file.discard()
            raise ChecksumMismatchError(
                f"{state.filename}: sha256 mismatch, expected {state.sha256.lower()} "
                f"but got {actual}; the downloaded file was deleted",
                details={"expected_sha256": state.sha256.lower(), "actual_sha256": actual},
            )

    # ------------------------------------------------------------------
    # State transitions and persistence
    # ------------------------------------------------------------------

    def _headers(self) -> dict[str, str]:
        """Auth header only. The token never appears in a URL or a log line."""
        return {"Authorization": f"Bearer {self._token}"} if self._token else {}

    def _set_status(self, state: _FileState, status: DownloadStatus) -> None:
        state.status = status
        self._persist(force=True)
        self._emit(state)

    def _describe_os_error(self, state: _FileState, exc: OSError) -> str:
        if exc.errno not in (errno.ENOSPC, getattr(errno, "EDQUOT", -1)):
            return _describe(exc)
        diskspace.clear_cache()
        free = diskspace.free_bytes(state.dest.parent) or 0
        return (
            f"the disk is full ({free / 1024**3:.1f} GiB free); the partial is kept. "
            "Free some space, then Resume."
        )

    # Adapted from StudioForge src/studioforge/core/downloader.py (MIT, LaserLloyd).
    def _fail(self, state: _FileState, message: str) -> None:
        """Give up on a file, recording what a Resume would continue from."""
        state.error = message
        state.last_error = message
        state.next_retry_at = None
        state.attempt = 0
        state.part_bytes = _stat_size(_part_path(state.dest))
        log.error("downloader.failed id=%s error=%s", state.id, message)
        self._set_status(state, "failed")

    def _persist(self, *, force: bool = False) -> None:
        """Write ``downloads.json`` atomically, at most once a second unless forced."""
        now = self._clock()
        if not force and now - self._last_persist < _PERSIST_INTERVAL_S:
            return
        self._last_persist = now
        payload = {
            "schema": _STATE_SCHEMA,
            "groups": [
                {
                    "group_id": g.id,
                    "repo_id": g.repo_id,
                    "revision": g.revision,
                    "license": g.license,
                    "created_at": g.created_at,
                    "error": g.error,
                    "files": [self._files[i].row() for i in g.files if i in self._files],
                }
                for g in self._groups.values()
            ],
        }
        target = self.downloads_file
        tmp = target.with_name(f"{target.name}.{uuid.uuid4().hex[:8]}.tmp")
        try:
            target.parent.mkdir(parents=True, exist_ok=True)
            tmp.write_text(json.dumps(payload, indent=1), encoding="utf-8")
            os.replace(tmp, target)
        except OSError as exc:
            # A progress hint; the .part size is the truth on the next start.
            log.warning("downloader.persist_failed error=%s", exc)
        finally:
            with contextlib.suppress(OSError):
                tmp.unlink(missing_ok=True)

    def _load_state(self) -> None:
        """Rebuild groups from ``downloads.json``, re-validating every path in it."""
        self._files.clear()
        self._groups.clear()
        try:
            data = json.loads(self.downloads_file.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return
        except (OSError, ValueError) as exc:
            log.warning("downloader.state_unreadable error=%s", exc)
            return
        rows = data.get("groups") if isinstance(data, dict) else None
        for raw in rows if isinstance(rows, list) else []:
            try:
                repo = validate_repo_id(str(raw["repo_id"]))
                gid = model_slug(repo)
                states: list[_FileState] = []
                for row in raw.get("files") or []:
                    rel = str(row["filename"])
                    status = cast(DownloadStatus, str(row.get("status") or "paused"))
                    state = _FileState(
                        id=f"{gid}:{rel}",
                        group_id=gid,
                        repo_id=repo,
                        filename=rel,
                        dest=self._dest_for(repo, rel),
                        status=status,
                        total_bytes=int(row.get("total_bytes") or 0),
                        downloaded_bytes=int(row.get("downloaded_bytes") or 0),
                        sha256=row.get("sha256"),
                        error=row.get("error"),
                        created_at=float(row.get("created_at") or time.time()),
                    )
                    if state.status != "completed":
                        state.downloaded_bytes = _stat_size(_part_path(state.dest))
                    states.append(state)
            except (AttributeError, KeyError, TypeError, ValueError, ModelError) as exc:
                log.warning("downloader.state_row_skipped error=%s", _describe(exc))
                continue
            for state in states:
                self._files[state.id] = state
            self._groups[gid] = _Group(
                id=gid,
                repo_id=repo,
                files=[s.id for s in states],
                revision=raw.get("revision"),
                license=raw.get("license"),
                created_at=float(raw.get("created_at") or time.time()),
                error=raw.get("error"),
            )
