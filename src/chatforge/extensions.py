"""Add-on seams: the ``chatforge.extensions`` entry-point group and the :class:`ExtensionAPI`.

An add-on (a voice package, for instance) is a separate wheel that declares::

    [project.entry-points."chatforge.extensions"]
    voice = "chatforge_voice:setup"

ChatForge calls ``setup(api)`` once, at start, with an :class:`ExtensionAPI` bound to that
add-on's name. Through it the add-on can

* ``register_settings_section()``: a card in Settings > General, stored under
  ``[extensions.<name>]`` in ``config.toml`` (and ``CHATFORGE_EXTENSIONS__<NAME>__<KEY>`` in the
  environment, like every other setting);
* ``on_turn()``: hear each finished turn (:class:`TurnInfo`);
* ``on_event()``: hear every event the page hears (``chat.delta`` for streaming speech);
* ``on_startup()`` / ``on_shutdown()``: acquire and release resources;
* ``chat.send()`` / ``chat.cancel()``: speak *into* the chat (the same path as typing);
* ``audio``: placeholder registries for capture and playback back-ends. ChatForge itself
  has no audio stack; an add-on that has one registers it here so another add-on can use it.

A broken add-on is logged and skipped. Nothing in this module imports the GUI, so it is
tested without a window. ``docs/extensions.md`` is the add-on author's guide.
"""

from __future__ import annotations

import contextlib
import os
import re
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass, field
from importlib import metadata
from pathlib import Path
from typing import Any, Literal

from chatforge.logging_setup import get_logger

log = get_logger(__name__)

ENTRY_POINT_GROUP = "chatforge.extensions"
#: Bumped when ``ExtensionAPI`` changes incompatibly. An add-on may declare ``API_VERSION``
#: (an int) on its entry-point object; a different value is refused.
API_VERSION = 1
#: ``CHATFORGE_NO_EXTENSIONS=1`` starts ChatForge without loading any add-on (safe mode).
DISABLE_ENV = "CHATFORGE_NO_EXTENSIONS"

_NAME_RE = re.compile(r"^[a-z][a-z0-9_-]{0,31}$")
_KEY_RE = re.compile(r"^[a-z][a-z0-9_]{0,47}$")

FieldType = Literal["bool", "int", "float", "text", "choice"]


class ExtensionError(Exception):
    """An add-on called the API wrongly (bad name, duplicate key, invalid value)."""


# --------------------------------------------------------------------------- #
# Settings sections
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class SettingField:
    """One setting of an add-on. ``key`` is the name in ``[extensions.<name>]``."""

    key: str
    label: str
    type: FieldType = "text"
    default: Any = None
    choices: tuple[str, ...] = ()
    minimum: float | None = None
    maximum: float | None = None
    help: str = ""

    def __post_init__(self) -> None:
        if not _KEY_RE.match(self.key):
            raise ExtensionError(f"setting key {self.key!r} must be lowercase letters, digits, _")
        if self.type == "choice" and not self.choices:
            raise ExtensionError(f"setting {self.key!r}: a choice needs choices")

    def coerce(self, value: Any) -> Any:
        """``value`` as this field's type, or raise :class:`ExtensionError`."""
        try:
            if self.type == "bool":
                if isinstance(value, str):
                    low = value.strip().lower()
                    if low in ("1", "true", "yes", "on"):
                        return True
                    if low in ("0", "false", "no", "off", ""):
                        return False
                    raise ValueError(value)
                return bool(value)
            if self.type in ("int", "float"):
                if isinstance(value, bool):
                    raise ValueError(value)
                number = int(value) if self.type == "int" else float(value)
                if self.minimum is not None and number < self.minimum:
                    raise ValueError(f"at least {self.minimum:g}")
                if self.maximum is not None and number > self.maximum:
                    raise ValueError(f"at most {self.maximum:g}")
                return number
            text = "" if value is None else str(value)
            if self.type == "choice" and text not in self.choices:
                raise ValueError(f"one of {', '.join(self.choices)}")
            return text
        except (TypeError, ValueError) as exc:
            raise ExtensionError(f"{self.label}: {exc}") from None

    def to_dict(self) -> dict[str, Any]:
        return {
            "key": self.key,
            "label": self.label,
            "type": self.type,
            "default": self.default,
            "choices": list(self.choices),
            "min": self.minimum,
            "max": self.maximum,
            "help": self.help,
        }


