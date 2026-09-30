"""chat.engine.ChatEngine against the fake OpenAI server (PLAN §1.6, §2 WS6 accept).

Remote providers are custom ``kind="openai"`` providers pointing at the fake server
(loopback, so no key is needed); the local provider is the real ``LocalModelManager``
over a stub supervisor whose "OVMS" is the fake server's ``/v3`` prefix.
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any

import pytest

from aichat.chat.engine import ChatEngine
from aichat.chat.history import PromptBudget, estimate_tokens
from aichat.config import validate_config
from aichat.llm.providers import ProviderRegistry
from aichat.paths import Paths
from aichat.runtime.manager import LocalModelManager
from aichat.tools.registry import ToolRegistry, ToolResult
from tests.fakes.openai_server import (
    CapturedRequest,
    Reply,
    chunk,
    fake_openai_server,
    json_reply,
    sse,
    text_chunks,
    tool_call_chunks,
    usage_chunk,
)
from tests.unit.test_manager import QWEN, TEST_CATALOG, StubSupervisor

# --------------------------------------------------------------------------- #
# Harness
# --------------------------------------------------------------------------- #


class Harness:
    def __init__(
        self,
        tmp_path: Path,
        base_url: str,
        *,
        chat: dict | None = None,
        tools: dict | None = None,
        local: dict | None = None,
        provider: str = "fake",
        quirks: list[str] | None = None,
        manager: bool = False,
    ) -> None:
        self.paths = Paths.from_home(tmp_path / "home")
        self.base_url = base_url
        self.cfg = validate_config(
            {
                "chat": {"provider": provider, "model": "fake-model", **(chat or {})},
                "local": {"idle_unload_minutes": 10, **(local or {})},
                "tools": tools or {},
                "providers": {
                    "fake": {
                        "id": "fake",
                        "kind": "openai",
                        "display_name": "Fake",
                        "base_url": base_url,
                        "models": ["fake-model"],
                        "default_model": "fake-model",
                        "quirks": quirks or [],
                        "region": "custom" if quirks and "minimax" in quirks else None,
                        "extra_body": {"reasoning_split": True} if quirks else {},
                    }
                },
            }
        )
        self.sup: StubSupervisor | None = None
        self.manager: LocalModelManager | None = None
        if manager:
            self.sup = ServingStub(base_url)
            self.manager = LocalModelManager(
                self.sup,
                get_config=lambda: self.cfg,
                paths=self.paths,
                catalog=TEST_CATALOG,
                is_installed=lambda: True,
            )
        self.providers = ProviderRegistry(
            self.cfg.providers, settings=lambda: self.cfg, manager=self.manager
        )
        self.tools = ToolRegistry(self.cfg.tools)
        self.search_calls: list[str] = []
        self.search_result = lambda q: ToolResult(True, f"1. {q} — https://x — snippet", "1 result")

        async def fake_search(query: str, max_results: int | None = None) -> ToolResult:
            self.search_calls.append(query)
            return self.search_result(query)

        self.tools._search.search = fake_search  # type: ignore[method-assign]
        self.events: list[dict] = []
        self.engine = self.new_engine()

    def new_engine(self) -> ChatEngine:
        return ChatEngine(
            providers=self.providers,
            tools=self.tools,
            get_config=lambda: self.cfg,
            conversation_file=self.paths.conversation_file,
            clock=lambda: 1_700_000_000.0,
        )

    async def send(self, text: str, rid: str = "r1") -> list[dict]:
        start = len(self.events)
        await self.engine.send(text, rid, self.events.append)
        return self.events[start:]

    def of(self, etype: str, events: list[dict] | None = None) -> list[dict]:
        return [e for e in (events if events is not None else self.events) if e["type"] == etype]


class ServingStub(StubSupervisor):
    """A stub supervisor whose loaded "model" is the fake server's /v3 endpoint."""

    def __init__(self, base_url: str) -> None:
        super().__init__()
        self.url = base_url

    async def start(self, spec, on_tick=None) -> str:
        await super().start(spec, on_tick)
        return self.url


def calc_call(cid: str, expr: str) -> Reply:
    return sse(tool_call_chunks([(cid, "calculator", json.dumps({"expression": expr}))]))


