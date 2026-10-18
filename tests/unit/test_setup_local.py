"""The first-run "Set up local AI" flow, against fakes only.

Hugging Face is ``FakeHfApi`` (in memory, or behind a real loopback socket for the end-to-end
test); the runtime install is a stub. No NPU, no network, no live StudioForge.
"""

from __future__ import annotations

import asyncio
import os
from pathlib import Path
from typing import Any

import pytest

from chatforge.config import AppConfig, load_config, update_config
from chatforge.models.catalog import Catalog
from chatforge.models.downloader import Downloader
from chatforge.models.hf_search import HfSearch
from chatforge.models.registry import Registry
from chatforge.paths import Paths
from chatforge.runtime.npu_detect import NpuProbe
from chatforge.setup_local import LocalSetup
from tests.fakes.hf_api import FakeFile, FakeHfApi
from tests.fakes.hf_http_server import serve_fake_hf

STARTER = Catalog.load().recommended().id
BIG = 10**15


def starter_files() -> dict[str, FakeFile]:
    return {
        "openvino_model.xml": FakeFile(b"<net/>" * 40, lfs=False),
        "openvino_model.bin": FakeFile(os.urandom(90_000)),
        "openvino_tokenizer.xml": FakeFile(b"<tok/>" * 9, lfs=False),
        "openvino_tokenizer.bin": FakeFile(os.urandom(4000)),
        "openvino_detokenizer.xml": FakeFile(b"<detok/>" * 9, lfs=False),
        "openvino_detokenizer.bin": FakeFile(os.urandom(3000)),
        "config.json": FakeFile(b'{"a": 1}', lfs=False),
    }


class Harness:
    def __init__(self, home: Path) -> None:
        self.paths = Paths.default()
        self.paths.ensure_dirs()
        self.cfg: AppConfig = load_config(self.paths)
        self.events: list[dict[str, Any]] = []
        self.runtime = False
        self.runtime_error: Exception | None = None
        self.npu = NpuProbe(False, "no Intel NPU (AI Boost) was found on this PC")
        self.free = BIG
        self.fake = FakeHfApi()
        self.repo = self.fake.add_repo(STARTER, starter_files(), license="apache-2.0")
        self.catalog = Catalog.load()
        self.registry = Registry(
            self.paths.models_dir,
            self.paths.cache_dir,
            catalog=self.catalog,
            state_file=self.paths.state_file,
        )
        self.registry.scan()
        self.client = self.fake.client()
        self.downloader: Downloader | None = None
        self.setup: LocalSetup | None = None
        self.installs = 0
        self.runtime_hooks = 0

    def patch(self, patch: dict[str, Any]) -> Any:
        self.cfg, restart = update_config(self.cfg, patch, self.paths)
        return self.cfg, restart

    async def fake_install(self, progress) -> None:
        self.installs += 1
        if self.runtime_error is not None:
            progress({"status": "error", "error": str(self.runtime_error)})
            raise self.runtime_error
        for i in (1, 2, 3):
            progress(
                {
                    "status": "downloading",
                    "downloaded_bytes": i * 1000,
                    "total_bytes": 3000,
                    "speed_bps": 1e6,
                    "eta_s": 1,
                }
            )
            await asyncio.sleep(0)
        progress({"status": "extracting", "downloaded_bytes": 3000, "total_bytes": 3000})
        self.runtime = True
        progress({"status": "done", "downloaded_bytes": 3000, "total_bytes": 3000})

    def _runtime_hook(self) -> None:
        self.runtime_hooks += 1

    async def probe(self) -> NpuProbe:
        return self.npu

    async def build(self, *, endpoint: str | None = None) -> LocalSetup:
        if endpoint is None:
            hf = HfSearch(client=self.client)
            self.downloader = Downloader(
                self.paths.models_dir,
                self.paths.downloads_file,
                hf=hf,
                client=self.client,
                free_bytes=lambda _p: self.free,
            )
        else:
            hf = HfSearch(endpoint=endpoint)
            self.downloader = Downloader(
                self.paths.models_dir,
                self.paths.downloads_file,
                hf=hf,
                endpoint=endpoint,
                free_bytes=lambda _p: self.free,
            )
        await self.downloader.start()
        self.setup = LocalSetup(
            paths=self.paths,
            get_config=lambda: self.cfg,
            catalog=self.catalog,
            registry=self.registry,
            downloader=self.downloader,
            emit=self.events.append,
            apply_patch=self.patch,
            install_runtime=self.fake_install,
            runtime_installed=lambda: self.runtime,
            probe_npu=self.probe,
            free_bytes=lambda _p: self.free,
            on_runtime_installed=self._runtime_hook,
        )
        return self.setup

    async def run_to_end(self, timeout: float = 20.0) -> dict[str, Any]:
        assert self.setup is not None
        await self.setup.start()
        task = self.setup._task
        assert task is not None
        await asyncio.wait_for(asyncio.shield(task), timeout)
        return self.setup.snapshot()

    def phases(self) -> list[str]:
        out: list[str] = []
        for e in self.events:
            if e["type"] == "setup.progress" and (not out or out[-1] != e["phase"]):
                out.append(e["phase"])
        return out

    async def aclose(self) -> None:
        if self.setup is not None:
            await self.setup.aclose()
        if self.downloader is not None:
            for f in self.repo.files.values():
                f.release.set()
            await self.downloader.aclose()
        await self.client.aclose()


