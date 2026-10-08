"""The Linux (GTK) shell: xutil, the popup's Linux branch, the settings window raise, the tray
backend choice, the app's Linux seams.

No display exists here. A ``Gtk.Window`` is replaced by :class:`FakeGtk` (the methods the
code calls, recording them), pywebview's window by :class:`FakeWindow`, GLib by nothing (so
``call_on_gtk`` runs directly) or by :class:`FakeGLib` where the main-loop hop is the point.
"""

from __future__ import annotations

import subprocess
import sys
import threading
import types
from types import SimpleNamespace
from typing import Any

import pytest

from chatforge import app as app_mod
from chatforge.desktop import icon, settings_window, tray, win32util, xutil
from chatforge.desktop import popup as popup_mod
from chatforge.desktop.popup import Popup
from chatforge.desktop.win32util import Rect

POSIX_ONLY = pytest.mark.skipif(
    sys.platform == "win32",
    reason="exercises the Linux branch with POSIX path and HOME semantics; covered by the Ubuntu job",
)

WORK = Rect(0, 0, 1920, 1040)
CORNER = Rect(1488, 408, 1908, 1028)  # 420 x 620 at 100 %, lower right, margin 12
PLACED = Rect(1488, 388, 1908, 1028)  # the configured 420 x 640 popup in the lower right


@pytest.fixture
def linux(monkeypatch):
    monkeypatch.setattr(xutil, "IS_LINUX", True)
    monkeypatch.setattr(win32util, "IS_WINDOWS", False)
    monkeypatch.setattr(xutil, "work_area", lambda: WORK)


# --- xutil -----------------------------------------------------------------------------


def test_prefer_x11_only_for_wayland_with_xwayland(monkeypatch):
    monkeypatch.setattr(xutil, "IS_LINUX", True)
    env = {"WAYLAND_DISPLAY": "wayland-0", "DISPLAY": ":0"}
    assert xutil.prefer_x11(env) is True
    assert env["GDK_BACKEND"] == "x11" and env["QT_QPA_PLATFORM"] == "xcb"
    for plain in ({"DISPLAY": ":0"}, {"WAYLAND_DISPLAY": "wayland-0"}, {}):
        assert xutil.prefer_x11(plain) is False and "GDK_BACKEND" not in plain


def test_prefer_x11_keeps_what_the_user_chose(monkeypatch):
    monkeypatch.setattr(xutil, "IS_LINUX", True)
    env = {
        "WAYLAND_DISPLAY": "w",
        "DISPLAY": ":0",
        "GDK_BACKEND": "wayland",
        "QT_QPA_PLATFORM": "wayland",
    }
    assert xutil.prefer_x11(env) is False
    assert env["GDK_BACKEND"] == "wayland" and env["QT_QPA_PLATFORM"] == "wayland"


def test_prefer_x11_is_a_noop_off_linux(monkeypatch):
    monkeypatch.setattr(xutil, "IS_LINUX", False)
    env = {"WAYLAND_DISPLAY": "w", "DISPLAY": ":0"}
    assert xutil.prefer_x11(env) is False and "GDK_BACKEND" not in env


def test_session_kind():
    assert xutil.session_kind({"WAYLAND_DISPLAY": "w", "DISPLAY": ":0"}) == "wayland"
    assert xutil.session_kind({"DISPLAY": ":0"}) == "x11"
    assert xutil.session_kind({}) == "none"


class FakeGLib:
    """``GLib.idle_add`` / ``timeout_add`` that run the callback on a thread of their own,
    like the GTK main loop running it away from the caller."""

    def __init__(self) -> None:
        self.timeouts: list[tuple[int, Any]] = []
        self.threads: list[str] = []

    def idle_add(self, fn: Any) -> None:
        def run() -> None:
            self.threads.append(threading.current_thread().name)
            fn()

        threading.Thread(target=run, name="gtk-main").start()

    def timeout_add(self, ms: int, fn: Any) -> None:
        self.timeouts.append((ms, fn))


@pytest.fixture
def glib(monkeypatch) -> FakeGLib:
    fake = FakeGLib()
    monkeypatch.setitem(sys.modules, "gi.repository", SimpleNamespace(GLib=fake))
    return fake


def test_call_on_gtk_is_direct_without_glib_or_on_the_main_thread(glib):
    assert xutil.call_on_gtk(lambda x: x + 1, 1) == 2  # main thread: direct
    assert glib.threads == []