def body_has_tools(req: CapturedRequest) -> bool:
    return bool(req.json.get("tools"))


# --------------------------------------------------------------------------- #
# Event sequence
# --------------------------------------------------------------------------- #


async def test_event_sequence_and_fields(tmp_path) -> None:
    with fake_openai_server(sse(text_chunks("Hello there, friend.", 4))) as srv:
        h = Harness(tmp_path, srv.base_url)
        events = await h.send("Hi")
    types = [e["type"] for e in events]
    assert types[0] == "chat.start" and types[-1] == "chat.done"
    assert types[1] == "chat.phase" and events[1]["phase"] == "generating"
    assert set(types[2:-1]) == {"chat.delta"}
    assert all(e["request_id"] == "r1" for e in events)
    assert events[0] == {
        "type": "chat.start",
        "request_id": "r1",
        "provider": "fake",
        "model": "fake-model",
    }
    assert "".join(e.get("content", "") for e in h.of("chat.delta", events)) == (
        "Hello there, friend."
    )
    done = events[-1]
    assert done["finish_reason"] == "stop" and done["content"] == "Hello there, friend."
    assert done["usage"]["completion_tokens"] >= 2 and done["rounds"] == 1
    assert done["elapsed_s"] >= 0 and "tok_per_s" in done and done["model"] == "fake-model"
    # system prompt: short, dated
    sent = srv.chat_requests[0].json["messages"]
    assert sent[0]["role"] == "system" and "Today is" in sent[0]["content"]
    assert sent[-1] == {"role": "user", "content": "Hi"}
    # snapshot + persistence
    snap = h.engine.snapshot()
    assert snap["busy"] is False
    items = snap["conversation"]
    assert items[0] == {"role": "user", "content": "Hi", "ts": 1_700_000_000.0}
    assert items[1]["role"] == "assistant" and items[1]["content"] == "Hello there, friend."
    saved = json.loads(h.paths.conversation_file.read_text(encoding="utf-8"))
    assert [m["role"] for m in saved["messages"]] == ["user", "assistant"]


async def test_reasoning_deltas_emit_thinking_phase(tmp_path) -> None:
    reply = sse(
        [
            chunk({"role": "assistant", "reasoning_content": "Let me "}),
            chunk({"reasoning_content": "think."}),
            chunk({"content": "42"}),
            chunk({}, finish_reason="stop"),
            usage_chunk(),
        ]
    )
    with fake_openai_server(reply) as srv:
        h = Harness(tmp_path, srv.base_url)
        events = await h.send("Question?")
    phases = [e["phase"] for e in h.of("chat.phase", events)]
    assert phases == ["generating", "thinking", "generating"]
    assert "".join(e.get("reasoning", "") for e in h.of("chat.delta", events)) == "Let me think."
    assert h.engine.conversation_items()[1]["reasoning"] == "Let me think."


async def test_empty_content_with_reasoning_done_carries_final_content(tmp_path) -> None:
    reply = sse(
        [
            chunk({"role": "assistant", "reasoning_content": "The answer is 7."}),
            chunk({}, finish_reason="stop"),
            usage_chunk(),
        ]
    )
    with fake_openai_server(reply) as srv:
        h = Harness(tmp_path, srv.base_url)
        events = await h.send("?")
    assert not any("content" in e for e in h.of("chat.delta", events))
    assert events[-1]["content"] == "The answer is 7."  # differs from the (empty) stream
    assert h.engine.conversation_items()[1]["content"] == "The answer is 7."


# --------------------------------------------------------------------------- #
# Tool loop
# --------------------------------------------------------------------------- #


