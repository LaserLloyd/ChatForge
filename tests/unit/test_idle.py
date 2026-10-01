"""runtime.idle.IdleReaper on a fake clock (PLAN §2 WS6 accept, §3 idle race)."""

from __future__ import annotations

import asyncio

import pytest

from chatforge.runtime.idle import IdleReaper
from tests.fakes.clock import FakeClock
from tests.unit.test_manager import QWEN, StubSupervisor, make_manager

TTL = 600.0


async def ready_manager(tmp_path, clock: FakeClock, sup: StubSupervisor | None = None):
    mgr, sup, holder = make_manager(tmp_path, sup, clock=clock)
    await mgr.ensure_loaded(QWEN)
    reaper = IdleReaper(mgr, lambda: TTL, clock=clock, sleep=clock.sleep)
    return mgr, sup, reaper


async def settle() -> None:
    for _ in range(10):
        await asyncio.sleep(0)


async def test_unload_exactly_at_ttl(tmp_path) -> None:
    clock = FakeClock(100.0)
    mgr, sup, reaper = await ready_manager(tmp_path, clock)
    clock.advance(TTL - 0.001)
    assert await reaper.sweep_once() is False
    assert mgr.status()["state"] == "ready"
    clock.advance(0.001)
    assert await reaper.sweep_once() is True
    st = mgr.status()
    assert st["state"] == "unloaded" and st["last_unload"]["reason"] == "idle"
    assert sup.stops == 1


async def test_run_loop_sweeps_every_15s_and_unloads_at_ttl(tmp_path) -> None:
    clock = FakeClock(0.0)
    mgr, _, _ = await ready_manager(tmp_path, clock)
    unloaded_at: list[float] = []
    mgr.subscribe(lambda s: s["state"] == "unloading" and unloaded_at.append(clock.now))
    reaper = IdleReaper(mgr, lambda: 60.0, clock=clock, sleep=clock.sleep, interval_s=15)
    task = asyncio.create_task(reaper.run())
    try:
        async with asyncio.timeout(5):
            while mgr.status()["state"] != "unloaded":
                await asyncio.sleep(0)
    finally:
        task.cancel()
    assert unloaded_at == [60.0]  # sweeps at 0, 15, 30, 45 keep it; 60 unloads
    assert reaper.unloads == 1


async def test_no_unload_while_in_flight(tmp_path) -> None:
    clock = FakeClock(0.0)
    mgr, _, reaper = await ready_manager(tmp_path, clock)
    inside, release = asyncio.Event(), asyncio.Event()

    async def long_request() -> None:
        async with mgr.lease(QWEN):
            inside.set()
            await release.wait()

    task = asyncio.create_task(long_request())
    await inside.wait()
    clock.advance(TTL * 10)
    assert await reaper.sweep_once() is False
    assert mgr.status()["state"] == "ready"
    release.set()
    await task
    # the lease end touched last_activity: a full TTL must pass again
    assert await reaper.sweep_once() is False
    clock.advance(TTL)
    assert await reaper.sweep_once() is True


async def test_no_unload_during_compile_and_sweep_does_not_block(tmp_path) -> None:
    clock = FakeClock(0.0)
    sup = StubSupervisor(ticks=("compiling",))
    sup.gate = asyncio.Event()
    mgr, _, _ = make_manager(tmp_path, sup, clock=clock)
    reaper = IdleReaper(mgr, lambda: TTL, clock=clock, sleep=clock.sleep)
    loading = asyncio.create_task(mgr.ensure_loaded(QWEN))
    await sup.started.wait()
    assert mgr.status()["state"] == "compiling"
    clock.advance(TTL * 10)
    async with asyncio.timeout(1):  # the load holds the lock; the sweep must not wait on it
        assert await reaper.sweep_once() is False
    sup.gate.set()
    await loading
    assert mgr.status()["state"] == "ready" and sup.stops == 0


async def test_request_before_sweep_keeps_the_model(tmp_path) -> None:
    """The request wins the lock first: in_flight is re-checked under the lock."""
    clock = FakeClock(0.0)
    mgr, sup, reaper = await ready_manager(tmp_path, clock)
    clock.advance(TTL + 1)
    inside, release = asyncio.Event(), asyncio.Event()
    seen: list[str] = []

    async def request() -> None:
        async with mgr.lease(QWEN) as base:
            seen.append(base)
            inside.set()
            await release.wait()
            assert sup.is_alive()

    await mgr.lock.acquire()
    req_task = asyncio.create_task(request())
    await settle()
    sweep = asyncio.create_task(reaper.sweep_once())  # passes the unlocked pre-check
    await settle()
    mgr.lock.release()
    await inside.wait()
    assert await sweep is False
    release.set()
    await req_task
    assert sup.stops == 0 and mgr.status()["state"] == "ready"


