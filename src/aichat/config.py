"""AI Chat configuration: pydantic-settings models, TOML load, atomic save, live updates.

# Adapted from StudioForge src/studioforge/config.py (load/validate shape) (MIT, LaserLloyd)
# base_url rules follow DisPatch backend/app/llm_api.py `normalize_base_url`.

``ProviderSpec`` and ``Timeouts`` live here (not in ``llm/``) so ``llm.*`` imports them
from ``aichat.config`` without a backwards dependency.

Layers, highest priority first: init kwargs, environment (``AICHAT_<SECTION>__<KEY>``),
``config.toml``, defaults. The file never holds secrets.
"""

from __future__ import annotations

import contextlib
import contextvars
import copy
import logging
import os
import re
import time
import tomllib
from pathlib import Path
from typing import Any, Literal, get_args, get_origin
from urllib.parse import urlparse, urlunparse

import tomli_w
from pydantic import BaseModel, Field, ValidationError, field_validator, model_validator
from pydantic.fields import FieldInfo
from pydantic_settings import (
    BaseSettings,
    PydanticBaseSettingsSource,
    SettingsConfigDict,
)

from aichat.errors import ConfigError
from aichat.paths import Paths

_log = logging.getLogger(__name__)

SCHEMA_VERSION = 3

#: Changes to these keys need the local model reloaded ("reload required").
RESTART_KEYS: tuple[str, ...] = (
    "local.device",
    "local.max_prompt_len",
    "local.extra_args",
    "local.ovms_variant",
)

_API_KEY_ENV_RE = re.compile(r"^[A-Z][A-Z0-9_]{0,63}$")
_PROVIDER_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,63}$")
_THEME_RE = re.compile(r"^[a-z0-9][a-z0-9-]{0,63}$")


# --------------------------------------------------------------------------- #
# Validation helpers
# --------------------------------------------------------------------------- #


def normalize_base_url(url: str) -> str:
    """Validate and tidy a provider base URL; raises ``ValueError``.

    # Adapted from DisPatch backend/app/llm_api.py `normalize_base_url` (MIT, LaserLloyd)

    Only the scheme is policed, not the destination: pointing the app at any host the user
    chose is the feature. ``file://`` and a bare ``localhost:1234`` (which ``urlparse`` reads
    as scheme ``localhost``) are mistakes; embedded credentials (``user:pass@host``) would
    end up in config and logs, so they are refused too.
    """
    text = (url or "").strip().rstrip("/")
    if not text:
        raise ValueError("a base URL is required, e.g. http://127.0.0.1:1234/v1")
    try:
        parsed = urlparse(text)
        parsed.port  # noqa: B018 - raises ValueError on a bad port
    except ValueError as exc:
        raise ValueError(f"not a valid URL: {exc}") from None
    if parsed.scheme not in ("http", "https"):
        raise ValueError(
            "the base URL must start with http:// or https:// "
            f"(got {parsed.scheme or 'no'} scheme; write the http:// prefix out in full)"
        )
    if not parsed.hostname:
        raise ValueError("the base URL has no host")
    if parsed.username is not None or parsed.password is not None or "@" in parsed.netloc:
        raise ValueError("the base URL must not contain a user name or password")
    if parsed.query or parsed.fragment:
        raise ValueError("the base URL must not contain a query string or fragment")
    return urlunparse(parsed._replace(path=parsed.path.rstrip("/")))


_HOTKEY_MODIFIERS = {
    "ctrl": "Ctrl",
    "control": "Ctrl",
    "alt": "Alt",
    "shift": "Shift",
    "win": "Win",
    "super": "Win",
    "meta": "Win",
    "cmd": "Win",
}
_HOTKEY_MOD_ORDER = ("Ctrl", "Alt", "Shift", "Win")
_HOTKEY_NAMED = {
    "space": "Space",
    "enter": "Enter",
    "return": "Enter",
    "tab": "Tab",
    "esc": "Esc",
    "escape": "Esc",
    "backspace": "Backspace",
    "delete": "Delete",
    "del": "Delete",
    "insert": "Insert",
    "ins": "Insert",
    "home": "Home",
    "end": "End",
    "pageup": "PageUp",
    "pagedown": "PageDown",
    "up": "Up",
    "down": "Down",
    "left": "Left",
    "right": "Right",
}
_HOTKEY_PUNCT = set("`-=[]\\;',./")


