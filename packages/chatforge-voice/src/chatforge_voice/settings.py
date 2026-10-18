"""The add-on's settings: one declarative table, the engines' view of it, and the guard rails.

The table is ``ENGINE_SETTINGS`` (the rows forge-voice-core's engines read) plus this add-on's own
rows, wrapped in a ``Schema`` with the ``CHATFORGE_VOICE_`` environment prefix. Values come from

1. the environment (``CHATFORGE_VOICE_*``), then
2. ChatForge's ``[extensions.voice]`` table in ``config.toml`` (what the Settings card saves), then
3. the default.

Only the CPU, push-to-talk path exists in this release. The rows for hands-free listening and for a
remote speech server are real (so a config file that names them is understood), but they are not
acted on: :func:`unsupported` turns them into one clear sentence each, and the engine settings
object is always built for this computer.
"""

from __future__ import annotations

import os
from collections.abc import Mapping
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any

from forge_voice_core import settings_schema as S
from forge_voice_core.engine_settings import EngineSettings

from . import messages

ENV_PREFIX = "CHATFORGE_VOICE_"
DEFAULT_HOTKEY = "Ctrl+Alt+Space"
MODES = ("push_to_talk", "hands_free")
BACKENDS = ("cpu", "speechforge")
SPEAK_MODES = ("voice_only", "always", "off")


