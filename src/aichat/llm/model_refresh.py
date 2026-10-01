"""Keep the remote providers' model lists current.

A provider's ``models`` list is what the popup's model menu offers. It is seeded when the
provider is added (MiniMax, OpenAI, DeepSeek, StudioForge) and goes stale as providers ship
new models. :func:`fetch_models` reads the provider's own ``GET /models`` with the saved key;
:func:`merge_models` folds the result into the stored list. The bridge's ``refresh_models``
and the app's daily background refresh both use these.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any

import httpx

from aichat.config import ProviderSpec, Timeouts
from aichat.llm.client import OpenAICompatClient
from aichat.llm.errors import LLMError
from aichat.llm.providers import key_required, make_quirks, no_key_error, resolve_base_url
from aichat.logging_setup import register_secret

#: Model ids that are not chat models (OpenAI and StudioForge list these too).
_NOT_CHAT = re.compile(
    r"embed|whisper|tts|dall-?e|moderation|rerank|transcri|audio|realtime|image|sora|"
    r"davinci|babbage|speech|search-preview",
    re.IGNORECASE,
)
_TIMEOUTS = Timeouts(connect_s=10, stall_s=30, wall_s=60)


@dataclass
class FetchResult:
    ok: bool
    models: list[str] = field(default_factory=list)
    error: str | None = None
    hint: str | None = None
    code: str | None = None
    #: Context windows the provider reported, by model id.
    contexts: dict[str, int] = field(default_factory=dict)


# Where servers put a model's context window, most specific first. StudioForge (and LM
# Studio) report the context a model is loaded with, and the trained maximum otherwise.
_CONTEXT_KEYS = (
    "loaded_context_length",
    "context_length",
    "context_window",
    "max_context_length",
    "max_model_len",
    "max_input_tokens",
    "n_ctx",
)


def _as_tokens(value: Any) -> int | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, int | float) and 1024 <= value <= 10_000_000:
        return int(value)
    return None


def context_of(entry: dict[str, Any]) -> int | None:
    """The context window a ``GET /models`` entry reports, if any."""
    for scope in (
        entry,
        entry.get("studioforge") if isinstance(entry.get("studioforge"), dict) else {},
    ):
        for key in _CONTEXT_KEYS:
            tokens = _as_tokens(scope.get(key))
            if tokens:
                return tokens
    sf = entry.get("studioforge")
    if isinstance(sf, dict):
        return _as_tokens(sf.get("n_ctx_train"))
    return None


def _is_chat_entry(entry: dict[str, Any]) -> bool:
    """StudioForge tags each model with a kind; only chat models belong in the menu."""
    sf = entry.get("studioforge")
    kind = sf.get("kind") if isinstance(sf, dict) else None
    if isinstance(kind, str) and kind and kind != "chat":
        return False
    mtype = entry.get("type")
    return not (isinstance(mtype, str) and "embed" in mtype.lower())


def chat_models(ids: list[str]) -> list[str]:
    """The chat-capable ids, sorted and de-duplicated."""
    return sorted({m for m in ids if m and not _NOT_CHAT.search(m)})


def is_configured(spec: ProviderSpec, key: str | None) -> bool:
    """A remote provider that can be asked for its models right now."""
    return spec.kind != "ovms" and (bool(key) or not key_required(spec))


async def fetch_models(
    spec: ProviderSpec,
    key: str | None,
    *,
    transport: httpx.AsyncBaseTransport | None = None,
) -> FetchResult:
    """The provider's current chat models from ``GET /models``. Never raises."""
    if key:
        register_secret(key)
    if spec.kind == "ovms":
        return FetchResult(False, error="The local models are listed in Settings → Models.")
    try:
        base_url = resolve_base_url(spec)
        if not key and key_required(spec):
            raise no_key_error(spec)
        client = OpenAICompatClient(
            base_url, key, quirks=make_quirks(spec), timeouts=_TIMEOUTS, transport=transport
        )
        async with client:
            entries = await client.list_model_entries()
    except LLMError as e:
        return FetchResult(False, error=e.message, hint=e.hint, code=e.code)
    except Exception as e:  # noqa: BLE001 - a refresh must always get an answer
        return FetchResult(False, error="The model list could not be read", hint=type(e).__name__)
    chat_entries = [e for e in entries if _is_chat_entry(e)]
    models = chat_models([e["id"] for e in chat_entries])
    if not models:
        return FetchResult(
            False,
            error=f"{spec.display_name} does not publish a model list",
            hint="Keep the current list, or type a model name in Settings → Providers.",
            code="no_catalogue",
        )
    wanted = set(models)
    contexts = {e["id"]: ctx for e in chat_entries if e["id"] in wanted and (ctx := context_of(e))}
    return FetchResult(True, models, contexts=contexts)


def merge_models(spec: ProviderSpec, fetched: list[str]) -> tuple[list[str], str | None]:
    """``(models, default_model)`` after a refresh.

    The stored order is kept for models still offered, new models are added after them
    (sorted), and models the provider no longer offers are dropped. The default stays if
    it is still offered, otherwise it becomes the first model.
    """
    offered = set(fetched)
    kept = [m for m in spec.models if m in offered]
    added = sorted(offered - set(kept))
    models = kept + added
    default = (
        spec.default_model if spec.default_model in offered else (models[0] if models else None)
    )
    return models, default


def change_summary(before: list[str], after: list[str]) -> dict[str, Any]:
    return {
        "added": sorted(set(after) - set(before)),
        "removed": sorted(set(before) - set(after)),
    }


__all__ = [
    "FetchResult",
    "change_summary",
    "chat_models",
    "fetch_models",
    "is_configured",
    "merge_models",
]
