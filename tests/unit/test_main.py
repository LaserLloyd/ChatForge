"""``python -m chatforge --after-pid PID``: the copy Restart starts waits for the old one."""

from __future__ import annotations

import subprocess
import sys
import time
from typing import Any

import psutil
import pytest

from chatforge import __main__ as cli


@pytest.fixture
def started(monkeypatch) -> list[tuple]:
    """Replace the app and the wait; records what ``main`` did, in order."""
    import chatforge.app

    log: list[tuple] = []
    monkeypatch.setattr(cli, "wait_for_exit", lambda pid: log.append(("wait", pid)) or True)
    monkeypatch.setattr(chatforge.app, "main", lambda mode: log.append(("run", mode)) or 0)
    return log


def test_after_pid_waits_then_starts_the_app(started):
    assert cli.main(["--hidden", "--after-pid", "4321"]) == 0
    assert started == [("wait", 4321), ("run", "hidden")]


def test_a_plain_start_does_not_wait(started):
    assert cli.main(["--show"]) == 0
    assert started == [("run", "show")]


def test_after_pid_must_be_a_number(started):
    with pytest.raises(SystemExit):
        cli.main(["--after-pid", "abc"])
    assert started == []


class FakeProcess:
    """``psutil.Process``: ``created`` per pid; ``wait`` per the scenario."""

    created: dict[int | None, float] = {}
    outcome: str = "exits"
    waits: list[float] = []

    def __init__(self, pid: int | None = None) -> None:
        if pid not in self.created:
            raise psutil.NoSuchProcess(pid or 0)
        self.pid = pid

    def create_time(self) -> float:
        return self.created[self.pid]

    def wait(self, timeout: float | None = None) -> Any:
        FakeProcess.waits.append(timeout)
        if self.outcome == "timeout":
            raise psutil.TimeoutExpired(timeout)
        return 0


@pytest.fixture
def fake_psutil(monkeypatch):
    FakeProcess.created = {None: 200.0, 77: 100.0}  # 77 started before us (the old copy)
    FakeProcess.outcome = "exits"
    FakeProcess.waits = []
    monkeypatch.setattr(psutil, "Process", FakeProcess)
    return FakeProcess


def test_wait_for_exit_waits_for_the_old_copy(fake_psutil):
    assert cli.wait_for_exit(77) is True
    assert fake_psutil.waits == [cli.AFTER_PID_TIMEOUT_S]


def test_wait_for_exit_gives_up_after_the_timeout(fake_psutil):
    fake_psutil.outcome = "timeout"
    assert cli.wait_for_exit(77, timeout_s=0.5) is False
    assert fake_psutil.waits == [0.5]


def test_wait_for_exit_does_not_wait_for_a_gone_or_reused_pid(fake_psutil):
    assert cli.wait_for_exit(78) is True  # no such process
    fake_psutil.created[79] = 300.0  # started after us: the pid was reused
    assert cli.wait_for_exit(79) is True
    assert cli.wait_for_exit(0) is True
    assert fake_psutil.waits == []


def test_wait_for_exit_really_waits_for_an_older_process():
    """A child process waits for an older one, as the restarted copy waits for the old."""
    old = subprocess.Popen(
        [sys.executable, "-c", "import sys; sys.stdin.read()"], stdin=subprocess.PIPE
    )
    waiter = None
    try:
        code = (
            "import sys\n"
            "from chatforge.__main__ import wait_for_exit\n"
            "print('waiting', flush=True)\n"
            "print(wait_for_exit(int(sys.argv[1]), 20), flush=True)\n"
        )
        waiter = subprocess.Popen(
            [sys.executable, "-c", code, str(old.pid)], stdout=subprocess.PIPE, text=True
        )
        assert waiter.stdout.readline().strip() == "waiting"
        time.sleep(0.3)
        assert waiter.poll() is None  # still waiting: the old process is alive
        old.stdin.close()  # the old process exits
        # Reap it now: on POSIX an exited child stays a zombie (still "running" to psutil)
        # until its parent waits for it. The real old copy is never the new one's child.
        assert old.wait(5) is not None
        out, _ = waiter.communicate(timeout=30)
        assert out.split() == ["True"]
    finally:
        for proc in (old, waiter):
            if proc is not None and proc.poll() is None:
                proc.kill()
                proc.wait(5)
