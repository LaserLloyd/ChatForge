"""The ``Api`` surface (exactly the contract), no key echo, reply shapes."""

from __future__ import annotations

import inspect
import json
import re
from pathlib import Path

import pytest

from chatforge.config import load_config
from chatforge.desktop.bridge import (
    CONTRACT_METHODS,
    Api,
    Services,
    download_view,
    error_payload,
    runtime_install_view,
    ui_config,
)
from chatforge.desktop.core_loop import CoreLoop
from chatforge.desktop.events import EventSink
from chatforge.paths import Paths

# Every method dev-mock.js implements: `async name(` inside makeApi().
_MOCK = Path(__file__).resolve().parents[2] / "src/chatforge/web/static/js/dev-mock.js"

SECRET = "sk-test-secret-value-0123456789"


def _mock_methods() -> set[str]:
    text = _MOCK.read_text(encoding="utf-8")
    body = text[text.index("function makeApi()") : text.index("export function install")]
    return set(re.findall(r"^\s{4}async (\w+)\(", body, re.M))


@pytest.fixture
def events() -> tuple[EventSink, list[dict]]:
    delivered: list[str] = []
    sink = EventSink(deliver=delivered.append, coalesce_ms=0)

    def drain() -> list[dict]:
        out: list[dict] = []
        while not sink._queue.empty():  # noqa: SLF001
            out.append(sink._queue.get_nowait())  # noqa: SLF001
        return out

    sink.drain = drain  # type: ignore[attr-defined]
    return sink, delivered  # type: ignore[return-value]


@pytest.fixture
def loop():
    core = CoreLoop("test-core").start()
    yield core
    core.stop()


@pytest.fixture
def services(tmp_path: Path, fake_keyring, events, monkeypatch) -> Services:
    monkeypatch.delenv("MINIMAX_API_KEY", raising=False)
    paths = Paths.from_home(tmp_path / "home")
    paths.ensure_dirs()
    cfg = load_config(paths)
    s = Services(paths, cfg)
    s.events = events[0]
    from chatforge.llm.providers import ProviderRegistry

    s.providers = ProviderRegistry(cfg.providers, settings=lambda: s.config)
    return s


@pytest.fixture
def api(services: Services) -> Api:
    return Api(services)


class FakePopup:
    def __init__(self) -> None:
        self.hidden = 0
        self.settings_noted = 0
        self.resizes: list[tuple] = []
        #: What end_resize answers: the new logical size, or None (nothing changed).
        self.end_size: tuple[int, int] | None = None
        self.reset_to: list[tuple[int, int]] = []

    def begin_resize(self, edge: str, grab_x: int, grab_y: int, *, follow: bool) -> bool:
        self.resizes.append(("begin", edge, grab_x, grab_y, follow))
        return True

    def drag_resize(self, dx: int, dy: int) -> bool:
        self.resizes.append(("drag", dx, dy))
        return True

    def end_resize(self) -> tuple[int, int] | None:
        self.resizes.append(("end",))
        return self.end_size

    def reset_size(self, width: int, height: int) -> bool:
        self.reset_to.append((width, height))
        return True

    def hide_from_js(self, *, blur: bool = False) -> None:
        self.hidden += 1
        self.blurred = blur

    def note_settings_opening(self) -> None:
        self.settings_noted += 1


# --- surface ---------------------------------------------------------------------------


def test_public_surface_is_exactly_the_contract() -> None:
    public = {name for name in dir(Api) if not name.startswith("_")}
    assert public == set(CONTRACT_METHODS)
    for name in CONTRACT_METHODS:
        assert callable(getattr(Api, name)), name


def test_instance_has_no_public_attributes(api: Api) -> None:
    public = {name for name in vars(api) if not name.startswith("_")}
    assert public == set()


def test_contract_matches_dev_mock() -> None:
    assert _mock_methods() == set(CONTRACT_METHODS)


def test_methods_keep_real_signatures_for_pywebview() -> None:
    # pywebview builds the JS API from the argument names, so no *args wrappers.
    params = {
        name: [p for p in inspect.signature(getattr(Api, name)).parameters if p != "self"]
        for name in CONTRACT_METHODS
    }
    assert params["send_message"] == ["text", "attachment_ids", "action_id"]
    assert params["attach_files"] == []
    assert params["attach_data"] == ["name", "base64_data"]
    assert params["remove_attachment"] == ["attachment_id"]
    assert params["open_document"] == ["path"]
    assert params["reveal_document"] == ["path"]
    assert params["save_document"] == ["path"]
    assert params["select_model"] == ["provider_id", "model_id"]
    assert params["save_api_key"] == ["provider_id", "key"]
    assert params["test_provider"] == ["provider_id", "key_or_null", "model_or_null"]
    assert params["search_models"] == ["query", "author_or_null"]
    assert params["get_logs"] == ["n", "level"]
    assert params["start_resize"] == ["edge", "grab_x", "grab_y", "follow"]
    assert params["drag_resize"] == ["dx", "dy"]
    assert params["end_resize"] == []
    assert params["reset_popup_size"] == []
    for name, names in params.items():
        assert "args" not in names and "kwargs" not in names, name


# --- keys ----------------------------------------------------------------------------------


def test_save_api_key_never_returns_the_key(api: Api, fake_keyring, events) -> None:
    reply = api.save_api_key("minimax", SECRET)
    assert reply["ok"] is True
    assert reply["key"]["source"] == "keyring"
    assert SECRET not in json.dumps(reply)
    assert fake_keyring.get_password("ChatForge", "minimax") == SECRET
    evts = events[0].drain()
    assert [e["type"] for e in evts] == ["key.status"]
    assert evts[0]["provider_id"] == "minimax" and evts[0]["source"] == "keyring"
    assert SECRET not in json.dumps(evts)


