"""OVMS supervisor: argv golden, env, readiness, crash detection, stop/kill-tree.

Process tests use the stdlib fake in tests/fakes/ovms_child.py.
"""

from __future__ import annotations

import asyncio
import json
import os
import time
from pathlib import Path

import httpx
import psutil
import pytest

from aichat.runtime import compile_cache
from aichat.runtime import ovms_supervisor as sup_mod
from aichat.runtime.jobobject import describe_exit_code, tracked_pids
from aichat.runtime.ovms_supervisor import (
    LaunchSpec,
    OvmsError,
    OvmsSupervisor,
    build_argv,
    launch_hash,
    ovms_env,
    parse_model_state,
    redact_argv,
    rotate_log,
)
from aichat.runtime.ports import pick_free_port
from tests.fakes import ovms_child

MODEL = "OpenVINO/Qwen2.5-1.5B-Instruct-int4-ov"


def _spec(tmp_path: Path, **kw) -> LaunchSpec:
    base = dict(
        model_id=MODEL,
        model_path=tmp_path / "models" / "OpenVINO" / "Qwen2.5-1.5B-Instruct-int4-ov",
        device="NPU",
        max_prompt_len=4096,
        cache_dir=compile_cache.cache_dir_for(tmp_path / "cache", MODEL, "NPU", 4096),
        tool_parser="hermes3",
        reasoning_parser=None,
        port=0,
        extra_args=["--fake_load_s", "0.3"],
        log_path=tmp_path / "logs" / "ovms.log",
    )
    base.update(kw)
    spec = LaunchSpec(**base)
    Path(spec.model_path).mkdir(parents=True, exist_ok=True)
    return spec


def _supervisor(tmp_path: Path, **kw) -> OvmsSupervisor:
    exe_dir = tmp_path / "runtime" / "ovms-2026.4.0" / "ovms"
    exe_dir.mkdir(parents=True, exist_ok=True)  # no python/ subdir: no PYTHONHOME for the fake
    kw.setdefault("state_file", tmp_path / "state.json")
    kw.setdefault("poll_interval_s", 0.05)
    kw.setdefault("tick_interval_s", 0.0)
    kw.setdefault("load_timeout_s", 30.0)
    kw.setdefault("port_start", 18700)
    return OvmsSupervisor(exe_dir / "ovms.exe", command_prefix=ovms_child.command_prefix(), **kw)


# ---------------------------------------------------------------------------
# Pure helpers
# ---------------------------------------------------------------------------


def test_build_argv_golden_npu():
    spec = LaunchSpec(
        model_id="OpenVINO/Qwen3-4B-int4-ov",
        model_path=Path(r"C:\AIChat\models\OpenVINO\Qwen3-4B-int4-ov"),
        device="npu",
        max_prompt_len=4096,
        cache_dir=Path(r"C:\AIChat\cache\ov\OpenVINO--Qwen3-4B-int4-ov\NPU-4096"),
        tool_parser="hermes3",
        reasoning_parser="qwen3",
        port=18611,
    )
    assert build_argv(Path(r"C:\rt\ovms.exe"), spec) == [
        str(Path(r"C:\rt\ovms.exe")),
        "--rest_port", "18611",
        "--rest_bind_address", "127.0.0.1",
        "--model_name", "OpenVINO/Qwen3-4B-int4-ov",
        "--model_path", str(Path(r"C:\AIChat\models\OpenVINO\Qwen3-4B-int4-ov")),
        "--task", "text_generation",
        "--target_device", "NPU",
        "--max_prompt_len", "4096",
        "--cache_dir", str(Path(r"C:\AIChat\cache\ov\OpenVINO--Qwen3-4B-int4-ov\NPU-4096")),
        "--tool_parser", "hermes3",
        "--reasoning_parser", "qwen3",
        "--log_level", "INFO",
    ]  # fmt: skip


def test_build_argv_cpu_omits_npu_only_flag_and_passes_extras(tmp_path):
    spec = _spec(
        tmp_path,
        device="CPU",
        port=1,
        tool_parser=None,
        enable_prefix_caching=False,
        extra_args=["--plugin_config", '{"A":"B"}'],
    )
    argv = build_argv(Path("ovms.exe"), spec)
    assert "--max_prompt_len" not in argv
    assert "--tool_parser" not in argv and "--reasoning_parser" not in argv
    assert argv[argv.index("--enable_prefix_caching") + 1] == "false"
    assert argv[-2:] == ["--plugin_config", '{"A":"B"}']
    assert "--port" not in argv  # never a gRPC port