async def test_two_round_tool_loop(tmp_path) -> None:
    replies = (calc_call("c1", "2+2"), sse(text_chunks("It is 4.", 2)))
    with fake_openai_server(*replies) as srv:
        h = Harness(tmp_path, srv.base_url)
        events = await h.send("What is 2+2?")
        reqs = srv.chat_requests
    seq = [(e["type"], e.get("phase")) for e in events if e["type"] != "chat.delta"]
    assert seq == [
        ("chat.start", None),
        ("chat.phase", "generating"),
        ("chat.phase", "calling_tool"),
        ("chat.tool_call", None),
        ("chat.tool_result", None),
        ("chat.phase", "generating"),
        ("chat.done", None),
    ]
    call = h.of("chat.tool_call", events)[0]
    assert call["call_id"] == "c1" and call["name"] == "calculator"
    assert json.loads(call["arguments"]) == {"expression": "2+2"}
    result = h.of("chat.tool_result", events)[0]
    assert result["ok"] is True and result["call_id"] == "c1" and result["summary"]
    assert len(reqs) == 2
    second = reqs[1].json["messages"]
    assert second[-2]["role"] == "assistant" and second[-2]["tool_calls"][0]["id"] == "c1"
    assert second[-1]["role"] == "tool" and second[-1]["tool_call_id"] == "c1"
    assert "4" in second[-1]["content"]
    assert all(not k.startswith("_") for m in second for k in m)  # private keys stripped
    assert body_has_tools(reqs[0]) and body_has_tools(reqs[1])
    item = h.engine.conversation_items()[1]
    assert item["content"] == "It is 4."
    assert item["tools"][0] | {"summary": ""} == {
        "call_id": "c1",
        "name": "calculator",
        "arguments": '{"expression": "2+2"}',
        "ok": True,
        "summary": "",
    }
    assert events[-1]["rounds"] == 2


async def test_max_rounds_stop_then_final_answer_without_tools(tmp_path) -> None:
    counter = {"n": 0}

    def reply(req: CapturedRequest) -> Reply:
        if not req.json.get("tools"):
            return sse(text_chunks("Final answer.", 2))
        counter["n"] += 1
        return calc_call(f"c{counter['n']}", f"{counter['n']}+1")

    with fake_openai_server(default_reply=lambda: sse(text_chunks("unused", 1))) as srv:
        srv.enqueue(*([reply] * 5))
        h = Harness(tmp_path, srv.base_url, chat={"max_tool_rounds": 2})
        events = await h.send("Loop please")
        reqs = srv.chat_requests
    assert len(h.of("chat.tool_result", events)) == 2  # two executed rounds
    assert len(reqs) == 4  # 2 tool rounds + the refused 3rd + a final no-tools round
    assert [body_has_tools(r) for r in reqs] == [True, True, True, False]
    last = reqs[-1].json["messages"]
    assert last[-1]["role"] == "tool" and "limit" in last[-1]["content"]
    assert events[-1]["type"] == "chat.done" and events[-1]["content"] == "Final answer."
    tools_shown = h.engine.conversation_items()[1]["tools"]
    assert [t["call_id"] for t in tools_shown] == ["c1", "c2"]  # the refused call is hidden


async def test_duplicate_call_stops_the_loop(tmp_path) -> None:
    replies = (
        calc_call("c1", "6*7"),
        calc_call("c2", "6*7"),  # identical (name, arguments)
        sse(text_chunks("42.", 1)),
    )
    with fake_openai_server(*replies) as srv:
        h = Harness(tmp_path, srv.base_url)
        events = await h.send("6*7?")
        reqs = srv.chat_requests
    assert len(h.of("chat.tool_call", events)) == 1
    assert len(reqs) == 3 and not body_has_tools(reqs[2])
    assert "already made" in reqs[2].json["messages"][-1]["content"]
    assert events[-1]["content"] == "42."


async def test_disabled_tool_becomes_a_tool_message(tmp_path) -> None:
    replies = (
        sse(tool_call_chunks([("f1", "fetch_url", '{"url": "https://example.com"}')])),
        sse(text_chunks("Could not fetch.", 1)),
    )
    with fake_openai_server(*replies) as srv:
        h = Harness(tmp_path, srv.base_url, tools={"enabled": ["calculator"]})
        events = await h.send("Fetch it")
        reqs = srv.chat_requests
    result = h.of("chat.tool_result", events)[0]
    assert result["ok"] is False and "disabled" in result["summary"]
    assert "not enabled" in reqs[1].json["messages"][-1]["content"]
    assert [t["function"]["name"] for t in reqs[0].json["tools"]] == ["calculator"]
    assert events[-1]["type"] == "chat.done"


