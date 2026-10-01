"""Disk-space reporting and folder sizes."""

from __future__ import annotations

from pathlib import Path

import pytest

from chatforge.models import diskspace


@pytest.fixture(autouse=True)
def _clear_cache():
    diskspace.clear_cache()
    yield
    diskspace.clear_cache()


def test_existing_ancestor_walks_up(tmp_path: Path) -> None:
    missing = tmp_path / "a" / "b" / "c"
    assert diskspace._existing_ancestor(missing) == tmp_path
    assert diskspace._existing_ancestor(tmp_path) == tmp_path


def test_disk_report_subtracts_queue(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(diskspace, "_usage", lambda _p: (1000, 400, 600))
    report = diskspace.disk_report(tmp_path / "not-yet", 700)
    assert report["total_bytes"] == 1000
    assert report["free_bytes"] == 600
    assert report["queued_bytes"] == 700
    assert report["free_after_queue_bytes"] == -100
    assert report["error"] is None


def test_disk_report_never_raises(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    def boom(_p: Path) -> tuple[int, int, int]:
        raise OSError("unmapped drive")

    monkeypatch.setattr(diskspace, "_usage", boom)
    report = diskspace.disk_report(tmp_path, 5)
    assert report["free_bytes"] == 0
    assert "unmapped drive" in report["error"]
    assert diskspace.free_bytes(tmp_path) is None


def test_free_bytes_real_volume(tmp_path: Path) -> None:
    free = diskspace.free_bytes(tmp_path)
    assert isinstance(free, int) and free > 0


def test_usage_is_cached(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[str] = []

    def fake_disk_usage(path: Path) -> tuple[int, int, int]:
        calls.append(str(path))
        return (10, 5, 5)

    monkeypatch.setattr(diskspace.shutil, "disk_usage", fake_disk_usage)
    diskspace._usage(tmp_path)
    diskspace._usage(tmp_path)
    assert len(calls) == 1
    diskspace.clear_cache()
    diskspace._usage(tmp_path)
    assert len(calls) == 2


def test_folder_size_skips_dot_dirs(tmp_path: Path) -> None:
    (tmp_path / "a.bin").write_bytes(b"x" * 10)
    (tmp_path / "sub").mkdir()
    (tmp_path / "sub" / "b.bin").write_bytes(b"x" * 5)
    (tmp_path / ".cache" / "huggingface").mkdir(parents=True)
    (tmp_path / ".cache" / "huggingface" / "big").write_bytes(b"x" * 100)
    assert diskspace.folder_size(tmp_path) == 115
    assert diskspace.folder_size(tmp_path, skip_dot_dirs=True) == 15
    assert diskspace.folder_size(tmp_path / "missing") == 0
    assert diskspace.folder_size(tmp_path / "a.bin") == 10
