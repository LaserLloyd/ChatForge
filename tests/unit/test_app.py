"""App wiring without windows: Restart (a detached copy, then the normal quit), the
reply-finished popup hook (``ui.show_on_reply``), and the runtime state the tray gets.

No real process is started: ``subprocess.Popen`` and ``spawn_restart`` are replaced.
"""

from __future__ import annotations

import asyncio
import os
import subprocess
import sys
import threading
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from chatforge import app as app_mod
from chatforge import autostart
from chatforge.app import App
from chatforge.config import load_config
from chatforge.desktop.core_loop import CoreLoop
from chatforge.paths import Paths


@pytest.fixture
def app(tmp_path: Path) -> App:
    paths = Paths.from_home(tmp_path / "home")
    paths.ensure_dirs()
    return App(paths, load_config(paths), mode="hidden")


class FakeTray:
    def __init__(self) -> None:
        self.notes: list[str] = []
        self.updates: list[tuple] = []

    def notify(self, message: str, title: str | None = None) -> None:
        self.notes.append(message)

    def update(self, state: str, status: str, *, runtime_state: str | None = None) -> None:
        self.updates.append((state, status, runtime_state))


class FakePopup:
    def __init__(self) -> None:
        self.shown = threading.Event()
        self.threads: list[str] = []

    def show_inactive(self, source: str = "reply") -> bool:
        self.threads.append(threading.current_thread().name)
        self.shown.set()
        return True


# --- Restart ------------------------------------------------------------------------------


def test_restart_command_starts_a_hidden_copy_after_this_process():
    assert app_mod.restart_command() == [
        *autostart.launch_argv(),
        "--after-pid",
        str(os.getpid()),
    ]
    assert app_mod.restart_command(42)[-2:] == ["--after-pid", "42"]


def test_restart_command_prefers_pythonw_next_to_a_console_python(monkeypatch, tmp_path):
    # Started with python.exe (a terminal, `uv run`): the copy must still be the windowless
    # pythonw.exe, as start at login uses, or it would own a console window.
    (tmp_path / "python.exe").write_bytes(b"")
    (tmp_path / "pythonw.exe").write_bytes(b"")
    monkeypatch.setattr(sys, "executable", str(tmp_path / "python.exe"))
    assert app_mod.restart_command(7) == [
        str(tmp_path / "pythonw.exe"),
        "-P",
        "-m",
        "chatforge",
        "--hidden",
        "--after-pid",
        "7",
    ]


def test_restart_command_keeps_an_interpreter_path_with_spaces_as_one_argument(
    monkeypatch, tmp_path
):
    home = tmp_path / "My Projects" / "ChatForge" / ".venv" / "Scripts"
    home.mkdir(parents=True)
    (home / "pythonw.exe").write_bytes(b"")
    monkeypatch.setattr(sys, "executable", str(home / "python.exe"))
    argv = app_mod.restart_command(7)
    assert argv[0] == str(home / "pythonw.exe")
    assert subprocess.list2cmdline(argv).startswith(f'"{home / "pythonw.exe"}" -P -m chatforge')


def test_spawn_restart_is_detached_and_runs_in_the_app_home(monkeypatch, tmp_path):
    seen: dict[str, Any] = {}

    def fake_popen(args: list[str], **kwargs: Any) -> Any:
        seen["args"], seen["kwargs"] = args, kwargs
        return SimpleNamespace(pid=999)

    monkeypatch.setattr(app_mod.subprocess, "Popen", fake_popen)
    proc = app_mod.spawn_restart(tmp_path)
    assert proc.pid == 999
    assert seen["args"] == app_mod.restart_command()
    kw = seen["kwargs"]
    assert kw["cwd"] == str(tmp_path)
    assert kw["stdin"] is kw["stdout"] is kw["stderr"] is subprocess.DEVNULL
    assert kw["close_fds"] is True  # the single-instance socket must not be inherited
    if os.name == "nt":
        flags = subprocess.CREATE_NO_WINDOW | subprocess.CREATE_NEW_PROCESS_GROUP
        assert kw["creationflags"] == flags
        # DETACHED_PROCESS would give a venv launcher's interpreter a visible console.
        assert not kw["creationflags"] & subprocess.DETACHED_PROCESS
        assert not kw["creationflags"] & subprocess.CREATE_NEW_CONSOLE


def test_restart_starts_the_copy_then_quits(app, monkeypatch):
    order: list[str] = []
    monkeypatch.setattr(
        app_mod, "spawn_restart", lambda home: order.append(f"spawn {home}") or SimpleNamespace()
    )
    monkeypatch.setattr(app, "quit", lambda: order.append("quit"))
    assert app.restart() is True
    assert order == [f"spawn {app.paths.home}", "quit"]


