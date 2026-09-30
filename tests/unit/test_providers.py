"""Provider registry, remote key resolution and the local OVMS provider."""

from __future__ import annotations

from contextlib import asynccontextmanager
from types import SimpleNamespace

import pytest

from aichat import secrets
from aichat.config import ProviderSpec, Timeouts
from aichat.errors import AppError
from aichat.llm.errors import LLMError
from aichat.llm.events import Completed
from aichat.llm.minimax import MiniMaxQuirks
from aichat.llm.providers import (
    LOCAL_ID,
    MINIMAX_BASE,
    SEED_PROVIDERS,
    LocalOvmsProvider,
    ProviderRegistry,
    RemoteProvider,
    key_required,
    make_quirks,
    resolve_base_url,
)
from aichat.llm.quirks import GenericQuirks, OvmsQuirks
from tests.fakes.openai_server import fake_openai_server

KEY = "sk-cp-unit-test-key-0001"


@pytest.fixture
def no_env_key(monkeypatch):
    monkeypatch.delenv("MINIMAX_API_KEY", raising=False)


def _settings(**over):
    return SimpleNamespace(
        local=SimpleNamespace(
            max_prompt_len=over.get("max_prompt_len", 4096), enable_thinking=False
        ),
        tools=SimpleNamespace(tool_result_max_chars_local=1500, tool_result_max_chars_cloud=6000),
        chat=SimpleNamespace(temperature=0.7, max_output_tokens=1024),
        providers=over.get("providers", dict(SEED_PROVIDERS)),
    )


class FakeManager:
    def __init__(self, base_url="http://127.0.0.1:18611/v3", fail: Exception | None = None):
        self.base_url = base_url
        self.fail = fail
        self.loaded: list[str] = []
        self.subscribers: list = []
        self.leased: list[str] = []

    async def ensure_loaded(self, model_id: str) -> str:
        for cb in list(self.subscribers):
            cb({"state": "compiling", "model_id": model_id})
        if self.fail:
            raise self.fail
        self.loaded.append(model_id)
        return self.base_url

    @asynccontextmanager
    async def lease(self, model_id: str):
        self.leased.append(model_id)
        yield self.base_url

    def status(self) -> dict:
        return {"state": "ready"}

    def subscribe(self, cb):
        self.subscribers.append(cb)
        return lambda: self.subscribers.remove(cb)


def _custom(base_url="http://127.0.0.1:1234/v1", **kw) -> ProviderSpec:
    return ProviderSpec(
        id=kw.pop("id", "lmstudio"),
        kind="openai",
        display_name=kw.pop("display_name", "LM Studio"),
        base_url=base_url,
        models=kw.pop("models", ["m1"]),
        **kw,
    )


# --------------------------------------------------------------------------- #
# Seeds and helpers
# --------------------------------------------------------------------------- #


def test_seeds_and_regions() -> None:
    assert set(SEED_PROVIDERS) == {"local-npu", "minimax"}
    assert SEED_PROVIDERS["local-npu"].builtin is True
    mm = SEED_PROVIDERS["minimax"]
    assert mm.default_model == "MiniMax-M3"
    assert MINIMAX_BASE == {
        "international": "https://api.minimax.io/v1",
        "china": "https://api.minimaxi.com/v1",
    }
    assert resolve_base_url(mm) == "https://api.minimax.io/v1"
    assert (
        resolve_base_url(mm.model_copy(update={"region": "china"})) == "https://api.minimaxi.com/v1"
    )
    custom = mm.model_copy(update={"region": "custom", "base_url": "https://proxy.example/v1/"})
    assert resolve_base_url(custom) == "https://proxy.example/v1"
    with pytest.raises(LLMError) as ei:
        resolve_base_url(_custom(base_url=None))
    assert ei.value.code == "bad_request"


def test_make_quirks_and_key_required() -> None:
    assert isinstance(make_quirks(SEED_PROVIDERS["minimax"]), MiniMaxQuirks)
    assert isinstance(make_quirks(SEED_PROVIDERS["local-npu"]), OvmsQuirks)
    assert type(make_quirks(_custom())) is GenericQuirks
    assert key_required(SEED_PROVIDERS["minimax"]) is True
    assert key_required(SEED_PROVIDERS["local-npu"]) is False
    assert key_required(_custom()) is False  # loopback server
    assert key_required(_custom(base_url="https://api.example.com/v1")) is True