def test_call_on_gtk_hops_to_the_main_loop_from_a_worker(glib):
    out: list[Any] = []
    worker = threading.Thread(target=lambda: out.append(xutil.call_on_gtk(lambda: "done")))
    worker.start()
    worker.join(3)
    assert out == ["done"] and glib.threads == ["gtk-main"]


def test_call_on_gtk_swallows_errors_and_times_out(glib):
    def boom() -> None:
        raise RuntimeError("gtk")

    out: list[Any] = []

    def work() -> None:
        out.append(xutil.call_on_gtk(boom))
        glib.idle_add = lambda fn: None  # a loop that never answers
        out.append(xutil.call_on_gtk(lambda: 1, timeout=0.05))

    worker = threading.Thread(target=work)
    worker.start()
    worker.join(3)
    assert out == [None, None]


class FakeGtk:
    """The ``Gtk.Window`` methods the shell uses, recorded in order in ``calls``."""

    def __init__(self, log: list | None = None) -> None:
        self.calls: list[Any] = log if log is not None else []
        self.pos = (1488, 408)
        self.size = (420, 620)
        self.handlers: dict[str, Any] = {}

    def present(self) -> None:
        self.calls.append("present")

    def hide(self) -> None:
        self.calls.append("hide")

    def move(self, x: int, y: int) -> None:
        self.pos = (x, y)
        self.calls.append(("move", x, y))

    def resize(self, w: int, h: int) -> None:
        self.size = (w, h)
        self.calls.append(("resize", w, h))

    def get_position(self) -> tuple[int, int]:
        return self.pos

    def get_size(self) -> tuple[int, int]:
        return self.size

    def set_accept_focus(self, value: bool) -> None:
        self.calls.append(("accept_focus", value))

    def set_focus_on_map(self, value: bool) -> None:
        self.calls.append(("focus_on_map", value))

    def set_skip_taskbar_hint(self, value: bool) -> None:
        self.calls.append(("skip_taskbar", value))

    def set_skip_pager_hint(self, value: bool) -> None:
        self.calls.append(("skip_pager", value))

    def connect(self, name: str, fn: Any) -> None:
        self.handlers[name] = fn


def test_is_gtk_window_needs_the_gtk_methods():
    assert xutil.is_gtk_window(FakeGtk())
    assert not xutil.is_gtk_window(None)
    assert not xutil.is_gtk_window(SimpleNamespace(Hide=lambda: None))  # a WinForms form


def test_native_helpers_ignore_a_native_that_is_not_gtk():
    form = SimpleNamespace(Hide=lambda: None)
    assert xutil.skip_taskbar(form) is False
    assert xutil.present(form) is False
    assert xutil.prepare_no_focus(form) is False
    xutil.restore_focus(form)  # no error
    assert xutil.hide_native(form) is False
    assert xutil.window_rect(SimpleNamespace(native=form)) is None
    assert xutil.connect_focus_in(form, lambda: None) is False


def test_present_and_no_focus_sequence():
    g = FakeGtk()
    assert xutil.prepare_no_focus(g) and xutil.present(g)
    xutil.restore_focus(g)  # no GLib loaded: applied at once
    assert g.calls == [
        ("accept_focus", False),
        ("focus_on_map", False),
        ("focus_on_map", True),
        ("accept_focus", True),
        "present",
        ("accept_focus", True),
        ("focus_on_map", True),
    ]


def test_restore_focus_is_deferred_when_the_main_loop_runs(glib):
    g = FakeGtk()
    xutil.restore_focus(g, 250)
    assert g.calls == [] and glib.timeouts[0][0] == 250
    glib.timeouts[0][1]()
    assert g.calls == [("accept_focus", True), ("focus_on_map", True)]


def test_focus_in_event_calls_the_handler_and_lets_the_event_through():
    g = FakeGtk()
    seen: list[int] = []
    assert xutil.connect_focus_in(g, lambda: seen.append(1))
    assert g.handlers["focus-in-event"](g, object()) is False and seen == [1]