def parse_hotkey(spec: str) -> tuple[tuple[str, ...], str]:
    """Parse ``"Ctrl+Alt+Space"`` into ``(("Ctrl", "Alt"), "Space")``; raises ``ValueError``.

    At least one modifier (Ctrl, Alt, Shift, Win) plus exactly one key: a letter, digit,
    ``F1``-``F24``, a named key (Space, Enter, Tab, Esc, arrows, ...) or one of
    ``` `-=[]\\;',./ ```. Modifiers come back in canonical order.
    """
    if not isinstance(spec, str) or not spec.strip():
        raise ValueError("hotkey is empty; use a form like Ctrl+Alt+Space")
    parts = [p.strip() for p in spec.split("+")]
    if len(parts) < 2 or any(not p for p in parts):
        raise ValueError(
            f"invalid hotkey {spec!r}; use modifiers plus one key, e.g. Ctrl+Alt+Space"
        )
    *mod_parts, key_part = parts
    mods: set[str] = set()
    for part in mod_parts:
        canon = _HOTKEY_MODIFIERS.get(part.lower())
        if canon is None:
            raise ValueError(f"unknown modifier {part!r} in hotkey; use Ctrl, Alt, Shift or Win")
        mods.add(canon)
    lowered = key_part.lower()
    key: str | None = None
    if lowered in _HOTKEY_MODIFIERS:
        raise ValueError(f"hotkey {spec!r} has no key, only modifiers")
    if lowered in _HOTKEY_NAMED:
        key = _HOTKEY_NAMED[lowered]
    elif re.fullmatch(r"f([1-9]|1[0-9]|2[0-4])", lowered):
        key = lowered.upper()
    elif len(key_part) == 1 and (key_part.isascii() and key_part.isalnum()):
        key = key_part.upper()
    elif len(key_part) == 1 and key_part in _HOTKEY_PUNCT:
        key = key_part
    if key is None:
        raise ValueError(f"unknown key {key_part!r} in hotkey")
    return tuple(m for m in _HOTKEY_MOD_ORDER if m in mods), key


# --------------------------------------------------------------------------- #
# Provider models (shared with llm/*)
# --------------------------------------------------------------------------- #


class Timeouts(BaseModel):
    connect_s: float = Field(default=10, gt=0)
    stall_s: float = Field(default=90, gt=0)
    wall_s: float = Field(default=600, gt=0)


class ProviderSpec(BaseModel):
    id: str
    kind: Literal["ovms", "openai"]
    display_name: str
    base_url: str | None = None
    region: Literal["international", "china", "custom"] | None = None
    api_key_env: str | None = None
    models: list[str] = Field(default_factory=list)
    default_model: str | None = None
    quirks: list[str] = Field(default_factory=list)
    supports_tools: bool = True
    max_output_tokens: int = Field(default=2048, ge=1)
    temperature: float | None = Field(default=None, ge=0, le=2)
    extra_body: dict[str, Any] = Field(default_factory=dict)
    timeouts: Timeouts = Field(default_factory=Timeouts)
    builtin: bool = False
    #: ``None``: a key is required unless the base URL is loopback. ``False``: the key is
    #: optional (a LAN server such as StudioForge that may or may not have one set).
    key_required: bool | None = None
    #: Where to get a key (shown as a link in Settings).
    docs_url: str | None = None
    #: The context window in tokens (prompt + reply) when no per-model value is known.
    #: ``None`` uses the cloud default (``chat.history.CLOUD_CAP_TOKENS``).
    context_tokens: int | None = Field(default=None, ge=2048, le=10_000_000)
    #: Context windows reported by the provider's ``GET /models`` (StudioForge reports the
    #: loaded context of each model), filled by "Refresh models". Wins over
    #: ``context_tokens`` for the models it names.
    model_context: dict[str, int] = Field(default_factory=dict)

    @field_validator("id")
    @classmethod
    def _check_id(cls, v: str) -> str:
        if not _PROVIDER_ID_RE.match(v):
            raise ValueError("provider id must be 1-64 letters, digits, '-' or '_'")
        return v

    @field_validator("display_name")
    @classmethod
    def _check_display_name(cls, v: str) -> str:
        v = v.strip()
        if not v:
            raise ValueError("display name must not be empty")
        return v

    @field_validator("base_url")
    @classmethod
    def _check_base_url(cls, v: str | None) -> str | None:
        if v is None or not v.strip():
            return None
        return normalize_base_url(v)

    @field_validator("api_key_env")
    @classmethod
    def _check_api_key_env(cls, v: str | None) -> str | None:
        if v is None or v == "":
            return None
        if not _API_KEY_ENV_RE.match(v):
            raise ValueError("api_key_env must match ^[A-Z][A-Z0-9_]{0,63}$")
        return v

    @field_validator("docs_url")
    @classmethod
    def _check_docs_url(cls, v: str | None) -> str | None:
        if v is None or not v.strip():
            return None
        v = v.strip()
        if not v.startswith(("https://", "http://")):
            raise ValueError("docs_url must be an http(s) URL")
        return v


