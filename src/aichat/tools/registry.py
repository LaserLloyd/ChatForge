"""Tool registry: OpenAI tool schemas, allowlist check and safe dispatch.

``ToolRegistry.call`` never raises for bad arguments, unknown tools or tool failures; those
come back as ``ToolResult(ok=False)`` so the model can see and correct them. Only
``ToolNotAllowed`` (a known tool that is not enabled) propagates.
"""

from __future__ import annotations

import json
import re
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from typing import Any

from aichat.errors import AppError

_FENCE = re.compile(r"^\s*```[A-Za-z0-9_-]*\s*\n?(.*?)\n?\s*```\s*$", re.DOTALL)


@dataclass
class ToolResult:
    ok: bool
    content: str  # text fed back to the model
    summary: str = ""  # short label for the UI


class ToolNotAllowed(AppError):
    """The model asked for a tool that is not enabled."""

    code = "tool_not_allowed"
    hint = "Enable the tool in Settings."
    action = "open_settings"


def _schema(name: str, description: str, properties: dict[str, Any], required: list[str]) -> dict:
    return {
        "type": "function",
        "function": {
            "name": name,
            "description": description,
            "parameters": {"type": "object", "properties": properties, "required": required},
        },
    }


# Canonical order; descriptions are deliberately terse (4096-token NPU prompt window).
_SCHEMAS: dict[str, dict] = {
    "web_search": _schema(
        "web_search",
        "Search the web for current information.",
        {"query": {"type": "string", "description": "search query"}},
        ["query"],
    ),
    "fetch_url": _schema(
        "fetch_url",
        "Fetch a web page and return its text.",
        {"url": {"type": "string", "description": "http(s) URL"}},
        ["url"],
    ),
    "current_datetime": _schema(
        "current_datetime", "Get the current local date, time and timezone.", {}, []
    ),
    "calculator": _schema(
        "calculator",
        "Evaluate a math expression.",
        {"expression": {"type": "string", "description": "e.g. sqrt(2)*10"}},
        ["expression"],
    ),
}
TOOL_NAMES: tuple[str, ...] = tuple(_SCHEMAS)


def assert_tool_allowed(name: str, enabled: Iterable[str]) -> None:
    """Raise ``ToolNotAllowed`` unless ``name`` is in ``enabled``."""
    if name not in set(enabled):
        raise ToolNotAllowed(f"Tool '{name}' is not enabled.")


def _strip_trailing_commas(text: str) -> str:
    """Drop commas directly before ``}`` or ``]``, ignoring anything inside JSON strings."""
    out: list[str] = []
    in_str = False
    escaped = False
    i = 0
    while i < len(text):
        ch = text[i]
        if in_str:
            out.append(ch)
            if escaped:
                escaped = False
            elif ch == "\\":
                escaped = True
            elif ch == '"':
                in_str = False
        elif ch == '"':
            in_str = True
            out.append(ch)
        elif ch == ",":
            j = i + 1
            while j < len(text) and text[j].isspace():
                j += 1
            if j < len(text) and text[j] in "}]":
                i += 1
                continue
            out.append(ch)
        else:
            out.append(ch)
        i += 1
    return "".join(out)


def parse_arguments(arguments_json: str | None) -> dict[str, Any]:
    """Parse tool-call arguments with a one-pass repair; raise ``ValueError`` if hopeless."""
    text = (arguments_json or "").strip()
    if not text:
        return {}
    try:
        value = json.loads(text)
    except json.JSONDecodeError:
        repaired = text
        m = _FENCE.match(repaired)
        if m:
            repaired = m.group(1).strip()
        repaired = _strip_trailing_commas(repaired)
        try:
            value = json.loads(repaired)
        except json.JSONDecodeError as exc:
            raise ValueError(f"arguments are not valid JSON ({exc.msg})") from None
    if not isinstance(value, dict):
        raise ValueError("arguments must be a JSON object")
    return value


def _opt(cfg: Any, name: str, default: Any) -> Any:
    if cfg is None:
        return default
    if isinstance(cfg, Mapping):
        return cfg.get(name, default)
    return getattr(cfg, name, default)


def _clip(text: str, max_chars: int) -> str:
    if max_chars > 0 and len(text) > max_chars:
        return text[: max(0, max_chars - 1)].rstrip() + "…"
    return text


class ToolRegistry:
    """Owns the tool implementations (and the search cache/spacing state).

    ``cfg`` is the ``[tools]`` config section (any object or mapping exposing
    ``web_search_max_results``, ``web_search_min_interval_s``, ``fetch_max_bytes``,
    ``fetch_timeout_s`` and ``block_private_addresses``); missing values use the defaults.
    """

    def __init__(self, cfg: Any = None, *, transport: Any = None) -> None:
        from aichat.tools.web_search import WebSearch

        self._fetch_max_bytes = int(_opt(cfg, "fetch_max_bytes", 1_000_000))
        self._fetch_timeout_s = float(_opt(cfg, "fetch_timeout_s", 10))
        self._block_private = bool(_opt(cfg, "block_private_addresses", True))
        self._transport = transport
        self._search = WebSearch(
            max_results=int(_opt(cfg, "web_search_max_results", 5)),
            min_interval_s=float(_opt(cfg, "web_search_min_interval_s", 2.0)),
        )

    def schemas(self, enabled: Iterable[str]) -> list[dict]:
        """OpenAI ``tools`` array for the enabled tools (canonical order, unknown names ignored)."""
        wanted = set(enabled)
        return [_SCHEMAS[name] for name in TOOL_NAMES if name in wanted]

    async def call(
        self, name: str, arguments_json: str, *, enabled: Iterable[str], max_chars: int
    ) -> ToolResult:
        if name not in _SCHEMAS:
            return ToolResult(
                False,
                f"Unknown tool '{name}'. Available tools: {', '.join(TOOL_NAMES)}.",
                f"unknown tool {name}",
            )
        assert_tool_allowed(name, enabled)  # the only exception that may escape
        try:
            args = parse_arguments(arguments_json)
        except ValueError as exc:
            return ToolResult(
                False,
                f"Invalid arguments for {name}: {exc}. Send a JSON object.",
                f"bad arguments for {name}",
            )
        try:
            result = await self._dispatch(name, args, max_chars)
        except Exception as exc:  # a tool bug must not kill the chat turn
            return ToolResult(False, f"Tool {name} failed: {type(exc).__name__}.", f"{name} failed")
        result.content = _clip(result.content, max_chars)
        return result

    async def _dispatch(self, name: str, args: dict[str, Any], max_chars: int) -> ToolResult:
        if name == "current_datetime":
            from aichat.tools import clock

            return await clock.run()
        if name == "calculator":
            from aichat.tools import calculator

            expr = args.get("expression")
            if not isinstance(expr, str):
                return _missing(name, "expression")
            return await calculator.run(expr)
        if name == "web_search":
            query = args.get("query")
            if not isinstance(query, str) or not query.strip():
                return _missing(name, "query")
            return await self._search.search(query)
        # fetch_url
        from aichat.tools import fetch_url

        url = args.get("url")
        if not isinstance(url, str) or not url.strip():
            return _missing(name, "url")
        return await fetch_url.fetch(
            url,
            max_chars=max_chars if max_chars > 0 else 1500,
            max_bytes=self._fetch_max_bytes,
            timeout_s=self._fetch_timeout_s,
            block_private=self._block_private,
            transport=self._transport,
        )


def _missing(tool: str, field: str) -> ToolResult:
    return ToolResult(
        False,
        f"Invalid arguments for {tool}: '{field}' (string) is required.",
        f"bad arguments for {tool}",
    )