def _cpu_default(cap: int) -> int:
    """A thread count that leaves the rest of a small laptop to the chat model."""
    return max(2, min(cap, (os.cpu_count() or 4) // 2))


def _own(id_: str, type_: str, default: Any, **kw: Any) -> S.Setting:
    kw.setdefault("group", "system")
    return S.Setting(id_, type_, default, **kw)


OWN_ROWS: tuple[S.Setting, ...] = (
    _own(
        "enabled", "bool", False, level="basic", env=(ENV_PREFIX + "ENABLED",),
        label="Voice (CPU)", help="Talk to ChatForge and hear it answer, all on this computer's CPU.",
    ),
    _own(
        "mode", "enum", "push_to_talk", level="basic", choices=MODES, env=(ENV_PREFIX + "MODE",),
        label="Mode", help="Only push-to-talk exists in this release; hands-free is planned.",
    ),
    _own(
        "backend", "enum", "cpu", level="basic", choices=BACKENDS, env=(ENV_PREFIX + "BACKEND",),
        label="Where speech runs", help="Only this computer's CPU in this release; SpeechForge is planned.",
    ),
    _own(
        "hotkey", "str", DEFAULT_HOTKEY, level="basic", max_len=64, env=(ENV_PREFIX + "HOTKEY",),
        group="input", label="Push-to-talk key (hold)",
        help="Hold it while you speak, release to send. Modifiers plus one key, e.g. Ctrl+Alt+Space or F9.",
    ),
    _own(
        "speak_replies", "enum", "voice_only", level="basic", choices=SPEAK_MODES,
        env=(ENV_PREFIX + "SPEAK_REPLIES",), group="tts", label="Speak replies",
        help="voice_only: read out the answers to what you said; always: every answer; off: never.",
    ),
    _own(
        "max_record_s", "float", 30.0, lo=3.0, hi=120.0, env=(ENV_PREFIX + "MAX_RECORD_S",),
        group="input", label="Longest recording (s)", help="Recording stops by itself after this long.",
    ),
    _own(
        "input_device", "str", "", max_len=128, env=(ENV_PREFIX + "INPUT_DEVICE",), group="input",
        label="Microphone", help="Part of a device name; empty is the system default.",
    ),
    _own(
        "output_device", "str", "", max_len=128, env=(ENV_PREFIX + "OUTPUT_DEVICE",), group="input",
        label="Speaker", help="Part of a device name; empty is the system default.",
    ),
    _own(
        "idle_unload_minutes", "int", 10, lo=0, hi=240, env=(ENV_PREFIX + "IDLE_UNLOAD_MINUTES",),
        label="Unload models after (min)", help="Frees about 3 GB of RAM when idle; 0 keeps them loaded.",
    ),
)

#: ``threads`` / ``tts_threads`` are environment-only rows in the engine table; here they are also
#: read from the file, with defaults that fit a laptop rather than the many-core box they were tuned on.
_ENGINE_ROWS = tuple(
    replace(r, store="file", default=_cpu_default(6 if r.id == "threads" else 8))
    if r.id in ("threads", "tts_threads")
    else r
    for r in S.ENGINE_SETTINGS
)

SCHEMA = S.Schema(_ENGINE_ROWS + OWN_ROWS, env_prefix=ENV_PREFIX)

#: The keys the ChatForge Settings card shows (the rest are reachable from config.toml / the environment).
CARD_KEYS = ("enabled", "hotkey", "speak_replies", "speed", "max_record_s", "input_device", "output_device")


@dataclass(frozen=True)
class VoiceEngineSettings(EngineSettings):
    """What forge-voice-core's engines get: its defaults, this add-on's values, and the CPU only."""

    enabled: bool = False
    mode: str = "push_to_talk"
    backend: str = "cpu"
    hotkey: str = DEFAULT_HOTKEY
    speak_replies: str = "voice_only"
    max_record_s: float = 30.0
    input_device: str = ""
    output_device: str = ""
    idle_unload_minutes: int = 10
    #: Sentences saying why a requested feature is not acted on (see :func:`unsupported`).
    notices: tuple[str, ...] = ()

    @property
    def switched_on(self) -> bool:
        return self.enabled

    @property
    def forced(self) -> str | None:
        return None


def unsupported(values: Mapping[str, Any]) -> list[str]:
    """Why something the configuration asks for is not done in this release (empty = nothing)."""
    out: list[str] = []
    if values.get("mode", "push_to_talk") != "push_to_talk":
        out.append(messages.NOT_IN_THIS_RELEASE_HANDS_FREE)
    remote = (
        values.get("backend", "cpu") != "cpu"
        or values.get("stt_provider", "cpu") != "cpu"
        or values.get("tts_provider", "auto") not in ("auto", "cpu")
        or values.get("tts_engine", "auto") not in ("auto", "chatterbox")
        or bool(values.get("hq_url"))
        or bool(values.get("rig.enabled"))
    )
    if remote:
        out.append(messages.NOT_IN_THIS_RELEASE_REMOTE)
    return out


def resolve(prefs: Mapping[str, Any] | None = None, environ: Mapping[str, str] | None = None) -> dict:
    """Every setting's value: environment, then ``prefs`` (``[extensions.voice]``), then default."""
    environ = os.environ if environ is None else environ
    clean = SCHEMA.clean_prefs(dict(prefs or {}))
    values, _sources = SCHEMA.resolve(clean, environ)
    # The schema lets the host's file decide ``enabled`` (DisPatch has a separate force flag); here the
    # environment is the operator's override and simply wins.
    ev, _name = S.env_value(SCHEMA.setting("enabled"), environ)
    if ev is not S.MISSING:
        values["enabled"] = bool(ev)
    return values


def build(values: Mapping[str, Any], models_dir: Path | None = None) -> VoiceEngineSettings:
    """The engines' settings object for resolved ``values`` (always local: see :func:`unsupported`)."""
    kw: dict[str, Any] = {
        "stt_provider": "cpu",
        "tts_provider": "cpu",
        "tts_engine": "auto",
        "stt_engine": "parakeet",
        "hq_url": "",
        "stt_url": "",
        "threads": int(values.get("threads", _cpu_default(6))),
        "tts_threads": int(values.get("tts_threads", _cpu_default(8))),
        "speed": float(values.get("speed", 1.05)),
        "builtin_voice": str(values.get("builtin_voice", "") or ""),
        "enabled": bool(values.get("enabled", False)),
        "mode": str(values.get("mode", "push_to_talk")),
        "backend": str(values.get("backend", "cpu")),
        "hotkey": str(values.get("hotkey") or DEFAULT_HOTKEY),
        "speak_replies": str(values.get("speak_replies", "voice_only")),
        "max_record_s": float(values.get("max_record_s", 30.0)),
        "input_device": str(values.get("input_device", "") or ""),
        "output_device": str(values.get("output_device", "") or ""),
        "idle_unload_minutes": int(values.get("idle_unload_minutes", 10)),
        "notices": tuple(unsupported(values)),
    }
    if models_dir is not None:
        kw["models_dir"] = Path(models_dir)
    return VoiceEngineSettings(**kw)  # rt and rig keep their defaults: the rig is off


def from_prefs(prefs: Mapping[str, Any] | None, environ: Mapping[str, str] | None = None) -> VoiceEngineSettings:
    """``prefs`` (ChatForge's ``api.settings``) to the engines' settings."""
    from forge_voice_core import models

    return build(resolve(prefs, environ), models.speech_dir())
