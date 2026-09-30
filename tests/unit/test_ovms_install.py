"""OVMS runtime install: download/verify/resume, zip-slip guard, status."""

from __future__ import annotations

import asyncio
import hashlib
import io
import os
import stat
import zipfile
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest

from aichat.runtime import ovms_install as oi


def _zip_bytes(members: dict[str, bytes], *, symlink: str | None = None) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        for name, data in members.items():
            zf.writestr(name, data)
        if symlink:
            info = zipfile.ZipInfo(symlink)
            info.external_attr = (stat.S_IFLNK | 0o777) << 16
            zf.writestr(info, "target")
    return buf.getvalue()


def _asset(data: bytes, name: str = "ovms_test.zip") -> oi.OvmsAsset:
    return oi.OvmsAsset("python_on", name, len(data), hashlib.sha256(data).hexdigest())


def _server(data: bytes, *, honour_range: bool = True, calls: list | None = None):
    def handler(request: httpx.Request) -> httpx.Response:
        if calls is not None:
            calls.append(dict(request.headers))
        rng = request.headers.get("Range")
        if rng and honour_range:
            start = int(rng.split("=")[1].split("-")[0])
            if start >= len(data):
                return httpx.Response(416)
            body = data[start:]
            return httpx.Response(
                206,
                content=body,
                headers={"Content-Range": f"bytes {start}-{len(data) - 1}/{len(data)}"},
            )
        return httpx.Response(200, content=data)

    return httpx.AsyncClient(transport=httpx.MockTransport(handler))


def test_pinned_constants():
    assert oi.OVMS_VERSION == "2026.4.0"
    assert oi.ASSET == "ovms_windows_2026.4.0_python_on.zip"
    assert oi.ASSET_BYTES == 138_798_816
    assert oi.ASSET_SHA256 == "5a022e44e794e6a9cb0f1c6c40822167dac53a36daf9af123c9411974cef1914"
    assert oi.ASSETS["python_off"].size == 117_195_695
    assert oi.ASSETS["python_on"].url().endswith("/v2026.4.0/ovms_windows_2026.4.0_python_on.zip")
    with pytest.raises(oi.InstallError) as err:
        oi.asset_for("python_maybe")
    assert err.value.code == "unknown_variant"


async def test_download_verifies_and_publishes(tmp_path):
    data = os.urandom(300_000)
    asset = _asset(data)
    events: list[dict] = []
    async with _server(data) as client:
        out = await oi.download_asset(
            asset,
            tmp_path / asset.filename,
            client=client,
            progress=events.append,
            url="https://x/test.zip",
        )
    assert out.read_bytes() == data
    assert not (tmp_path / (asset.filename + ".part")).exists()
    assert any(e["status"] == "verifying" for e in events)


async def test_checksum_mismatch_deletes_part(tmp_path):
    data = os.urandom(50_000)
    asset = oi.OvmsAsset("python_on", "bad.zip", len(data), "0" * 64)
    async with _server(data) as client:
        with pytest.raises(oi.InstallError) as err:
            await oi.download_asset(asset, tmp_path / "bad.zip", client=client, url="https://x/z")
    assert err.value.code == "checksum"
    assert not (tmp_path / "bad.zip").exists()
    assert not (tmp_path / "bad.zip.part").exists()


async def test_size_mismatch_refused(tmp_path):
    data = os.urandom(10_000)
    asset = oi.OvmsAsset("python_on", "s.zip", len(data) + 5, hashlib.sha256(data).hexdigest())
    async with _server(data) as client:
        with pytest.raises(oi.InstallError) as err:
            await oi.download_asset(asset, tmp_path / "s.zip", client=client, url="https://x/z")
    assert err.value.code == "size"


async def test_resume_with_range(tmp_path):
    data = os.urandom(200_000)
    asset = _asset(data)
    (tmp_path / (asset.filename + ".part")).write_bytes(data[:70_000])
    calls: list = []
    async with _server(data, calls=calls) as client:
        out = await oi.download_asset(
            asset, tmp_path / asset.filename, client=client, url="https://x/z"
        )
    assert out.read_bytes() == data
    assert calls[0].get("range") == "bytes=70000-"


async def test_server_ignoring_range_restarts_cleanly(tmp_path):
    data = os.urandom(120_000)
    asset = _asset(data)
    (tmp_path / (asset.filename + ".part")).write_bytes(b"\0" * 40_000)  # garbage prefix
    async with _server(data, honour_range=False) as client:
        out = await oi.download_asset(
            asset, tmp_path / asset.filename, client=client, url="https://x/z"
        )
    assert out.read_bytes() == data


