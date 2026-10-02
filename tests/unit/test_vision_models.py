"""Which models can see pictures: the provider seeds, ``ProviderSpec.vision_for`` and what
``GET /models`` reports (model_refresh)."""

from __future__ import annotations

import asyncio

import pytest

from chatforge.config import ProviderSpec, seed_providers, validate_config
from chatforge.llm import model_refresh as mr
from tests.fakes.openai_server import fake_openai_server, json_reply


@pytest.mark.parametrize(
    ("provider", "model", "sees"),
    [
        ("openai", "gpt-4o-mini", True),
        ("openai", "gpt-4.1", True),
        ("openai", "gpt-5.5", True),
        ("openai", "GPT-6-Astra", True),
        ("openai", "o3", True),
        ("openai", "o4-mini-2025-04-16", True),
        ("openai", "o3-mini", False),
        ("openai", "o1-mini", False),
        ("openai", "gpt-3.5-turbo", False),
        ("openai", "gpt-4-turbo", True),
        ("openai", "gpt-4-turbo-2024-04-09", True),
        ("openai", "gpt-4-turbo-preview", False),
        ("minimax", "MiniMax-M3", True),
        ("minimax", "MiniMax-M3.1-Flash-Preview", True),
        ("minimax", "MiniMax-M2.7", False),
        ("deepseek", "deepseek-flash", True),
        ("deepseek", "deepseek-v4-flash-vision-exp", True),
        ("deepseek", "deepseek-chat", False),
        ("deepseek", "deepseek-v4-pro", False),
        ("studioforge", "unsloth/Qwen3.8-27B-GGUF/Qwen3.8-27B-Q5_K_S", False),
        ("local-npu", "OpenVINO/Qwen2.5-1.5B-Instruct-int4-ov", False),
    ],
)
def test_seeded_providers_know_their_vision_models(provider: str, model: str, sees: bool) -> None:
    assert seed_providers()[provider].vision_for(model) is sees


def test_what_the_provider_reported_wins() -> None:
    spec = ProviderSpec(
        id="lab",
        kind="openai",
        display_name="Lab",
        base_url="http://127.0.0.1:1234/v1",
        vision_models=["*-vl-*"],
        model_vision={"qwen2.5-vl-7b": False, "gemma-3-27b": True},
    )
    assert spec.vision_for("Qwen2.5-VL-3B") is True  # the pattern, any case
    assert spec.vision_for("qwen2.5-vl-7b") is False  # reported
    assert spec.vision_for("gemma-3-27b") is True  # reported
    assert spec.vision_for("llama-3") is False and spec.vision_for("") is False
    assert spec.model_copy(update={"vision_models": ["*"]}).vision_for("anything") is True


def test_an_existing_config_gets_the_seed_patterns() -> None:
    # A config.toml written before pictures existed has no vision_models for OpenAI.
    cfg = validate_config({"providers": {"openai": {"models": ["gpt-4o"]}}})
    assert cfg.providers["openai"].vision_for("gpt-4o") is True
    assert cfg.providers["openai"].model_vision == {}


@pytest.mark.parametrize(
    ("entry", "sees"),
    [
        ({"id": "m", "studioforge": {"kind": "chat", "vision": True}}, True),
        (
            {
                "id": "m",
                "studioforge": {"kind": "chat", "vision": False},
                "capabilities": ["tools"],
            },
            False,
        ),
        ({"id": "m", "type": "vlm"}, True),
        ({"id": "m", "architecture": {"input_modalities": ["text", "image"]}}, True),
        ({"id": "m", "architecture": {"input_modalities": ["text"]}}, False),
        ({"id": "m", "input_modalities": ["TEXT", "Image"]}, True),
        ({"id": "m", "capabilities": {"vision": True}}, True),
        ({"id": "m", "capabilities": ["tools", "vision"]}, True),
        ({"id": "m", "capabilities": ["tools"]}, None),  # does not say
        ({"id": "m", "type": "llm"}, None),
        ({"id": "m"}, None),
    ],
)
def test_vision_of_a_models_entry(entry: dict, sees: bool | None) -> None:
    assert mr.vision_of(entry) is sees


def test_fetch_models_reports_vision_for_the_models_that_say() -> None:
    listing = {
        "object": "list",
        "data": [
            {"id": "qwen-vl", "studioforge": {"kind": "chat", "vision": True}},
            {"id": "qwen-text", "studioforge": {"kind": "chat", "vision": False}},
            {"id": "plain"},
            {"id": "embedder", "studioforge": {"kind": "embedding", "vision": False}},
        ],
    }
    with fake_openai_server(models_reply=json_reply(listing)) as srv:
        spec = seed_providers()["studioforge"].model_copy(update={"base_url": srv.base_url})
        result = asyncio.run(mr.fetch_models(spec, None))
    assert result.ok and result.models == ["plain", "qwen-text", "qwen-vl"]
    assert result.vision == {"qwen-vl": True, "qwen-text": False}
