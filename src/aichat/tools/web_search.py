"""``web_search`` tool: DuckDuckGo text search via ``ddgs`` (blocking, run in a thread).

Spacing between real searches and a small TTL cache keep us under ddgs rate limits.
``_ddgs_factory``, ``_clock`` and ``_sleep`` are module-level so tests can replace them.
"""

from __future__ import annotations

import asyncio
import time
from collections import OrderedDict
from typing import Any

from aichat.tools.registry import ToolResult

SNIPPET_CHARS = 200
MAX_QUERY_CHARS = 300
DDGS_TIMEOUT_S = 10
CACHE_TTL_S = 600.0
CACHE_SIZE = 32

_clock = time.monotonic
_sleep = asyncio.sleep


def _ddgs_factory(timeout: int = DDGS_TIMEOUT_S) -> Any:
    from ddgs import DDGS  # imported lazily: it pulls in a lot of dependencies

    return DDGS(timeout=timeout)


def _friendly_error(exc: BaseException) -> str:
    name = type(exc).__name__
    if name == "RatelimitException" or "ratelimit" in str(exc).lower():
        return "Search is rate-limited right now; try again shortly."
    if name == "TimeoutException" or isinstance(exc, TimeoutError):
        return "Search timed out; try again shortly."
    return "Search unavailable; try again shortly."


def _format(results: list[dict[str, Any]]) -> str:
    lines = []
    for i, item in enumerate(results, 1):
        title = " ".join(str(item.get("title") or "").split())
        url = str(item.get("href") or item.get("url") or "").strip()
        snippet = " ".join(str(item.get("body") or item.get("snippet") or "").split())
        lines.append(f"{i}. {title} — {url} — {snippet[:SNIPPET_CHARS]}")
    return "\n".join(lines)


class WebSearch:
    """Stateful search helper: spacing, cache and error mapping."""

    def __init__(
        self,
        *,
        max_results: int = 5,
        min_interval_s: float = 2.0,
        cache_ttl_s: float = CACHE_TTL_S,
        cache_size: int = CACHE_SIZE,
    ) -> None:
        self.max_results = max_results
        self.min_interval_s = min_interval_s
        self.cache_ttl_s = cache_ttl_s
        self.cache_size = cache_size
        self._cache: OrderedDict[tuple[str, int], tuple[float, str, int]] = OrderedDict()
        self._next_slot = 0.0

    def _cache_get(self, key: tuple[str, int]) -> tuple[str, int] | None:
        entry = self._cache.get(key)
        if entry is None:
            return None
        stamp, text, count = entry
        if _clock() - stamp > self.cache_ttl_s:
            del self._cache[key]
            return None
        self._cache.move_to_end(key)
        return text, count

    def _cache_put(self, key: tuple[str, int], text: str, count: int) -> None:
        self._cache[key] = (_clock(), text, count)
        self._cache.move_to_end(key)
        while len(self._cache) > self.cache_size:
            self._cache.popitem(last=False)

    async def _wait_turn(self) -> None:
        # Reserve the slot synchronously (no await in between) so concurrent calls queue up.
        now = _clock()
        start = max(now, self._next_slot)
        self._next_slot = start + self.min_interval_s
        if start > now:
            await _sleep(start - now)

    async def search(self, query: str, max_results: int | None = None) -> ToolResult:
        query = " ".join(str(query or "").split())[:MAX_QUERY_CHARS]
        if not query:
            return ToolResult(False, "Search error: query is empty.", "search error")
        n = max(1, min(int(max_results or self.max_results), 10))
        key = (query.casefold(), n)

        cached = self._cache_get(key)
        if cached is not None:
            text, count = cached
            return ToolResult(True, text, f"Searched: {query} ({count} results, cached)")

        await self._wait_turn()
        try:
            results = await asyncio.wait_for(
                asyncio.to_thread(self._blocking_search, query, n), DDGS_TIMEOUT_S + 5
            )
        except TimeoutError as exc:
            return ToolResult(False, _friendly_error(exc), "search timed out")
        except Exception as exc:
            if "no results" in str(exc).lower():
                return ToolResult(True, "No results found.", f"Searched: {query} (0 results)")
            message = _friendly_error(exc)
            return ToolResult(False, message, "search failed")
        if not results:
            return ToolResult(True, "No results found.", f"Searched: {query} (0 results)")
        text = _format(results)
        self._cache_put(key, text, len(results))
        return ToolResult(True, text, f"Searched: {query} ({len(results)} results)")

    @staticmethod
    def _blocking_search(query: str, n: int) -> list[dict[str, Any]]:
        ddgs = _ddgs_factory(timeout=DDGS_TIMEOUT_S)
        return list(ddgs.text(query, max_results=n) or [])
