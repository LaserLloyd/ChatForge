"""Job object (ctypes, no pywin32), Linux parent-death signal, process-name matching."""

from __future__ import annotations

import ctypes
import os
import signal
import subprocess
import sys
import time
from pathlib import Path
from types import SimpleNamespace

import psutil
import pytest

from chatforge.runtime import jobobject as jo

SRC = Path(jo.__file__).resolve().parents[2]


# ---------------------------------------------------------------------------
# Structure layout (what kernel32 expects)
# ---------------------------------------------------------------------------


@pytest.mark.skipif(ctypes.sizeof(ctypes.c_void_p) != 8, reason="64-bit layout")
def test_structures_match_the_64_bit_win32_layout():
    basic = jo.JOBOBJECT_BASIC_LIMIT_INFORMATION
    ext = jo.JOBOBJECT_EXTENDED_LIMIT_INFORMATION
    assert ctypes.sizeof(basic) == 64
    assert ctypes.sizeof(jo.IO_COUNTERS) == 48
    assert ctypes.sizeof(ext) == 144
    offsets = {name: getattr(basic, name).offset for name, _ in basic._fields_}
    assert offsets == {
        "PerProcessUserTimeLimit": 0,
        "PerJobUserTimeLimit": 8,
        "LimitFlags": 16,
        "MinimumWorkingSetSize": 24,
        "MaximumWorkingSetSize": 32,
        "ActiveProcessLimit": 40,
        "Affinity": 48,
        "PriorityClass": 56,
        "SchedulingClass": 60,
    }
    assert ext.IoInfo.offset == 64
    assert ext.ProcessMemoryLimit.offset == 112
    assert ext.JobMemoryLimit.offset == 120
    assert ext.PeakProcessMemoryUsed.offset == 128
    assert ext.PeakJobMemoryUsed.offset == 136


def test_constants():
    assert jo.JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE == 0x2000
    assert jo.JOB_OBJECT_EXTENDED_LIMIT_INFORMATION_CLASS == 9  # JobObjectExtendedLimitInformation


def test_no_pywin32_import_left():
    text = Path(jo.__file__).read_text(encoding="utf-8")
    assert "import win32" not in text and "from win32" not in text


# ---------------------------------------------------------------------------
# WindowsChildJob against a recording fake kernel32
# ---------------------------------------------------------------------------


class FakeKernel32:
    def __init__(self, *, create=0x1234, set_ok=1, open_ok=0x5678, assign_ok=1):
        self.create, self.set_ok, self.open_ok, self.assign_ok = create, set_ok, open_ok, assign_ok
        self.calls: list[tuple] = []
        self.limit_flags = None
        self.info_size = None

    def CreateJobObjectW(self, attrs, name):  # noqa: N802 - Win32 name
        self.calls.append(("CreateJobObjectW", attrs, name))
        return self.create

    def SetInformationJobObject(self, handle, klass, info, size):  # noqa: N802
        struct = getattr(info, "_obj", info)
        self.limit_flags = struct.BasicLimitInformation.LimitFlags
        self.info_size = size
        self.calls.append(("SetInformationJobObject", handle, klass, size))
        return self.set_ok

    def OpenProcess(self, access, inherit, pid):  # noqa: N802
        self.calls.append(("OpenProcess", access, inherit, pid))
        return self.open_ok

    def AssignProcessToJobObject(self, job, process):  # noqa: N802
        self.calls.append(("AssignProcessToJobObject", job, process))
        return self.assign_ok

    def CloseHandle(self, handle):  # noqa: N802
        self.calls.append(("CloseHandle", handle))
        return 1


@pytest.fixture
def k32(monkeypatch):
    fake = FakeKernel32()
    monkeypatch.setattr(jo, "_load_kernel32", lambda: fake)
    return fake