async def test_complete_but_wrong_part_restarts(tmp_path):
    data = os.urandom(20_000)
    asset = _asset(data)
    # A stale .part of the right length but wrong bytes is re-fetched from zero.
    (tmp_path / (asset.filename + ".part")).write_bytes(os.urandom(len(data)))
    calls: list = []
    async with _server(data, calls=calls) as client:
        out = await oi.download_asset(
            asset, tmp_path / asset.filename, client=client, url="https://x/z"
        )
    assert out.read_bytes() == data
    assert "range" not in calls[0]


async def test_complete_part_published_without_request(tmp_path):
    data = os.urandom(20_000)
    asset = _asset(data)
    (tmp_path / (asset.filename + ".part")).write_bytes(data)

    def handler(request):  # pragma: no cover - must not be called
        raise AssertionError("no request expected")

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        out = await oi.download_asset(asset, tmp_path / asset.filename, client=client)
    assert out.read_bytes() == data


async def test_416_recovers_by_restarting(tmp_path):
    data = os.urandom(30_000)
    asset = _asset(data)
    (tmp_path / (asset.filename + ".part")).write_bytes(data[:1000])

    def handler(request):
        if request.headers.get("Range"):
            return httpx.Response(416)
        return httpx.Response(200, content=data)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        out = await oi.download_asset(
            asset, tmp_path / asset.filename, client=client, url="https://x/z"
        )
    assert out.read_bytes() == data


async def test_existing_verified_zip_is_reused_without_request(tmp_path):
    data = os.urandom(5_000)
    asset = _asset(data)
    (tmp_path / asset.filename).write_bytes(data)

    def handler(request):  # pragma: no cover - must not be called
        raise AssertionError("no request expected")

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        out = await oi.download_asset(asset, tmp_path / asset.filename, client=client)
    assert out.read_bytes() == data


async def test_cancel_keeps_part_for_resume(tmp_path):
    data = os.urandom(3 * (1 << 20))
    asset = _asset(data)
    cancel = asyncio.Event()

    def progress(event):
        if event.get("downloaded_bytes", 0) > 0:
            cancel.set()

    async with _server(data) as client:
        with pytest.raises(oi.InstallError) as err:
            await oi.download_asset(
                asset,
                tmp_path / asset.filename,
                client=client,
                url="https://x/z",
                progress=progress,
                cancel=cancel,
            )
    assert err.value.code == "cancelled"
    assert (tmp_path / (asset.filename + ".part")).exists()


async def test_http_error_is_not_retried(tmp_path):
    data = b"abc"
    asset = _asset(data)
    calls = []

    def handler(request):
        calls.append(1)
        return httpx.Response(404)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        with pytest.raises(oi.InstallError) as err:
            await oi.download_asset(asset, tmp_path / "x.zip", client=client, url="https://x/z")
    assert err.value.code == "http"
    assert len(calls) == 1


@pytest.mark.parametrize(
    "name",
    ["../evil.txt", "ovms/../../evil.txt", "/abs/evil.txt", "C:/evil.txt", "..\\evil.txt"],
)
def test_zip_slip_is_refused_before_writing(tmp_path, name):
    zpath = tmp_path / "evil.zip"
    zpath.write_bytes(_zip_bytes({"ovms/ok.txt": b"ok", name: b"bad"}))
    dest = tmp_path / "out"
    with pytest.raises(oi.InstallError) as err:
        oi.safe_extract(zpath, dest)
    assert err.value.code == "unsafe_zip"
    assert not (dest / "ovms" / "ok.txt").exists()  # nothing written at all
    assert not (tmp_path / "evil.txt").exists()


def test_symlink_member_refused(tmp_path):
    zpath = tmp_path / "link.zip"
    zpath.write_bytes(_zip_bytes({"ovms/ok.txt": b"ok"}, symlink="ovms/link"))
    with pytest.raises(oi.InstallError) as err:
        oi.safe_extract(zpath, tmp_path / "out")
    assert err.value.code == "unsafe_zip"


def test_corrupt_zip(tmp_path):
    zpath = tmp_path / "junk.zip"
    zpath.write_bytes(b"not a zip")
    with pytest.raises(oi.InstallError) as err:
        oi.safe_extract(zpath, tmp_path / "out")
    assert err.value.code == "bad_zip"


