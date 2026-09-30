"""Live NPU smoke: real OVMS + a real model on the Intel NPU.

Skipped unless ``AICHAT_LIVE_NPU=1`` (and Windows). Uses the installed runtime and
model under ``%LOCALAPPDATA%\\AIChat`` (or ``AICHAT_HOME``)::

    $env:AICHAT_LIVE_NPU = "1"; py -3.12 -m uv run pytest -m live_npu -s

``AICHAT_LIVE_MODEL`` overrides the model (default: the WS1-validated
``OpenVINO/Qwen2.5-1.5B-Instruct-int4-ov``). Loads, answers "What is 2+2?",
runs one ``current_datetime`` tool round trip, unloads, and asserts that no
``ovms.exe`` is left running. (The idle-unload timing check belongs to the
manager's live test, WS6.)
"""

from __future__ import annotations

import json
import os
import time
from pathlib import Path

import httpx
import pytest

from aichat.runtime import compile_cache
from aichat.runtime.jobobject import find_processes
from aichat.runtime.ovms_install import OVMS_VERSION, ovms_exe, vcredist_present
from aichat.runtime.ovms_supervisor import LaunchSpec, OvmsSupervisor

pytestmark = [
    pytest.mark.live_npu,
    pytest.mark.skipif(os.environ.get("AICHAT_LIVE_NPU") != "1", reason="set AICHAT_LIVE_NPU=1"),
    pytest.mark.skipif(os.name != "nt", reason="NPU runtime is Windows-only"),
    pytest.mark.timeout(1200),
]

DEFAULT_MODEL = "OpenVINO/Qwen2.5-1.5B-Instruct-int4-ov"
NO_THINK = {"chat_template_kwargs": {"enable_thinking": False}}
TOOLS = [
    {
        "type": "function",
        "function": {
            "name": "current_datetime",
            "description": "Get the current local date, time, weekday and timezone.",
            "parameters": {"type": "object", "properties": {}, "required": []},
        },
    }
]
SYSTEM = (
    "You are AI Chat, a concise desktop assistant. Answer briefly. Use tools only when "
    "they help (current facts, web pages, dates, arithmetic)."
)


def _home() -> Path:
    env = os.environ.get("AICHAT_HOME")
    return Path(env) if env else Path(os.environ["LOCALAPPDATA"]) / "AIChat"


async def _stream(client: httpx.AsyncClient, base_url: str, body: dict) -> dict:
    body = {**body, "stream": True, "stream_options": {"include_usage": True}}
    content, calls, finish, usage = "", {}, None, {}
    t0 = time.monotonic()
    first = None
    async with client.stream("POST", f"{base_url}/chat/completions", json=body) as resp:
        assert resp.status_code == 200, await resp.aread()
        async for line in resp.aiter_lines():
            if not line.startswith("data:"):
                continue
            data = line[5:].strip()
            if data == "[DONE]":
                break
            obj = json.loads(data)
            usage = obj.get("usage") or usage
            for choice in obj.get("choices", []):
                delta = choice.get("delta") or {}
                if delta.get("content"):
                    first = first or time.monotonic()
                    content += delta["content"]
                for tc in delta.get("tool_calls") or []:
                    first = first or time.monotonic()
                    entry = calls.setdefault(
                        tc.get("index", 0), {"id": None, "name": "", "args": ""}
                    )
                    entry["id"] = tc.get("id") or entry["id"]
                    fn = tc.get("function") or {}
                    entry["name"] += fn.get("name") or ""
                    entry["args"] += fn.get("arguments") or ""
                finish = choice.get("finish_reason") or finish
    elapsed = time.monotonic() - (first or t0)
    tokens = usage.get("completion_tokens") or 0
    return {
        "content": content,
        "tool_calls": list(calls.values()),
        "finish": finish,
        "usage": usage,
        "tok_s": (tokens - 1) / elapsed if tokens > 1 and elapsed > 0 else None,
    }


