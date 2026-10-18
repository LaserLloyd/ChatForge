"""First run, 0.2.0: autostart is asked (never silent), the version is one number, the NPU
probe, the bridge methods behind the cards, and the shipped example config."""

from __future__ import annotations

import json
import re
import tomllib
from pathlib import Path
from typing import Any

import pytest

import chatforge
from chatforge import __main__ as cli
from chatforge import autostart
from chatforge.config import AppConfig, load_config, to_toml_dict, validate_config
from chatforge.desktop.bridge import CONTRACT_METHODS, Api, Services
from chatforge.paths import Paths
from chatforge.runtime import npu_detect

ROOT = Path(__file__).resolve().parents[2]


# --------------------------------------------------------------------------- version


def test_one_version_everywhere() -> None:
    pyproject = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    package = json.loads((ROOT / "package.json").read_text(encoding="utf-8"))
    assert pyproject["project"]["version"] == chatforge.__version__ == package["version"]
    assert re.fullmatch(r"\d+\.\d+\.\d+", chatforge.__version__)
    assert (ROOT / ".python-version").read_text().strip() == "3.12"
    assert "<3.14" in pyproject["project"]["requires-python"]


def test_changelog_has_the_current_version() -> None:
    text = (ROOT / "CHANGELOG.md").read_text(encoding="utf-8")
    assert f"## [{chatforge.__version__}]" in text
    assert "Upgrading" in text


def test_version_flag(capsys) -> None:
    with pytest.raises(SystemExit) as ei:
        cli.main(["--version"])
    assert ei.value.code == 0
    assert capsys.readouterr().out.strip() == f"chatforge {chatforge.__version__}"


def test_doctor_has_no_stale_workstream_strings() -> None:
    text = (ROOT / "src/chatforge/__main__.py").read_text(encoding="utf-8")
    assert "WS9" not in text


# --------------------------------------------------------------------------- autostart asks


def test_a_new_config_does_not_enable_autostart(chatforge_home: Path) -> None:
    cfg = load_config(Paths.default())
    assert cfg.startup.autostart is False and cfg.startup.autostart_asked is False


def test_app_main_never_registers_autostart_on_its_own(monkeypatch, chatforge_home: Path) -> None:
    """The old first-run block called autostart.enable(); it must stay gone."""
    import chatforge.app as app_mod

    called: list[str] = []
    monkeypatch.setattr(autostart, "enable", lambda *a, **k: called.append("enable"))
    monkeypatch.setattr(app_mod.single_instance, "acquire_single_instance", lambda: None)
    monkeypatch.setattr(app_mod.single_instance, "signal_existing", lambda: False)
    monkeypatch.setattr(app_mod.xutil, "prefer_x11", lambda: False)
    monkeypatch.setattr(
        app_mod, "_bootstrap_logging", lambda *a, **k: None
    )  # keep the global log setup
    monkeypatch.setattr("chatforge.desktop.win32util.ensure_dpi_awareness", lambda: None)
    assert app_mod.main("hidden", paths=Paths.default()) == 0  # "another instance is running"
    assert called == []


def test_the_cli_records_the_installer_s_answer(monkeypatch, chatforge_home: Path, capsys) -> None:
    status = autostart.AutostartStatus(True, "Task Scheduler", chatforge_home / "ChatForge.vbs")
    monkeypatch.setattr(autostart, "enable", lambda *a, **k: status)
    monkeypatch.setattr(
        autostart,
        "disable",
        lambda *a, **k: autostart.AutostartStatus(False, "Task Scheduler", None),
    )
    assert cli.main(["autostart", "enable"]) == 0
    cfg = load_config(Paths.default())
    assert (cfg.startup.autostart, cfg.startup.autostart_asked) == (True, True)
    assert cli.main(["autostart", "disable"]) == 0
    cfg = load_config(Paths.default())
    assert (cfg.startup.autostart, cfg.startup.autostart_asked) == (False, True)
    assert "autostart" in capsys.readouterr().out


