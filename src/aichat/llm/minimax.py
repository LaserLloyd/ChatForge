"""MiniMax quirks (docs/research-brief.md "MiniMax API findings").

- ``reasoning_split: true``; ``max_completion_tokens``; no penalties/logit_bias/n;
  temperature clamped to [0, 2].
- ``base_resp.status_code`` is checked on every payload (HTTP 200 bodies included).
- Reasoning arrives as ``reasoning_content`` or ``reasoning_details`` (handle both).
- The assistant message is echoed back unchanged next turn (interleaved thinking).
- ``<minimax:tool_call>`` XML is stripped from the visible text.

``_MINIMAX_QUICK_RE``, ``_MINIMAX_TOOL_XML_RE``, ``_strip_outside_code`` (with its
code-region helpers) and ``strip_minimax_tool_call_xml`` are
Adapted from DisPatch backend/app/openclaw_text.py (MIT, LaserLloyd).
"""

from __future__ import annotations

import bisect
import re
from typing import Any

from aichat.llm.errors import LLMError
from aichat.llm.events import AssistantMessage, ToolCall
from aichat.llm.quirks import (
    GenericQuirks,
    clean_messages,
    join_reasoning,
    reasoning_text,
    tool_calls_openai,
)
from aichat.llm.thinking import TagRule, split_thinking

REGION_HINT = "International keys use api.minimax.io; China keys use api.minimaxi.com"

_DROP_PARAMS = ("presence_penalty", "frequency_penalty", "logit_bias", "n")

# base_resp.status_code -> (code, message, hint, action, retryable)
_BASE_RESP_MAP: dict[int, tuple[str, str, str | None, str | None, bool | None]] = {
    1004: (
        "region_or_key",
        "MiniMax rejected the key for this region",
        REGION_HINT,
        "open_settings",
        None,
    ),
    2049: (
        "region_or_key",
        "MiniMax rejected the API key",
        REGION_HINT,
        "open_settings",
        None,
    ),
    1002: (
        "rate_limit",
        "MiniMax is rate-limiting this key",
        "Too many requests; wait a moment and try again.",
        "retry",
        True,
    ),
    1008: (
        "balance",
        "The MiniMax account balance is insufficient",
        "Top up the account, or check the plan.",
        None,
        None,
    ),
    1039: (
        "context_overflow",
        "The conversation exceeds MiniMax's token limit",
        "Start a new chat or shorten the message.",
        None,
        None,
    ),
    2013: (
        "bad_request",
        "MiniMax rejected the request parameters",
        None,
        None,
        None,
    ),
    2056: (
        "quota",
        "The MiniMax plan usage limit is reached",
        "Plan window used up; resets within 5 h.",
        "retry",
        None,
    ),
}


def base_resp_error(base_resp: Any) -> LLMError | None:
    """``LLMError`` for a non-zero ``base_resp.status_code``, else ``None``."""
    if not isinstance(base_resp, dict):
        return None
    status = base_resp.get("status_code")
    try:
        status = int(status) if status is not None else 0
    except (TypeError, ValueError):
        return None
    if status == 0:
        return None
    said = str(base_resp.get("status_msg") or "").strip()[:300]
    tail = f" MiniMax said: {said} ({status})" if said else f" (MiniMax code {status})"
    mapped = _BASE_RESP_MAP.get(status)
    details = {"base_resp_code": status}
    if mapped is None:
        return LLMError(
            "MiniMax returned an error",
            code="server",
            hint=tail.strip(),
            details=details,
        )
    code, message, hint, action, retryable = mapped
    return LLMError(
        message,
        code=code,  # type: ignore[arg-type]
        hint=(hint.rstrip(".") + "." + tail) if hint else tail.strip(),
        action=action,  # type: ignore[arg-type]
        retryable=retryable,
        details=details,
    )


def merge_reasoning_details(parts: list[dict]) -> list[dict] | None:
    """Merge streamed ``reasoning_details`` lists by ``index`` (text concatenated,
    other fields first-seen). ``None`` if no part carried the field."""
    slots: dict[Any, dict] = {}
    seen = False
    for p in parts:
        details = p.get("reasoning_details")
        if not isinstance(details, list):
            continue
        seen = True
        for pos, d in enumerate(details):
            if not isinstance(d, dict):
                continue
            idx = d.get("index", pos)
            slot = slots.get(idx)
            if slot is None:
                slots[idx] = dict(d)
                continue
            for k, v in d.items():
                if k == "text":
                    slot["text"] = (slot.get("text") or "") + (v or "")
                elif v is not None and slot.get(k) is None:
                    slot[k] = v
    if not seen:
        return None
    return [slots[k] for k in sorted(slots, key=lambda x: (not isinstance(x, int), str(x)))]


def _merge_reasoning_content(parts: list[dict]) -> str | None:
    pieces = [p["reasoning_content"] for p in parts if isinstance(p.get("reasoning_content"), str)]
    return "".join(pieces) if pieces else None


# --------------------------------------------------------------------------- #
# <minimax:tool_call> stripping
# Adapted from DisPatch backend/app/openclaw_text.py (MIT, LaserLloyd).
# --------------------------------------------------------------------------- #

