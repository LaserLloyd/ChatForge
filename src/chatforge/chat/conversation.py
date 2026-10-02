"""The single current conversation: an OpenAI-shaped message list plus UI metadata.

Messages are stored exactly as they are echoed to providers (``Quirks.history_message``
output, user turns, ``role: tool`` results). UI metadata rides along in private
``_``-prefixed keys, which every ``Quirks.prepare_body`` strips before sending:

* all:        ``_ts`` (epoch seconds)
* user:       ``_attachments`` (attached files: ``[{name, kind, chars, truncated, text}]``;
  ``content`` stays the typed text and ``chat.history`` adds the file blocks per request;
  a picture, ``kind: "image"``, also has ``{file, media_type, width, height, thumb}`` and,
  once read for a model that cannot see it, ``ocr``: see ``chatforge.images``),
  ``_cut`` (the message was too long for the model's context window when it was sent, so
  its end or its files were cut), and for a quick action (``chat.actions``) ``_action``
  (``{id, label, tools, style}``) and ``_text`` (what the user typed; ``content`` is then
  the expanded prompt the model sees)
* assistant:  ``_reasoning`` (display reasoning), ``_model``, ``_stopped`` (cancelled
  partial), and the provider's own ``_origin`` / ``_visible`` (MiniMax echo: these MUST
  survive a save/load round trip or the echo breaks after a restart)
* tool:       ``_name``, ``_ok``, ``_summary``, ``_hidden`` (a synthetic "not run"
  result that the UI does not show as a chip), ``_document`` (a file the tool saved:
  ``{name, path, size, kind}``)

``Conversation.context_start_ts`` is where the model's view of the conversation started
in the last turn: the ``_ts`` of the first message its prompt still held after older turns
were left out to fit its context window (``None``: it saw everything). ``items()`` puts a
notice before that message.

``conversation.json`` is ``{"schema": 1, "saved_at": ts, "context_start_ts": ts | null,
"messages": [...]}``, written atomically (tmp + ``os.replace``).
"""

from __future__ import annotations

import contextlib
import json
import os
import time
from pathlib import Path
from typing import Any

from chatforge.logging_setup import get_logger

log = get_logger(__name__)

SCHEMA = 1
#: Older whole turn groups are dropped past this many stored messages.
MAX_STORED_MESSAGES = 400
TOOL_ARGUMENTS_UI_CHARS = 300
#: The notice item ``items()`` puts before the first message the model still sees.
CONTEXT_CUT_NOTICE = "Older messages are past the context window (too long for context)."


def visible_content(msg: dict) -> str:
    """What the user sees for an assistant message (MiniMax keeps raw content)."""
    if "_visible" in msg and isinstance(msg["_visible"], str):
        return msg["_visible"]
    content = msg.get("content")
    return content if isinstance(content, str) else ""


def attachment_views(msg: dict) -> list[dict]:
    """``[{name, kind, chars, truncated}]`` for a user message's attached files (no text),
    plus ``{width, height, thumb}`` for a picture."""
    files = msg.get("_attachments")
    if not isinstance(files, list):
        return []
    out: list[dict] = []
    for f in files:
        if not isinstance(f, dict):
            continue
        view: dict[str, Any] = {
            "name": str(f.get("name") or "file"),
            "kind": str(f.get("kind") or "text"),
            "chars": int(f.get("chars") or 0),
            "truncated": bool(f.get("truncated", False)),
        }
        if view["kind"] == "image":
            thumb = f.get("thumb")
            view.update(
                width=f.get("width") if isinstance(f.get("width"), int) else None,
                height=f.get("height") if isinstance(f.get("height"), int) else None,
                thumb=thumb if isinstance(thumb, str) and thumb.startswith("data:image/") else None,
            )
        out.append(view)
    return out


def document_view(msg: dict) -> dict | None:
    """``{name, path, size, kind}`` for a tool message that saved a document, else ``None``."""
    doc = msg.get("_document")
    if not isinstance(doc, dict) or not doc.get("path"):
        return None
    return {
        "name": str(doc.get("name") or ""),
        "path": str(doc.get("path")),
        "size": int(doc.get("size") or 0),
        "kind": doc.get("kind"),
    }


def action_view(msg: dict, item: dict) -> None:
    """A quick action's user message (``_action``): the popup shows the typed text
    (``_text``, not the expanded prompt) and ``action: {id, label}``."""
    action = msg.get("_action")
    if not isinstance(action, dict):
        return
    typed = msg.get("_text")
    item["content"] = typed if isinstance(typed, str) else ""
    item["action"] = {"id": str(action.get("id") or ""), "label": str(action.get("label") or "")}


