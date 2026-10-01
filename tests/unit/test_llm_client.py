"""OpenAICompatClient against the stdlib fake OpenAI server (PLAN §5 "LLM client")."""

from __future__ import annotations

import asyncio
import socket
import time

import pytest

from chatforge.config import Timeouts
from chatforge.llm.client import ChatRequest, OpenAICompatClient
from chatforge.llm.errors import LLMError
from chatforge.llm.events import Completed, ContentDelta, ReasoningDelta
from chatforge.llm.minimax import MiniMaxQuirks
from chatforge.llm.quirks import GenericQuirks, OvmsQuirks
from tests.fakes.openai_server import (
    chunk,
    fake_openai_server,
    json_reply,
    raw_reply,
    scenario_429,
    scenario_base_resp,
    scenario_error_frame,
    scenario_inline_think,
    scenario_reasoning_content,
    scenario_reasoning_details,
    scenario_split_content,
    scenario_stall,
    scenario_tool_calls_split,
    sse,
    text_chunks,
    usage_chunk,
)

KEY = "sk-test-unit-000000"
FAST = Timeouts(connect_s=2, stall_s=5, wall_s=20)


def _req(**kw) -> ChatRequest:
    base = {"model": "fake-model", "messages": [{"role": "user", "content": "hi"}]}
    base.update(kw)
    return ChatRequest(**base)


async def _collect(client, req=None, cancel=None):
    content, reasoning, done = [], [], None
    async for ev in client.stream_chat(req or _req(), cancel):
        if isinstance(ev, ContentDelta):
            content.append(ev.text)
        elif isinstance(ev, ReasoningDelta):
            reasoning.append(ev.text)
        elif isinstance(ev, Completed):
            done = ev
    return content, reasoning, done


def _client(srv, quirks=None, timeouts=FAST, key=KEY) -> OpenAICompatClient:
    return OpenAICompatClient(
        srv.base_url, key, quirks=quirks or GenericQuirks(), timeouts=timeouts
    )


async def test_split_content_streams_and_completes() -> None:
    text = "Hello, world! Streaming works."
    with fake_openai_server(scenario_split_content(text)) as srv:
        async with _client(srv) as client:
            content, reasoning, done = await _collect(client, _req(temperature=0.3, max_tokens=77))
    assert "".join(content) == text
    assert len(content) >= 3  # arrived incrementally
    assert reasoning == []
    assert done is not None
    assert done.message.content == text
    assert done.finish_reason == "stop"
    assert done.served_model == "fake-model"
    assert done.usage["completion_tokens"] >= 2
    assert done.elapsed_s > 0
    # what was sent
    sent = srv.chat_requests[0]
    assert sent.headers["authorization"] == f"Bearer {KEY}"
    assert sent.json["stream"] is True
    assert sent.json["stream_options"] == {"include_usage": True}
    assert sent.json["max_tokens"] == 77
    assert sent.json["temperature"] == 0.3
    assert "tools" not in sent.json


async def test_no_key_sends_no_authorization_header() -> None:
    with fake_openai_server() as srv:
        async with _client(srv, key=None) as client:
            await _collect(client)
    assert "authorization" not in srv.chat_requests[0].headers


async def test_tool_calls_split_across_chunks_and_indexes() -> None:
    tools = [{"type": "function", "function": {"name": "current_datetime", "parameters": {}}}]
    with fake_openai_server(scenario_tool_calls_split()) as srv:
        async with _client(srv) as client:
            _, _, done = await _collect(client, _req(tools=tools))
    calls = done.message.tool_calls
    assert [(c.id, c.name, c.arguments) for c in calls] == [
        ("call_a", "current_datetime", "{}"),
        ("call_b", "web_search", '{"query": "npu news", "max_results": 3}'),
    ]
    assert done.finish_reason == "tool_calls"
    assert srv.last_body["tools"] == tools


async def test_reasoning_content() -> None:
    with fake_openai_server(scenario_reasoning_content("Let me think.", "42")) as srv:
        async with _client(srv) as client:
            content, reasoning, done = await _collect(client)
    assert "".join(reasoning) == "Let me think."
    assert "".join(content) == "42"
    assert done.message.reasoning == "Let me think."
    assert done.message.content == "42"


async def test_reasoning_details_generic() -> None:
    with fake_openai_server(scenario_reasoning_details("Done.")) as srv:
        async with _client(srv) as client:
            content, reasoning, done = await _collect(client)
    assert "".join(reasoning) == "Step one."
    assert done.message.reasoning == "Step one."
    assert done.message.content == "Done."


async def test_inline_think_is_routed_to_reasoning_while_streaming() -> None:
    with fake_openai_server(scenario_inline_think("Paris.")) as srv:
        async with _client(srv) as client:
            content, reasoning, done = await _collect(client)
    assert "".join(content) == "Paris."
    assert all("<" not in c for c in content)
    assert "".join(reasoning) == "The capital of France."
    assert done.message.content == "Paris."
    assert done.message.reasoning == "The capital of France."


