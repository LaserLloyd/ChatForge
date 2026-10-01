"""doctor.py: the pure verdict functions, table rendering and exit code."""

from pathlib import Path
from types import SimpleNamespace

from chatforge import doctor
from chatforge.doctor import Check


def test_python_version_threshold():
    assert doctor.check_python((3, 12, 4)).status == "pass"
    assert doctor.check_python((3, 13, 0)).status == "pass"
    assert doctor.check_python((3, 11, 9)).status == "fail"


def test_parse_webview2_version():
    assert doctor.parse_webview2_version("141.0.3537.57") == "141.0.3537.57"
    assert doctor.parse_webview2_version(" 1.2.3 ") == "1.2.3"
    assert doctor.parse_webview2_version("0.0.0.0") is None
    assert doctor.parse_webview2_version("") is None
    assert doctor.parse_webview2_version(None) is None
    assert doctor.parse_webview2_version(42) is None


def test_read_webview2_version_walks_hives_and_wow6432node():
    seen: list[tuple[int, str]] = []

    def reader(hive: int, subkey: str, value: str) -> object:
        seen.append((hive, subkey))
        assert value == "pv"
        if hive == 0x80000001 and "WOW6432Node" in subkey:
            return "140.0.1.2"
        return None

    assert doctor.read_webview2_version(reader) == "140.0.1.2"
    assert len(seen) == 4  # HKLM native, HKLM wow, HKCU native, HKCU wow
    assert doctor.read_webview2_version(lambda *_a: None) is None
    assert doctor.read_webview2_version(lambda *_a: "0.0.0.0") is None


def test_webview2_and_vcredist_verdicts():
    assert doctor.check_webview2("141.0").status == "pass"
    assert doctor.check_webview2(None).status == "fail"
    assert doctor.check_vcredist(True, "v14.50.35719.00").detail == "v14.50.35719.00"
    assert doctor.check_vcredist(False, None).status == "fail"
    assert "winget" in doctor.check_vcredist(False, None).detail


def test_npu_verdicts():
    assert doctor.check_npu(["Intel(R) AI Boost [OK]"]).status == "pass"
    assert doctor.check_npu(["Some GPU"]).status == "fail"
    assert doctor.check_npu([]).status == "fail"
    assert doctor.check_npu(None).status == "warn"


def test_ovms_verdicts():
    assert doctor.check_ovms({"installed": False}).status == "fail"
    good = {
        "installed": True,
        "version": "2026.4.0",
        "variant": "python_on",
        "wanted_variant": "python_on",
        "variant_matches": True,
        "exe": r"C:\x\ovms.exe",
    }
    check = doctor.check_ovms(good)
    assert check.status == "pass" and "2026.4.0" in check.detail
    assert doctor.check_ovms({**good, "variant_matches": False}).status == "warn"


def test_model_verdicts():
    mid = "OpenVINO/Qwen2.5-1.5B-Instruct-int4-ov"
    assert doctor.check_model(mid, None).status == "fail"
    partial = SimpleNamespace(complete=False, missing=["openvino_model.bin"], size_bytes=1)
    check = doctor.check_model(mid, partial)
    assert check.status == "fail" and "openvino_model.bin" in check.detail
    full = SimpleNamespace(complete=True, missing=[], size_bytes=935_999_480, source="adopted")
    check = doctor.check_model(mid, full)
    assert check.status == "pass" and "0.94 GB" in check.detail and "adopted" in check.detail


def test_compiled_verdicts():
    mid = "OpenVINO/Qwen2.5-1.5B-Instruct-int4-ov"
    warm = doctor.check_compiled(mid, "NPU", True, 3.0)
    assert warm.status == "pass" and "~3 s" in warm.detail
    cold = doctor.check_compiled(mid, "NPU", False, 45.2)
    assert cold.status == "warn" and "~45 s" in cold.detail


