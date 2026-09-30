"""Kernel-enforced lifetime for the OVMS child, plus process-tree helpers.

Adapted from StudioForge src/studioforge/core/supervisor.py (MIT, LaserLloyd):
``WindowsChildJob``, ``create_child_job``, ``_load_win32``, ``CREATE_SUSPENDED``,
the ``_TRACKED_PIDS``/atexit net, ``kill_process_tree``, ``process_is_alive``,
``process_create_time``, ``describe_exit_code`` and ``WINDOWS_EXIT_STATUS``.
Cut: the POSIX pdeathsig shim (AI Chat's child only ever runs on Windows; on
POSIX the atexit net and the process group are the fallback).
"""

from __future__ import annotations

import atexit
import contextlib
import os
import subprocess
from typing import Any

import psutil
import structlog

log = structlog.get_logger(__name__)

# ---------------------------------------------------------------------------
# CreateProcess flags
# ---------------------------------------------------------------------------

#: ``CreateProcess`` flag: the child exists but its initial thread is suspended
#: until someone resumes it. Not exported by :mod:`subprocess`, so spelled out.
CREATE_SUSPENDED = 0x00000004
#: No console window. Without it a console flashes up when the app runs under
#: ``pythonw`` (PLAN §1.10).
CREATE_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0x08000000)
#: New process group, so a console Ctrl+C does not race our own shutdown.
CREATE_NEW_PROCESS_GROUP = getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0x00000200)


def spawn_creationflags(*, suspended: bool) -> int:
    """The ``creationflags`` for an OVMS spawn on Windows (0 elsewhere)."""
    if os.name != "nt":
        return 0
    flags = CREATE_NO_WINDOW | CREATE_NEW_PROCESS_GROUP
    if suspended:
        flags |= CREATE_SUSPENDED
    return flags


# ---------------------------------------------------------------------------
# Interpreter-exit safety net
# ---------------------------------------------------------------------------

_TRACKED_PIDS: set[int] = set()
_ATEXIT_REGISTERED = False


def _register_atexit() -> None:
    global _ATEXIT_REGISTERED
    if not _ATEXIT_REGISTERED:
        atexit.register(_kill_tracked_pids)
        _ATEXIT_REGISTERED = True


def _kill_tracked_pids() -> None:
    """Last-resort cleanup: an abrupt exit must not orphan an NPU holder."""
    for pid in list(_TRACKED_PIDS):
        with contextlib.suppress(Exception):
            kill_process_tree(pid, timeout=2.0, force=True)
    _TRACKED_PIDS.clear()


def track_pid(pid: int) -> None:
    """Add ``pid`` to the atexit kill list."""
    _register_atexit()
    _TRACKED_PIDS.add(pid)


def untrack_pid(pid: int | None) -> None:
    if pid is not None:
        _TRACKED_PIDS.discard(pid)


def tracked_pids() -> set[int]:
    return set(_TRACKED_PIDS)


# ---------------------------------------------------------------------------
# Kernel-enforced child lifetime (Windows job object)
# ---------------------------------------------------------------------------

#: ``OpenProcess`` rights needed to move a process into a job object.
_PROCESS_SET_QUOTA = 0x0100
_PROCESS_TERMINATE = 0x0001


# Adapted from StudioForge src/studioforge/core/supervisor.py _load_win32 (MIT, LaserLloyd)
def _load_win32() -> tuple[Any, Any]:
    """Import the pywin32 job-object bindings.

    A module-level function purely so tests can monkeypatch it to raise
    (simulating a box without pywin32) or to return fakes.
    """
    import win32api
    import win32job

    return win32job, win32api


# Adapted from StudioForge src/studioforge/core/supervisor.py WindowsChildJob (MIT, LaserLloyd)
class WindowsChildJob:
    """A job object whose members die when this process does.

    ``JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE`` terminates every process still in
    the job the moment its **last handle** closes, and the handle closes when
    this process ends however it ends (Task Manager, crash, ``pythonw`` killed
    at logoff). The ``atexit`` net alone does not cover those cases, and an
    orphaned ``ovms.exe`` keeps the NPU and ~2 GB of shared memory.

    The job is anonymous so two app instances never share one. Nested jobs are
    legal on Windows 8+; where nesting is refused anyway the failure is logged
    and the load continues unprotected -- a safety net must never be the reason
    a model will not load.
    """

    def __init__(self) -> None:
        win32job, _ = _load_win32()
        self._win32job = win32job
        self._handle: Any = win32job.CreateJobObject(None, "")
        info = win32job.QueryInformationJobObject(
            self._handle, win32job.JobObjectExtendedLimitInformation
        )
        info["BasicLimitInformation"]["LimitFlags"] |= win32job.JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
        win32job.SetInformationJobObject(
            self._handle, win32job.JobObjectExtendedLimitInformation, info
        )
        self._closed = False
        self._warned_pids: set[int] = set()

    @property
    def available(self) -> bool:
        return not self._closed and self._handle is not None

    def assign(self, pid: int) -> bool:
        """Put ``pid`` in the job. Never raises; ``False`` means "unprotected"."""
        if not self.available:
            return False
        try:
            win32job, win32api = self._win32job, _load_win32()[1]
            handle = win32api.OpenProcess(_PROCESS_SET_QUOTA | _PROCESS_TERMINATE, False, pid)
            try:
                win32job.AssignProcessToJobObject(self._handle, handle)
            finally:
                with contextlib.suppress(Exception):
                    handle.Close()
        except Exception as exc:  # noqa: BLE001 - the net must not break the load
            if pid not in self._warned_pids:
                self._warned_pids.add(pid)
                log.warning(
                    "child_job_assign_failed",
                    pid=pid,
                    error=str(exc),
                    detail="ovms.exe is not protected by a job object; a hard kill of the "
                    "app would leave it running",
                )
            return False
        return True

    def close(self) -> None:
        """Close the job handle, killing anything still in it. Idempotent."""
        if self._closed:
            return
        self._closed = True
        with contextlib.suppress(Exception):
            self._handle.Close()
        self._handle = None


