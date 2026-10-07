"""Loose background tasks, off-loop keyring reads, and the diagnostics redaction."""

from __future__ import annotations

import asyncio
import logging
import threading
from pathlib import Path

import pytest

from chatforge import doctor, secrets
from chatforge.desktop.core_loop import CoreLoop
from chatforge.llm.providers import SEED_PROVIDERS, RemoteProvider
from chatforge.runtime import manager as manager_mod


def test_register_secret_is_available_from_secrets() -> None:
    from chatforge.logging_setup import register_secret

    assert secrets.register_secret is register_secret


# --- tasks that fail are logged ---------------------------------------------------------------


def test_core_loop_create_task_logs_a_failing_task(caplog: pytest.LogCaptureFixture) -> None:
    core = CoreLoop("test-hardening").start()
    try:

        async def boom() -> None:
            raise RuntimeError("chore exploded")

        async def go() -> None:
            task = core.create_task(boom(), name="chore")
            await asyncio.wait({task})
            await asyncio.sleep(0)  # let the done-callbacks run

        with caplog.at_level(logging.ERROR, logger="chatforge.desktop.core_loop"):
            core.run(go(), timeout=5)
    finally:
        core.stop()
    assert any("chore" in r.message and "chore exploded" in r.message for r in caplog.records)


def test_core_loop_create_task_keeps_quiet_for_cancelled_and_ok(
    caplog: pytest.LogCaptureFixture,
) -> None:
    core = CoreLoop("test-hardening-quiet").start()
    try:

        async def go() -> None:
            ok = core.create_task(asyncio.sleep(0), name="fine")
            slow = core.create_task(asyncio.sleep(60), name="slow")
            await ok
            slow.cancel()
            await asyncio.wait({slow})
            await asyncio.sleep(0)

        with caplog.at_level(logging.ERROR, logger="chatforge.desktop.core_loop"):
            core.run(go(), timeout=5)
    finally:
        core.stop()
    assert not [r for r in caplog.records if r.levelno >= logging.ERROR]


async def test_manager_background_task_failures_are_logged(capsys) -> None:
    async def boom() -> None:
        raise RuntimeError("watch died")

    task = asyncio.create_task(boom(), name="ovms-watch")
    task.add_done_callback(manager_mod._log_task_failure)  # noqa: SLF001
    await asyncio.wait({task})
    await asyncio.sleep(0)
    assert "background_task_failed" in capsys.readouterr().out
    ok = asyncio.create_task(asyncio.sleep(0))
    ok.add_done_callback(manager_mod._log_task_failure)  # noqa: SLF001
    await ok


def test_manager_creates_its_watch_and_precompile_tasks_with_a_failure_logger() -> None:
    import inspect

    source = inspect.getsource(manager_mod.LocalModelManager)
    assert source.count("_log_task_failure") >= 2
    assert "add_done_callback(_log_task_failure)" in source


# --- keyring reads stay off the loop ----------------------------------------------------------------


async def test_remote_provider_reads_its_key_off_the_event_loop(monkeypatch) -> None:
    loop_thread = threading.current_thread()
    seen: list[threading.Thread] = []

    def fake_get(provider_id: str, env_name: str | None):
        seen.append(threading.current_thread())
        return "sk-thread-test-key-123456", "keyring"

    monkeypatch.setattr(secrets, "get_api_key", fake_get)
    provider = RemoteProvider(SEED_PROVIDERS["openai"])
    client = await provider.client_for("gpt-test")
    await client.aclose()
    assert seen and seen[0] is not loop_thread


async def test_unadopted_models_are_listed_off_the_event_loop(tmp_path: Path) -> None:
    from chatforge.app import App
    from chatforge.config import load_config
    from chatforge.paths import Paths

    paths = Paths.from_home(tmp_path / "home")
    paths.ensure_dirs()
    app = App(paths, load_config(paths), mode="hidden")
    loop_thread = threading.current_thread()
    seen: list[threading.Thread] = []

    class Registry:
        def unadopted(self) -> list:
            seen.append(threading.current_thread())
            return []

    app.services.registry = Registry()
    await app._adopt_unadopted()  # noqa: SLF001
    assert seen and seen[0] is not loop_thread


# --- redaction used by the diagnostics report --------------------------------------------------------------


def test_redact_config_text_keeps_names_and_hides_key_like_values() -> None:
    text = "\n".join(
        [
            'api_key = "sk-abc"',
            'api_key_env = "OPENAI_API_KEY"',
            'hf_token = "hf_x"',
            "max_tokens = 4096",
            'password = "p"',
            'Authorization = "Bearer abcdefgh12345678"',
            'base_url = "https://u:p@host.test/v1?key=zzz"',
            'note = "sk-abcdefghijklmnopqrstuvwxyz0123456789"',
            'model = "OpenVINO/Qwen3-8B-int4-ov"',
        ]
    )
    out = doctor.redact_config_text(text)
    for leaked in ('sk-abc"', "hf_x", '"p"', "abcdefgh12345678", "u:p@", "zzz", "sk-abcdefghij"):
        assert leaked not in out, leaked
    assert 'api_key_env = "OPENAI_API_KEY"' in out
    assert "max_tokens = 4096" in out
    assert 'model = "OpenVINO/Qwen3-8B-int4-ov"' in out
    assert "https://***REDACTED***@host.test/v1?***REDACTED***" in out


def test_diagnostics_report_without_a_config_file_or_logs(tmp_path: Path) -> None:
    from chatforge.config import load_config
    from chatforge.paths import Paths

    paths = Paths.from_home(tmp_path / "home")
    cfg = load_config(paths)
    paths.config_file.unlink()
    text = doctor.diagnostics_report(paths, cfg, None, [])
    assert "(no config.toml)" in text and "(none)" in text
    assert "active provider: " + cfg.chat.provider in text
