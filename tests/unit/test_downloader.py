"""Downloader against the in-memory HF API. No real network, tmp dirs only."""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
from pathlib import Path
from typing import Any

import pytest

from chatforge.models import downloader as downloader_mod
from chatforge.models.downloader import (
    FREE_SPACE_MARGIN_BYTES,
    Downloader,
    _part_path,
    _PartFile,
)
from chatforge.models.hf_search import HfSearch, ModelError
from chatforge.models.registry import Registry, read_sidecar
from tests.fakes.hf_api import FakeFile, FakeHfApi

REPO = "OpenVINO/Qwen3-4B-int4-ov"
GID = "OpenVINO--Qwen3-4B-int4-ov"
BIN = "openvino_model.bin"
BIN_BYTES = os.urandom(64_000)


def repo_files() -> dict[str, FakeFile]:
    return {
        "openvino_model.xml": FakeFile(b"<net/>" * 20, lfs=False),
        BIN: FakeFile(BIN_BYTES),
        "openvino_tokenizer.xml": FakeFile(b"<tok/>" * 5, lfs=False),
        "openvino_tokenizer.bin": FakeFile(os.urandom(3000)),
        "openvino_detokenizer.xml": FakeFile(b"<detok/>" * 5, lfs=False),
        "openvino_detokenizer.bin": FakeFile(os.urandom(2000)),
        "config.json": FakeFile(b'{"a": 1}', lfs=False),
        ".gitattributes": FakeFile(b"*.bin filter=lfs", lfs=False),
    }


class Sleeps:
    def __init__(self) -> None:
        self.calls: list[float] = []

    async def __call__(self, seconds: float) -> None:
        self.calls.append(seconds)
        await asyncio.sleep(0)


class Env:
    def __init__(self, home: Path) -> None:
        self.models = home / "models"
        self.cache = home / "cache"
        self.downloads_file = home / "downloads.json"
        self.fake = FakeHfApi()
        self.repo = self.fake.add_repo(REPO, repo_files())
        self.client = self.fake.client()
        self.sleeps = Sleeps()
        self.hf = HfSearch(client=self.client, sleep=self.sleeps)
        self.free = 10**15
        self.events: list[dict[str, Any]] = []
        self.downloaders: list[Downloader] = []

    @property
    def model_dir(self) -> Path:
        return self.models / "OpenVINO" / "Qwen3-4B-int4-ov"

    def file(self, name: str) -> FakeFile:
        return self.repo.files[name]

    def make(self, **kwargs: Any) -> Downloader:
        dl = Downloader(
            self.models,
            self.downloads_file,
            hf=self.hf,
            client=self.client,
            free_bytes=lambda _p: self.free,
            sleep=self.sleeps,
            **kwargs,
        )
        dl.subscribe(self.events.append)
        self.downloaders.append(dl)
        return dl

    def put_part(self, name: str, data: bytes) -> Path:
        dest = self.model_dir / name
        dest.parent.mkdir(parents=True, exist_ok=True)
        part = _part_path(dest)
        part.write_bytes(data)
        return part


@pytest.fixture
async def env(chatforge_home: Path):
    e = Env(chatforge_home)
    yield e
    for dl in e.downloaders:
        for f in e.repo.files.values():
            f.release.set()
        await dl.aclose()
    await e.client.aclose()


async def run(dl: Downloader) -> dict[str, Any]:
    await dl.start()
    gid = await dl.enqueue(REPO)
    assert gid == GID
    await asyncio.wait_for(dl.join(gid), 10)
    group = dl.get(gid)
    assert group is not None
    return group


def assert_all_published(env: Env) -> None:
    for name, f in env.repo.files.items():
        if name.startswith("."):
            continue
        dest = env.model_dir / name
        assert dest.read_bytes() == f.content, name
        assert not _part_path(dest).exists()


# -- happy path ------------------------------------------------------------


