"""``wikipedia`` tool: the lead summary of the best-matching English Wikipedia article.

The topic goes through Wikipedia's ``opensearch`` (forgiving about spelling and case),
then the REST summary endpoint returns the article's description and opening paragraph.
"""

from __future__ import annotations

from typing import Any
from urllib.parse import quote

import httpx

from aichat.tools.registry import ToolResult
from aichat.tools.webapi import ApiError, get_json

SEARCH_URL = "https://en.wikipedia.org/w/api.php"
SUMMARY_URL = "https://en.wikipedia.org/api/rest_v1/page/summary/{title}"
MAX_TOPIC_CHARS = 200


async def find_title(
    topic: str, *, transport: httpx.AsyncBaseTransport | None = None
) -> str | None:
    data = await get_json(
        SEARCH_URL,
        {
            "action": "opensearch",
            "search": topic,
            "limit": 1,
            "namespace": 0,
            "format": "json",
        },
        transport=transport,
    )
    # opensearch answers [query, [titles], [descriptions], [urls]]
    if isinstance(data, list) and len(data) > 1 and isinstance(data[1], list) and data[1]:
        title = data[1][0]
        return title if isinstance(title, str) and title else None
    return None


def _page_url(summary: dict[str, Any], title: str) -> str:
    urls = summary.get("content_urls")
    if isinstance(urls, dict):
        page = (urls.get("desktop") or {}).get("page")
        if isinstance(page, str) and page:
            return page
    return f"https://en.wikipedia.org/wiki/{quote(title.replace(' ', '_'))}"


async def run(topic: str, *, transport: httpx.AsyncBaseTransport | None = None) -> ToolResult:
    topic = " ".join(str(topic or "").split())[:MAX_TOPIC_CHARS]
    if not topic:
        return ToolResult(
            False, "Invalid arguments for wikipedia: 'topic' is required.", "bad arguments"
        )
    try:
        title = await find_title(topic, transport=transport)
        if title is None:
            return ToolResult(
                True, f"No Wikipedia article matches '{topic}'.", "Wikipedia: no match"
            )
        summary = await get_json(
            SUMMARY_URL.format(title=quote(title.replace(" ", "_"), safe="")),
            transport=transport,
        )
    except ApiError as exc:
        return ToolResult(False, f"Wikipedia lookup failed: {exc}.", "wikipedia failed")
    if not isinstance(summary, dict):
        return ToolResult(False, "Wikipedia lookup failed: unexpected reply.", "wikipedia failed")
    name = str(summary.get("title") or title)
    lines = [name]
    description = summary.get("description")
    if isinstance(description, str) and description:
        lines[0] = f"{name} ({description})"
    if summary.get("type") == "disambiguation":
        lines.append("This is a disambiguation page: the topic has several meanings.")
    extract = summary.get("extract")
    if isinstance(extract, str) and extract.strip():
        lines.append(extract.strip())
    lines.append(f"URL: {_page_url(summary, name)}")
    return ToolResult(True, "\n".join(lines), f"Wikipedia: {name}")