# --------------------------------------------------------------------------- #
# Errors, no_key, cancel
# --------------------------------------------------------------------------- #


async def test_no_key_gives_add_key_and_records_nothing(tmp_path, fake_keyring, monkeypatch):
    monkeypatch.delenv("MINIMAX_API_KEY", raising=False)
    with fake_openai_server() as srv:
        h = Harness(tmp_path, srv.base_url, provider="minimax", chat={"model": "MiniMax-M3"})
        events = await h.send("Hello MiniMax")
        assert srv.chat_requests == []
    assert [e["type"] for e in events] == ["chat.start", "chat.error"]
    err = events[-1]
    assert err["code"] == "no_key" and err["action"] == "add_key"
    assert err["message"] and err["hint"]
    assert h.engine.conversation_items() == []
    assert not h.paths.conversation_file.exists()


async def test_server_error_rolls_back_the_turn(tmp_path) -> None:
    with fake_openai_server(json_reply({"error": {"message": "boom"}}, status=500)) as srv:
        h = Harness(tmp_path, srv.base_url)
        events = await h.send("Hi")
    err = events[-1]
    assert err["type"] == "chat.error" and err["code"] == "server" and err["action"] == "retry"
    assert h.engine.conversation_items() == []


async def test_over_long_message_is_refused(tmp_path) -> None:
    with fake_openai_server() as srv:
        h = Harness(tmp_path, srv.base_url, chat={"max_prompt_chars": 10})
        events = await h.send("x" * 11)
        assert srv.chat_requests == []
    assert [e["type"] for e in events] == ["chat.error"]
    assert events[0]["code"] == "bad_request"
    assert h.engine.conversation_items() == []


async def test_cancel_keeps_partial_text(tmp_path) -> None:
    slow = sse(text_chunks("one two three four five six seven eight", 8), event_delay_s=0.15)
    with fake_openai_server(slow) as srv:
        h = Harness(tmp_path, srv.base_url)
        task = asyncio.create_task(h.engine.send("Count", "r9", h.events.append))
        async with asyncio.timeout(10):
            while not h.of("chat.delta"):
                await asyncio.sleep(0.01)
        assert h.engine.snapshot()["busy"] is True
        h.engine.cancel("r9")
        await task
    err = h.events[-1]
    assert err["type"] == "chat.error" and err["code"] == "cancelled"
    assert err["action"] is None and err["partial"] is True
    streamed = "".join(e.get("content", "") for e in h.of("chat.delta"))
    items = h.engine.conversation_items()
    assert items[0]["content"] == "Count"
    assert items[1]["content"] == streamed.strip() and items[1]["stopped"] is True
    saved = json.loads(h.paths.conversation_file.read_text(encoding="utf-8"))
    assert saved["messages"][-1]["_stopped"] is True
    assert h.engine.snapshot()["busy"] is False
    h.engine.cancel("r9")  # late cancel is a no-op


async def test_cancel_during_tool_call_repairs_history(tmp_path) -> None:
    with fake_openai_server(calc_call("c1", "1+1")) as srv:
        h = Harness(tmp_path, srv.base_url)
        gate = asyncio.Event()

        async def slow_call(*a: Any, **kw: Any) -> ToolResult:
            await gate.wait()
            return ToolResult(True, "2", "2")

        h.tools.call = slow_call  # type: ignore[method-assign]
        task = asyncio.create_task(h.engine.send("1+1", "r2", h.events.append))
        async with asyncio.timeout(10):
            while not h.of("chat.tool_call"):
                await asyncio.sleep(0.01)
        h.engine.cancel("r2")
        await task
    assert h.events[-1]["code"] == "cancelled"
    msgs = h.engine.conversation.messages
    assert msgs[-1]["role"] == "tool" and msgs[-1]["tool_call_id"] == "c1"
    assert msgs[-1]["_ok"] is False


async def test_new_chat_clears_and_persists(tmp_path) -> None:
    with fake_openai_server(sse(text_chunks("ok", 1))) as srv:
        h = Harness(tmp_path, srv.base_url)
        await h.send("Hi")
    assert h.engine.conversation_items()
    h.engine.new_chat()
    assert h.engine.conversation_items() == []
    saved = json.loads(h.paths.conversation_file.read_text(encoding="utf-8"))
    assert saved["messages"] == []
    assert h.new_engine().conversation_items() == []