async def test_full_download_publishes_files_sidecar_and_state(env: Env) -> None:
    group = await run(env.make())
    assert group["status"] == "completed"
    assert group["files_done"] == group["files_total"] == 7
    assert_all_published(env)
    assert not (env.model_dir / ".gitattributes").exists()
    assert not env.fake.resolve_requests(".gitattributes")

    sidecar = read_sidecar(env.model_dir)
    assert sidecar is not None
    assert sidecar["source"] == "chatforge-downloader"
    assert sidecar["repo_id"] == REPO
    assert sidecar["revision"] == env.repo.sha
    assert sidecar["license"] == "apache-2.0"
    by_path = {f["path"]: f for f in sidecar["files"]}
    assert by_path[BIN]["sha256"] == hashlib.sha256(BIN_BYTES).hexdigest()
    assert by_path[BIN]["size"] == len(BIN_BYTES)

    # Downloads are pinned to the listed revision.
    assert f"/resolve/{env.repo.sha}/" in env.fake.resolve_requests(BIN)[0].url.path

    state = json.loads(env.downloads_file.read_text())
    assert state["schema"] == 1
    assert state["groups"][0]["repo_id"] == REPO
    assert {f["status"] for f in state["groups"][0]["files"]} == {"completed"}

    # The registry now sees a complete, downloader-sourced model.
    rec = Registry(env.models, env.cache).get(REPO)
    assert rec is not None and rec.complete and rec.source == "chatforge-downloader"


async def test_progress_events_carry_group_fields(env: Env) -> None:
    await run(env.make())
    assert env.events
    last = env.events[-1]
    assert last["group_id"] == GID and last["repo_id"] == REPO
    assert last["group_status"] == "completed"
    assert last["group_percent"] == 100.0
    assert {"downloaded_bytes", "total_bytes", "speed_bps", "eta_s", "percent"} <= set(last)


async def test_unsubscribe_stops_callbacks(env: Env) -> None:
    dl = env.make()
    seen: list[dict[str, Any]] = []
    unsubscribe = dl.subscribe(seen.append)
    unsubscribe()
    await run(dl)
    assert seen == []
    assert env.events  # the other subscriber still ran


async def test_raising_subscriber_is_dropped_not_fatal(env: Env) -> None:
    dl = env.make()

    def bad(_p: dict[str, Any]) -> None:
        raise RuntimeError("closed window")

    dl.subscribe(bad)
    group = await run(dl)
    assert group["status"] == "completed"


async def test_existing_complete_files_are_adopted_without_traffic(env: Env) -> None:
    env.model_dir.mkdir(parents=True)
    for name, f in env.repo.files.items():
        (env.model_dir / name).write_bytes(f.content)
    group = await run(env.make())
    assert group["status"] == "completed"
    assert env.fake.resolve_requests() == []
    assert read_sidecar(env.model_dir)["source"] == "chatforge-downloader"


# -- resume protocol ---------------------------------------------------------


async def test_206_resume_appends_from_part(env: Env) -> None:
    have = 20_000
    env.put_part(BIN, BIN_BYTES[:have])
    group = await run(env.make())
    assert group["status"] == "completed"
    requests = env.fake.resolve_requests(BIN)
    assert len(requests) == 1
    assert requests[0].headers["Range"] == f"bytes={have}-"
    assert_all_published(env)


async def test_200_ignoring_range_restarts_from_zero(env: Env) -> None:
    env.file(BIN).ignore_range = True
    env.put_part(BIN, b"\xff" * 20_000)  # garbage: appending would corrupt the file
    group = await run(env.make())
    assert group["status"] == "completed"
    requests = env.fake.resolve_requests(BIN)
    assert len(requests) == 1 and requests[0].headers["Range"] == "bytes=20000-"
    assert (env.model_dir / BIN).read_bytes() == BIN_BYTES


async def test_416_recovers_by_restarting_without_range(env: Env) -> None:
    env.put_part(BIN, b"\xee" * (len(BIN_BYTES) + 10))
    group = await run(env.make())
    assert group["status"] == "completed"
    requests = env.fake.resolve_requests(BIN)
    assert [r.headers.get("Range") for r in requests] == [f"bytes={len(BIN_BYTES) + 10}-", None]
    assert (env.model_dir / BIN).read_bytes() == BIN_BYTES


async def test_complete_partial_is_published_without_a_request(env: Env) -> None:
    env.put_part(BIN, BIN_BYTES)
    group = await run(env.make())
    assert group["status"] == "completed"
    assert env.fake.resolve_requests(BIN) == []
    assert (env.model_dir / BIN).read_bytes() == BIN_BYTES


# -- verification failures ---------------------------------------------------