def test_save_api_key_rejects_bad_keys_without_echo(api: Api) -> None:
    for bad in ("", "  ", "a\nb", "x" * 600):
        reply = api.save_api_key("minimax", bad)
        assert reply["ok"] is False
        assert reply["error"]["code"] == "invalid_key"
        assert bad.strip() not in json.dumps(reply) or not bad.strip()
    assert api.save_api_key("nope", SECRET)["error"]["code"] == "not_found"
    assert api.save_api_key("local-npu", SECRET)["ok"] is False


def test_remove_api_key(api: Api, fake_keyring) -> None:
    api.save_api_key("minimax", SECRET)
    reply = api.remove_api_key("minimax")
    assert reply["ok"] and reply["removed"] is True
    assert reply["key"]["source"] == "none"
    assert fake_keyring.get_password("ChatForge", "minimax") is None


def test_test_provider_uses_typed_key_transiently(
    api: Api, services: Services, loop, fake_keyring, monkeypatch
) -> None:
    services.loop = loop
    seen: dict = {}

    async def fake_probe(spec, api_key, model, **_kw):
        seen["key"] = api_key
        seen["model"] = model
        return {
            "ok": True,
            "models": ["m"],
            "latency_s": 0.1,
            "error": None,
            "hint": None,
            "code": None,
        }

    monkeypatch.setattr("chatforge.llm.probe.test_provider", fake_probe)
    reply = api.test_provider("minimax", SECRET, None)
    assert reply["ok"] is True and reply["models"] == ["m"]
    assert seen["key"] == SECRET
    assert SECRET not in json.dumps(reply)
    assert fake_keyring.get_password("ChatForge", "minimax") is None  # not stored

    # No typed key: the saved one is resolved.
    api.save_api_key("minimax", SECRET + "saved")
    api.test_provider("minimax", None, "MiniMax-M3")
    assert seen["key"] == SECRET + "saved" and seen["model"] == "MiniMax-M3"


def test_test_provider_failure_shape(api: Api, services: Services, loop, monkeypatch) -> None:
    services.loop = loop

    async def fake_probe(spec, api_key, model, **_kw):
        return {
            "ok": False,
            "models": [],
            "latency_s": 0,
            "error": "bad",
            "hint": "h",
            "code": "region_or_key",
        }

    monkeypatch.setattr("chatforge.llm.probe.test_provider", fake_probe)
    reply = api.test_provider("minimax", None, None)
    assert reply == {
        "ok": False,
        "models": [],
        "latency_s": 0,
        "error": "bad",
        "hint": "h",
        "code": "region_or_key",
    }


# --- state and settings ---------------------------------------------------------------------


def test_get_state_shape(api: Api, services: Services) -> None:
    st = api.get_state()
    assert st["ok"] is True
    assert set(st) == {
        "ok",
        "config",
        "providers",
        "selected",
        "recommended_model",
        "runtime",
        "conversation",
        "limits",
        "theme",
    }
    assert st["recommended_model"] is None  # no catalog loaded in this fixture
    assert set(st["config"]) == {"chat", "ui", "local", "quick_actions"}
    assert [a["id"] for a in st["config"]["quick_actions"]][:4] == [
        "proof",
        "improve",
        "check",
        "insight",
    ]
    assert set(st["config"]["quick_actions"][0]) == {"id", "label", "hint", "tools"}
    assert set(st["config"]["chat"]) == {
        "provider",
        "model",
        "show_reasoning",
        "max_prompt_chars",
        "recent_models",
    }
    assert st["config"]["chat"]["recent_models"] == []
    assert st["selected"] == {
        "provider": services.config.chat.provider,
        "model": services.config.chat.model,
    }
    assert st["limits"] == {"max_prompt_chars": services.config.chat.max_prompt_chars}
    assert st["theme"] == "laserlloyd"
    assert st["conversation"] == []
    ids = [p["id"] for p in st["providers"]]
    assert ids[0] == "local-npu" and "minimax" in ids
    for p in st["providers"]:
        assert set(p) == {
            "id",
            "display_name",
            "kind",
            "models",
            "default_model",
            "region",
            "base_url",
            "builtin",
            "docs_url",
            "key",
            "vision",
        }
        assert set(p["key"]) >= {"source", "env_name", "env_overrides_saved"}
    rt = st["runtime"]
    assert set(rt) >= {
        "state",
        "model_id",
        "device",
        "elapsed_s",
        "expected_s",
        "first_compile",
        "idle_timeout_s",
        "unload_at",
        "error",
    }
    assert rt["state"] in ("unloaded", "not_installed")


def test_get_state_reports_the_catalogs_recommended_model(api: Api, services: Services) -> None:
    from chatforge.models.catalog import Catalog

    services.catalog = Catalog.load()
    expected = services.catalog.recommended()
    assert expected is not None
    assert api.get_state()["recommended_model"] == expected.id


def test_update_settings_persists_and_reports_restart(api: Api, services: Services, events) -> None:
    reply = api.update_settings({"local": {"device": "GPU", "idle_unload_minutes": 3}})
    assert reply["ok"] is True and reply["errors"] == {}
    assert reply["restart_required"] == ["local.device"]
    assert reply["config"]["local"]["device"] == "GPU"
    assert services.config.local.device == "GPU"
    assert 'device = "GPU"' in services.paths.config_file.read_text(encoding="utf-8")
    evts = events[0].drain()
    assert evts[-1]["type"] == "settings.changed"
    assert evts[-1]["config"] == ui_config(services.config)


def test_update_settings_invalid_reports_field_errors(api: Api, services: Services) -> None:
    reply = api.update_settings({"local": {"device": "TPU"}})
    assert reply["ok"] is False
    assert "local.device" in reply["errors"]
    assert reply["error"]["code"] == "invalid_config"
    assert services.config.local.device == "NPU"
    reply = api.update_settings({"nope": {"x": 1}})
    assert reply["ok"] is False and "nope" in reply["errors"]