async def test_persistence_off_deletes_the_file(tmp_path) -> None:
    with fake_openai_server(sse(text_chunks("ok", 1)), sse(text_chunks("ok", 1))) as srv:
        h = Harness(tmp_path, srv.base_url)
        await h.send("Hi")
        assert h.paths.conversation_file.exists()
        h.cfg.chat.persist_conversation = False
        await h.send("Again", "r2")
    assert not h.paths.conversation_file.exists()


async def test_context_overflow_retries_once_with_half_history(tmp_path) -> None:
    overflow = json_reply({"error": "Input length exceeds the maximum allowed length"}, status=400)
    with fake_openai_server(overflow, sse(text_chunks("Short answer.", 1))) as srv:
        h = Harness(tmp_path, srv.base_url)
        for i in range(4):
            h.engine.conversation.append({"role": "user", "content": f"old question {i}"})
            h.engine.conversation.append({"role": "assistant", "content": f"old answer {i}"})
        events = await h.send("New question")
        reqs = srv.chat_requests
    assert events[-1]["type"] == "chat.done"
    first, second = (r.json["messages"] for r in reqs)
    assert len(first) == 1 + 8 + 1
    assert len(second) == 1 + 4 + 1  # the older two of four old turns were dropped
    assert second[1]["content"] == "old question 2"


# --------------------------------------------------------------------------- #
# Local provider (manager over a stub supervisor)
# --------------------------------------------------------------------------- #


async def test_local_loading_phase_and_lease(tmp_path) -> None:
    with fake_openai_server(
        sse(text_chunks("Local hi.", 2)), sse(text_chunks("Again.", 1)), prefix="/v3"
    ) as srv:
        h = Harness(
            tmp_path, srv.base_url, provider="local-npu", chat={"model": QWEN}, manager=True
        )
        first = await h.send("Hi")
        second = await h.send("Hi again", "r2")
        reqs = srv.chat_requests
    assert [e["phase"] for e in h.of("chat.phase", first)][0] == "loading_model"
    assert "loading_model" not in [e["phase"] for e in h.of("chat.phase", second)]
    assert first[-1]["type"] == "chat.done" and first[-1]["content"] == "Local hi."
    body = reqs[0].json
    assert body["chat_template_kwargs"] == {"enable_thinking": False}
    assert body["model"] == QWEN and len(h.sup.starts) == 1
    assert h.manager.in_flight == 0 and h.manager.status()["state"] == "ready"


async def test_local_budget_trims_five_large_search_results(tmp_path) -> None:
    calls = [(f"s{i}", "web_search", json.dumps({"query": f"topic {i}"})) for i in range(5)]
    with fake_openai_server(
        sse(tool_call_chunks(calls)), sse(text_chunks("Summary.", 1)), prefix="/v3"
    ) as srv:
        h = Harness(
            tmp_path,
            srv.base_url,
            provider="local-npu",
            chat={"model": QWEN},
            tools={"tool_result_max_chars_local": 3000},
            manager=True,
        )
        h.search_result = lambda q: ToolResult(True, f"results for {q}: " + "w" * 4000, "5")
        for i in range(3):  # earlier big turns that no longer fit
            h.engine.conversation.append({"role": "user", "content": f"old {i} " + "u" * 3000})
            h.engine.conversation.append({"role": "assistant", "content": "a" * 3000})
        events = await h.send("Search five topics")
        reqs = srv.chat_requests
    assert events[-1]["type"] == "chat.done" and len(h.search_calls) == 5
    budget = PromptBudget(max_prompt_tokens=4096 - 128, tool_result_chars=3000)
    final = reqs[1].json
    assert estimate_tokens(final["messages"], final["tools"], budget) <= budget.max_prompt_tokens
    msgs = final["messages"]
    tools = [m for m in msgs if m["role"] == "tool"]
    assert len(tools) == 5  # all five kept, each shortened
    assert all(m["content"].startswith("results for topic") for m in tools)
    assert all(m["content"].endswith("[truncated]") and len(m["content"]) < 3000 for m in tools)
    users = [m["content"] for m in msgs if m["role"] == "user"]
    assert users == ["Search five topics"]  # the old turns were dropped whole
    # the assistant tool_calls message still precedes its five results
    idx = next(i for i, m in enumerate(msgs) if m.get("tool_calls"))
    assert [m["role"] for m in msgs[idx + 1 : idx + 6]] == ["tool"] * 5
    # the stored conversation is untouched by prompt trimming
    assert len(h.engine.conversation.messages) == 6 + 1 + 1 + 5 + 1


