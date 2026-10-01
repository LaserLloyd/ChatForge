"""Tool registry: OpenAI tool schemas, allowlist check and safe dispatch.

``ToolRegistry.call`` never raises for bad arguments, unknown tools or tool failures; those
come back as ``ToolResult(ok=False)`` so the model can see and correct them. Only
``ToolNotAllowed`` (a known tool that is not enabled) propagates.
"""

from __future__ import annotations

import asyncio
import json
import re
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from typing import Any

from chatforge.errors import AppError

_FENCE = re.compile(r"^\s*```[A-Za-z0-9_-]*\s*\n?(.*?)\n?\s*```\s*$", re.DOTALL)


@dataclass
class ToolResult:
    ok: bool
    content: str  # text fed back to the model
    summary: str = ""  # short label for the UI
    #: A file the tool saved for the user: ``{name, path, size, kind}`` (``create_document``).
    document: dict[str, Any] | None = None


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


@dataclass(frozen=True)
class ToolSpec:
    """One tool: its OpenAI schema, a UI label, and whether the small local model gets it.

    The local NPU model has a 4096-token prompt window and gets confused by long tool
    lists, so only the ``local`` tools are offered to it; cloud and LAN models get all.
    """

    name: str
    label: str
    schema: dict
    local: bool


def _spec(
    name: str,
    label: str,
    description: str,
    properties: dict[str, Any],
    required: list[str],
    *,
    local: bool,
) -> ToolSpec:
    return ToolSpec(name, label, _schema(name, description, properties, required), local)


def _str(description: str) -> dict[str, str]:
    return {"type": "string", "description": description}


# Canonical order; descriptions are deliberately terse (4096-token NPU prompt window).
_SPECS: dict[str, ToolSpec] = {
    s.name: s
    for s in (
        _spec(
            "web_search",
            "Web search",
            "Search the web for current facts, events and prices.",
            {"query": _str("search query")},
            ["query"],
            local=True,
        ),
        _spec(
            "news_search",
            "News search",
            "Search recent news articles.",
            {"query": _str("news topic")},
            ["query"],
            local=False,
        ),
        _spec(
            "fetch_url",
            "Fetch a web page",
            "Fetch a web page and return its text.",
            {"url": _str("http(s) URL")},
            ["url"],
            local=True,
        ),
        _spec(
            "weather",
            "Weather",
            "Current weather and forecast for a place.",
            {
                "location": _str("city, optionally ', country'; empty = user's home"),
                "days": {"type": "integer", "description": "forecast days 1-7"},
            },
            [],
            local=True,
        ),
        _spec(
            "wikipedia",
            "Wikipedia",
            "Summary of a Wikipedia article.",
            {"topic": _str("article topic")},
            ["topic"],
            local=False,
        ),
        _spec(
            "exchange_rate",
            "Exchange rates",
            "Convert money between currencies (ECB rates).",
            {
                "from": _str("3-letter code, e.g. USD"),
                "to": _str("3-letter code, e.g. JPY"),
                "amount": {"type": "number"},
            },
            ["from", "to"],
            local=False,
        ),
        _spec(
            "current_datetime",
            "Current date and time",
            "Get the current local date, time and timezone.",
            {},
            [],
            local=True,
        ),
        _spec(
            "calculator",
            "Calculator",
            "Evaluate a math expression.",
            {"expression": _str("e.g. sqrt(2)*10")},
            ["expression"],
            local=True,
        ),
        _spec(
            "create_document",
            "Create documents",
            "Save a file for the user, e.g. report.docx or data.csv.",
            {
                "filename": _str("file name with extension: .md .txt .csv .docx .json ..."),
                "content": _str("the full text; Markdown for .docx"),
                "format": _str("optional file type if the name has none, e.g. docx"),
            },
            ["filename", "content"],
            local=False,
        ),
    )
}
_SCHEMAS: dict[str, dict] = {name: spec.schema for name, spec in _SPECS.items()}
TOOL_NAMES: tuple[str, ...] = tuple(_SPECS)
LOCAL_TOOL_NAMES: tuple[str, ...] = tuple(n for n, s in _SPECS.items() if s.local)
#: Enabled for a new config (every tool).
DEFAULT_ENABLED: tuple[str, ...] = TOOL_NAMES