async def test_empty_content_with_reasoning_shows_reasoning() -> None:
    reply = sse(
        [
            chunk({"role": "assistant", "reasoning_content": "Only reasoning here."}),
            chunk({}, finish_reason="stop"),
        ]
    )
    with fake_openai_server(reply) as srv:
        async with _client(srv) as client:
            _, _, done = await _collect(client)
    assert done.message.content == "Only reasoning here."


@pytest.mark.parametrize(
    ("code", "message", "expected"),
    [
        ("server_error", "upstream exploded", "server"),
        (429, "slow down", "rate_limit"),
        ("context_length_exceeded", "maximum context length is 4096 tokens", "context_overflow"),
    ],
)
async def test_error_frame(code, message, expected) -> None:
    with fake_openai_server(scenario_error_frame(message, code)) as srv:
        async with _client(srv) as client:
            seen: list[str] = []
            with pytest.raises(LLMError) as ei:
                async for ev in client.stream_chat(_req()):
                    if isinstance(ev, ContentDelta):
                        seen.append(ev.text)
    assert ei.value.code == expected
    assert "".join(seen) == "partial"  # deltas before the error were delivered


async def test_200_non_sse_base_resp_2049_minimax() -> None:
    with fake_openai_server(scenario_base_resp(2049, "invalid api key")) as srv:
        async with _client(srv, quirks=MiniMaxQuirks()) as client:
            with pytest.raises(LLMError) as ei:
                await _collect(client)
    err = ei.value
    assert err.code == "region_or_key"
    assert err.action == "open_settings"
    assert "api.minimax.io" in err.hint and "api.minimaxi.com" in err.hint


async def test_200_non_sse_body_generic_is_server_error() -> None:
    with fake_openai_server(raw_reply("<html>not an api</html>", content_type="text/html")) as srv:
        async with _client(srv) as client:
            with pytest.raises(LLMError) as ei:
                await _collect(client)
    assert ei.value.code == "server"
    assert "No SSE data" in ei.value.hint


async def test_200_non_stream_completion_is_accepted() -> None:
    body = {
        "model": "fake-model",
        "choices": [
            {
                "index": 0,
                "message": {
                    "role": "assistant",
                    "content": "whole",
                    "tool_calls": [
                        {
                            "id": "c1",
                            "type": "function",
                            "function": {"name": "a", "arguments": "{}"},
                        },
                        {
                            "id": "c2",
                            "type": "function",
                            "function": {"name": "b", "arguments": "{}"},
                        },
                    ],
                },
                "finish_reason": "tool_calls",
            }
        ],
        "usage": {"prompt_tokens": 3, "completion_tokens": 4},
    }
    with fake_openai_server(json_reply(body)) as srv:
        async with _client(srv) as client:
            content, _, done = await _collect(client)
    assert "".join(content) == "whole"
    assert [c.name for c in done.message.tool_calls] == ["a", "b"]


async def test_429_with_retry_after() -> None:
    with fake_openai_server(scenario_429("7")) as srv:
        async with _client(srv) as client:
            with pytest.raises(LLMError) as ei:
                await _collect(client)
    err = ei.value
    assert err.code == "rate_limit"
    assert err.retryable is True
    assert err.retry_after_s == 7
    assert err.action == "retry"


@pytest.mark.parametrize(
    ("status", "body", "expected"),
    [
        (401, {"error": {"message": "bad key"}}, "auth"),
        (
            400,
            {"error": {"message": "This model's maximum context length is 4096"}},
            "context_overflow",
        ),
        (400, {"error": {"message": "bad param"}}, "bad_request"),
        (404, {"error": {"message": "no such model"}}, "not_found"),
        (500, {"error": {"message": "boom"}}, "server"),
    ],
)
async def test_http_status_mapping(status, body, expected) -> None:
    with fake_openai_server(json_reply(body, status=status)) as srv:
        async with _client(srv) as client:
            with pytest.raises(LLMError) as ei:
                await _collect(client)
    assert ei.value.code == expected
    assert ei.value.status == status


async def test_stall_timeout() -> None:
    t = Timeouts(connect_s=2, stall_s=0.6, wall_s=20)
    with fake_openai_server(scenario_stall(after=1, stall_s=10)) as srv:
        async with _client(srv, timeouts=t) as client:
            start = time.monotonic()
            with pytest.raises(LLMError) as ei:
                await _collect(client)
            waited = time.monotonic() - start
    assert ei.value.code == "stalled"
    assert ei.value.retryable is True
    assert waited < 5


async def test_wall_timeout() -> None:
    t = Timeouts(connect_s=2, stall_s=5, wall_s=0.8)
    slow = sse(text_chunks("a slow but steady stream of text", 30), event_delay_s=0.1)
    with fake_openai_server(slow) as srv:
        async with _client(srv, timeouts=t) as client:
            with pytest.raises(LLMError) as ei:
                await _collect(client)
    assert ei.value.code == "timeout"


async def test_cancel_mid_stream_keeps_partial() -> None:
    slow = sse(text_chunks("one two three four five six", 12), event_delay_s=0.15)
    cancel = asyncio.Event()
    got: list[str] = []
    with fake_openai_server(slow) as srv:
        async with _client(srv) as client:
            with pytest.raises(LLMError) as ei:
                async for ev in client.stream_chat(_req(), cancel):
                    if isinstance(ev, ContentDelta):
                        got.append(ev.text)
                        cancel.set()
    assert ei.value.code == "cancelled"
    assert got and "".join(got) != "one two three four five six"


