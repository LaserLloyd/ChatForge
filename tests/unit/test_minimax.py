"""MiniMax quirks: body shape, base_resp mapping, reasoning fields, echo, XML strip."""

from __future__ import annotations

import json

import pytest

from aichat.config import Timeouts
from aichat.llm.client import ChatRequest, OpenAICompatClient
from aichat.llm.errors import LLMError
from aichat.llm.events import AssistantMessage, Completed, ContentDelta, ToolCall
from aichat.llm.minimax import (
    MiniMaxQuirks,
    base_resp_error,
    merge_reasoning_details,
    strip_minimax_tool_call_xml,
)
from aichat.llm.quirks import OvmsQuirks
from tests.fakes.openai_server import chunk, fake_openai_server, sse, usage_chunk

FAST = Timeouts(connect_s=2, stall_s=5, wall_s=20)
MODEL = "MiniMax-M3"


async def _run(client, req):
    content, done = [], None
    async for ev in client.stream_chat(req):
        if isinstance(ev, ContentDelta):
            content.append(ev.text)
        elif isinstance(ev, Completed):
            done = ev
    return "".join(content), done


def test_prepare_body_shape() -> None:
    body = {
        "model": MODEL,
        "messages": [{"role": "user", "content": "hi"}],
        "max_tokens": 512,
        "temperature": 5,
        "presence_penalty": 0.5,
        "frequency_penalty": 0.5,
        "logit_bias": {"1": 2},
        "n": 2,
        "stream": True,
    }
    out = MiniMaxQuirks().prepare_body(body)
    assert out["reasoning_split"] is True
    assert out["max_completion_tokens"] == 512
    assert "max_tokens" not in out
    for k in ("presence_penalty", "frequency_penalty", "logit_bias", "n"):
        assert k not in out
    assert out["temperature"] == 2.0
    assert out["stream_options"] == {"include_usage": True}
    assert body["max_tokens"] == 512  # input not mutated
    assert MiniMaxQuirks().prepare_body({"messages": [], "temperature": -1})["temperature"] == 0.0


async def test_request_body_over_the_wire() -> None:
    with fake_openai_server() as srv:
        client = OpenAICompatClient(
            srv.base_url, "sk-cp-test-000000", quirks=MiniMaxQuirks(), timeouts=FAST
        )
        async with client:
            await _run(
                client,
                ChatRequest(
                    model=MODEL,
                    messages=[{"role": "user", "content": "hi"}],
                    temperature=1.0,
                    max_tokens=4096,
                    extra_body={"reasoning_split": True, "presence_penalty": 1},
                ),
            )
    body = srv.last_body
    assert body["reasoning_split"] is True
    assert body["max_completion_tokens"] == 4096
    assert "max_tokens" not in body and "presence_penalty" not in body
    assert body["temperature"] == 1.0


@pytest.mark.parametrize(
    ("status", "code", "action", "retryable"),
    [
        (1004, "region_or_key", "open_settings", False),
        (2049, "region_or_key", "open_settings", False),
        (1002, "rate_limit", "retry", True),
        (1008, "balance", None, False),
        (1039, "context_overflow", None, False),
        (2013, "bad_request", None, False),
        (2056, "quota", "retry", False),
        (1000, "server", "retry", True),
    ],
)
def test_base_resp_mapping(status, code, action, retryable) -> None:
    err = base_resp_error({"status_code": status, "status_msg": "msg"})
    assert isinstance(err, LLMError)
    assert (err.code, err.action, err.retryable) == (code, action, retryable)
    assert err.details["base_resp_code"] == status
    if status in (1004, 2049):
        assert err.hint.startswith(
            "International keys use api.minimax.io; China keys use api.minimaxi.com"
        )
    if status == 2056:
        assert "resets within 5 h" in err.hint


def test_base_resp_ok_is_ignored() -> None:
    assert base_resp_error({"status_code": 0, "status_msg": "success"}) is None
    assert base_resp_error(None) is None
    MiniMaxQuirks().check_payload({"base_resp": {"status_code": 0}})


