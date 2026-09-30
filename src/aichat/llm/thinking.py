"""Inline thinking-tag handling: whole-text split plus an incremental stream splitter.

``_THINK_TAGS``, ``split_thinking`` and ``strip_thinking_tags`` are
Adapted from CrucibleForge crucibleforge/api.py (MIT, LaserLloyd).
``StreamSplitter`` and ``merge_reasoning`` are new.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

# Adapted from CrucibleForge crucibleforge/api.py (MIT, LaserLloyd): _THINK_TAGS.
# Thinking models (gemma `(think)`, deepseek/qwen `<think>`, qwen3 `<|thinking|>`)
# emit CoT wrapped in these delimiters. When the server does not extract reasoning
# into a separate channel, the tags leak into content.
_THINK_TAGS = re.compile(
    r"<think>|</think>|<\|thinking\|>|<\|/thinking\|>|</\|thinking\|>|\(think\)",
    re.IGNORECASE,
)
# Closers are matched case-insensitively like the openers (CrucibleForge used a
# case-sensitive str.find here, so "<THINK>...</THINK>" swallowed the answer).
_CLOSERS = {
    "<think>": re.compile(r"</think>", re.IGNORECASE),
    "<|thinking|>": re.compile(r"<\|/thinking\|>|</\|thinking\|>", re.IGNORECASE),
}


# Adapted from CrucibleForge crucibleforge/api.py (MIT, LaserLloyd): split_thinking.
def split_thinking(text: str) -> tuple[str, str]:
    """``(visible answer, inline thinking)``.

    Same parsing as :func:`strip_thinking_tags`, but the thinking blocks are kept:
    a provider that sends its reasoning inline in ``content`` (MiniMax-M3's
    ``<think>…</think>``) still lands it in the reasoning channel.
    """
    if not text:
        return text, ""
    out: list[str] = []
    think: list[str] = []
    pos = 0
    while True:
        m = _THINK_TAGS.search(text, pos)
        if not m:
            out.append(text[pos:])
            break
        tag = m.group(0).lower()
        if tag == "(think)":
            out.append(text[pos : m.start()])
            close = _THINK_TAGS.search(text, m.end())
            if not close:
                think.append(text[m.end() :])
                break
            think.append(text[m.end() : close.start()])
            pos = close.end()
        elif tag in ("<think>", "<|thinking|>"):
            out.append(text[pos : m.start()])
            close = _CLOSERS[tag].search(text, m.end())
            if not close:
                think.append(text[m.end() :])
                break
            think.append(text[m.end() : close.start()])
            pos = close.end()
        else:
            out.append(text[pos : m.start()])
            pos = m.end()
    return "".join(out), "\n".join(t.strip("\n") for t in think)


# Adapted from CrucibleForge crucibleforge/api.py (MIT, LaserLloyd): strip_thinking_tags.
def strip_thinking_tags(text: str) -> str:
    """Remove thinking blocks. An unclosed opener drops the remainder; stray
    closers are dropped as bare tokens. Idempotent."""
    if not text:
        return text
    out: list[str] = []
    pos = 0
    while True:
        m = _THINK_TAGS.search(text, pos)
        if not m:
            out.append(text[pos:])
            break
        tag = m.group(0).lower()
        if tag == "(think)":
            out.append(text[pos : m.start()])
            close = _THINK_TAGS.search(text, m.end())
            if not close:
                break
            pos = close.end()
        elif tag in ("<think>", "<|thinking|>"):
            out.append(text[pos : m.start()])
            close = _CLOSERS[tag].search(text, m.end())
            if not close:
                break
            pos = close.end()
        else:
            out.append(text[pos : m.start()])
            pos = m.end()
    return "".join(out)


def merge_reasoning(
    content: str, reasoning: str, *, has_tool_calls: bool, finish_reason: str | None
) -> str:
    """The visible text to show. Empty content with reasoning present shows the
    reasoning (a model that answered inside its thinking channel), unless the reply
    ran out of tokens (an unfinished thought is never an answer) or it is a tool call."""
    if content.strip() or has_tool_calls or finish_reason == "length":
        return content
    if reasoning.strip():
        return strip_thinking_tags(reasoning).strip()
    return content


# --------------------------------------------------------------------------- #
# Incremental splitter for streamed content
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class TagRule:
    """A tagged block inside streamed content. ``channel`` is ``"reasoning"`` (routed
    to ReasoningDelta) or ``"drop"`` (hidden from the visible stream)."""

    open: str
    closes: tuple[str, ...]
    channel: str


THINK_RULES: tuple[TagRule, ...] = (
    TagRule("<think>", ("</think>",), "reasoning"),
    TagRule("<|thinking|>", ("<|/thinking|>", "</|thinking|>"), "reasoning"),
    TagRule("(think)", ("(think)",), "reasoning"),
)
_STRAY_CLOSERS: tuple[str, ...] = ("</think>", "<|/thinking|>", "</|thinking|>")


def _alternation(tags: tuple[str, ...] | list[str]) -> re.Pattern[str]:
    ordered = sorted(set(tags), key=len, reverse=True)
    return re.compile("|".join(re.escape(t) for t in ordered), re.IGNORECASE)


def _partial_suffix_len(buf: str, tags: tuple[str, ...] | list[str]) -> int:
    """Length of the longest suffix of ``buf`` that is a proper prefix of a tag."""
    longest = max((len(t) for t in tags), default=0)
    low = buf[-longest:].lower() if longest else ""
    for k in range(min(longest - 1, len(low)), 0, -1):
        tail = low[-k:]
        if any(t.lower().startswith(tail) for t in tags):
            return k
    return 0


class StreamSplitter:
    """Routes streamed content text into ``content`` / ``reasoning`` pieces.

    Tags may be split across chunks: a trailing fragment that could be the start of
    a tag is held back until the next chunk (or :meth:`flush`). Semantics match
    :func:`split_thinking`: unclosed blocks run to the end, stray closers vanish.
    """

    def __init__(self, extra_rules: tuple[TagRule, ...] = ()) -> None:
        self._rules = THINK_RULES + tuple(extra_rules)
        self._by_open = {r.open.lower(): r for r in self._rules}
        self._outside_tags = [r.open for r in self._rules] + list(_STRAY_CLOSERS)
        self._open_re = _alternation(self._outside_tags)
        self._close_res = {r.open.lower(): _alternation(r.closes) for r in self._rules}
        self._active: TagRule | None = None
        self._buf = ""

    def feed(self, text: str) -> list[tuple[str, str]]:
        self._buf += text
        out: list[tuple[str, str]] = []
        while self._buf:
            if self._active is None:
                m = self._open_re.search(self._buf)
                if m:
                    out.append(("content", self._buf[: m.start()]))
                    self._active = self._by_open.get(m.group(0).lower())  # None: stray closer
                    self._buf = self._buf[m.end() :]
                    continue
                keep = _partial_suffix_len(self._buf, self._outside_tags)
                cut = len(self._buf) - keep
                out.append(("content", self._buf[:cut]))
                self._buf = self._buf[cut:]
                break
            rule = self._active
            m = self._close_res[rule.open.lower()].search(self._buf)
            if m:
                out.append((rule.channel, self._buf[: m.start()]))
                self._buf = self._buf[m.end() :]
                self._active = None
                continue
            keep = _partial_suffix_len(self._buf, rule.closes)
            cut = len(self._buf) - keep
            out.append((rule.channel, self._buf[:cut]))
            self._buf = self._buf[cut:]
            break
        return _compact(out)

    def flush(self) -> list[tuple[str, str]]:
        channel = self._active.channel if self._active else "content"
        out = [(channel, self._buf)]
        self._buf = ""
        self._active = None
        return _compact(out)


def _compact(pieces: list[tuple[str, str]]) -> list[tuple[str, str]]:
    merged: list[tuple[str, str]] = []
    for channel, text in pieces:
        if not text or channel == "drop":
            continue
        if merged and merged[-1][0] == channel:
            merged[-1] = (channel, merged[-1][1] + text)
        else:
            merged.append((channel, text))
    return merged