async def test_live_npu_load_answer_tool_unload():
    home = _home()
    model_id = os.environ.get("AICHAT_LIVE_MODEL", DEFAULT_MODEL)
    exe = ovms_exe(home / "runtime", OVMS_VERSION)
    model_path = home / "models" / Path(model_id)
    if not exe.is_file():
        pytest.skip(f"OVMS runtime not installed at {exe}")
    if not (model_path / "openvino_model.bin").is_file():
        pytest.skip(f"model not downloaded at {model_path}")
    assert vcredist_present()
    assert not find_processes("ovms.exe"), "an ovms.exe is already running; stop it first"

    spec = LaunchSpec(
        model_id=model_id,
        model_path=model_path,
        device="NPU",
        max_prompt_len=4096,
        cache_dir=compile_cache.cache_dir_for(home / "cache", model_id, "NPU", 4096),
        tool_parser="hermes3",
        reasoning_parser="qwen3" if "qwen3" in model_id.lower() else None,
        log_path=home / "logs" / "ovms.log",
    )
    sup = OvmsSupervisor(exe, state_file=home / "state.json", load_timeout_s=900)
    phases: list[str] = []
    try:
        base_url = await sup.start(spec, lambda phase, _el: phases.append(phase))
        print(
            f"\nloaded {model_id} in {sup.load_s:.1f}s "
            f"({'first compile' if sup.first_compile else 'cached'}; expected ~{sup.expected_s:.0f}s)"
            f" on port {sup.port}; phases={sorted(set(phases))}"
        )
        async with httpx.AsyncClient(timeout=180, trust_env=False) as client:
            answer = await _stream(
                client,
                base_url,
                {
                    "model": model_id,
                    "max_tokens": 64,
                    **NO_THINK,
                    "messages": [
                        {"role": "system", "content": SYSTEM},
                        {"role": "user", "content": "What is 2+2?"},
                    ],
                },
            )
            print(f"2+2 -> {answer['content']!r} ({answer['tok_s'] or 0:.1f} tok/s)")
            assert "4" in answer["content"]
            assert answer["finish"] == "stop"

            messages = [
                {"role": "system", "content": SYSTEM},
                {"role": "user", "content": "What is today's date? Use the tool."},
            ]
            first = await _stream(
                client,
                base_url,
                {"model": model_id, "max_tokens": 128, **NO_THINK, "messages": messages,
                 "tools": TOOLS},
            )  # fmt: skip
            print(f"tool call -> {first['tool_calls']} finish={first['finish']}")
            assert first["finish"] == "tool_calls"
            call = first["tool_calls"][0]
            assert call["name"] == "current_datetime"
            json.loads(call["args"] or "{}")
            messages.append(
                {
                    "role": "assistant",
                    "content": first["content"],
                    "tool_calls": [
                        {
                            "id": call["id"],
                            "type": "function",
                            "function": {"name": call["name"], "arguments": call["args"] or "{}"},
                        }
                    ],
                }
            )
            messages.append(
                {
                    "role": "tool",
                    "tool_call_id": call["id"],
                    "content": json.dumps(
                        {"iso": "2026-09-30T23:30:00+01:00", "weekday": "Wednesday"}
                    ),
                }
            )
            final = await _stream(
                client,
                base_url,
                {"model": model_id, "max_tokens": 128, **NO_THINK, "messages": messages,
                 "tools": TOOLS},
            )  # fmt: skip
            print(f"final -> {final['content']!r} finish={final['finish']}")
            assert final["finish"] == "stop"
            assert "30" in final["content"] or "September" in final["content"]

            tok = await client.post(
                base_url.removesuffix("/v3") + "/v3/tokenize",
                json={"model": model_id, "text": "Hello world"},
            )
            assert tok.status_code == 200 and tok.json()["tokens"]
        assert compile_cache.is_compiled(spec.cache_dir, ovms_version=OVMS_VERSION)
    finally:
        await sup.aclose()
    time.sleep(1.0)
    assert not find_processes("ovms.exe"), "ovms.exe survived the unload"
    assert sup.exit_event.is_set() and not sup.crashed