def test_build_argv_requires_port(tmp_path):
    with pytest.raises(ValueError):
        build_argv(Path("ovms.exe"), _spec(tmp_path, port=0))


def test_launch_hash_tracks_compile_relevant_fields(tmp_path):
    spec = _spec(tmp_path)
    h = launch_hash(spec)
    from dataclasses import replace

    assert launch_hash(replace(spec, port=1234, log_path=tmp_path / "x.log")) == h
    for changed in (
        replace(spec, device="GPU"),
        replace(spec, max_prompt_len=2048),
        replace(spec, reasoning_parser="qwen3"),
        replace(spec, cache_dir=tmp_path / "other"),
        replace(spec, extra_args=["--plugin_config", "{}"]),
    ):
        assert launch_hash(changed) != h
    assert launch_hash(spec, variant="python_off") != h
    assert launch_hash(spec, ovms_version="2027.0.0") != h


def test_ovms_env_mirrors_setupvars_and_strips_venv(tmp_path):
    ovms = tmp_path / "ovms"
    (ovms / "python" / "Scripts").mkdir(parents=True)
    (ovms / "espeak-ng-data").mkdir()
    venv = tmp_path / "venv"
    # OS-native entries: a "C:\\Windows" literal splits on the ":" pathsep of the Linux CI leg.
    windows, tools = str(tmp_path / "Windows"), str(tmp_path / "Tools")
    base = {
        "Path": os.pathsep.join([str(venv / "Scripts"), windows, tools]),
        "VIRTUAL_ENV": str(venv),
        "PYTHONHOME": "C:\\wrong",
        "PYTHONPATH": "C:\\wrong\\lib",
        "API_KEY": "secret-value",
        "USERPROFILE": "C:\\Users\\me",
    }
    env = ovms_env(ovms, base)
    assert env["OVMS_DIR"] == str(ovms)
    assert env["PYTHONHOME"] == str(ovms / "python")
    assert env["SCRIPTS"] == str(ovms / "python" / "Scripts")
    assert env["ESPEAK_DATA_PATH"] == str(ovms / "espeak-ng-data")
    parts = env["PATH"].split(os.pathsep)
    assert parts[:3] == [str(ovms), str(ovms / "python"), str(ovms / "python" / "Scripts")]
    assert parts[3:] == [windows, tools]  # venv Scripts removed
    for gone in ("VIRTUAL_ENV", "PYTHONPATH", "API_KEY", "Path"):
        assert gone not in env
    assert env["USERPROFILE"] == "C:\\Users\\me"
    assert "PYTHONPATH" not in env  # setupvars never sets it; the bundled python uses ._pth


def test_ovms_env_python_off_appends_dir(tmp_path):
    ovms = tmp_path / "ovms"
    ovms.mkdir()
    windows = str(tmp_path / "Windows")  # OS-native: no ":" on the Linux CI leg
    env = ovms_env(ovms, {"PATH": windows, "PYTHONHOME": "C:\\x"})
    assert env["PATH"].split(os.pathsep) == [windows, str(ovms)]
    assert "PYTHONHOME" not in env and "ESPEAK_DATA_PATH" not in env


def test_parse_model_state():
    body = {
        MODEL: {
            "model_version_status": [
                {"version": "1", "state": "LOADING"},
                {"version": "2", "state": "AVAILABLE"},
            ]
        }
    }
    assert parse_model_state(body, MODEL) == "AVAILABLE"
    assert parse_model_state({}, MODEL) is None
    assert parse_model_state({"other": {}}, MODEL) is None
    assert parse_model_state([], MODEL) is None


def test_redact_argv():
    argv = [
        r"C:\Users\me\ovms.exe",
        "--model_path",
        "/home/me/models/x",
        "--api_key_file",
        "k.txt",
        "--rest_port",
        "1",
    ]
    assert redact_argv(argv) == [
        "ovms.exe",
        "--model_path",
        "x",
        "--api_key_file",
        "<redacted>",
        "--rest_port",
        "1",
    ]


def test_rotate_log(tmp_path):
    log = tmp_path / "ovms.log"
    log.write_text("x" * 100)
    (tmp_path / "ovms.log.1").write_text("old1")
    rotate_log(log, max_bytes=50, backups=2)
    assert not log.exists()
    assert (tmp_path / "ovms.log.1").read_text() == "x" * 100
    assert (tmp_path / "ovms.log.2").read_text() == "old1"
    log.write_text("small")
    rotate_log(log, max_bytes=50, backups=2)
    assert log.read_text() == "small"


