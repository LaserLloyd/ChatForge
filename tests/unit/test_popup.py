"""Popup: close handling (user vs. sign-out/exit), the shown gate, lock discipline, and the
no-focus show when a reply finishes.

pywebview and WinForms are replaced by small fakes that keep the parts that matter: the
``closing`` event's veto, ``FormClosing`` subscribers running in order, and ``show``/
``hide`` blocking on the GUI thread.
"""

from __future__ import annotations

import threading
from types import SimpleNamespace
from typing import Any

import pytest

from chatforge.desktop import popup as popup_mod
from chatforge.desktop import win32util
from chatforge.desktop.popup import Popup


class FakeEvent:
    """pywebview ``Event``: ``+=`` handlers, plus the ``set``/``wait``/``is_set`` flag."""

    def __init__(self) -> None:
        self.handlers: list[Any] = []
        self._flag = threading.Event()

    def __iadd__(self, fn: Any) -> FakeEvent:
        self.handlers.append(fn)
        return self

    def fire(self, *args: Any) -> list[Any]:
        return [fn(*args) for fn in self.handlers]

    def set(self) -> None:
        self._flag.set()

    def clear(self) -> None:
        self._flag.clear()

    def is_set(self) -> bool:
        return self._flag.is_set()

    def wait(self, timeout: float | None = None) -> bool:
        return self._flag.wait(timeout)


class FakeDotNetEvent:
    def __init__(self) -> None:
        self.handlers: list[Any] = []

    def __iadd__(self, fn: Any) -> FakeDotNetEvent:
        self.handlers.append(fn)
        return self


class FakeForm:
    def __init__(self) -> None:
        self.FormClosing = FakeDotNetEvent()
        self.Activated = FakeDotNetEvent()
        self.Handle = 4242
        self.hidden = 0

    def Hide(self) -> None:  # noqa: N802 - WinForms name
        self.hidden += 1


class FakeArgs:
    def __init__(self, reason: str) -> None:
        self.CloseReason = reason
        self.Cancel = False


class FakeWindow:
    def __init__(self) -> None:
        self.events = SimpleNamespace(
            closing=FakeEvent(), closed=FakeEvent(), before_show=FakeEvent(), shown=FakeEvent()
        )
        self.native: FakeForm | None = None
        self.calls: list[str] = []
        self.destroyed = False

    def show(self) -> None:
        self.calls.append("show")

    def hide(self) -> None:
        self.calls.append("hide")

    def destroy(self) -> None:
        self.destroyed = True

    def run_js(self, _script: str) -> None:
        pass


def create_native(window: FakeWindow) -> FakeForm:
    """What pywebview's ``create()`` does on the GUI thread: build the form (which
    subscribes BrowserForm.on_closing first), then fire ``before_show``."""
    form = FakeForm()
    window.native = form

    def browser_form_on_closing(_sender: Any, args: FakeArgs) -> None:
        if any(r is False for r in window.events.closing.fire()):
            args.Cancel = True

    form.FormClosing += browser_form_on_closing
    window.events.before_show.fire()
    return form


def close(window: FakeWindow, reason: str) -> bool:
    """Raise ``FormClosing``; returns whether the close was cancelled."""
    args = FakeArgs(reason)
    for handler in window.native.FormClosing.handlers:
        handler(window.native, args)
    return args.Cancel


@pytest.fixture(autouse=True)
def no_win32(monkeypatch):
    # Placement and foregrounding would touch real windows with the fake HWND.
    monkeypatch.setattr(win32util, "IS_WINDOWS", False)


@pytest.fixture
def made(monkeypatch) -> tuple[Popup, FakeWindow]:
    import webview

    window = FakeWindow()
    monkeypatch.setattr(webview, "create_window", lambda *a, **k: window)
    cfg = SimpleNamespace(ui=SimpleNamespace(width=420, height=640, margin=12, hide_on_blur=True))
    clock = SimpleNamespace(now=100.0)
    popup = Popup(
        url="http://127.0.0.1/index.html",
        api=None,
        config=lambda: cfg,
        events=None,
        clock=lambda: clock.now,
    )
    popup.create()
    return popup, window


# --- closing --------------------------------------------------------------------------