def test_keyring_and_minimax_verdicts():
    assert doctor.check_keyring("keyring.backends.Windows.WinVaultKeyring", True).status == "pass"
    assert doctor.check_keyring("keyring.backends.fail.Keyring", False).status == "fail"
    assert doctor.check_minimax_key(False, False).status == "warn"
    assert doctor.check_minimax_key(True, False).status == "pass"
    assert doctor.check_minimax_key(False, True).status == "pass"
    detail = doctor.check_minimax_key(True, True).detail
    assert "True" in detail and "sk-" not in detail  # booleans only, never a value


def test_hotkey_verdicts():
    assert doctor.check_hotkey("Ctrl+Alt+C", "ok", False).status == "pass"
    assert doctor.check_hotkey("Ctrl+Alt+C", "in_use", True).status == "pass"
    assert doctor.check_hotkey("Ctrl+Alt+C", "in_use", False).status == "fail"
    assert doctor.check_hotkey("Ctrl+Alt+C", "invalid:unknown key", False).status == "fail"
    assert doctor.check_hotkey("Ctrl+Alt+C", "error:Windows error 5", False).status == "warn"


def test_autostart_verdicts(tmp_path):
    exe = r"C:\Repo\.venv\Scripts\pythonw.exe"
    path = tmp_path / "ChatForge.vbs"
    assert doctor.check_autostart(False, None, "", exe).status == "warn"
    good = doctor.check_autostart(True, path, f'shell.Run "{exe} -m chatforge --hidden"', exe)
    assert good.status == "pass" and str(path) in good.detail
    wrong_exe = doctor.check_autostart(
        True, path, 'shell.Run "D:\\other\\pythonw.exe --hidden"', exe
    )
    assert wrong_exe.status == "warn" and "point at" in wrong_exe.detail
    no_hidden = doctor.check_autostart(True, path, f'shell.Run "{exe} -m chatforge"', exe)
    assert no_hidden.status == "warn" and "--hidden" in no_hidden.detail


def test_autostart_task_scheduler_verdicts(tmp_path):
    exe = r"C:\Repo\.venv\Scripts\pythonw.exe"
    script = tmp_path / "ChatForge.vbs"
    text = f'shell.Run "{exe} -m chatforge --hidden"'
    good = doctor.check_autostart(True, script, text, exe, mechanism="Task Scheduler")
    assert good.status == "pass"
    assert good.detail == f"enabled via Task Scheduler ({script})"
    shim = tmp_path / "Startup" / "ChatForge.vbs"
    twice = doctor.check_autostart(
        True, script, text, exe, mechanism="Task Scheduler", duplicate=shim
    )
    assert twice.status == "warn" and str(shim) in twice.detail and "twice" in twice.detail
    missing = doctor.check_autostart(True, script, "", exe, mechanism="Task Scheduler")
    assert missing.status == "warn" and "missing" in missing.detail
    assert "point at" not in missing.detail  # one clear problem, not three


def test_autostart_verdict_reads_the_task_status(tmp_path):
    from chatforge import autostart

    script = tmp_path / "ChatForge.vbs"
    exe = autostart.launch_argv()[0]
    autostart._write_utf16(script, f'shell.Run "{exe} -m chatforge --hidden"\n')
    status = autostart.AutostartStatus(True, "Task Scheduler", script)
    good = doctor.autostart_verdict(status)
    assert good.status == "pass" and good.detail == f"enabled via Task Scheduler ({script})"
    shim = tmp_path / "Startup" / "ChatForge.vbs"
    twice = doctor.autostart_verdict(
        autostart.AutostartStatus(True, "Task Scheduler", script, duplicate=shim)
    )
    assert twice.status == "warn" and str(shim) in twice.detail