def test_move_resize_uses_the_native_window_then_pywebview():
    g = FakeGtk()
    window = SimpleNamespace(native=g)
    assert xutil.move_resize(window, CORNER)
    assert g.calls == [("resize", 420, 620), ("move", 1488, 408)]
    seen: list[Any] = []
    fallback = SimpleNamespace(
        native=None,
        resize=lambda w, h: seen.append(("resize", w, h)),
        move=lambda x, y: seen.append(("move", x, y)),
    )
    assert xutil.move_resize(fallback, CORNER)
    assert seen == [("resize", 420, 620), ("move", 1488, 408)]

    def broken(*_a: Any) -> None:
        raise RuntimeError("closed")

    assert (
        xutil.move_resize(SimpleNamespace(native=None, resize=broken, move=broken), CORNER) is False
    )


def test_window_rect_reads_position_and_size():
    assert xutil.window_rect(SimpleNamespace(native=FakeGtk())) == CORNER


def test_work_area_prefers_gdk_then_pywebview_then_a_default(monkeypatch):
    monkeypatch.setattr(xutil, "_gdk_work_area", lambda: Rect(0, 27, 1920, 1080))
    assert xutil.work_area() == Rect(0, 27, 1920, 1080)

    def no_gdk() -> Rect:
        raise ImportError("gi")

    monkeypatch.setattr(xutil, "_gdk_work_area", no_gdk)
    import webview

    monkeypatch.setattr(webview, "screens", [SimpleNamespace(x=0, y=0, width=2560, height=1440)])
    assert xutil.work_area() == Rect(0, 0, 2560, 1440)
    monkeypatch.setattr(webview, "screens", [])
    assert xutil.work_area() == Rect(0, 0, *xutil.DEFAULT_SCREEN)
    monkeypatch.setattr(xutil, "_gdk_work_area", lambda: Rect(0, 0, 0, 0))  # nonsense
    assert xutil.work_area() == Rect(0, 0, *xutil.DEFAULT_SCREEN)


def test_pointer_reads_root_position_and_primary_button(monkeypatch):
    class FakeRoot:
        def query_pointer(self) -> Any:
            return SimpleNamespace(root_x=200, root_y=100, mask=xutil.BUTTON1_MASK | 0x1)

    class FakeDisplay:
        closed = False

        def screen(self) -> Any:
            return SimpleNamespace(root=FakeRoot())

        def close(self) -> None:
            FakeDisplay.closed = True

    monkeypatch.setitem(
        sys.modules, "Xlib", SimpleNamespace(display=SimpleNamespace(Display=FakeDisplay))
    )
    monkeypatch.setitem(sys.modules, "Xlib.display", SimpleNamespace(Display=FakeDisplay))
    monkeypatch.setenv("DISPLAY", ":0")
    monkeypatch.setattr(xutil, "gdk_scale", lambda: 2)  # GDK_SCALE=2: X pixels are doubled
    pointer = xutil.open_pointer()
    assert pointer is not None and pointer.read() == ((100, 50), True)
    pointer.close()
    assert FakeDisplay.closed


def test_open_pointer_is_none_without_display_or_xlib(monkeypatch):
    monkeypatch.delenv("DISPLAY", raising=False)
    assert xutil.open_pointer() is None
    monkeypatch.setenv("DISPLAY", ":0")
    monkeypatch.setitem(sys.modules, "Xlib", None)  # import fails
    monkeypatch.setitem(sys.modules, "Xlib.display", None)
    assert xutil.open_pointer() is None


def test_unix_signals_and_program_name_use_glib(monkeypatch):
    added: list[int] = []
    names: list[str] = []
    glib_mod = SimpleNamespace(
        PRIORITY_HIGH=-100,
        unix_signal_add=lambda prio, sig, fn: added.append(sig),
        set_prgname=names.append,
        set_application_name=names.append,
    )
    gi = types.ModuleType("gi")
    gi.require_version = lambda *_a: None
    gi.repository = SimpleNamespace(GLib=glib_mod)
    monkeypatch.setitem(sys.modules, "gi", gi)
    monkeypatch.setitem(sys.modules, "gi.repository", gi.repository)
    monkeypatch.setattr(xutil, "IS_LINUX", True)
    assert xutil.install_unix_signals(lambda: None, (2, 15)) is True and added == [2, 15]
    assert xutil.set_program_name("chatforge") is True and names == ["chatforge", "chatforge"]
    monkeypatch.setattr(xutil, "IS_LINUX", False)
    assert xutil.install_unix_signals(lambda: None, (2,)) is False
    assert xutil.set_program_name("x") is False


