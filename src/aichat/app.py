"""Application wiring: single instance, config, logging, services, windows, tray, hotkey.

Threads (PLAN 1):

* **main** runs ``webview.start()`` (it must);
* **core** (:class:`~aichat.desktop.core_loop.CoreLoop`) runs the asyncio loop that owns
  every service: registry, providers, tools, manager + idle reaper, downloader, engine;
* **events** drains the event queue into ``window.run_js`` (50 ms coalescing);
* **tray** runs ``pystray`` ``icon.run()``; **hotkey** runs the ``RegisterHotKey``
  message loop; **single-instance** accepts ``SHOW`` from a second launch;
* the **static** server thread serves ``src/aichat/web`` on ``127.0.0.1:<random>``.

``js_api`` calls arrive on pywebview worker threads and forward to the core loop.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import os
import signal
import subprocess
import sys
import threading
from pathlib import Path
from typing import Any

from aichat import autostart, single_instance
from aichat.config import AppConfig, load_config
from aichat.logging_setup import configure_logging, flush_logging, get_logger
from aichat.paths import Paths

log = get_logger(__name__)

Mode = str  # "hidden" | "show" | "settings"

QUIT_TIMEOUT_S = 20.0
#: The taskbar identity (groups AI Chat's windows apart from other Python programs).
APP_USER_MODEL_ID = "LaserLloyd.AIChat"
#: Bump the version when the artwork changes, so the new icon is written.
APP_ICON_FILE = "app-icon-v2.ico"
MODEL_REFRESH_FIRST_DELAY_S = 60.0
MODEL_REFRESH_INTERVAL_S = 24 * 3600.0
#: ``CreateProcess`` flags for the copy that Restart starts: no visible console, and not in
#: this process's console group (a Ctrl+C or Ctrl+Break here must not reach it). Not
#: ``DETACHED_PROCESS``: a venv's ``python.exe`` is a launcher that starts the real
#: interpreter as its own child, and a detached launcher's child gets a new, visible console.
#: ``CREATE_NO_WINDOW`` gives the launcher a hidden console that its child shares.
RESTART_CREATIONFLAGS = (
    getattr(subprocess, "CREATE_NO_WINDOW", 0x08000000)
    | getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0x00000200)
    if os.name == "nt"
    else 0
)


class App:
    """Everything that lives for the process. ``main()`` builds one and runs it."""

    def __init__(self, paths: Paths, cfg: AppConfig, *, mode: Mode = "show") -> None:
        from aichat.desktop.bridge import Api, Services

        self.paths = paths
        self.mode = mode
        self.services = Services(paths, cfg)
        self.api = Api(self.services)
        self.instance_sock: Any = None
        self.instance_server: Any = None
        self.static: Any = None
        self.tray: Any = None
        self.hotkey: Any = None
        self._quit_lock = threading.Lock()
        self._quitting = False
        self._reaper_task: Any = None
        self.exit_code = 0

    # --- config access -----------------------------------------------------------------

    @property
    def cfg(self) -> AppConfig:
        return self.services.config

    def get_config(self) -> AppConfig:
        return self.services.config

    # --- services (core loop) ------------------------------------------------------------

    async def _build_services(self) -> None:
        """Runs on the core loop: construct every loop-owned service."""
        from aichat.llm.providers import ProviderRegistry
        from aichat.models.catalog import Catalog
        from aichat.models.downloader import Downloader
        from aichat.models.hf_search import HfSearch
        from aichat.models.registry import Registry
        from aichat.tools.registry import ToolRegistry

        s = self.services
        paths = self.paths
        cfg = self.cfg

        s.catalog = Catalog.load()
        s.registry = Registry(
            paths.models_dir, paths.cache_dir, catalog=s.catalog, state_file=paths.state_file
        )
        s.registry.scan()
        s.hf = HfSearch(endpoint=cfg.hf.endpoint)
        s.downloader = Downloader(
            paths.models_dir, paths.downloads_file, hf=s.hf, endpoint=cfg.hf.endpoint
        )
        await s.downloader.start()
        s.downloader.subscribe(self._on_download_progress)
        s.tools = ToolRegistry(cfg.tools)

        s.manager = self._build_manager()
        if s.manager is not None:
            s.manager.subscribe(self._on_runtime_status)
            await s.manager.start()
            self._start_reaper()

        s.providers = ProviderRegistry(cfg.providers, manager=s.manager, settings=self.get_config)
        s.engine = self._build_engine()

        # Adoption of models that were copied in by hand (needs the network; never fatal).
        s.loop.create_task(self._adopt_unadopted(), name="adopt-models")
        s.loop.create_task(self._refresh_models_daily(), name="refresh-models")

    def _build_manager(self) -> Any:
        try:
            from aichat.runtime import ovms_install
            from aichat.runtime.manager import LocalModelManager
            from aichat.runtime.ovms_supervisor import OvmsSupervisor
        except ImportError as exc:  # pragma: no cover - WS6/WS1 not merged
            log.warning("local runtime manager unavailable", error=str(exc))
            return None
        s = self.services
        cfg = self.cfg
        exe = ovms_install.ovms_exe(self.paths.runtime_dir, cfg.local.ovms_version)
        supervisor = OvmsSupervisor(
            exe,
            variant=cfg.local.ovms_variant,
            ovms_version=cfg.local.ovms_version,
            state_file=self.paths.state_file,
            load_timeout_s=float(cfg.local.load_timeout_s),
        )
        return LocalModelManager(
            supervisor,
            get_config=self.get_config,
            paths=self.paths,
            registry=s.registry,
            catalog=s.catalog,
            is_installed=lambda: bool(
                ovms_install.runtime_status(self.paths, self.get_config().local)["installed"]
            ),
        )

    def _start_reaper(self) -> None:
        try:
            from aichat.runtime.idle import IdleReaper
        except ImportError as exc:  # pragma: no cover
            log.warning("idle reaper unavailable", error=str(exc))
            return
        reaper = IdleReaper(self.services.manager, self.services.manager.idle_ttl_s)
        self._reaper_task = self.services.loop.create_task(reaper.run(), name="idle-reaper")

    def _build_engine(self) -> Any:
        try:
            from aichat.chat.engine import ChatEngine
        except ImportError as exc:  # pragma: no cover - WS6 not merged
            log.warning("chat engine unavailable", error=str(exc))
            return None
        return ChatEngine(
            providers=self.services.providers,
            tools=self.services.tools,
            get_config=self.get_config,
            conversation_file=self.paths.conversation_file,
        )

    async def _refresh_models_daily(self) -> None:
        """Re-read the remote providers' model lists a minute after start, then daily
        (``chat.auto_refresh_models``). Providers without a key are skipped; a failure is
        logged and retried at the next interval, never surfaced as an error."""
        await asyncio.sleep(MODEL_REFRESH_FIRST_DELAY_S)
        while True:
            api = getattr(self, "api", None)
            if api is not None and self.cfg.chat.auto_refresh_models:
                try:
                    await api._refresh_models_async(None)  # noqa: SLF001 - same package
                except Exception as exc:  # noqa: BLE001 - background chore
                    log.warning("model list refresh failed", error=type(exc).__name__)
            await asyncio.sleep(MODEL_REFRESH_INTERVAL_S)

    async def _adopt_unadopted(self) -> None:
        s = self.services
        try:
            pending = s.registry.unadopted()
        except Exception:  # noqa: BLE001
            log.exception("registry.unadopted failed")
            return
        for record in pending:
            try:
                files = await s.hf.repo_files(record.id)
                meta = await s.hf.repo_meta(record.id)
                s.registry.adopt(record.id, files, revision=meta.sha, license=meta.license)
                log.info("model adopted", model=record.id)
            except Exception as exc:  # noqa: BLE001 - offline or mismatched: try again next start
                log.warning("model adoption skipped", model=record.id, error=str(exc)[:200])

    async def _close_services(self) -> None:
        """WS6 quit order: cancel the reaper, cancel chats, unload, then the rest."""
        s = self.services
        if self._reaper_task is not None:
            self._reaper_task.cancel()
            with contextlib.suppress(BaseException):
                await self._reaper_task
            self._reaper_task = None
        if s.engine is not None:
            with contextlib.suppress(Exception):
                s.engine.cancel_all("shutdown")
        if s.manager is not None:
            with contextlib.suppress(Exception):
                await asyncio.wait_for(s.manager.aclose(), timeout=15)
        if s.downloader is not None:
            with contextlib.suppress(Exception):
                await asyncio.wait_for(s.downloader.aclose(), timeout=5)
        if s.hf is not None:
            with contextlib.suppress(Exception):
                await s.hf.aclose()

    # --- event hooks (called on the core loop) -------------------------------------------

    def _on_runtime_status(self, status: dict[str, Any]) -> None:
        from aichat.desktop.icon import state_for_runtime

        s = self.services
        if s.events is not None:
            s.events.emit({"type": "runtime.status", **status})
        if self.tray is not None:
            cfg = self.cfg
            local = cfg.providers.get(cfg.chat.provider, None)
            local_selected = local is None or local.kind == "ovms"
            state = status.get("state")
            text = _status_text(status)
            self.tray.update(
                state_for_runtime(state, local_selected=local_selected),
                text,
                runtime_state=str(state or "unloaded"),
            )

    def _on_download_progress(self, payload: dict[str, Any]) -> None:
        from aichat.desktop.bridge import download_view

        s = self.services
        if s.events is None or s.downloader is None:
            return
        summary = s.downloader.get(str(payload.get("group_id")))
        if summary is not None:
            s.events.emit({"type": "download.progress", **download_view(summary)})

    def _on_config_changed(self, cfg: AppConfig) -> None:
        # Tools read their limits from the config object they were built with.
        from aichat.tools.registry import ToolRegistry

        s = self.services
        with contextlib.suppress(Exception):
            s.tools = ToolRegistry(cfg.tools)
            if s.engine is not None:
                s.engine._tools = s.tools  # noqa: SLF001 - engine has no setter (WS6)

    # --- desktop -------------------------------------------------------------------------

    def _on_popup_shown(self) -> None:
        cfg = self.cfg
        spec = cfg.providers.get(cfg.chat.provider)
        if spec is not None and spec.kind == "ovms" and cfg.local.autoload_on_open:
            with contextlib.suppress(Exception):
                self.api._warm_local_model()  # noqa: SLF001 - same package

    def _on_reply_end(self, _request_id: str) -> None:
        """A reply finished or failed (``Services.on_reply_end``, on the core loop, so it
        must not block): with ``ui.show_on_reply``, bring the hidden popup back in its
        corner without taking the focus. The popup call waits on the GUI thread, so it runs
        on a thread of its own."""
        popup = self.services.popup
        if popup is None or self._quitting or not self.cfg.ui.show_on_reply:
            return

        def show() -> None:
            try:
                popup.show_inactive(source="reply")
            except Exception:  # noqa: BLE001
                log.exception("showing the popup for a finished reply failed")

        threading.Thread(target=show, name="aichat-reply-show", daemon=True).start()

    def _set_autostart(self, want: bool) -> str | None:
        reply = self.api.set_autostart(want)
        if reply.get("ok"):
            return None
        return str(reply.get("error", {}).get("message") or "Could not change autostart.")

    def _build_desktop(self) -> None:
        from aichat.desktop.events import EventSink
        from aichat.desktop.hotkey import HotkeyThread
        from aichat.desktop.popup import Popup
        from aichat.desktop.settings_window import SettingsWindow
        from aichat.desktop.tray import Tray

        s = self.services
        s.events = EventSink().start()
        s.popup = Popup(
            url=self.static.url("index.html"),
            api=self.api,
            config=self.get_config,
            events=s.events,
            on_shown=self._on_popup_shown,
        )
        s.popup.create()
        s.settings_window = SettingsWindow(
            url=self.static.url("settings.html"),
            api=self.api,
            events=s.events,
            on_opening=s.popup.note_settings_opening,
        )
        s.on_config_changed = self._on_config_changed
        s.on_reply_end = self._on_reply_end

        self.tray = Tray(
            on_open=lambda: s.popup.toggle(source="tray"),
            on_settings=lambda: s.settings_window.open(),
            on_load=lambda: self.api.load_model(),
            on_unload=lambda: self.api.unload_model(),
            on_quit=self.quit,
            on_restart=self.restart,
            logs_dir=self.paths.logs_dir,
            autostart_enabled=lambda: autostart.status().enabled,
            set_autostart=self._set_autostart,
        )
        self.hotkey = HotkeyThread(on_press=lambda: s.popup.toggle(source="hotkey"))
        s.hotkey = self.hotkey

    def _start_desktop_threads(self) -> None:
        s = self.services
        self.tray.start()
        error = self.hotkey.start(self.cfg.ui.hotkey)
        if error:
            log.warning("hotkey unavailable", error=error)
            self.tray.notify(error)
        if self.instance_sock is not None:
            self.instance_server = single_instance.start_server(
                self.instance_sock, lambda: s.popup.show(source="instance")
            )
        if s.manager is not None:
            with contextlib.suppress(Exception):
                self._on_runtime_status(s.manager.status())

    def _on_webview_started(self) -> None:
        """pywebview's ``func``: runs on a worker thread once the popup exists."""
        s = self.services
        try:
            self._start_desktop_threads()
            if self.mode == "show":
                s.popup.show(source="cli")
            elif self.mode == "settings":
                s.settings_window.open()
            log.info("ready", mode=self.mode, static=self.static.base_url)
        except Exception:  # noqa: BLE001
            log.exception("startup failed")
            self.exit_code = 1
            self.quit()

    # --- run / quit ----------------------------------------------------------------------

    def _app_icon(self) -> str | None:
        """The app's own taskbar identity and window icon.

        Without these, Windows groups the windows under ``pythonw.exe`` and pywebview copies
        its Python icon. The AppUserModelID gives AI Chat its own taskbar button; the
        ``.ico`` (written once, versioned by name) is used for every window.
        """
        if os.name != "nt":
            return None
        with contextlib.suppress(Exception):
            import ctypes

            ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID(APP_USER_MODEL_ID)
        try:
            from aichat.desktop.icon import write_app_ico

            return str(write_app_ico(self.paths.home / APP_ICON_FILE))
        except Exception as exc:  # noqa: BLE001 - a missing icon must not stop the app
            log.warning("app icon unavailable", error=type(exc).__name__)
            return None

    def run(self) -> int:
        from aichat.desktop.core_loop import CoreLoop
        from aichat.desktop.webserver import StaticServer

        s = self.services
        self.static = StaticServer().start()
        s.loop = CoreLoop().start()
        s.loop.run(self._build_services(), timeout=60)
        self._build_desktop()
        self._install_signal_handlers()

        import webview

        webview.settings["OPEN_EXTERNAL_LINKS_IN_BROWSER"] = True
        webview.settings["DRAG_REGION_SELECTOR"] = ".pywebview-drag-region"
        webview.settings["SHOW_DEFAULT_MENUS"] = False
        try:
            webview.start(
                self._on_webview_started,
                gui="edgechromium",
                private_mode=False,
                storage_path=str(self.paths.webview_dir),
                debug=False,
                icon=self._app_icon(),
            )
        finally:
            self._shutdown_after_webview()
        return self.exit_code

    def _install_signal_handlers(self) -> None:
        # The main thread sits in webview.start(), which polls its GUI thread, so Python
        # signal handlers do run there. Ctrl+Break and SIGTERM quit through quit() (the
        # launch check relies on CTRL_BREAK_EVENT). SIGINT is ours only until
        # webview.start(): pywebview then installs its own Ctrl+C handler, which calls
        # Application.Exit() on the GUI thread. The popup lets that close through
        # (Popup._on_form_closing), so webview.start() returns and
        # _shutdown_after_webview() quits the same way.
        def _handler(_signum: int, _frame: Any) -> None:
            threading.Thread(target=self.quit, name="aichat-signal-quit", daemon=True).start()

        for name in ("SIGINT", "SIGBREAK", "SIGTERM"):
            sig = getattr(signal, name, None)
            if sig is not None:
                with contextlib.suppress(Exception):
                    signal.signal(sig, _handler)

    def quit(self) -> None:
        """Clean quit from any thread: unload the model, stop the icon, destroy windows."""
        with self._quit_lock:
            if self._quitting:
                return
            self._quitting = True
        log.info("quitting")
        s = self.services
        if s.loop is not None and s.loop.running:
            with contextlib.suppress(Exception):
                s.loop.run(self._close_services(), timeout=QUIT_TIMEOUT_S)
        if self.hotkey is not None:
            with contextlib.suppress(Exception):
                self.hotkey.stop()
        if self.tray is not None:
            with contextlib.suppress(Exception):
                self.tray.stop()
        if self.instance_server is not None:
            with contextlib.suppress(Exception):
                self.instance_server.stop()
        if s.events is not None:
            with contextlib.suppress(Exception):
                s.events.stop()
        # Destroying the last (master) window ends webview.start() on the main thread.
        with contextlib.suppress(Exception):
            if s.settings_window is not None:
                s.settings_window.close()
        with contextlib.suppress(Exception):
            if s.popup is not None:
                s.popup.destroy()

    def restart(self) -> bool:
        """Tray "Restart": start a fresh copy, then quit the same way as Quit (the model is
        unloaded). The copy waits for this process to exit (``--after-pid``) before it
        takes the single-instance port. Returns False, and keeps running, when the copy
        could not be started."""
        with self._quit_lock:
            if self._quitting:
                return False
        try:
            proc = spawn_restart(self.paths.home)
        except (OSError, ValueError, subprocess.SubprocessError) as exc:
            log.warning("restart failed", error=str(exc)[:200])
            if self.tray is not None:
                self.tray.notify(f"Could not restart AI Chat: {exc}")
            return False
        log.info("restarting", new_pid=getattr(proc, "pid", None))
        self.quit()
        return True

    def _shutdown_after_webview(self) -> None:
        self.quit()
        s = self.services
        if s.loop is not None:
            with contextlib.suppress(Exception):
                s.loop.stop(timeout=5)
        if self.static is not None:
            with contextlib.suppress(Exception):
                self.static.stop()
        if self.instance_sock is not None:
            with contextlib.suppress(OSError):
                self.instance_sock.close()
        flush_logging()


