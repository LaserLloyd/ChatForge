"""chat.conversation: UI items, repair, trim and atomic persistence."""

from __future__ import annotations

import json

from aichat.chat import conversation as store
from aichat.chat.conversation import Conversation


def _call(cid: str, name: str, args: str = "{}") -> dict:
    return {"id": cid, "type": "function", "function": {"name": name, "arguments": args}}


def sample() -> Conversation:
    return Conversation(
        [
            {"role": "user", "content": "What's new?", "_ts": 10.0},
            {
                "role": "assistant",
                "content": "Let me look.",
                "tool_calls": [_call("c1", "web_search", '{"query": "news"}')],
                "_ts": 11.0,
                "_reasoning": "Need a search.",
                "_model": "m1",
            },
            {
                "role": "tool",
                "tool_call_id": "c1",
                "content": "1. result",
                "_name": "web_search",
                "_ok": True,
                "_summary": "5 results",
            },
            {
                "role": "assistant",
                "content": "<think>x</think>Here it is.",
                "_visible": "Here it is.",
                "_origin": "minimax",
                "reasoning_details": [{"type": "reasoning.text", "text": "x"}],
                "_ts": 12.0,
                "_reasoning": "Summarise.",
                "_model": "m1",
            },
        ]
    )


def test_items_fold_rounds_into_one_assistant_item() -> None:
    items = sample().items()
    assert items[0] == {"role": "user", "content": "What's new?", "ts": 10.0}
    a = items[1]
    assert a["role"] == "assistant"
    assert a["content"] == "Let me look.\n\nHere it is."  # visible text, not raw MiniMax
    assert a["reasoning"] == "Need a search.\n\nSummarise."
    assert a["ts"] == 12.0 and a["model"] == "m1"
    assert a["tools"] == [
        {
            "call_id": "c1",
            "name": "web_search",
            "arguments": '{"query": "news"}',
            "ok": True,
            "summary": "5 results",
        }
    ]
    assert len(items) == 2


def test_hidden_tool_results_are_not_chips() -> None:
    conv = Conversation(
        [
            {"role": "user", "content": "q", "_ts": 1.0},
            {"role": "assistant", "content": "", "tool_calls": [_call("c9", "calculator")]},
            {"role": "tool", "tool_call_id": "c9", "content": "Not run", "_hidden": True},
            {"role": "assistant", "content": "done"},
        ]
    )
    item = conv.items()[1]
    assert "tools" not in item and item["content"] == "done"


def test_repair_answers_dangling_tool_calls() -> None:
    conv = Conversation(
        [
            {"role": "user", "content": "q"},
            {
                "role": "assistant",
                "content": "",
                "tool_calls": [_call("a", "clock"), _call("b", "calculator")],
            },
            {"role": "tool", "tool_call_id": "a", "content": "12:00", "_ok": True},
        ]
    )
    assert conv.repair_tool_calls(ts=5.0) == 1
    last = conv.messages[-1]
    assert last["role"] == "tool" and last["tool_call_id"] == "b"
    assert last["_ok"] is False and last["content"] == "Not run (stopped)."
    assert conv.repair_tool_calls() == 0


def test_rollback_and_trim_by_whole_groups() -> None:
    conv = Conversation()
    for i in range(10):
        conv.append({"role": "user", "content": f"u{i}"})
        conv.append({"role": "assistant", "content": f"a{i}"})
    mark = len(conv)
    conv.append({"role": "user", "content": "extra"})
    conv.rollback(mark)
    assert len(conv) == 20
    conv.trim(max_messages=7)
    assert conv.messages[0] == {"role": "user", "content": "u7"}
    assert len(conv) == 6


def test_save_load_round_trip_keeps_private_keys(tmp_path) -> None:
    path = tmp_path / "conversation.json"
    conv = sample()
    store.save(path, conv)
    raw = json.loads(path.read_text(encoding="utf-8"))
    assert raw["schema"] == 1
    loaded = store.load(path)
    assert loaded.messages == conv.messages
    assert loaded.messages[3]["_origin"] == "minimax"
    assert loaded.messages[3]["_visible"] == "Here it is."
    assert not list(tmp_path.glob("*.tmp"))


def test_load_missing_and_corrupt(tmp_path) -> None:
    path = tmp_path / "conversation.json"
    assert len(store.load(path)) == 0
    path.write_text("{not json", encoding="utf-8")
    assert len(store.load(path)) == 0
    assert (tmp_path / "conversation.json.bad").exists()
    assert not path.exists()
    store.delete(path)  # idempotent