#: StudioForge (an OpenAI-compatible GPU LLM server) on this PC by default; point it at the GPU box in Settings → Providers.
STUDIOFORGE_DEFAULT_URL = "http://localhost:1234/v1"


# Adapted from DisPatch backend/app/llm_api.py ``PROVIDERS`` (MIT, LaserLloyd): the OpenAI
# and DeepSeek presets (base URL, key env var, models, key docs link).
def seed_providers() -> dict[str, ProviderSpec]:
    """The providers every config starts with (PLAN 1.7), as fresh copies.
    StudioForge is the owner's own OpenAI-compatible GPU server."""
    return {
        "local-npu": ProviderSpec(
            id="local-npu",
            kind="ovms",
            display_name="Local (NPU)",
            builtin=True,
            quirks=["ovms"],
        ),
        "minimax": ProviderSpec(
            id="minimax",
            kind="openai",
            display_name="MiniMax",
            region="international",
            base_url="https://api.minimax.io/v1",
            api_key_env="MINIMAX_API_KEY",
            models=[
                "MiniMax-M3",
                "MiniMax-M2.7-highspeed",
                "MiniMax-M2.7",
                "MiniMax-M3.1-Flash-Preview",
            ],
            default_model="MiniMax-M3",
            quirks=["minimax"],
            supports_tools=True,
            max_output_tokens=4096,
            temperature=1.0,
            extra_body={"reasoning_split": True},
            timeouts=Timeouts(connect_s=10, stall_s=90, wall_s=600),
            # MiniMax's models take up to 1M tokens of context.
            context_tokens=1_000_000,
        ),
        "studioforge": ProviderSpec(
            id="studioforge",
            kind="openai",
            display_name="StudioForge",
            base_url=STUDIOFORGE_DEFAULT_URL,
            api_key_env="STUDIOFORGE_API_KEY",
            models=["unsloth/Qwen3.8-27B-GGUF/Qwen3.8-27B-Q5_K_S"],
            default_model="unsloth/Qwen3.8-27B-GGUF/Qwen3.8-27B-Q5_K_S",
            supports_tools=True,
            max_output_tokens=4096,
            # The first request may load a 20+ GB model into VRAM.
            timeouts=Timeouts(connect_s=10, stall_s=300, wall_s=900),
            key_required=False,
        ),
        "openai": ProviderSpec(
            id="openai",
            kind="openai",
            display_name="OpenAI",
            base_url="https://api.openai.com/v1",
            api_key_env="OPENAI_API_KEY",
            models=["gpt-4o-mini", "gpt-4o"],
            default_model="gpt-4o-mini",
            quirks=["openai"],
            supports_tools=True,
            max_output_tokens=4096,
            docs_url="https://platform.openai.com/api-keys",
        ),
        "deepseek": ProviderSpec(
            id="deepseek",
            kind="openai",
            display_name="DeepSeek",
            base_url="https://api.deepseek.com/v1",
            api_key_env="DEEPSEEK_API_KEY",
            models=["deepseek-chat", "deepseek-reasoner"],
            default_model="deepseek-chat",
            quirks=["deepseek"],
            supports_tools=True,
            max_output_tokens=4096,
            docs_url="https://platform.deepseek.com/api_keys",
        ),
    }


# --------------------------------------------------------------------------- #
# Sections
# --------------------------------------------------------------------------- #


def _upper(v: Any) -> Any:
    return v.strip().upper() if isinstance(v, str) else v


