"""Live MiniMax smoke (``pytest -m live_minimax -s``).

Skips unless ``aichat.secrets.get_api_key("minimax", "MINIMAX_API_KEY")`` resolves
(env var or Windows Credential Manager). Never prints the key.
"""

from __future__ import annotations

import datetime as _dt
import json
import time

import pytest

from aichat import secrets
from aichat.llm import probe
from aichat.llm.events import Completed, ContentDelta, ReasoningDelta
from aichat.llm.providers import SEED_PROVIDERS, RemoteProvider

pytestmark = pytest.mark.live_minimax

MODEL = "MiniMax-M3"

_TOOLS = [
    {
        "type": "function",
        "function": {
            "name": "current_datetime",
            "description": "Get the current local date and time.",
            "parameters": {"type": "object", "properties": {}, "required": []},
        },
    }
]


def _current_datetime_stub(_arguments: str) -> str:
    now = _dt.datetime.now().astimezone()
    return json.dumps({"iso": now.isoformat(timespec="seconds"), "weekday": now.strftime("%A")})


@pytest.fixture(scope="module")
def has_key() -> bool:
    key, source = secrets.get_api_key("minimax", "MINIMAX_API_KEY")
    if not key:
        pytest.skip("no MiniMax key (set MINIMAX_API_KEY or save one in Settings)")
    print(f"\n[live_minimax] key source: {source}")  # the source only, never the key
    return True


async def _stream(client, req):
    content, reasoning, done = [], [], None
    t0 = time.monotonic()
    first = None
    async for ev in client.stream_chat(req):
        if first is None and not isinstance(ev, Completed):
            first = time.monotonic() - t0
        if isinstance(ev, ContentDelta):
            content.append(ev.text)
        elif isinstance(ev, ReasoningDelta):
            reasoning.append(ev.text)
        elif isinstance(ev, Completed):
            done = ev
    return "".join(content), "".join(reasoning), done, first


def _reasoning_field(done: Completed) -> str:
    fields = [k for k in ("reasoning_details", "reasoning_content") if k in done.message.extras]
    if not fields and "<think>" in done.message.extras.get("raw_content", ""):
        fields = ["inline <think>"]
    return ", ".join(fields) or "none"


async def test_probe(has_key) -> None:
    spec = SEED_PROVIDERS["minimax"]
    key, _ = secrets.get_api_key(spec.id, spec.api_key_env)
    result = await probe.test_provider(spec, key, MODEL)
    safe = {k: v for k, v in result.items() if k != "models"}
    print(f"[live_minimax] probe: {safe} models_count={len(result.get('models') or [])}")
    assert result["ok"], result


async def test_stream_and_tool_round_trip(has_key) -> None:
    provider = RemoteProvider(SEED_PROVIDERS["minimax"])
    client = await provider.client_for(MODEL)
    async with client:
        # 1. plain streamed reply
        req = provider.make_request(
            MODEL, [{"role": "user", "content": "Say hello in five words."}]
        )
        text, _reasoning, done, ttft = await _stream(client, req)
        assert done is not None and text.strip()
        print(
            f"[live_minimax] reply: {text.strip()[:80]!r} reasoning_field={_reasoning_field(done)} "
            f"ttft={ttft:.2f}s total={done.elapsed_s:.2f}s tok/s={done.tok_per_s} "
            f"served={done.served_model}"
        )

        # 2. one current_datetime round trip, echoing the assistant message unchanged
        messages = [
            {"role": "system", "content": "Use tools when they help."},
            {"role": "user", "content": "What is the current date and time? Use the tool."},
        ]
        req = provider.make_request(MODEL, list(messages), _TOOLS)
        _t, _r, first, _ = await _stream(client, req)
        assert first is not None
        assert first.message.tool_calls, "expected a current_datetime tool call"
        call = first.message.tool_calls[0]
        assert call.name == "current_datetime"
        print(
            f"[live_minimax] tool call: {call.name} finish={first.finish_reason} "
            f"reasoning_field={_reasoning_field(first)} latency={first.elapsed_s:.2f}s"
        )
        messages.append(client.quirks.history_message(first.message))
        for c in first.message.tool_calls:
            messages.append(
                {
                    "role": "tool",
                    "tool_call_id": c.id,
                    "content": _current_datetime_stub(c.arguments),
                }
            )
        req = provider.make_request(MODEL, messages, _TOOLS)
        answer, _r, second, _ = await _stream(client, req)  # a 2013 here = echo rejected
        assert second is not None and answer.strip()
        print(
            f"[live_minimax] answer: {answer.strip()[:120]!r} "
            f"reasoning_field={_reasoning_field(second)} latency={second.elapsed_s:.2f}s"
        )