def test_job_is_created_with_kill_on_close(k32):
    job = jo.WindowsChildJob()
    assert job.available
    assert k32.calls[0] == ("CreateJobObjectW", None, None)  # anonymous: never shared
    assert k32.calls[1][:3] == ("SetInformationJobObject", 0x1234, 9)
    assert k32.limit_flags == jo.JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
    assert k32.info_size == ctypes.sizeof(jo.JOBOBJECT_EXTENDED_LIMIT_INFORMATION)


def test_assign_opens_assigns_and_closes_the_process_handle(k32):
    job = jo.WindowsChildJob()
    assert job.assign(4321) is True
    names = [c[0] for c in k32.calls]
    assert names[-3:] == ["OpenProcess", "AssignProcessToJobObject", "CloseHandle"]
    open_call = next(c for c in k32.calls if c[0] == "OpenProcess")
    assert open_call[1] == jo._PROCESS_SET_QUOTA | jo._PROCESS_TERMINATE and open_call[3] == 4321
    assert ("AssignProcessToJobObject", 0x1234, 0x5678) in k32.calls
    assert k32.calls[-1] == ("CloseHandle", 0x5678)  # the per-process handle, not the job


def test_assign_failure_is_logged_once_and_never_raises(k32):
    k32.assign_ok = 0
    job = jo.WindowsChildJob()
    assert job.assign(7) is False
    assert job.assign(7) is False
    assert [c for c in k32.calls if c[0] == "CloseHandle"] == [("CloseHandle", 0x5678)] * 2
    k32.open_ok = 0
    assert job.assign(8) is False  # OpenProcess failed: nothing to close


def test_close_is_idempotent_and_closes_the_job_handle(k32):
    job = jo.WindowsChildJob()
    job.close()
    job.close()
    assert [c for c in k32.calls if c[0] == "CloseHandle"] == [("CloseHandle", 0x1234)]
    assert not job.available
    assert job.assign(1) is False


def test_failed_create_or_set_raises_and_does_not_leak(monkeypatch):
    fake = FakeKernel32(create=0)
    monkeypatch.setattr(jo, "_load_kernel32", lambda: fake)
    with pytest.raises(OSError):
        jo.WindowsChildJob()
    fake = FakeKernel32(set_ok=0)
    monkeypatch.setattr(jo, "_load_kernel32", lambda: fake)
    with pytest.raises(OSError):
        jo.WindowsChildJob()
    assert ("CloseHandle", 0x1234) in fake.calls  # the half-made job handle is closed


def test_create_child_job_degrades_to_none(monkeypatch):
    monkeypatch.setattr(jo.os, "name", "nt")

    def boom():
        raise OSError("nested job refused")

    monkeypatch.setattr(jo, "_load_kernel32", boom)
    assert jo.create_child_job() is None
    monkeypatch.setattr(jo, "_load_kernel32", lambda: FakeKernel32())
    assert isinstance(jo.create_child_job(), jo.WindowsChildJob)


def test_create_child_job_is_none_off_windows(monkeypatch):
    monkeypatch.setattr(jo.os, "name", "posix")
    assert jo.create_child_job() is None


@pytest.mark.skipif(os.name != "nt", reason="needs the real kernel32")
def test_real_job_object_kills_its_member_on_close():
    job = jo.create_child_job()
    assert job is not None and job.available
    child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"])
    try:
        assert job.assign(child.pid) is True
        job.close()
        assert child.wait(timeout=10) is not None
    finally:
        child.kill()


# ---------------------------------------------------------------------------
# Linux parent-death signal
# ---------------------------------------------------------------------------

_PARENT_SCRIPT = """
import subprocess, sys
sys.path.insert(0, sys.argv[1])
from chatforge.runtime.jobobject import make_pdeathsig_preexec
pre = make_pdeathsig_preexec()
assert pre is not None
child = subprocess.Popen(["sleep", "300"], start_new_session=True, preexec_fn=pre)
print(child.pid, flush=True)
sys.stdin.read()  # wait to be killed
"""