#: The v1 default system prompt; a config still carrying it is moved to the new default.
LEGACY_SYSTEM_PROMPT = (
    "You are AI Chat, a concise desktop assistant. Answer briefly. Use tools only when "
    "they help (current facts, web pages, dates, arithmetic)."
)
#: The persona only. The tool guidance (live internet access, "never say you cannot look it
#: up") is added by ``chat.prompts`` when tools are actually offered, so a model without
#: tools is never told it can reach the internet.
DEFAULT_SYSTEM_PROMPT = (
    "You are AI Chat, a concise desktop assistant. Answer briefly, and work things out step "
    "by step when a question needs it."
)
#: Earlier defaults that a migration (or the next save) moves to the current default.
OLD_DEFAULT_PROMPTS: frozenset[str] = frozenset(
    {
        LEGACY_SYSTEM_PROMPT,
        "You are AI Chat, a concise desktop assistant with live internet access through your "
        "tools. Answer briefly. Work things out step by step with your tools when a question "
        "needs it, and never claim you cannot access current information.",
    }
)


class ChatCfg(BaseModel):
    provider: str = "local-npu"
    model: str = "OpenVINO/Qwen2.5-1.5B-Instruct-int4-ov"
    #: The personality: who the assistant is and how it talks (Settings → General).
    system_prompt: str = Field(default=DEFAULT_SYSTEM_PROMPT, max_length=20_000)
    #: Standing instructions from the user: facts about them, preferences, rules ("answer
    #: in metric", "I work in IT"). Sent after the personality with every message.
    instructions: str = Field(default="", max_length=20_000)
    #: The composer limit for one message. Large contexts (MiniMax: 1M tokens, about 4M
    #: characters) make long pastes and attached files reasonable.
    max_prompt_chars: int = Field(default=4000, ge=1, le=4_000_000)
    max_tool_rounds: int = Field(default=6, ge=0, le=20)
    #: When the local model fails (load error, crash, timeout), re-run the message on this
    #: provider ("" = no fallback). ``fallback_model`` "" uses the provider's default model.
    fallback_provider: str = ""
    fallback_model: str = ""
    #: Re-read the remote providers' model lists (``GET /models``) once a day.
    auto_refresh_models: bool = True

    @field_validator("system_prompt")
    @classmethod
    def _current_default(cls, v: str) -> str:
        """An unedited earlier default becomes the current default."""
        return DEFAULT_SYSTEM_PROMPT if v in OLD_DEFAULT_PROMPTS else v

    temperature: float = Field(default=0.7, ge=0, le=2)
    max_output_tokens: int = Field(default=1024, ge=1, le=131_072)
    show_reasoning: Literal["collapsed", "hidden"] = "collapsed"
    persist_conversation: bool = True


class LocalCfg(BaseModel):
    device: Literal["NPU", "GPU", "CPU"] = "NPU"
    idle_unload_minutes: int = Field(default=10, ge=0)
    max_prompt_len: int = Field(default=4096, ge=1024, le=8192)
    enable_thinking: bool = False
    autoload_on_open: bool = True
    load_timeout_s: int = Field(default=900, ge=1)
    ovms_version: str = "2026.4.0"
    ovms_variant: Literal["python_on", "python_off"] = "python_on"
    extra_args: list[str] = Field(default_factory=list)

    @field_validator("device", mode="before")
    @classmethod
    def _device_upper(cls, v: Any) -> Any:
        return _upper(v)


#: Tools added in schema v2; a v1 config gets them switched on once, by the migration.
TOOLS_ADDED_V2: tuple[str, ...] = (
    "news_search",
    "weather",
    "wikipedia",
    "exchange_rate",
)
#: Tools added in schema v3 (v2 configs were written before ``create_document`` existed).
TOOLS_ADDED_V3: tuple[str, ...] = ("create_document",)


class ToolsCfg(BaseModel):
    enabled: list[str] = Field(
        default_factory=lambda: [
            "web_search",
            "news_search",
            "fetch_url",
            "weather",
            "wikipedia",
            "exchange_rate",
            "current_datetime",
            "calculator",
            "create_document",
        ]
    )
    #: Home location for weather and "near me" questions, e.g. "Lisbon, Portugal".
    location: str = Field(default="", max_length=100)
    units: Literal["metric", "imperial"] = "metric"
    web_search_max_results: int = Field(default=5, ge=1, le=20)
    web_search_min_interval_s: float = Field(default=2.0, ge=0)
    fetch_max_bytes: int = Field(default=1_000_000, ge=1)
    fetch_timeout_s: float = Field(default=10, gt=0)
    tool_result_max_chars_local: int = Field(default=1500, ge=1)
    tool_result_max_chars_cloud: int = Field(default=6000, ge=1)
    block_private_addresses: bool = True
    #: The most text one attached file contributes (longer files are cut when read).
    attachment_max_chars: int = Field(default=200_000, ge=1000, le=4_000_000)
    #: Where ``create_document`` saves files; "" = "AI Chat" in the user's Documents.
    documents_dir: str = Field(default="", max_length=1000)

    @field_validator("documents_dir")
    @classmethod
    def _strip_documents_dir(cls, v: str) -> str:
        return v.strip()