def restart_command(pid: int | None = None) -> list[str]:
    """The command line of the copy Restart starts: the same one start at login uses
    (``pythonw.exe`` next to this interpreter, so the tray app owns no console, even when
    this copy was started with ``python.exe``), hidden in the tray, after ``pid`` (this
    process by default) has exited."""
    after = os.getpid() if pid is None else int(pid)
    return [*autostart.launch_argv(), "--after-pid", str(after)]


def spawn_restart(home: Path) -> subprocess.Popen[bytes]:
    """Start the replacement copy apart from this process (no visible console, own process
    group, no inherited handles), in ``home``. Raises ``OSError`` when it cannot start."""
    return subprocess.Popen(  # noqa: S603 - our own interpreter and module
        restart_command(),
        cwd=str(home),
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        close_fds=True,
        creationflags=RESTART_CREATIONFLAGS,
    )


def _status_text(status: dict[str, Any]) -> str:
    state = status.get("state")
    model = str(status.get("model_id") or "").rpartition("/")[2]
    if state == "ready":
        return f"{model} ready on {status.get('device') or 'NPU'}"
    if state in ("starting", "compiling"):
        expected = status.get("expected_s")
        eta = f" (~{int(expected)} s)" if expected else ""
        word = "Compiling" if state == "compiling" or status.get("first_compile") else "Loading"
        return f"{word} {model}{eta}"
    if state == "unloading":
        return f"Unloading {model}"
    if state == "error":
        return f"Model error: {str(status.get('error') or '')[:60]}"
    if state == "not_installed":
        return "Runtime not installed"
    return "No model loaded"


