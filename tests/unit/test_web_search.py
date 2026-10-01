"""web_search with ddgs mocked: success, rate limit, timeout, cache and spacing."""

import asyncio
import threading

import pytest
from ddgs.exceptions import DDGSException, RatelimitException
from ddgs.exceptions import TimeoutException as DDGSTimeout

from chatforge.tools import web_search
from chatforge.tools.web_search import WebSearch


class FakeDDGS:
    calls: list[tuple[str, int]]
    timeouts: list[int]

    def __init__(self, results=None, error=None):
        self.results = results if results is not None else []
        self.error = error
        self.calls = []
        self.timeouts = []
        self.threads = []

    def factory(self, timeout=10):
        self.timeouts.append(timeout)
        outer = self

        class _Client:
            def text(self, query, max_results=5, **kw):
                outer.calls.append((query, max_results))
                outer.threads.append(threading.current_thread())
                if outer.error is not None:
                    raise outer.error
                return outer.results

        return _Client()


RESULTS = [
    {"title": "Python", "href": "https://python.org", "body": "The Python language. " * 20},
    {"title": "PyPI\nIndex", "href": "https://pypi.org", "body": "Packages"},
]


@pytest.fixture
def ddgs(monkeypatch, fake_clock):
    fake = FakeDDGS(RESULTS)
    monkeypatch.setattr(web_search, "_ddgs_factory", fake.factory)
    monkeypatch.setattr(web_search, "_clock", fake_clock)
    monkeypatch.setattr(web_search, "_sleep", fake_clock.sleep)
    return fake


async def test_success_format_and_thread(ddgs):
    res = await WebSearch().search("python")
    assert res.ok
    lines = res.content.splitlines()
    assert len(lines) == 2
    title, url, snippet = lines[0][3:].split(" — ")
    assert lines[0].startswith("1. ")
    assert (title, url) == ("Python", "https://python.org")
    assert len(snippet) == 200
    assert lines[1] == "2. PyPI Index — https://pypi.org — Packages"
    assert ddgs.calls == [("python", 5)]
    assert ddgs.timeouts == [10]
    assert ddgs.threads[0] is not threading.main_thread()
    assert "2 results" in res.summary


async def test_max_results_passed_and_clamped(ddgs):
    await WebSearch(max_results=3).search("a")
    await WebSearch().search("b", max_results=99)
    assert ddgs.calls == [("a", 3), ("b", 10)]


async def test_ratelimit_is_friendly(ddgs):
    ddgs.error = RatelimitException("202 Ratelimit")
    res = await WebSearch().search("x")
    assert not res.ok
    assert "rate-limited" in res.content
    assert "Ratelimit" not in res.content  # raw error text is not leaked


async def test_timeout_is_friendly(ddgs):
    ddgs.error = DDGSTimeout("boom")
    res = await WebSearch().search("x")
    assert not res.ok
    assert "timed out" in res.content


async def test_builtin_timeout_error(ddgs):
    ddgs.error = TimeoutError()
    res = await WebSearch().search("x")
    assert not res.ok
    assert "timed out" in res.content


async def test_generic_failure(ddgs):
    ddgs.error = RuntimeError("kaboom")
    res = await WebSearch().search("x")
    assert not res.ok
    assert "unavailable" in res.content
    assert "kaboom" not in res.content


async def test_no_results(ddgs):
    ddgs.results = []
    res = await WebSearch().search("x")
    assert res.ok
    assert res.content == "No results found."
    ddgs.error = DDGSException("No results found.")
    res = await WebSearch().search("y")
    assert res.ok
    assert res.content == "No results found."


async def test_empty_query(ddgs):
    res = await WebSearch().search("   ")
    assert not res.ok
    assert ddgs.calls == []


async def test_errors_are_not_cached(ddgs):
    ws = WebSearch(min_interval_s=0)
    ddgs.error = RatelimitException("x")
    assert not (await ws.search("q")).ok
    ddgs.error = None
    assert (await ws.search("q")).ok
    assert len(ddgs.calls) == 2


async def test_cache_hit_within_ttl(ddgs, fake_clock):
    ws = WebSearch()
    first = await ws.search("Python")
    fake_clock.advance(599)
    second = await ws.search("  python ")  # same key after normalisation
    assert second.content == first.content
    assert "cached" in second.summary
    assert len(ddgs.calls) == 1


async def test_cache_expires_after_ttl(ddgs, fake_clock):
    ws = WebSearch()
    await ws.search("python")
    fake_clock.advance(601)
    await ws.search("python")
    assert len(ddgs.calls) == 2


async def test_cache_lru_eviction(ddgs):
    ws = WebSearch(min_interval_s=0)
    for i in range(33):
        await ws.search(f"q{i}")
    assert len(ddgs.calls) == 33
    await ws.search("q32")  # newest is still cached
    assert len(ddgs.calls) == 33
    await ws.search("q0")  # oldest was evicted
    assert len(ddgs.calls) == 34
    assert len(ws._cache) == 32


async def test_lru_recency_refresh(ddgs):
    ws = WebSearch(min_interval_s=0, cache_size=2)
    await ws.search("a")
    await ws.search("b")
    await ws.search("a")  # refresh a
    await ws.search("c")  # evicts b, not a
    n = len(ddgs.calls)
    await ws.search("a")
    assert len(ddgs.calls) == n
    await ws.search("b")
    assert len(ddgs.calls) == n + 1


async def test_spacing_between_searches(ddgs, fake_clock, monkeypatch):
    sleeps = []
    real_sleep = fake_clock.sleep

    async def spy(seconds):
        sleeps.append(seconds)
        await real_sleep(seconds)

    monkeypatch.setattr(web_search, "_sleep", spy)
    ws = WebSearch(min_interval_s=2.0)
    await ws.search("one")
    assert sleeps == []  # the first search never waits
    await ws.search("two")
    assert sleeps == [pytest.approx(2.0)]
    fake_clock.advance(0.5)
    await ws.search("three")
    assert sleeps[-1] == pytest.approx(1.5)
    fake_clock.advance(10)
    await ws.search("four")
    assert len(sleeps) == 2  # long gap: no wait


async def test_cache_hits_do_not_consume_spacing(ddgs, fake_clock, monkeypatch):
    sleeps = []
    monkeypatch.setattr(web_search, "_sleep", lambda s: sleeps.append(s) or asyncio.sleep(0))
    ws = WebSearch()
    await ws.search("one")
    await ws.search("one")
    await ws.search("one")
    assert sleeps == []


async def test_concurrent_searches_are_queued(ddgs, fake_clock, monkeypatch):
    sleeps = []

    async def spy(seconds):
        sleeps.append(seconds)
        fake_clock.advance(seconds)

    monkeypatch.setattr(web_search, "_sleep", spy)
    ws = WebSearch(min_interval_s=2.0)
    await asyncio.gather(ws.search("a"), ws.search("b"), ws.search("c"))
    assert sum(sleeps) == pytest.approx(4.0)  # a, b, c start 0 s, 2 s and 4 s apart
    assert fake_clock() == pytest.approx(4.0)


async def test_default_factory_builds_ddgs():
    client = web_search._ddgs_factory(timeout=3)
    assert type(client).__name__ == "DDGS"