class UiCfg(BaseModel):
    theme: str = "laserlloyd"
    hotkey: str = "Ctrl+Alt+C"
    hide_on_blur: bool = True
    #: Bring the hidden popup back (without taking the focus) when a reply finishes.
    show_on_reply: bool = True
    width: int = Field(default=420, ge=200, le=4000)  # logical px
    height: int = Field(default=620, ge=200, le=4000)
    margin: int = Field(default=12, ge=0, le=200)

    @field_validator("theme")
    @classmethod
    def _check_theme(cls, v: str) -> str:
        if not _THEME_RE.match(v):
            raise ValueError("theme must be a lowercase name like 'laserlloyd'")
        return v

    @field_validator("hotkey")
    @classmethod
    def _check_hotkey(cls, v: str) -> str:
        mods, key = parse_hotkey(v)
        return "+".join((*mods, key))


class StartupCfg(BaseModel):
    autostart: bool = True


class LogCfg(BaseModel):
    level: Literal["DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"] = "INFO"
    max_bytes: int = Field(default=5_000_000, ge=0)  # 0 = never rotate
    backup_count: int = Field(default=3, ge=1, le=100)

    @field_validator("level", mode="before")
    @classmethod
    def _level_upper(cls, v: Any) -> Any:
        return _upper(v)


class HfCfg(BaseModel):
    endpoint: str = "https://huggingface.co"
    default_author: str = "OpenVINO"

    @field_validator("endpoint")
    @classmethod
    def _check_endpoint(cls, v: str) -> str:
        return normalize_base_url(v)


# --------------------------------------------------------------------------- #
# Root settings
# --------------------------------------------------------------------------- #

#: The raw TOML table for the load in progress; read by :class:`_TomlDictSource`.
_RAW_TOML: contextvars.ContextVar[dict[str, Any] | None] = contextvars.ContextVar(
    "aichat_raw_toml", default=None
)

#: When set, settings are built from the given data alone (no environment, no file).
_PURE: contextvars.ContextVar[bool] = contextvars.ContextVar(
    "aichat_pure_validation", default=False
)


class _TomlDictSource(PydanticBaseSettingsSource):
    """Feeds the already-parsed ``config.toml`` table to pydantic-settings (below env)."""

    def get_field_value(self, field: FieldInfo, field_name: str) -> tuple[Any, str, bool]:
        return None, field_name, False

    def __call__(self) -> dict[str, Any]:
        return copy.deepcopy(_RAW_TOML.get() or {})


def _deep_merge(base: dict[str, Any], over: dict[str, Any]) -> dict[str, Any]:
    out = dict(base)
    for key, value in over.items():
        if isinstance(value, dict) and isinstance(out.get(key), dict):
            out[key] = _deep_merge(out[key], value)
        else:
            out[key] = copy.deepcopy(value)
    return out


