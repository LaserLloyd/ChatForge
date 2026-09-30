"""System prompt text (PLAN §1.6 step 2: short, includes the date).

Only the date goes in, not the time: the system prompt is the start of every
prompt, and keeping it stable for a whole day lets OVMS reuse its prefix cache.
The ``current_datetime`` tool answers "what time is it".
"""

from __future__ import annotations

from datetime import datetime

DEFAULT_SYSTEM_PROMPT = (
    "You are AI Chat, a concise desktop assistant. Answer briefly. Use tools only when "
    "they help (current facts, web pages, dates, arithmetic)."
)
#: WS1 (docs/RUNTIME-NOTES.md): without this sentence the small NPU model calls tools
#: for questions it can answer directly. It is kept even in a custom system prompt.
TOOLS_SENTENCE = "Use tools only when they help."


def system(now: datetime | None = None, base: str | None = None, *, tools: bool = False) -> str:
    """The system prompt: ``base`` (``chat.system_prompt``) plus today's date. With
    ``tools`` offered, the "use tools only when they help" guidance is guaranteed."""
    now = now or datetime.now().astimezone()
    text = (base or "").strip() or DEFAULT_SYSTEM_PROMPT
    if tools and "use tools only when" not in text.lower():
        text = f"{text} {TOOLS_SENTENCE}"
    return f"{text}\nToday is {now:%A} {now.day} {now:%B %Y}."


def system_message(
    now: datetime | None = None, base: str | None = None, *, tools: bool = False
) -> dict:
    return {"role": "system", "content": system(now, base, tools=tools)}