# Minimax embeds tool calls as XML inside text blocks instead of emitting
# structured tool calls.
_MINIMAX_QUICK_RE = re.compile(r"minimax:tool_call", re.I)
_MINIMAX_TOOL_XML_RE = re.compile(r"<invoke\b[^>]*>[\s\S]*?</invoke>|</?minimax:tool_call>", re.I)

_FENCE_RE = re.compile(r"(^|\n)(```|~~~)[^\n]*\n[\s\S]*?(?:\n\2|$)")
# Possessive quantifiers: no catastrophic backtracking on a wall of backticks.
_INLINE_CODE_RE = re.compile(r"`++[^`]++`++")


def _find_code_regions(text: str) -> list[tuple[int, int]]:
    """Fenced + inline code spans, so the strip skips documentation examples."""
    fences: list[tuple[int, int]] = []
    for m in _FENCE_RE.finditer(text):
        start = m.start() + len(m.group(1))
        fences.append((start, m.start() + len(m.group(0))))
    regions = list(fences)
    i = 0
    for m in _INLINE_CODE_RE.finditer(text):
        while i < len(fences) and fences[i][1] <= m.start():
            i += 1
        if i < len(fences) and m.start() >= fences[i][0] and m.end() <= fences[i][1]:
            continue
        regions.append((m.start(), m.end()))
    regions.sort()
    return regions


def _is_inside_code(pos: int, regions: list[tuple[int, int]]) -> bool:
    if not regions:
        return False
    i = bisect.bisect_right(regions, (pos, float("inf"))) - 1
    return i >= 0 and regions[i][0] <= pos < regions[i][1]


def _strip_outside_code(text: str, pattern: re.Pattern[str]) -> str:
    """Apply ``pattern`` removal only outside fenced/inline code regions."""
    if not text:
        return text
    regions = _find_code_regions(text)
    out: list[str] = []
    last = 0
    for m in pattern.finditer(text):
        if _is_inside_code(m.start(), regions):
            continue
        out.append(text[last : m.start()])
        last = m.end()
    out.append(text[last:])
    return "".join(out)


def strip_minimax_tool_call_xml(text: str) -> str:
    """Remove the Minimax tool invocations that leak into text."""
    if not text or not _MINIMAX_QUICK_RE.search(text):
        return text
    return _strip_outside_code(text, _MINIMAX_TOOL_XML_RE)


# --------------------------------------------------------------------------- #
# Quirks
# --------------------------------------------------------------------------- #


class MiniMaxQuirks(GenericQuirks):
    name = "minimax"
    stream_rules: tuple[TagRule, ...] = (
        TagRule("<minimax:tool_call>", ("</minimax:tool_call>",), "drop"),
    )

    def prepare_body(self, body: dict) -> dict:
        body = dict(body)
        for k in _DROP_PARAMS:
            body.pop(k, None)
        if "max_tokens" in body:
            limit = body.pop("max_tokens")
            if limit is not None:
                body.setdefault("max_completion_tokens", limit)
        temp = body.get("temperature")
        if isinstance(temp, int | float):
            body["temperature"] = min(2.0, max(0.0, float(temp)))
        body["reasoning_split"] = True
        if body.get("stream"):
            opts = dict(body.get("stream_options") or {})
            opts["include_usage"] = True
            body["stream_options"] = opts
        body["messages"] = clean_messages(body.get("messages", []), origin=self.name)
        return body

    def check_payload(self, obj: dict) -> None:
        if isinstance(obj, dict):
            err = base_resp_error(obj.get("base_resp"))
            if err is not None:
                raise err

    def finalize(
        self, raw_content: str, reasoning_parts: list, tool_calls: list[ToolCall]
    ) -> AssistantMessage:
        visible, inline = split_thinking(raw_content)
        visible = strip_minimax_tool_call_xml(visible)
        extras: dict[str, Any] = {"raw_content": raw_content}
        details = merge_reasoning_details(reasoning_parts)
        if details is not None:
            extras["reasoning_details"] = details
        content = _merge_reasoning_content(reasoning_parts)
        if content is not None:
            extras["reasoning_content"] = content
        return AssistantMessage(
            content=visible.strip(),
            reasoning=join_reasoning(reasoning_text(reasoning_parts), inline),
            tool_calls=list(tool_calls),
            extras=extras,
        )

    def history_message(self, msg: AssistantMessage) -> dict:
        """The assistant message exactly as MiniMax sent it: raw content, tool_calls,
        and whichever reasoning field arrived. ``_visible`` is what other providers get."""
        out: dict[str, Any] = {
            "role": "assistant",
            "content": msg.extras.get("raw_content", msg.content),
        }
        if msg.tool_calls:
            out["tool_calls"] = tool_calls_openai(msg.tool_calls)
        for key in ("reasoning_details", "reasoning_content"):
            if key in msg.extras:
                out[key] = msg.extras[key]
        out["_origin"] = self.name
        out["_visible"] = msg.content
        return out