def test_disk_verdicts():
    home = Path(r"C:\Users\x\AppData\Local\ChatForge")
    assert doctor.check_disk(None, home).status == "warn"
    assert doctor.check_disk(1 * 1024**3, home).status == "fail"
    assert doctor.check_disk(5 * 1024**3, home).status == "warn"
    check = doctor.check_disk(400 * 1024**3, home)
    assert check.status == "pass" and "400.0 GB" in check.detail


def test_app_running_verdicts():
    assert doctor.check_app_running(True, 47831, []).status == "pass"
    assert "47831" in doctor.check_app_running(True, 47831, []).detail
    assert doctor.check_app_running(True, 47831, [1234]).status == "pass"
    assert doctor.check_app_running(False, 47831, []).status == "pass"
    stray = doctor.check_app_running(False, 47831, [1234])
    assert stray.status == "warn" and "1234" in stray.detail


def test_render_table_and_exit_code():
    checks = [
        Check("Python", "pass", "3.12.4"),
        Check("Free disk", "warn", "5 GB"),
        Check("OVMS runtime", "fail", "not installed"),
    ]
    text = doctor.render_table(checks)
    lines = text.splitlines()
    assert lines[0].startswith("CHECK")
    assert any(line.startswith("Python") and "PASS" in line for line in lines)
    assert any("WARN" in line and "5 GB" in line for line in lines)
    assert lines[-1] == "3 checks: 1 pass, 1 warn, 1 fail"
    assert doctor.exit_code(checks) == 1
    assert doctor.exit_code(checks[:2]) == 0
    assert doctor.exit_code([]) == 0


def test_safe_wraps_a_broken_probe():
    def boom() -> Check:
        raise RuntimeError("no registry")

    check = doctor._safe("WebView2 runtime", boom)
    assert check.status == "warn" and "RuntimeError" in check.detail


def test_load_config_readonly_never_writes(chatforge_home):
    from chatforge.paths import Paths

    paths = Paths.from_home(chatforge_home)
    cfg, check = doctor.load_config_readonly(paths)
    assert check.status == "warn" and not paths.config_file.exists()
    assert cfg.chat.provider == "local-npu"

    paths.config_file.write_text("[ui]\nhotkey = 'nope'\n", encoding="utf-8")
    cfg, check = doctor.load_config_readonly(paths)
    assert check.status == "fail" and cfg.ui.hotkey == "Ctrl+Alt+C"
    assert paths.config_file.exists() and not paths.config_file.with_suffix(".toml.bad").exists()

    paths.config_file.write_text("[ui]\nhotkey = 'Ctrl+Alt+K'\n", encoding="utf-8")
    cfg, check = doctor.load_config_readonly(paths)
    assert check.status == "pass" and cfg.ui.hotkey == "Ctrl+Alt+K"


def test_main_hook_is_wired(monkeypatch, capsys):
    from chatforge import __main__ as cli

    monkeypatch.setattr(doctor, "run_checks", lambda paths=None, cfg=None: [Check("X", "pass")])
    assert cli.main(["doctor"]) == 0
    assert "1 checks: 1 pass, 0 warn, 0 fail" in capsys.readouterr().out
    monkeypatch.setattr(doctor, "run_checks", lambda paths=None, cfg=None: [Check("X", "fail")])
    assert cli.main(["doctor"]) == 1


def test_compiled_models_verdicts():
    states = [
        doctor.CompileState("OpenVINO/Qwen2.5-1.5B-Instruct-int4-ov", "NPU", True, 11.1),
        doctor.CompileState("OpenVINO/Qwen3-4B-int4-ov", "GPU", False, 122.7),
    ]
    cold = doctor.check_compiled_models(states)
    assert cold.status == "warn" and cold.ok
    assert "Qwen3-4B-int4-ov on GPU ~123 s" in cold.detail and "background" in cold.detail
    assert "Qwen2.5" not in cold.detail
    off = doctor.check_compiled_models(states, precompile=False)
    assert "local.precompile is off" in off.detail
    warm = doctor.check_compiled_models(states[:1])
    assert warm.status == "pass" and "Qwen2.5-1.5B-Instruct-int4-ov on NPU ~11 s" in warm.detail
    assert doctor.check_compiled_models([]).status == "pass"