# --------------------------------------------------------------------------- #
# Entry
# --------------------------------------------------------------------------- #


def _bootstrap_logging(paths: Paths, cfg: AppConfig | None) -> None:
    level = cfg.logging.level if cfg is not None else "INFO"
    kwargs: dict[str, Any] = {"log_dir": paths.logs_dir}
    if cfg is not None:
        kwargs.update(max_bytes=cfg.logging.max_bytes, backup_count=cfg.logging.backup_count)
    configure_logging(level, **kwargs)


def main(mode: Mode = "show", *, paths: Paths | None = None) -> int:
    """Run the tray app. Returns the process exit code."""
    from aichat.desktop import win32util

    # Before any window exists (§7, DPI): per-monitor-v2 awareness.
    win32util.ensure_dpi_awareness()

    paths = paths or Paths.default()
    paths.ensure_dirs()
    first_run = not paths.config_file.exists()
    _bootstrap_logging(paths, None)
    cfg = load_config(paths, recover=True)
    _bootstrap_logging(paths, cfg)
    logging.getLogger("pywebview").setLevel(logging.WARNING)

    sock = single_instance.acquire_single_instance()
    if sock is None:
        delivered = single_instance.signal_existing()
        log.info("another instance is running", signalled=delivered)
        return 0

    if first_run and cfg.startup.autostart:
        try:
            autostart.enable(home=paths.home)
        except Exception as exc:  # noqa: BLE001 - never fatal
            log.warning("first-run autostart failed", error=str(exc))

    app = App(paths, cfg, mode=mode)
    app.instance_sock = sock
    log.info(
        "starting", mode=mode, home=str(paths.home), pid=os.getpid(), python=sys.version.split()[0]
    )
    try:
        return app.run()
    except Exception:  # noqa: BLE001
        log.exception("fatal error")
        return 1
    finally:
        flush_logging()


__all__ = ["App", "main"]