def test_restart_that_cannot_start_the_copy_keeps_running(app, monkeypatch):
    def fail(_home: Path) -> Any:
        raise FileNotFoundError("pythonw.exe not found")

    quits: list[bool] = []
    monkeypatch.setattr(app_mod, "spawn_restart", fail)
    monkeypatch.setattr(app, "quit", lambda: quits.append(True))
    app.tray = FakeTray()
    assert app.restart() is False
    assert quits == []
    assert app.tray.notes == ["Could not restart ChatForge: pythonw.exe not found"]


def test_restart_while_quitting_starts_nothing(app, monkeypatch):
    spawned: list[Path] = []
    monkeypatch.setattr(app_mod, "spawn_restart", spawned.append)
    app._quitting = True
    assert app.restart() is False
    assert spawned == []


# --- ui.show_on_reply ------------------------------------------------------------------------


def test_reply_end_shows_the_popup_quietly_off_the_calling_thread(app):
    popup = app.services.popup = FakePopup()
    assert app.cfg.ui.show_on_reply is True
    app._on_reply_end("req_1")
    assert popup.shown.wait(2)
    assert popup.threads == ["chatforge-reply-show"]


def test_reply_end_respects_the_setting_and_quitting(app):
    popup = app.services.popup = FakePopup()
    app.cfg.ui.show_on_reply = False
    app._on_reply_end("req_1")
    app.cfg.ui.show_on_reply = True
    app._quitting = True
    app._on_reply_end("req_2")
    assert not popup.shown.wait(0.2)


def test_a_finished_reply_reaches_the_popup_through_the_bridge(app):
    class Engine:
        def __init__(self) -> None:
            self.outcome: dict = {"type": "chat.done"}

        async def send(self, text, request_id, emit, attachments=None):
            emit({**self.outcome, "request_id": request_id})

    s = app.services
    s.loop = CoreLoop("test-app-core").start()
    try:
        s.engine = Engine()
        popup = s.popup = FakePopup()
        s.on_reply_end = app._on_reply_end  # what _build_desktop wires
        s.engine.outcome = {"type": "chat.error", "code": "cancelled"}
        assert app.api.send_message("stop me")["ok"]
        assert not popup.shown.wait(0.3)  # a stopped reply does not bring it back
        s.engine.outcome = {"type": "chat.done"}
        assert app.api.send_message("hi")["ok"]
        assert popup.shown.wait(2)
    finally:
        s.loop.stop()


def test_build_desktop_wires_the_reply_hook_and_restart(app, monkeypatch):
    from chatforge.desktop import events, hotkey, popup, settings_window, tray

    made: dict[str, dict] = {}

    def fake(name: str) -> type:
        class Fake:
            def __init__(self, *_args: Any, **kwargs: Any) -> None:
                made[name] = kwargs

            def start(self, *_args: Any) -> Any:
                return self

            def create(self) -> None:
                return None

            def note_settings_opening(self) -> None:
                return None

        return Fake

    monkeypatch.setattr(events, "EventSink", fake("events"))
    monkeypatch.setattr(hotkey, "HotkeyThread", fake("hotkey"))
    monkeypatch.setattr(popup, "Popup", fake("popup"))
    monkeypatch.setattr(settings_window, "SettingsWindow", fake("settings"))
    monkeypatch.setattr(tray, "Tray", fake("tray"))
    app.static = SimpleNamespace(url=lambda page: f"http://127.0.0.1/{page}")
    app._build_desktop()
    assert app.services.on_reply_end == app._on_reply_end
    assert made["tray"]["on_restart"] == app.restart
    assert made["tray"]["on_quit"] == app.quit


# --- tray state -----------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("state", "runtime_state"),
    [("starting", "starting"), ("ready", "ready"), ("unloaded", "unloaded"), (None, "unloaded")],
)
def test_runtime_status_gives_the_tray_the_runtime_state(app, state, runtime_state):
    app.tray = FakeTray()
    app._on_runtime_status({"state": state, "model_id": "OpenVINO/Qwen2.5-1.5B-Instruct-int4-ov"})
    assert app.tray.updates[-1][2] == runtime_state


def test_core_loop_is_never_blocked_by_the_popup(app):
    """_on_reply_end runs on the core loop: a popup that waits on the GUI thread must not
    hold it up."""
    release = threading.Event()

    class SlowPopup(FakePopup):
        def show_inactive(self, source: str = "reply") -> bool:
            release.wait(5)
            return super().show_inactive(source)

    popup = app.services.popup = SlowPopup()
    loop = CoreLoop("test-app-core-2").start()
    try:

        async def on_loop() -> None:
            app._on_reply_end("req_9")

        loop.run(asyncio.wait_for(on_loop(), 1))  # returns at once
        release.set()
        assert popup.shown.wait(2)
    finally:
        loop.stop()