def test_update_settings_location_units_and_fallback(api: Api, services: Services) -> None:
    reply = api.update_settings(
        {
            "tools": {"location": "Porto,  Portugal", "units": "imperial"},
            "chat": {"fallback_provider": "studioforge", "fallback_model": "m1"},
        }
    )
    assert reply["ok"] is True
    cfg = reply["config"]
    assert (cfg["tools"]["location"], cfg["tools"]["units"]) == ("Porto, Portugal", "imperial")
    assert (cfg["chat"]["fallback_provider"], cfg["chat"]["fallback_model"]) == (
        "studioforge",
        "m1",
    )
    text = services.paths.config_file.read_text(encoding="utf-8")
    assert 'fallback_provider = "studioforge"' in text and 'units = "imperial"' in text
    # The local runtime can never be its own fallback.
    reply = api.update_settings({"chat": {"fallback_provider": "local-npu"}})
    assert reply["config"]["chat"]["fallback_provider"] == ""


def test_removing_the_fallback_provider_clears_the_fallback(api: Api, services: Services) -> None:
    spec = {
        "id": "acme",
        "kind": "openai",
        "display_name": "Acme",
        "base_url": "https://acme.test/v1",
        "models": ["a1"],
    }
    assert api.upsert_provider(spec)["ok"] is True
    api.update_settings({"chat": {"fallback_provider": "acme", "fallback_model": "a1"}})
    assert services.config.chat.fallback_provider == "acme"
    assert api.remove_provider("acme")["ok"] is True
    assert (services.config.chat.fallback_provider, services.config.chat.fallback_model) == (
        "",
        "",
    )


def test_seeded_providers_show_as_built_in(api: Api) -> None:
    views = {p["id"]: p for p in api.list_providers()["providers"]}
    for pid in ("minimax", "studioforge", "openai", "deepseek"):
        assert views[pid]["builtin"] is True
    assert views["studioforge"]["key"]["required"] is False
    assert views["openai"]["docs_url"] == "https://platform.openai.com/api-keys"


def test_get_settings_full_config(api: Api) -> None:
    reply = api.get_settings()
    assert reply["ok"] and reply["restart_required"] == [] and reply["errors"] == {}
    assert reply["config"]["providers"]["minimax"]["id"] == "minimax"
    # Never a key value or a key-carrying field, only the env var *name*.
    assert re.search(r'"(api_key|key)"\s*:', json.dumps(reply)) is None


def test_upsert_and_remove_provider(api: Api, services: Services) -> None:
    spec = {
        "id": "acme",
        "kind": "openai",
        "display_name": "Acme",
        "base_url": "https://acme.test/v1",
        "models": ["a1"],
        "default_model": "a1",
        "quirks": [],
        "supports_tools": True,
    }
    reply = api.upsert_provider(spec)
    assert reply["ok"] is True
    assert reply["provider"]["id"] == "acme" and reply["provider"]["builtin"] is False
    assert "acme" in services.config.providers
    assert "[providers.acme]" in services.paths.config_file.read_text(encoding="utf-8")
    assert [p["id"] for p in api.list_providers()["providers"]][-1] == "acme"

    assert api.select_model("acme", None)["selected"] == {"provider": "acme", "model": "a1"}
    reply = api.remove_provider("acme")
    assert reply["ok"] is True
    assert "acme" not in services.config.providers
    assert services.config.chat.provider == "local-npu"
    assert api.remove_provider("minimax")["error"]["code"] == "bad_request"
    assert api.remove_provider("local-npu")["ok"] is False
    assert api.upsert_provider({"id": "bad id"})["ok"] is False
    assert (
        api.upsert_provider(
            {"id": "local-npu", "kind": "openai", "display_name": "x", "base_url": "https://x/v1"}
        )["ok"]
        is False
    )


def test_select_model_remote_and_events(api: Api, services: Services, events) -> None:
    reply = api.select_model("minimax", "MiniMax-M2.7")
    assert reply["ok"] and reply["selected"] == {"provider": "minimax", "model": "MiniMax-M2.7"}
    assert services.config.chat.provider == "minimax"
    assert [e["type"] for e in events[0].drain()] == ["settings.changed"]
    assert api.select_model("ghost", None)["error"]["code"] == "not_found"


# --- chat and window ops ------------------------------------------------------------------------


def test_send_message_validation(api: Api, services: Services) -> None:
    assert api.send_message("")["error"]["code"] == "bad_request"
    assert api.send_message("   ")["error"]["code"] == "bad_request"
    too_long = "x" * (services.config.chat.max_prompt_chars + 1)
    assert api.send_message(too_long)["error"]["code"] == "context_overflow"
    reply = api.send_message("hello")  # no engine wired
    assert reply["ok"] is False and reply["error"]["code"] == "server"


def test_send_message_dispatches_to_engine(api: Api, services: Services, loop) -> None:
    import asyncio

    services.loop = loop
    calls: list[tuple] = []
    done = asyncio.Event()

    class Engine:
        async def send(self, text, request_id, emit):
            calls.append((text, request_id))
            emit({"type": "chat.done", "request_id": request_id})
            done.set()

        def cancel(self, rid):
            calls.append(("cancel", rid))

        def new_chat(self):
            calls.append(("new",))

        def snapshot(self):
            return {"conversation": [{"role": "user", "content": "hi", "ts": 1}]}

    services.engine = Engine()
    reply = api.send_message("hi")
    assert reply["ok"] and reply["request_id"].startswith("req_")
    loop.run(asyncio.wait_for(done.wait(), 2))
    assert calls[0] == ("hi", reply["request_id"])
    assert api.stop_generation(reply["request_id"])["ok"]
    assert api.new_chat()["ok"]
    assert ("cancel", reply["request_id"]) in calls and ("new",) in calls
    assert api.get_state()["conversation"] == [{"role": "user", "content": "hi", "ts": 1}]


def test_regenerate_dispatches_to_engine(api: Api, services: Services, loop, events) -> None:
    import asyncio

    services.loop = loop
    calls: list[str] = []
    done = asyncio.Event()
    ended: list[str] = []
    services.on_reply_end = ended.append

    class Engine:
        async def regenerate(self, request_id, emit):
            calls.append(request_id)
            emit({"type": "chat.done", "request_id": request_id})
            done.set()

    assert api.regenerate()["error"]["code"] == "server"  # no engine wired
    services.engine = Engine()
    reply = api.regenerate()
    assert reply["ok"] and reply["request_id"].startswith("req_")
    loop.run(asyncio.wait_for(done.wait(), 2))
    assert calls == [reply["request_id"]] and ended == [reply["request_id"]]