async def test_base_resp_checked_on_sse_chunks() -> None:
    reply = sse(
        [
            chunk({"role": "assistant", "content": "par"}),
            chunk({}, base_resp={"status_code": 1002, "status_msg": "rate limit"}),
        ]
    )
    with fake_openai_server(reply) as srv:
        client = OpenAICompatClient(srv.base_url, "k" * 10, quirks=MiniMaxQuirks(), timeouts=FAST)
        async with client:
            with pytest.raises(LLMError) as ei:
                await _run(client, ChatRequest(model=MODEL, messages=[]))
    assert ei.value.code == "rate_limit"
    assert ei.value.retryable is True


async def test_base_resp_in_http_error_body() -> None:
    from tests.fakes.openai_server import json_reply

    reply = json_reply(
        {"base_resp": {"status_code": 1039, "status_msg": "token limit"}}, status=400
    )
    with fake_openai_server(reply) as srv:
        client = OpenAICompatClient(srv.base_url, "k" * 10, quirks=MiniMaxQuirks(), timeouts=FAST)
        async with client:
            with pytest.raises(LLMError) as ei:
                await _run(client, ChatRequest(model=MODEL, messages=[]))
    assert ei.value.code == "context_overflow"


def test_merge_reasoning_details_by_index() -> None:
    parts = [
        {"reasoning_details": [{"type": "reasoning.text", "id": "r1", "index": 0, "text": "A"}]},
        {"reasoning_details": [{"index": 1, "type": "reasoning.text", "id": "r2", "text": "X"}]},
        {"reasoning_details": [{"index": 0, "text": "B", "signature": "sig"}]},
        {"reasoning_content": "ignored here"},
    ]
    assert merge_reasoning_details(parts) == [
        {"type": "reasoning.text", "id": "r1", "index": 0, "text": "AB", "signature": "sig"},
        {"index": 1, "type": "reasoning.text", "id": "r2", "text": "X"},
    ]
    assert merge_reasoning_details([{"reasoning_content": "x"}]) is None


def test_strip_minimax_tool_call_xml() -> None:
    text = (
        'Checking.\n<minimax:tool_call>\n<invoke name="web_search">'
        '<parameter name="query">x</parameter></invoke>\n</minimax:tool_call>\nDone.'
    )
    assert strip_minimax_tool_call_xml(text) == "Checking.\n\n\n\nDone."
    code = 'Example:\n```\n<minimax:tool_call><invoke name="a"></invoke></minimax:tool_call>\n```\n'
    assert strip_minimax_tool_call_xml(code) == code
    assert strip_minimax_tool_call_xml("plain") == "plain"


def test_finalize_strips_xml_and_keeps_raw() -> None:
    raw = (
        '<think>hmm</think>Answer <minimax:tool_call><invoke name="x"></invoke></minimax:tool_call>'
    )
    msg = MiniMaxQuirks().finalize(raw, [{"reasoning_content": "rc"}], [])
    assert msg.content == "Answer"
    assert msg.reasoning == "rc\nhmm"
    assert msg.extras["raw_content"] == raw
    assert msg.extras["reasoning_content"] == "rc"
    assert "reasoning_details" not in msg.extras


# --------------------------------------------------------------------------- #
# The echo: the second request carries the first turn's message unchanged
# --------------------------------------------------------------------------- #

_DETAIL = {"type": "reasoning.text", "id": "reasoning-text-1", "format": "MiniMax-response-v1"}
_ARGS = '{"timezone": "local"}'


def _turn_one_reply():
    return sse(
        [
            chunk(
                {
                    "role": "assistant",
                    "reasoning_details": [{**_DETAIL, "index": 0, "text": "Need "}],
                },
                model=MODEL,
            ),
            chunk(
                {"reasoning_details": [{**_DETAIL, "index": 0, "text": "the time."}]}, model=MODEL
            ),
            chunk({"content": "<think>inline</think>Let me check. "}, model=MODEL),
            chunk(
                {
                    "tool_calls": [
                        {
                            "index": 0,
                            "id": "call_function_abc_1",
                            "type": "function",
                            "function": {"name": "current_datetime", "arguments": _ARGS[:9]},
                        }
                    ]
                },
                model=MODEL,
            ),
            chunk(
                {"tool_calls": [{"index": 0, "function": {"arguments": _ARGS[9:]}}]}, model=MODEL
            ),
            chunk({}, finish_reason="tool_calls", model=MODEL),
            usage_chunk(model=MODEL),
        ]
    )