async def test_request_during_idle_unload_waits_and_reloads(tmp_path) -> None:
    """The sweep wins: the request waits for the unload, then reloads -- never a dead port."""
    clock = FakeClock(0.0)
    sup = StubSupervisor()
    mgr, _, reaper = await ready_manager(tmp_path, clock, sup)
    old = mgr.base_url
    clock.advance(TTL)
    sup.stop_gate = asyncio.Event()
    sweep = asyncio.create_task(reaper.sweep_once())
    await sup.stop_entered.wait()  # unloading, lock held
    assert mgr.status()["state"] == "unloading"
    got: list[tuple[str, bool]] = []

    async def request() -> None:
        async with mgr.lease(QWEN) as base:
            got.append((base, sup.is_alive()))

    req_task = asyncio.create_task(request())
    await settle()
    assert not got  # waiting on the lock
    sup.stop_gate.set()
    assert await sweep is True
    await req_task
    base, alive = got[0]
    assert alive and base != old
    assert sup.log == ["start", "stop", "start"]
    assert mgr.status()["state"] == "ready"


async def test_ttl_zero_never_unloads(tmp_path) -> None:
    clock = FakeClock(0.0)
    mgr, sup, _ = await ready_manager(tmp_path, clock)
    reaper = IdleReaper(mgr, lambda: 0, clock=clock, sleep=clock.sleep)
    clock.advance(10**9)
    assert await reaper.sweep_once() is False
    assert mgr.status()["state"] == "ready" and sup.stops == 0


async def test_sweeper_never_dies(tmp_path) -> None:
    clock = FakeClock(0.0)
    mgr, _, _ = await ready_manager(tmp_path, clock)
    calls = {"n": 0}

    def flaky_ttl() -> float:
        calls["n"] += 1
        if calls["n"] == 1:
            raise ValueError("bad setting")
        return 60.0

    reaper = IdleReaper(mgr, flaky_ttl, clock=clock, sleep=clock.sleep)
    real_sweep = reaper.sweep_once
    boom = {"left": 2}

    async def sometimes_broken() -> bool:
        if boom["left"]:
            boom["left"] -= 1
            raise RuntimeError("sweep exploded")
        return await real_sweep()

    reaper.sweep_once = sometimes_broken  # type: ignore[method-assign]
    task = asyncio.create_task(reaper.run())
    try:
        async with asyncio.timeout(5):
            while mgr.status()["state"] != "unloaded":
                await asyncio.sleep(0)
    finally:
        task.cancel()
    assert boom["left"] == 0  # two sweeps raised and the loop carried on
    assert calls["n"] >= 2  # the unreadable TTL was logged, not fatal
    assert mgr.status()["last_unload"]["reason"] == "idle"


async def test_crash_then_request_reloads(tmp_path) -> None:
    clock = FakeClock(0.0)
    mgr, sup, reaper = await ready_manager(tmp_path, clock)
    sup.crash()
    await settle()
    assert mgr.status()["state"] == "error"
    clock.advance(TTL * 2)
    assert await reaper.sweep_once() is False  # not "ready": the reaper leaves it alone
    async with mgr.lease(QWEN):
        assert sup.is_alive()
    assert mgr.status()["state"] == "ready"


@pytest.mark.parametrize("reason", ["user"])
async def test_user_unload_during_request_cancels_it(tmp_path, reason) -> None:
    clock = FakeClock(0.0)
    mgr, sup, _ = await ready_manager(tmp_path, clock)
    inside = asyncio.Event()
    cancelled = asyncio.Event()

    async def request() -> None:
        async with mgr.lease(QWEN, on_cancel=cancelled.set):
            inside.set()
            await cancelled.wait()  # the holder stops when told to

    task = asyncio.create_task(request())
    await inside.wait()
    await mgr.unload(reason)
    await task
    assert cancelled.is_set() and sup.stops == 1
    assert mgr.status()["state"] == "unloaded" and mgr.in_flight == 0