def test_describe_exit_code_windows_status():
    fields = describe_exit_code(3221226505)
    assert fields["exit_code_hex"] == "0xC0000409"
    assert fields["exit_status"] == "STATUS_STACK_BUFFER_OVERRUN"
    assert describe_exit_code(None)["exit_code_unavailable"] is True


# ---------------------------------------------------------------------------
# Process behaviour (fake child)
# ---------------------------------------------------------------------------


async def test_start_ready_then_stop(tmp_path):
    sup = _supervisor(tmp_path)
    # Long enough that the 503 "loading" window is always observed, even when the
    # first poll's refused connect costs Windows ~1 s of SYN retries.
    spec = _spec(tmp_path, extra_args=["--fake_load_s", "2.0"])
    ticks: list[tuple[str, float]] = []
    try:
        base_url = await sup.start(spec, lambda phase, el: ticks.append((phase, el)))
        assert base_url == f"http://127.0.0.1:{sup.port}/v3"
        assert sup.port >= 18700 and sup.is_alive() and sup.state == "ready"
        phases = [p for p, _ in ticks]
        assert phases[-1] == "ready"
        assert "compiling" in phases  # cold cache on the first load
        assert sup.first_compile is True
        assert sup.pid in tracked_pids()
        async with httpx.AsyncClient() as client:
            models = (await client.get(f"{base_url}/models")).json()
        assert models["data"][0]["id"] == MODEL
        assert any("AVAILABLE" in line for line in sup.stderr_tail())
        assert compile_cache.read_marker(spec.cache_dir)["model_id"] == MODEL
        state = json.loads((tmp_path / "state.json").read_text())
        assert state["launch"][MODEL]["hash"] == launch_hash(spec)
        log_text = (tmp_path / "logs" / "ovms.log").read_text(encoding="utf-8")
        assert log_text.startswith("=== aichat launch")
        assert await sup.health() is True
        pid = sup.pid
    finally:
        await sup.stop()
    assert sup.state == "stopped"
    assert sup.exit_event.is_set() and not sup.crashed
    assert not psutil.pid_exists(pid) or not sup.is_alive()
    assert pid not in tracked_pids()
    await sup.aclose()


async def test_readiness_transitions_starting_then_loading_when_warm(tmp_path):
    sup = _supervisor(tmp_path)
    spec = _spec(tmp_path)
    # Pretend a previous compile left a warm cache.
    (Path(spec.cache_dir)).mkdir(parents=True, exist_ok=True)
    (Path(spec.cache_dir) / "1.blob").write_bytes(b"x")
    compile_cache.mark_compiled(
        spec.cache_dir,
        model_id=MODEL,
        device="NPU",
        max_prompt_len=4096,
        ovms_version="2026.4.0",
        load_s=40,
    )
    ticks: list[str] = []
    spec = _spec(tmp_path, extra_args=["--fake_rest_delay_s", "2.5", "--fake_load_s", "4.0"])
    try:
        await sup.start(spec, lambda phase, _el: ticks.append(phase))
    finally:
        await sup.aclose()
    assert sup.first_compile is False
    assert ticks.index("starting") < ticks.index("loading") < ticks.index("ready")
    assert "compiling" not in ticks
    assert sup.expected_s == compile_cache.CACHED_LOAD_S



async def test_marker_for_other_compile_settings_reads_as_a_first_compile(tmp_path):
    from aichat.runtime.ovms_supervisor import spec_compile_hash

    sup = _supervisor(tmp_path)
    spec = _spec(tmp_path)
    Path(spec.cache_dir).mkdir(parents=True, exist_ok=True)
    (Path(spec.cache_dir) / "1.blob").write_bytes(b"x")
    compile_cache.mark_compiled(
        spec.cache_dir,
        model_id=MODEL,
        device="NPU",
        max_prompt_len=4096,
        ovms_version="2026.4.0",
        load_s=40,
        compile_hash="0123456789abcdef",  # e.g. written with another --plugin_config
    )
    try:
        await sup.start(spec)
    finally:
        await sup.aclose()
    assert sup.first_compile is True  # the blob for these settings does not exist yet
    marker = compile_cache.read_marker(spec.cache_dir)
    assert marker["compile_hash"] == spec_compile_hash(spec)
    assert "cached_load_s" not in marker  # this load was the compile
    assert compile_cache.is_compiled(spec.cache_dir, compile_hash=spec_compile_hash(spec))

