"""Provider registry: the built-in local OVMS runtime plus OpenAI-compatible remotes.

``ProviderSpec`` and ``Timeouts`` live in :mod:`aichat.config` (PLAN §7 item 3); the
seeds come from :func:`aichat.config.seed_providers` so there is one source of truth.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from contextlib import AbstractAsyncContextManager
from typing import Any, Protocol, runtime_checkable

import httpx

from aichat import secrets
from aichat.config import ProviderSpec, Timeouts, seed_providers
from aichat.errors import AppError
from aichat.llm.client import ChatRequest, OpenAICompatClient
from aichat.llm.errors import LLMError, is_loopback_url, normalize_base_url
from aichat.llm.minimax import MiniMaxQuirks
from aichat.llm.quirks import DeepSeekQuirks, GenericQuirks, OpenAIQuirks, OvmsQuirks, Quirks

MINIMAX_BASE: dict[str, str] = {
    "international": "https://api.minimax.io/v1",
    "china": "https://api.minimaxi.com/v1",
}

SEED_PROVIDERS: dict[str, ProviderSpec] = seed_providers()
SEED_IDS: frozenset[str] = frozenset(SEED_PROVIDERS)
LOCAL_ID = "local-npu"
LOCAL_BUDGET_RESERVE_TOKENS = 128
_DEFAULT_TOOL_CHARS = {"local": 1500, "cloud": 6000}

StatusCallback = Callable[[dict], None]


# --------------------------------------------------------------------------- #
# Spec helpers
# --------------------------------------------------------------------------- #


def is_minimax(spec: ProviderSpec) -> bool:
    return "minimax" in spec.quirks


def resolve_base_url(spec: ProviderSpec) -> str:
    """Region → base URL for MiniMax (``custom`` uses ``base_url``); else ``base_url``."""
    if is_minimax(spec) and spec.region in MINIMAX_BASE:
        return MINIMAX_BASE[spec.region]
    if spec.base_url:
        return normalize_base_url(spec.base_url)
    raise LLMError(
        f"{spec.display_name} has no base URL",
        code="bad_request",
        hint="Set the provider's base URL in Settings → Providers.",
        action="open_settings",
    )


def key_required(spec: ProviderSpec) -> bool:
    """Remote providers need a key, except OpenAI-compatible servers on loopback and
    providers that declare the key optional (``key_required = false``)."""
    if spec.kind == "ovms":
        return False
    if spec.key_required is not None:
        return spec.key_required
    try:
        return not is_loopback_url(resolve_base_url(spec))
    except LLMError:
        return True


def context_tokens_for(spec: ProviderSpec, model: str) -> int | None:
    """The context window for ``model``: what the provider reported for it (by "Refresh
    models"), capped by the provider's own setting; else the setting; else ``None`` (the
    cloud default). The setting is a cap because a reported value can be a model's trained
    maximum rather than the context it is actually loaded with."""
    reported = spec.model_context.get(model)
    if reported and spec.context_tokens:
        return min(reported, spec.context_tokens)
    return reported or spec.context_tokens


def prompt_tokens_for(spec: ProviderSpec, model: str) -> int | None:
    """How much of the context window the prompt may use: the window minus room for the
    reply. ``None`` keeps the cloud default."""
    ctx = context_tokens_for(spec, model)
    if not ctx:
        return None
    return max(1024, ctx - spec.max_output_tokens - 256)


def make_quirks(spec: ProviderSpec, *, enable_thinking: bool = False) -> Quirks:
    if spec.kind == "ovms" or "ovms" in spec.quirks:
        return OvmsQuirks(enable_thinking=enable_thinking)
    if is_minimax(spec):
        return MiniMaxQuirks()
    if "deepseek" in spec.quirks:
        return DeepSeekQuirks()
    if "openai" in spec.quirks:
        return OpenAIQuirks()
    return GenericQuirks()


def no_key_error(spec: ProviderSpec) -> LLMError:
    env = f" (or set {spec.api_key_env})" if spec.api_key_env else ""
    return LLMError(
        f"Add your {spec.display_name} API key",
        code="no_key",
        action="add_key",
        hint=f"Paste the key in the card below or in Settings → Providers{env}.",
    )


def _get(obj: Any, dotted: str, default: Any) -> Any:
    for part in dotted.split("."):
        if obj is None:
            return default
        obj = obj.get(part) if isinstance(obj, Mapping) else getattr(obj, part, None)
    return default if obj is None else obj


def _prompt_budget(**params: Any) -> Any:
    from aichat.chat.history import PromptBudget  # WS6; imported late to avoid a cycle

    return PromptBudget(**params)


# --------------------------------------------------------------------------- #
# Providers
# --------------------------------------------------------------------------- #


@runtime_checkable
class Provider(Protocol):
    spec: ProviderSpec

    async def client_for(
        self, model: str, on_status: StatusCallback | None = None
    ) -> OpenAICompatClient: ...

    def key_status(self) -> dict: ...

    def budget(self, model: str) -> Any: ...  # chat.history.PromptBudget


class LocalModelManagerLike(Protocol):
    """What :class:`LocalOvmsProvider` needs from ``runtime.manager.LocalModelManager``."""

    async def ensure_loaded(self, model_id: str) -> str: ...

    def lease(self, model_id: str) -> AbstractAsyncContextManager[str]: ...

    def status(self) -> dict: ...


class RemoteProvider:
    """An OpenAI-compatible cloud (or LAN) provider; the key comes from env > keyring."""

    def __init__(
        self,
        spec: ProviderSpec,
        *,
        settings: Callable[[], Any] | None = None,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self.spec = spec
        self._settings = settings or (lambda: None)
        self._transport = transport

    @property
    def requires_key(self) -> bool:
        return key_required(self.spec)

    def resolve_key(self) -> str | None:
        key, _source = secrets.get_api_key(self.spec.id, self.spec.api_key_env)
        if not key and self.requires_key:
            raise no_key_error(self.spec)
        return key

    async def client_for(
        self, model: str, on_status: StatusCallback | None = None
    ) -> OpenAICompatClient:
        key = self.resolve_key()
        return OpenAICompatClient(
            resolve_base_url(self.spec),
            key,
            quirks=make_quirks(self.spec),
            timeouts=self.spec.timeouts,
            transport=self._transport,
        )

    def key_status(self) -> dict:
        status = dict(secrets.key_status(self.spec.id, self.spec.api_key_env))
        status["required"] = self.requires_key
        return status

    def budget_params(self, model: str) -> dict:
        chars = _get(
            self._settings(), "tools.tool_result_max_chars_cloud", _DEFAULT_TOOL_CHARS["cloud"]
        )
        return {
            "max_prompt_tokens": prompt_tokens_for(self.spec, model),
            "tool_result_chars": int(chars),
            "local": False,
        }

    def budget(self, model: str) -> Any:
        return _prompt_budget(**self.budget_params(model))

    def make_request(
        self, model: str, messages: list[dict], tools: list[dict] | None = None
    ) -> ChatRequest:
        return ChatRequest(
            model=model,
            messages=messages,
            tools=tools if (tools and self.spec.supports_tools) else None,
            temperature=self.spec.temperature,
            max_tokens=self.spec.max_output_tokens,
            extra_body=dict(self.spec.extra_body),
        )


class LocalOvmsProvider:
    """The built-in NPU runtime. ``manager`` is WS6's ``LocalModelManager``."""

    def __init__(
        self,
        spec: ProviderSpec,
        manager: LocalModelManagerLike,
        *,
        settings: Callable[[], Any] | None = None,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self.spec = spec
        self.manager = manager
        self._settings = settings or (lambda: None)
        self._transport = transport

    @property
    def enable_thinking(self) -> bool:
        return bool(_get(self._settings(), "local.enable_thinking", False))

    async def client_for(
        self, model: str, on_status: StatusCallback | None = None
    ) -> OpenAICompatClient:
        unsubscribe = None
        subscribe = getattr(self.manager, "subscribe", None)
        if on_status is not None and callable(subscribe):
            unsubscribe = subscribe(on_status)
        try:
            base_url = await self.manager.ensure_loaded(model)
        except LLMError:
            raise
        except AppError as e:
            raise LLMError(
                e.message or "The local model failed to load",
                code="model_loading_failed",
                hint=e.hint or "See Settings → Logs.",
                details=e.details,
            ) from e
        except Exception as e:  # noqa: BLE001 - runtime failures become a UI error
            raise LLMError(
                "The local model failed to load",
                code="model_loading_failed",
                hint=f"{type(e).__name__}: {str(e)[:200]}. See Settings → Logs.",
            ) from e
        finally:
            if unsubscribe is not None:
                unsubscribe()
        return self.make_client(base_url)

    def make_client(self, base_url: str) -> OpenAICompatClient:
        """A client for a ``base_url`` from ``manager.lease()`` (the port can change on reload)."""
        return OpenAICompatClient(
            base_url,
            None,
            quirks=OvmsQuirks(enable_thinking=self.enable_thinking),
            timeouts=self.spec.timeouts,
            transport=self._transport,
        )

    def lease(self, model: str) -> AbstractAsyncContextManager[str]:
        return self.manager.lease(model)

    def status(self) -> dict:
        return self.manager.status()

    def key_status(self) -> dict:
        return {"source": "none", "env_name": None, "env_overrides_saved": False, "required": False}

    def budget_params(self, model: str) -> dict:
        settings = self._settings()
        max_len = int(_get(settings, "local.max_prompt_len", 4096))
        chars = _get(settings, "tools.tool_result_max_chars_local", _DEFAULT_TOOL_CHARS["local"])
        return {
            "max_prompt_tokens": max(256, max_len - LOCAL_BUDGET_RESERVE_TOKENS),
            "tool_result_chars": int(chars),
        }

    def budget(self, model: str) -> Any:
        return _prompt_budget(**self.budget_params(model))

    def make_request(
        self, model: str, messages: list[dict], tools: list[dict] | None = None
    ) -> ChatRequest:
        settings = self._settings()
        return ChatRequest(
            model=model,
            messages=messages,
            tools=tools if (tools and self.spec.supports_tools) else None,
            temperature=_get(settings, "chat.temperature", self.spec.temperature),
            max_tokens=int(_get(settings, "chat.max_output_tokens", 1024)),
            extra_body=dict(self.spec.extra_body),
        )


# --------------------------------------------------------------------------- #
# Registry
# --------------------------------------------------------------------------- #


class ProviderRegistry:
    """CRUD over the configured providers. ``on_change(providers)`` is called after
    every upsert/remove so the caller can persist them (``update_config``)."""

    def __init__(
        self,
        specs: Mapping[str, ProviderSpec] | None = None,
        *,
        manager: LocalModelManagerLike | None = None,
        settings: Callable[[], Any] | None = None,
        on_change: Callable[[dict[str, ProviderSpec]], None] | None = None,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self.manager = manager
        self._settings = settings
        self._on_change = on_change
        self._transport = transport
        self._specs: dict[str, ProviderSpec] = {}
        self.replace_all(specs or {})

    @classmethod
    def from_config(cls, get_config: Callable[[], Any], **kw: Any) -> ProviderRegistry:
        return cls(get_config().providers, settings=get_config, **kw)

    def replace_all(self, specs: Mapping[str, ProviderSpec]) -> None:
        """Reload from config (seeds are always present)."""
        merged = {pid: s.model_copy(deep=True) for pid, s in SEED_PROVIDERS.items()}
        merged.update({pid: s.model_copy(deep=True) for pid, s in specs.items()})
        self._specs = merged

    def list(self) -> list[ProviderSpec]:
        order = sorted(self._specs, key=lambda pid: (pid != LOCAL_ID, pid not in SEED_IDS))
        return [self._specs[pid].model_copy(deep=True) for pid in order]

    def spec(self, pid: str) -> ProviderSpec:
        try:
            return self._specs[pid]
        except KeyError:
            raise LLMError(
                f"Unknown provider {pid!r}",
                code="not_found",
                hint="Pick a provider in Settings → Providers.",
                action="open_settings",
            ) from None

    def get(self, pid: str) -> Provider:
        spec = self.spec(pid)
        if spec.kind == "ovms":
            if self.manager is None:
                raise LLMError(
                    "The local runtime is not available",
                    code="model_loading_failed",
                    hint="The local model manager has not started.",
                )
            return LocalOvmsProvider(
                spec, self.manager, settings=self._settings, transport=self._transport
            )
        return RemoteProvider(spec, settings=self._settings, transport=self._transport)

    def upsert(self, spec: ProviderSpec) -> ProviderSpec:
        existing = self._specs.get(spec.id)
        if existing is not None:
            if existing.kind != spec.kind:
                raise LLMError(
                    f"Cannot change the kind of provider {spec.id!r}",
                    code="bad_request",
                    hint="Add a new provider instead.",
                )
            spec = spec.model_copy(update={"builtin": existing.builtin})
        else:
            if spec.kind == "ovms":
                raise LLMError(
                    "Only the built-in local runtime can use kind 'ovms'",
                    code="bad_request",
                    hint="Custom providers are OpenAI-compatible (kind 'openai').",
                )
            spec = spec.model_copy(update={"builtin": False})
        if spec.kind == "openai":
            if is_minimax(spec) and spec.region in MINIMAX_BASE:
                spec = spec.model_copy(update={"base_url": MINIMAX_BASE[spec.region]})
            resolve_base_url(spec)  # raises for a missing/invalid URL
        self._specs[spec.id] = spec
        self._changed()
        return spec.model_copy(deep=True)

    def remove(self, pid: str) -> None:
        spec = self.spec(pid)
        if spec.builtin or pid in SEED_IDS:
            raise LLMError(
                f"{spec.display_name} is built in and cannot be removed",
                code="bad_request",
                hint="Built-in providers can be edited but not removed.",
            )
        del self._specs[pid]
        self._changed()

    def _changed(self) -> None:
        if self._on_change is not None:
            self._on_change({pid: s.model_copy(deep=True) for pid, s in self._specs.items()})


__all__ = [
    "LOCAL_ID",
    "MINIMAX_BASE",
    "SEED_IDS",
    "SEED_PROVIDERS",
    "LocalOvmsProvider",
    "Provider",
    "ProviderRegistry",
    "ProviderSpec",
    "RemoteProvider",
    "Timeouts",
    "key_required",
    "make_quirks",
    "resolve_base_url",
]