# --------------------------------------------------------------------------- #
# RemoteProvider
# --------------------------------------------------------------------------- #


async def test_remote_no_key_raises_add_key(fake_keyring, no_env_key) -> None:
    provider = RemoteProvider(SEED_PROVIDERS["minimax"])
    with pytest.raises(LLMError) as ei:
        await provider.client_for("MiniMax-M3")
    assert ei.value.code == "no_key"
    assert ei.value.action == "add_key"
    assert provider.key_status()["source"] == "none"
    assert provider.key_status()["required"] is True


async def test_remote_key_from_keyring(fake_keyring, no_env_key) -> None:
    secrets.set_api_key("minimax", KEY)
    provider = RemoteProvider(SEED_PROVIDERS["minimax"])
    client = await provider.client_for("MiniMax-M3")
    try:
        assert client.base_url == "https://api.minimax.io/v1"
        assert isinstance(client.quirks, MiniMaxQuirks)
        assert client.timeouts == SEED_PROVIDERS["minimax"].timeouts
        assert KEY not in repr(client)
    finally:
        await client.aclose()
    assert provider.key_status()["source"] == "keyring"


async def test_remote_env_key_wins(fake_keyring, monkeypatch) -> None:
    monkeypatch.setenv("MINIMAX_API_KEY", KEY)
    provider = RemoteProvider(SEED_PROVIDERS["minimax"])
    client = await provider.client_for("MiniMax-M3")
    await client.aclose()
    assert provider.key_status()["source"] == "env"


async def test_remote_end_to_end_against_fake(fake_keyring) -> None:
    with fake_openai_server() as srv:
        spec = _custom(base_url=srv.base_url, id="fake")
        provider = RemoteProvider(spec, settings=_settings)
        client = await provider.client_for("fake-model")
        async with client:
            req = provider.make_request(
                "fake-model", [{"role": "user", "content": "hi"}], tools=None
            )
            events = [ev async for ev in client.stream_chat(req)]
    assert isinstance(events[-1], Completed)
    assert events[-1].message.content == "ok"
    assert srv.last_body["max_tokens"] == spec.max_output_tokens


def test_remote_make_request_and_budget() -> None:
    spec = SEED_PROVIDERS["minimax"]
    provider = RemoteProvider(spec, settings=_settings)
    tools = [{"type": "function", "function": {"name": "x"}}]
    req = provider.make_request("MiniMax-M3", [], tools)
    assert req.temperature == 1.0 and req.max_tokens == 4096 and req.tools == tools
    assert req.extra_body == {"reasoning_split": True}
    no_tools = RemoteProvider(spec.model_copy(update={"supports_tools": False}))
    assert no_tools.make_request("m", [], tools).tools is None
    assert provider.budget_params("MiniMax-M3") == {
        "max_prompt_tokens": None,
        "tool_result_chars": 6000,
    }


# --------------------------------------------------------------------------- #
# LocalOvmsProvider
# --------------------------------------------------------------------------- #


async def test_local_provider_loads_and_reports_status() -> None:
    mgr = FakeManager()
    provider = LocalOvmsProvider(SEED_PROVIDERS["local-npu"], mgr, settings=_settings)
    statuses: list[dict] = []
    client = await provider.client_for("OpenVINO/Qwen3-4B-int4-ov", statuses.append)
    try:
        assert client.base_url == "http://127.0.0.1:18611/v3"
        assert isinstance(client.quirks, OvmsQuirks)
        assert client.quirks.enable_thinking is False
    finally:
        await client.aclose()
    assert mgr.loaded == ["OpenVINO/Qwen3-4B-int4-ov"]
    assert statuses == [{"state": "compiling", "model_id": "OpenVINO/Qwen3-4B-int4-ov"}]
    assert mgr.subscribers == []  # unsubscribed afterwards
    async with provider.lease("m") as base:
        assert base == mgr.base_url
    assert provider.status() == {"state": "ready"}
    assert provider.key_status()["required"] is False


@pytest.mark.parametrize(
    "failure", [AppError("OVMS exited", code="runtime_error", hint="see logs"), RuntimeError("x")]
)
async def test_local_provider_load_failure(failure) -> None:
    provider = LocalOvmsProvider(SEED_PROVIDERS["local-npu"], FakeManager(fail=failure))
    with pytest.raises(LLMError) as ei:
        await provider.client_for("m")
    assert ei.value.code == "model_loading_failed"