def test_form_closing_handler_is_added_after_pywebviews(made):
    popup, window = made
    form = create_native(window)
    assert len(form.FormClosing.handlers) == 2
    assert form.FormClosing.handlers[1] == popup._on_form_closing
    assert form.Activated.handlers == [popup._on_activated]


def test_before_show_without_native_is_harmless(made):
    popup, window = made
    window.events.before_show.fire()  # window.native is None
    assert window.native is None


def test_user_close_hides_instead_of_closing(made):
    popup, window = made
    form = create_native(window)
    window.events.shown.set()
    assert close(window, "UserClosing") is True
    assert form.hidden == 1
    assert popup._last_hide_reason == "close" and popup._destroying is False


@pytest.mark.parametrize("reason", ["ApplicationExitCall", "TaskManagerClosing", "None"])
def test_exit_and_task_manager_closes_go_through(made, reason):
    popup, window = made
    create_native(window)
    assert close(window, reason) is False
    assert popup._destroying is True
    # Anything after that (the second form, pywebview's own Close) closes as well.
    assert close(window, "UserClosing") is False


def test_sign_out_is_never_vetoed_and_a_cancelled_one_keeps_alt_f4(made):
    popup, window = made
    form = create_native(window)
    assert close(window, "WindowsShutDown") is False  # WM_QUERYENDSESSION answered "yes"
    # Another app cancelled the sign-out: the popup is still here and Alt+F4 still hides.
    assert popup._destroying is False
    assert close(window, "UserClosing") is True
    assert form.hidden == 2


def test_destroy_closes_for_real(made):
    popup, window = made
    create_native(window)
    popup.destroy()
    assert window.destroyed
    assert close(window, "UserClosing") is False


def test_close_handler_errors_never_escape_into_winforms(made):
    popup, window = made

    class Broken:
        @property
        def CloseReason(self) -> str:  # noqa: N802
            raise RuntimeError("boom")

    popup._on_form_closing(None, Broken())  # logged, not raised


def test_real_dotnet_close_reasons_match():
    clr = pytest.importorskip("clr")
    try:
        clr.AddReference("System.Windows.Forms")
        from System.Windows.Forms import CloseReason, FormClosingEventArgs
    except Exception as exc:  # noqa: BLE001
        pytest.skip(f"WinForms unavailable: {exc}")
    assert str(CloseReason.UserClosing) == popup_mod.USER_CLOSING
    assert str(CloseReason.WindowsShutDown) == popup_mod.WINDOWS_SHUTDOWN
    popup = Popup(url="x", api=None, config=lambda: None, events=None)
    args = FormClosingEventArgs(CloseReason.WindowsShutDown, True)
    popup._on_form_closing(None, args)
    assert args.Cancel is False
    args = FormClosingEventArgs(CloseReason.UserClosing, True)
    popup._on_form_closing(None, args)
    assert args.Cancel is True


# --- the shown gate ---------------------------------------------------------------------


def test_hwnd_is_none_until_the_window_was_shown(made):
    popup, window = made
    create_native(window)
    assert popup.hwnd is None
    window.events.shown.set()
    assert popup.hwnd == 4242


def test_show_waits_for_the_window_to_exist(made):
    popup, window = made
    worker = threading.Thread(target=popup.show, args=("cli",))
    worker.start()
    worker.join(0.2)
    assert worker.is_alive() and window.calls == []  # still waiting for "shown"
    create_native(window)
    window.events.shown.set()
    worker.join(5)
    assert not worker.is_alive()
    assert window.calls == ["show"] and popup.visible


def test_show_gives_up_when_the_window_never_appears(made, monkeypatch):
    popup, window = made
    monkeypatch.setattr(popup_mod, "SHOWN_WAIT_S", 0.05)
    popup.show(source="cli")
    assert window.calls == [] and not popup.visible


def test_hide_before_the_window_exists_does_not_wait(made):
    popup, window = made
    popup.hide(reason="tray")
    assert window.calls == []


# --- lock discipline ---------------------------------------------------------------------


