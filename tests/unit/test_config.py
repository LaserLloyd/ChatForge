"""config.py: defaults, TOML round trip, env override, validation, update_config."""

import logging
import tomllib

import pytest

from aichat import config as config_mod
from aichat.config import (
    AppConfig,
    ProviderSpec,
    Timeouts,
    find_unknown_keys,
    load_config,
    normalize_base_url,
    parse_hotkey,
    save_config,
    update_config,
)
from aichat.errors import ConfigError
from aichat.paths import Paths


@pytest.fixture
def paths(aichat_home):
    return Paths.default()


# --- ProviderSpec / Timeouts ------------------------------------------------


def test_timeouts_defaults():
    t = Timeouts()
    assert (t.connect_s, t.stall_s, t.wall_s) == (10, 90, 600)


def test_provider_spec_defaults_and_env_name_rule():
    spec = ProviderSpec(id="x", kind="openai", display_name="X")
    assert spec.supports_tools is True
    assert spec.max_output_tokens == 2048
    assert spec.timeouts == Timeouts()
    assert spec.builtin is False
    assert (
        ProviderSpec(id="x", kind="openai", display_name="X", api_key_env="A_B1").api_key_env
        == "A_B1"
    )
    for bad in ("lower", "1ABC", "A B", "A-B", "", "A" * 65 + "B"):
        if bad == "":
            continue  # empty means "no env var"
        with pytest.raises(ValueError):
            ProviderSpec(id="x", kind="openai", display_name="X", api_key_env=bad)


# --- defaults ---------------------------------------------------------------


def test_defaults_match_plan(paths):
    cfg = load_config(paths)
    assert cfg.schema_version == 1
    assert cfg.chat.provider == "local-npu"
    assert cfg.chat.model == "OpenVINO/Qwen3-4B-int4-ov"
    assert cfg.chat.max_prompt_chars == 4000
    assert cfg.chat.max_tool_rounds == 4
    assert cfg.chat.show_reasoning == "collapsed"
    assert cfg.local.device == "NPU"
    assert cfg.local.idle_unload_minutes == 10
    assert cfg.local.max_prompt_len == 4096
    assert cfg.local.ovms_variant == "python_on"
    assert cfg.local.ovms_version == "2026.4.0"
    assert cfg.local.extra_args == []
    assert cfg.tools.enabled == ["web_search", "fetch_url", "current_datetime", "calculator"]
    assert cfg.ui.theme == "laserlloyd"
    assert cfg.ui.hotkey == "Ctrl+Alt+Space"
    assert cfg.startup.autostart is True
    assert cfg.logging.level == "INFO"
    assert cfg.logging.max_bytes == 5_000_000
    assert cfg.logging.backup_count == 3
    assert cfg.hf.endpoint == "https://huggingface.co"
    assert cfg.hf.default_author == "OpenVINO"


def test_removed_keys_are_gone():
    cfg = AppConfig()
    assert not hasattr(cfg.local, "enable_prefix_caching")
    assert not hasattr(cfg.local, "port")
    assert not hasattr(cfg.ui, "themes")


def test_seed_providers(paths):
    cfg = load_config(paths)
    assert set(cfg.providers) == {"local-npu", "minimax"}
    local = cfg.providers["local-npu"]
    assert (local.kind, local.display_name, local.builtin, local.quirks) == (
        "ovms",
        "Local (NPU)",
        True,
        ["ovms"],
    )
    mm = cfg.providers["minimax"]
    assert mm.id == "minimax"
    assert mm.kind == "openai"
    assert mm.region == "international"
    assert mm.base_url == "https://api.minimax.io/v1"
    assert mm.api_key_env == "MINIMAX_API_KEY"
    assert mm.models == [
        "MiniMax-M3",
        "MiniMax-M2.7-highspeed",
        "MiniMax-M2.7",
        "MiniMax-M3.1-Flash-Preview",
    ]
    assert mm.default_model == "MiniMax-M3"
    assert mm.quirks == ["minimax"]
    assert mm.max_output_tokens == 4096
    assert mm.temperature == 1.0
    assert mm.extra_body == {"reasoning_split": True}
    assert mm.timeouts == Timeouts(connect_s=10, stall_s=90, wall_s=600)


