"""probe.test_provider (the Settings "Test" button)."""

from __future__ import annotations

import socket

import pytest

from chatforge.config import ProviderSpec, Timeouts
from chatforge.llm import probe
from chatforge.llm.providers import SEED_PROVIDERS
from tests.fakes.openai_server import (
    fake_openai_server,
    json_reply,
    scenario_base_resp,
)

KEY = "sk-cp-probe-test-key-0001"
FAST = Timeouts(connect_s=2, stall_s=5, wall_s=10)


def _spec(base_url: str, **kw) -> ProviderSpec:
    return ProviderSpec(
        id=kw.pop("id", "fake"),
        kind="openai",
        display_name="Fake",
        base_url=base_url,
        models=kw.pop("models", ["seed-a", "seed-b"]),
        **kw,
    )


def _minimax_at(base_url: str) -> ProviderSpec:
    return SEED_PROVIDERS["minimax"].model_copy(update={"region": "custom", "base_url": base_url})


async def test_models_listed_then_chat(monkeypatch) -> None:
    registered: list[tuple[str, int]] = []
    with fake_openai_server(models=["api-model"]) as srv:
        monkeypatch.setattr(
            probe, "register_secret", lambda v: registered.append((v, len(srv.requests)))
        )
        result = await probe.test_provider(_spec(srv.base_url), KEY, None, timeouts=FAST)
        paths = [r.path for r in srv.requests]
        chat = srv.last_body
    assert result["ok"] is True
    assert result["models"] == ["api-model"]
    assert result["models_source"] == "api"
    assert result["model"] == "api-model"
    assert result["latency_s"] >= 0
    assert paths == ["/v1/models", "/v1/chat/completions"]
    assert chat["max_tokens"] == 1
    # the key was registered for redaction before any request reached the server
    assert registered == [(KEY, 0)]
    assert KEY not in repr(result)


async def test_models_404_falls_back_to_seeds() -> None:
    with fake_openai_server(models=None) as srv:
        result = await probe.test_provider(_spec(srv.base_url), KEY, None, timeouts=FAST)
        chat = srv.last_body
    assert result["ok"] is True
    assert result["models"] == ["seed-a", "seed-b"]
    assert result["models_source"] == "seeded"
    assert chat["model"] == "seed-a"


async def test_explicit_model_is_used() -> None:
    with fake_openai_server(models=None) as srv:
        result = await probe.test_provider(
            _spec(srv.base_url, default_model="seed-b"), KEY, "typed-model", timeouts=FAST
        )
        assert srv.last_body["model"] == "typed-model"
    assert result["model"] == "typed-model"


async def test_minimax_body_and_seeds() -> None:
    with fake_openai_server(models=None) as srv:
        result = await probe.test_provider(_minimax_at(srv.base_url), KEY, None, timeouts=FAST)
        chat = srv.last_body
    assert result["ok"] is True
    assert result["model"] == "MiniMax-M3"
    assert chat["reasoning_split"] is True
    assert chat["max_completion_tokens"] == 1
    assert "max_tokens" not in chat


async def test_minimax_2049_on_models_200() -> None:
    reply = json_reply({"base_resp": {"status_code": 2049, "status_msg": "invalid api key"}})
    with fake_openai_server(models_reply=reply) as srv:
        result = await probe.test_provider(_minimax_at(srv.base_url), KEY, None, timeouts=FAST)
        assert len(srv.chat_requests) == 0  # no point chatting with a rejected key
    assert result["ok"] is False
    assert result["code"] == "region_or_key"
    assert "api.minimaxi.com" in result["hint"]
    assert result["action"] == "open_settings"


async def test_minimax_2049_on_chat_200() -> None:
    with fake_openai_server(scenario_base_resp(2049), models=None) as srv:
        result = await probe.test_provider(_minimax_at(srv.base_url), KEY, None, timeouts=FAST)
    assert result["ok"] is False
    assert result["code"] == "region_or_key"
    assert result["models"] == SEED_PROVIDERS["minimax"].models


async def test_auth_error() -> None:
    reply = json_reply({"error": {"message": "invalid key"}}, status=401)
    with fake_openai_server(models_reply=reply) as srv:
        result = await probe.test_provider(_spec(srv.base_url), KEY, None, timeouts=FAST)
    assert result["ok"] is False
    assert result["code"] == "auth"


async def test_no_key_for_remote_makes_no_request() -> None:
    result = await probe.test_provider(SEED_PROVIDERS["minimax"], None, None)
    assert result["ok"] is False
    assert result["code"] == "no_key"
    assert result["action"] == "add_key"


async def test_local_spec_is_not_probed() -> None:
    result = await probe.test_provider(SEED_PROVIDERS["local-npu"], None, None)
    assert result["ok"] is False
    assert result["code"] == "bad_request"


async def test_no_model_available() -> None:
    with fake_openai_server(models=None) as srv:
        result = await probe.test_provider(_spec(srv.base_url, models=[]), KEY, None, timeouts=FAST)
    assert result["ok"] is False
    assert result["code"] == "bad_request"


async def test_unreachable() -> None:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
    result = await probe.test_provider(
        _spec(f"http://127.0.0.1:{port}/v1"), KEY, None, timeouts=FAST
    )
    assert result["ok"] is False
    assert result["code"] == "unreachable"


@pytest.mark.parametrize("bad", ["ftp://x/v1"])
async def test_bad_base_url(bad) -> None:
    spec = _spec("http://127.0.0.1:1/v1").model_copy(update={"base_url": bad})
    result = await probe.test_provider(spec, KEY, None, timeouts=FAST)
    assert result["ok"] is False
    assert result["code"] == "bad_request"