def test_close_during_show_does_not_deadlock(made):
    """window.show() blocks until the GUI thread runs it; if the GUI thread is meanwhile
    handling a user close, _on_closing must not wait for a lock show() holds."""
    popup, window = made
    create_native(window)
    window.events.shown.set()
    gui_finished: list[bool] = []

    def blocking_show() -> None:
        done = threading.Event()

        def gui() -> None:
            close(window, "UserClosing")
            done.set()

        threading.Thread(target=gui, daemon=True).start()
        gui_finished.append(done.wait(2))

    window.show = blocking_show
    worker = threading.Thread(target=popup.show, args=("hotkey",), daemon=True)
    worker.start()
    worker.join(10)
    assert not worker.is_alive()
    assert gui_finished == [True]


def test_window_calls_happen_outside_the_lock(made):
    popup, window = made
    create_native(window)
    window.events.shown.set()
    free: list[bool] = []

    def probe() -> None:
        # Another thread (the GUI thread, in real life) must be able to take the lock.
        def take() -> None:
            got = popup._lock.acquire(timeout=1)
            if got:
                popup._lock.release()
            free.append(got)

        t = threading.Thread(target=take)
        t.start()
        t.join()

    window.show = probe
    window.hide = probe
    popup.show(source="tray")
    popup.hide(reason="tray")
    assert free == [True, True]


# --- toggle -------------------------------------------------------------------------------


def test_tray_click_right_after_a_page_hide_keeps_it_hidden(made):
    popup, window = made
    create_native(window)
    window.events.shown.set()
    popup.show(source="tray")
    popup.hide_from_js()
    assert popup.toggle(source="tray") is False
    assert window.calls == ["show", "hide"]
    assert popup.toggle(source="hotkey") is True
    assert popup.toggle(source="hotkey") is False
    assert window.calls == ["show", "hide", "show", "hide"]


# --- show_inactive (ui.show_on_reply) -------------------------------------------------------


class FakeWin32:
    """The Win32 calls the popup makes, against one fake window."""

    def __init__(self) -> None:
        self.visible = False
        self.foreground = False
        self.calls: list[tuple] = []

    def show_window(self, hwnd: int, activate: bool = True) -> None:
        self.calls.append(("show_window", hwnd, activate))
        self.visible = True

    def force_foreground(self, hwnd: int) -> bool:
        self.calls.append(("force_foreground", hwnd))
        self.foreground = True
        return True

    def set_window_pos(self, hwnd: int, rect: Any, **kw: Any) -> bool:
        self.calls.append(("set_window_pos", hwnd, kw.get("activate")))
        return True


class FakeSink:
    def __init__(self) -> None:
        self.events: list[dict] = []

    def attach(self, _window: Any) -> None:
        pass

    def detach(self, _window: Any) -> None:
        pass

    def emit(self, evt: dict) -> None:
        self.events.append(evt)


@pytest.fixture
def quiet(monkeypatch):
    """A created, shown-once, currently hidden popup on a faked Win32."""
    import webview

    w32 = FakeWin32()
    monkeypatch.setattr(win32util, "IS_WINDOWS", True)
    monkeypatch.setattr(win32util, "is_window_visible", lambda _h: w32.visible)
    monkeypatch.setattr(win32util, "is_foreground", lambda _h: w32.foreground)
    monkeypatch.setattr(win32util, "show_window", w32.show_window)
    monkeypatch.setattr(win32util, "force_foreground", w32.force_foreground)
    monkeypatch.setattr(win32util, "set_window_pos", w32.set_window_pos)
    monkeypatch.setattr(win32util, "set_rounded_corners", lambda _h: True)
    monkeypatch.setattr(win32util, "dpi_for_window", lambda _h: 96)
    monkeypatch.setattr(win32util, "work_area_at_cursor", lambda: win32util.Rect(0, 0, 1920, 1040))

    window = FakeWindow()

    def show() -> None:  # pywebview: Show() + Activate()
        window.calls.append("show")
        w32.visible = True
        w32.foreground = True

    def hide() -> None:
        window.calls.append("hide")
        w32.visible = False
        w32.foreground = False

    window.show = show
    window.hide = hide
    monkeypatch.setattr(webview, "create_window", lambda *a, **k: window)
    cfg = SimpleNamespace(ui=SimpleNamespace(width=420, height=640, margin=12, hide_on_blur=True))
    sink = FakeSink()
    shown_hook: list[bool] = []
    popup = Popup(
        url="http://127.0.0.1/index.html",
        api=None,
        config=lambda: cfg,
        events=sink,
        on_shown=lambda: shown_hook.append(True),
    )
    popup.create()
    form = create_native(window)
    window.events.shown.set()
    return SimpleNamespace(
        popup=popup, window=window, w32=w32, sink=sink, form=form, shown_hook=shown_hook
    )