@pytest.fixture
async def h(chatforge_home: Path):
    harness = Harness(chatforge_home)
    yield harness
    await harness.aclose()


# --------------------------------------------------------------------------- the card is offered


async def test_a_fresh_install_is_offered_the_card(h: Harness) -> None:
    setup = await h.build()
    snap = setup.snapshot()
    assert snap["needed"] is True
    assert snap["phase"] == "idle"
    assert snap["model_id"] == STARTER
    assert snap["model_label"] and snap["model_licence"] == "apache-2.0"
    assert snap["runtime_installed"] is False and snap["model_installed"] is False
    assert 0.9e9 < snap["model_bytes"] < 1.0e9  # the catalog's 0.93 GB until HF says otherwise


async def test_not_offered_when_another_provider_is_selected(h: Harness) -> None:
    setup = await h.build()
    h.patch({"chat": {"provider": "studioforge", "model": "m"}})
    assert setup.needed() is False


async def test_not_offered_after_not_now(h: Harness) -> None:
    setup = await h.build()
    setup.dismiss()
    assert h.cfg.startup.local_setup_dismissed is True
    assert setup.snapshot()["needed"] is False


# --------------------------------------------------------------------------- the happy path


async def test_runtime_model_check_done_and_the_chat_points_at_the_model(h: Harness) -> None:
    h.patch({"chat": {"model": "", "provider": "local-npu"}})
    await h.build()
    snap = await h.run_to_end()
    assert snap["phase"] == "done", snap
    assert h.phases() == ["checking", "runtime", "model", "verifying", "done"]
    assert snap["percent"] == 100.0
    assert snap["runtime_installed"] and snap["model_installed"]
    assert snap["needed"] is False
    assert snap["verified"]["files"] == 7 and snap["verified"]["bytes"] > 90_000
    assert h.cfg.chat.model == STARTER and h.cfg.chat.provider == "local-npu"
    rec = h.registry.get(STARTER)
    assert rec is not None and rec.complete
    # Every file arrived exactly as published (the downloader hashed the big ones).
    for name, f in h.repo.files.items():
        assert (
            h.paths.models_dir / "OpenVINO" / STARTER.split("/")[1] / name
        ).read_bytes() == f.content


async def test_the_pages_are_told_when_the_runtime_arrives(h: Harness) -> None:
    await h.build()
    await h.run_to_end()
    assert h.runtime_hooks == 1  # the status line stops saying "not installed"


async def test_progress_never_goes_backwards(h: Harness) -> None:
    await h.build()
    await h.run_to_end()
    seen = [e["percent"] for e in h.events if e["type"] == "setup.progress"]
    assert seen == sorted(seen)
    assert seen[-1] == 100.0 and 0.0 <= seen[0] < 100.0


async def test_no_npu_says_cpu_and_slower_before_the_download(h: Harness) -> None:
    await h.build()
    await h.run_to_end()
    events = [e for e in h.events if e["type"] == "setup.progress"]
    first_note = next(i for i, e in enumerate(events) if e.get("device_note"))
    first_model = next(i for i, e in enumerate(events) if e["phase"] == "model")
    assert first_note < first_model  # said before the first byte of the model
    note = events[first_note]["device_note"]
    assert note["device"] == "CPU" and note["slower"] is True
    assert "CPU" in note["label"] and "slower" in note["label"]
    assert h.cfg.local.device == "NPU"  # the setting is not rewritten: a later NPU just works


