"""ToolRegistry: schemas, allowlist, JSON repair, never-raise dispatch."""

import json

import pytest

from aichat.errors import AppError
from aichat.tools import registry as reg
from aichat.tools.registry import (
    TOOL_NAMES,
    ToolNotAllowed,
    ToolRegistry,
    ToolResult,
    assert_tool_allowed,
    parse_arguments,
)

ALL = list(TOOL_NAMES)


def test_tool_names():
    assert TOOL_NAMES == ("web_search", "fetch_url", "current_datetime", "calculator")


def test_toolresult_shape():
    r = ToolResult(True, "c", "s")
    assert (r.ok, r.content, r.summary) == (True, "c", "s")


def test_schemas_are_openai_shaped_and_small():
    schemas = ToolRegistry().schemas(ALL)
    assert [s["function"]["name"] for s in schemas] == ALL
    for s in schemas:
        assert s["type"] == "function"
        fn = s["function"]
        assert fn["parameters"]["type"] == "object"
        assert set(fn["parameters"]["required"]) <= set(fn["parameters"]["properties"])
        assert len(fn["description"]) < 60
    assert len(json.dumps(schemas)) < 1300  # ~400 tokens at 3 chars/token


def test_schemas_filter_and_ignore_unknown():
    r = ToolRegistry()
    assert [s["function"]["name"] for s in r.schemas(["calculator", "bogus"])] == ["calculator"]
    assert r.schemas([]) == []
    assert [s["function"]["name"] for s in r.schemas(iter(["calculator", "web_search"]))] == [
        "web_search",
        "calculator",
    ]


def test_assert_tool_allowed():
    assert_tool_allowed("calculator", ["calculator"])
    assert_tool_allowed("calculator", iter(["x", "calculator"]))
    with pytest.raises(ToolNotAllowed) as ei:
        assert_tool_allowed("fetch_url", ["calculator"])
    assert isinstance(ei.value, AppError)
    assert ei.value.to_payload()["code"] == "tool_not_allowed"
    with pytest.raises(ToolNotAllowed):
        assert_tool_allowed("anything", [])


# --- argument parsing ---------------------------------------------------------------------


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ('{"a": 1}', {"a": 1}),
        ("", {}),
        ("   ", {}),
        (None, {}),
        ('```json\n{"a": 1}\n```', {"a": 1}),
        ('```\n{"a": 1}\n```', {"a": 1}),
        ('```json {"a": 1}```', {"a": 1}),
        ('{"a": 1,}', {"a": 1}),
        ('{"a": [1, 2,], "b": {"c": 3,},}', {"a": [1, 2], "b": {"c": 3}}),
        ('```json\n{"a": 1,\n}\n```', {"a": 1}),
        ('{"expression": "f(1,)"}', {"expression": "f(1,)"}),  # commas in strings untouched
        ('{"q": "a,}"}', {"q": "a,}"}),
        ('{"q": "say \\"hi,\\" ]"}', {"q": 'say "hi," ]'}),
    ],
)
def test_parse_arguments_ok(raw, expected):
    assert parse_arguments(raw) == expected


@pytest.mark.parametrize(
    "raw", ["{bad", "not json", '{"a": }', "[1, 2]", '"str"', "42", "{'a': 1}"]
)
def test_parse_arguments_bad(raw):
    with pytest.raises(ValueError):
        parse_arguments(raw)


# --- dispatch -----------------------------------------------------------------------------


async def call(registry, name, args, enabled=None, max_chars=1500):
    return await registry.call(
        name,
        args if isinstance(args, str) else json.dumps(args),
        enabled=ALL if enabled is None else enabled,
        max_chars=max_chars,
    )


async def test_calculator_call():
    r = await call(ToolRegistry(), "calculator", {"expression": "sqrt(2)*10"})
    assert r.ok
    assert r.content == "14.142135623731"


async def test_calculator_call_with_repaired_json():
    r = await call(ToolRegistry(), "calculator", '```json\n{"expression": "2+2",}\n```')
    assert r.ok
    assert r.content == "4"


