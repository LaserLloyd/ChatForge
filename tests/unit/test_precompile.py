"""runtime.precompile.Precompiler: which models it compiles, when, and what it remembers."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from aichat.config import validate_config
from aichat.models.catalog import Catalog
from aichat.paths import Paths
from aichat.runtime import compile_cache
from aichat.runtime.manager import LocalModelManager
from aichat.runtime.ovms_supervisor import OvmsError, spec_compile_hash
from aichat.runtime.precompile import (
    MAX_ATTEMPTS,
    RETRY_FAILED_AFTER_S,
    STATE_KEY,
    Precompiler,
)
from tests.unit.test_manager import StubSupervisor

A = "Test/Alpha-int4-ov"
B = "Test/Beta-int4-ov"
C = "Test/Gamma-int4-ov"
BAD = "Test/Broken-int4-ov"

CATALOG = Catalog.from_dict(
    {
        "model": [
            {"id": A, "label": "A", "npu": "recommended", "tool_parser": "hermes3"},
            {"id": B, "label": "B", "npu": "supported"},
            {"id": C, "label": "C", "npu": "supported"},
        ],
        "avoid": [{"pattern": "Broken", "reason": "garbage output"}],
    }
)


class CompilingStub(StubSupervisor):
    """Like OVMS: a successful load leaves a blob and our marker in ``--cache_dir``."""

    def __init__(self, fail_models: tuple[str, ...] = (), **kw):
        super().__init__(**kw)
        self.fail_models = fail_models

    async def start(self, spec, on_tick=None) -> str:
        if spec.model_id in self.fail_models:
            self.starts.append(spec)
            raise OvmsError("exited while loading", code="model_failed", hint="See logs")
        base = await super().start(spec, on_tick)
        spec.cache_dir.mkdir(parents=True, exist_ok=True)
        (spec.cache_dir / "1.blob").write_bytes(b"blob")
        compile_cache.mark_compiled(
            spec.cache_dir,
            model_id=spec.model_id,
            device=spec.device,
            max_prompt_len=spec.max_prompt_len,
            ovms_version="2026.4.0",
            load_s=1.0,
            compile_hash=spec_compile_hash(spec, ovms_version="2026.4.0"),
        )
        return base


class FakeRegistry:
    def __init__(self, models_dir: Path, records: list[SimpleNamespace]):
        self.models_dir = models_dir
        self.records = {r.id: r for r in records}
        self.scans = 0
        self.fp = 1

    def all(self):
        return sorted(self.records.values(), key=lambda r: r.id)

    def get(self, mid):
        return self.records.get(mid)

    def scan(self):
        self.scans += 1
        return self.all()

    def fingerprint(self):
        return self.fp


def rec(models_dir: Path, mid: str, *, used: float | None = None, source="aichat-downloader"):
    publisher, _, name = mid.partition("/")
    return SimpleNamespace(
        id=mid,
        path=models_dir / publisher / name,
        complete=True,
        missing=[],
        source=source,
        last_used_at=used,
    )


class Clock:
    def __init__(self, now: float = 1_800_000_000.0):
        self.now = now

    def __call__(self) -> float:
        return self.now


def setup(tmp_path, records, *, local=None, chat_model=A, sup=None):
    paths = Paths.from_home(tmp_path / "home")
    registry = FakeRegistry(
        paths.models_dir, [rec(paths.models_dir, *r[:1], **r[1]) for r in records]
    )
    holder = {"cfg": validate_config({"chat": {"model": chat_model}, "local": local or {}})}
    sup = sup or CompilingStub()
    mgr = LocalModelManager(
        sup,
        get_config=lambda: holder["cfg"],
        paths=paths,
        registry=registry,
        catalog=CATALOG,
        is_installed=lambda: True,
        unload_grace_s=1.0,
        precompile=False,  # the tests drive their own Precompiler
    )
    clock = Clock()
    pre = Precompiler(mgr, wall_clock=clock, first_delay_s=0, interval_s=0)
    return pre, mgr, sup, registry, holder, clock


async def test_compiles_cold_models_most_wanted_first_and_unloads(tmp_path) -> None:
    pre, mgr, sup, _, _, _ = setup(
        tmp_path, [(A, {}), (B, {"used": 10.0}), (C, {"used": 20.0})], chat_model=B
    )
    assert [mid for mid, _ in pre.candidates()] == [B, C, A]  # selected, then most recent
    assert await pre.sweep_once() == [B, C, A]
    assert [s.model_id for s in sup.starts] == [B, C, A]
    assert sup.log == ["start", "stop"] * 3  # each one unloaded again
    assert mgr.status()["state"] == "unloaded"
    # Warm now: nothing left to do.
    assert pre.candidates() == [] and await pre.sweep_once() == []
    assert len(sup.starts) == 3


async def test_skips_what_it_should_not_compile(tmp_path) -> None:
    pre, _, _, _, _, _ = setup(
        tmp_path,
        [
            (A, {"source": None}),  # copied in by hand, never loaded: maybe half-written
            (BAD, {}),  # avoid-listed and never picked by the user
            (C, {}),
        ],
        chat_model="MiniMax-M3",  # a cloud model is selected
    )
    assert [mid for mid, _ in pre.candidates()] == [C]


async def test_avoid_listed_model_the_user_picked_compiles_for_its_fallback_device(
    tmp_path,
) -> None:
    pre, _, sup, _, _, _ = setup(tmp_path, [(BAD, {"used": 5.0})])
    assert await pre.sweep_once() == [BAD]
    assert sup.starts[0].device == "GPU"  # where it will actually run


async def test_only_slow_compile_devices_and_only_when_enabled(tmp_path) -> None:
    pre, _, _, _, holder, _ = setup(tmp_path, [(A, {})], local={"device": "CPU"})
    assert pre.candidates() == []
    holder["cfg"] = validate_config({"chat": {"model": A}, "local": {"precompile": False}})
    assert await pre.sweep_once() == []
    holder["cfg"] = validate_config({"chat": {"model": A}})
    assert [mid for mid, _ in pre.candidates()] == [A]


async def test_never_while_the_runtime_is_in_use(tmp_path) -> None:
    pre, mgr, sup, _, _, _ = setup(tmp_path, [(A, {}), (B, {})])
    await mgr.ensure_loaded(B)  # the user is chatting
    assert await pre.sweep_once() == []
    assert [s.model_id for s in sup.starts] == [B]


async def test_a_failure_is_remembered_and_retried_later(tmp_path) -> None:
    sup = CompilingStub(fail_models=(A,))
    pre, mgr, _, _, _, clock = setup(tmp_path, [(A, {}), (B, {})], sup=sup)
    assert await pre.sweep_once() == [B]  # A failed, B still compiled
    state = json.loads(Path(mgr.paths.state_file).read_text(encoding="utf-8"))
    (key,) = state[STATE_KEY]
    assert key.startswith(f"{A}|NPU|4096|2026.4.0|") and state[STATE_KEY][key]["error"]
    assert mgr.status()["state"] == "unloaded" and mgr.status()["error"] is None
    assert pre.candidates() == []  # not retried right away...
    clock.now += RETRY_FAILED_AFTER_S + 1
    pre._attempts.clear()  # noqa: SLF001 - a new app run
    sup.fail_models = ()
    assert await pre.sweep_once() == [A]  # ...but after the back-off
    state = json.loads(Path(mgr.paths.state_file).read_text(encoding="utf-8"))
    assert STATE_KEY not in state


async def test_a_compile_that_keeps_being_cancelled_is_dropped_for_this_run(tmp_path) -> None:
    pre, mgr, sup, _, _, _ = setup(tmp_path, [(A, {})])
    sup.gate = asyncio.Event()
    for attempt in range(MAX_ATTEMPTS):
        sup.started.clear()
        sweep = asyncio.create_task(pre.sweep_once())
        await sup.started.wait()
        await mgr.unload("user")  # the user stops it
        assert await sweep == [], attempt
    assert pre.candidates() == []


async def test_new_download_is_noticed_through_the_fingerprint(tmp_path) -> None:
    pre, _, _, registry, _, _ = setup(tmp_path, [])
    await pre.sweep_once()
    assert registry.scans == 1  # first look
    await pre.sweep_once()
    assert registry.scans == 1  # nothing changed: no rescan
    registry.records[A] = rec(registry.models_dir, A)
    registry.fp = 2  # a download finished
    assert await pre.sweep_once() == [A]
    assert registry.scans == 2


async def test_run_waits_then_sweeps_and_survives_errors(tmp_path) -> None:
    pre, _, _, _, _, _ = setup(tmp_path, [(A, {})])
    sleeps: list[float] = []
    calls = {"n": 0}
    real_sweep = pre.sweep_once

    async def flaky_sweep():
        calls["n"] += 1
        if calls["n"] == 1:
            raise RuntimeError("boom")
        return await real_sweep()

    async def fake_sleep(seconds: float) -> None:
        sleeps.append(seconds)
        if len(sleeps) >= 3:
            raise asyncio.CancelledError

    pre.sweep_once = flaky_sweep  # type: ignore[method-assign]
    pre._sleep = fake_sleep  # noqa: SLF001
    pre.first_delay_s, pre.interval_s = 60.0, 120.0
    with pytest.raises(asyncio.CancelledError):
        await pre.run()
    assert sleeps == [60.0, 120.0, 120.0] and calls["n"] == 2
    assert pre.compiled == [A]
