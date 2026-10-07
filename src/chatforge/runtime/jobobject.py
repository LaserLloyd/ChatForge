"""Kernel-enforced lifetime for the OVMS child, plus process-tree helpers.

Adapted from StudioForge src/studioforge/core/supervisor.py (MIT, LaserLloyd):
``WindowsChildJob``, ``create_child_job``, ``CREATE_SUSPENDED``, the
``_TRACKED_PIDS``/atexit net, ``kill_process_tree``, ``process_is_alive``,
``process_create_time``, ``describe_exit_code`` and ``WINDOWS_EXIT_STATUS``.

The job object talks to kernel32 through :mod:`ctypes` (no pywin32). On Linux the
equivalent kernel-enforced lifetime is ``prctl(PR_SET_PDEATHSIG, SIGTERM)`` in the
child (:func:`make_pdeathsig_preexec`); the atexit net covers a clean exit on both.
"""

from __future__ import annotations

import atexit
import contextlib
import ctypes
import os
import signal
import subprocess
import sys
from collections.abc import Callable
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
#: ``JOBOBJECTINFOCLASS.JobObjectExtendedLimitInformation``.
JOB_OBJECT_EXTENDED_LIMIT_INFORMATION_CLASS = 9
#: ``LimitFlags``: kill every process in the job when the last handle closes.
JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE = 0x00002000


class JOBOBJECT_BASIC_LIMIT_INFORMATION(ctypes.Structure):  # noqa: N801 - Win32 name
    """``JOBOBJECT_BASIC_LIMIT_INFORMATION`` (64 bytes on x64, 48 on x86)."""

    _fields_ = (
        ("PerProcessUserTimeLimit", ctypes.c_int64),  # LARGE_INTEGER
        ("PerJobUserTimeLimit", ctypes.c_int64),  # LARGE_INTEGER
        ("LimitFlags", ctypes.c_uint32),  # DWORD
        ("MinimumWorkingSetSize", ctypes.c_size_t),  # SIZE_T
        ("MaximumWorkingSetSize", ctypes.c_size_t),  # SIZE_T
        ("ActiveProcessLimit", ctypes.c_uint32),  # DWORD
        ("Affinity", ctypes.c_size_t),  # ULONG_PTR
        ("PriorityClass", ctypes.c_uint32),  # DWORD
        ("SchedulingClass", ctypes.c_uint32),  # DWORD
    )


class IO_COUNTERS(ctypes.Structure):  # noqa: N801 - Win32 name
    """``IO_COUNTERS`` (six ULONGLONGs, 48 bytes)."""

    _fields_ = (
        ("ReadOperationCount", ctypes.c_uint64),
        ("WriteOperationCount", ctypes.c_uint64),
        ("OtherOperationCount", ctypes.c_uint64),
        ("ReadTransferCount", ctypes.c_uint64),
        ("WriteTransferCount", ctypes.c_uint64),
        ("OtherTransferCount", ctypes.c_uint64),
    )


class JOBOBJECT_EXTENDED_LIMIT_INFORMATION(ctypes.Structure):  # noqa: N801 - Win32 name
    """``JOBOBJECT_EXTENDED_LIMIT_INFORMATION`` (144 bytes on x64, 112 on x86)."""

    _fields_ = (
        ("BasicLimitInformation", JOBOBJECT_BASIC_LIMIT_INFORMATION),
        ("IoInfo", IO_COUNTERS),
        ("ProcessMemoryLimit", ctypes.c_size_t),
        ("JobMemoryLimit", ctypes.c_size_t),
        ("PeakProcessMemoryUsed", ctypes.c_size_t),
        ("PeakJobMemoryUsed", ctypes.c_size_t),
    )


def _load_kernel32() -> Any:
    """``kernel32`` with the job-object prototypes declared (Windows only).

    A module-level function purely so tests can monkeypatch it to raise
    (simulating a failure) or to return a fake that records the calls.
    """
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)  # type: ignore[attr-defined]
    handle, dword, boolean = ctypes.c_void_p, ctypes.c_uint32, ctypes.c_int32
    kernel32.CreateJobObjectW.argtypes = [ctypes.c_void_p, ctypes.c_wchar_p]
    kernel32.CreateJobObjectW.restype = handle
    kernel32.SetInformationJobObject.argtypes = [handle, ctypes.c_int32, ctypes.c_void_p, dword]
    kernel32.SetInformationJobObject.restype = boolean
    kernel32.AssignProcessToJobObject.argtypes = [handle, handle]
    kernel32.AssignProcessToJobObject.restype = boolean
    kernel32.OpenProcess.argtypes = [dword, boolean, dword]
    kernel32.OpenProcess.restype = handle
    kernel32.CloseHandle.argtypes = [handle]
    kernel32.CloseHandle.restype = boolean
    return kernel32