def test_unix_signals_without_pygobject_are_skipped(monkeypatch):
    monkeypatch.setattr(xutil, "IS_LINUX", True)
    monkeypatch.setitem(sys.modules, "gi", None)
    assert xutil.install_unix_signals(lambda: None, (2,)) is False
    assert xutil.set_program_name("x") is False


# --- the popup on GTK ------------------------------------------------------------------


class FakeEvent:
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

    def is_set(self) -> bool:
        return self._flag.is_set()

    def wait(self, timeout: float | None = None) -> bool:
        return self._flag.wait(timeout)


class FakeWindow:
    """pywebview's window on GTK: ``native`` is the Gtk.Window; show/hide are queued by
    pywebview (``glib.idle_add``), here they just record."""

    def __init__(self, log: list) -> None:
        self.events = SimpleNamespace(
            closing=FakeEvent(), closed=FakeEvent(), before_show=FakeEvent(), shown=FakeEvent()
        )
        self.log = log
        self.native = FakeGtk(log)
        self.destroyed = False

    def show(self) -> None:
        self.log.append("show")

    def hide(self) -> None:
        self.log.append("hide")

    def destroy(self) -> None:
        self.destroyed = True


class FakeSink:
    def __init__(self) -> None:
        self.events: list[dict] = []

    def attach(self, _w: Any) -> None:
        pass

    def detach(self, _w: Any) -> None:
        pass

    def emit(self, evt: dict) -> None:
        self.events.append(evt)


@pytest.fixture
def gtk_popup(monkeypatch, linux):
    import webview

    log: list[Any] = []
    window = FakeWindow(log)
    created: dict[str, Any] = {}

    def create_window(*a: Any, **k: Any) -> FakeWindow:
        created.update(k)
        return window

    monkeypatch.setattr(webview, "create_window", create_window)
    cfg = SimpleNamespace(ui=SimpleNamespace(width=420, height=640, margin=12, sticky=False))
    sink = FakeSink()
    hooks: list[bool] = []
    popup = Popup(
        url="http://127.0.0.1/index.html",
        api=None,
        config=lambda: cfg,
        events=sink,
        on_shown=lambda: hooks.append(True),
    )
    popup.create()
    window.events.before_show.fire()
    window.events.shown.set()
    return SimpleNamespace(
        popup=popup,
        window=window,
        gtk=window.native,
        log=log,
        sink=sink,
        created=created,
        hooks=hooks,
    )


def test_gtk_window_is_created_resizable_but_frameless(gtk_popup):
    # A non-resizable GTK window ignores resize(), and the page's handles need it.
    assert gtk_popup.created["resizable"] is True and gtk_popup.created["frameless"] is True


def test_windows_window_stays_non_resizable(monkeypatch):
    import webview

    monkeypatch.setattr(xutil, "IS_LINUX", False)
    got: dict[str, Any] = {}
    monkeypatch.setattr(webview, "create_window", lambda *a, **k: got.update(k) or FakeWindow([]))
    cfg = SimpleNamespace(ui=SimpleNamespace(width=420, height=640, margin=12, sticky=False))
    Popup(url="u", api=None, config=lambda: cfg, events=None).create()
    assert got["resizable"] is False


def test_before_show_keeps_the_popup_off_the_dock_and_watches_focus(gtk_popup):
    assert ("skip_taskbar", True) in gtk_popup.gtk.calls
    assert "focus-in-event" in gtk_popup.gtk.handlers


def test_close_hides_through_the_closing_veto(gtk_popup):
    p, w = gtk_popup.popup, gtk_popup.window
    p._visible = True
    assert w.events.closing.fire() == [False]  # pywebview's delete-event handler keeps it
    assert "hide" in gtk_popup.gtk.calls and p.visible is False
    assert w.destroyed is False


def test_destroy_lets_the_close_through(gtk_popup):
    p, w = gtk_popup.popup, gtk_popup.window
    p.destroy()
    assert w.destroyed and w.events.closing.fire() == [True]
    assert "hide" not in gtk_popup.gtk.calls


def test_close_with_a_native_that_cannot_hide_still_vetoes(gtk_popup):
    gtk_popup.window.native = SimpleNamespace()  # no hide()
    assert gtk_popup.window.events.closing.fire() == [False]


def test_target_rect_is_the_lower_right_of_the_work_area(gtk_popup):
    assert gtk_popup.popup.target_rect() == PLACED
    assert gtk_popup.popup.target_rect((320, 400)) == Rect(1588, 628, 1908, 1028)


