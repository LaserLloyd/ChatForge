"""Fit a conversation into a model's prompt window (PLAN §1.4 ``chat/history.py``, §1.6, §3).

The local NPU runs with a static prompt window (``local.max_prompt_len``, 4096 by
default), so every request is trimmed before it is sent. The order is fixed:

1. old tool results (before the latest user message) are cut to 300 characters;
2. the oldest whole turn groups are dropped (a group starts at a user message, so an
   assistant ``tool_calls`` message is never separated from its tool messages);
3. the current turn's tool results are cut to share what is left of the budget;
4. if the system prompt, the tool schemas and the latest user message alone do not
   fit, ``LLMError(context_overflow)`` is raised with a "shorten or switch" hint.

Token counts are estimated at ``chars_per_token`` (3.0, conservative for Qwen). A
cloud budget (``max_prompt_tokens=None``) only trims past a generous cap.

History messages may carry private ``_``-prefixed keys (``_origin``, ``_visible``,
``_ts`` ...). They are kept on the returned copies (quirks strip them before sending)
and ignored when estimating size.
"""

from __future__ import annotations

import json
import math
from dataclasses import dataclass

from aichat.llm.errors import LLMError

#: Old tool results are cut to this many characters (step 1).
OLD_TOOL_RESULT_CHARS = 300
#: The smallest share a current tool result is cut to before giving up (step 3).
MIN_TOOL_RESULT_CHARS = 120
#: Estimated budget for providers without a known window (cloud), in tokens.
CLOUD_CAP_TOKENS = 100_000
#: Per-message chat-template overhead (role markers, separators), in tokens. WS1
#: measured about 5 on Qwen2.5 (docs/RUNTIME-NOTES.md, /v3/tokenize).
MESSAGE_OVERHEAD_TOKENS = 5
#: The tool-calling preamble the chat template adds when tools are offered (WS1: two
#: small schemas cost ~190 prompt tokens, more than their JSON alone).
TOOLS_TEMPLATE_OVERHEAD_TOKENS = 100
TRUNCATION_MARK = " …[truncated]"

HINT_SHORTEN = "Shorten the message or switch to a cloud model."
HINT_TOO_LONG = (
    "The tool results and replies of this turn do not fit. Start a new chat, ask for "
    "less, or switch to a cloud model."
)


@dataclass
class PromptBudget:
    """How much prompt a provider/model accepts. ``max_prompt_tokens=None`` = cloud."""

    max_prompt_tokens: int | None = None
    chars_per_token: float = 3.0
    tool_result_chars: int = 1500

    @property
    def limit_tokens(self) -> int:
        return self.max_prompt_tokens if self.max_prompt_tokens is not None else CLOUD_CAP_TOKENS

    @property
    def is_local(self) -> bool:
        return self.max_prompt_tokens is not None

    def tokens(self, chars: int) -> int:
        return math.ceil(max(0, chars) / max(0.5, self.chars_per_token))

    def scaled(self, factor: float) -> PromptBudget:
        """A tighter copy (used for the one retry after a server-side overflow)."""
        if self.max_prompt_tokens is None:
            return PromptBudget(None, self.chars_per_token, self.tool_result_chars)
        return PromptBudget(
            max(64, int(self.max_prompt_tokens * factor)),
            self.chars_per_token,
            self.tool_result_chars,
        )


# --------------------------------------------------------------------------- #
# Estimation helpers
# --------------------------------------------------------------------------- #


def public(msg: dict) -> dict:
    """The message without private ``_`` keys (what a provider may see)."""
    return {k: v for k, v in msg.items() if not str(k).startswith("_")}


def message_chars(msg: dict) -> int:
    return len(json.dumps(public(msg), ensure_ascii=False, default=str))


def message_tokens(msg: dict, budget: PromptBudget) -> int:
    return budget.tokens(message_chars(msg)) + MESSAGE_OVERHEAD_TOKENS


def tools_tokens(tools: list[dict] | None, budget: PromptBudget) -> int:
    if not tools:
        return 0
    return budget.tokens(len(json.dumps(tools, ensure_ascii=False))) + (
        TOOLS_TEMPLATE_OVERHEAD_TOKENS
    )


def estimate_tokens(messages: list[dict], tools: list[dict] | None, budget: PromptBudget) -> int:
    return sum(message_tokens(m, budget) for m in messages) + tools_tokens(tools, budget)


def truncate_text(text: str, limit: int) -> str:
    if limit < 0 or len(text) <= limit:
        return text
    keep = max(0, limit - len(TRUNCATION_MARK))
    return text[:keep].rstrip() + TRUNCATION_MARK


def _truncate_tool(msg: dict, limit: int) -> dict:
    content = msg.get("content")
    if not isinstance(content, str) or len(content) <= limit:
        return msg
    out = dict(msg)
    out["content"] = truncate_text(content, limit)
    return out


