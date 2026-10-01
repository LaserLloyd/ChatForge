"""runtime.manager.LocalModelManager against a stub supervisor (and once against the
real OvmsSupervisor driving tests/fakes/ovms_child.py)."""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path

import httpx
import pytest

from aichat.config import validate_config
from aichat.llm.errors import LLMError
from aichat.llm.providers import SEED_PROVIDERS, LocalOvmsProvider
from aichat.models.catalog import Catalog
from aichat.paths import Paths
from aichat.runtime.manager import LocalModelManager
from aichat.runtime.ovms_supervisor import OvmsError
from tests.fakes.clock import FakeClock

# A test-owned catalog: the packaged one changes with NPU findings (WS1 moved the
# default to Qwen2.5-1.5B and put Qwen3-4B on the avoid list).
QWEN = "Test/Chat-A-int4-ov"  # tools + reasoning parser
SMALL = "Test/Chat-B-int4-ov"  # tools only
TEST_CATALOG = Catalog.from_dict(
    {
        "model": [
            {
                "id": QWEN,
                "label": "A",
                "npu": "recommended",
                "tool_parser": "hermes3",
                "reasoning_parser": "qwen3",
            },
            {"id": SMALL, "label": "B", "npu": "supported", "tool_parser": "hermes3"},
        ],
        "avoid": [{"pattern": "Broken", "reason": "garbage output"}],
    }
)


class StubSupervisor:
    """Just enough of OvmsSupervisor: start/stop/is_alive/exit_event/crashed/last_error."""

    def __init__(self, *, ticks: tuple[str, ...] = (), fail: BaseException | None = None):
        self.exit_event = asyncio.Event()
        self.ticks = ticks
        self.fail = fail
        self.alive = False
        self.starts: list = []
        self.stops = 0
        self.port = 18610
        self.gate: asyncio.Event | None = None  # hold start() until set
        self.stop_gate: asyncio.Event | None = None  # hold stop() until set
        self.started = asyncio.Event()
        self.stop_entered = asyncio.Event()
        self.crashed = False
        self.last_error: str | None = None
        self.log: list[str] = []

    async def start(self, spec, on_tick=None) -> str:
        self.starts.append(spec)
        self.log.append("start")
        self.exit_event.clear()
        self.crashed = False
        self.started.set()
        for phase in self.ticks:
            if on_tick:
                on_tick(phase, 1.0)
        if self.gate is not None:
            await self.gate.wait()
        if self.fail is not None:
            raise self.fail
        self.port += 1
        self.alive = True
        return f"http://127.0.0.1:{self.port}/v3"

    async def stop(self, timeout_s: float = 10) -> None:
        self.stops += 1
        self.log.append("stop")
        self.stop_entered.set()
        if self.stop_gate is not None:
            await self.stop_gate.wait()
        self.alive = False
        self.exit_event.set()

    def is_alive(self) -> bool:
        return self.alive

    def crash(self) -> None:
        self.alive = False
        self.crashed = True
        self.last_error = "The local model server exited with code 3221226505."
        self.exit_event.set()


def make_manager(
    tmp_path: Path,
    sup: StubSupervisor | None = None,
    *,
    clock: FakeClock | None = None,
    local: dict | None = None,
    installed: bool = True,
    **kw,
) -> tuple[LocalModelManager, StubSupervisor, dict]:
    sup = sup or StubSupervisor()
    holder = {"cfg": validate_config({"local": {"idle_unload_minutes": 10, **(local or {})}})}
    paths = Paths.from_home(tmp_path / "home")
    clock = clock or FakeClock(1000.0)
    mgr = LocalModelManager(
        sup,
        get_config=lambda: holder["cfg"],
        paths=paths,
        catalog=TEST_CATALOG,
        clock=clock,
        wall_clock=lambda: 1_700_000_000.0 + clock.now,
        is_installed=lambda: installed,
        unload_grace_s=1.0,
        **kw,
    )
    return mgr, sup, holder


# --------------------------------------------------------------------------- #