# --- file round trip ---------------------------------------------------------


def test_load_creates_file_with_defaults(paths):
    assert not paths.config_file.exists()
    cfg = load_config(paths)
    assert paths.config_file.is_file()
    raw = tomllib.loads(paths.config_file.read_text(encoding="utf-8"))
    assert raw["schema_version"] == 1
    assert raw["providers"]["minimax"]["base_url"] == "https://api.minimax.io/v1"
    assert "id" not in raw["providers"]["minimax"]
    assert "themes" not in raw["ui"]
    assert "port" not in raw["local"]
    assert load_config(paths) == cfg


def test_save_and_reload_round_trip(paths):
    cfg = load_config(paths)
    cfg.chat.system_prompt = 'Line one "quoted"\nLine two \u00e9\u4e2d'
    cfg.local.extra_args = ["--plugin_config", '{"NPUW_LLM_GENERATE_HINT":"BEST_PERF"}']
    cfg.ui.theme = "midnight-gold"
    cfg.providers["custom"] = ProviderSpec(
        id="custom",
        kind="openai",
        display_name="My API",
        base_url="http://127.0.0.1:1234/v1",
        api_key_env="MY_API_KEY",
        models=["a", "b"],
        temperature=0.2,
        extra_body={"top_k": 5, "nested": {"x": [1, 2]}},
    )
    save_config(cfg, paths)
    assert load_config(paths) == cfg
    assert not list(paths.home.glob("*.tmp"))


def test_save_is_atomic_when_replace_fails(paths, monkeypatch):
    cfg = load_config(paths)
    before = paths.config_file.read_bytes()
    cfg.ui.theme = "glacier"

    def boom(*_a, **_k):
        raise PermissionError("locked")

    monkeypatch.setattr(config_mod.os, "replace", boom)
    monkeypatch.setattr(config_mod.time, "sleep", lambda _s: None)
    with pytest.raises(ConfigError):
        save_config(cfg, paths)
    assert paths.config_file.read_bytes() == before
    assert not list(paths.home.glob("*.tmp"))


def test_partial_file_fills_defaults_and_keeps_seeds(paths):
    paths.home.mkdir(parents=True, exist_ok=True)
    paths.config_file.write_text(
        '[local]\ndevice = "gpu"\n\n[providers.minimax]\nregion = "china"\n'
        'base_url = "https://api.minimaxi.com/v1"\n',
        encoding="utf-8",
    )
    cfg = load_config(paths)
    assert cfg.local.device == "GPU"
    assert cfg.chat.provider == "local-npu"
    assert cfg.providers["minimax"].region == "china"
    assert cfg.providers["minimax"].models[0] == "MiniMax-M3"  # merged over the seed
    assert cfg.providers["minimax"].id == "minimax"
    assert cfg.providers["local-npu"].builtin is True  # seed re-added


def test_custom_provider_from_file(paths):
    paths.home.mkdir(parents=True, exist_ok=True)
    paths.config_file.write_text(
        '[providers.acme]\nkind = "openai"\ndisplay_name = "Acme"\n'
        'base_url = "https://api.acme.example/v1/"\napi_key_env = "ACME_KEY"\n',
        encoding="utf-8",
    )
    cfg = load_config(paths)
    assert cfg.providers["acme"].id == "acme"
    assert cfg.providers["acme"].base_url == "https://api.acme.example/v1"
    assert cfg.providers["acme"].builtin is False


# --- unknown keys ------------------------------------------------------------