def test_target_rect_waits_for_the_window(gtk_popup):
    gtk_popup.window.events.shown._flag.clear()
    assert gtk_popup.popup.target_rect() is None and gtk_popup.popup.place() is None


def test_show_places_shows_presents_and_announces(gtk_popup):
    gtk_popup.popup.show(source="hotkey")
    kinds = [c if isinstance(c, str) else c[0] for c in gtk_popup.log]
    assert (
        kinds.index("resize") < kinds.index("move") < kinds.index("show") < kinds.index("present")
    )
    assert ("resize", 420, 640) in gtk_popup.log and ("move", 1488, 388) in gtk_popup.log
    assert gtk_popup.popup.visible
    assert gtk_popup.sink.events == [{"type": "popup.shown"}] and gtk_popup.hooks == [True]


def test_show_survives_a_placement_failure(gtk_popup, monkeypatch):
    monkeypatch.setattr(
        xutil, "work_area", lambda: (_ for _ in ()).throw(RuntimeError("no display"))
    )
    gtk_popup.popup.show()
    assert "show" in gtk_popup.log and gtk_popup.popup.visible


def test_toggle_hides_and_shows(gtk_popup):
    assert gtk_popup.popup.toggle(source="hotkey") is True
    assert gtk_popup.popup.toggle(source="hotkey") is False
    assert gtk_popup.log.count("show") == 1 and gtk_popup.log.count("hide") == 1


def test_show_inactive_shows_without_taking_the_focus(gtk_popup):
    p = gtk_popup.popup
    assert p.show_inactive() is True and p.visible
    log = gtk_popup.log
    show = log.index("show")
    assert log.index(("accept_focus", False)) < show and log.index(("focus_on_map", False)) < show
    assert "present" not in log
    assert ("accept_focus", True) in log[show:]  # undone after the show
    # Not the user opening it: no popup.shown, no autoload hook.
    assert gtk_popup.sink.events == [] and gtk_popup.hooks == []


def test_a_quietly_shown_popup_ignores_page_hides_until_the_user_clicks_in(gtk_popup):
    p = gtk_popup.popup
    p.show_inactive()
    p.hide_from_js()
    p.hide_from_js(blur=True)
    assert "hide" not in gtk_popup.log
    gtk_popup.gtk.handlers["focus-in-event"](gtk_popup.gtk, object())  # clicked into it
    p.hide_from_js()
    assert "hide" in gtk_popup.log and not p.visible


def test_hotkey_on_a_quietly_shown_popup_focuses_it(gtk_popup):
    p = gtk_popup.popup
    p.show_inactive()
    assert p.toggle(source="hotkey") is True
    assert "present" in gtk_popup.log and p.visible


def test_show_inactive_does_nothing_when_visible_quitting_or_not_gtk(gtk_popup):
    p = gtk_popup.popup
    p.show()
    assert p.show_inactive() is False
    p.hide()
    p._destroying = True
    assert p.show_inactive() is False
    p._destroying = False
    gtk_popup.window.native = SimpleNamespace()
    assert p.show_inactive() is False


def test_show_inactive_failure_leaves_it_hidable(gtk_popup, monkeypatch):
    p = gtk_popup.popup

    def boom() -> None:
        raise RuntimeError("closed")

    monkeypatch.setattr(gtk_popup.window, "show", boom)
    assert p.show_inactive() is False
    assert p._shown_inactive is False and ("accept_focus", True) in gtk_popup.log


# resizing on GTK


def test_a_touch_resize_moves_the_top_left_and_reports_the_size(gtk_popup):
    p, g = gtk_popup.popup, gtk_popup.gtk
    assert p.begin_resize("top-left", 4, 4, follow=False) is True
    assert p.drag_resize(-200, -100) is True
    assert g.pos == (1288, 308) and g.size == (620, 720)
    assert p.end_resize() == (620, 720)
    assert p.drag_resize(-300, 0) is False


def test_edges_resize_one_way_and_never_below_the_minimum(gtk_popup):
    p, g = gtk_popup.popup, gtk_popup.gtk
    p.begin_resize("left", 2, 300, follow=False)
    p.drag_resize(-80, -500)
    assert (g.pos, g.size) == ((1408, 408), (500, 620))
    assert p.end_resize() == (500, 620)
    p.begin_resize("top", 300, 2, follow=False)
    p.drag_resize(900, 900)
    assert g.size[1] == popup_mod.MIN_SIZE[1] and g.size[0] == 500