async def test_load_builds_launch_spec_from_config_and_catalog(tmp_path) -> None:
    mgr, sup, _ = make_manager(tmp_path)
    await mgr.start()
    assert mgr.status()["state"] == "unloaded"
    base = await mgr.ensure_loaded(QWEN)
    assert base == "http://127.0.0.1:18611/v3"
    spec = sup.starts[0]
    assert spec.model_id == QWEN
    assert spec.device == "NPU" and spec.max_prompt_len == 4096
    assert spec.tool_parser == "hermes3" and spec.reasoning_parser == "qwen3"
    home = tmp_path / "home"
    assert spec.model_path == home / "models" / "Test" / "Chat-A-int4-ov"
    assert spec.cache_dir == home / "cache" / "ov" / "Test--Chat-A-int4-ov" / "NPU-4096"
    assert spec.log_path == home / "logs" / "ovms.log"
    st = mgr.status()
    assert st["state"] == "ready" and st["model_id"] == QWEN and st["device"] == "NPU"
    assert st["first_compile"] is True and st["expected_s"] == 360.0
    assert st["idle_timeout_s"] == 600.0
    assert st["unload_at"] == pytest.approx(1_700_000_000.0 + 1000.0 + 600.0)
    # already loaded: no second start
    assert await mgr.ensure_loaded(QWEN) == base
    assert len(sup.starts) == 1


async def test_switch_model_and_reload_required_setting(tmp_path) -> None:
    mgr, sup, holder = make_manager(tmp_path)
    await mgr.ensure_loaded(QWEN)
    await mgr.ensure_loaded(SMALL)  # switch: stop then start
    assert sup.log == ["start", "stop", "start"]
    assert sup.starts[1].reasoning_parser is None
    holder["cfg"] = validate_config({"local": {"device": "CPU"}})  # "reload required"
    await mgr.ensure_loaded(SMALL)
    assert sup.log[-2:] == ["stop", "start"] and sup.starts[-1].device == "CPU"


async def test_status_ticks_while_compiling(tmp_path) -> None:
    sup = StubSupervisor(ticks=("starting", "compiling", "compiling"))
    mgr, _, _ = make_manager(tmp_path, sup)
    seen: list[dict] = []
    unsubscribe = mgr.subscribe(seen.append)
    await mgr.ensure_loaded(QWEN)
    states = [s["state"] for s in seen]
    assert states[0] == "compiling" and states[-1] == "ready"
    assert states.count("compiling") >= 3  # initial + one per supervisor tick
    ticking = [s for s in seen if s["state"] == "compiling"]
    assert all(s["expected_s"] == 360.0 and s["first_compile"] for s in ticking)
    assert all(s["elapsed_s"] is not None and s["unload_at"] is None for s in ticking)
    unsubscribe()
    await mgr.unload()
    assert seen[-1]["state"] == "ready"  # nothing after unsubscribe


async def test_lease_counts_in_flight_and_serialises_npu(tmp_path) -> None:
    clock = FakeClock(0.0)
    mgr, _, _ = make_manager(tmp_path, clock=clock)
    first_in = asyncio.Event()
    release_first = asyncio.Event()
    order: list[str] = []

    async def job(name: str, hold: asyncio.Event | None) -> None:
        async with mgr.lease(QWEN) as base:
            assert base.startswith("http://127.0.0.1:")
            order.append(f"{name}-in")
            if hold is not None:
                first_in.set()
                await hold.wait()
            order.append(f"{name}-out")

    t1 = asyncio.create_task(job("a", release_first))
    await first_in.wait()
    t2 = asyncio.create_task(job("b", None))
    for _ in range(5):
        await asyncio.sleep(0)
    assert mgr.in_flight == 2  # b is counted while it queues for the NPU
    assert order == ["a-in"]
    clock.advance(30)
    release_first.set()
    await asyncio.gather(t1, t2)
    assert order == ["a-in", "a-out", "b-in", "b-out"]
    assert mgr.in_flight == 0
    assert mgr.last_activity == 30  # touched at the end


async def test_lease_decrements_on_error(tmp_path) -> None:
    mgr, _, _ = make_manager(tmp_path)
    with pytest.raises(RuntimeError):
        async with mgr.lease(QWEN):
            raise RuntimeError("request failed")
    assert mgr.in_flight == 0


async def test_crash_sets_error_and_next_request_reloads_once(tmp_path) -> None:
    mgr, sup, _ = make_manager(tmp_path)
    old = await mgr.ensure_loaded(QWEN)
    sup.crash()
    for _ in range(5):
        await asyncio.sleep(0)
    st = mgr.status()
    assert st["state"] == "error" and "3221226505" in st["error"]
    async with mgr.lease(QWEN) as base:
        assert base != old and sup.is_alive()
    assert len(sup.starts) == 2

    # a reload that fails is tried once per request, then reported
    sup.crash()
    await asyncio.sleep(0)
    sup.fail = OvmsError("exited while loading", code="exited", hint="See logs")
    with pytest.raises(LLMError) as ei:
        async with mgr.lease(QWEN):
            pass
    assert ei.value.code == "model_loading_failed"
    assert mgr.status()["state"] == "error"
    assert len(sup.starts) == 3
    sup.fail = None
    assert await mgr.ensure_loaded(QWEN)
    assert mgr.status()["state"] == "ready"