async def test_size_mismatch_fails_and_discards(env: Env) -> None:
    env.file(BIN).listed_size = len(BIN_BYTES) + 5
    group = await run(env.make())
    assert group["status"] == "failed"
    assert "size mismatch" in group["error"]
    assert not (env.model_dir / BIN).exists()
    assert not _part_path(env.model_dir / BIN).exists()
    assert read_sidecar(env.model_dir) is None
    # Not retried: a size mismatch is an answer, not a hiccup.
    assert len(env.fake.resolve_requests(BIN)) == 1


async def test_sha_mismatch_fails_and_deletes_partial(env: Env) -> None:
    corrupt = bytearray(BIN_BYTES)
    corrupt[100] ^= 0xFF
    env.file(BIN).serve = bytes(corrupt)
    group = await run(env.make())
    assert group["status"] == "failed"
    assert "sha256 mismatch" in group["error"]
    assert not (env.model_dir / BIN).exists()
    assert not _part_path(env.model_dir / BIN).exists()
    assert len(env.fake.resolve_requests(BIN)) == 1


async def test_failed_file_stops_the_rest_of_the_group(env: Env) -> None:
    env.file(BIN).serve = b"\x00" * len(BIN_BYTES)
    group = await run(env.make())
    statuses = {f["filename"]: f["status"] for f in group["files"]}
    assert statuses[BIN] == "failed"
    assert statuses["openvino_tokenizer.xml"] == "queued"
    assert env.fake.resolve_requests("openvino_tokenizer.xml") == []


# -- retries -----------------------------------------------------------------


async def test_429_backs_off_honouring_retry_after(env: Env) -> None:
    env.file(BIN).rate_limit = 2
    env.file(BIN).retry_after = "10"
    group = await run(env.make())
    assert group["status"] == "completed"
    assert len(env.sleeps.calls) == 2
    # Retry-After 10 beats the 2s/4s base, jittered by 0.8-1.2.
    assert all(8.0 <= s <= 12.0 for s in env.sleeps.calls)
    assert len(env.fake.resolve_requests(BIN)) == 3


async def test_5xx_is_retried_with_exponential_backoff(env: Env) -> None:
    env.file(BIN).server_errors = 2
    group = await run(env.make())
    assert group["status"] == "completed"
    first, second = env.sleeps.calls
    assert 1.6 <= first <= 2.4 and 3.2 <= second <= 4.8


async def test_retries_are_bounded(env: Env) -> None:
    env.file(BIN).server_errors = 99
    group = await run(env.make())
    assert group["status"] == "failed"
    assert len(env.fake.resolve_requests(BIN)) == downloader_mod.DOWNLOAD_MAX_ATTEMPTS
    assert "HTTP 503" in group["error"]


# -- cancel, pause, lock -------------------------------------------------------


async def test_cancel_mid_file_removes_part(env: Env) -> None:
    env.file(BIN).hang_after = 5000
    dl = env.make()
    await dl.start()
    gid = await dl.enqueue(REPO)
    await asyncio.wait_for(env.file(BIN).hung.wait(), 10)
    part = _part_path(env.model_dir / BIN)
    assert part.exists() and part.stat().st_size == 5000
    assert dl.is_downloading(REPO)

    await dl.cancel(gid)

    assert not part.exists()
    assert not (env.model_dir / BIN).exists()
    group = dl.get(gid)
    assert group["status"] == "canceled"
    assert not dl.is_downloading(REPO)
    state = json.loads(env.downloads_file.read_text())
    statuses = {f["filename"]: f["status"] for f in state["groups"][0]["files"]}
    assert statuses[BIN] == "canceled"


async def test_pause_keeps_part_and_resume_continues_with_range(env: Env) -> None:
    env.file(BIN).hang_after = 5000
    dl = env.make()
    await dl.start()
    gid = await dl.enqueue(REPO)
    await asyncio.wait_for(env.file(BIN).hung.wait(), 10)
    await dl.pause(gid)
    part = _part_path(env.model_dir / BIN)
    assert part.stat().st_size == 5000
    assert dl.get(gid)["status"] == "paused"

    env.file(BIN).hang_after = None
    await dl.resume(gid)
    await asyncio.wait_for(dl.join(gid), 10)
    assert dl.get(gid)["status"] == "completed"
    assert env.fake.resolve_requests(BIN)[-1].headers["Range"] == "bytes=5000-"
    assert_all_published(env)