def _runtime_zip() -> bytes:
    return _zip_bytes(
        {
            "ovms/ovms.exe": b"MZ fake",
            "ovms/setupvars.ps1": b"# vars",
            "ovms/python/python.exe": b"MZ py",
            "ovms/python/Lib/site-packages/jinja2/__init__.py": b"",
        }
    )


async def test_install_end_to_end_and_idempotent(tmp_path, monkeypatch):
    data = _runtime_zip()
    monkeypatch.setitem(oi.ASSETS, "python_on", _asset(data, oi.ASSET))
    paths = SimpleNamespace(runtime_dir=tmp_path / "runtime")
    cfg = SimpleNamespace(ovms_version="2026.4.0", ovms_variant="python_on")
    events: list[dict] = []
    async with _server(data) as client:
        exe = await oi.install(paths, cfg, events.append, asyncio.Event(), client=client)
    assert exe == tmp_path / "runtime" / "ovms-2026.4.0" / "ovms" / "ovms.exe"
    assert exe.read_bytes() == b"MZ fake"
    assert not (tmp_path / "runtime" / "ovms-2026.4.0.tmp").exists()
    assert events[-1]["status"] == "done"
    assert [e["status"] for e in events].count("extracting") == 1
    status = oi.runtime_status(paths, cfg)
    assert status["installed"] and status["variant"] == "python_on"
    assert status["version"] == "2026.4.0" and status["variant_matches"]
    assert status["sha256"] == hashlib.sha256(data).hexdigest()
    assert isinstance(status["vcredist"], bool)

    def handler(request):  # pragma: no cover - second install must not download
        raise AssertionError("no request expected")

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        again = await oi.install(paths, cfg, lambda e: None, asyncio.Event(), client=client)
    assert again == exe


async def test_install_rejects_zip_without_exe(tmp_path, monkeypatch):
    data = _zip_bytes({"ovms/readme.txt": b"hi"})
    monkeypatch.setitem(oi.ASSETS, "python_on", _asset(data, oi.ASSET))
    paths = SimpleNamespace(runtime_dir=tmp_path / "runtime")
    cfg = SimpleNamespace(ovms_version="2026.4.0", ovms_variant="python_on")
    events: list[dict] = []
    async with _server(data) as client:
        with pytest.raises(oi.InstallError) as err:
            await oi.install(paths, cfg, events.append, asyncio.Event(), client=client)
    assert err.value.code == "bad_zip"
    assert events[-1]["status"] == "error"
    assert not (tmp_path / "runtime" / "ovms-2026.4.0").exists()


def test_status_not_installed(tmp_path):
    paths = SimpleNamespace(runtime_dir=tmp_path / "runtime")
    cfg = SimpleNamespace(ovms_version="2026.4.0", ovms_variant="python_on")
    status = oi.runtime_status(paths, cfg)
    assert status["installed"] is False
    assert status["exe"] is None and status["version"] is None


def test_detect_variant_and_adopt(tmp_path):
    ovms = tmp_path / "ovms-2026.4.0" / "ovms"
    ovms.mkdir(parents=True)
    assert oi.detect_variant(ovms) is None
    (ovms / "ovms.exe").write_bytes(b"MZ")
    assert oi.detect_variant(ovms) == "python_off"
    (ovms / "python").mkdir()
    (ovms / "python" / "python.exe").write_bytes(b"MZ")
    assert oi.detect_variant(ovms) == "python_on"
    marker = oi.adopt_existing(tmp_path)
    assert marker["variant"] == "python_on" and marker["adopted"]
    assert oi.read_runtime_marker(tmp_path)["sha256"] == oi.ASSET_SHA256


@pytest.mark.skipif(os.name != "nt", reason="VC++ runtime is Windows-only")
def test_vcredist_present_on_this_box():
    # The target laptop has VC++ x64 v14.50 (PLAN §4); CI windows runners have it too.
    assert oi.vcredist_present() is True
    assert (oi.vcredist_version() or "").startswith("v14")


@pytest.mark.skipif(os.name == "nt", reason="non-Windows behaviour")
def test_vcredist_absent_off_windows():
    assert oi.vcredist_present() is False


def test_member_target_accepts_normal_names(tmp_path):
    target = oi._member_target(tmp_path, "ovms/python/Lib/x.py")
    assert target == (tmp_path / "ovms" / "python" / "Lib" / "x.py").resolve()
    assert Path(target).is_relative_to(tmp_path.resolve())
