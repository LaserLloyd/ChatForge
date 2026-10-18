"""Wires the add-on into ChatForge: the Settings card, the hooks, the host link and the helper threads.

Only imported after ``chatforge_voice.setup`` has checked that forge-voice-core is installed. Nothing
here starts a thread, opens a socket or loads a model until ChatForge calls ``on_startup``, and then
only the light parts (the panel's loopback server, the key watcher, the idle reaper): the speech
models load on the first use.
"""

from __future__ import annotations

import logging
import threading
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

from forge_voice_core import hostlink

from . import hotkey, settings
from .audio_io import Player, Recorder
from .controller import Deps, VoiceController, default_deps
from .host import ChatForgeHost
from .models_card import ModelsService
from .panel import PanelServer

log = logging.getLogger("chatforge_voice")

SECTION_TITLE = "Voice (CPU)"
SECTION_TEXT = (
    "Hold the push-to-talk key, speak, release: ChatForge answers out loud. Speech recognition and the "
    "spoken reply run on this computer's CPU (nothing leaves it). Before the first use, download the "
    "voice models (about 1.4 GB) in the Voice panel: run `chatforge-voice panel` in a terminal "
    "(ChatForge must be running). Needs about 3 GB of free memory while voice is loaded."
)
_TYPES = {"bool": "bool", "enum": "choice", "float": "float", "int": "int", "str": "text"}


def card_fields(extensions_module: Any) -> list[Any]:
    """The Settings card's ``SettingField`` list, generated from the schema rows."""
    fields = []
    for key in settings.CARD_KEYS:
        row = settings.SCHEMA.setting(key)
        kw: dict[str, Any] = {
            "label": row.label or key,
            "type": _TYPES[row.type],
            "default": row.default,
            "help": row.help,
        }
        if row.choices:
            kw["choices"] = tuple(row.choices)
        if row.lo is not None:
            kw["minimum"], kw["maximum"] = row.lo, row.hi
        fields.append(extensions_module.SettingField(key, **kw))
    return fields


class VoiceAddon:
    def __init__(
        self,
        api: Any,
        *,
        deps: Deps | None = None,
        models: ModelsService | None = None,
        is_down: Callable[[int], bool] | None = None,
        settings_ttl: float = 1.0,
    ) -> None:
        self.api = api
        self._deps = deps
        self._models = models
        self._is_down = is_down
        self._ttl = settings_ttl
        self._cache: tuple[float, Any] | None = None
        self.controller: VoiceController | None = None
        self.panel: PanelServer | None = None
        self.watcher: hotkey.HoldKeyWatcher | None = None
        self._stop = threading.Event()
        self._reaper: threading.Thread | None = None
        self._spec = ""

    # ---- settings --------------------------------------------------------
    def engine_settings(self) -> settings.VoiceEngineSettings:
        now = time.monotonic()
        cached = self._cache
        if cached is not None and now - cached[0] < self._ttl:
            return cached[1]
        value = settings.from_prefs(self.api.settings)
        self._cache = (now, value)
        return value

    # ---- registration (ChatForge calls this once, at start) -----------------
    def register(self) -> None:
        try:
            import chatforge.extensions as ext
        except ImportError:
            raise RuntimeError(
                "This ChatForge has no add-on support yet. Update ChatForge to 0.2.0 or later."
            ) from None

        api = self.api
        api.register_settings_section(
            card_fields(ext), title=SECTION_TITLE, section_id="main", description=SECTION_TEXT
        )
        hostlink.install(ChatForgeHost(api.data_dir, self.engine_settings))
        self.controller = VoiceController(
            api.chat, self.engine_settings, self._deps or default_deps()
        )
        self._models = self._models or ModelsService(self.engine_settings)
        self.panel = PanelServer(self.controller, self._models, api.data_dir)
        self.watcher = hotkey.HoldKeyWatcher(
            self.controller.talk_start,
            self.controller.talk_stop,
            is_down=self._is_down,
            active=lambda: self.engine_settings().enabled,
        )
        api.on_turn(self.controller.on_turn)
        api.on_event(self.controller.on_event)
        api.on_startup(self.start)
        api.on_shutdown(self.stop)
        # The audio placeholders: another add-on can reuse these back-ends.
        api.audio.register_capture("sounddevice", lambda **kw: Recorder(**kw))
        api.audio.register_playback("sounddevice", lambda **kw: Player(**kw))

    # ---- lifecycle ---------------------------------------------------------
    def start(self) -> None:
        assert self.controller is not None and self.panel is not None and self.watcher is not None
        try:
            self.panel.start()
        except OSError:
            log.exception("voice: the panel could not start")
        self._sync_hotkey()
        if hotkey.supported() or self._is_down is not None:
            self.watcher.start()
        self._reaper = threading.Thread(
            target=self._reap, name="chatforge-voice-reaper", daemon=True
        )
        self._reaper.start()
        log.info("voice add-on ready (enabled=%s)", self.engine_settings().enabled)

    def _reap(self) -> None:
        beat = 0
        while not self._stop.wait(1.0):
            beat += 1
            try:
                self._sync_hotkey()
                if beat % 5 == 0 and self.controller is not None:
                    self.controller.maintain()
            except Exception:  # noqa: BLE001
                log.exception("voice: housekeeping failed")

    def _sync_hotkey(self) -> None:
        s = self.engine_settings()
        if self.watcher is not None and s.hotkey != self._spec:
            self._spec = s.hotkey
            if not self.watcher.set_spec(s.hotkey):
                log.warning("voice: %s", self.watcher.error)

    def stop(self) -> None:
        self._stop.set()
        if self.watcher is not None:
            self.watcher.stop()
        if self.controller is not None:
            self.controller.shutdown()
        if self.panel is not None:
            self.panel.stop()
        hostlink.reset()


def cli_data_dir() -> Path:
    """The add-on's data folder as ChatForge computes it (``<home>/extensions/voice``), for the CLI."""
    import os

    explicit = os.environ.get("CHATFORGE_VOICE_DATA_DIR", "").strip()
    if explicit:
        return Path(explicit).expanduser()
    try:
        from chatforge.paths import app_home

        home = app_home()
    except Exception:  # noqa: BLE001 - ChatForge not importable from this environment
        if os.name == "nt":
            base = os.environ.get("LOCALAPPDATA") or str(Path.home() / "AppData" / "Local")
            home = Path(base) / "ChatForge"
        else:
            home = (
                Path(os.environ.get("XDG_DATA_HOME") or Path.home() / ".local" / "share")
                / "ChatForge"
            )
    return home / "extensions" / "voice"