async def test_part_lock_contention_refuses_second_writer(env: Env) -> None:
    dest = env.model_dir / BIN
    dest.parent.mkdir(parents=True)
    with _PartFile(_part_path(dest)):
        group = await run(env.make())
    assert group["status"] == "failed"
    assert "another process is writing" in group["error"]
    assert env.fake.resolve_requests(BIN) == []
    assert not dest.exists()


def test_part_file_lock_is_exclusive(tmp_path: Path) -> None:
    part = tmp_path / "x.part"
    with _PartFile(part), pytest.raises(ModelError) as info, _PartFile(part):
        pass
    assert info.value.code == "part_file_locked"
    with _PartFile(part):  # released on close
        pass


# -- restart -------------------------------------------------------------------


async def test_pending_group_is_paused_after_restart_then_resumes(env: Env) -> None:
    env.file(BIN).hang_after = 5000
    first = env.make()
    await first.start()
    await first.enqueue(REPO)
    await asyncio.wait_for(env.file(BIN).hung.wait(), 10)
    await first.aclose()  # app exit mid-transfer

    # Simulate a crash too: the row says "running" on disk.
    state = json.loads(env.downloads_file.read_text())
    for row in state["groups"][0]["files"]:
        if row["status"] != "completed":
            row["status"] = "running"
    env.downloads_file.write_text(json.dumps(state))

    env.file(BIN).hang_after = None
    before = len(env.fake.requests)
    second = env.make()
    await second.start()
    group = second.get(GID)
    assert group is not None and group["status"] == "paused"
    await asyncio.sleep(0.05)
    assert len(env.fake.requests) == before  # not auto-resumed
    bin_row = next(f for f in group["files"] if f["filename"] == BIN)
    assert bin_row["downloaded_bytes"] == 5000  # re-derived from the .part

    await second.resume(GID)
    await asyncio.wait_for(second.join(GID), 10)
    assert second.get(GID)["status"] == "completed"
    assert env.fake.resolve_requests(BIN)[-1].headers["Range"] == "bytes=5000-"
    assert_all_published(env)


async def test_enqueue_of_paused_group_resumes_it(env: Env) -> None:
    env.file(BIN).hang_after = 5000
    dl = env.make()
    await dl.start()
    await dl.enqueue(REPO)
    await asyncio.wait_for(env.file(BIN).hung.wait(), 10)
    await dl.pause(GID)
    env.file(BIN).hang_after = None
    assert await dl.enqueue(REPO) == GID
    await asyncio.wait_for(dl.join(GID), 10)
    assert dl.get(GID)["status"] == "completed"


async def test_tampered_state_file_rows_are_dropped(env: Env) -> None:
    env.downloads_file.parent.mkdir(parents=True, exist_ok=True)
    env.downloads_file.write_text(
        json.dumps(
            {
                "schema": 1,
                "groups": [
                    {"repo_id": "../../evil", "files": [{"filename": "x", "status": "running"}]},
                    {"repo_id": REPO, "files": [{"filename": "../../x.bin", "status": "queued"}]},
                    "not a dict",
                ],
            }
        )
    )
    dl = env.make()
    await dl.start()
    assert dl.all() == []


# -- refusals ------------------------------------------------------------------


async def test_refuses_without_free_space_plus_margin(env: Env) -> None:
    total = sum(len(f.content) for n, f in env.repo.files.items() if not n.startswith("."))
    env.free = total + FREE_SPACE_MARGIN_BYTES - 1
    dl = env.make()
    await dl.start()
    with pytest.raises(ModelError) as info:
        await dl.enqueue(REPO)
    assert info.value.code == "insufficient_disk"
    assert dl.all() == []
    assert env.fake.resolve_requests() == []

    env.free = total + FREE_SPACE_MARGIN_BYTES
    await dl.enqueue(REPO)
    await asyncio.wait_for(dl.join(GID), 10)
    assert dl.get(GID)["status"] == "completed"


async def test_unknown_free_space_does_not_refuse(env: Env) -> None:
    env.free = None
    group = await run(env.make())
    assert group["status"] == "completed"


@pytest.mark.parametrize("bad", ["../evil", "OpenVINO/..", "OpenVINO/../../x", "a\\b/c"])
async def test_traversal_repo_id_rejected(env: Env, bad: str) -> None:
    dl = env.make()
    await dl.start()
    with pytest.raises(ModelError) as info:
        await dl.enqueue(bad)
    assert info.value.code == "invalid_repo_id"
    assert env.fake.requests == []


