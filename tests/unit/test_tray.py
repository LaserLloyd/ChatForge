"""Tray menu: Load model / Unload models enabling, Restart above Quit, handler threads.

The menu is built with the real pystray classes (no icon is created), and each handler
is invoked the way pystray does it, then waited for on its own thread.
"""

from __future__ import annotations

import threading
from pathlib import Path
from typing import Any

import pytest

from aichat.desktop.tray import Tray

pystray = pytest.importorskip("pystray")


class Calls:
    """Records callback names; ``wait(name)`` blocks until that one ran."""

    def __init__(self) -> None:
        self.names: list[str] = []
        self._events: dict[str, threading.Event] = {}
        self.restart_result: bool = True

    def _event(self, name: str) -> threading.Event:
        return self._events.setdefault(name, threading.Event())

    def record(self, name: str) -> Any:
        def fn(*_args: Any) -> Any:
            self.names.append(name)
            self._event(name).set()
            return self.restart_result if name == "restart" else None

        return fn

    def wait(self, name: str, timeout: float = 2.0) -> bool:
        return self._event(name).wait(timeout)


class FakeIcon:
    HAS_NOTIFICATION = True

    def __init__(self) -> None:
        self.notes: list[str] = []
        self.menu_updates = 0
        self.notified = threading.Event()

    def notify(self, message: str, _title: str | None = None) -> None:
        self.notes.append(message)
        self.notified.set()

    def update_menu(self) -> None:
        self.menu_updates += 1


@pytest.fixture
def calls() -> Calls:
    return Calls()


@pytest.fixture
def tray(calls: Calls, tmp_path: Path) -> Tray:
    return Tray(
        on_open=calls.record("open"),
        on_settings=calls.record("settings"),
        on_load=calls.record("load"),
        on_unload=calls.record("unload"),
        on_quit=calls.record("quit"),
        on_restart=calls.record("restart"),
        logs_dir=tmp_path / "logs",
        autostart_enabled=lambda: False,
        set_autostart=lambda _want: None,
    )


def items(tray: Tray) -> dict[str, Any]:
    menu = tray._build_menu()
    return {i.text: i for i in menu.items if i is not pystray.Menu.SEPARATOR}


def labels(tray: Tray) -> list[str]:
    return [i.text for i in tray._build_menu().items if i.visible]


def test_menu_order_has_load_and_unload_together_and_restart_above_quit(tray):
    tray.update("off", "No model loaded", runtime_state="unloaded")
    names = labels(tray)
    load = names.index("Load model")
    assert names[load + 1] == "Unload models"
    assert names[-2:] == ["Restart", "Quit"]
    assert "Unload model" not in names  # the old toggle is gone


@pytest.mark.parametrize(
    ("runtime_state", "load", "unload"),
    [
        (None, True, False),  # no runtime manager
        ("unloaded", True, False),
        ("not_installed", True, False),
        ("error", True, False),
        ("starting", False, True),
        ("compiling", False, True),
        ("ready", False, True),
        ("unloading", True, False),
    ],
)
def test_load_and_unload_enabling(tray, runtime_state, load, unload):
    if runtime_state is not None:
        tray.update("off", "status", runtime_state=runtime_state)
    menu = items(tray)
    assert menu["Load model"].enabled is load
    assert menu["Unload models"].enabled is unload
    assert menu["Unload models"].visible is True  # always shown, greyed out when idle


def test_unload_models_also_works_with_a_cloud_provider_selected(tray):
    # The dot is grey ("off") for a cloud provider, but the local model is still loaded.
    tray.update("off", "Qwen ready on NPU", runtime_state="ready")
    assert items(tray)["Unload models"].enabled is True


def test_load_and_unload_run_their_callbacks_off_the_pump_thread(tray, calls):
    menu = items(tray)
    menu["Load model"](None)
    assert calls.wait("load")
    menu["Unload models"](None)
    assert calls.wait("unload")
    assert calls.names == ["load", "unload"]


def test_runtime_state_change_refreshes_the_menu(tray):
    icon = tray.icon = FakeIcon()
    tray.update("off", "No model loaded", runtime_state="unloaded")
    first = icon.menu_updates
    # Same dot and text, but the model started loading: the menu must be rebuilt.
    tray.update("off", "No model loaded", runtime_state="starting")
    assert icon.menu_updates == first + 1
    tray.update("off", "No model loaded", runtime_state="starting")
    assert icon.menu_updates == first + 1
    tray.icon = None


def test_restart_notifies_and_blocks_a_second_quit(tray, calls):
    icon = tray.icon = FakeIcon()
    items(tray)["Restart"](None)
    assert calls.wait("restart")
    assert icon.notes == ["Restarting AI Chat…"]
    items(tray)["Quit"](None)  # already on its way out
    items(tray)["Restart"](None)
    assert not calls.wait("quit", timeout=0.2)
    assert calls.names == ["restart"]
    tray.icon = None


def test_a_failed_restart_keeps_quit_working(tray, calls):
    calls.restart_result = False  # the new copy could not be started
    tray.icon = FakeIcon()
    items(tray)["Restart"](None)
    assert calls.wait("restart")
    for _ in range(100):  # the handler thread resets the flag right after the callback
        if not tray._quitting:
            break
        threading.Event().wait(0.01)
    items(tray)["Quit"](None)
    assert calls.wait("quit")
    tray.icon = None


def test_restart_is_hidden_without_a_callback(calls, tmp_path):
    tray = Tray(
        on_open=calls.record("open"),
        on_settings=calls.record("settings"),
        on_load=calls.record("load"),
        on_unload=calls.record("unload"),
        on_quit=calls.record("quit"),
        logs_dir=tmp_path,
        autostart_enabled=lambda: False,
        set_autostart=lambda _want: None,
    )
    assert "Restart" not in labels(tray)
    tray._menu_restart()
    assert tray._quitting is False
