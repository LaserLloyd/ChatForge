"""Stream events and the assembled assistant message (PLAN §1.4 ``llm/events.py``)."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass
class ToolCall:
    id: str
    name: str
    arguments: str  # JSON text exactly as the model produced it

    def to_openai(self) -> dict[str, Any]:
        """The OpenAI ``tool_calls[]`` item shape."""
        return {
            "id": self.id,
            "type": "function",
            "function": {"name": self.name, "arguments": self.arguments},
        }


@dataclass
class AssistantMessage:
    content: str  # visible text (think stripped, provider tool XML stripped)
    reasoning: str  # merged reasoning text (display only)
    tool_calls: list[ToolCall] = field(default_factory=list)
    # Provider-verbatim fields to echo back next turn: raw_content, reasoning_details,
    # reasoning_content, raw_tool_calls. Only the quirks that produced them read them.
    extras: dict[str, Any] = field(default_factory=dict)


@dataclass
class ContentDelta:
    text: str


@dataclass
class ReasoningDelta:
    text: str


@dataclass
class Completed:
    message: AssistantMessage
    finish_reason: str | None
    usage: dict[str, Any]
    served_model: str | None
    elapsed_s: float
    tok_per_s: float | None


StreamEvent = ContentDelta | ReasoningDelta | Completed