# Adapted from StudioForge src/studioforge/core/supervisor.py create_child_job (MIT, LaserLloyd)
def create_child_job() -> WindowsChildJob | None:
    """A job object for this process's children, or ``None`` where impossible."""
    if os.name != "nt":
        return None
    try:
        return WindowsChildJob()
    except Exception as exc:  # noqa: BLE001 - degrade, never refuse to serve
        log.warning(
            "child_job_unavailable",
            error=str(exc),
            detail="ovms.exe will not be killed automatically if the app is hard-killed",
        )
        return None


def resume_process(pid: int) -> None:
    """Resume a child created with ``CREATE_SUSPENDED``. Raises on failure."""
    psutil.Process(pid).resume()


# ---------------------------------------------------------------------------
# Process identity and teardown
# ---------------------------------------------------------------------------


# Adapted from StudioForge src/studioforge/core/supervisor.py process_create_time (MIT, LaserLloyd)
def process_create_time(pid: int) -> float | None:
    """Creation timestamp of ``pid``, or ``None`` if it cannot be read.

    Captured at spawn so a later liveness check cannot be fooled by pid reuse.
    """
    try:
        return float(psutil.Process(pid).create_time())
    except (psutil.Error, ValueError):  # pragma: no cover - race with exit
        return None


# Adapted from StudioForge src/studioforge/core/supervisor.py process_is_alive (MIT, LaserLloyd)
def process_is_alive(pid: int, *, create_time: float | None = None) -> bool:
    """Whether ``pid`` is a live (non-zombie) process, honouring ``create_time``."""
    try:
        proc = psutil.Process(pid)
        if proc.status() == psutil.STATUS_ZOMBIE:
            return False
        if create_time is not None and abs(proc.create_time() - create_time) > 1.0:
            # More than a second apart means the pid was recycled.
            return False
        return bool(proc.is_running())
    except (psutil.Error, ValueError):
        return False


#: Windows NTSTATUS values a crashing child actually exits with, by name.
WINDOWS_EXIT_STATUS: dict[int, str] = {
    0xC0000005: "STATUS_ACCESS_VIOLATION",
    0xC000001D: "STATUS_ILLEGAL_INSTRUCTION",
    0xC0000094: "STATUS_INTEGER_DIVIDE_BY_ZERO",
    0xC00000FD: "STATUS_STACK_OVERFLOW",
    0xC0000135: "STATUS_DLL_NOT_FOUND",
    0xC000013A: "STATUS_CONTROL_C_EXIT",
    0xC0000142: "STATUS_DLL_INIT_FAILED",
    0xC0000374: "STATUS_HEAP_CORRUPTION",
    0xC0000409: "STATUS_STACK_BUFFER_OVERRUN",
}


# Adapted from StudioForge src/studioforge/core/supervisor.py describe_exit_code (MIT, LaserLloyd)
def describe_exit_code(code: int | None) -> dict[str, Any]:
    """Log fields for an exit code, readable on both platforms.

    ``None`` is said out loud (``exit_code_unavailable: true``). A negative code
    is a POSIX signal; a code at or above ``0xC0000000`` is a Windows NTSTATUS
    (``exit_code_hex`` and, when known, ``exit_status``).
    """
    if code is None:
        return {"exit_code": None, "exit_code_unavailable": True}
    fields: dict[str, Any] = {"exit_code": code}
    if code < 0:
        import signal

        with contextlib.suppress(ValueError):
            fields["signal"] = signal.Signals(-code).name
    elif code >= 0xC0000000:
        fields["exit_code_hex"] = f"0x{code & 0xFFFFFFFF:08X}"
        name = WINDOWS_EXIT_STATUS.get(code & 0xFFFFFFFF)
        if name is not None:
            fields["exit_status"] = name
    return fields


# Adapted from StudioForge src/studioforge/core/supervisor.py kill_process_tree (MIT, LaserLloyd)
def kill_process_tree(pid: int, *, timeout: float = 15.0, force: bool = False) -> None:
    """Terminate ``pid`` and every descendant, escalating to a hard kill.

    Killing only the direct child is not enough: any survivor keeps its NPU
    context. So the whole tree is enumerated with psutil, signalled, waited on,
    and whatever is left is hard-killed. (On Windows ``terminate`` is already
    ``TerminateProcess``.)
    """
    try:
        parent = psutil.Process(pid)
    except (psutil.NoSuchProcess, ValueError):
        return
    try:
        procs: list[psutil.Process] = parent.children(recursive=True)
    except psutil.Error:
        procs = []
    procs.append(parent)

    for proc in procs:
        with contextlib.suppress(psutil.Error):
            if force:
                proc.kill()
            else:
                proc.terminate()
    _, alive = psutil.wait_procs(procs, timeout=max(0.0, timeout))
    for proc in alive:
        with contextlib.suppress(psutil.Error):
            proc.kill()
    if alive:
        psutil.wait_procs(alive, timeout=5.0)


def find_processes(name: str = "ovms.exe") -> list[int]:
    """Pids of every running process called ``name`` (case-insensitive). Never raises."""
    wanted = name.lower()
    pids: list[int] = []
    for proc in psutil.process_iter(["name"]):
        with contextlib.suppress(psutil.Error):
            if (proc.info.get("name") or "").lower() == wanted:
                pids.append(proc.pid)
    return pids
