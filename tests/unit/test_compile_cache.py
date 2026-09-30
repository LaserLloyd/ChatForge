"""Compile-cache layout, marker, ETA and clear."""

from __future__ import annotations

import json
import os
import stat

import pytest

from aichat.runtime import compile_cache as cc

MODEL = "OpenVINO/Qwen2.5-1.5B-Instruct-int4-ov"


def _blob(d, name="123.blob", size=10):
    d.mkdir(parents=True, exist_ok=True)
    p = d / name
    p.write_bytes(b"x" * size)
    os.chmod(p, stat.S_IREAD)  # OVMS writes blobs read-only
    return p


def test_slug_and_layout(tmp_path):
    assert cc.model_slug("OpenVINO/Qwen3-4B-int4-ov") == "OpenVINO--Qwen3-4B-int4-ov"
    assert cc.model_slug("../../etc") == "etc"
    d = cc.cache_dir_for(tmp_path, MODEL, "npu", 4096)
    assert d == tmp_path / "ov" / "OpenVINO--Qwen2.5-1.5B-Instruct-int4-ov" / "NPU-4096"
    assert cc.compile_key(MODEL, "npu", 4096, "2026.4.0") == f"{MODEL}|NPU|4096|2026.4.0"


def test_slug_cannot_escape(tmp_path):
    for bad in ("..", "../..", "a/../../b", "..\\..\\x", "/abs/path"):
        d = cc.cache_dir_for(tmp_path, bad, "NPU", 4096)
        assert d.resolve().is_relative_to((tmp_path / "ov").resolve())


def test_not_compiled_without_marker_or_blob(tmp_path):
    d = cc.cache_dir_for(tmp_path, MODEL, "NPU", 4096)
    assert not cc.is_compiled(d)
    _blob(d)
    assert not cc.is_compiled(d)  # blob without our marker
    cc.mark_compiled(
        d, model_id=MODEL, device="NPU", max_prompt_len=4096, ovms_version="2026.4.0", load_s=44.5
    )
    assert cc.is_compiled(d)
    assert not cc.is_compiled(d, ovms_version="2027.0.0")


def test_marker_without_blob_is_cold(tmp_path):
    d = cc.cache_dir_for(tmp_path, MODEL, "NPU", 4096)
    cc.mark_compiled(
        d, model_id=MODEL, device="NPU", max_prompt_len=4096, ovms_version="2026.4.0", load_s=44.5
    )
    assert not cc.is_compiled(d)


def test_expected_load_s_progression(tmp_path):
    state = tmp_path / "state.json"
    d = cc.cache_dir_for(tmp_path, MODEL, "NPU", 4096)
    key = cc.compile_key(MODEL, "NPU", 4096, "2026.4.0")
    assert cc.expected_load_s(d, state_file=state, key=key) == cc.DEFAULT_FIRST_COMPILE_S
    _blob(d)
    cc.mark_compiled(
        d,
        model_id=MODEL,
        device="NPU",
        max_prompt_len=4096,
        ovms_version="2026.4.0",
        load_s=44.5,
        state_file=state,
    )
    # Warm cache but no warm load measured yet.
    assert cc.expected_load_s(d, state_file=state, key=key) == cc.CACHED_LOAD_S
    cc.mark_compiled(
        d,
        model_id=MODEL,
        device="NPU",
        max_prompt_len=4096,
        ovms_version="2026.4.0",
        load_s=3.2,
        state_file=state,
    )
    assert cc.expected_load_s(d, state_file=state, key=key) == pytest.approx(3.2)
    marker = cc.read_marker(d)
    assert marker["first_compile_s"] == pytest.approx(44.5)
    assert marker["last_load_s"] == pytest.approx(3.2)
    saved = json.loads(state.read_text())
    assert saved["compiled"][key]["load_s"] == pytest.approx(44.5)
    # After a clear the recorded first compile is the ETA.
    cc.clear(tmp_path, MODEL)
    assert cc.expected_load_s(d, state_file=state, key=key) == pytest.approx(44.5)


def test_update_state_preserves_foreign_keys(tmp_path):
    state = tmp_path / "state.json"
    state.write_text(json.dumps({"last_used": {"m": 1.0}, "last_unload": {"reason": "idle"}}))
    cc.update_state(state, lambda s: s.setdefault("compiled", {}).update({"k": {"load_s": 1}}))
    data = json.loads(state.read_text())
    assert data["last_used"] == {"m": 1.0}
    assert data["last_unload"] == {"reason": "idle"}
    assert data["compiled"]["k"]["load_s"] == 1


def test_corrupt_state_reads_empty(tmp_path):
    state = tmp_path / "state.json"
    state.write_text("{not json")
    assert cc.load_state(state) == {}
    assert cc.load_state(None) == {}


def test_clear_variant_and_model(tmp_path):
    a = cc.cache_dir_for(tmp_path, MODEL, "NPU", 4096)
    b = cc.cache_dir_for(tmp_path, MODEL, "NPU", 2048)
    _blob(a, size=100)
    _blob(b, size=50)
    assert cc.clear(tmp_path, MODEL, device="NPU", max_prompt_len=2048) == 50
    assert a.exists() and not b.exists()
    assert cc.clear(tmp_path, MODEL) == 100
    assert not cc.model_cache_root(tmp_path, MODEL).exists()
    assert cc.clear(tmp_path, MODEL) == 0


def test_clear_refuses_ov_root(tmp_path, monkeypatch):
    monkeypatch.setattr(cc, "model_slug", lambda _m: ".")
    with pytest.raises(ValueError):
        cc.clear(tmp_path, "anything")


def test_dir_size(tmp_path):
    _blob(tmp_path / "x", size=7)
    _blob(tmp_path / "x" / "y", size=5)
    assert cc.dir_size(tmp_path / "x") == 12
    assert cc.dir_size(tmp_path / "missing") == 0
