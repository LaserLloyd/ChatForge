"""chat.history: PromptBudget, fit_messages ordering and context overflow (PLAN §1.6, §3)."""

from __future__ import annotations

import copy

import pytest

from chatforge.attachments import NOTE_CUT_LOCAL
from chatforge.chat.history import (
    CLOUD_CAP_TOKENS,
    CONTEXT_NOTE,
    HINT_SYSTEM,
    HINT_TOO_LONG,
    MESSAGE_CUT_NOTE,
    OLD_TOOL_RESULT_CHARS,
    PromptBudget,
    drop_oldest_groups,
    estimate_tokens,
    fit_messages,
    fit_prompt,
    halve_history,
    split_groups,
)
from chatforge.llm.errors import LLMError

SYSTEM = {"role": "system", "content": "You are ChatForge. Today is Wednesday 30 September 2026."}
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


def stamped(history: list[dict]) -> list[dict]:
    """Copies with a distinct ``_ts`` per message (10.0, 11.0, ...)."""
    return [{**m, "_ts": 10.0 + i} for i, m in enumerate(history)]


def test_nothing_left_out_leaves_the_system_prompt_alone() -> None:
    history = stamped(turn(1, 50) + [user("next")])
    fitted = fit_prompt(SYSTEM, history, TOOLS, PromptBudget(3968))
    assert fitted.messages[0] == SYSTEM
    assert (fitted.dropped, fitted.first_kept_ts, fitted.message_cut) == (0, None, False)
    assert not fitted.left_out


def test_dropped_turns_add_one_fixed_line_to_the_system_prompt() -> None:
    history = []
    for i in range(6):
        history += turn(i, 900)
    history = stamped([*history, user("latest question")])
    budget = PromptBudget(2000)
    fitted = fit_prompt(SYSTEM, history, TOOLS, budget)
    system = fitted.messages[0]
    assert system["content"] == f"{SYSTEM['content']}\n{CONTEXT_NOTE}"
    assert system["content"].count("Too long for context") == 1
    # The note's tokens are counted: the whole prompt still fits.
    assert estimate_tokens(fitted.messages, TOOLS, budget) <= 2000
    body = fitted.messages[1:]
    assert body[0]["role"] == "user" and body[-1]["content"] == "latest question"
    assert fitted.dropped == len(history) - len(body)
    assert fitted.first_kept_ts == body[0]["_ts"]
    assert not fitted.message_cut and fitted.left_out
    # The wording never changes, however much is dropped (the provider's prefix cache).
    tighter = fit_prompt(SYSTEM, history, TOOLS, PromptBudget(1000))
    assert tighter.dropped > fitted.dropped
    assert tighter.messages[0] == system


def test_overlong_latest_message_is_cut_at_the_end_instead_of_raising() -> None:
    history = stamped(turn(1, 100) + [user("start " + "x" * 20_000 + " END")])
    budget = PromptBudget(3968)
    fitted = fit_prompt(SYSTEM, history, TOOLS, budget)
    latest = fitted.messages[-1]
    assert latest["content"].startswith("start xxx")
    assert latest["content"].endswith(MESSAGE_CUT_NOTE)
    assert "END" not in latest["content"]
    assert latest["_ts"] == history[-1]["_ts"]  # private keys kept
    assert estimate_tokens(fitted.messages, TOOLS, budget) <= 3968
    # Most of the window goes to the message: it is cut only as far as needed.
    assert len(latest["content"]) > 3968 * 3 * 0.6
    assert fitted.message_cut
    assert fitted.dropped == 4 and fitted.first_kept_ts == history[-1]["_ts"]
    assert fitted.messages[0]["content"].endswith(CONTEXT_NOTE)
    assert len(history[-1]["content"]) == 20_010  # the stored message is untouched


