"""The Models card: sysreq rows, missing models, the download job with progress and cancel (no network)."""

from __future__ import annotations

import threading

from conftest import make_settings, wait_for
from forge_voice_core import fetch_job

from chatforge_voice import messages
from chatforge_voice.models_card import ModelsService

PROGRAMS = [
    {
        "id": "ffmpeg",
        "ok": False,
        "what": "decodes a clip",
        "required_for": "cloning",
        "optional": False,
        "fix": "winget install Gyan.FFmpeg",
    },
    {
        "id": "libopus",
        "ok": True,
        "what": "opus",
        "required_for": "slow links",
        "optional": True,
        "fix": "",
    },
]


def svc(job=None, free=50 * 1024**3, tmp=None):
    s = make_settings()
    return ModelsService(
        lambda: s,
        job=job or fetch_job.FetchJob(run=lambda **kw: 0),
        sysreq_check=lambda: PROGRAMS,
        models_root=lambda: tmp,
        free_bytes=lambda p: free,
    )


def test_status_reports_missing_models_in_this_addons_words(tmp_path):
    st = svc(tmp=tmp_path).status()
    assert st["ready"] is False and st["missing"]
    assert not any("dispatch-voice" in m for m in st["missing"])
    assert st["download_mb"] == messages.DOWNLOAD_SIZE_MB and st["ram_gb"] == 3


def test_status_carries_the_attribution_and_licences(tmp_path):
    st = svc(tmp=tmp_path).status()
    assert "CC-BY-4.0" in st["attribution"] and "NVIDIA" in st["attribution"]
    names = {lic["name"]: lic["licence"] for lic in st["licences"]}
    assert names["Parakeet TDT 0.6B v2 (int8)"] == "CC-BY-4.0"


def test_sysreq_rows_are_informational_for_push_to_talk(tmp_path):
    rows = {r["id"]: r for r in svc(tmp=tmp_path).status()["programs"]}
    assert (
        rows["ffmpeg"]["found"] is False and rows["ffmpeg"]["fix"] == "winget install Gyan.FFmpeg"
    )
    assert all(r["needed_for_push_to_talk"] is False for r in rows.values())


def test_the_download_asks_first(tmp_path):
    res = svc(tmp=tmp_path).start()
    assert res["ok"] is False and res["code"] == "confirm_required"
    assert "1.4 GB" in res["message"] and "CC-BY-4.0" in res["message"]


def test_the_download_refuses_when_the_disk_is_nearly_full(tmp_path):
    res = svc(free=1 * 1024**3, tmp=tmp_path / "not" / "there" / "yet").start(confirm=True)
    assert res["code"] == "disk_space" and "1.0 GB is free" in res["message"]


def test_download_progress_and_finish(tmp_path):
    gate = threading.Event()

    def body(*, only, force, verify, stock, echo, progress, cancel):
        progress(
            {
                "phase": "speech",
                "file": "silero_vad.onnx",
                "index": 0,
                "total": 2,
                "bytes": 1,
                "bytes_total": 2,
                "weight_mb": 700,
            }
        )
        gate.wait(5)
        progress(
            {
                "phase": "speech",
                "file": "silero_vad.onnx",
                "index": 1,
                "total": 2,
                "weight_mb": 700,
                "done": True,
            }
        )
        return 0

    job = fetch_job.FetchJob(run=body)
    s = svc(job=job, tmp=tmp_path)
    assert s.start(confirm=True) == {"ok": True}
    assert wait_for(lambda: job.status()["percent"] > 0)
    st = s.status()["job"]
    assert st["state"] == "running" and st["cancellable"] and st["file"] == "silero_vad.onnx"
    assert s.start(confirm=True)["code"] == "running"  # one job at a time
    gate.set()
    job.join(5)
    assert s.status()["job"]["state"] == "done" and s.status()["job"]["percent"] == 100.0


def test_download_can_be_cancelled(tmp_path):
    from forge_voice_core import download

    def body(*, cancel, **kw):
        assert cancel.wait(5)
        raise download.Cancelled()

    job = fetch_job.FetchJob(run=body)
    s = svc(job=job, tmp=tmp_path)
    s.start(confirm=True)
    assert s.cancel() == {"ok": True}
    job.join(5)
    assert job.status()["state"] == "cancelled"
    assert s.cancel() == {"ok": False}  # nothing running any more


def test_a_failed_download_says_so(tmp_path):
    def body(**kw):
        raise OSError("connection reset")

    job = fetch_job.FetchJob(run=body)
    s = svc(job=job, tmp=tmp_path)
    s.start(confirm=True)
    job.join(5)
    st = s.status()["job"]
    assert st["state"] == "failed" and "connection reset" in st["error"]
