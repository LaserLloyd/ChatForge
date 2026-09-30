"""chat.history: PromptBudget and fit_messages ordering (PLAN §1.6, §3)."""

from __future__ import annotations

import copy

import pytest

from aichat.chat.history import (
    CLOUD_CAP_TOKENS,
    HINT_SHORTEN,
    OLD_TOOL_RESULT_CHARS,
    PromptBudget,
    drop_oldest_groups,
    estimate_tokens,
    fit_messages,
    halve_history,
    split_groups,
)
from aichat.llm.errors import LLMError

SYSTEM = {"role": "system", "content": "You are AI Chat. Today is Wednesday 30 September 2026."}
TOOLS = [
    {
        "type": "function",
        "function": {
            "name": "web_search",
            "description": "Search the web.",
            "parameters": {"type": "object", "properties": {"query": {"type": "string"}}},
        },
    }
]


def user(text: str) -> dict:
    return {"role": "user", "content": text, "_ts": 1.0}


def assistant(text: str, calls: list[tuple[str, str]] | None = None) -> dict:
    msg: dict = {"role": "assistant", "content": text}
    if calls:
        msg["tool_calls"] = [
            {"id": cid, "type": "function", "function": {"name": name, "arguments": "{}"}}
            for cid, name in calls
        ]
    return msg


def tool(cid: str, content: str) -> dict:
    return {"role": "tool", "tool_call_id": cid, "content": content, "_ok": True}


def turn(i: int, size: int, *, with_tool: bool = True) -> list[dict]:
    msgs = [user(f"question {i} " + "q" * size)]
    if with_tool:
        msgs += [assistant("", [(f"c{i}", "web_search")]), tool(f"c{i}", "r" * size)]
    msgs.append(assistant(f"answer {i} " + "a" * size))
    return msgs


def assert_pairs_intact(messages: list[dict]) -> None:
    """Every tool message follows an assistant message that issued its call id."""
    open_ids: set[str] = set()
    for m in messages:
        if m["role"] == "assistant":
            open_ids = {c["id"] for c in m.get("tool_calls") or []}
        elif m["role"] == "tool":
            assert m["tool_call_id"] in open_ids, "tool result separated from its call"
        else:
            open_ids = set()


def test_prompt_budget_keywords_like_providers() -> None:
    b = PromptBudget(max_prompt_tokens=3968, tool_result_chars=1500)
    assert b.chars_per_token == 3.0 and b.is_local and b.limit_tokens == 3968
    cloud = PromptBudget(max_prompt_tokens=None, tool_result_chars=6000)
    assert not cloud.is_local and cloud.limit_tokens == CLOUD_CAP_TOKENS
    assert b.scaled(0.5).max_prompt_tokens == 1984
    assert cloud.scaled(0.5).max_prompt_tokens is None


def test_small_history_unchanged_and_private_keys_kept() -> None:
    history = [
        user("hi"),
        {"role": "assistant", "content": "raw", "_origin": "minimax", "_visible": "vis"},
    ]
    out = fit_messages(SYSTEM, history, TOOLS, PromptBudget(3968))
    assert out[0] == SYSTEM
    assert out[1:] == history
    assert out[2]["_origin"] == "minimax" and out[2]["_visible"] == "vis"


def test_private_keys_are_not_counted() -> None:
    b = PromptBudget(1000)
    plain = [user("hello")]
    heavy = [{**user("hello"), "_reasoning": "x" * 50_000}]
    assert estimate_tokens(plain, None, b) == estimate_tokens(heavy, None, b)


def test_inputs_not_mutated() -> None:
    history = turn(1, 2000) + turn(2, 2000) + [user("now")]
    before = copy.deepcopy(history)
    fit_messages(SYSTEM, history, TOOLS, PromptBudget(1500))
    assert history == before


def test_step1_old_tool_results_cut_to_300_on_local() -> None:
    history = turn(1, 1000) + [user("next")]
    out = fit_messages(SYSTEM, history, TOOLS, PromptBudget(3968))
    old_tool = next(m for m in out if m["role"] == "tool")
    assert len(old_tool["content"]) <= OLD_TOOL_RESULT_CHARS
    assert old_tool["content"].endswith("[truncated]")
    # nothing else touched
    assert out[1]["content"] == history[0]["content"]


def test_step1_cloud_keeps_old_tool_results_when_under_cap() -> None:
    history = turn(1, 1000) + [user("next")]
    out = fit_messages(SYSTEM, history, TOOLS, PromptBudget(None, tool_result_chars=6000))
    assert out[1:] == history


def test_step2_drops_oldest_whole_groups_never_splitting_tool_pairs() -> None:
    history = []
    for i in range(6):
        history += turn(i, 900)
    history += [user("latest question")]
    budget = PromptBudget(2000)
    out = fit_messages(SYSTEM, history, TOOLS, budget)
    body = out[1:]
    assert body[0]["role"] == "user"  # starts at a group boundary
    assert body[-1]["content"] == "latest question"
    assert_pairs_intact(body)
    assert estimate_tokens(out, TOOLS, budget) <= 2000
    # the newest old groups are the ones kept
    kept = [m["content"] for m in body if m["role"] == "user"]
    assert kept[-2].startswith("question 5")
    assert not any(k.startswith("question 0") for k in kept)


def test_step3_current_tool_results_share_the_budget() -> None:
    results = [tool(f"s{i}", f"result {i} " + "z" * 3000) for i in range(5)]
    history = [
        user("search five things"),
        assistant("", [(f"s{i}", "web_search") for i in range(5)]),
        *results,
    ]
    budget = PromptBudget(3968, tool_result_chars=3000)
    out = fit_messages(SYSTEM, history, TOOLS, budget)
    tools_out = [m for m in out if m["role"] == "tool"]
    assert len(tools_out) == 5  # none dropped, all shortened
    assert all(len(m["content"]) < 3000 for m in tools_out)
    assert all(m["content"].startswith(f"result {i}") for i, m in enumerate(tools_out))
    assert estimate_tokens(out, TOOLS, budget) <= 3968
    assert_pairs_intact(out[1:])


def test_step4_overflowing_core_raises_with_hint() -> None:
    history = turn(1, 100) + [user("x" * 20_000)]
    with pytest.raises(LLMError) as ei:
        fit_messages(SYSTEM, history, TOOLS, PromptBudget(3968))
    assert ei.value.code == "context_overflow"
    assert ei.value.hint == HINT_SHORTEN


def test_split_drop_and_halve_groups() -> None:
    history = [assistant("orphan")] + turn(1, 5) + turn(2, 5) + turn(3, 5) + [user("now")]
    groups = split_groups(history)
    assert [g[0]["role"] for g in groups] == ["assistant", "user", "user", "user", "user"]
    assert drop_oldest_groups(history, 2)[0]["content"].startswith("question 2")
    assert drop_oldest_groups(history, 99) == [user("now")]
    halved = halve_history(history)  # 4 old groups -> drop 2
    assert halved[0]["content"].startswith("question 2")
    assert halved[-1]["content"] == "now"
    assert halve_history([user("only")]) == [user("only")]