async def test_dead_child_detected_before_lease(tmp_path) -> None:
    mgr, sup, _ = make_manager(tmp_path)
    await mgr.ensure_loaded(QWEN)
    sup.alive = False  # e.g. after sleep/resume, no exit event seen
    async with mgr.lease(QWEN):
        assert sup.is_alive()
    assert len(sup.starts) == 2


async def test_user_unload_during_request_cancels_then_unloads(tmp_path) -> None:
    mgr, sup, _ = make_manager(tmp_path)
    inside = asyncio.Event()
    events: list[str] = []

    async def request() -> None:
        me = asyncio.current_task()

        def on_cancel() -> None:
            events.append(f"cancel(in_flight={mgr.in_flight}, stops={sup.stops})")
            me.cancel()

        async with mgr.lease(QWEN, on_cancel=on_cancel):
            inside.set()
            await asyncio.sleep(30)

    task = asyncio.create_task(request())
    await inside.wait()
    await mgr.unload("user")
    assert events == ["cancel(in_flight=1, stops=0)"]  # cancelled before the stop
    with pytest.raises(asyncio.CancelledError):
        await task
    assert mgr.in_flight == 0 and sup.stops == 1
    st = mgr.status()
    assert st["state"] == "unloaded" and st["last_unload"]["reason"] == "user"


async def test_unload_during_load_cancels_the_waiter(tmp_path) -> None:
    sup = StubSupervisor(ticks=("compiling",))
    sup.gate = asyncio.Event()
    mgr, _, _ = make_manager(tmp_path, sup)
    waiter = asyncio.create_task(mgr.ensure_loaded(QWEN))
    await sup.started.wait()
    assert mgr.status()["state"] == "compiling"
    await mgr.unload("user")
    with pytest.raises(LLMError) as ei:
        await waiter
    assert ei.value.code == "cancelled"
    assert mgr.status()["state"] == "unloaded"


async def test_cancelled_waiter_does_not_abort_the_load(tmp_path) -> None:
    sup = StubSupervisor()
    sup.gate = asyncio.Event()
    mgr, _, _ = make_manager(tmp_path, sup)
    waiter = asyncio.create_task(mgr.ensure_loaded(QWEN))
    await sup.started.wait()
    waiter.cancel()  # the user pressed Stop while it compiled
    with pytest.raises(asyncio.CancelledError):
        await waiter
    sup.gate.set()
    base = await mgr.ensure_loaded(QWEN)  # joins/finishes the same load
    assert base and len(sup.starts) == 1 and mgr.status()["state"] == "ready"


async def test_not_installed(tmp_path) -> None:
    mgr, sup, _ = make_manager(tmp_path, installed=False)
    await mgr.start()
    assert mgr.status()["state"] == "not_installed"
    with pytest.raises(LLMError) as ei:
        await mgr.ensure_loaded(QWEN)
    assert ei.value.action == "open_settings" and not sup.starts


async def test_supervisor_not_installed_error_maps_state(tmp_path) -> None:
    sup = StubSupervisor(fail=OvmsError("missing", code="not_installed", hint="Install it"))
    mgr, _, _ = make_manager(tmp_path, sup)
    with pytest.raises(LLMError) as ei:
        await mgr.ensure_loaded(QWEN)
    assert ei.value.code == "model_loading_failed"
    assert ei.value.action == "open_settings" and "not_installed" in ei.value.hint


async def test_aclose_unloads_and_records_state(tmp_path) -> None:
    mgr, sup, _ = make_manager(tmp_path)
    await mgr.ensure_loaded(QWEN)
    await mgr.aclose()
    assert sup.stops == 1 and mgr.status()["state"] == "unloaded"
    import json

    state = json.loads((tmp_path / "home" / "state.json").read_text(encoding="utf-8"))
    assert state["last_unload"]["reason"] == "shutdown"
    assert QWEN in state["last_used"]