def test_current_tool_calls_and_results_are_kept_when_the_message_is_cut() -> None:
    results = [tool(f"s{i}", f"result {i} " + "z" * 3000) for i in range(3)]
    history = [
        user("y" * 12_000),
        assistant("", [(f"s{i}", "web_search") for i in range(3)]),
        *results,
    ]
    budget = PromptBudget(3968, tool_result_chars=3000)
    fitted = fit_prompt(SYSTEM, history, TOOLS, budget)
    out = fitted.messages
    assert [m["role"] for m in out] == ["system", "user", "assistant", "tool", "tool", "tool"]
    assert_pairs_intact(out[1:])
    assert all(m["content"].startswith(f"result {i}") for i, m in enumerate(out[3:]))
    assert out[1]["content"].endswith(MESSAGE_CUT_NOTE)
    assert fitted.message_cut and fitted.dropped == 0
    assert out[0] == SYSTEM  # nothing earlier was dropped, so no note
    assert estimate_tokens(out, TOOLS, budget) <= 3968


def test_attached_files_of_the_latest_message_count_as_cut() -> None:
    text = "word " * 5000
    record = {"name": "big.txt", "kind": "text", "chars": len(text), "truncated": False}
    history = [{**user("Summarise this"), "_attachments": [{**record, "text": text}]}]
    fitted = fit_prompt(SYSTEM, history, [], PromptBudget(1000))
    content = fitted.messages[-1]["content"]
    assert content.startswith("Summarise this\n\n<file") and NOTE_CUT_LOCAL in content
    assert MESSAGE_CUT_NOTE not in content  # the files shared the room; the text is whole
    assert fitted.message_cut and fitted.dropped == 0


def test_a_kept_file_turn_does_not_hide_the_turns_dropped_after_it() -> None:
    file_text = {"name": "spec.txt", "kind": "text", "chars": 16, "truncated": False}
    history = stamped(
        [
            user("old " + "a" * 1500),
            assistant("b" * 1500),
            {**user("Read this"), "_attachments": [{**file_text, "text": "the spec says 42"}]},
            assistant("Read it."),
            user("later " + "c" * 1500),
            assistant("d" * 1500),
            user("What number does the spec give?"),
        ]
    )
    fitted = fit_prompt(SYSTEM, history, [], PromptBudget(max_prompt_tokens=700))
    assert [m["_ts"] for m in fitted.messages[1:]] == [12.0, 13.0, 16.0]
    assert fitted.dropped == 4
    # The model's unbroken view starts at the latest message, after the dropped "later" turn.
    assert fitted.first_kept_ts == 16.0


def test_overflow_retry_drops_the_older_half_and_reports_it() -> None:
    history = stamped(turn(1, 5) + turn(2, 5) + turn(3, 5) + turn(4, 5) + [user("now")])
    fitted = fit_prompt(SYSTEM, history, TOOLS, PromptBudget(None), halve=True)
    body = fitted.messages[1:]
    assert body[0]["content"].startswith("question 3")
    assert fitted.dropped == 8 and fitted.first_kept_ts == body[0]["_ts"]
    assert fitted.messages[0]["content"].endswith(CONTEXT_NOTE)
    assert body == fit_messages(SYSTEM, halve_history(history), TOOLS, PromptBudget(None))[1:]


def test_a_system_prompt_that_cannot_fit_raises() -> None:
    huge = {"role": "system", "content": "Be helpful. " * 2000}
    with pytest.raises(LLMError) as ei:
        fit_prompt(huge, [user("hi")], TOOLS, PromptBudget(3968))
    assert ei.value.code == "context_overflow"
    assert ei.value.hint == HINT_SYSTEM
    assert "system prompt" in ei.value.message


def test_a_turn_whose_own_tool_results_cannot_fit_still_raises() -> None:
    calls = [(f"t{i}", "web_search") for i in range(30)]
    history = [user("q"), assistant("", calls), *[tool(c, "r" * 3000) for c, _ in calls]]
    with pytest.raises(LLMError) as ei:
        fit_prompt(SYSTEM, history, TOOLS, PromptBudget(1000))
    assert ei.value.code == "context_overflow"
    assert ei.value.hint == HINT_TOO_LONG


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