async def test_child_crash_while_ready_sets_exit_event(tmp_path):
    sup = _supervisor(tmp_path)
    spec = _spec(tmp_path, extra_args=["--fake_load_s", "0.1", "--fake_crash_after_s", "0.5"])
    try:
        await sup.start(spec)
        await asyncio.wait_for(sup.exit_event.wait(), 15)
        assert sup.crashed is True
        assert sup.state == "exited"
        assert not sup.is_alive()
        if os.name == "nt":
            assert sup.exit_code == 3221226505
        assert "fake crash" in (sup.last_error or "")
    finally:
        await sup.aclose()


async def test_load_failure_raises_with_tail(tmp_path):
    sup = _supervisor(tmp_path)
    spec = _spec(tmp_path, extra_args=["--fake_load_s", "0.1", "--fake_fail_load"])
    with pytest.raises(OvmsError) as err:
        await sup.start(spec)
    assert err.value.code == "model_failed"
    assert "LOADING_PRECONDITION_FAILED" in err.value.message
    assert sup.state == "failed" and not sup.is_alive()
    await sup.aclose()


async def test_explicit_busy_port_fails_with_port_code(tmp_path):
    import socket

    holder = socket.socket()
    if os.name == "nt":
        holder.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
    holder.bind(("127.0.0.1", 0))
    holder.listen()
    busy = holder.getsockname()[1]
    sup = _supervisor(tmp_path)
    try:
        with pytest.raises(OvmsError) as err:
            await sup.start(_spec(tmp_path, port=busy))
        assert err.value.code == "port"
    finally:
        holder.close()
        await sup.aclose()


async def test_auto_port_retries_once_after_bind_failure(tmp_path, monkeypatch):
    import socket

    holder = socket.socket()
    if os.name == "nt":
        holder.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
    holder.bind(("127.0.0.1", 0))
    holder.listen()
    busy = holder.getsockname()[1]
    calls: list[set] = []

    def fake_pick(start, span=100, *, host="127.0.0.1", exclude=()):
        calls.append(set(exclude))
        return busy if len(calls) == 1 else pick_free_port(18750, exclude=exclude)

    monkeypatch.setattr(sup_mod, "pick_free_port", fake_pick)
    sup = _supervisor(tmp_path)
    try:
        await sup.start(_spec(tmp_path))
        assert sup.port != busy
        assert calls[1] == {busy}
    finally:
        holder.close()
        await sup.aclose()


async def test_stop_kills_whole_tree(tmp_path):
    sup = _supervisor(tmp_path)
    pidfile = tmp_path / "pids.txt"
    spec = _spec(
        tmp_path,
        extra_args=["--fake_load_s", "0.1", "--fake_grandchild", "--fake_pidfile", str(pidfile)],
    )
    await sup.start(spec)
    child, grandchild = (int(p) for p in pidfile.read_text().split())
    assert psutil.pid_exists(grandchild)
    await sup.stop(timeout_s=5)
    deadline = time.monotonic() + 10
    while time.monotonic() < deadline and (
        psutil.pid_exists(child) or psutil.pid_exists(grandchild)
    ):
        await asyncio.sleep(0.1)
    for pid in (child, grandchild):
        if psutil.pid_exists(pid):
            assert psutil.Process(pid).status() == psutil.STATUS_ZOMBIE
    assert sup.exit_event.is_set() and not sup.crashed
    await sup.aclose()


async def test_timeout_kills_child(tmp_path):
    sup = _supervisor(tmp_path, load_timeout_s=1.0)
    with pytest.raises(OvmsError) as err:
        await sup.start(_spec(tmp_path, extra_args=["--fake_load_s", "30"]))
    assert err.value.code == "timeout"
    assert not sup.is_alive()
    await sup.aclose()


async def test_cancel_during_start_kills_child(tmp_path):
    sup = _supervisor(tmp_path)
    task = asyncio.create_task(sup.start(_spec(tmp_path, extra_args=["--fake_load_s", "30"])))
    for _ in range(100):
        await asyncio.sleep(0.05)
        if sup.pid:
            break
    pid = sup.pid
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert not sup.is_alive()
    assert pid is not None and pid not in tracked_pids()
    await sup.aclose()


async def test_restart_replaces_running_child(tmp_path):
    sup = _supervisor(tmp_path)
    try:
        await sup.start(_spec(tmp_path))
        first = sup.pid
        await sup.start(_spec(tmp_path))
        assert sup.pid != first and sup.is_alive()
        assert not psutil.pid_exists(first) or psutil.Process(first).status() == "zombie"
    finally:
        await sup.aclose()