def test_a_click_on_a_handle_changes_nothing(gtk_popup):
    p, g = gtk_popup.popup, gtk_popup.gtk
    p.begin_resize("top-left", 4, 4, follow=False)
    p.drag_resize(-2, -1)  # under the drag threshold
    assert p.end_resize() is None and g.size == (420, 620)


def test_resize_needs_a_window_and_a_known_handle(gtk_popup):
    p = gtk_popup.popup
    assert p.begin_resize("bottom", 1, 1, follow=False) is False
    gtk_popup.window.native = SimpleNamespace()  # not GTK: no rect to start from
    assert p.begin_resize("left", 1, 1, follow=False) is False
    gtk_popup.window.events.shown._flag.clear()
    assert p.begin_resize("left", 1, 1, follow=False) is False


class FakePointer:
    """Scripted ``((x, y), button_down)`` polls; the last one repeats."""

    def __init__(self, polls: list) -> None:
        self.polls = polls
        self._scale = 1
        self.closed = threading.Event()

    def read(self) -> Any:
        return self.polls.pop(0) if len(self.polls) > 1 else self.polls[0]

    def close(self) -> None:
        self.closed.set()


def test_a_mouse_resize_follows_the_pointer_until_the_button_is_up(gtk_popup, monkeypatch):
    p, g = gtk_popup.popup, gtk_popup.gtk
    monkeypatch.setattr(popup_mod, "RESIZE_POLL_S", 0.001)
    pointer = FakePointer([((1492, 412), True), ((1392, 312), True), ((1292, 312), False)])
    monkeypatch.setattr(xutil, "open_pointer", lambda: pointer)
    assert p.begin_resize("top-left", 4, 4, follow=True) is True
    assert pointer.closed.wait(3)
    assert (g.pos, g.size) == ((1288, 308), (620, 720))
    assert p.end_resize() == (620, 720)


def test_a_mouse_resize_needs_the_pointer(gtk_popup, monkeypatch):
    monkeypatch.setattr(xutil, "open_pointer", lambda: None)  # no python-xlib / DISPLAY
    assert gtk_popup.popup.begin_resize("left", 4, 4, follow=True) is False


def test_hiding_cancels_a_resize_and_reset_size_puts_the_popup_back(gtk_popup):
    p, g = gtk_popup.popup, gtk_popup.gtk
    p.show()
    p.begin_resize("left", 2, 300, follow=False)
    p.hide()
    assert p.drag_resize(-80, 0) is False
    p.show()
    assert p.reset_size(420, 640) is True and g.size == (420, 640)
    p.hide()
    assert p.reset_size(420, 640) is False  # hidden: it opens at the size anyway


def test_settings_window_is_raised_with_present(linux):
    g = FakeGtk()
    shown: list[str] = []
    window = SimpleNamespace(native=g, show=lambda: shown.append("show"))
    sw = settings_window.SettingsWindow(url="u", api=None)
    sw._raise(window)
    assert shown == ["show"] and g.calls[-1] == "present"


def test_settings_window_raise_without_a_gtk_native_is_harmless(linux):
    settings_window.SettingsWindow(url="u", api=None)._raise(
        SimpleNamespace(native=None, show=lambda: None)
    )


# --- tray backend ----------------------------------------------------------------------


def test_tray_backend_defaults_to_xorg_with_a_display(monkeypatch):
    monkeypatch.setattr(xutil, "IS_LINUX", True)
    env = {"DISPLAY": ":0"}
    assert tray.choose_backend(env) == "xorg" and env["PYSTRAY_BACKEND"] == "xorg"


def test_tray_backend_honours_the_environment(monkeypatch):
    monkeypatch.setattr(xutil, "IS_LINUX", True)
    for name in ("appindicator", "gtk", "dummy"):
        env = {"DISPLAY": ":0", "PYSTRAY_BACKEND": name}
        assert tray.choose_backend(env) == name and env["PYSTRAY_BACKEND"] == name


def test_tray_backend_is_left_to_pystray_without_a_display_or_off_linux(monkeypatch):
    monkeypatch.setattr(xutil, "IS_LINUX", True)
    env: dict[str, str] = {}
    assert tray.choose_backend(env) is None and env == {}
    monkeypatch.setattr(xutil, "IS_LINUX", False)
    env = {"DISPLAY": ":0"}
    assert tray.choose_backend(env) is None and "PYSTRAY_BACKEND" not in env


