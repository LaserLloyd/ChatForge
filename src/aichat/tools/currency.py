"""``exchange_rate`` tool: currency conversion at the latest ECB reference rate.

Rates come from Frankfurter (no API key), which republishes the European Central Bank's
daily reference rates for about 30 major currencies. The ``.app`` host is a fallback for
the ``.dev`` one.
"""

from __future__ import annotations

import re
from typing import Any

import httpx

from aichat.tools.registry import ToolResult
from aichat.tools.webapi import ApiError, get_json

RATE_URLS = ("https://api.frankfurter.dev/v1/latest", "https://api.frankfurter.app/latest")
_CODE = re.compile(r"^[A-Z]{3}$")
MAX_AMOUNT = 1e15


def _code(value: Any) -> str | None:
    text = str(value or "").strip().upper()
    return text if _CODE.match(text) else None


def _fmt(value: float) -> str:
    if abs(value) >= 1:
        return f"{value:,.2f}"
    return f"{value:.6g}"


async def run(
    base: Any,
    target: Any,
    amount: Any = 1,
    *,
    transport: httpx.AsyncBaseTransport | None = None,
) -> ToolResult:
    frm, to = _code(base), _code(target)
    if frm is None or to is None:
        return ToolResult(
            False,
            "Invalid arguments for exchange_rate: 'from' and 'to' must be 3-letter "
            "currency codes such as USD or JPY.",
            "bad arguments",
        )
    try:
        qty = float(amount if amount not in (None, "") else 1)
    except (TypeError, ValueError):
        qty = float("nan")
    if not (0 < qty < MAX_AMOUNT):
        return ToolResult(
            False,
            "Invalid arguments for exchange_rate: 'amount' must be a positive number.",
            "bad arguments",
        )
    if frm == to:
        return ToolResult(True, f"{_fmt(qty)} {frm} = {_fmt(qty)} {to}", f"{frm} to {to}")
    data: Any = None
    last: ApiError | None = None
    for url in RATE_URLS:
        try:
            data = await get_json(url, {"from": frm, "to": to, "amount": qty}, transport=transport)
            break
        except ApiError as exc:
            last = exc
            if exc.status is not None and 400 <= exc.status < 500:
                break  # an unsupported currency; the mirror would say the same
    if data is None:
        if last is not None and last.status is not None and 400 <= last.status < 500:
            return ToolResult(
                False,
                f"No ECB reference rate for {frm} to {to}. Only about 30 major currencies "
                "are covered; try web_search for others.",
                "rate not available",
            )
        reason = str(last) if last else "no reply"
        return ToolResult(False, f"Exchange-rate lookup failed: {reason}.", "rate lookup failed")
    rate = (data.get("rates") or {}).get(to) if isinstance(data, dict) else None
    if not isinstance(rate, int | float):
        return ToolResult(False, f"No rate for {frm} to {to} in the reply.", "rate not available")
    day = data.get("date", "latest")
    text = f"{_fmt(qty)} {frm} = {_fmt(float(rate))} {to} (ECB reference rate for {day})"
    return ToolResult(True, text, f"{frm} to {to}")