def test_unknown_keys_warn_but_do_not_crash(paths, caplog):
    paths.home.mkdir(parents=True, exist_ok=True)
    paths.config_file.write_text(
        'mystery = 1\n[local]\nport = 18611\nenable_prefix_caching = true\ndevice = "CPU"\n'
        '[ui]\nthemes = ["a"]\n[providers.minimax]\nbogus = 2\n[providers.minimax.timeouts]\nx = 1\n',
        encoding="utf-8",
    )
    with caplog.at_level(logging.WARNING, logger="aichat.config"):
        cfg = load_config(paths)
    assert cfg.local.device == "CPU"
    text = "\n".join(r.getMessage() for r in caplog.records)
    for key in (
        "mystery",
        "local.port",
        "local.enable_prefix_caching",
        "ui.themes",
        "providers.minimax.bogus",
        "providers.minimax.timeouts.x",
    ):
        assert key in text


def test_find_unknown_keys_ignores_extra_body_contents():
    raw = {
        "providers": {"m": {"extra_body": {"anything": {"goes": 1}}, "timeouts": {"stall_s": 5}}}
    }
    assert find_unknown_keys(raw) == []


# --- env override ------------------------------------------------------------


def test_env_override(paths, monkeypatch):
    paths.home.mkdir(parents=True, exist_ok=True)
    paths.config_file.write_text(
        '[local]\ndevice = "CPU"\nidle_unload_minutes = 3\n', encoding="utf-8"
    )
    monkeypatch.setenv("AICHAT_LOCAL__DEVICE", "gpu")
    monkeypatch.setenv("AICHAT_CHAT__MAX_TOOL_ROUNDS", "2")
    monkeypatch.setenv("AICHAT_UI__HIDE_ON_BLUR", "false")
    cfg = load_config(paths)
    assert cfg.local.device == "GPU"
    assert cfg.local.idle_unload_minutes == 3  # file value still applies
    assert cfg.chat.max_tool_rounds == 2
    assert cfg.ui.hide_on_blur is False


def test_env_override_beats_file_but_is_not_written_on_first_run(paths, monkeypatch):
    monkeypatch.setenv("AICHAT_LOCAL__DEVICE", "CPU")
    cfg = load_config(paths)
    assert cfg.local.device == "CPU"
    assert tomllib.loads(paths.config_file.read_text(encoding="utf-8"))["local"]["device"] == "NPU"


def test_env_override_is_not_persisted_by_update(paths, monkeypatch):
    load_config(paths)
    monkeypatch.setenv("AICHAT_LOCAL__DEVICE", "CPU")
    cfg = load_config(paths)
    new, _ = update_config(cfg, {"ui": {"theme": "glacier"}}, paths)
    assert new.local.device == "CPU"  # effective value still overridden
    on_disk = tomllib.loads(paths.config_file.read_text(encoding="utf-8"))
    assert on_disk["local"]["device"] == "NPU"
    assert on_disk["ui"]["theme"] == "glacier"


def test_env_invalid_value_raises(paths, monkeypatch):
    monkeypatch.setenv("AICHAT_LOCAL__DEVICE", "TPU")
    with pytest.raises(ConfigError):
        load_config(paths)


# --- validation --------------------------------------------------------------


