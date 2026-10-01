"""``weather`` tool: current conditions and a short daily forecast from Open-Meteo.

Open-Meteo needs no API key. A place name is geocoded first; in "City, Region" or
"City, Country" the part after the comma picks among same-named places. With no place,
the home location from Settings (``tools.location``) is used.

Weather data by Open-Meteo.com (CC BY 4.0).
"""

from __future__ import annotations

import re
from datetime import date
from typing import Any

import httpx

from chatforge.tools.registry import ToolResult
from chatforge.tools.webapi import ApiError, get_json

GEOCODE_URL = "https://geocoding-api.open-meteo.com/v1/search"
FORECAST_URL = "https://api.open-meteo.com/v1/forecast"
MAX_DAYS = 7
DEFAULT_DAYS = 3
MAX_PLACE_CHARS = 100

_CURRENT = (
    "temperature_2m,apparent_temperature,relative_humidity_2m,precipitation,"
    "weather_code,wind_speed_10m,wind_gusts_10m"
)
_DAILY = (
    "weather_code,temperature_2m_max,temperature_2m_min,"
    "precipitation_probability_max,precipitation_sum"
)

#: WMO weather interpretation codes, as documented by Open-Meteo.
WMO_CODES: dict[int, str] = {
    0: "clear sky",
    1: "mainly clear",
    2: "partly cloudy",
    3: "overcast",
    45: "fog",
    48: "freezing fog",
    51: "light drizzle",
    53: "drizzle",
    55: "heavy drizzle",
    56: "light freezing drizzle",
    57: "freezing drizzle",
    61: "light rain",
    63: "rain",
    65: "heavy rain",
    66: "light freezing rain",
    67: "freezing rain",
    71: "light snow",
    73: "snow",
    75: "heavy snow",
    77: "snow grains",
    80: "light rain showers",
    81: "rain showers",
    82: "violent rain showers",
    85: "light snow showers",
    86: "snow showers",
    95: "thunderstorm",
    96: "thunderstorm with hail",
    99: "severe thunderstorm with hail",
}

NO_LOCATION = (
    "No location was given and no home location is set. Ask the user which city they "
    "mean, then call weather again with it."
)


def describe(code: Any) -> str:
    try:
        return WMO_CODES.get(int(code), "unknown conditions")
    except (TypeError, ValueError):
        return "unknown conditions"


def _matches(result: dict[str, Any], hint: str) -> bool:
    """Whether a geocoding result fits the part after the comma ("Perth, US" must not pick
    Perth, Australia): a whole region/country name, or an exact country code."""
    names = [str(result.get(k) or "").casefold() for k in ("country", "admin1", "admin2", "admin3")]
    code = str(result.get("country_code") or "").casefold()
    for part in (p.strip() for p in hint.split(",")):
        if not part:
            continue
        if part == code or (part in ("us", "usa", "u.s.") and code == "us"):
            return True
        if any(re.search(rf"\b{re.escape(part)}\b", name) for name in names if name):
            return True
    return False


async def geocode(
    place: str, *, transport: httpx.AsyncBaseTransport | None = None
) -> dict[str, Any] | None:
    """The best Open-Meteo geocoding match for ``place``, or ``None``."""
    name, _, hint = place.partition(",")
    name, hint = name.strip(), hint.strip().casefold()
    if not name:
        return None
    # A stray trailing word ("Porto right" from "in Porto right now") finds nothing, so
    # retry with the last word dropped, down to one word.
    words = name.split()
    results: list[dict[str, Any]] = []
    for n in range(len(words), max(0, len(words) - 3), -1):
        data = await get_json(
            GEOCODE_URL,
            {"name": " ".join(words[:n]), "count": 10, "language": "en", "format": "json"},
            transport=transport,
        )
        if isinstance(data, dict):
            results = [r for r in (data.get("results") or []) if isinstance(r, dict)]
        if results:
            break
    if not results:
        return None
    if hint:
        for result in results:
            if _matches(result, hint):
                return result
    return results[0]


def place_label(result: dict[str, Any]) -> str:
    parts: list[str] = []
    for key in ("name", "admin1", "country"):
        value = str(result.get(key) or "").strip()
        if value and value not in parts:
            parts.append(value)
    return ", ".join(parts) or "that place"