async def test_an_npu_is_named_as_the_device(h: Harness) -> None:
    h.npu = NpuProbe(True, "an Intel NPU is present")
    await h.build()
    snap = await h.run_to_end()
    assert snap["device_note"]["device"] == "NPU" and snap["device_note"]["slower"] is False


async def test_a_configured_cpu_device_is_called_cpu(h: Harness) -> None:
    h.patch({"local": {"device": "CPU"}})
    await h.build()
    snap = await h.run_to_end()
    assert snap["device_note"]["device"] == "CPU"


async def test_an_existing_runtime_is_not_installed_again(h: Harness) -> None:
    h.runtime = True
    await h.build()
    snap = await h.run_to_end()
    assert snap["phase"] == "done" and h.installs == 0
    assert "runtime" not in h.phases()


async def test_the_users_other_local_model_choice_is_kept(h: Harness) -> None:
    other = h.paths.models_dir / "OpenVINO" / "Other-int4-ov"
    other.mkdir(parents=True)
    for name in (
        "openvino_model.xml",
        "openvino_model.bin",
        "openvino_tokenizer.xml",
        "openvino_detokenizer.xml",
    ):
        (other / name).write_bytes(b"x")
    h.registry.scan()
    h.patch({"chat": {"model": "OpenVINO/Other-int4-ov"}})
    await h.build()
    await h.run_to_end()
    assert h.cfg.chat.model == "OpenVINO/Other-int4-ov"


# --------------------------------------------------------------------------- end to end over a socket


async def test_end_to_end_over_a_real_loopback_http_server(h: Harness) -> None:
    with serve_fake_hf(h.fake) as endpoint:
        await h.build(endpoint=endpoint)
        snap = await h.run_to_end()
        assert snap["phase"] == "done", snap
        assert h.fake.resolve_requests("openvino_model.bin")
    assert h.registry.get(STARTER).complete


# --------------------------------------------------------------------------- failure and cancel


async def test_too_little_disk_stops_before_any_download(h: Harness) -> None:
    h.free = 500 * 1024**2
    await h.build()
    snap = await h.run_to_end()
    assert snap["phase"] == "error"
    assert snap["failure"]["code"] == "insufficient_disk"
    assert "disk space" in snap["failure"]["message"]
    assert h.installs == 0 and h.fake.requests == []


async def test_a_runtime_failure_is_reported_and_try_again_works(h: Harness) -> None:
    h.runtime_error = RuntimeError("could not reach github.com")
    await h.build()
    snap = await h.run_to_end()
    assert snap["phase"] == "error" and "github.com" in snap["failure"]["message"]
    assert snap["failure"]["hint"]  # says what to do next
    h.runtime_error = None
    snap = await h.run_to_end()
    assert snap["phase"] == "done" and snap["failure"] is None


async def test_a_corrupt_download_is_not_accepted(h: Harness) -> None:
    h.repo.files["openvino_model.bin"].serve = os.urandom(90_000)  # hash will not match
    await h.build()
    snap = await h.run_to_end(timeout=30)
    assert snap["phase"] == "error", snap
    assert h.registry.get(STARTER) is None or not h.registry.get(STARTER).complete
    assert h.cfg.chat.model != "" and snap["failure"]["message"]


async def test_cancel_mid_download_removes_the_partial_files(h: Harness) -> None:
    big = h.repo.files["openvino_model.bin"]
    big.hang_after = 20_000
    await h.build()
    await h.setup.start()
    for _ in range(400):
        await asyncio.sleep(0.01)
        if big.hung.is_set():
            break
    assert big.hung.is_set(), "the fake never reached the stall point"
    assert h.setup.snapshot()["phase"] == "model"
    snap = await h.setup.cancel()
    assert snap["phase"] == "cancelled"
    model_dir = h.paths.models_dir / "OpenVINO" / STARTER.split("/")[1]
    leftovers = [p.name for p in model_dir.rglob("*") if p.is_file()] if model_dir.exists() else []
    assert leftovers == [] or all(not n.endswith(".part") for n in leftovers)
    assert "done" not in h.phases()


async def test_start_twice_runs_once(h: Harness) -> None:
    await h.build()
    await h.setup.start()
    await h.setup.start()
    await asyncio.wait_for(asyncio.shield(h.setup._task), 20)
    assert h.installs == 1
