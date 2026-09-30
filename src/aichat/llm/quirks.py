"""Per-provider request/response quirks (PLAN §1.4 ``llm/quirks.py``).

A ``Quirks`` object is stateless and shared across requests: per-stream state lives
in the client, which hands the raw pieces to :meth:`Quirks.finalize` at the end.

History messages built by :meth:`Quirks.history_message` may carry private ``_``-prefixed
keys (``_origin``: which quirks produced it; ``_visible``: the display text). Every
``prepare_body`` strips them, and provider-verbatim extras (``reasoning_details``,
``reasoning_content``, raw content) only go back to the quirks that produced them.
"""

from __future__ import annotations

import json
import re
from typing import Any, Protocol, runtime_checkable

from aichat.llm.events import AssistantMessage, ToolCall
from aichat.llm.thinking import TagRule, split_thinking

REASONING_KEYS: tuple[str, ...] = ("reasoning_content", "reasoning", "reasoning_details")


@runtime_checkable
class Quirks(Protocol):
    name: str
    stream_rules: tuple[TagRule, ...]  # extra tagged blocks to route/hide while streaming

    def prepare_body(self, body: dict) -> dict: ...

    def check_payload(self, obj: dict) -> None: ...  # raises LLMError

    def reasoning_from_delta(self, delta: dict) -> str | None: ...

    def finalize(
        self, raw_content: str, reasoning_parts: list, tool_calls: list[ToolCall]
    ) -> AssistantMessage: ...

    def history_message(self, msg: AssistantMessage) -> dict: ...


# --------------------------------------------------------------------------- #
# Shared helpers
# --------------------------------------------------------------------------- #


def clean_messages(
    messages: list[dict], *, origin: str | None, minimal: bool = False
) -> list[dict]:
    """Strip private keys; keep provider extras only on assistant messages whose
    ``_origin`` equals ``origin``. ``minimal`` keeps just the OpenAI core fields
    (role/content/tool_calls/tool_call_id/name), as local OVMS templates want."""
    out: list[dict] = []
    for m in messages:
        mine = origin is not None and m.get("_origin") == origin
        c = {k: v for k, v in m.items() if not k.startswith("_")}
        if c.get("role") == "assistant" and not mine:
            for k in REASONING_KEYS:
                c.pop(k, None)
            if "_visible" in m:  # raw provider content is not ours to echo
                c["content"] = m["_visible"]
        if minimal:
            keep = {"role", "content", "tool_calls", "tool_call_id", "name"}
            c = {k: v for k, v in c.items() if k in keep}
            if c.get("role") == "assistant" and c.get("content") is None:
                c["content"] = ""
        out.append(c)
    return out


def delta_reasoning(delta: dict) -> str:
    """Reasoning text carried by one delta: ``reasoning_details[].text`` if present,
    else ``reasoning_content`` / ``reasoning`` (never both, so nothing is doubled)."""
    details = delta.get("reasoning_details")
    if isinstance(details, list):
        texts = [
            d["text"] for d in details if isinstance(d, dict) and isinstance(d.get("text"), str)
        ]
        if texts:
            return "".join(texts)
    for key in ("reasoning_content", "reasoning"):
        v = delta.get(key)
        if isinstance(v, str) and v:
            return v
    return ""


def reasoning_text(parts: list[dict]) -> str:
    """Concatenate reasoning text from raw per-delta reasoning fields."""
    return "".join(delta_reasoning(p) for p in parts)


def join_reasoning(streamed: str, inline: str) -> str:
    streamed, inline = streamed.strip("\n"), inline.strip("\n")
    if streamed and inline:
        return f"{streamed}\n{inline}"
    return streamed or inline


def tool_calls_openai(calls: list[ToolCall]) -> list[dict]:
    return [c.to_openai() for c in calls]