@pytest.fixture
def api(tmp_path: Path, fake_keyring) -> Api:
    paths = Paths.from_home(tmp_path / "home")
    paths.ensure_dirs()
    return Api(Services(paths, load_config(paths)))


def test_answer_autostart_yes_registers_and_remembers(api: Api, monkeypatch) -> None:
    calls: list[str] = []
    home = api._s.paths.home

    def enable(argv=None, *, home=None):
        calls.append("enable")
        return autostart.AutostartStatus(True, "Task Scheduler", home / "ChatForge.vbs")

    monkeypatch.setattr(autostart, "enable", enable)
    reply = api.answer_autostart(True)
    assert reply["ok"] and reply["enabled"] is True and calls == ["enable"]
    assert (api._s.config.startup.autostart, api._s.config.startup.autostart_asked) == (True, True)
    assert home.exists()


def test_answer_autostart_no_registers_nothing_and_remembers(api: Api, monkeypatch) -> None:
    monkeypatch.setattr(autostart, "enable", lambda *a, **k: pytest.fail("must not register"))
    monkeypatch.setattr(
        autostart, "status", lambda: autostart.AutostartStatus(False, "Task Scheduler", None)
    )
    monkeypatch.setattr(autostart, "disable", lambda *a, **k: pytest.fail("nothing to remove"))
    reply = api.answer_autostart(False)
    assert reply["ok"] and reply["enabled"] is False
    assert (api._s.config.startup.autostart, api._s.config.startup.autostart_asked) == (False, True)


def test_get_state_carries_the_setup_and_autostart_flags(api: Api) -> None:
    class Setup:
        def snapshot(self) -> dict[str, Any]:
            return {"phase": "idle", "needed": True}

    api._s.setup = Setup()
    st = api.get_state()
    assert st["setup"]["needed"] is True
    assert st["setup"]["autostart_asked"] is False and st["setup"]["autostart_enabled"] is False


def test_setup_methods_answer_cleanly_without_the_service(api: Api) -> None:
    assert api.get_state()["setup"] is None
    for call in (api.setup_local_status, api.setup_local_start, api.setup_local_cancel):
        reply = call()
        assert reply["ok"] is False and reply["error"]["code"] == "server"


def test_the_new_methods_are_in_the_contract() -> None:
    for name in (
        "setup_local_status",
        "setup_local_start",
        "setup_local_cancel",
        "setup_local_dismiss",
        "answer_autostart",
        "get_extensions",
        "set_extension_settings",
    ):
        assert name in CONTRACT_METHODS


# --------------------------------------------------------------------------- NPU probe


@pytest.fixture(autouse=True)
def _fresh_probe():
    npu_detect.reset()
    yield
    npu_detect.reset()


def test_classify_reads_the_pnp_lines() -> None:
    assert npu_detect.classify(["Intel(R) AI Boost [OK]"]).present is True
    assert npu_detect.classify([]).present is False
    broken = npu_detect.classify(["Intel(R) AI Boost [Error]"])
    assert broken.present is False and "driver" in broken.reason
    assert npu_detect.classify(None).present is None


def test_only_a_finished_probe_that_found_nothing_counts_as_absent(monkeypatch) -> None:
    from chatforge import doctor

    assert npu_detect.absent() is False  # not probed yet: unknown, assume present
    monkeypatch.setattr(doctor, "list_npu_devices", lambda timeout_s=20.0: None)
    assert npu_detect.probe().present is None
    assert npu_detect.absent() is False  # a failed query never moves anyone off the NPU
    assert npu_detect.cached() is None  # and is not cached, so the next call asks again
    monkeypatch.setattr(doctor, "list_npu_devices", lambda timeout_s=20.0: [])
    assert npu_detect.probe().present is False
    assert npu_detect.absent() is True
    assert "no Intel NPU" in npu_detect.absence_reason()


