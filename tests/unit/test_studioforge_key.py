"""StudioForge 2.0 wants a bearer key off loopback; the app must say so and say how.

A fake StudioForge answers 401 unless ``Authorization: Bearer <good key>`` arrives. The
default URL is always loopback or a fake server (never the live StudioForge).
"""

from __future__ import annotations

import pytest

from chatforge import secrets
from chatforge.config import STUDIOFORGE_KEY_HELP, load_config, seed_providers
from chatforge.llm.client import ChatRequest
from chatforge.llm.errors import LLMError
from chatforge.llm.probe import test_provider as run_provider_test
from chatforge.llm.providers import RemoteProvider, key_hint, key_required, no_key_error
from chatforge.paths import Paths
from tests.fakes.openai_server import Reply, fake_openai_server, json_reply, sse, text_chunks

GOOD = "sf-key-unit-test-0001"
UNREACHABLE = "http://127.0.0.1:9/v1"  # the discard port: nothing listens, nothing is contacted


def _auth_gate(request) -> Reply:
    if request.headers.get("authorization") != f"Bearer {GOOD}":
        return json_reply({"error": {"message": "missing or invalid API key"}}, status=401)
    return sse(text_chunks("hello", 1))


def _spec(base_url: str):
    spec = seed_providers()["studioforge"].model_copy(deep=True)
    spec.base_url = base_url
    return spec


def _request(provider: RemoteProvider) -> ChatRequest:
    return provider.make_request("m", [{"role": "user", "content": "hi"}])


async def _drain(client, req) -> None:
    async with client:
        async for _ in client.stream_chat(req):
            pass


def test_the_seed_requires_a_key_off_loopback_only() -> None:
    spec = seed_providers()["studioforge"]
    assert spec.key_required is None  # automatic, not "optional"
    assert key_required(spec) is False  # http://localhost:1234/v1
    lan = spec.model_copy(update={"base_url": "http://192.168.0.20:1234/v1"})
    assert key_required(lan) is True
    named = spec.model_copy(update={"base_url": "http://studioforge.example:1234/v1"})
    assert key_required(named) is True


def test_a_missing_key_error_names_the_command_that_makes_one(fake_keyring, monkeypatch) -> None:
    monkeypatch.delenv("STUDIOFORGE_API_KEY", raising=False)
    spec = _spec("http://192.168.0.20:1234/v1")
    err = no_key_error(spec)
    assert err.code == "no_key" and err.action == "add_key"
    assert "studioforge keys create" in err.hint
    assert "Settings" in err.hint


async def test_no_stored_key_off_loopback_fails_before_the_network(
    fake_keyring, monkeypatch
) -> None:
    monkeypatch.delenv("STUDIOFORGE_API_KEY", raising=False)
    provider = RemoteProvider(_spec("http://192.168.0.20:1234/v1"))
    with pytest.raises(LLMError) as ei:
        await provider.client_for("m")
    assert ei.value.code == "no_key"
    assert "studioforge keys create" in ei.value.hint


async def test_a_401_without_a_key_tells_the_user_to_add_one(fake_keyring, monkeypatch) -> None:
    monkeypatch.delenv("STUDIOFORGE_API_KEY", raising=False)
    with fake_openai_server(_auth_gate) as srv:
        provider = RemoteProvider(_spec(srv.base_url))  # loopback: no key needed to start
        client = await provider.client_for("m")
        with pytest.raises(LLMError) as ei:
            await _drain(client, _request(provider))
        assert "authorization" not in srv.chat_requests[-1].headers  # truly sent no key
    err = ei.value
    assert err.code == "auth" and err.status == 401
    assert err.action == "add_key"  # the page shows the key card
    assert "StudioForge" in err.hint and "studioforge keys create" in err.hint
    assert "Settings" in err.hint


async def test_a_wrong_key_says_it_was_not_accepted(fake_keyring, monkeypatch) -> None:
    monkeypatch.delenv("STUDIOFORGE_API_KEY", raising=False)
    secrets.set_api_key("studioforge", "sf-key-wrong")
    with fake_openai_server(_auth_gate) as srv:
        provider = RemoteProvider(_spec(srv.base_url))
        client = await provider.client_for("m")
        with pytest.raises(LLMError) as ei:
            await _drain(client, _request(provider))
    assert ei.value.action == "open_settings"
    assert "not accepted" in ei.value.hint


async def test_the_right_key_is_sent_as_a_bearer_and_works(fake_keyring, monkeypatch) -> None:
    monkeypatch.delenv("STUDIOFORGE_API_KEY", raising=False)
    secrets.set_api_key("studioforge", GOOD)
    with fake_openai_server(_auth_gate) as srv:
        provider = RemoteProvider(_spec(srv.base_url))
        client = await provider.client_for("m")
        await _drain(client, _request(provider))
        assert srv.chat_requests[-1].headers["authorization"] == f"Bearer {GOOD}"


async def test_the_provider_test_button_gives_the_same_advice(fake_keyring, monkeypatch) -> None:
    monkeypatch.delenv("STUDIOFORGE_API_KEY", raising=False)
    with fake_openai_server(_auth_gate, models=None) as srv:
        spec = _spec(srv.base_url)
        result = await run_provider_test(spec, None, None)
    assert result["ok"] is False
    assert "studioforge keys create" in str(result)


def test_key_hint_for_a_provider_without_help_text_stays_generic() -> None:
    spec = seed_providers()["openai"]
    assert key_hint(spec).endswith("requires one.")
    assert "studioforge" not in key_hint(spec).lower()


def test_an_old_config_that_pinned_the_key_optional_is_migrated(chatforge_home) -> None:
    import tomli_w

    paths = Paths.default()

    paths.config_file.write_text(
        tomli_w.dumps(
            {
                "schema_version": 4,
                "providers": {
                    "studioforge": {"base_url": UNREACHABLE, "key_required": False},
                },
            }
        ),
        encoding="utf-8",
    )
    cfg = load_config(paths)
    sf = cfg.providers["studioforge"]
    assert sf.key_required is None
    assert sf.key_help == STUDIOFORGE_KEY_HELP
    assert cfg.startup.autostart_asked is True  # an existing install is not asked again