def tool_catalog() -> list[dict[str, Any]]:
    """``[{name, label, local}]`` in canonical order, for Settings."""
    return [{"name": s.name, "label": s.label, "local": s.local} for s in _SPECS.values()]


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
    ``fetch_timeout_s``, ``block_private_addresses``, ``location``, ``units`` and
    ``documents_dir``); missing values use the defaults.
    """

    def __init__(self, cfg: Any = None, *, transport: Any = None) -> None:
        from chatforge.tools.web_search import WebSearch

        self._fetch_max_bytes = int(_opt(cfg, "fetch_max_bytes", 1_000_000))
        self._fetch_timeout_s = float(_opt(cfg, "fetch_timeout_s", 10))
        self._block_private = bool(_opt(cfg, "block_private_addresses", True))
        self._location = str(_opt(cfg, "location", "") or "").strip()
        self._units = str(_opt(cfg, "units", "metric") or "metric")
        #: ``tools.documents_dir`` ("" = Documents\ChatForge), for ``create_document``.
        self._documents_dir = str(_opt(cfg, "documents_dir", "") or "")
        self._transport = transport
        self._search = WebSearch(
            max_results=int(_opt(cfg, "web_search_max_results", 5)),
            min_interval_s=float(_opt(cfg, "web_search_min_interval_s", 2.0)),
        )
        self._handlers = {
            "web_search": self._web_search,
            "news_search": self._news_search,
            "fetch_url": self._fetch_url,
            "weather": self._weather,
            "wikipedia": self._wikipedia,
            "exchange_rate": self._exchange_rate,
            "current_datetime": self._current_datetime,
            "calculator": self._calculator,
            "create_document": self._create_document,
        }

    @property
    def location(self) -> str:
        """The home location from Settings ("" when unset)."""
        return self._location

    def schemas(self, enabled: Iterable[str], *, local: bool = False) -> list[dict]:
        """OpenAI ``tools`` array for the enabled tools (canonical order, unknown names
        ignored). ``local`` keeps only the tools offered to the small local model."""
        wanted = set(enabled)
        names = LOCAL_TOOL_NAMES if local else TOOL_NAMES
        return [_SCHEMAS[name] for name in names if name in wanted]

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
        return await self._handlers[name](args, max_chars)

    async def _current_datetime(self, args: dict[str, Any], max_chars: int) -> ToolResult:
        from chatforge.tools import clock

        return await clock.run()

    async def _calculator(self, args: dict[str, Any], max_chars: int) -> ToolResult:
        from chatforge.tools import calculator

        expr = args.get("expression")
        if not isinstance(expr, str):
            return _missing("calculator", "expression")
        return await calculator.run(expr)

    async def _web_search(self, args: dict[str, Any], max_chars: int) -> ToolResult:
        query = args.get("query")
        if not isinstance(query, str) or not query.strip():
            return _missing("web_search", "query")
        return await self._search.search(query)

    async def _news_search(self, args: dict[str, Any], max_chars: int) -> ToolResult:
        query = args.get("query")
        if not isinstance(query, str) or not query.strip():
            return _missing("news_search", "query")
        return await self._search.search(query, kind="news")

    async def _fetch_url(self, args: dict[str, Any], max_chars: int) -> ToolResult:
        from chatforge.tools import fetch_url

        url = args.get("url")
        if not isinstance(url, str) or not url.strip():
            return _missing("fetch_url", "url")
        return await fetch_url.fetch(
            url,
            max_chars=max_chars if max_chars > 0 else 1500,
            max_bytes=self._fetch_max_bytes,
            timeout_s=self._fetch_timeout_s,
            block_private=self._block_private,
            transport=self._transport,
        )

    async def _weather(self, args: dict[str, Any], max_chars: int) -> ToolResult:
        from chatforge.tools import weather

        location = args.get("location")
        if location is not None and not isinstance(location, str):
            return _missing("weather", "location")
        days = args.get("days")
        if isinstance(days, str) and days.strip().isdigit():
            days = int(days)
        return await weather.run(
            location,
            days=days if isinstance(days, int) and not isinstance(days, bool) else None,
            units=self._units,
            default_location=self._location,
            transport=self._transport,
        )

    async def _wikipedia(self, args: dict[str, Any], max_chars: int) -> ToolResult:
        from chatforge.tools import wikipedia

        topic = args.get("topic") or args.get("query")
        if not isinstance(topic, str) or not topic.strip():
            return _missing("wikipedia", "topic")
        return await wikipedia.run(topic, transport=self._transport)

    async def _exchange_rate(self, args: dict[str, Any], max_chars: int) -> ToolResult:
        from chatforge.tools import currency

        return await currency.run(
            args.get("from"), args.get("to"), args.get("amount", 1), transport=self._transport
        )

    async def _create_document(self, args: dict[str, Any], max_chars: int) -> ToolResult:
        from chatforge.tools import documents

        filename = args.get("filename") or args.get("name")
        content = args.get("content")
        if not isinstance(filename, str) or not filename.strip():
            return _missing("create_document", "filename")
        if not isinstance(content, str):
            return _missing("create_document", "content")
        fmt = args.get("format")
        return await asyncio.to_thread(
            documents.create_document,
            filename,
            content,
            fmt if isinstance(fmt, str) else None,
            folder=self._documents_dir,
        )


def _missing(tool: str, field: str) -> ToolResult:
    return ToolResult(
        False,
        f"Invalid arguments for {tool}: '{field}' (string) is required.",
        f"bad arguments for {tool}",
    )