def test_recent_models_newest_first_unique_and_capped(api: Api, services: Services) -> None:
    def recent() -> list[tuple[str, str]]:
        return [
            (r["provider"], r["model"]) for r in ui_config(services.config)["chat"]["recent_models"]
        ]

    assert recent() == []
    api.select_model("minimax", "MiniMax-M2.7")
    api.select_model("openai", "gpt-5-mini")
    api.select_model("minimax", "MiniMax-M2.7")
    assert recent() == [("minimax", "MiniMax-M2.7"), ("openai", "gpt-5-mini")]
    api.select_model("deepseek", "deepseek-chat")
    api.select_model("openai", "gpt-5")
    assert recent() == [
        ("openai", "gpt-5"),
        ("deepseek", "deepseek-chat"),
        ("minimax", "MiniMax-M2.7"),
    ]
    # Kept in config.toml, so the menu survives a restart.
    assert load_config(services.paths).chat.recent_models[0].model == "gpt-5"


def test_sending_records_the_model_used(api: Api, services: Services, loop, events) -> None:
    import asyncio

    services.loop = loop
    done = asyncio.Event()

    class Engine:
        async def send(self, text, request_id, emit):
            done.set()

    services.engine = Engine()
    assert services.config.chat.recent_models == []
    events[0].drain()
    assert api.send_message("hi")["ok"]
    loop.run(asyncio.wait_for(done.wait(), 2))
    first = services.config.chat.recent_models[0]
    assert (first.provider, first.model) == (
        services.config.chat.provider,
        services.config.chat.model,
    )
    changed = [e for e in events[0].drain() if e["type"] == "settings.changed"]
    assert changed and changed[0]["config"]["chat"]["recent_models"][0]["model"] == first.model
    # Already first: the next message writes nothing.
    assert api.send_message("again")["ok"]
    assert not [e for e in events[0].drain() if e["type"] == "settings.changed"]


class EndingEngine:
    """Ends each request with ``outcome`` once ``release`` is set (immediately by default)."""

    def __init__(self, outcome: dict) -> None:
        import asyncio

        self.outcome = outcome
        self.release: asyncio.Event | None = None
        self.done = asyncio.Event()
        self.cancelled: list[str] = []

    async def send(self, text, request_id, emit, attachments=None):
        if self.release is not None:
            await self.release.wait()
        emit({**self.outcome, "request_id": request_id})
        self.done.set()

    def cancel(self, rid):
        self.cancelled.append(rid)


def _ends(api: Api, services: Services, loop, outcome: dict) -> tuple[list[str], list[dict]]:
    """Send one message that ends with ``outcome``: (on_reply_end calls, page events)."""
    import asyncio

    services.loop = loop
    engine = services.engine = EndingEngine(outcome)
    ended: list[str] = []
    services.on_reply_end = ended.append
    reply = api.send_message("hi")
    loop.run(asyncio.wait_for(engine.done.wait(), 2))
    assert ended in ([], [reply["request_id"]])
    return ended, services.events.drain()


@pytest.mark.parametrize(
    "outcome",
    [{"type": "chat.done"}, {"type": "chat.error", "code": "no_key"}],
    ids=["done", "error"],
)
def test_reply_end_is_reported_after_the_last_event(api, services, loop, outcome) -> None:
    ended, delivered = _ends(api, services, loop, outcome)
    assert len(ended) == 1
    assert delivered[-1]["type"] == outcome["type"]  # the page got the event first


def test_a_stopped_reply_is_not_reported(api, services, loop) -> None:
    ended, delivered = _ends(api, services, loop, {"type": "chat.error", "code": "cancelled"})
    assert ended == [] and delivered[-1]["code"] == "cancelled"


def test_a_reply_the_page_abandoned_is_not_reported(api, services, loop) -> None:
    import asyncio

    services.loop = loop
    engine = services.engine = EndingEngine({"type": "chat.done"})
    engine.release = asyncio.Event()
    ended: list[str] = []
    services.on_reply_end = ended.append
    rid = api.send_message("hi")["request_id"]
    # The page gave up on it (New chat, or Stop) but the engine finished it anyway.
    assert api.stop_generation(rid)["ok"]
    loop.call(engine.release.set)
    loop.run(asyncio.wait_for(engine.done.wait(), 2))
    assert engine.cancelled == [rid] and ended == []
    # The next request is reported as usual.
    engine.done.clear()
    rid2 = api.send_message("again")["request_id"]
    loop.run(asyncio.wait_for(engine.done.wait(), 2))
    assert ended == [rid2]


def test_a_failing_reply_end_hook_does_not_break_the_turn(api, services, loop) -> None:
    import asyncio
    import base64

    services.loop = loop
    engine = services.engine = EndingEngine({"type": "chat.done"})

    def broken(_rid: str) -> None:
        raise RuntimeError("boom")

    services.on_reply_end = broken
    aid = api.attach_data("a.txt", base64.b64encode(b"alpha").decode())["attachments"][0]["id"]
    assert api.send_message("Read", [aid])["ok"] is True
    loop.run(asyncio.wait_for(engine.done.wait(), 2))
    assert aid not in services.attachments  # the files path still releases them
    assert services.events.drain()[-1]["type"] == "chat.done"


def test_window_ops(api: Api, services: Services) -> None:
    popup = FakePopup()
    services.popup = popup
    assert api.hide_popup()["ok"] and popup.hidden == 1 and popup.blurred is False
    assert api.hide_popup("blur")["ok"] and popup.hidden == 2 and popup.blurred is True
    assert api.open_settings()["ok"] is False  # no settings window wired
    assert popup.settings_noted == 1