class GenericQuirks:
    """Plain OpenAI-compatible provider: no body changes, visible content echoed."""

    name = "generic"
    stream_rules: tuple[TagRule, ...] = ()

    def prepare_body(self, body: dict) -> dict:
        body = dict(body)
        body["messages"] = clean_messages(body.get("messages", []), origin=None)
        return body

    def check_payload(self, obj: dict) -> None:
        return None

    def reasoning_from_delta(self, delta: dict) -> str | None:
        return delta_reasoning(delta) or None

    def finalize(
        self, raw_content: str, reasoning_parts: list, tool_calls: list[ToolCall]
    ) -> AssistantMessage:
        visible, inline = split_thinking(raw_content)
        return AssistantMessage(
            content=visible.strip(),
            reasoning=join_reasoning(reasoning_text(reasoning_parts), inline),
            tool_calls=list(tool_calls),
            extras={"raw_content": raw_content},
        )

    def history_message(self, msg: AssistantMessage) -> dict:
        out: dict[str, Any] = {"role": "assistant", "content": msg.content}
        if msg.tool_calls:
            out["tool_calls"] = tool_calls_openai(msg.tool_calls)
        return out


# --------------------------------------------------------------------------- #
# OVMS
# --------------------------------------------------------------------------- #

_TOOL_CALL_RE = re.compile(r"<tool_call>\s*(\{.*?\})\s*</tool_call>", re.DOTALL)
_TOOL_CALL_TAIL_RE = re.compile(r"<tool_call>.*\Z", re.DOTALL)  # unclosed trailing block


def parse_tool_call_tags(text: str) -> tuple[str, list[ToolCall]]:
    """Hermes-style ``<tool_call>{json}</tool_call>`` blocks in content →
    ``(visible text, calls)``. Unparseable blocks are dropped from the text."""
    calls: list[ToolCall] = []
    for m in _TOOL_CALL_RE.finditer(text):
        try:
            obj = json.loads(m.group(1))
        except ValueError:
            continue
        if not isinstance(obj, dict) or not isinstance(obj.get("name"), str):
            continue
        args = obj.get("arguments", obj.get("parameters", {}))
        arguments = args if isinstance(args, str) else json.dumps(args, ensure_ascii=False)
        calls.append(ToolCall(id=f"call_{len(calls)}", name=obj["name"], arguments=arguments))
    visible = _TOOL_CALL_TAIL_RE.sub("", _TOOL_CALL_RE.sub("", text))
    return visible, calls


class OvmsQuirks(GenericQuirks):
    """Local OVMS: ``chat_template_kwargs.enable_thinking``, no ``n``, usage in the
    stream, hermes ``<tool_call>`` fallback, and a minimal history."""

    name = "ovms"
    stream_rules: tuple[TagRule, ...] = (TagRule("<tool_call>", ("</tool_call>",), "drop"),)

    def __init__(self, enable_thinking: bool = False) -> None:
        self.enable_thinking = enable_thinking

    def prepare_body(self, body: dict) -> dict:
        body = dict(body)
        body.pop("n", None)
        kwargs = dict(body.get("chat_template_kwargs") or {})
        kwargs["enable_thinking"] = bool(self.enable_thinking)
        body["chat_template_kwargs"] = kwargs
        if body.get("stream"):
            opts = dict(body.get("stream_options") or {})
            opts["include_usage"] = True
            body["stream_options"] = opts
        body["messages"] = clean_messages(body.get("messages", []), origin=None, minimal=True)
        return body

    def finalize(
        self, raw_content: str, reasoning_parts: list, tool_calls: list[ToolCall]
    ) -> AssistantMessage:
        visible, inline = split_thinking(raw_content)
        visible, parsed = parse_tool_call_tags(visible)
        calls = list(tool_calls) or parsed
        return AssistantMessage(
            content=visible.strip(),
            reasoning=join_reasoning(reasoning_text(reasoning_parts), inline),
            tool_calls=calls,
            extras={},
        )

    def history_message(self, msg: AssistantMessage) -> dict:
        out: dict[str, Any] = {"role": "assistant", "content": msg.content or ""}
        if msg.tool_calls:
            out["tool_calls"] = tool_calls_openai(msg.tool_calls)
        return out