async def test_second_turn_echo_is_byte_equal() -> None:
    with fake_openai_server(_turn_one_reply()) as srv:
        quirks = MiniMaxQuirks()
        client = OpenAICompatClient(srv.base_url, "k" * 10, quirks=quirks, timeouts=FAST)
        async with client:
            messages = [{"role": "user", "content": "What time is it?"}]
            visible, done = await _run(client, ChatRequest(model=MODEL, messages=list(messages)))
            assert visible == "Let me check. "
            assert done.message.content == "Let me check."
            assert done.message.reasoning == "Need the time.\ninline"
            messages.append(quirks.history_message(done.message))
            messages.append(
                {
                    "role": "tool",
                    "tool_call_id": "call_function_abc_1",
                    "content": "2026-09-30T12:00",
                }
            )
            await _run(client, ChatRequest(model=MODEL, messages=messages))
    second = srv.chat_requests[1]
    sent_assistant = second.json["messages"][1]
    expected = {
        "role": "assistant",
        "content": "<think>inline</think>Let me check. ",
        "tool_calls": [
            {
                "id": "call_function_abc_1",
                "type": "function",
                "function": {"name": "current_datetime", "arguments": _ARGS},
            }
        ],
        "reasoning_details": [{**_DETAIL, "index": 0, "text": "Need the time."}],
    }
    assert json.dumps(sent_assistant, sort_keys=True) == json.dumps(expected, sort_keys=True)
    # the private bookkeeping keys never go over the wire
    assert b'"_origin"' not in second.body and b'"_visible"' not in second.body
    assert second.json["messages"][2]["role"] == "tool"


async def test_reasoning_content_variant_is_echoed() -> None:
    reply = sse(
        [
            chunk({"role": "assistant", "reasoning_content": "thinking"}, model=MODEL),
            chunk({"content": "Hi!"}, model=MODEL),
            chunk({}, finish_reason="stop", model=MODEL),
        ]
    )
    with fake_openai_server(reply) as srv:
        quirks = MiniMaxQuirks()
        client = OpenAICompatClient(srv.base_url, "k" * 10, quirks=quirks, timeouts=FAST)
        async with client:
            _, done = await _run(client, ChatRequest(model=MODEL, messages=[]))
    hist = quirks.history_message(done.message)
    assert hist["reasoning_content"] == "thinking"
    assert "reasoning_details" not in hist
    assert hist["content"] == "Hi!"


def test_minimax_history_goes_to_local_as_visible_only() -> None:
    msg = AssistantMessage(
        content="Answer",
        reasoning="r",
        tool_calls=[ToolCall("c1", "calculator", '{"expression": "1+1"}')],
        extras={"raw_content": "<think>r</think>Answer", "reasoning_details": [{"index": 0}]},
    )
    hist = MiniMaxQuirks().history_message(msg)
    local = OvmsQuirks().prepare_body({"messages": [hist]})["messages"][0]
    assert local == {
        "role": "assistant",
        "content": "Answer",
        "tool_calls": [
            {
                "id": "c1",
                "type": "function",
                "function": {"name": "calculator", "arguments": '{"expression": "1+1"}'},
            }
        ],
    }
    # and a foreign assistant message sent to MiniMax loses foreign reasoning fields
    foreign = {"role": "assistant", "content": "x", "reasoning_content": "other provider"}
    assert MiniMaxQuirks().prepare_body({"messages": [foreign]})["messages"][0] == {
        "role": "assistant",
        "content": "x",
    }


async def test_minimax_xml_hidden_while_streaming() -> None:
    reply = sse(
        [
            chunk({"role": "assistant", "content": "Look <minimax:tool"}, model=MODEL),
            chunk(
                {"content": '_call><invoke name="x"></invoke></minimax:tool_call> ok'}, model=MODEL
            ),
            chunk({}, finish_reason="stop", model=MODEL),
        ]
    )
    with fake_openai_server(reply) as srv:
        client = OpenAICompatClient(srv.base_url, "k" * 10, quirks=MiniMaxQuirks(), timeouts=FAST)
        async with client:
            visible, done = await _run(client, ChatRequest(model=MODEL, messages=[]))
    assert visible == "Look  ok"
    assert done.message.content == "Look  ok"