def test_the_pin_saves_sticky(api: Api, services: Services) -> None:
    assert services.config.ui.sticky is True  # on by default
    services.events.drain()
    assert api.set_sticky(False) == {"ok": True, "sticky": False}
    assert services.config.ui.sticky is False
    assert load_config(services.paths).ui.sticky is False
    changed = services.events.drain()
    assert [e["type"] for e in changed] == ["settings.changed"]
    assert changed[0]["config"]["ui"]["sticky"] is False
    assert api.set_sticky(False)["sticky"] is False  # no change: nothing written or sent
    assert services.events.drain() == []
    assert api.set_sticky(True)["sticky"] is True
    assert load_config(services.paths).ui.sticky is True


def test_resize_calls_reach_the_popup(api: Api, services: Services) -> None:
    popup = FakePopup()
    services.popup = popup
    assert api.start_resize("top-left", 4.4, 5.6, True) == {"ok": True, "resizing": True}
    assert api.drag_resize(-20.2, 7.5) == {"ok": True, "resizing": True}
    assert popup.resizes == [("begin", "top-left", 4, 6, True), ("drag", -20, 8)]
    for bad in ("bottom", "", None, ["top"]):
        assert api.start_resize(bad, 0, 0, True)["error"]["code"] == "bad_request"
    assert len(popup.resizes) == 2


def test_resize_without_a_popup_is_harmless(api: Api) -> None:
    assert api.start_resize("top", 1, 1, False) == {"ok": True, "resizing": False}
    assert api.drag_resize(1, 1) == {"ok": True, "resizing": False}
    assert api.end_resize() == {"ok": True, "width": 420, "height": 620}


def test_the_size_a_resize_ends_at_is_kept(api: Api, services: Services) -> None:
    popup = FakePopup()
    services.popup = popup
    popup.end_size = (612, 805)
    services.events.drain()
    assert api.end_resize() == {"ok": True, "width": 612, "height": 805}
    assert (services.config.ui.width, services.config.ui.height) == (612, 805)
    # Like every saved setting, the pages hear about it.
    changed = services.events.drain()
    assert [e["type"] for e in changed] == ["settings.changed"]
    assert changed[0]["config"]["ui"]["width"] == 612
    on_disk = load_config(services.paths).ui
    assert (on_disk.width, on_disk.height) == (612, 805)
    # Kept within what the popup and the config allow.
    popup.end_size = (100, 9000)
    assert api.end_resize() == {"ok": True, "width": 320, "height": 4000}


def test_a_resize_that_changed_nothing_writes_nothing(
    api: Api, services: Services, monkeypatch
) -> None:
    popup = FakePopup()
    services.popup = popup
    writes: list[dict] = []
    monkeypatch.setattr(api, "_apply_patch", lambda patch: writes.append(patch))
    assert api.end_resize() == {"ok": True, "width": 420, "height": 620}  # a click
    popup.end_size = (420, 620)  # dragged back to where it was
    services.events.drain()
    assert api.end_resize()["ok"] is True
    assert writes == []
    assert services.events.drain() == []


@pytest.mark.parametrize("call", ["new_chat", "reset_popup_size"])
def test_clear_chat_and_reset_bring_back_the_default_size(
    api: Api, services: Services, call: str
) -> None:
    popup = FakePopup()
    services.popup = popup
    popup.end_size = (700, 900)
    api.end_resize()
    reply = getattr(api, call)()
    assert reply["ok"] is True
    if call == "reset_popup_size":
        assert reply == {"ok": True, "width": 420, "height": 620}
    assert (services.config.ui.width, services.config.ui.height) == (420, 620)
    on_disk = load_config(services.paths).ui
    assert (on_disk.width, on_disk.height) == (420, 620)
    assert popup.reset_to == [(420, 620)]


def test_clear_chat_still_clears_when_the_size_reset_fails(
    api: Api, services: Services, monkeypatch
) -> None:
    popup = FakePopup()
    services.popup = popup

    def broken(_width: int, _height: int) -> bool:
        raise OSError("no window")

    monkeypatch.setattr(popup, "reset_size", broken)
    assert api.new_chat() == {"ok": True}


def test_open_external_only_http(api: Api, monkeypatch) -> None:
    opened: list[str] = []
    monkeypatch.setattr("webbrowser.open", lambda url: opened.append(url))
    assert api.open_external("file:///C:/Windows")["error"]["code"] == "bad_request"
    assert api.open_external("javascript:alert(1)")["ok"] is False
    assert api.open_external("")["ok"] is False
    assert api.open_external("https://example.com/x")["ok"] is True


def test_load_unload_without_manager(api: Api) -> None:
    assert api.load_model()["error"]["code"] == "server"
    assert api.unload_model()["error"]["code"] == "server"


def test_logs_and_autostart_shapes(api: Api, monkeypatch) -> None:
    import logging

    from chatforge import autostart

    logging.getLogger("chatforge.test").warning("hello from the test")
    reply = api.get_logs(50, "WARNING")
    assert reply["ok"] and isinstance(reply["lines"], list)
    # Never the real Task Scheduler from a test.
    off = autostart.AutostartStatus(False, "Task Scheduler", None)
    monkeypatch.setattr(autostart, "status", lambda: off)
    reply = api.get_autostart()
    assert reply["ok"] and set(reply) >= {"enabled", "mode", "path"}
    assert reply["mode"] == "task-scheduler"


def test_set_hotkey_validates_and_registers(api: Api, services: Services) -> None:
    class Hotkey:
        def __init__(self) -> None:
            self.specs: list[str] = []

        def register(self, spec: str) -> str | None:
            self.specs.append(spec)
            return (
                "Ctrl+Alt+Delete is in use by another app." if spec == "Ctrl+Alt+Delete" else None
            )

    services.hotkey = Hotkey()
    assert api.set_hotkey("not a hotkey")["error"]["code"] == "bad_request"
    assert api.set_hotkey("ctrl+alt+delete")["error"]["code"] == "conflict"
    reply = api.set_hotkey("ctrl+shift+space")
    assert reply == {"ok": True, "hotkey": "Ctrl+Shift+Space"}
    assert services.config.ui.hotkey == "Ctrl+Shift+Space"


