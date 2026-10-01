"""JSON GET helper for the fixed public APIs behind the data tools (weather, Wikipedia,
exchange rates).

The hosts are constants in each tool module, never chosen by the model, so these requests
skip the SSRF vetting that ``fetch_url`` applies to model-chosen URLs. Every failure is an
:class:`ApiError` whose message is safe to show to the model.
"""

from __future__ import annotations

import json
from typing import Any

import httpx

#: Wikimedia answers 403 to a User-Agent without contact details (w.wiki/4wJS).
USER_AGENT = "AIChat/0.1 (+https://github.com/LaserLloyd/AI-Chat; desktop assistant)"
TIMEOUT_S = 10.0
MAX_BYTES = 2_000_000


class ApiError(Exception):
    """A lookup failed; ``status`` is the HTTP status when the server answered."""

    def __init__(self, message: str, status: int | None = None) -> None:
        super().__init__(message)
        self.status = status


async def get_json(
    url: str,
    params: dict[str, Any] | None = None,
    *,
    transport: httpx.AsyncBaseTransport | None = None,
    timeout_s: float = TIMEOUT_S,
) -> Any:
    """``GET url`` and decode the JSON body; raise :class:`ApiError` on any failure."""
    headers = {"User-Agent": USER_AGENT, "Accept": "application/json"}
    body = bytearray()
    try:
        async with (
            httpx.AsyncClient(
                transport=transport,
                timeout=httpx.Timeout(timeout_s),
                follow_redirects=True,
                headers=headers,
            ) as http,
            http.stream("GET", url, params=params) as resp,
        ):
            if resp.status_code >= 400:
                raise ApiError(f"the service answered HTTP {resp.status_code}", resp.status_code)
            async for chunk in resp.aiter_bytes():
                body += chunk
                if len(body) > MAX_BYTES:  # stop reading; do not buffer a runaway reply
                    raise ApiError("the reply was too large")
    except httpx.TimeoutException as exc:
        raise ApiError("the service timed out") from exc
    except httpx.HTTPError as exc:
        raise ApiError("the service could not be reached") from exc
    try:
        return json.loads(body)
    except ValueError as exc:
        raise ApiError("the reply was not valid JSON") from exc