@dataclass(frozen=True)
class SettingsSection:
    extension: str
    id: str
    title: str
    fields: tuple[SettingField, ...]
    description: str = ""

    def defaults(self) -> dict[str, Any]:
        return {f.key: f.default for f in self.fields}

    def to_dict(self) -> dict[str, Any]:
        return {
            "extension": self.extension,
            "id": self.id,
            "title": self.title,
            "description": self.description,
            "fields": [f.to_dict() for f in self.fields],
        }


# --------------------------------------------------------------------------- #
# Turns, audio, chat
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class TurnInfo:
    """A finished turn, as ``on_turn`` callbacks receive it."""

    request_id: str
    #: What the user typed ("" for a regenerated turn).
    user_text: str
    #: The reply's final visible text ("" for a failed or stopped turn).
    reply_text: str
    provider: str
    model: str
    ok: bool
    #: ``chat.error`` code when ``ok`` is false (``"cancelled"`` for a stop).
    error_code: str | None = None


class AudioBackends:
    """Placeholder registries for capture and playback. ChatForge ships no backend.

    ``register_capture("name", factory)``: ``factory()`` returns an object the add-on defines
    (frames in, text out, ...). The contract is intentionally empty today: the first add-on to
    ship audio decides it, and this class is where a second add-on finds it.
    """

    def __init__(self) -> None:
        self._capture: dict[str, Callable[..., Any]] = {}
        self._playback: dict[str, Callable[..., Any]] = {}

    def register_capture(self, name: str, factory: Callable[..., Any]) -> None:
        self._capture[str(name)] = factory

    def register_playback(self, name: str, factory: Callable[..., Any]) -> None:
        self._playback[str(name)] = factory

    def capture_backends(self) -> list[str]:
        return sorted(self._capture)

    def playback_backends(self) -> list[str]:
        return sorted(self._playback)

    def create_capture(self, name: str, *args: Any, **kwargs: Any) -> Any:
        try:
            return self._capture[name](*args, **kwargs)
        except KeyError:
            raise ExtensionError(f"no capture back-end named {name!r}") from None

    def create_playback(self, name: str, *args: Any, **kwargs: Any) -> Any:
        try:
            return self._playback[name](*args, **kwargs)
        except KeyError:
            raise ExtensionError(f"no playback back-end named {name!r}") from None


class ChatPort:
    """Send text into the chat and stop a reply, as the popup does. Bound by the app."""

    def __init__(self) -> None:
        self._send: Callable[[str], str | None] | None = None
        self._cancel: Callable[[str], None] | None = None

    def bind(self, send: Callable[[str], str | None], cancel: Callable[[str], None]) -> None:
        self._send, self._cancel = send, cancel

    def send(self, text: str) -> str | None:
        """Send ``text`` as the user's message; returns the request id (``None``: refused)."""
        if self._send is None:
            raise ExtensionError("the chat is not running yet")
        return self._send(str(text))

    def cancel(self, request_id: str) -> None:
        if self._cancel is not None:
            self._cancel(str(request_id))


# --------------------------------------------------------------------------- #
# The API an add-on sees
# --------------------------------------------------------------------------- #