class AppConfig(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="AICHAT_",
        env_nested_delimiter="__",
        case_sensitive=False,
        extra="ignore",
    )

    schema_version: int = SCHEMA_VERSION
    chat: ChatCfg = Field(default_factory=ChatCfg)
    local: LocalCfg = Field(default_factory=LocalCfg)
    providers: dict[str, ProviderSpec] = Field(default_factory=seed_providers)
    tools: ToolsCfg = Field(default_factory=ToolsCfg)
    ui: UiCfg = Field(default_factory=UiCfg)
    startup: StartupCfg = Field(default_factory=StartupCfg)
    logging: LogCfg = Field(default_factory=LogCfg)
    hf: HfCfg = Field(default_factory=HfCfg)

    @classmethod
    def settings_customise_sources(
        cls,
        settings_cls: type[BaseSettings],
        init_settings: PydanticBaseSettingsSource,
        env_settings: PydanticBaseSettingsSource,
        dotenv_settings: PydanticBaseSettingsSource,
        file_secret_settings: PydanticBaseSettingsSource,
    ) -> tuple[PydanticBaseSettingsSource, ...]:
        if _PURE.get():
            return (init_settings,)
        return (init_settings, env_settings, _TomlDictSource(settings_cls))

    @field_validator("providers", mode="before")
    @classmethod
    def _prepare_providers(cls, value: Any) -> Any:
        """Table key becomes ``id``; a seeded provider's table is merged over its seed."""
        if not isinstance(value, dict):
            return value
        seeds = {pid: spec.model_dump() for pid, spec in seed_providers().items()}
        out: dict[str, Any] = {}
        for pid, spec in value.items():
            if isinstance(spec, dict):
                merged = _deep_merge(seeds.get(pid, {}), spec)
                merged["id"] = pid
                out[pid] = merged
            else:
                out[pid] = spec
        return out

    @field_validator("tools", mode="before")
    @classmethod
    def _strip_location(cls, value: Any) -> Any:
        if isinstance(value, dict) and isinstance(value.get("location"), str):
            value = {**value, "location": " ".join(value["location"].split())}
        return value

    @model_validator(mode="after")
    def _ensure_seeds(self) -> AppConfig:
        for pid, spec in self.providers.items():
            if spec.id != pid:
                raise ValueError(f"providers.{pid}: id {spec.id!r} does not match its key")
        for pid, seed in seed_providers().items():
            if pid not in self.providers:
                self.providers[pid] = seed
        # The local runtime is not a user-managed provider: it can never lose its flag.
        self.providers["local-npu"].builtin = True
        # The fallback must be a remote provider that exists. A stale value (provider
        # removed by hand) is dropped rather than failing the whole config load.
        fb = self.chat.fallback_provider
        if fb and (fb not in self.providers or self.providers[fb].kind == "ovms"):
            _log.warning("chat.fallback_provider %r is not a remote provider; ignoring it", fb)
            self.chat.fallback_provider = ""
            self.chat.fallback_model = ""
        return self

    def get(self, dotted: str) -> Any:
        """Read ``"local.device"``-style keys."""
        node: Any = self
        for part in dotted.split("."):
            node = node[part] if isinstance(node, dict) else getattr(node, part)
        return node


# --------------------------------------------------------------------------- #
# Unknown-key detection
# --------------------------------------------------------------------------- #


def _model_types(annotation: Any) -> tuple[type[BaseModel] | None, type[BaseModel] | None]:
    """``(model, mapping_value_model)`` for a field annotation."""
    if isinstance(annotation, type) and issubclass(annotation, BaseModel):
        return annotation, None
    if get_origin(annotation) is dict:
        args = get_args(annotation)
        if len(args) == 2 and isinstance(args[1], type) and issubclass(args[1], BaseModel):
            return None, args[1]
    return None, None


def _walk_unknown(raw: dict[str, Any], model: type[BaseModel], prefix: str) -> list[str]:
    unknown: list[str] = []
    for key, value in raw.items():
        field = model.model_fields.get(key)
        if field is None:
            unknown.append(f"{prefix}{key}")
            continue
        sub, mapped = _model_types(field.annotation)
        if sub is not None and isinstance(value, dict):
            unknown += _walk_unknown(value, sub, f"{prefix}{key}.")
        elif mapped is not None and isinstance(value, dict):
            for name, item in value.items():
                if isinstance(item, dict):
                    unknown += _walk_unknown(item, mapped, f"{prefix}{key}.{name}.")
    return unknown


def find_unknown_keys(raw: dict[str, Any]) -> list[str]:
    """Dotted names of keys in ``raw`` that no model field accepts (e.g. ``local.port``)."""
    return _walk_unknown(raw, AppConfig, "")


# --------------------------------------------------------------------------- #
# Load / save / update
# --------------------------------------------------------------------------- #


def _errors_of(exc: ValidationError) -> dict[str, str]:
    out: dict[str, str] = {}
    for err in exc.errors():
        loc = ".".join(str(p) for p in err["loc"]) or "config"
        msg = str(err["msg"])
        msg = msg.removeprefix("Value error, ")
        out.setdefault(loc, msg)
    return out


def _config_error(exc: ValidationError, prefix: str = "Invalid settings") -> ConfigError:
    errors = _errors_of(exc)
    first_key, first_msg = next(iter(errors.items()))
    return ConfigError(
        f"{prefix}: {first_key}: {first_msg}",
        hint="Fix the value and try again.",
        details={"errors": errors},
    )


def validate_config(data: dict[str, Any]) -> AppConfig:
    """Validate ``data`` alone: no environment overrides, no file. Raises ``ValidationError``."""
    token = _PURE.set(True)
    try:
        return AppConfig.model_validate(data)
    finally:
        _PURE.reset(token)


