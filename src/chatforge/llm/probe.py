"""The provider "Test" button.

Adapted from DisPatch backend/app/llm_api.py (MIT, LaserLloyd): the ``_probe_openai``
flow (``GET /models``; a 404/405 means "no catalogue here", not "unreachable"), plus
MiniMax ``base_resp`` handling, a seeded-model fallback and a one-token chat that
proves the endpoint that matters and measures latency.
"""

from __future__ import annotations

import logging
import time
from typing import Any

import httpx

from chatforge.config import ProviderSpec, Timeouts
from chatforge.llm.client import ChatRequest, OpenAICompatClient
from chatforge.llm.errors import LLMError
from chatforge.llm.events import Completed
from chatforge.llm.providers import key_required, make_quirks, no_key_error, resolve_base_url
from chatforge.logging_setup import register_secret

log = logging.getLogger(__name__)

# Errors from /models that make the chat attempt pointless.
_FATAL_LIST_CODES = frozenset({"auth", "region_or_key", "no_key", "balance", "quota"})


def _fail(e: LLMError, models: list[str]) -> dict[str, Any]:
    return {
        "ok": False,
        "models": models,
        "latency_s": None,
        "error": e.message,
        "hint": e.hint,
        "code": e.code,
        "action": e.action,
    }


def _probe_timeouts(spec: ProviderSpec) -> Timeouts:
    t = spec.timeouts
    return Timeouts(
        connect_s=min(t.connect_s, 10.0), stall_s=min(t.stall_s, 30.0), wall_s=min(t.wall_s, 60.0)
    )


async def test_provider(
    spec: ProviderSpec,
    api_key: str | None,
    model: str | None,
    *,
    transport: httpx.AsyncBaseTransport | None = None,
    timeouts: Timeouts | None = None,
) -> dict[str, Any]:
    """``{"ok", "models", "latency_s", "error", "hint", "code", ...}``. Never raises and
    never includes the key."""
    if api_key:
        register_secret(api_key)  # before any network call, so no log line can carry it
    seeded = list(spec.models)
    if spec.kind == "ovms":
        return _fail(
            LLMError(
                "The local runtime is tested by loading a model",
                code="bad_request",
                hint="Use Load in Settings → Models.",
            ),
            seeded,
        )
    try:
        base_url = resolve_base_url(spec)
    except LLMError as e:
        return _fail(e, seeded)
    if not api_key and key_required(spec):
        return _fail(no_key_error(spec), seeded)

    try:
        client = OpenAICompatClient(
            base_url,
            api_key,
            quirks=make_quirks(spec),
            timeouts=timeouts or _probe_timeouts(spec),
            transport=transport,
        )
    except LLMError as e:
        return _fail(e, seeded)

    try:
        async with client:
            models_source = "api"
            try:
                models = await client.list_models()
            except LLMError as e:
                if e.code in _FATAL_LIST_CODES:
                    return _fail(e, seeded)
                log.info("probe %s: /models failed (%s); trying chat", spec.id, e.code)
                models = []
            if not models:
                models, models_source = seeded, "seeded"
            chosen = (model or "").strip() or spec.default_model or (models[0] if models else "")
            if not chosen:
                return _fail(
                    LLMError(
                        "This server has no model list",
                        code="bad_request",
                        hint="Type the model name and test again; the chat endpoint "
                        "will be checked directly.",
                    ),
                    models,
                )
            req = ChatRequest(
                model=chosen,
                messages=[{"role": "user", "content": "Hi"}],
                max_tokens=1,
                extra_body=dict(spec.extra_body),
            )
            t0 = time.monotonic()
            served = None
            try:
                async for ev in client.stream_chat(req):
                    if isinstance(ev, Completed):
                        served = ev.served_model
            except LLMError as e:
                return _fail(e, models)
            latency = time.monotonic() - t0
    except Exception as e:  # noqa: BLE001 - the Test button must always get an answer
        log.warning("probe %s failed: %s", spec.id, type(e).__name__)
        return _fail(
            LLMError("The test failed unexpectedly", code="server", hint=type(e).__name__), seeded
        )
    return {
        "ok": True,
        "models": models,
        "models_source": models_source,
        "model": chosen,
        "served_model": served,
        "latency_s": round(latency, 3),
        "error": None,
        "hint": None,
        "code": None,
    }


# Keep pytest from collecting this when a test module imports it by name.
test_provider.__test__ = False  # type: ignore[attr-defined]