class ExtensionAPI:
    """What ``setup(api)`` receives. One instance per add-on."""

    api_version = API_VERSION

    def __init__(self, name: str, host: ExtensionHost) -> None:
        self.name = name
        self._host = host
        self.log = get_logger(f"chatforge.extensions.{name}")
        self.chat = host.chat
        self.audio = host.audio

    @property
    def app_version(self) -> str:
        return self._host.app_version

    @property
    def data_dir(self) -> Path:
        """A folder the add-on may write to (``<ChatForge home>/extensions/<name>``)."""
        path = self._host.home / "extensions" / self.name
        path.mkdir(parents=True, exist_ok=True)
        return path

    @property
    def settings(self) -> dict[str, Any]:
        """The add-on's current settings: registered defaults, then ``config.toml``/env."""
        return self._host.settings_for(self.name)

    def register_settings_section(
        self,
        fields: Iterable[SettingField],
        *,
        title: str,
        section_id: str = "main",
        description: str = "",
    ) -> None:
        self._host.add_section(self.name, section_id, title, tuple(fields), description)

    def on_turn(self, callback: Callable[[TurnInfo], None]) -> None:
        self._host.turn_callbacks.append((self.name, callback))

    def on_event(self, callback: Callable[[dict[str, Any]], None]) -> None:
        self._host.event_callbacks.append((self.name, callback))

    def on_startup(self, callback: Callable[[], None]) -> None:
        self._host.startup_callbacks.append((self.name, callback))

    def on_shutdown(self, callback: Callable[[], None]) -> None:
        self._host.shutdown_callbacks.append((self.name, callback))


@dataclass
class LoadedExtension:
    name: str
    ok: bool
    version: str = ""
    error: str = ""


