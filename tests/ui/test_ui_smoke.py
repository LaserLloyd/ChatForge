"""Scripted launch check (PLAN §7: the UI smoke test is a manual checklist plus this).

Runs ``python -m aichat --show`` with a temporary ``AICHAT_HOME`` (so no real model is
loaded and no real config is touched), then asserts:

1. the popup window "AI Chat" appears and its rect sits inside the work area at the
   bottom right with the configured margin, and has rounded corners;
2. ``python -m aichat --settings`` opens the "AI Chat Settings" window;
3. each run quits cleanly on Ctrl+Break, leaving no ``pythonw``/``python`` process of
   ours and no ``ovms.exe``.

Windows desktop session only:  ``py -3.12 -m uv run pytest -m ui -s tests/ui``
"""

from __future__ import annotations

import os
import signal
import subprocess
import sys
import time
from pathlib import Path

import pytest

pytestmark = pytest.mark.ui

REPO = Path(__file__).resolve().parents[2]
PYTHON = REPO / ".venv" / "Scripts" / "python.exe"


def _tasklist(name: str) -> set[int]:
    out = subprocess.run(
        ["tasklist", "/FI", f"IMAGENAME eq {name}", "/FO", "CSV", "/NH"],
        capture_output=True,
        text=True,
        check=False,
    ).stdout
    pids: set[int] = set()
    for line in out.splitlines():
        parts = [p.strip('"') for p in line.split('","')]
        if len(parts) >= 2 and parts[0].lower() == name.lower():
            pids.add(int(parts[1]))
    return pids


def _wait_window(title: str, timeout: float = 40.0) -> int:
    from aichat.desktop import win32util

    deadline = time.time() + timeout
    while time.time() < deadline:
        hwnd = win32util.find_window(title)
        if hwnd and win32util.is_window_visible(hwnd):
            return hwnd
        time.sleep(0.25)
    raise AssertionError(f"window {title!r} did not appear within {timeout}s")


def _prepare_home(home: Path) -> None:
    """A config that never touches the real machine: no autostart shim on first run."""
    if not (home / "config.toml").exists():
        lines = ["schema_version = 1", "", "[startup]", "autostart = false", ""]
        (home / "config.toml").write_text("\n".join(lines), encoding="utf-8")


def _launch(mode: str, home: Path) -> subprocess.Popen[bytes]:
    _prepare_home(home)
    env = dict(os.environ, AICHAT_HOME=str(home), PYTHONUNBUFFERED="1")
    env.pop("MINIMAX_API_KEY", None)
    return subprocess.Popen(  # noqa: S603
        [str(PYTHON), "-m", "aichat", mode],
        cwd=str(REPO),
        env=env,
        creationflags=subprocess.CREATE_NEW_PROCESS_GROUP,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
    )


def _owned_by(hwnd: int, proc: subprocess.Popen[bytes], before: set[int]) -> bool:
    """The venv ``python.exe`` is a launcher whose child owns the windows."""
    from aichat.desktop import win32util

    pid = win32util.window_pid(hwnd)
    return pid == proc.pid or pid in (_tasklist("python.exe") - before)


def _quit(proc: subprocess.Popen[bytes], timeout: float = 30.0) -> str:
    proc.send_signal(signal.CTRL_BREAK_EVENT)
    try:
        out, _ = proc.communicate(timeout=timeout)
    except subprocess.TimeoutExpired:
        proc.kill()
        out, _ = proc.communicate()
        raise AssertionError("the app did not quit on Ctrl+Break") from None
    return out.decode("utf-8", errors="replace")


@pytest.fixture(scope="module")
def home(tmp_path_factory: pytest.TempPathFactory) -> Path:
    return tmp_path_factory.mktemp("aichat-home")


@pytest.fixture(autouse=True)
def _windows_only() -> None:
    if sys.platform != "win32":
        pytest.skip("Windows desktop only")
    if not PYTHON.is_file():
        pytest.skip(f"{PYTHON} is missing")
    # The checks below read window rects in physical pixels; a DPI-unaware test process
    # would get virtualised (scaled) coordinates and a mismatched DPI.
    from aichat.desktop import win32util

    win32util.ensure_dpi_awareness()


def test_show_places_popup_and_quits_cleanly(home: Path) -> None:
    from aichat.desktop import win32util

    before_pyw = _tasklist("pythonw.exe")
    before_py = _tasklist("python.exe")
    proc = _launch("--show", home)
    try:
        hwnd = _wait_window("AI Chat")
        assert _owned_by(hwnd, proc, before_py)
        window_pid = win32util.window_pid(hwnd)
        time.sleep(1.0)  # let placement and the page settle

        rect = win32util.window_rect(hwnd)
        _mon, work = win32util.monitor_info_at((rect.left + 5, rect.top + 5))
        dpi = win32util.dpi_for_window(hwnd)
        expected = win32util.place(work, dpi, 420, 620, 12)
        assert work.contains(rect), (rect, work)
        assert rect == expected, (rect, expected)
        assert win32util.get_corner_preference(hwnd) == win32util.DWMWCP_ROUND
        assert win32util.is_window_visible(hwnd)

        assert (home / "config.toml").is_file()
        assert (home / "logs" / "aichat.log").is_file()
    finally:
        out = _quit(proc)

    assert proc.returncode == 0, out
    assert win32util.find_window("AI Chat") is None
    assert _tasklist("ovms.exe") == set()
    assert _tasklist("pythonw.exe") - before_pyw == set()
    left = _tasklist("python.exe")
    assert proc.pid not in left and window_pid not in left
    log = (home / "logs" / "aichat.log").read_text(encoding="utf-8", errors="replace")
    assert "quitting" in log
    assert "autostart enabled" not in log


def test_settings_opens_and_quits_cleanly(home: Path) -> None:
    from aichat.desktop import win32util

    before_py = _tasklist("python.exe")
    proc = _launch("--settings", home)
    try:
        hwnd = _wait_window("AI Chat Settings")
        assert _owned_by(hwnd, proc, before_py)
        window_pid = win32util.window_pid(hwnd)
        rect = win32util.window_rect(hwnd)
        assert rect.width > 600 and rect.height > 400
        # The popup exists but stays hidden in this mode.
        popup = win32util.find_window("AI Chat")
        assert popup is None or not win32util.is_window_visible(popup)
    finally:
        out = _quit(proc)
    assert proc.returncode == 0, out
    assert win32util.find_window("AI Chat Settings") is None
    assert _tasklist("ovms.exe") == set()
    left = _tasklist("python.exe")
    assert proc.pid not in left and window_pid not in left


def test_second_launch_signals_the_first(home: Path) -> None:
    from aichat.desktop import win32util

    before_py = _tasklist("python.exe")
    proc = _launch("--hidden", home)
    try:
        deadline = time.time() + 40
        while time.time() < deadline and win32util.find_window("AI Chat") is None:
            time.sleep(0.25)
        hidden = win32util.find_window("AI Chat")
        assert hidden is not None and not win32util.is_window_visible(hidden)
        first_pids = _tasklist("python.exe") - before_py
        second = _launch("--show", home)
        assert second.wait(timeout=30) == 0
        hwnd = _wait_window("AI Chat", timeout=10)  # the SHOW signal made it visible
        assert win32util.window_pid(hwnd) in first_pids | {proc.pid}
    finally:
        out = _quit(proc)
    assert proc.returncode == 0, out
    assert _tasklist("python.exe") & first_pids == set()