def _build(raw: dict[str, Any]) -> AppConfig:
    """Construct settings from a raw TOML table, with env overrides applied on top."""
    token = _RAW_TOML.set(raw)
    try:
        return AppConfig()
    except ValidationError as exc:
        raise _config_error(exc, "Invalid config") from None
    finally:
        _RAW_TOML.reset(token)


def _read_raw(path: Path) -> dict[str, Any]:
    try:
        return tomllib.loads(path.read_text(encoding="utf-8-sig"))
    except tomllib.TOMLDecodeError as exc:
        raise ConfigError(
            f"config.toml is not valid TOML: {exc}",
            hint=f"Fix or delete {path}.",
            details={"errors": {"config": str(exc)}},
        ) from None
    except OSError as exc:
        raise ConfigError(f"Could not read {path}: {exc}") from None


def _strip_none(value: Any) -> Any:
    if isinstance(value, dict):
        return {k: _strip_none(v) for k, v in value.items() if v is not None}
    if isinstance(value, list):
        return [_strip_none(v) for v in value if v is not None]
    return value


def to_toml_dict(cfg: AppConfig) -> dict[str, Any]:
    """``cfg`` as a TOML-serialisable dict: no ``None``, provider ids only as table keys."""
    data = cfg.model_dump(mode="json")
    for spec in data["providers"].values():
        spec.pop("id", None)
    return _strip_none(data)


def save_config(cfg: AppConfig, paths: Paths) -> None:
    """Write ``config.toml`` atomically (tmp file in the same folder, fsync, ``os.replace``)."""
    text = tomli_w.dumps(to_toml_dict(cfg))
    target = paths.config_file
    target.parent.mkdir(parents=True, exist_ok=True)
    tmp = target.with_name(target.name + ".tmp")
    with tmp.open("w", encoding="utf-8", newline="\n") as handle:
        handle.write(text)
        handle.flush()
        os.fsync(handle.fileno())
    last: OSError | None = None
    for attempt in range(5):
        try:
            os.replace(tmp, target)
            return
        except PermissionError as exc:  # antivirus / indexer briefly holding the target
            last = exc
            time.sleep(0.05 * (attempt + 1))
    _unlink_quiet(tmp)
    raise ConfigError(f"Could not save {target}: {last}")


def _unlink_quiet(path: Path) -> None:
    with contextlib.suppress(OSError):
        path.unlink()


def migrate_raw(raw: dict[str, Any]) -> tuple[dict[str, Any], bool]:
    """Bring a raw ``config.toml`` table up to :data:`SCHEMA_VERSION`; ``(raw, changed)``.

    v1 → v2: the tools added in v2 are switched on (a v1 file lists its enabled tools
    explicitly, so new defaults would never reach it), and the v1 default system prompt
    becomes the v2 default. A prompt the user edited is left alone.

    v2 → v3: ``create_document`` is switched on the same way.
    """
    try:
        version = int(raw.get("schema_version", 1))
    except (TypeError, ValueError):
        version = 1
    if version >= SCHEMA_VERSION:
        return raw, False
    raw = copy.deepcopy(raw)
    if version < 2:
        _enable_new_tools(raw, TOOLS_ADDED_V2)
        chat = raw.get("chat")
        if isinstance(chat, dict) and chat.get("system_prompt") in OLD_DEFAULT_PROMPTS:
            chat["system_prompt"] = DEFAULT_SYSTEM_PROMPT
        if isinstance(chat, dict) and chat.get("max_tool_rounds") == 4:  # the v1 default
            chat["max_tool_rounds"] = 6
    if version < 3:
        _enable_new_tools(raw, TOOLS_ADDED_V3)
    raw["schema_version"] = SCHEMA_VERSION
    return raw, True


def _enable_new_tools(raw: dict[str, Any], added: tuple[str, ...]) -> None:
    """Append ``added`` to an explicit ``tools.enabled`` list (in place).

    A file lists its enabled tools explicitly, so a new default would never reach it. An
    empty list means "no tools" on purpose and stays empty.
    """
    tools = raw.get("tools")
    if isinstance(tools, dict) and isinstance(tools.get("enabled"), list):
        enabled = [t for t in tools["enabled"] if isinstance(t, str)]
        if enabled:
            tools["enabled"] = enabled + [t for t in added if t not in enabled]