# --- icon, app -------------------------------------------------------------------------


def test_app_png_is_written_once(tmp_path):
    from PIL import Image

    path = icon.write_app_png(tmp_path / "sub" / "app.png")
    with Image.open(path) as img:
        assert img.format == "PNG" and img.size == (256, 256)
    stamp = path.stat().st_mtime_ns
    assert icon.write_app_png(path) == path and path.stat().st_mtime_ns == stamp
    assert not list(tmp_path.rglob("*.tmp"))


@POSIX_ONLY
def test_app_icon_on_linux_is_the_png(tmp_path, monkeypatch):
    monkeypatch.setattr(app_mod.os, "name", "posix")
    fake = SimpleNamespace(paths=SimpleNamespace(home=tmp_path))
    result = app_mod.App._app_icon_png(fake)
    assert result == str(tmp_path / app_mod.APP_ICON_PNG)
    assert (tmp_path / app_mod.APP_ICON_PNG).is_file()


def test_app_icon_off_windows_delegates_to_the_png(monkeypatch):
    monkeypatch.setattr(app_mod.os, "name", "posix")
    fake = SimpleNamespace(_app_icon_png=lambda: "/x/app.png")
    assert app_mod.App._app_icon(fake) == "/x/app.png"


def test_webview_gui_is_edgechromium_only_on_windows(monkeypatch):
    monkeypatch.setattr(app_mod.os, "name", "nt")
    assert app_mod.webview_gui() == "edgechromium"
    monkeypatch.setattr(app_mod.os, "name", "posix")
    assert app_mod.webview_gui() is None


def test_signal_handlers_also_go_through_glib(monkeypatch):
    seen: list[tuple] = []
    monkeypatch.setattr(xutil, "install_unix_signals", lambda fn, sigs: seen.append(sigs) or True)
    monkeypatch.setattr(app_mod.signal, "signal", lambda *_a: None)
    app_mod.App._install_signal_handlers(SimpleNamespace(quit=lambda: None))
    assert seen and app_mod.signal.SIGTERM in seen[0] and app_mod.signal.SIGINT in seen[0]


def test_app_icon_failure_is_not_fatal(tmp_path, monkeypatch):
    monkeypatch.setattr(icon, "write_app_png", lambda *_a: (_ for _ in ()).throw(OSError("ro")))
    fake = SimpleNamespace(paths=SimpleNamespace(home=tmp_path))
    assert app_mod.App._app_icon_png(fake) is None


@POSIX_ONLY
def test_spawn_restart_on_posix_starts_a_new_session(monkeypatch, tmp_path):
    seen: dict[str, Any] = {}
    monkeypatch.setattr(app_mod.os, "name", "posix")
    monkeypatch.setattr(
        app_mod.subprocess, "Popen", lambda args, **kw: seen.update(kw) or SimpleNamespace(pid=1)
    )
    app_mod.spawn_restart(tmp_path)
    assert seen["start_new_session"] is True and "creationflags" not in seen
    assert seen["close_fds"] is True and seen["stdin"] is subprocess.DEVNULL


def test_hotkey_status_is_logged_when_the_hotkey_starts(monkeypatch):
    messages: list[tuple[str, dict]] = []
    monkeypatch.setattr(app_mod.log, "info", lambda event, **kw: messages.append((event, kw)))
    monkeypatch.setattr(app_mod.log, "warning", lambda event, **kw: messages.append((event, kw)))
    notes: list[str] = []

    def fake_app(error: str | None) -> Any:
        hotkey = SimpleNamespace(start=lambda _spec: error, status="Copilot key hooked")
        services = SimpleNamespace(manager=None)
        return SimpleNamespace(
            services=services,
            tray=SimpleNamespace(start=lambda: None, notify=notes.append),
            hotkey=hotkey,
            cfg=SimpleNamespace(ui=SimpleNamespace(hotkey="Copilot")),
            instance_sock=None,
        )

    app_mod.App._start_desktop_threads(fake_app(None))
    assert ("hotkey ready", {"status": "Copilot key hooked"}) in messages and notes == []
    messages.clear()
    app_mod.App._start_desktop_threads(fake_app("Could not hook"))
    assert messages == [("hotkey unavailable", {"error": "Could not hook"})]
    assert notes == ["Could not hook"]