def test_local_budget_and_request() -> None:
    provider = LocalOvmsProvider(
        SEED_PROVIDERS["local-npu"], FakeManager(), settings=lambda: _settings(max_prompt_len=4096)
    )
    assert provider.budget_params("m") == {"max_prompt_tokens": 3968, "tool_result_chars": 1500}
    req = provider.make_request("m", [], None)
    assert req.temperature == 0.7 and req.max_tokens == 1024


def test_budget_object_when_history_available() -> None:
    history = pytest.importorskip("aichat.chat.history")
    provider = LocalOvmsProvider(SEED_PROVIDERS["local-npu"], FakeManager(), settings=_settings)
    budget = provider.budget("m")
    assert isinstance(budget, history.PromptBudget)
    assert budget.max_prompt_tokens == 3968


# --------------------------------------------------------------------------- #
# ProviderRegistry
# --------------------------------------------------------------------------- #


def test_registry_list_and_get() -> None:
    reg = ProviderRegistry.from_config(_settings, manager=FakeManager())
    ids = [s.id for s in reg.list()]
    assert ids[0] == LOCAL_ID and "minimax" in ids
    assert isinstance(reg.get("minimax"), RemoteProvider)
    assert isinstance(reg.get(LOCAL_ID), LocalOvmsProvider)
    with pytest.raises(LLMError) as ei:
        reg.get("nope")
    assert ei.value.code == "not_found"


def test_registry_local_without_manager() -> None:
    reg = ProviderRegistry()
    with pytest.raises(LLMError) as ei:
        reg.get(LOCAL_ID)
    assert ei.value.code == "model_loading_failed"


def test_registry_upsert_and_remove_custom() -> None:
    changes: list[dict] = []
    reg = ProviderRegistry(on_change=changes.append)
    saved = reg.upsert(_custom(builtin=True))
    assert saved.builtin is False  # new providers are never builtin
    assert "lmstudio" in changes[-1]
    assert reg.spec("lmstudio").base_url == "http://127.0.0.1:1234/v1"
    reg.upsert(_custom(display_name="LM Studio 2"))
    assert reg.spec("lmstudio").display_name == "LM Studio 2"
    reg.remove("lmstudio")
    assert "lmstudio" not in changes[-1]
    with pytest.raises(LLMError):
        reg.spec("lmstudio")


@pytest.mark.parametrize("pid", ["local-npu", "minimax"])
def test_registry_refuses_removing_seeded(pid) -> None:
    reg = ProviderRegistry()
    with pytest.raises(LLMError) as ei:
        reg.remove(pid)
    assert ei.value.code == "bad_request"
    assert "cannot be removed" in ei.value.message
    assert reg.spec(pid)


def test_registry_upsert_validation() -> None:
    reg = ProviderRegistry()
    with pytest.raises(LLMError):
        reg.upsert(ProviderSpec(id="npu2", kind="ovms", display_name="Another"))
    with pytest.raises(LLMError):
        reg.upsert(SEED_PROVIDERS["minimax"].model_copy(update={"kind": "ovms"}))
    with pytest.raises(LLMError):
        reg.upsert(_custom(base_url=None, id="nourl"))
    # builtin flag of the seeded local provider survives edits
    reg.upsert(SEED_PROVIDERS["local-npu"].model_copy(update={"builtin": False}))
    assert reg.spec(LOCAL_ID).builtin is True


def test_registry_minimax_region_switch_updates_base_url() -> None:
    reg = ProviderRegistry()
    saved = reg.upsert(SEED_PROVIDERS["minimax"].model_copy(update={"region": "china"}))
    assert saved.base_url == "https://api.minimaxi.com/v1"
    custom = SEED_PROVIDERS["minimax"].model_copy(
        update={"region": "custom", "base_url": "https://mm-proxy.example/v1"}
    )
    assert reg.upsert(custom).base_url == "https://mm-proxy.example/v1"


def test_registry_replace_all_keeps_seeds() -> None:
    reg = ProviderRegistry()
    reg.replace_all({"lmstudio": _custom()})
    assert {s.id for s in reg.list()} == {"local-npu", "minimax", "lmstudio"}


def test_provider_timeouts_type() -> None:
    assert isinstance(SEED_PROVIDERS["minimax"].timeouts, Timeouts)
