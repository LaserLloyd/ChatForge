"""System tray icon and menu (pystray), running ``icon.run()`` on its own thread.

# Adapted from StudioForge src/studioforge/tray/tray_app.py: `_build_menu` shape,
# `_refresh`, `_notify`, `_spawn_thread`, `_open_path`, `_on_toggle_autostart`, `_on_quit`
# (MIT, LaserLloyd). Cut: server supervision, adoption, watchdog, ports, MCP, API client.

Menu (PLAN WS7 step 6): status line, Open chat (default, so a left click opens it),
Settings, Load model, Unload models, Open logs folder, Start at login (checked), Restart,
Quit. Handlers run on a spawned thread, never on pystray's message-pump thread.

"Load model" is enabled while the local model is neither loaded nor loading; "Unload
models" while it is loaded or loading (unloading also abandons a load in progress).
"""

from __future__ import annotations

import logging
import os
import threading
from collections.abc import Callable, MutableMapping
from pathlib import Path
from typing import Any

from chatforge.desktop import win32util, xutil
from chatforge.desktop.icon import IconState, make_icon_image

_log = logging.getLogger(__name__)

APP_NAME = "ChatForge"

#: ``runtime.status`` states in which the local model is loading.
LOADING_STATES = frozenset({"starting", "compiling"})


def choose_backend(environ: MutableMapping[str, str] | None = None) -> str | None:
    """Linux: pin pystray's backend before it is imported, and return the choice.

    ``PYSTRAY_BACKEND`` (``appindicator``, ``gtk``, ``xorg``, ``dummy``) is honoured as is.
    Unset, an X display means ``xorg``: the AppIndicator and GTK backends need a GTK main
    loop of their own, which pywebview already owns, while ``xorg`` talks to the X server
    from the tray thread. Without a display nothing is set and pystray decides (and fails
    with its own message). ``None`` off Linux.
    """
    env = os.environ if environ is None else environ
    if not xutil.is_linux():
        return None
    explicit = env.get("PYSTRAY_BACKEND")
    if explicit:
        return explicit
    if env.get("DISPLAY"):
        env["PYSTRAY_BACKEND"] = "xorg"
        return "xorg"
    return None


