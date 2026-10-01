"""System prompt text (PLAN §1.6 step 2: short, includes the date).

Only the date goes in, not the time: the system prompt is the start of every
prompt, and keeping it stable for a whole day lets OVMS reuse its prefix cache.
The ``current_datetime`` tool answers "what time is it". The home location (Settings →
General) is stable too, so it is safe to include.
"""

from __future__ import annotations

from datetime import datetime

from chatforge.config import DEFAULT_SYSTEM_PROMPT

#: WS1 (docs/RUNTIME-NOTES.md): the small NPU model both over-calls tools for simple
#: questions and refuses ("I have no real-time data") for current ones. This text steers
#: both ways. It is added only when tools are offered (a model without tools must not be
#: told it can reach the internet), and it is kept even in a custom system prompt.
TOOLS_SENTENCE = (
    "You have live internet access through your tools. For anything current or local "
    "(weather, news, prices, recent events) call a tool and answer only from what it "
    "returns, instead of saying you cannot look it up; answer simple questions directly."
)
_TOOLS_MARKER = "for anything current or local"

__all__ = ["DEFAULT_SYSTEM_PROMPT", "TOOLS_SENTENCE", "system", "system_message"]


def system(
    now: datetime | None = None,
    base: str | None = None,
    *,
    tools: bool = False,
    location: str = "",
    instructions: str = "",
) -> str:
    """The system prompt: ``base`` (the personality, ``chat.system_prompt``) plus today's
    date. With ``tools`` offered, the tool guidance and the home location are added. The
    user's standing ``instructions`` (``chat.instructions``) come last, under a heading,
    so the model treats them as the user's own directions."""
    now = now or datetime.now().astimezone()
    text = (base or "").strip() or DEFAULT_SYSTEM_PROMPT
    if tools and _TOOLS_MARKER not in text.lower():
        text = f"{text} {TOOLS_SENTENCE}"
    lines = [text, f"Today is {now:%A} {now.day} {now:%B %Y}."]
    location = " ".join((location or "").split())
    if tools and location:
        lines.append(f"The user's home location is {location}.")
    instructions = (instructions or "").strip()
    if instructions:
        lines.append(f"\nThe user's instructions (follow them in every reply):\n{instructions}")
    return "\n".join(lines)


def system_message(
    now: datetime | None = None,
    base: str | None = None,
    *,
    tools: bool = False,
    location: str = "",
    instructions: str = "",
) -> dict:
    content = system(now, base, tools=tools, location=location, instructions=instructions)
    return {"role": "system", "content": content}
