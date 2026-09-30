"""``current_datetime`` tool."""

from __future__ import annotations

from datetime import datetime

from aichat.tools.registry import ToolResult

_WEEKDAYS = ("Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday")


def format_utc_offset(now: datetime) -> str:
    """``UTC+05:30`` / ``UTC-07:00`` for an aware datetime."""
    offset = now.utcoffset()
    minutes = round(offset.total_seconds() / 60) if offset is not None else 0
    sign = "+" if minutes >= 0 else "-"
    hh, mm = divmod(abs(minutes), 60)
    return f"UTC{sign}{hh:02d}:{mm:02d}"


def describe(now: datetime | None = None) -> dict[str, str]:
    """Local time facts: ``iso``, ``weekday``, ``timezone``, ``utc_offset``."""
    if now is None:
        now = datetime.now().astimezone()
    elif now.tzinfo is None:
        now = now.astimezone()
    return {
        "iso": now.replace(microsecond=0).isoformat(),
        "weekday": _WEEKDAYS[now.weekday()],
        "timezone": now.tzname() or "local",
        "utc_offset": format_utc_offset(now),
    }


async def run(now: datetime | None = None) -> ToolResult:
    info = describe(now)
    content = (
        f"Local time: {info['iso']}\n"
        f"Weekday: {info['weekday']}\n"
        f"Timezone: {info['timezone']} ({info['utc_offset']})"
    )
    return ToolResult(True, content, f"{info['weekday']} {info['iso'][:16].replace('T', ' ')}")