def _timestamp(value: Any) -> float | None:
    """``value`` if it is a number (a ``_ts``), else ``None``."""
    if isinstance(value, bool) or not isinstance(value, int | float):
        return None
    return float(value)


class Conversation:
    """An ordered message list with snapshot, rollback and repair helpers."""

    def __init__(
        self, messages: list[dict] | None = None, *, context_start_ts: float | None = None
    ) -> None:
        self.messages: list[dict] = [dict(m) for m in (messages or []) if isinstance(m, dict)]
        #: Where the model's view started in the last turn (see the module docstring).
        self.context_start_ts: float | None = _timestamp(context_start_ts)

    def __len__(self) -> int:
        return len(self.messages)

    # -- mutation ---------------------------------------------------------- #

    def append(self, msg: dict) -> None:
        self.messages.append(msg)

    def rollback(self, mark: int) -> None:
        """Drop everything appended after ``len() == mark``."""
        del self.messages[mark:]

    def clear(self) -> None:
        self.messages.clear()
        self.context_start_ts = None

    def mark_latest_cut(self) -> None:
        """Mark the latest user message as cut to fit the context window (``_cut``)."""
        for msg in reversed(self.messages):
            if msg.get("role") == "user":
                msg["_cut"] = True
                return

    def repair_tool_calls(
        self, *, note: str = "Not run (stopped).", ts: float | None = None
    ) -> int:
        """Give every assistant ``tool_calls`` entry a ``role: tool`` answer, so the
        history stays valid for the next request. Returns how many were added."""
        added = 0
        i = 0
        while i < len(self.messages):
            msg = self.messages[i]
            calls = msg.get("tool_calls") if msg.get("role") == "assistant" else None
            if not calls:
                i += 1
                continue
            j = i + 1
            answered: set[str] = set()
            while j < len(self.messages) and self.messages[j].get("role") == "tool":
                answered.add(str(self.messages[j].get("tool_call_id")))
                j += 1
            missing = [c for c in calls if str(c.get("id")) not in answered]
            for k, call in enumerate(missing):
                self.messages.insert(
                    j + k,
                    {
                        "role": "tool",
                        "tool_call_id": call.get("id"),
                        "content": note,
                        "_name": (call.get("function") or {}).get("name", ""),
                        "_ok": False,
                        "_summary": "stopped",
                        "_ts": ts if ts is not None else time.time(),
                    },
                )
            added += len(missing)
            i = j + len(missing)
        return added

    def trim(self, max_messages: int = MAX_STORED_MESSAGES) -> None:
        """Drop the oldest whole turn groups until at most ``max_messages`` remain."""
        if len(self.messages) <= max_messages:
            return
        starts = [i for i, m in enumerate(self.messages) if m.get("role") == "user"]
        for start in starts:
            if len(self.messages) - start <= max_messages:
                del self.messages[:start]
                return
        if starts:  # one enormous turn: keep it whole
            del self.messages[: starts[-1]]

    # -- views ------------------------------------------------------------- #

    def llm_history(self) -> list[dict]:
        """Copies of the stored messages (private keys included; quirks strip them)."""
        return [dict(m) for m in self.messages]

    def items(self) -> list[dict]:
        """The conversation as the popup renders it::

            {role: "user", content, ts, attachments?: [{name, kind, chars, truncated,
             width?, height?, thumb?}], cut?: true, action?: {id, label}}
            {role: "assistant", content, ts, model?, reasoning?, stopped?,
             tools?: [{call_id, name, arguments, ok, summary}],
             documents?: [{name, path, size, kind}]}
            {role: "notice", kind: "context_cut", content}

        All assistant/tool messages after one user message fold into one assistant item
        (round contents joined with a blank line, reasoning likewise). ``attachments`` and
        ``documents`` are present only when there are some; ``cut`` only on a message that
        was cut to fit the context window. The ``context_cut`` notice comes before the
        first message the model still saw in the last turn (the first user message sent at
        or after ``context_start_ts``), when older messages were left out.
        """
        out: list[dict] = []
        cur: dict[str, Any] | None = None
        chips: dict[str, dict] = {}

        def flush() -> None:
            nonlocal cur
            if cur is None:
                return
            item: dict[str, Any] = {
                "role": "assistant",
                "content": "\n\n".join(p for p in cur["parts"] if p),
                "ts": cur["ts"],
            }
            if cur["model"]:
                item["model"] = cur["model"]
            reasoning = "\n\n".join(r for r in cur["reasoning"] if r)
            if reasoning:
                item["reasoning"] = reasoning
            if cur["tools"]:
                item["tools"] = cur["tools"]
            if cur["stopped"]:
                item["stopped"] = True
            if cur["documents"]:
                item["documents"] = cur["documents"]
            out.append(item)
            cur = None

        for msg in self.messages:
            role = msg.get("role")
            if role == "user":
                flush()
                content = msg.get("content")
                user: dict[str, Any] = {
                    "role": "user",
                    "content": content if isinstance(content, str) else "",
                    "ts": msg.get("_ts"),
                }
                attachments = attachment_views(msg)
                if attachments:
                    user["attachments"] = attachments
                if msg.get("_cut"):
                    user["cut"] = True
                action_view(msg, user)
                out.append(user)
                continue
            if role not in ("assistant", "tool"):
                continue
            if cur is None:
                cur = {
                    "parts": [],
                    "reasoning": [],
                    "tools": [],
                    "documents": [],
                    "ts": msg.get("_ts"),
                    "model": None,
                    "stopped": False,
                }
            if msg.get("_ts") is not None:
                cur["ts"] = msg["_ts"]
            if role == "assistant":
                cur["parts"].append(visible_content(msg).strip())
                if msg.get("_reasoning"):
                    cur["reasoning"].append(str(msg["_reasoning"]))
                if msg.get("_model"):
                    cur["model"] = msg["_model"]
                if msg.get("_stopped"):
                    cur["stopped"] = True
                for call in msg.get("tool_calls") or []:
                    fn = call.get("function") or {}
                    chip = {
                        "call_id": call.get("id"),
                        "name": fn.get("name", ""),
                        "arguments": str(fn.get("arguments", ""))[:TOOL_ARGUMENTS_UI_CHARS],
                        "ok": None,
                        "summary": "",
                    }
                    chips[str(call.get("id"))] = chip
                    cur["tools"].append(chip)
            else:  # tool
                document = document_view(msg)
                if document is not None:
                    cur["documents"].append(document)
                chip = chips.get(str(msg.get("tool_call_id")))
                if chip is None:
                    continue
                if msg.get("_hidden"):
                    cur["tools"] = [c for c in cur["tools"] if c is not chip]
                    continue
                chip["ok"] = bool(msg.get("_ok", True))
                chip["summary"] = str(msg.get("_summary", ""))
        flush()
        self._insert_context_notice(out)
        return out

    def _insert_context_notice(self, items: list[dict]) -> None:
        start = self.context_start_ts
        if start is None:
            return
        for k, item in enumerate(items):
            ts = _timestamp(item.get("ts")) if item["role"] == "user" else None
            if ts is None or ts < start:
                continue
            # At 0 nothing comes before it: nothing the UI shows was left out.
            if k:
                notice = {"role": "notice", "kind": "context_cut", "content": CONTEXT_CUT_NOTICE}
                items.insert(k, notice)
            return

    # -- persistence ------------------------------------------------------- #

    def to_json(self) -> dict:
        return {
            "schema": SCHEMA,
            "saved_at": time.time(),
            "context_start_ts": self.context_start_ts,
            "messages": self.messages,
        }

    @classmethod
    def from_json(cls, data: Any) -> Conversation:
        if not isinstance(data, dict) or not isinstance(data.get("messages"), list):
            raise ValueError("not a conversation file")
        return cls(data["messages"], context_start_ts=_timestamp(data.get("context_start_ts")))