@dataclass
class ExtensionHost:
    """Loads add-ons and fans events out to them. One per app."""

    app_version: str
    get_config: Callable[[], Any]
    home: Path
    chat: ChatPort = field(default_factory=ChatPort)
    audio: AudioBackends = field(default_factory=AudioBackends)
    loaded: list[LoadedExtension] = field(default_factory=list)
    sections: list[SettingsSection] = field(default_factory=list)
    turn_callbacks: list[tuple[str, Callable[[TurnInfo], None]]] = field(default_factory=list)
    event_callbacks: list[tuple[str, Callable[[dict[str, Any]], None]]] = field(
        default_factory=list
    )
    startup_callbacks: list[tuple[str, Callable[[], None]]] = field(default_factory=list)
    shutdown_callbacks: list[tuple[str, Callable[[], None]]] = field(default_factory=list)

    # ------------------------------------------------------------------ loading

    def load(self, entry_points: Iterable[Any] | None = None) -> list[LoadedExtension]:
        """Call ``setup(api)`` of every installed add-on. Never raises."""
        if os.environ.get(DISABLE_ENV, "").strip().lower() in ("1", "true", "yes"):
            log.info("extensions_disabled", env=DISABLE_ENV)
            return self.loaded
        if entry_points is None:
            try:
                entry_points = metadata.entry_points(group=ENTRY_POINT_GROUP)
            except Exception as exc:  # noqa: BLE001
                log.warning("extensions_scan_failed", error=type(exc).__name__)
                return self.loaded
        for ep in entry_points:
            self._load_one(ep)
        return self.loaded

    def _load_one(self, ep: Any) -> None:
        name = str(getattr(ep, "name", ""))
        if not _NAME_RE.match(name):
            self.loaded.append(LoadedExtension(name, False, error="invalid extension name"))
            log.warning("extension_skipped", name=name, reason="invalid name")
            return
        try:
            target = ep.load()
            wanted = getattr(target, "API_VERSION", API_VERSION)
            if wanted != API_VERSION:
                raise ExtensionError(
                    f"needs extension API {wanted}, this ChatForge has {API_VERSION}"
                )
            setup = getattr(target, "setup", target)
            if not callable(setup):
                raise ExtensionError("the entry point is not callable and has no setup()")
            api = ExtensionAPI(name, self)
            setup(api)
        except Exception as exc:  # noqa: BLE001 - an add-on never takes the app down
            self._forget(name)
            self.loaded.append(LoadedExtension(name, False, error=str(exc)[:300]))
            log.warning(
                "extension_failed", name=name, error=type(exc).__name__, message=str(exc)[:200]
            )
            return
        dist = getattr(ep, "dist", None)
        version = str(getattr(dist, "version", "") or "")
        self.loaded.append(LoadedExtension(name, True, version=version))
        log.info("extension_loaded", name=name, version=version)

    def _forget(self, name: str) -> None:
        """Remove what a half-loaded add-on registered."""
        self.sections[:] = [s for s in self.sections if s.extension != name]
        for registry in (
            self.turn_callbacks,
            self.event_callbacks,
            self.startup_callbacks,
            self.shutdown_callbacks,
        ):
            registry[:] = [c for c in registry if c[0] != name]

    # ------------------------------------------------------------------ settings

    def add_section(
        self,
        extension: str,
        section_id: str,
        title: str,
        fields: tuple[SettingField, ...],
        description: str,
    ) -> None:
        if not _KEY_RE.match(section_id):
            raise ExtensionError(f"section id {section_id!r} must be lowercase letters, digits, _")
        if not title.strip():
            raise ExtensionError("a settings section needs a title")
        taken = {f.key for s in self.sections if s.extension == extension for f in s.fields}
        for f in fields:
            if f.key in taken:
                raise ExtensionError(f"setting key {f.key!r} is already registered")
            taken.add(f.key)
        if any(s.extension == extension and s.id == section_id for s in self.sections):
            raise ExtensionError(f"section {section_id!r} is already registered")
        self.sections.append(
            SettingsSection(extension, section_id, title.strip(), fields, description)
        )

    def fields_of(self, extension: str) -> dict[str, SettingField]:
        return {f.key: f for s in self.sections if s.extension == extension for f in s.fields}

    def settings_for(self, extension: str) -> dict[str, Any]:
        """Defaults overlaid with the ``[extensions.<name>]`` table (values coerced; a value
        that does not fit its field falls back to the default)."""
        fields = self.fields_of(extension)
        stored: Mapping[str, Any] = {}
        with contextlib.suppress(Exception):
            stored = self.get_config().extensions.get(extension, {}) or {}
        out: dict[str, Any] = {k: f.default for k, f in fields.items()}
        for key, value in stored.items():
            f = fields.get(key)
            if f is None:
                out[key] = value  # an add-on may keep keys it did not register
                continue
            try:
                out[key] = f.coerce(value)
            except ExtensionError:
                log.warning("extension_setting_invalid", extension=extension, key=key)
        return out

    def validate_values(self, extension: str, values: Mapping[str, Any]) -> dict[str, Any]:
        """``values`` checked against the registered fields: unknown keys and bad values raise
        :class:`ExtensionError`; the result is ready for ``update_config``."""
        fields = self.fields_of(extension)
        if not fields:
            raise ExtensionError(f"{extension!r} has no settings")
        clean: dict[str, Any] = {}
        for key, value in values.items():
            f = fields.get(key)
            if f is None:
                raise ExtensionError(f"unknown setting {key!r}")
            clean[key] = f.coerce(value)
        return clean

    def view(self) -> list[dict[str, Any]]:
        """The Settings page's data: each add-on, its sections, the current values."""
        names = {e.name for e in self.loaded if e.ok}
        out = []
        for ext in self.loaded:
            sections = [s.to_dict() for s in self.sections if s.extension == ext.name]
            out.append(
                {
                    "name": ext.name,
                    "ok": ext.ok,
                    "version": ext.version,
                    "error": ext.error,
                    "sections": sections,
                    "values": self.settings_for(ext.name) if ext.name in names else {},
                }
            )
        return out

    # ------------------------------------------------------------------ dispatch

    @property
    def wants_events(self) -> bool:
        return bool(self.event_callbacks)

    def dispatch_event(self, event: dict[str, Any]) -> None:
        for name, cb in list(self.event_callbacks):
            self._guarded(name, "on_event", cb, event)

    def dispatch_turn(self, turn: TurnInfo) -> None:
        for name, cb in list(self.turn_callbacks):
            self._guarded(name, "on_turn", cb, turn)

    def startup(self) -> None:
        for name, cb in list(self.startup_callbacks):
            self._guarded(name, "on_startup", cb)

    def shutdown(self) -> None:
        for name, cb in reversed(list(self.shutdown_callbacks)):
            self._guarded(name, "on_shutdown", cb)

    @staticmethod
    def _guarded(name: str, what: str, cb: Callable[..., Any], *args: Any) -> None:
        try:
            cb(*args)
        except Exception as exc:  # noqa: BLE001 - an add-on's bug is not the chat's
            log.warning("extension_callback_failed", name=name, hook=what, error=type(exc).__name__)