async def test_local_provider_uses_manager(tmp_path) -> None:
    mgr, _, holder = make_manager(tmp_path)
    provider = LocalOvmsProvider(SEED_PROVIDERS["local-npu"], mgr, settings=lambda: holder["cfg"])
    client = await provider.client_for(QWEN)
    try:
        assert client.base_url == mgr.base_url
    finally:
        await client.aclose()
    assert provider.budget(QWEN).max_prompt_tokens == 3968
    assert mgr.model_supports_tools(QWEN) and not mgr.model_supports_tools("acme/llama-7b")
    assert not mgr.model_supports_tools("Test/Broken-int4-ov")  # avoid-listed: no parser


# --------------------------------------------------------------------------- #
# Real OvmsSupervisor + fake OVMS child
# --------------------------------------------------------------------------- #


@pytest.mark.timeout(60)
async def test_real_supervisor_with_fake_child(tmp_path) -> None:
    from aichat.runtime.ovms_supervisor import OvmsSupervisor
    from tests.fakes import ovms_child

    paths = Paths.from_home(tmp_path / "home")
    model_dir = paths.models_dir / "Test" / "Chat-A-int4-ov"
    model_dir.mkdir(parents=True)
    cfg = validate_config({"local": {"extra_args": ["--fake_load_s", "0.2"]}})
    sup = OvmsSupervisor(
        paths.runtime_dir / "ovms.exe",
        command_prefix=ovms_child.command_prefix(),
        state_file=paths.state_file,
        load_timeout_s=30,
        poll_interval_s=0.05,
        use_job=sys.platform == "win32",
    )
    mgr = LocalModelManager(sup, get_config=lambda: cfg, paths=paths, catalog=TEST_CATALOG)
    try:
        async with mgr.lease(QWEN) as base:
            async with httpx.AsyncClient(trust_env=False) as http:
                models = (await http.get(f"{base}/models")).json()
            assert models["data"][0]["id"] == QWEN
        assert mgr.status()["state"] == "ready"
        await mgr.unload("idle")
        assert mgr.status()["state"] == "unloaded" and not sup.is_alive()
    finally:
        await mgr.aclose()


@pytest.mark.timeout(60)
async def test_real_supervisor_crash_then_reload(tmp_path) -> None:
    from aichat.runtime.ovms_supervisor import OvmsSupervisor
    from tests.fakes import ovms_child

    paths = Paths.from_home(tmp_path / "home")
    (paths.models_dir / "Test" / "Chat-A-int4-ov").mkdir(parents=True)
    extra = ["--fake_load_s", "0.1", "--fake_crash_after_s", "1.0"]
    cfg = validate_config({"local": {"extra_args": extra}})
    sup = OvmsSupervisor(
        paths.runtime_dir / "ovms.exe",
        command_prefix=ovms_child.command_prefix(),
        state_file=paths.state_file,
        load_timeout_s=30,
        poll_interval_s=0.05,
        use_job=sys.platform == "win32",
    )
    mgr = LocalModelManager(sup, get_config=lambda: cfg, paths=paths, catalog=TEST_CATALOG)
    try:
        first = await mgr.ensure_loaded(QWEN)
        async with asyncio.timeout(15):
            while mgr.status()["state"] != "error":
                await asyncio.sleep(0.05)
        assert sup.crashed and mgr.status()["error"]
        async with mgr.lease(QWEN) as base:  # the next request reloads once
            async with httpx.AsyncClient(trust_env=False) as http:
                resp = await http.get(f"{base}/models")
            assert resp.status_code == 200
        assert first  # (the port may or may not be reused)
    finally:
        await mgr.aclose()
    assert not sup.is_alive()


async def test_external_stop_is_not_a_crash(tmp_path) -> None:
    mgr, sup, _ = make_manager(tmp_path)
    await mgr.ensure_loaded(QWEN)
    await sup.stop()  # e.g. someone called supervisor.stop() directly
    for _ in range(5):
        await asyncio.sleep(0)
    st = mgr.status()
    assert st["state"] == "unloaded" and st["error"] is None
    assert st["last_unload"]["reason"] == "stopped"
    async with mgr.lease(QWEN):
        assert sup.is_alive()


# --------------------------------------------------------------------------- #
# NPU-incompatible models: device fallback
# --------------------------------------------------------------------------- #

BROKEN = "Test/Broken-int4-ov"  # matches TEST_CATALOG's avoid pattern