def test_the_launch_path_uses_the_probe_on_windows(monkeypatch) -> None:
    from chatforge.runtime import ovms_supervisor as sup

    monkeypatch.setattr(sup, "is_windows_host", lambda: True)
    assert sup.npu_unavailable_on_host("NPU") is False
    monkeypatch.setattr(npu_detect, "_cached", npu_detect.NpuProbe(False, "no Intel NPU found"))
    assert sup.npu_unavailable_on_host("NPU") is True
    assert sup.npu_unavailable_on_host("CPU") is False
    assert "slower" in sup.npu_absence_reason()
    monkeypatch.setattr(npu_detect, "_cached", npu_detect.NpuProbe(True, "present"))
    assert sup.npu_unavailable_on_host("NPU") is False


# --------------------------------------------------------------------------- example config


def test_config_example_loads_and_matches_the_defaults_it_documents(chatforge_home: Path) -> None:
    raw = tomllib.loads((ROOT / "config.example.toml").read_text(encoding="utf-8"))
    cfg = validate_config(raw)
    assert isinstance(cfg, AppConfig)
    assert cfg.schema_version == chatforge.config.SCHEMA_VERSION
    assert cfg.startup.autostart is False
    # Every key the example sets exists in the model (no typo that silently does nothing).
    assert chatforge.config.find_unknown_keys(raw) == []
    assert to_toml_dict(cfg)["chat"]["model"] == raw["chat"]["model"]


def test_wait_returns_once_a_probe_has_finished(monkeypatch) -> None:
    from chatforge import doctor

    monkeypatch.setattr(doctor, "list_npu_devices", lambda timeout_s=20.0: [])
    assert npu_detect.wait(5.0) is True  # starts the probe itself and waits for it
    assert npu_detect.absent() is True
    assert npu_detect.wait(0.0) is True  # already done: no waiting


def test_a_failed_probe_still_releases_the_waiters(monkeypatch) -> None:
    from chatforge import doctor

    monkeypatch.setattr(doctor, "list_npu_devices", lambda timeout_s=20.0: None)
    assert npu_detect.wait(5.0) is True  # finished (without an answer): do not wait forever
    assert npu_detect.absent() is False


async def test_the_first_load_waits_for_the_probe_on_windows(monkeypatch) -> None:
    """The cold-start race seen on a real PC: a warm-up load began before the probe finished and
    launched OVMS for an NPU that is not there."""
    from chatforge.runtime import manager as manager_mod
    from chatforge.runtime import ovms_supervisor as sup

    calls: list[float] = []
    monkeypatch.setattr(manager_mod, "is_windows_host", lambda: True)
    monkeypatch.setattr(npu_detect, "wait", lambda timeout_s=15.0: calls.append(timeout_s) or True)
    mgr = manager_mod.LocalModelManager.__new__(manager_mod.LocalModelManager)
    mgr._get_config = lambda: type("C", (), {"local": type("L", (), {"device": "NPU"})()})()
    await mgr._settle_device()
    assert calls == [manager_mod.NPU_PROBE_WAIT_S]
    calls.clear()
    mgr._get_config = lambda: type("C", (), {"local": type("L", (), {"device": "CPU"})()})()
    await mgr._settle_device()
    assert calls == []  # an explicit CPU/GPU choice is not delayed
    assert sup is not None


def test_autostart_no_record_leaves_no_data_folder(monkeypatch, tmp_path: Path, capsys) -> None:
    """The uninstaller asks the app to remove the sign-in entry; that must not create a data
    folder (a test run once left a stray ChatForge folder in LOCALAPPDATA after an uninstall)."""
    home = tmp_path / "never-created"
    monkeypatch.setenv("CHATFORGE_HOME", str(home))
    monkeypatch.setattr(
        autostart,
        "disable",
        lambda *a, **k: autostart.AutostartStatus(False, "Task Scheduler", None),
    )
    assert cli.main(["autostart", "disable", "--no-record"]) == 0
    assert not home.exists()
    assert "autostart" in capsys.readouterr().out