def save(path: Path, conv: Conversation) -> None:
    """Atomically write ``conv`` to ``path`` (every key kept, private ones included)."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f"{path.name}.{os.getpid()}.tmp")
    try:
        with tmp.open("w", encoding="utf-8", newline="\n") as fh:
            json.dump(conv.to_json(), fh, ensure_ascii=False, default=str)
            fh.flush()
            with contextlib.suppress(OSError):
                os.fsync(fh.fileno())
        os.replace(tmp, path)
    finally:
        with contextlib.suppress(OSError):
            tmp.unlink()


def load(path: Path) -> Conversation:
    """Read ``path``; a missing file is an empty conversation. A corrupt file is moved
    aside to ``<name>.bad`` (so the next save does not destroy it) and ignored."""
    path = Path(path)
    if not path.exists():
        return Conversation()
    try:
        return Conversation.from_json(json.loads(path.read_text(encoding="utf-8")))
    except (OSError, ValueError) as exc:
        log.warning("conversation_unreadable", path=str(path), error=str(exc)[:200])
        with contextlib.suppress(OSError):
            os.replace(path, path.with_name(path.name + ".bad"))
        return Conversation()


def delete(path: Path) -> None:
    with contextlib.suppress(FileNotFoundError):
        Path(path).unlink()