def test_show_inactive_shows_without_activating(quiet):
    q = quiet
    assert q.popup.show_inactive() is True
    assert q.popup.visible
    # Placed (no activation), then SW_SHOWNA; never pywebview's show() or SetForegroundWindow.
    assert q.w32.calls == [("set_window_pos", 4242, False), ("show_window", 4242, False)]
    assert q.window.calls == []
    # Not "the user opened it": the page must not grab the focus, no autoload hook.
    assert q.sink.events == [] and q.shown_hook == []


def test_a_quietly_shown_popup_ignores_page_hides_until_activated(quiet):
    q = quiet
    q.popup.show_inactive()
    q.popup.hide_from_js()  # a stray blur
    assert q.window.calls == [] and q.popup.visible
    for handler in q.form.Activated.handlers:  # the user clicks into it
        handler(q.form, None)
    q.popup.hide_from_js()  # now a blur hides it as usual
    assert q.window.calls == ["hide"] and not q.popup.visible


def test_tray_and_app_hides_still_work_on_a_quietly_shown_popup(quiet):
    q = quiet
    q.popup.show_inactive()
    q.popup.hide(reason="app")
    assert q.window.calls == ["hide"]
    # Hidden again: the flag is gone, so a later normal show behaves as before.
    q.popup.show(source="tray")
    q.popup.hide_from_js()
    assert q.window.calls == ["hide", "show", "hide"]


def test_hotkey_on_a_quietly_shown_popup_focuses_it(quiet):
    q = quiet
    q.popup.show_inactive()
    assert q.popup.toggle(source="hotkey") is True
    assert q.window.calls == ["show"]  # shown with the focus, not hidden
    assert ("force_foreground", 4242) in q.w32.calls
    assert q.sink.events == [{"type": "popup.shown"}] and q.shown_hook == [True]
    assert q.popup.toggle(source="hotkey") is False  # now a normal toggle
    assert q.window.calls == ["show", "hide"]


def test_show_inactive_does_nothing_when_visible_quitting_or_never_shown(quiet):
    q = quiet
    q.popup.show(source="tray")
    assert q.popup.show_inactive() is False
    assert ("show_window", 4242, False) not in q.w32.calls
    q.popup.hide(reason="app")
    # Before pywebview first showed it there is no window to bring back (and no wait).
    q.window.events.shown.clear()
    assert q.popup.show_inactive() is False
    q.window.events.shown.set()
    # Quitting: nothing comes back.
    q.popup.destroy()
    assert q.popup.show_inactive() is False
    assert ("show_window", 4242, False) not in q.w32.calls


def test_show_inactive_that_ends_up_in_front_is_a_normal_show(quiet):
    q = quiet
    q.w32.foreground = True  # e.g. a hotkey show won the race and activated it
    assert q.popup.show_inactive() is True
    q.popup.hide_from_js()
    assert q.window.calls == ["hide"]


def test_show_inactive_failure_leaves_the_popup_hidable(quiet, monkeypatch):
    q = quiet

    def broken(_hwnd: int, activate: bool = True) -> None:
        raise OSError("access denied")

    monkeypatch.setattr(win32util, "show_window", broken)
    assert q.popup.show_inactive() is False
    assert q.popup._shown_inactive is False


def test_show_inactive_calls_win32_outside_the_lock(quiet, monkeypatch):
    q = quiet
    free: list[bool] = []

    def probe(_hwnd: int, activate: bool = True) -> None:
        def take() -> None:
            got = q.popup._lock.acquire(timeout=1)
            if got:
                q.popup._lock.release()
            free.append(got)

        t = threading.Thread(target=take)
        t.start()
        t.join()
        q.w32.visible = True

    monkeypatch.setattr(win32util, "show_window", probe)
    assert q.popup.show_inactive() is True
    assert free == [True]