def test_npu_models_verdicts():
    from chatforge.models.npu_compat import NpuVerdict

    good = ("OpenVINO/Qwen2.5-1.5B-Instruct-int4-ov", NpuVerdict(True))
    bad = ("OpenVINO/Qwen3-4B-int4-ov", NpuVerdict(False, "Garbage on the NPU.", "avoid"))
    unknown = ("acme/x-ov", NpuVerdict(None, "no info"))
    check = doctor.check_npu_models("NPU", [good, bad])
    assert check.status == "warn" and "Qwen3-4B-int4-ov: Garbage on the NPU." in check.detail
    assert "runs on GPU instead" in check.detail
    assert "refused" in doctor.check_npu_models("NPU", [bad], "none").detail
    passing = doctor.check_npu_models("NPU", [good, unknown])
    assert passing.status == "pass" and "1 not verifiable" in passing.detail
    assert doctor.check_npu_models("GPU", [bad]).status == "pass"  # not on the NPU at all


def test_local_target_model():
    assert doctor.local_target_model("a/x", ["a/x", "b/y"]) == "a/x"
    assert doctor.local_target_model("MiniMax-M3", ["a/x", "b/y"]) == "a/x"
    assert doctor.local_target_model("MiniMax-M3", []) is None


def _install(models_dir: Path, mid: str) -> Path:
    from chatforge.models.registry import REQUIRED_FILES

    d = models_dir.joinpath(*mid.split("/"))
    d.mkdir(parents=True)
    for name in REQUIRED_FILES:
        (d / name).write_bytes(b"x")
    return d


def test_local_model_checks_with_a_cloud_provider_selected(tmp_path):
    from chatforge.config import validate_config
    from chatforge.models.catalog import Catalog
    from chatforge.paths import Paths
    from chatforge.runtime import compile_cache

    paths = Paths.from_home(tmp_path / "home")
    good, bad = "Test/Good-int4-ov", "Test/Broken-int4-ov"
    for mid in (good, bad):
        _install(paths.models_dir, mid)
    catalog = Catalog.from_dict(
        {
            "model": [{"id": good, "label": "G", "npu": "recommended"}],
            "avoid": [{"pattern": "Broken", "reason": "Garbage on the NPU."}],
        }
    )
    cfg = validate_config({"chat": {"provider": "minimax", "model": "MiniMax-M3"}})
    # A warm NPU cache for the good model, written with the exact compile settings.
    from chatforge.runtime.manager import LocalModelManager

    planner = LocalModelManager(
        None, get_config=lambda: cfg, paths=paths, catalog=catalog, precompile=False
    )
    spec = planner.build_spec(good)
    compile_cache.mark_compiled(
        spec.cache_dir,
        model_id=good,
        device="NPU",
        max_prompt_len=4096,
        ovms_version=cfg.local.ovms_version,
        load_s=66.0,
        compile_hash=planner.compile_hash(spec),
    )
    (spec.cache_dir / "1.blob").write_bytes(b"b")

    model, npu, compiled = doctor.local_model_checks(paths, cfg, catalog=catalog)
    # Not "MiniMax-M3 is not on disk": the cloud model is not a local model.
    assert model.status == "pass" and model.detail.startswith("Test/Broken-int4-ov")
    assert npu.status == "warn" and "runs on GPU instead" in npu.detail
    assert compiled.status == "warn"
    assert "Broken-int4-ov on GPU" in compiled.detail and "Good-int4-ov" not in compiled.detail

    empty = Paths.from_home(tmp_path / "empty")
    model, npu, compiled = doctor.local_model_checks(empty, cfg, catalog=catalog)
    assert model.status == "pass" and "no local model installed" in model.detail
    assert compiled.status == "pass" and npu.status == "pass"