@pytest.mark.parametrize("bad_path", ["../../escape.bin", "sub/../../escape.bin", "C:evil.bin"])
async def test_traversal_filename_rejected(env: Env, bad_path: str, chatforge_home: Path) -> None:
    env.repo.extra_tree.append({"type": "file", "path": bad_path, "size": 3})
    dl = env.make()
    await dl.start()
    with pytest.raises(ModelError) as info:
        await dl.enqueue(REPO)
    assert info.value.code == "unsafe_filename"
    assert dl.all() == []
    assert env.fake.resolve_requests() == []
    assert not (chatforge_home.parent / "escape.bin").exists()


async def test_dest_outside_models_dir_is_refused(env: Env) -> None:
    dl = env.make()
    with pytest.raises(ModelError) as info:
        dl._assert_dest(env.models.parent / "x.bin")
    assert info.value.code == "path_escape"


# -- persistence cadence -------------------------------------------------------


async def test_downloads_json_is_throttled_to_1hz(
    env: Env, monkeypatch: pytest.MonkeyPatch
) -> None:
    now = [100.0]
    dl = env.make(clock=lambda: now[0])
    writes: list[str] = []
    real_replace = os.replace

    def counting_replace(src: Any, dst: Any) -> None:
        writes.append(str(dst))
        real_replace(src, dst)

    monkeypatch.setattr(downloader_mod.os, "replace", counting_replace)
    dl._persist()
    dl._persist()
    now[0] += 0.5
    dl._persist()
    assert len(writes) == 1
    now[0] += 0.6
    dl._persist()
    assert len(writes) == 2
    dl._persist(force=True)
    assert len(writes) == 3
    assert not list(env.downloads_file.parent.glob("*.tmp"))


# -- code files are never downloaded -----------------------------------------


async def test_code_files_in_a_repo_are_skipped_and_logged(env: Env, caplog) -> None:
    for name in (
        "modeling_custom.py",
        "setup.PY",
        "cached.pyc",
        "install.sh",
        "run.bat",
        "fix.ps1",
        "sub/helper.py",
    ):
        env.repo.files[name] = FakeFile(b"print('x')\n", lfs=False)
    env.repo.files["README.pyx.md"] = FakeFile(b"docs", lfs=False)  # not a skipped ending
    with caplog.at_level("INFO"):
        group = await run(env.make())
    assert group["status"] == "completed"
    for name in ("modeling_custom.py", "setup.PY", "cached.pyc", "install.sh", "run.bat"):
        assert not (env.model_dir / name).exists()
        assert not env.fake.resolve_requests(name)
    assert not (env.model_dir / "fix.ps1").exists()
    assert not (env.model_dir / "sub" / "helper.py").exists()
    assert (env.model_dir / "README.pyx.md").read_bytes() == b"docs"
    assert (env.model_dir / BIN).read_bytes() == BIN_BYTES
    lines = [r.getMessage() for r in caplog.records if "skipped_code" in r.getMessage()]
    assert len(lines) == 1
    assert "files=7" in lines[0] and "modeling_custom.py" in lines[0]


async def test_a_repo_of_only_code_has_nothing_to_download(env: Env) -> None:
    env.repo.files.clear()
    env.repo.files["run.py"] = FakeFile(b"x", lfs=False)
    dl = env.make()
    await dl.start()
    with pytest.raises(ModelError) as info:
        await dl.enqueue(REPO)
    assert info.value.code == "empty_repo"


async def test_no_skip_line_when_nothing_is_skipped(env: Env, caplog) -> None:
    with caplog.at_level("INFO"):
        await run(env.make())
    assert not [r for r in caplog.records if "skipped_code" in r.getMessage()]


# -- the token is registered for log scrubbing -------------------------------


def test_the_hf_token_is_registered_as_a_secret(env: Env) -> None:
    from chatforge import logging_setup

    token = "hf_" + "A1b2C3d4E5f6G7h8"
    env.make(token=token)
    assert token in logging_setup._secret_values
    assert logging_setup._scrub_text(f"Authorization failed for {token}") == (
        f"Authorization failed for {logging_setup._REDACTED}"
    )
    logging_setup._secret_values.discard(token)