@pytest.mark.parametrize(
    ("section", "patch"),
    [
        ("local.device", {"local": {"device": "TPU"}}),
        ("local.idle_unload_minutes", {"local": {"idle_unload_minutes": -1}}),
        ("local.max_prompt_len", {"local": {"max_prompt_len": 1023}}),
        ("local.max_prompt_len", {"local": {"max_prompt_len": 8193}}),
        ("ui.hotkey", {"ui": {"hotkey": "Space"}}),
        ("ui.hotkey", {"ui": {"hotkey": "Ctrl+"}}),
        ("ui.hotkey", {"ui": {"hotkey": "Ctrl+Alt"}}),
        ("ui.hotkey", {"ui": {"hotkey": "Hyper+K"}}),
        ("ui.hotkey", {"ui": {"hotkey": "Ctrl+F25"}}),
        ("ui.theme", {"ui": {"theme": "Bad Theme!"}}),
        ("chat.temperature", {"chat": {"temperature": 3}}),
        ("chat.show_reasoning", {"chat": {"show_reasoning": "yes"}}),
        ("logging.level", {"logging": {"level": "LOUD"}}),
        (
            "providers.minimax.base_url",
            {"providers": {"minimax": {"base_url": "ftp://x.example/v1"}}},
        ),
        ("providers.minimax.base_url", {"providers": {"minimax": {"base_url": "localhost:1234"}}}),
        (
            "providers.minimax.base_url",
            {"providers": {"minimax": {"base_url": "https://user:pw@api.minimax.io/v1"}}},
        ),
        (
            "providers.minimax.api_key_env",
            {"providers": {"minimax": {"api_key_env": "minimax_key"}}},
        ),
        (
            "providers.minimax.timeouts.stall_s",
            {"providers": {"minimax": {"timeouts": {"stall_s": 0}}}},
        ),
        ("hf.endpoint", {"hf": {"endpoint": "file:///etc"}}),
    ],
)
def test_validation_errors(paths, section, patch):
    cfg = load_config(paths)
    before = paths.config_file.read_bytes()
    with pytest.raises(ConfigError) as exc:
        update_config(cfg, patch, paths)
    assert exc.value.code == "invalid_config"
    assert section in exc.value.details["errors"]
    assert paths.config_file.read_bytes() == before  # nothing written


def test_invalid_file_raises_config_error(paths):
    paths.home.mkdir(parents=True, exist_ok=True)
    paths.config_file.write_text("[local]\nmax_prompt_len = 99999\n", encoding="utf-8")
    with pytest.raises(ConfigError) as exc:
        load_config(paths)
    assert "local.max_prompt_len" in exc.value.details["errors"]


def test_invalid_toml_raises_and_recover_resets(paths):
    paths.home.mkdir(parents=True, exist_ok=True)
    paths.config_file.write_text("this is = = not toml", encoding="utf-8")
    with pytest.raises(ConfigError):
        load_config(paths)
    cfg = load_config(paths, recover=True)
    assert cfg.local.device == "NPU"
    assert paths.config_file.with_name("config.toml.bad").is_file()
    assert load_config(paths) == cfg


def test_boundary_values_accepted():
    assert (
        AppConfig.model_validate({"local": {"max_prompt_len": 1024}}).local.max_prompt_len == 1024
    )
    assert (
        AppConfig.model_validate({"local": {"max_prompt_len": 8192}}).local.max_prompt_len == 8192
    )
    assert (
        AppConfig.model_validate({"local": {"idle_unload_minutes": 0}}).local.idle_unload_minutes
        == 0
    )


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("Ctrl+Alt+Space", (("Ctrl", "Alt"), "Space")),
        ("alt+ctrl+space", (("Ctrl", "Alt"), "Space")),
        ("Shift+Win+f12", (("Shift", "Win"), "F12")),
        ("Control+K", (("Ctrl",), "K")),
        ("Ctrl+Shift+1", (("Ctrl", "Shift"), "1")),
        ("Ctrl+Alt+Esc", (("Ctrl", "Alt"), "Esc")),
    ],
)
def test_parse_hotkey(text, expected):
    assert parse_hotkey(text) == expected


def test_hotkey_is_normalised_in_config():
    assert (
        AppConfig.model_validate({"ui": {"hotkey": "alt+ctrl+space"}}).ui.hotkey == "Ctrl+Alt+Space"
    )


@pytest.mark.parametrize(
    ("url", "expected"),
    [
        ("http://127.0.0.1:1234/v1/", "http://127.0.0.1:1234/v1"),
        ("  https://api.example.com/v1  ", "https://api.example.com/v1"),
        ("https://api.example.com", "https://api.example.com"),
    ],
)
def test_normalize_base_url_ok(url, expected):
    assert normalize_base_url(url) == expected