def _last_error() -> OSError:
    """The calling thread's last Win32 error as an ``OSError`` (``WinError`` on Windows)."""
    code = getattr(ctypes, "get_last_error", lambda: 0)()  # Windows-only in ctypes
    winerror = getattr(ctypes, "WinError", None)
    return winerror(code) if winerror is not None else OSError(f"Win32 error {code}")


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

    Implemented with ``ctypes`` calls to ``CreateJobObjectW``,
    ``SetInformationJobObject``, ``AssignProcessToJobObject`` (via ``OpenProcess``)
    and ``CloseHandle``; no pywin32.
    """

    def __init__(self) -> None:
        kernel32 = _load_kernel32()
        self._k32 = kernel32
        handle = kernel32.CreateJobObjectW(None, None)
        if not handle:
            raise _last_error()
        self._handle: Any = handle
        try:
            info = JOBOBJECT_EXTENDED_LIMIT_INFORMATION()
            info.BasicLimitInformation.LimitFlags |= JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
            ok = kernel32.SetInformationJobObject(
                handle,
                JOB_OBJECT_EXTENDED_LIMIT_INFORMATION_CLASS,
                ctypes.byref(info),
                ctypes.sizeof(info),
            )
            if not ok:
                raise _last_error()
        except BaseException:
            with contextlib.suppress(Exception):
                kernel32.CloseHandle(handle)
            raise
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
            kernel32 = self._k32
            process = kernel32.OpenProcess(_PROCESS_SET_QUOTA | _PROCESS_TERMINATE, 0, pid)
            if not process:
                raise _last_error()
            try:
                if not kernel32.AssignProcessToJobObject(self._handle, process):
                    raise _last_error()
            finally:
                with contextlib.suppress(Exception):
                    kernel32.CloseHandle(process)
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
        handle, self._handle = self._handle, None
        with contextlib.suppress(Exception):
            self._k32.CloseHandle(handle)


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


# ---------------------------------------------------------------------------
# Kernel-enforced child lifetime (Linux parent-death signal)
# ---------------------------------------------------------------------------

#: ``prctl`` option: the signal the kernel sends this process when its parent dies.
PR_SET_PDEATHSIG = 1


def make_pdeathsig_preexec(
    sig: int = signal.SIGTERM, *, parent_pid: int | None = None
) -> Callable[[], None] | None:
    """A ``preexec_fn`` that arms ``prctl(PR_SET_PDEATHSIG, sig)`` in the child.

    With it a hard kill of the app (SIGKILL, a crash) also ends ovms, which the
    atexit net cannot do. ``None`` where it is unavailable (not Linux, no ``prctl``).

    The kernel delivers the signal when the *thread* that forked the child exits, so
    spawn from a thread that lives as long as the app (the asyncio loop thread does).
    If the parent already died between the fork and the ``prctl`` call the child exits
    at once instead of living on unprotected.

    Libc is bound here, in the parent; the returned function only calls ``prctl`` and
    ``getppid`` so it is safe to run between ``fork`` and ``exec``.
    """
    if not sys.platform.startswith("linux"):
        return None
    try:
        libc = ctypes.CDLL(None, use_errno=True)
        prctl = libc.prctl
        prctl.argtypes = [
            ctypes.c_int,
            ctypes.c_ulong,
            ctypes.c_ulong,
            ctypes.c_ulong,
            ctypes.c_ulong,
        ]
        prctl.restype = ctypes.c_int
    except (OSError, AttributeError):
        return None
    expected_parent = parent_pid if parent_pid is not None else os.getpid()
    signum = int(sig)

    def _arm() -> None:
        prctl(PR_SET_PDEATHSIG, signum, 0, 0, 0)
        if os.getppid() != expected_parent:
            os._exit(1)

    return _arm


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
    """Pids of every running process called ``name`` (case-insensitive). Never raises.

    ``ovms.exe`` and ``ovms`` are the same program on Windows and Linux, so either
    name matches both.
    """
    wanted = {name.lower()}
    if name.lower() in ("ovms.exe", "ovms"):
        wanted = {"ovms.exe", "ovms"}
    pids: list[int] = []
    for proc in psutil.process_iter(["name"]):
        with contextlib.suppress(psutil.Error):
            if (proc.info.get("name") or "").lower() in wanted:
                pids.append(proc.pid)
    return pids