def _wait_gone(pid: int, seconds: float = 10.0) -> bool:
    end = time.monotonic() + seconds
    while time.monotonic() < end:
        try:
            if psutil.Process(pid).status() == psutil.STATUS_ZOMBIE:
                return True
        except psutil.NoSuchProcess:
            return True
        time.sleep(0.05)
    return False


@pytest.mark.skipif(not sys.platform.startswith("linux"), reason="prctl is Linux-only")
def test_pdeathsig_ends_the_child_when_the_parent_is_killed(tmp_path):
    script = tmp_path / "parent.py"
    script.write_text(_PARENT_SCRIPT, encoding="utf-8")
    parent = subprocess.Popen(
        [sys.executable, "-I", str(script), str(SRC)],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        text=True,
    )
    child_pid = None
    try:
        child_pid = int(parent.stdout.readline())
        assert psutil.pid_exists(child_pid)
        parent.send_signal(signal.SIGKILL)  # a hard kill: no atexit, no finally
        parent.wait(timeout=10)
        assert _wait_gone(child_pid), "ovms-like child outlived the SIGKILLed parent"
    finally:
        if child_pid is not None and psutil.pid_exists(child_pid):
            os.kill(child_pid, signal.SIGKILL)
        parent.kill()
        parent.wait(timeout=10)


@pytest.mark.skipif(not sys.platform.startswith("linux"), reason="prctl is Linux-only")
def test_preexec_calls_prctl_with_pdeathsig_and_sigterm(monkeypatch):
    calls: list[tuple] = []

    class FakePrctl:
        def __init__(self):
            self.argtypes = None
            self.restype = None

        def __call__(self, *args):
            calls.append(args)
            return 0

    monkeypatch.setattr(jo.ctypes, "CDLL", lambda *a, **k: SimpleNamespace(prctl=FakePrctl()))
    # Run in-process, so our "parent" is the real parent of this test process.
    pre = jo.make_pdeathsig_preexec(parent_pid=os.getppid())
    assert pre is not None
    pre()
    assert calls == [(jo.PR_SET_PDEATHSIG, int(signal.SIGTERM), 0, 0, 0)]
    assert jo.PR_SET_PDEATHSIG == 1


def test_preexec_is_none_off_linux_or_without_libc(monkeypatch):
    monkeypatch.setattr(jo.sys, "platform", "win32")
    assert jo.make_pdeathsig_preexec() is None
    monkeypatch.setattr(jo.sys, "platform", "linux")

    def no_libc(*a, **k):
        raise OSError("no libc")

    monkeypatch.setattr(jo.ctypes, "CDLL", no_libc)
    assert jo.make_pdeathsig_preexec() is None


@pytest.mark.skipif(not sys.platform.startswith("linux"), reason="prctl is Linux-only")
def test_preexec_exits_the_child_when_the_parent_is_already_gone(tmp_path):
    # parent_pid that is not our real parent: the child must refuse to live unprotected.
    pre = jo.make_pdeathsig_preexec(parent_pid=os.getpid() + 10_000_000)
    child = subprocess.Popen(["sleep", "300"], preexec_fn=pre)
    try:
        assert child.wait(timeout=10) == 1
    finally:
        child.kill()


# ---------------------------------------------------------------------------
# Process names
# ---------------------------------------------------------------------------


def test_find_processes_matches_ovms_on_both_platforms(monkeypatch):
    procs = [
        SimpleNamespace(pid=1, info={"name": "ovms.exe"}),
        SimpleNamespace(pid=2, info={"name": "ovms"}),
        SimpleNamespace(pid=3, info={"name": "OVMS.EXE"}),
        SimpleNamespace(pid=4, info={"name": "ovmsx"}),
        SimpleNamespace(pid=5, info={"name": None}),
        SimpleNamespace(pid=6, info={"name": "python"}),
    ]
    monkeypatch.setattr(psutil, "process_iter", lambda attrs=None: iter(list(procs)))
    assert jo.find_processes("ovms.exe") == [1, 2, 3]
    assert jo.find_processes("ovms") == [1, 2, 3]
    assert jo.find_processes("python") == [6]