async def test_stale_graph_removed_only_when_hash_changes(tmp_path):
    sup = _supervisor(tmp_path)
    spec = _spec(tmp_path)
    graph = Path(spec.model_path) / "graph.pbtxt"
    graph.write_text("stale")
    try:
        await sup.start(spec)
        assert sup.graph_removed and not graph.exists()
        await sup.stop()
        graph.write_text("same-hash graph")  # hash recorded now matches this spec
        await sup.start(spec)
        assert not sup.graph_removed and graph.exists()
        await sup.stop()
        from dataclasses import replace

        await sup.start(replace(spec, max_prompt_len=2048))
        assert sup.graph_removed and not graph.exists()
    finally:
        await sup.aclose()


async def test_fake_child_speaks_the_ovms_chat_protocol(tmp_path):
    """Guards the contract other workstreams test against (see ovms_child docstring)."""
    record = tmp_path / "requests.jsonl"
    sup = _supervisor(tmp_path)
    spec = _spec(tmp_path, extra_args=["--fake_load_s", "0.1", "--fake_record", str(record)])
    tools = [{"type": "function", "function": {"name": "current_datetime", "parameters": {}}}]
    try:
        base = await sup.start(spec)
        async with httpx.AsyncClient(timeout=10) as client:
            body = {
                "model": MODEL,
                "stream": True,
                "stream_options": {"include_usage": True},
                "tools": tools,
                "chat_template_kwargs": {"enable_thinking": False},
                "messages": [{"role": "user", "content": "What's the date today?"}],
            }
            chunks = []
            async with client.stream("POST", f"{base}/chat/completions", json=body) as resp:
                async for line in resp.aiter_lines():
                    if line.startswith("data:"):
                        chunks.append(line[5:].strip())
            assert chunks[-1] == "[DONE]"
            objs = [json.loads(c) for c in chunks[:-1]]
            first_tc = objs[0]["choices"][0]["delta"]["tool_calls"][0]
            assert first_tc["function"] == {"name": "current_datetime"} and first_tc["id"]
            assert objs[1]["choices"][0]["delta"]["tool_calls"][0]["function"] == {
                "arguments": "{}"
            }
            assert objs[2]["choices"][0]["finish_reason"] == "tool_calls"
            assert objs[3]["choices"] == [] and objs[3]["usage"]["prompt_tokens"] > 0

            plain = await client.post(
                f"{base}/chat/completions",
                json={"model": MODEL, "messages": [{"role": "user", "content": "What is 2+2?"}]},
            )
            assert plain.json()["choices"][0]["message"]["content"] == "4"
            wrong = await client.post(
                f"{base}/chat/completions", json={"model": "nope", "messages": [{"role": "user"}]}
            )
            assert wrong.status_code == 404
            over = await client.post(
                f"{base}/chat/completions",
                json={"model": MODEL, "messages": [{"role": "user", "content": "x" * 20000}]},
            )
            assert over.status_code == 400 and "maximum allowed length" in over.text
            tok = await client.post(f"{base}/tokenize", json={"model": MODEL, "text": "a b c"})
            assert len(tok.json()["tokens"]) == 3
        recorded = [json.loads(x) for x in record.read_text().splitlines()]
        assert recorded[0]["chat_template_kwargs"] == {"enable_thinking": False}
    finally:
        await sup.aclose()


async def test_not_installed(tmp_path):
    sup = OvmsSupervisor(tmp_path / "missing" / "ovms.exe")
    with pytest.raises(OvmsError) as err:
        await sup.start(_spec(tmp_path))
    assert err.value.code == "not_installed"


@pytest.mark.skipif(os.name != "nt", reason="job objects are Windows-only")
async def test_child_is_in_kill_on_close_job(tmp_path):
    import win32api
    import win32con
    import win32job

    sup = _supervisor(tmp_path)
    try:
        await sup.start(_spec(tmp_path))
        handle = win32api.OpenProcess(win32con.PROCESS_QUERY_LIMITED_INFORMATION, False, sup.pid)
        try:
            assert win32job.IsProcessInJob(handle, None)
        finally:
            handle.Close()
        pid = sup.pid
    finally:
        await sup.aclose()
    assert not psutil.pid_exists(pid) or psutil.Process(pid).status() == psutil.STATUS_ZOMBIE