def test_disk_usage_and_runtime_status(api: Api) -> None:
    usage = api.disk_usage()
    assert usage["ok"] and set(usage) == {"ok", "models", "cache", "runtime", "logs", "free"}
    status = api.runtime_status()
    assert status["ok"] and status["installed"] is False and status["version"] is None


def test_list_models_and_downloads_without_services(api: Api) -> None:
    assert api.list_models() == {"ok": True, "models": []}
    assert api.list_downloads() == {"ok": True, "downloads": []}
    assert api.start_download("OpenVINO/x")["error"]["code"] == "server"
    assert api.search_models("q", None)["error"]["code"] == "server"


# --- helpers ----------------------------------------------------------------------------------


def test_error_payload_shapes() -> None:
    from chatforge.errors import AppError
    from chatforge.runtime.ovms_install import InstallError
    from chatforge.runtime.ovms_supervisor import OvmsError

    p = error_payload(AppError("boom", code="x", hint="h", action="retry"))
    assert p == {"code": "x", "message": "boom", "hint": "h", "action": "retry"}
    p = error_payload(OvmsError("dead", code="exited", hint="see log", details={"rc": 1}))
    assert p["code"] == "exited" and p["hint"] == "see log" and p["details"] == {"rc": 1}
    p = error_payload(InstallError("bad zip", code="checksum"))
    assert p["code"] == "checksum" and p["message"] == "bad zip"
    assert error_payload(ValueError("nope"))["code"] == "bad_request"
    assert error_payload(TimeoutError())["code"] == "timeout"
    p = error_payload(RuntimeError("x"))
    assert p["code"] == "server" and "Logs" in p["hint"]


def test_download_and_install_views() -> None:
    view = download_view(
        {
            "group_id": "g",
            "repo_id": "a/b",
            "status": "running",
            "downloaded_bytes": 5,
            "total_bytes": 10,
            "percent": 50.0,
            "speed_bps": 2.0,
            "eta_s": 2.5,
            "current_file": "f.bin",
            "files_done": 1,
            "files_total": 3,
            "error": None,
        }
    )
    assert (
        view["download_id"] == "g" and view["status"] == "downloading" and view["file"] == "f.bin"
    )
    assert download_view({"group_id": "g", "status": "completed"})["status"] == "done"
    assert download_view({"group_id": "g", "status": "canceled"})["status"] == "cancelled"
    assert download_view({"group_id": "g", "status": "failed"})["status"] == "error"
    inst = runtime_install_view({"status": "extracting"})
    assert set(inst) == {"status", "downloaded_bytes", "total_bytes", "speed_bps", "eta_s", "error"}


# --- quick actions ---------------------------------------------------------------------------


def test_send_message_passes_a_quick_action_to_the_engine(
    api: Api, services: Services, loop
) -> None:
    import asyncio

    services.loop = loop
    calls: list[tuple] = []
    done = asyncio.Event()

    class Engine:
        async def send(self, text, request_id, emit, **kw):
            calls.append((text, kw))
            emit({"type": "chat.done", "request_id": request_id})
            if len(calls) == 2:
                done.set()

    services.engine = Engine()
    assert api.send_message("Teh cat", None, "proof")["ok"] is True
    assert api.send_message("plain")["ok"] is True
    loop.run(asyncio.wait_for(done.wait(), 2))
    # Without an action the engine is called exactly as before.
    assert calls == [("Teh cat", {"action": "proof"}), ("plain", {})]
    bad = api.send_message("x", [], "no-such-action")
    assert bad["ok"] is False and bad["error"]["code"] == "bad_request"
    api.update_settings({"chat": {"hidden_quick_actions": ["proof"]}})
    assert api.send_message("x", [], "proof")["error"]["code"] == "bad_request"
    assert len(calls) == 2


def test_quick_actions_in_settings_and_events(api: Api, services: Services, events) -> None:
    reply = api.get_settings()
    qa = reply["quick_actions"]
    assert [d["id"] for d in qa["defaults"]][:4] == ["proof", "improve", "check", "insight"]
    assert qa["items"] == qa["defaults"]
    patch = {
        "chat": {
            "quick_actions": [
                {"id": "proof", "label": "Proofread", "instructions": ""},
                {"label": "Haiku", "instructions": "Write a haiku.", "tools": False},
            ],
            "hidden_quick_actions": ["translate"],
        }
    }
    reply = api.update_settings(patch)
    assert reply["ok"] is True
    items = {i["id"]: i for i in reply["quick_actions"]["items"]}
    assert items["proof"]["label"] == "Proofread" and "translate" not in items
    assert items["custom-haiku"]["builtin"] is False
    changed = events[0].drain()[-1]
    assert changed["type"] == "settings.changed"
    views = changed["config"]["quick_actions"]
    assert views[0] == {
        "id": "proof",
        "label": "Proofread",
        "hint": "Paste the text to proofread…",
        "tools": False,
    }
    assert views[1]["id"] == "custom-haiku"  # the list's order: proof, haiku, then the rest
    bad = api.update_settings({"chat": {"quick_actions": [{"label": " "}]}})
    assert bad["ok"] is False and any(k.startswith("chat.quick_actions") for k in bad["errors"])


# --- drop_last_turn, undo_clear ---------------------------------------------------------------


class _TurnEngine:
    """Just enough engine for the turn methods; every call runs on the core loop."""

    def __init__(self) -> None:
        from chatforge.errors import AppError

        self._err = AppError
        self.items = [
            {"role": "user", "content": "hi", "ts": 1},
            {"role": "assistant", "content": "hello", "ts": 2},
        ]
        self.cleared: list[dict] | None = None
        self.busy = False

    def snapshot(self) -> dict:
        return {"conversation": list(self.items)}

    def drop_last_turn(self) -> dict:
        if self.busy:
            raise self._err("A reply is still being written.", code="busy")
        if not self.items:
            raise self._err("There is no message to remove.", code="empty")
        self.items = []
        return {"content": "hi", "attachments": [], "action": None}

    def new_chat(self) -> None:
        self.cleared, self.items = self.items, []

    def restore_cleared(self) -> bool:
        if self.cleared is None:
            return False
        self.items, self.cleared = self.cleared, None
        return True