def load_config(paths: Paths, *, recover: bool = False) -> AppConfig:
    """Load ``config.toml`` (creating it with defaults when missing) plus env overrides.

    Unknown keys are logged as one warning and ignored. A file that does not parse or
    validate raises :class:`ConfigError`; with ``recover=True`` it is instead renamed to
    ``config.toml.bad`` and defaults are written, so an autostarted tray app never dies on a
    typo.
    """
    paths.home.mkdir(parents=True, exist_ok=True)
    path = paths.config_file
    if not path.exists():
        # Env overrides are applied to the returned object only, never written to the file.
        save_config(validate_config({}), paths)
        return _build({})
    try:
        raw = _read_raw(path)
        raw, migrated = migrate_raw(raw)
        cfg = _build(raw)
        if migrated:
            try:
                save_config(validate_config(raw), paths)
            except (ValidationError, ConfigError) as exc:  # keep running on the old file
                _log.warning("could not save the migrated config.toml: %s", exc)
    except ConfigError as exc:
        if not recover:
            raise
        bad = path.with_name(path.name + ".bad")
        with contextlib.suppress(OSError):
            os.replace(path, bad)
        _log.warning("config.toml was invalid (%s); moved to %s and reset to defaults", exc, bad)
        save_config(validate_config({}), paths)
        return _build({})
    unknown = find_unknown_keys(raw)
    if unknown:
        _log.warning("ignoring unknown keys in config.toml: %s", ", ".join(sorted(unknown)))
    return cfg


def _expand_patch(patch: dict[str, Any]) -> dict[str, Any]:
    """Turn top-level dotted keys (``"local.device": "GPU"``) into nested dicts."""
    out: dict[str, Any] = {}
    for key, value in patch.items():
        parts = key.split(".") if isinstance(key, str) else [key]
        node = out
        for part in parts[:-1]:
            nxt = node.get(part)
            if not isinstance(nxt, dict):
                nxt = node[part] = {}
            node = nxt
        if isinstance(value, dict) and isinstance(node.get(parts[-1]), dict):
            node[parts[-1]] = _deep_merge(node[parts[-1]], value)
        else:
            node[parts[-1]] = value
    return out


def update_config(
    cfg: AppConfig, patch: dict[str, Any], paths: Paths
) -> tuple[AppConfig, list[str]]:
    """Apply a nested (or dotted-key) patch, validate, save, and report reload-required keys.

    ``patch`` looks like ``{"local": {"device": "GPU"}}`` or ``{"local.device": "GPU"}``.
    A provider table set to ``None`` removes that provider (built-in ones are refused).
    Returns ``(new_cfg, restart_keys)`` where ``restart_keys`` is the subset of
    :data:`RESTART_KEYS` whose value changed. Unknown keys and invalid values raise
    :class:`ConfigError` with ``details["errors"] = {dotted_key: message}``; nothing is
    written in that case.

    Environment overrides stay in the returned object but are not persisted: the file is
    rewritten from its own previous state plus the patch.
    """
    expanded = _expand_patch(patch)

    unknown = find_unknown_keys(expanded)
    if unknown:
        raise ConfigError(
            f"Unknown setting: {unknown[0]}",
            details={"errors": {k: "unknown setting" for k in unknown}},
        )

    removals: list[str] = []
    providers_patch = expanded.get("providers")
    if isinstance(providers_patch, dict):
        for pid in [p for p, v in providers_patch.items() if v is None]:
            if pid in cfg.providers and cfg.providers[pid].builtin:
                raise ConfigError(
                    f"Provider {pid!r} is built in and cannot be removed.",
                    details={"errors": {f"providers.{pid}": "built-in provider cannot be removed"}},
                )
            removals.append(pid)
            del providers_patch[pid]

    def apply(base: AppConfig) -> AppConfig:
        merged = _deep_merge(base.model_dump(mode="python"), expanded)
        for pid in removals:
            merged["providers"].pop(pid, None)
        try:
            return validate_config(merged)
        except ValidationError as exc:
            raise _config_error(exc) from None

    new_cfg = apply(cfg)

    # What goes to disk: the file's own state (no env overrides) plus the patch.
    disk_base = cfg
    if paths.config_file.exists():
        try:
            # Migrated like load_config does, so a save never writes the new schema version
            # over an un-migrated file (that would skip the migration for good).
            disk_base = validate_config(migrate_raw(_read_raw(paths.config_file))[0])
        except (ConfigError, ValidationError):
            disk_base = cfg
    save_config(apply(disk_base), paths)

    restart = [k for k in RESTART_KEYS if cfg.get(k) != new_cfg.get(k)]
    return new_cfg, restart
