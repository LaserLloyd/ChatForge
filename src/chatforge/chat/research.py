"""Help small models use their tools: spot questions that need live data, and replies
that refuse instead of looking something up.

The local NPU model (Qwen2.5-1.5B) often answers "I don't have real-time data" even
with ``web_search`` offered. The engine uses :func:`plan` in two ways:

* local models, ``eager=True``: before the first round, a question with a *clear*
  live-data intent runs the matching tool up front, so the model answers from the result.
  This path is strict, since a wrong guess puts an irrelevant result into a 4096-token
  prompt;
* any model, ``eager=False``: when a reply with no tool calls is a first-person refusal
  (:func:`is_refusal`), the reply is dropped, the tool runs, and the model answers again.

Everything here is plain pattern matching; it never calls a model.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any

MAX_QUERY_CHARS = 200
#: Only the start of a reply is checked for a refusal; a real answer gets to the point.
REFUSAL_WINDOW_CHARS = 300

_I = re.IGNORECASE
_WEATHER_STRONG = re.compile(r"\b(weather|forecast)\b", _I)
_WEATHER_WORD = re.compile(
    r"\b(rain(ing|y)?|snow(ing|y)?|typhoon|hurricane|humid(ity)?|umbrella|sunny|cloudy|"
    r"windy|storm(y)?|heat ?wave|temperature)\b",
    _I,
)
# A weather word alone is not a weather question ("bake at what temperature", "a poem about
# a sunny day"); with one of these it is ("will it rain", "how cold is it outside").
_WEATHER_CUE = re.compile(
    r"\b(will it|is it|is there|going to|outside|today|tonight|tomorrow|right now|"
    r"at the moment|this (morning|afternoon|evening|week(end)?))\b",
    _I,
)
_NEWS = re.compile(r"\b(news|headlines?|breaking)\b", _I)
# Explicit recency only: "latest", "right now", "today's", "price of".
_LIVE = re.compile(
    r"\b(latest|right now|today'?s|tonight'?s?|yesterday'?s?|last night|"
    r"this (morning|week(end)?)'?s?|price of|stock price|(share|stock) (price|value)|"
    r"live score|exchange rate)\b",
    _I,
)
# "in Tokyo", "for lisbon, portugal", "at Heathrow": the place runs to punctuation other than
# a comma, then is cut at the first word that cannot be part of a place name.
_PLACE = re.compile(r"\b(?:in|for|at|near|around)\s+([\w'. ,-]+)", _I)
_PLACE_END = re.compile(
    r"\b(and|or|but|so|if|because|with|when|while|to|should|do|does|did|will|would|"
    r"can|could|is|are|was|were|be|been|going|like|please|i|you|we|today|tomorrow|"
    r"tonight|now|right|currently|outside|anymore|yet|this|next|later|week(end)?|morning|"
    r"afternoon|evening|monday|tuesday|wednesday|thursday|friday|saturday|sunday)\b.*$",
    _I,
)
# Time words that follow "at"/"for" but are not places ("at the moment", "for Christmas").
_TIME_NOUN = re.compile(
    r"^(moment|minute|time|day|night|noon|midnight|weekend|holidays?|christmas|"
    r"summer|winter|spring|autumn|fall|"
    r"easter|halloween|thanksgiving|new year'?s?|all|a|it|me|my|our|here|there|home|"
    r"my area|area|general)$",
    _I,
)
_CURRENCY = re.compile(r"\b([A-Z]{3})\s+(?:to|in|into)\s+([A-Z]{3})\b")
#: ISO codes the exchange-rate tool can answer (ECB reference rates), so "NYC to LAX" is
#: not read as a currency pair.
_CURRENCIES = frozenset(
    [
        "AUD",
        "BGN",
        "BRL",
        "CAD",
        "CHF",
        "CNY",
        "CZK",
        "DKK",
        "EUR",
        "GBP",
        "HKD",
        "HUF",
        "IDR",
        "ILS",
        "INR",
        "ISK",
        "JPY",
        "KRW",
        "MXN",
        "MYR",
        "NOK",
        "NZD",
        "PHP",
        "PLN",
        "RON",
        "SEK",
        "SGD",
        "THB",
        "TRY",
        "USD",
        "ZAR",
    ]
)

# A refusal speaks in the first person about itself: "I don't have access to real-time
# data", "I can't browse the internet". General statements ("if you can't access your
# router", "the latest information suggests") are answers, not refusals.
_REFUSAL = re.compile(
    r"\b(I|I'm|I am)\b[^.!?\n]{0,60}?\b("
    r"(do not|don't|cannot|can't|(am )?unable to|(am )?not able to|have no (way|ability) to)"
    r"\s+(have\s+)?(direct\s+)?(access|browse|check|look up|retrieve|search|get|fetch|"
    r"provide (real|live|current|up-to-date))"
    r"|(do not|don't) have (any\s+)?(real[- ]?time|live|current|up[- ]to[- ]date|internet|web)"
    r"|(lack|have no) (internet|web|real[- ]?time|live) (access|data|information)"
    r")",
    _I,
)
_CUTOFF = re.compile(r"\bmy (knowledge|training)( data)? (cut-?off|only goes|ends)", _I)
# A promise to look something up with no tool call behind it: "I will use my search engine
# to gather information... Please wait while I fetch relevant data."
_DEFERRAL = re.compile(
    r"\b(I will|I'll|let me|I am going to|I'm going to)\s+(now\s+)?("
    r"search|look (it |that |this )?up|fetch|gather|browse|query|retrieve|"
    r"check (online|the web|the internet)|use (my|a|the) (search|web|browser|tools?))\b"
    r"|\bplease wait while I\b",
    _I,
)


@dataclass
class Plan:
    """A tool call the engine should make itself."""

    name: str
    arguments: dict[str, Any] = field(default_factory=dict)


def _clean_place(text: str) -> str:
    text = _PLACE_END.sub("", text).strip(" ,.?!'-")
    text = re.sub(r"^the\b\s*", "", text, flags=_I)
    if not text or _TIME_NOUN.match(text):
        return ""
    return text[:100]


def weather_place(question: str) -> str:
    """The place named in a weather question ("" when none is named)."""
    for m in _PLACE.finditer(question):
        place = _clean_place(m.group(1))
        if place:
            return place
    return ""


def is_weather_question(question: str, *, strict: bool) -> bool:
    if _WEATHER_STRONG.search(question):
        return True
    if not _WEATHER_WORD.search(question):
        return False
    return not strict or bool(_WEATHER_CUE.search(question))


def _query(question: str) -> str:
    return " ".join(question.split())[:MAX_QUERY_CHARS]


def plan(question: str, enabled: set[str] | frozenset[str], *, eager: bool) -> Plan | None:
    """The tool call that answers ``question``, or ``None``.

    ``eager`` (before the model's first round) only fires on clear live-data intents;
    otherwise (after a refusal) any question falls back to ``web_search``.
    """
    q = " ".join((question or "").split())
    if not q:
        return None
    # Strict on both paths: after a refusal a stray weather word ("bake at what
    # temperature") must still fall through to a web search, not a weather lookup.
    weather_q = is_weather_question(q, strict=True)
    if weather_q and "weather" in enabled:
        place = weather_place(q)
        return Plan("weather", {"location": place} if place else {})
    if "exchange_rate" in enabled:
        m = _CURRENCY.search(q)
        if m and m.group(1) in _CURRENCIES and m.group(2) in _CURRENCIES:
            return Plan("exchange_rate", {"from": m.group(1), "to": m.group(2)})
    if _NEWS.search(q):
        if "news_search" in enabled:
            return Plan("news_search", {"query": _query(q)})
        if "web_search" in enabled:
            return Plan("web_search", {"query": _query(q)})
    if "web_search" not in enabled:
        return None
    if not eager or weather_q or _LIVE.search(q):
        return Plan("web_search", {"query": _query(q)})
    return None


# A refusal worth a lookup is about live information; "I can't access your files" is not.
_LIVE_CUE = re.compile(
    r"real[- ]?time|\blive\b|\bcurrent|up[- ]to[- ]date|\blatest\b|\btoday\b|internet|"
    r"\bweb\b|online|\bbrowse|\bnews\b|weather|forecast|\bprices?\b|\bscores?\b",
    _I,
)
# What follows the refusing verb: "... access [to] your calendar" is about the user's data.
_ABOUT_YOURS = re.compile(r"^\s*(to\s+)?(access\s+(to\s+)?)?your\b", _I)
_WAIT = re.compile(r"\b(please wait|one moment|hold on|stand by|give me a (moment|second))\b", _I)
_TYPOGRAPHIC = str.maketrans(
    {"\u2019": "'", "\u2018": "'", "\u2010": "-", "\u2011": "-", "\u2013": "-"}
)
#: A promise only counts in a short reply; a long one is an answer.
DEFERRAL_MAX_CHARS = 400


def _normalize(text: str) -> str:
    """Typographic apostrophes and hyphens (models emit them) as plain ASCII."""
    return text.translate(_TYPOGRAPHIC)


def _head(reply: str) -> str:
    return _normalize((reply or "").strip()[:REFUSAL_WINDOW_CHARS])


def is_refusal(reply: str) -> bool:
    """True when the start of ``reply`` says, in the first person, that it cannot get
    live or current information (not "I can't access your files")."""
    head = _head(reply)
    if not head:
        return False
    if _CUTOFF.search(head):
        return True
    for m in _REFUSAL.finditer(head):
        if _ABOUT_YOURS.match(head[m.end() :]):
            continue
        if _LIVE_CUE.search(head):
            return True
    return False


def is_deferral(reply: str) -> bool:
    """True when a short ``reply`` promises a lookup and stops there ("let me search for
    that", "please wait while I fetch…"), rather than answering after the promise ("I'll
    look it up: Paris"). Only meaningful for a reply that made no tool call."""
    text = _normalize((reply or "").strip())
    if not text or len(text) > DEFERRAL_MAX_CHARS:
        return False
    m = _DEFERRAL.search(text)
    if not m:
        return False
    if _WAIT.search(text):
        return True
    rest = text[m.end() :]
    if ":" in rest[:40]:
        return False  # "I'll look it up: <the answer>"
    # The promise ends the reply: nothing follows the end of its own sentence.
    return len(re.split(r"(?<=[.!?])\s+", rest.strip(), maxsplit=1)) == 1


def needs_lookup(reply: str) -> bool:
    """A tool-less reply that should be replaced by a real lookup: a refusal, or a
    promise to look something up that was never acted on."""
    return is_refusal(reply) or is_deferral(reply)


__all__ = [
    "Plan",
    "is_deferral",
    "is_refusal",
    "is_weather_question",
    "needs_lookup",
    "plan",
    "weather_place",
]