def test_drop_last_turn_reply_shape_and_errors(api: Api, services: Services, loop) -> None:
    services.loop = loop
    assert api.drop_last_turn()["error"]["code"] == "server"  # no engine wired
    engine = services.engine = _TurnEngine()
    engine.busy = True
    reply = api.drop_last_turn()
    assert reply["ok"] is False and reply["error"]["code"] == "busy"
    assert set(reply["error"]) == {"code", "message", "hint", "action"}
    engine.busy = False
    reply = api.drop_last_turn()
    assert reply == {
        "ok": True,
        "removed": {"content": "hi", "attachments": [], "action": None},
        "conversation": [],
    }
    assert api.drop_last_turn()["error"]["code"] == "empty"


def test_undo_clear_restores_once(api: Api, services: Services, loop) -> None:
    services.loop = loop
    assert api.undo_clear()["error"]["code"] == "nothing_to_undo"  # no engine wired
    engine = services.engine = _TurnEngine()
    assert api.undo_clear()["error"]["code"] == "nothing_to_undo"
    assert api.new_chat() == {"ok": True}
    reply = api.undo_clear()
    assert reply == {"ok": True, "conversation": engine.items}
    assert [i["content"] for i in reply["conversation"]] == ["hi", "hello"]
    assert api.undo_clear()["error"]["code"] == "nothing_to_undo"


def test_drop_last_turn_and_undo_clear_against_the_real_engine(
    api: Api, services: Services, loop, tmp_path
) -> None:
    from chatforge.chat.engine import ChatEngine

    engine = ChatEngine(
        providers=services.providers,
        tools=None,
        get_config=lambda: services.config,
        conversation_file=services.paths.conversation_file,
    )
    engine.conversation.append({"role": "user", "content": "q", "_ts": 1.0})
    engine.conversation.append({"role": "assistant", "content": "a", "_ts": 2.0})
    services.loop, services.engine = loop, engine
    api.new_chat()
    assert api.get_state()["conversation"] == []
    restored = api.undo_clear()
    assert [i["role"] for i in restored["conversation"]] == ["user", "assistant"]
    dropped = api.drop_last_turn()
    assert dropped["removed"] == {"content": "q", "attachments": [], "action": None}
    assert dropped["conversation"] == []
    import json as _json

    saved = _json.loads(services.paths.conversation_file.read_text(encoding="utf-8"))
    assert saved["messages"] == []


# --- open_config_folder, diagnostics, open_external ------------------------------------------


def test_open_config_folder_opens_the_folder_holding_config_toml(
    api: Api, services: Services, monkeypatch
) -> None:
    from chatforge.desktop import win32util

    opened: list = []
    monkeypatch.setattr(win32util, "open_path", opened.append)
    assert api.open_config_folder() == {"ok": True}
    assert opened == [services.paths.config_file.parent]


def test_diagnostics_is_a_redacted_report(
    api: Api, services: Services, fake_keyring, monkeypatch
) -> None:
    import logging

    from chatforge import __version__
    from chatforge.logging_setup import RING_BUFFER, register_secret

    token = "tok-0123456789abcdefghij0123456789ABCDEF"
    api.upsert_provider(
        {
            "id": "acme",
            "kind": "openai",
            "display_name": "Acme",
            "base_url": "https://acme.test/v1",
            "models": ["a1"],
        }
    )
    api.save_api_key("acme", SECRET)
    services.paths.config_file.write_text(
        services.paths.config_file.read_text(encoding="utf-8")
        + f'\n[extra]\napi_key = "{SECRET}"\nhf_token = "{token}"\n'
        + 'base_url = "https://user:hunter2pass@x.test/v1?key=querysecret99"\n',
        encoding="utf-8",
    )
    register_secret("leaky-log-secret-1")
    RING_BUFFER.records.clear()
    for i in range(80):  # the ring buffer is not installed unless logging is configured
        RING_BUFFER.emit(
            logging.LogRecord(
                "diag.test", logging.WARNING, __file__, 1, "line %d leaky-log-secret-1", (i,), None
            )
        )
    reply = api.diagnostics()
    assert reply["ok"] is True and set(reply) == {"ok", "text"}
    text = reply["text"]
    assert f"app version: {__version__}" in text
    assert "python: " in text and "os: " in text
    assert "active provider: local-npu" in text and "active model: " in text
    assert "[runtime]" in text and "state: " in text
    assert "[config.toml]" in text and "schema_version" in text
    assert "api_key_env" in text  # env var *names* are kept
    for leaked in (SECRET, token, "hunter2pass", "querysecret99", "leaky-log-secret-1"):
        assert leaked not in text
    assert "***REDACTED***" in text
    log_part = text.split("log lines]")[1].strip().splitlines()
    assert len(log_part) == 50 and log_part[-1].endswith("line 79 ***REDACTED***")
    RING_BUFFER.records.clear()


@pytest.mark.parametrize(
    ("url", "opens"),
    [
        ("https://example.com/a?b=1", True),
        ("HTTP://example.com", True),
        ("mailto:someone@example.com?subject=Bug%20report", True),
        ("MAILTO:a@b.c", True),
        ("mailto:", False),
        ("file:///etc/passwd", False),
        ("javascript:alert(1)", False),
        ("ftp://example.com", False),
        ("https://example.com/\r\nHost: x", False),
        ("https://example.com/\x00", False),
        ("https://example.com/\x1b[0m", False),
        ("mailto:a@b.c\nbcc:x@y.z", False),
        ("https://example.com/" + "a" * 5000, False),
        ("", False),
    ],
)
def test_open_external_accepts_http_https_and_mailto_only(
    api: Api, monkeypatch, url, opens
) -> None:
    import threading

    started: list = []

    class _Thread:
        def __init__(self, *, target, args, **_kw) -> None:
            self.args = args

        def start(self) -> None:
            started.append(self.args[0])

    monkeypatch.setattr(threading, "Thread", _Thread)
    reply = api.open_external(url)
    assert reply["ok"] is opens
    assert started == ([url] if opens else [])
    if not opens:
        assert reply["error"]["code"] == "bad_request"