# --------------------------------------------------------------------------- #
# Turn groups
# --------------------------------------------------------------------------- #


def last_user_index(history: list[dict]) -> int:
    for i in range(len(history) - 1, -1, -1):
        if history[i].get("role") == "user":
            return i
    return -1


def split_groups(history: list[dict]) -> list[list[dict]]:
    """Split into turn groups, each starting at a user message. Messages before the
    first user message form a leading group of their own."""
    groups: list[list[dict]] = []
    for msg in history:
        if msg.get("role") == "user" or not groups:
            groups.append([msg])
        else:
            groups[-1].append(msg)
    return groups


def drop_oldest_groups(history: list[dict], count: int) -> list[dict]:
    """Drop up to ``count`` of the oldest groups, never the current (latest-user) one."""
    if count <= 0:
        return list(history)
    groups = split_groups(history)
    current = groups[-1:] if last_user_index(history) >= 0 else groups
    old = groups[:-1] if last_user_index(history) >= 0 else []
    kept = old[min(count, len(old)) :]
    return [m for g in (*kept, *current) for m in g]


def halve_history(history: list[dict]) -> list[dict]:
    """Drop the older half of the old turn groups (the overflow retry)."""
    old = len(split_groups(history)) - (1 if last_user_index(history) >= 0 else 0)
    return drop_oldest_groups(history, max(1, math.ceil(old / 2))) if old > 0 else list(history)


# --------------------------------------------------------------------------- #
# fit_messages
# --------------------------------------------------------------------------- #


def fit_messages(
    system: dict, history: list[dict], tools: list[dict], budget: PromptBudget
) -> list[dict]:
    """``[system, *history']`` trimmed to ``budget`` (see the module docstring).

    Never mutates its inputs. Raises ``LLMError(code="context_overflow")``.
    """
    limit = budget.limit_tokens
    fixed = message_tokens(system, budget) + tools_tokens(tools, budget)
    msgs = [dict(m) for m in history]
    cut = last_user_index(msgs)

    def over(items: list[dict]) -> bool:
        return fixed + sum(message_tokens(m, budget) for m in items) > limit

    # Step 1: old tool results -> 300 chars (always on a local window; cloud only if over).
    if cut > 0 and (budget.is_local or over(msgs)):
        msgs = [
            _truncate_tool(m, OLD_TOOL_RESULT_CHARS) if i < cut and m.get("role") == "tool" else m
            for i, m in enumerate(msgs)
        ]

    # Step 2: drop the oldest whole turn groups.
    if cut >= 0:
        groups = split_groups(msgs)
        old, current = groups[:-1], groups[-1]
    else:
        old, current = [], msgs
    while old and over([m for g in old for m in g] + current):
        old.pop(0)
    msgs = [m for g in old for m in g] + current

    # Step 3: the current turn's tool results share what is left.
    start = len(msgs) - len(current)
    if over(msgs):
        tool_idx = [i for i in range(start, len(msgs)) if msgs[i].get("role") == "tool"]
        if tool_idx:
            emptied = [{**m, "content": ""} if i in tool_idx else m for i, m in enumerate(msgs)]
            used = fixed + sum(message_tokens(m, budget) for m in emptied)
            spare_chars = int(max(0, limit - used) * budget.chars_per_token)
            share = max(MIN_TOOL_RESULT_CHARS, spare_chars // len(tool_idx))
            originals = {i: msgs[i] for i in tool_idx}
            while True:
                for i in tool_idx:
                    msgs[i] = _truncate_tool(originals[i], share)
                # JSON escaping can make a cut result a little longer than ``share``.
                if not over(msgs) or share <= MIN_TOOL_RESULT_CHARS:
                    break
                share = max(MIN_TOOL_RESULT_CHARS, int(share * 0.85))

    # Step 4: give up with a useful message.
    if over(msgs):
        latest = msgs[start] if cut >= 0 and start < len(msgs) else None
        core = [latest] if latest is not None and latest.get("role") == "user" else []
        if fixed + sum(message_tokens(m, budget) for m in core) > limit:
            raise LLMError(
                "The message is too long for this model",
                code="context_overflow",
                hint=HINT_SHORTEN,
                details={"limit_tokens": limit},
            )
        raise LLMError(
            "The conversation is too long for this model",
            code="context_overflow",
            hint=HINT_TOO_LONG,
            details={"limit_tokens": limit},
        )
    return [dict(system), *msgs]


__all__ = [
    "CLOUD_CAP_TOKENS",
    "OLD_TOOL_RESULT_CHARS",
    "PromptBudget",
    "drop_oldest_groups",
    "estimate_tokens",
    "fit_messages",
    "halve_history",
    "last_user_index",
    "split_groups",
    "truncate_text",
]