class Tray:
    """The notification-area icon. All callbacks are optional and run off the pump thread."""

    def __init__(
        self,
        *,
        on_open: Callable[[], None],
        on_settings: Callable[[], None],
        on_load: Callable[[], None],
        on_unload: Callable[[], None],
        on_quit: Callable[[], None],
        logs_dir: Path,
        autostart_enabled: Callable[[], bool],
        set_autostart: Callable[[bool], str | None],
        on_restart: Callable[[], bool] | None = None,
        app_name: str = APP_NAME,
    ) -> None:
        self._on_open = on_open
        self._on_settings = on_settings
        self._on_load = on_load
        self._on_unload = on_unload
        self._on_quit = on_quit
        self._on_restart = on_restart
        self._logs_dir = logs_dir
        self._autostart_enabled = autostart_enabled
        self._set_autostart = set_autostart
        self._app_name = app_name

        self.icon: Any = None
        self.state: IconState = "off"
        self.status: str = "No model loaded"
        #: The local model's ``runtime.status`` state (``unloaded``, ``starting``, ...).
        self.runtime_state: str | None = None
        self._thread: threading.Thread | None = None
        self._quitting = False
        self._lock = threading.Lock()

    # --- lifecycle ------------------------------------------------------------------

    def start(self) -> Tray:
        choose_backend()  # before pystray picks its backend at import
        import pystray

        if self.icon is not None:
            return self
        self.icon = pystray.Icon(
            "chatforge",
            icon=make_icon_image(self.state),
            title=f"{self._app_name} - {self.status}",
            menu=self._build_menu(),
        )
        # The win32 backend allows run() off the main thread (the main thread belongs to
        # pywebview). pystray creates its hidden window and message loop on this thread.
        self._thread = threading.Thread(target=self._run, name="chatforge-tray", daemon=True)
        self._thread.start()
        return self

    def _run(self) -> None:
        try:
            self.icon.run()
        except Exception:  # noqa: BLE001 - a dead tray thread must be visible in the log
            _log.exception("tray icon loop failed")

    def stop(self, timeout: float = 3.0) -> None:
        icon, thread = self.icon, self._thread
        if icon is None:
            return
        try:
            icon.visible = False
            icon.stop()
        except Exception as exc:  # noqa: BLE001 - backend specific
            _log.warning("tray stop failed: %s", type(exc).__name__)
        if thread is not None and thread is not threading.current_thread():
            thread.join(timeout=timeout)
        self.icon = None
        self._thread = None

    # --- state ------------------------------------------------------------------------

    @property
    def model_loaded(self) -> bool:
        return self.runtime_state == "ready"

    @property
    def model_loading(self) -> bool:
        return self.runtime_state in LOADING_STATES

    def can_load(self) -> bool:
        """Whether "Load model" is enabled: the local model is neither loaded nor loading."""
        return not (self.model_loaded or self.model_loading)

    def can_unload(self) -> bool:
        """Whether "Unload models" is enabled: the local model is loaded or loading."""
        return self.model_loaded or self.model_loading

    def update(self, state: IconState, status: str, *, runtime_state: str | None = None) -> None:
        """Refresh the dot, tooltip and menu. Safe from any thread.

        ``runtime_state`` is the local model's ``runtime.status`` state; it decides which of
        Load model / Unload models is enabled (``None`` keeps the last one).
        """
        # Adapted from StudioForge src/studioforge/tray/tray_app.py `_refresh` (MIT, LaserLloyd)
        with self._lock:
            changed = state != self.state or status != self.status
            self.state = state
            self.status = status
            if runtime_state is not None and runtime_state != self.runtime_state:
                self.runtime_state = runtime_state
                changed = True
        icon = self.icon
        if icon is None or not changed:
            return
        try:
            icon.icon = make_icon_image(state)
            icon.title = f"{self._app_name} - {status}"[:127]
            icon.update_menu()
        except Exception as exc:  # noqa: BLE001 - backend specific
            _log.warning("could not refresh the tray icon: %s", type(exc).__name__)

    def notify(self, message: str, title: str | None = None) -> None:
        """Balloon notification, degrading to a log line when unsupported."""
        # Adapted from StudioForge src/studioforge/tray/tray_app.py `_notify` (MIT, LaserLloyd)
        try:
            if self.icon is not None and getattr(self.icon, "HAS_NOTIFICATION", False):
                self.icon.notify(message, title or self._app_name)
                return
        except Exception as exc:  # noqa: BLE001
            _log.warning("notification failed: %s", type(exc).__name__)
        _log.info("tray notification: %s", message)

    # --- menu -------------------------------------------------------------------------

    def _build_menu(self) -> Any:
        # Adapted from StudioForge src/studioforge/tray/tray_app.py `_build_menu` (MIT, LaserLloyd)
        import pystray

        item = pystray.MenuItem
        sep = pystray.Menu.SEPARATOR
        return pystray.Menu(
            item(lambda _i: self.status, None, enabled=False),
            sep,
            # default=True is what a LEFT click on the icon invokes.
            item("Open chat", self._menu_open, default=True),
            item("Settings", self._menu_settings),
            sep,
            item("Load model", self._menu_load, enabled=lambda _i: self.can_load()),
            item("Unload models", self._menu_unload, enabled=lambda _i: self.can_unload()),
            item("Open logs folder", self._menu_logs),
            sep,
            item(
                "Start at login",
                self._menu_toggle_autostart,
                checked=lambda _i: self._safe_autostart_enabled(),
            ),
            sep,
            item("Restart", self._menu_restart, visible=self._on_restart is not None),
            item("Quit", self._menu_quit),
        )

    def _safe_autostart_enabled(self) -> bool:
        try:
            return bool(self._autostart_enabled())
        except Exception as exc:  # noqa: BLE001 - platform specific
            _log.warning("could not read the autostart status: %s", type(exc).__name__)
            return False

    # --- handlers (each on its own thread) --------------------------------------------

    @staticmethod
    def _spawn_thread(fn: Callable[[], None], name: str) -> None:
        """Run a handler off the tray message-pump thread.

        pystray dispatches menu callbacks on the thread that owns the Win32 message
        loop; anything slow done there freezes the menu and the icon until it finishes.
        """
        # Adapted from StudioForge src/studioforge/tray/tray_app.py `_spawn_thread` (MIT, LaserLloyd)

        def _safe() -> None:
            try:
                fn()
            except Exception:  # noqa: BLE001
                _log.exception("tray action %s failed", name)

        threading.Thread(target=_safe, daemon=True, name=f"chatforge-tray-{name}").start()

    def _menu_open(self, _icon: Any = None, _item: Any = None) -> None:
        self._spawn_thread(self._on_open, "open")

    def _menu_settings(self, _icon: Any = None, _item: Any = None) -> None:
        self._spawn_thread(self._on_settings, "settings")

    def _menu_load(self, _icon: Any = None, _item: Any = None) -> None:
        self._spawn_thread(self._on_load, "load")

    def _menu_unload(self, _icon: Any = None, _item: Any = None) -> None:
        self._spawn_thread(self._on_unload, "unload")

    def _menu_logs(self, _icon: Any = None, _item: Any = None) -> None:
        self._spawn_thread(self._open_logs, "logs")

    def _open_logs(self) -> None:
        # Adapted from StudioForge src/studioforge/tray/tray_app.py `_open_path` (MIT, LaserLloyd)
        try:
            self._logs_dir.mkdir(parents=True, exist_ok=True)
            win32util.open_path(self._logs_dir)
        except (OSError, AttributeError) as exc:
            self.notify(f"Cannot open {self._logs_dir}: {exc}")

    def _menu_toggle_autostart(self, _icon: Any = None, _item: Any = None) -> None:
        # Adapted from StudioForge src/studioforge/tray/tray_app.py `_on_toggle_autostart` (MIT, LaserLloyd)
        def work() -> None:
            want = not self._safe_autostart_enabled()
            error = self._set_autostart(want)
            if error:
                self.notify(error)
            else:
                self.notify(
                    "ChatForge will start when you sign in."
                    if want
                    else "ChatForge will not start automatically."
                )
            icon = self.icon
            if icon is not None:
                icon.update_menu()

        self._spawn_thread(work, "autostart")

    def _menu_quit(self, _icon: Any = None, _item: Any = None) -> None:
        # Adapted from StudioForge src/studioforge/tray/tray_app.py `_on_quit` (MIT, LaserLloyd)
        with self._lock:
            if self._quitting:
                return
            self._quitting = True
        self._spawn_thread(self._on_quit, "quit")

    def _menu_restart(self, _icon: Any = None, _item: Any = None) -> None:
        """Quit the same way as Quit and start a fresh copy (``on_restart``). When the new
        copy cannot be started, ``on_restart`` returns False and this one keeps running."""
        on_restart = self._on_restart
        if on_restart is None:
            return
        with self._lock:
            if self._quitting:
                return
            self._quitting = True

        def work() -> None:
            self.notify(f"Restarting {self._app_name}…")
            restarted = False
            try:
                restarted = on_restart() is not False
            finally:
                if not restarted:
                    with self._lock:
                        self._quitting = False

        self._spawn_thread(work, "restart")