async def test_cancel_while_stalled() -> None:
    cancel = asyncio.Event()
    with fake_openai_server(scenario_stall(after=0, stall_s=10)) as srv:
        async with _client(srv) as client:
            asyncio.get_running_loop().call_later(0.3, cancel.set)
            start = time.monotonic()
            with pytest.raises(LLMError) as ei:
                await _collect(client, cancel=cancel)
    assert ei.value.code == "cancelled"
    assert time.monotonic() - start < 3


async def test_consumer_can_stop_early_without_hanging() -> None:
    slow = sse(text_chunks("x" * 50, 25), event_delay_s=0.05)
    with fake_openai_server(slow) as srv:
        async with _client(srv) as client:
            gen = client.stream_chat(_req())
            async for _ev in gen:
                break
            await gen.aclose()


async def test_unreachable() -> None:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
    client = OpenAICompatClient(
        f"http://127.0.0.1:{port}/v1", None, quirks=GenericQuirks(), timeouts=FAST
    )
    async with client:
        with pytest.raises(LLMError) as ei:
            await _collect(client)
    assert ei.value.code == "unreachable"


async def test_list_models_and_404() -> None:
    with fake_openai_server(models=["b-model", "a-model"]) as srv:
        async with _client(srv) as client:
            assert await client.list_models() == ["a-model", "b-model"]
        assert srv.requests[0].path == "/v1/models"
    with fake_openai_server(models=None) as srv:
        async with _client(srv) as client:
            assert await client.list_models() == []


async def test_list_models_405_and_auth_error() -> None:
    with fake_openai_server(models_reply=json_reply({"error": "no"}, status=405)) as srv:
        async with _client(srv) as client:
            assert await client.list_models() == []
    with fake_openai_server(models_reply=json_reply({"error": "no"}, status=401)) as srv:
        async with _client(srv) as client:
            with pytest.raises(LLMError) as ei:
                await client.list_models()
    assert ei.value.code == "auth"


def test_repr_never_contains_key() -> None:
    client = OpenAICompatClient("http://127.0.0.1:1/v1", KEY, quirks=GenericQuirks())
    assert KEY not in repr(client)


# --------------------------------------------------------------------------- #
# OVMS quirks through the client
# --------------------------------------------------------------------------- #


async def test_ovms_body_shape() -> None:
    history = [
        {"role": "system", "content": "sys"},
        {"role": "user", "content": "q"},
        {
            "role": "assistant",
            "content": "<think>r</think>raw minimax",
            "reasoning_details": [{"index": 0, "text": "r"}],
            "_origin": "minimax",
            "_visible": "raw minimax",
        },
        {"role": "user", "content": "again"},
    ]
    with fake_openai_server() as srv:
        client = OpenAICompatClient(
            srv.base_url, None, quirks=OvmsQuirks(enable_thinking=False), timeouts=FAST
        )
        async with client:
            await _collect(client, _req(messages=history, extra_body={"n": 2}))
    body = srv.last_body
    assert body["chat_template_kwargs"] == {"enable_thinking": False}
    assert "n" not in body
    assert body["stream_options"] == {"include_usage": True}
    assistant = body["messages"][2]
    assert assistant == {"role": "assistant", "content": "raw minimax"}
    assert all(not k.startswith("_") for m in body["messages"] for k in m)


async def test_ovms_tool_call_fallback_from_content() -> None:
    text = 'Sure.<tool_call>\n{"name": "current_datetime", "arguments": {}}\n</tool_call>'
    reply = sse(
        [
            chunk({"role": "assistant", "content": text[:12]}),
            chunk({"content": text[12:30]}),
            chunk({"content": text[30:]}),
            chunk({}, finish_reason="stop"),
            usage_chunk(),
        ]
    )
    with fake_openai_server(reply) as srv:
        client = OpenAICompatClient(srv.base_url, None, quirks=OvmsQuirks(), timeouts=FAST)
        async with client:
            content, _, done = await _collect(client)
    assert "".join(content) == "Sure."
    assert done.message.content == "Sure."
    assert [(c.name, c.arguments) for c in done.message.tool_calls] == [("current_datetime", "{}")]
    assert done.finish_reason == "tool_calls"
    hist = OvmsQuirks().history_message(done.message)
    assert hist == {
        "role": "assistant",
        "content": "Sure.",
        "tool_calls": [
            {
                "id": "call_0",
                "type": "function",
                "function": {"name": "current_datetime", "arguments": "{}"},
            }
        ],
    }


async def test_ovms_enable_thinking_true() -> None:
    with fake_openai_server() as srv:
        client = OpenAICompatClient(
            srv.base_url, None, quirks=OvmsQuirks(enable_thinking=True), timeouts=FAST
        )
        async with client:
            await _collect(client)
    assert srv.last_body["chat_template_kwargs"] == {"enable_thinking": True}
