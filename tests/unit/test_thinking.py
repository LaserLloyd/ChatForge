"""Inline thinking split/strip and the incremental stream splitter."""

from __future__ import annotations

import random

import pytest

from aichat.llm.thinking import (
    StreamSplitter,
    TagRule,
    merge_reasoning,
    split_thinking,
    strip_thinking_tags,
)


@pytest.mark.parametrize(
    ("text", "visible", "think"),
    [
        ("", "", ""),
        ("plain answer", "plain answer", ""),
        ("<think>hmm</think>Answer", "Answer", "hmm"),
        ("<THINK>\nhmm\n</THINK>Answer", "Answer", "hmm"),
        ("<|thinking|>a<|/thinking|>B", "B", "a"),
        ("<|thinking|>a</|thinking|>B", "B", "a"),
        ("(think) gemma (think)Out", "Out", " gemma "),
        ("<think>unclosed reasoning", "", "unclosed reasoning"),
        ("stray</think>Answer", "strayAnswer", ""),
        ("<think>a</think>X<think>b</think>Y", "XY", "a\nb"),
    ],
)
def test_split_thinking(text: str, visible: str, think: str) -> None:
    assert split_thinking(text) == (visible, think)


def test_strip_thinking_tags_drops_blocks_and_is_idempotent() -> None:
    text = "<think>secret</think>Hello <|thinking|>x<|/thinking|>world</think>"
    once = strip_thinking_tags(text)
    assert once == "Hello world"
    assert strip_thinking_tags(once) == once
    assert strip_thinking_tags("before<think>never closed") == "before"


def test_merge_reasoning_rule() -> None:
    assert merge_reasoning("", "the answer", has_tool_calls=False, finish_reason="stop") == (
        "the answer"
    )
    assert merge_reasoning("shown", "r", has_tool_calls=False, finish_reason="stop") == "shown"
    # an unfinished thought is never an answer
    assert merge_reasoning("", "r", has_tool_calls=False, finish_reason="length") == ""
    # a pure tool call keeps empty content
    assert merge_reasoning("", "r", has_tool_calls=True, finish_reason="tool_calls") == ""


def _run(splitter: StreamSplitter, chunks: list[str]) -> tuple[str, str]:
    content, reasoning = [], []
    for c in chunks:
        for channel, text in splitter.feed(c):
            (content if channel == "content" else reasoning).append(text)
    for channel, text in splitter.flush():
        (content if channel == "content" else reasoning).append(text)
    return "".join(content), "".join(reasoning)


SAMPLES = [
    "<think>The capital of France.</think>Paris.",
    "Intro <|thinking|>deep</|thinking|> outro",
    "a < b and c > d, no tags here <thin but not a tag",
    "stray</think>after",
    "(think)g(think)visible",
    "<think>never closed",
    'text <tool_call>{"name": "x"}</tool_call> tail',
]


@pytest.mark.parametrize("text", SAMPLES)
def test_stream_splitter_matches_split_thinking_for_any_chunking(text: str) -> None:
    rules = (TagRule("<tool_call>", ("</tool_call>",), "drop"),)
    rng = random.Random(1234)
    expected_splitter = StreamSplitter(rules)
    expected = _run(expected_splitter, [text])
    for _ in range(60):
        cuts = sorted(rng.sample(range(1, len(text)), k=min(len(text) - 1, rng.randint(1, 8))))
        pieces = [text[i:j] for i, j in zip([0, *cuts], [*cuts, len(text)], strict=True)]
        assert _run(StreamSplitter(rules), pieces) == expected
    # without the drop rule the result equals the whole-text split
    if "<tool_call>" not in text:
        visible, think = split_thinking(text)
        got_content, got_reasoning = _run(StreamSplitter(), [text])
        assert got_content == visible
        assert got_reasoning.replace("\n", "") == think.replace("\n", "")


def test_stream_splitter_char_by_char() -> None:
    text = "<think>abc</think>Hello <b>world</b>"
    content, reasoning = _run(StreamSplitter(), list(text))
    assert content == "Hello <b>world</b>"
    assert reasoning == "abc"


def test_stream_splitter_holds_back_only_possible_tag_prefix() -> None:
    s = StreamSplitter()
    assert s.feed("Hello <th") == [("content", "Hello ")]
    assert s.feed("ere") == [("content", "<there")]
    assert s.flush() == []


def test_stream_splitter_drop_rule_hides_block() -> None:
    s = StreamSplitter((TagRule("<minimax:tool_call>", ("</minimax:tool_call>",), "drop"),))
    out = s.feed("Hi <minimax:tool_") + s.feed("call><invoke/></minimax:tool_call> bye") + s.flush()
    assert out == [("content", "Hi "), ("content", " bye")]