class DeviceStub(StubSupervisor):
    """Fails ``start()`` on the devices in ``fail_on`` (like OVMS exiting while loading)."""

    def __init__(self, fail_on: tuple[str, ...] = (), **kw):
        super().__init__(**kw)
        self.fail_on = fail_on

    async def start(self, spec, on_tick=None) -> str:
        if spec.device in self.fail_on:
            self.starts.append(spec)
            self.log.append(f"fail:{spec.device}")
            raise OvmsError("exited with code 1 while loading", code="exited", hint="See logs")
        return await super().start(spec, on_tick)


async def test_npu_incompatible_model_runs_on_the_fallback_device(tmp_path) -> None:
    mgr, sup, _ = make_manager(tmp_path)
    await mgr.ensure_loaded(BROKEN)
    spec = sup.starts[0]
    assert spec.device == "GPU"
    assert spec.cache_dir.name == "GPU-4096"
    st = mgr.status()
    assert st["state"] == "ready" and st["device"] == "GPU"
    assert st["device_fallback"] == {"from": "NPU", "to": "GPU", "reason": "garbage output"}
    # A verified model on the same manager stays on the NPU, with no fallback flag.
    await mgr.ensure_loaded(QWEN)
    assert sup.starts[-1].device == "NPU" and mgr.status()["device_fallback"] is None


async def test_fallback_device_failure_moves_on_to_the_cpu(tmp_path) -> None:
    sup = DeviceStub(fail_on=("GPU",))
    mgr, _, _ = make_manager(tmp_path, sup)
    base = await mgr.ensure_loaded(BROKEN)  # one call: GPU fails, CPU loads
    assert base and [s.device for s in sup.starts] == ["GPU", "CPU"]
    st = mgr.status()
    assert st["state"] == "ready" and st["device"] == "CPU" and st["error"] is None
    assert st["device_fallback"]["to"] == "CPU"
    # Later calls go straight to the CPU (the same key: no reload).
    assert await mgr.ensure_loaded(BROKEN) == base and len(sup.starts) == 2


async def test_every_fallback_failing_is_a_clear_error(tmp_path) -> None:
    sup = DeviceStub(fail_on=("GPU", "CPU"))
    mgr, _, _ = make_manager(tmp_path, sup)
    with pytest.raises(LLMError) as ei:
        await mgr.ensure_loaded(BROKEN)
    assert ei.value.code == "model_loading_failed" and mgr.status()["state"] == "error"
    with pytest.raises(LLMError) as ei:
        await mgr.ensure_loaded(BROKEN)  # nothing left to try: refused without a start
    assert "does not run correctly on the NPU" in ei.value.message
    assert len(sup.starts) == 2


async def test_fallback_none_refuses_and_names_an_npu_model(tmp_path) -> None:
    mgr, sup, _ = make_manager(tmp_path, local={"npu_fallback_device": "none"})
    with pytest.raises(LLMError) as ei:
        await mgr.ensure_loaded(BROKEN)
    assert ei.value.action == "open_settings"
    assert "garbage output" in ei.value.hint and QWEN in ei.value.hint  # the recommended one
    assert not sup.starts


async def test_fallback_only_applies_when_the_device_is_npu(tmp_path) -> None:
    mgr, sup, _ = make_manager(tmp_path, local={"device": "CPU"})
    await mgr.ensure_loaded(BROKEN)
    assert sup.starts[0].device == "CPU" and mgr.status()["device_fallback"] is None


async def test_switching_models_cancels_the_old_models_leases_first(tmp_path) -> None:
    mgr, sup, _ = make_manager(tmp_path)
    inside = asyncio.Event()
    events: list[str] = []

    async def request() -> None:
        me = asyncio.current_task()

        def on_cancel() -> None:
            events.append(f"cancel(stops={sup.stops})")
            me.cancel()

        async with mgr.lease(QWEN, on_cancel=on_cancel):
            inside.set()
            await asyncio.sleep(30)

    task = asyncio.create_task(request())
    await inside.wait()
    await mgr.ensure_loaded(SMALL)  # the user picked another model mid-reply
    assert events == ["cancel(stops=0)"]  # the reply was told before OVMS stopped
    with pytest.raises(asyncio.CancelledError):
        await task
    assert mgr.in_flight == 0 and sup.log == ["start", "stop", "start"]
    assert mgr.status()["model_id"] == SMALL


# --------------------------------------------------------------------------- #
# Background precompile
# --------------------------------------------------------------------------- #