@pytest.mark.parametrize(
    "url",
    [
        "",
        "   ",
        "file:///c:/x",
        "ftp://x.example",
        "localhost:1234",
        "http://",
        "http://u@host/v1",
        "http://u:p@host/v1",
        "https://host/v1?key=1",
        "https://host:notaport/v1",
    ],
)
def test_normalize_base_url_rejects(url):
    with pytest.raises(ValueError):
        normalize_base_url(url)


# --- update_config -----------------------------------------------------------


def test_update_config_persists_and_returns_new(paths):
    cfg = load_config(paths)
    new, restart = update_config(
        cfg, {"ui": {"theme": "glacier"}, "chat": {"max_tool_rounds": 2}}, paths
    )
    assert restart == []
    assert new.ui.theme == "glacier"
    assert cfg.ui.theme == "laserlloyd"  # input not mutated
    assert load_config(paths) == new


@pytest.mark.parametrize(
    ("patch", "expected"),
    [
        ({"local": {"device": "GPU"}}, ["local.device"]),
        ({"local": {"max_prompt_len": 2048}}, ["local.max_prompt_len"]),
        ({"local": {"extra_args": ["--x"]}}, ["local.extra_args"]),
        ({"local": {"ovms_variant": "python_off"}}, ["local.ovms_variant"]),
        (
            {"local": {"device": "CPU", "max_prompt_len": 1024, "extra_args": ["a"]}},
            ["local.device", "local.max_prompt_len", "local.extra_args"],
        ),
        ({"local": {"idle_unload_minutes": 0}}, []),
        ({"local": {"enable_thinking": True, "autoload_on_open": False}}, []),
        ({"local": {"device": "NPU"}}, []),  # same value: not a change
    ],
)
def test_restart_keys(paths, patch, expected):
    cfg = load_config(paths)
    _new, restart = update_config(cfg, patch, paths)
    assert restart == expected


def test_update_config_dotted_keys(paths):
    cfg = load_config(paths)
    new, restart = update_config(cfg, {"local.device": "gpu", "ui.margin": 20}, paths)
    assert new.local.device == "GPU"
    assert new.ui.margin == 20
    assert restart == ["local.device"]


def test_update_config_rejects_unknown_keys(paths):
    cfg = load_config(paths)
    with pytest.raises(ConfigError) as exc:
        update_config(cfg, {"local": {"port": 1}}, paths)
    assert "local.port" in exc.value.details["errors"]
    with pytest.raises(ConfigError):
        update_config(cfg, {"nonsense": {"a": 1}}, paths)


def test_update_config_provider_crud(paths):
    cfg = load_config(paths)
    spec = {
        "kind": "openai",
        "display_name": "Acme",
        "base_url": "https://api.acme.example/v1",
        "api_key_env": "ACME_KEY",
        "models": ["acme-1"],
    }
    new, _ = update_config(cfg, {"providers": {"acme": spec}}, paths)
    assert new.providers["acme"].id == "acme"
    assert "acme" in load_config(paths).providers
    # partial edit merges
    new, _ = update_config(new, {"providers": {"acme": {"default_model": "acme-1"}}}, paths)
    assert new.providers["acme"].default_model == "acme-1"
    assert new.providers["acme"].base_url == "https://api.acme.example/v1"
    # remove
    new, _ = update_config(new, {"providers": {"acme": None}}, paths)
    assert "acme" not in new.providers
    assert "acme" not in load_config(paths).providers
    # builtin refused
    with pytest.raises(ConfigError):
        update_config(new, {"providers": {"local-npu": None}}, paths)


def test_update_config_without_existing_file(paths):
    cfg = AppConfig()
    new, restart = update_config(cfg, {"local": {"device": "GPU"}}, paths)
    assert restart == ["local.device"]
    assert paths.config_file.is_file()
    assert load_config(paths).local.device == "GPU"