def _num(value: Any) -> str:
    if isinstance(value, int | float):
        return f"{value:.0f}" if abs(value) >= 1 or value == 0 else f"{value:.1f}"
    return "?"


def _day_label(iso: str) -> str:
    try:
        d = date.fromisoformat(iso)
    except (TypeError, ValueError):
        return str(iso)
    return f"{d:%a} {d.day} {d:%b}"


def format_report(label: str, data: dict[str, Any]) -> str:
    cur = data.get("current") or {}
    cu = data.get("current_units") or {}
    t_unit = cu.get("temperature_2m", "°")
    lines = [
        f"Weather for {label} (local time {str(cur.get('time', '?')).replace('T', ' ')}, "
        f"{data.get('timezone', 'local')})"
    ]
    if cur:
        lines.append(
            f"Now: {_num(cur.get('temperature_2m'))}{t_unit} "
            f"(feels like {_num(cur.get('apparent_temperature'))}{t_unit}), "
            f"{describe(cur.get('weather_code'))}, "
            f"humidity {_num(cur.get('relative_humidity_2m'))}%, "
            f"wind {_num(cur.get('wind_speed_10m'))} {cu.get('wind_speed_10m', '')} "
            f"(gusts {_num(cur.get('wind_gusts_10m'))}), "
            f"precipitation {cur.get('precipitation') if isinstance(cur.get('precipitation'), int | float) else 0} "
            f"{cu.get('precipitation', '')}".rstrip()
        )
    daily = data.get("daily") or {}
    du = data.get("daily_units") or {}
    days = daily.get("time") or []
    if days:
        lines.append("Forecast:")
    for i, day in enumerate(days):

        def at(key: str, i: int = i) -> Any:
            seq = daily.get(key) or []
            return seq[i] if i < len(seq) else None

        rain = at("precipitation_probability_max")
        rain_txt = f", rain chance {_num(rain)}%" if rain is not None else ""
        amount = at("precipitation_sum")
        amount_txt = (
            f" ({amount} {du.get('precipitation_sum', '')})".rstrip()
            if isinstance(amount, int | float) and amount > 0
            else ""
        )
        lines.append(
            f"- {_day_label(day)}: {describe(at('weather_code'))}, "
            f"{_num(at('temperature_2m_min'))} to {_num(at('temperature_2m_max'))}"
            f"{du.get('temperature_2m_max', t_unit)}{rain_txt}{amount_txt}"
        )
    lines.append("Source: Open-Meteo.com")
    return "\n".join(lines)


async def run(
    location: str | None,
    *,
    days: int | None = None,
    units: str = "metric",
    default_location: str = "",
    transport: httpx.AsyncBaseTransport | None = None,
) -> ToolResult:
    place = " ".join(str(location or "").split())[:MAX_PLACE_CHARS] or default_location.strip()
    if not place:
        return ToolResult(False, NO_LOCATION, "weather: no location")
    n = max(1, min(int(days or DEFAULT_DAYS), MAX_DAYS))
    try:
        geo = await geocode(place, transport=transport)
        if geo is None:
            return ToolResult(
                False,
                f"No place called '{place}' was found. Try a nearby city, or add the "
                "country after a comma.",
                "weather: place not found",
            )
        params: dict[str, Any] = {
            "latitude": geo.get("latitude"),
            "longitude": geo.get("longitude"),
            "current": _CURRENT,
            "daily": _DAILY,
            "timezone": "auto",
            "forecast_days": n,
        }
        if units == "imperial":
            params.update(
                temperature_unit="fahrenheit", wind_speed_unit="mph", precipitation_unit="inch"
            )
        data = await get_json(FORECAST_URL, params, transport=transport)
    except ApiError as exc:
        return ToolResult(
            False, f"Weather lookup failed: {exc}. Try again shortly.", "weather failed"
        )
    if not isinstance(data, dict):
        return ToolResult(False, "Weather lookup failed: unexpected reply.", "weather failed")
    label = place_label(geo)
    return ToolResult(True, format_report(label, data), f"Weather: {label}")