async def test_local_unload_during_request_reports_cancelled(tmp_path) -> None:
    slow = sse(text_chunks("a b c d e f g h i j", 10), event_delay_s=0.15)
    with fake_openai_server(slow, prefix="/v3") as srv:
        h = Harness(
            tmp_path, srv.base_url, provider="local-npu", chat={"model": QWEN}, manager=True
        )
        task = asyncio.create_task(h.engine.send("Talk", "r5", h.events.append))
        async with asyncio.timeout(10):
            while not h.of("chat.delta"):
                await asyncio.sleep(0.01)
        await h.manager.unload("user")
        await task
    err = h.events[-1]
    assert err["code"] == "cancelled" and "unloaded" in err["message"]
    assert h.manager.status()["state"] == "unloaded" and h.manager.in_flight == 0
    assert h.engine.conversation_items()[1]["stopped"] is True


# --------------------------------------------------------------------------- #
# MiniMax echo across a restart
# --------------------------------------------------------------------------- #


def minimax_tool_round() -> Reply:
    detail = {"type": "reasoning.text", "id": "rd-1", "format": "MiniMax-response-v1"}
    events = [
        chunk({"role": "assistant", "reasoning_details": [{**detail, "index": 0, "text": "Use "}]}),
        chunk({"reasoning_details": [{**detail, "index": 0, "text": "the calculator."}]}),
        chunk({"content": "Computing."}),
    ]
    events += tool_call_chunks([("call_mm1", "calculator", '{"expression": "3*3"}')])
    return sse(events)


@pytest.mark.parametrize("restart", [True])
async def test_minimax_echo_survives_persist_and_reload(tmp_path, restart) -> None:
    replies = (minimax_tool_round(), sse(text_chunks("It is 9.", 1)), sse(text_chunks("Yes.", 1)))
    with fake_openai_server(*replies) as srv:
        h = Harness(tmp_path, srv.base_url, quirks=["minimax"])
        await h.send("3*3?")
        echoed_live = srv.chat_requests[1].json["messages"][2]
        if restart:
            h.engine = h.new_engine()  # a fresh engine reads conversation.json
        await h.send("Sure?", "r2")
        after_restart = srv.chat_requests[2].json["messages"]
    assert echoed_live["role"] == "assistant"
    assert echoed_live["content"] == "Computing."
    assert echoed_live["reasoning_details"][0]["text"] == "Use the calculator."
    assert echoed_live["tool_calls"][0]["id"] == "call_mm1"
    replayed = after_restart[2]
    assert json.dumps(replayed, sort_keys=True) == json.dumps(echoed_live, sort_keys=True)
    assert all(not k.startswith("_") for m in after_restart for k in m)
    saved = json.loads(h.paths.conversation_file.read_text(encoding="utf-8"))
    mm = saved["messages"][1]
    assert mm["_origin"] == "minimax" and mm["_visible"] == "Computing."
    assert srv.chat_requests[0].json["reasoning_split"] is True


async def test_custom_system_prompt_keeps_tool_guidance(tmp_path) -> None:
    with fake_openai_server(sse(text_chunks("Arr.", 1)), sse(text_chunks("Arr.", 1))) as srv:
        h = Harness(tmp_path, srv.base_url, chat={"system_prompt": "You are a pirate."})
        await h.send("Hi")
        h.cfg.tools.enabled = []
        await h.send("Hi", "r2")
        with_tools, without = (r.json["messages"][0]["content"] for r in srv.chat_requests)
    assert with_tools.startswith("You are a pirate. Use tools only when they help.")
    assert "Use tools" not in without and "Today is" in without