async def test_current_datetime_call_accepts_empty_args():
    for raw in ("", "{}", "null"[:0]):
        r = await call(ToolRegistry(), "current_datetime", raw)
        assert r.ok
        assert "Weekday" in r.content


async def test_bad_json_returns_error_not_raise():
    r = await call(ToolRegistry(), "calculator", "{oops")
    assert not r.ok
    assert "JSON" in r.content
    r = await call(ToolRegistry(), "calculator", "[1]")
    assert not r.ok


@pytest.mark.parametrize(
    ("name", "args"),
    [
        ("calculator", {}),
        ("calculator", {"expression": 5}),
        ("web_search", {}),
        ("web_search", {"query": "  "}),
        ("web_search", {"query": ["a"]}),
        ("fetch_url", {}),
        ("fetch_url", {"url": None}),
    ],
)
async def test_missing_or_mistyped_arguments(name, args):
    r = await call(ToolRegistry(), name, args)
    assert not r.ok
    assert "required" in r.content


async def test_unknown_tool_is_a_result_not_an_exception():
    r = await call(ToolRegistry(), "rm_rf", {})
    assert not r.ok
    assert "Unknown tool" in r.content
    for name in ALL:
        assert name in r.content


async def test_disabled_known_tool_raises_tool_not_allowed():
    with pytest.raises(ToolNotAllowed):
        await call(
            ToolRegistry(), "fetch_url", {"url": "http://example.com"}, enabled=["calculator"]
        )
    with pytest.raises(ToolNotAllowed):
        await call(ToolRegistry(), "calculator", {"expression": "1"}, enabled=[])


async def test_tool_exception_becomes_result(monkeypatch):
    from aichat.tools import calculator

    async def boom(expr):
        raise RuntimeError("secret internals")

    monkeypatch.setattr(calculator, "run", boom)
    r = await call(ToolRegistry(), "calculator", {"expression": "1"})
    assert not r.ok
    assert "RuntimeError" in r.content
    assert "secret internals" not in r.content


async def test_content_clipped_to_max_chars():
    r = await call(ToolRegistry(), "current_datetime", "{}", max_chars=20)
    assert len(r.content) <= 20
    assert r.content.endswith("…")


async def test_web_search_wired_through_registry(monkeypatch):
    from aichat.tools import web_search

    class Client:
        def text(self, q, max_results=5, **kw):
            Client.seen = (q, max_results)
            return [{"title": "T", "href": "https://x.io", "body": "B"}]

    monkeypatch.setattr(web_search, "_ddgs_factory", lambda timeout=10: Client())
    registry = ToolRegistry({"web_search_max_results": 3, "web_search_min_interval_s": 0})
    r = await call(registry, "web_search", {"query": "hello"})
    assert r.ok
    assert r.content == "1. T — https://x.io — B"
    assert Client.seen == ("hello", 3)


async def test_fetch_url_wired_through_registry_refuses_loopback():
    r = await call(ToolRegistry(), "fetch_url", {"url": "http://127.0.0.1:18611"})
    assert not r.ok
    assert "Blocked" in r.content


async def test_fetch_url_uses_config(monkeypatch):
    from aichat.tools import fetch_url

    seen = {}

    async def fake_fetch(url, **kw):
        seen.update(kw, url=url)
        return ToolResult(True, "x", "y")

    monkeypatch.setattr(fetch_url, "fetch", fake_fetch)

    class Cfg:
        fetch_max_bytes = 123
        fetch_timeout_s = 4
        block_private_addresses = False

    r = await call(ToolRegistry(Cfg()), "fetch_url", {"url": "http://a.b"}, max_chars=777)
    assert r.ok
    assert seen == {
        "url": "http://a.b",
        "max_chars": 777,
        "max_bytes": 123,
        "timeout_s": 4.0,
        "block_private": False,
        "transport": None,
    }


def test_module_exports():
    assert reg.ToolRegistry is ToolRegistry