async def test_precompile_compiles_then_unloads(tmp_path) -> None:
    import json

    sup = StubSupervisor(ticks=("compiling",))
    sup.gate = asyncio.Event()
    mgr, _, _ = make_manager(tmp_path, sup)
    task = asyncio.create_task(mgr.precompile(QWEN))
    await sup.started.wait()
    st = mgr.status()
    assert st["state"] == "compiling" and st["background"] is True and st["first_compile"]
    sup.gate.set()
    assert await task == "compiled"
    st = mgr.status()
    assert st["state"] == "unloaded" and st["background"] is False
    assert st["last_unload"]["reason"] == "precompiled"
    assert sup.log == ["start", "stop"]
    state = json.loads((tmp_path / "home" / "state.json").read_text(encoding="utf-8"))
    assert QWEN not in (state.get("last_used") or {})  # nobody used it


async def test_precompile_joined_by_a_request_keeps_the_model(tmp_path) -> None:
    sup = StubSupervisor()
    sup.gate = asyncio.Event()
    mgr, _, _ = make_manager(tmp_path, sup)
    background = asyncio.create_task(mgr.precompile(QWEN))
    await sup.started.wait()
    chat = asyncio.create_task(mgr.ensure_loaded(QWEN))  # the user asks meanwhile
    for _ in range(5):
        await asyncio.sleep(0)
    assert mgr.status()["background"] is False
    sup.gate.set()
    base = await chat
    assert await background == "compiled"
    assert mgr.status()["state"] == "ready" and mgr.base_url == base
    assert len(sup.starts) == 1 and sup.stops == 0


async def test_request_for_another_model_cancels_the_precompile(tmp_path) -> None:
    sup = StubSupervisor()
    sup.gate = asyncio.Event()
    mgr, _, _ = make_manager(tmp_path, sup)
    background = asyncio.create_task(mgr.precompile(QWEN))
    await sup.started.wait()
    chat = asyncio.create_task(mgr.ensure_loaded(SMALL))
    assert await background == "cancelled"
    sup.gate.set()
    await chat
    assert mgr.status()["state"] == "ready" and mgr.status()["model_id"] == SMALL


async def test_precompile_is_busy_while_the_runtime_is_in_use(tmp_path) -> None:
    mgr, sup, _ = make_manager(tmp_path)
    await mgr.ensure_loaded(QWEN)
    assert mgr.idle_for_background() is False
    assert await mgr.precompile(SMALL) == "busy"
    assert len(sup.starts) == 1 and mgr.status()["model_id"] == QWEN


async def test_precompile_skips_a_warm_cache(tmp_path) -> None:
    from aichat.runtime import compile_cache

    mgr, sup, holder = make_manager(tmp_path)
    spec = mgr.build_spec(QWEN)
    compile_cache.mark_compiled(
        spec.cache_dir,
        model_id=QWEN,
        device="NPU",
        max_prompt_len=4096,
        ovms_version="2026.4.0",
        load_s=60.0,
        compile_hash=mgr.compile_hash(spec),
    )
    (spec.cache_dir / "1.blob").write_bytes(b"b")
    assert mgr.is_warm(spec)
    assert await mgr.precompile(QWEN) == "warm" and not sup.starts
    # Other compile settings (a plugin_config in extra_args) make it cold again.
    holder["cfg"] = validate_config({"local": {"extra_args": ["--plugin_config", "{}"]}})
    assert not mgr.is_warm(mgr.build_spec(QWEN))


async def test_failed_precompile_leaves_no_error_banner(tmp_path) -> None:
    sup = StubSupervisor(fail=OvmsError("exited while loading", code="exited", hint="See logs"))
    mgr, _, _ = make_manager(tmp_path, sup)
    with pytest.raises(LLMError):
        await mgr.precompile(QWEN)
    st = mgr.status()
    assert st["state"] == "unloaded" and st["error"] is None and st["background"] is False


async def test_start_runs_the_precompiler_only_with_a_registry(tmp_path) -> None:
    mgr, _, _ = make_manager(tmp_path)
    await mgr.start()
    assert mgr.precompiler is None  # no registry: nothing to compile
    mgr2, _, _ = make_manager(tmp_path, registry=object())
    await mgr2.start()
    assert mgr2.precompiler is not None
    await mgr2.aclose()
    assert mgr2._precompile_task is None  # noqa: SLF001 - cancelled on close
    mgr3, _, _ = make_manager(tmp_path, registry=object(), precompile=False)
    await mgr3.start()
    assert mgr3.precompiler is None