# --- keys follow their destination ---------------------------------------------------------------


def _acme(**over) -> dict:
    spec = {
        "id": "acme",
        "kind": "openai",
        "display_name": "Acme",
        "base_url": "https://acme.test/v1",
        "models": ["a1"],
    }
    return {**spec, **over}


def test_changing_a_providers_host_removes_its_saved_key(api: Api, fake_keyring, events) -> None:
    assert api.upsert_provider(_acme())["key_removed"] is False
    api.save_api_key("acme", SECRET)
    events[0].drain()
    # same host, other path and models: the key stays
    same = api.upsert_provider(_acme(base_url="https://acme.test/v2", models=["a1", "a2"]))
    assert same["ok"] and same["key_removed"] is False
    assert fake_keyring.get_password("ChatForge", "acme") == SECRET
    moved = api.upsert_provider(_acme(base_url="https://evil.example/v1"))
    assert moved["ok"] is True and moved["key_removed"] is True
    assert moved["provider"]["key"]["source"] == "none"
    assert fake_keyring.get_password("ChatForge", "acme") is None
    assert SECRET not in json.dumps(moved)
    assert [e["type"] for e in events[0].drain()].count("key.status") == 1


def test_changing_the_port_removes_the_key_too(api: Api, fake_keyring) -> None:
    api.upsert_provider(_acme())
    api.save_api_key("acme", SECRET)
    assert api.upsert_provider(_acme(base_url="https://acme.test:8443/v1"))["key_removed"] is True


def test_update_settings_provider_patch_removes_the_key_and_says_so(
    api: Api, services: Services, fake_keyring
) -> None:
    api.upsert_provider(_acme())
    api.save_api_key("acme", SECRET)
    api.save_api_key("minimax", SECRET + "-mm")
    benign = api.update_settings({"providers": {"acme": {"models": ["a1", "a3"]}}})
    assert benign["ok"] and benign["key_removed"] is False and benign["keys_removed"] == []
    reply = api.update_settings({"providers": {"acme": {"base_url": "https://evil.example/v1"}}})
    assert reply["ok"] and reply["key_removed"] is True and reply["keys_removed"] == ["acme"]
    assert fake_keyring.get_password("ChatForge", "acme") is None
    assert fake_keyring.get_password("ChatForge", "minimax") == SECRET + "-mm"
    assert services.config.providers["acme"].base_url == "https://evil.example/v1"


def test_a_builtin_providers_env_var_change_removes_its_key(api: Api, fake_keyring) -> None:
    api.save_api_key("openai", SECRET)
    reply = api.update_settings({"providers": {"openai": {"api_key_env": "SOMETHING_ELSE"}}})
    assert reply["ok"] and reply["keys_removed"] == ["openai"]
    assert fake_keyring.get_password("ChatForge", "openai") is None


def test_removing_a_provider_through_settings_removes_its_key(api: Api, fake_keyring) -> None:
    api.upsert_provider(_acme())
    api.save_api_key("acme", SECRET)
    reply = api.update_settings({"providers": {"acme": None}})
    assert reply["ok"] and reply["keys_removed"] == ["acme"]
    assert fake_keyring.get_password("ChatForge", "acme") is None
    # re-adding the id elsewhere finds no key
    api.upsert_provider(_acme(base_url="https://evil.example/v1"))
    assert api.list_providers()["providers"][-1]["key"]["source"] == "none"


def test_custom_providers_cannot_name_an_env_var_for_their_key(
    api: Api, services: Services, fake_keyring
) -> None:
    reply = api.upsert_provider(_acme(api_key_env="AWS_SECRET_ACCESS_KEY"))
    assert reply["ok"] is False and reply["error"]["code"] == "bad_request"
    assert "acme" not in services.config.providers
    api.upsert_provider(_acme())
    reply = api.update_settings({"providers": {"acme": {"api_key_env": "AWS_SECRET_ACCESS_KEY"}}})
    assert reply["ok"] is False and reply["error"]["code"] == "bad_request"
    assert "providers.acme.api_key_env" in reply["errors"]
    assert services.config.providers["acme"].api_key_env is None
    # empty is fine, and a built-in provider keeps its env var setting
    assert api.upsert_provider(_acme(api_key_env=""))["ok"] is True
    assert api.upsert_provider(_acme(id="openai", api_key_env="OPENAI_API_KEY"))["ok"] is True


# --- config-only settings ----------------------------------------------------------------------------


def test_update_settings_cannot_change_documents_dir_or_private_address_block(
    api: Api, services: Services
) -> None:
    for patch in (
        {"tools": {"documents_dir": "/tmp/elsewhere"}},
        {"tools.documents_dir": "/tmp/elsewhere"},
        {"tools": {"block_private_addresses": False}},
        {"tools": {"location": "Oslo", "block_private_addresses": False}},
    ):
        reply = api.update_settings(patch)
        assert reply["ok"] is False and reply["error"]["code"] == "bad_request", patch
        assert reply["errors"]
    tools = services.config.tools
    assert tools.documents_dir == "" and tools.block_private_addresses is True
    assert tools.location == ""  # a refused patch applies nothing
    text = services.paths.config_file.read_text(encoding="utf-8")
    assert "elsewhere" not in text


def test_update_settings_ignores_unchanged_readonly_values(api: Api, services: Services) -> None:
    same = {"documents_dir": "", "block_private_addresses": True, "location": "Oslo"}
    reply = api.update_settings({"tools": same})
    assert reply["ok"] is True and services.config.tools.location == "Oslo"
