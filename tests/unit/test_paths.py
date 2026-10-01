"""paths.py: data locations."""

from pathlib import Path

from chatforge.paths import Paths, app_home


def test_app_home_uses_env_override(chatforge_home):
    assert app_home() == chatforge_home.resolve()


def test_app_home_falls_back_to_localappdata(monkeypatch, tmp_path):
    monkeypatch.delenv("CHATFORGE_HOME", raising=False)
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path))
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path))
    home = app_home()
    assert home.name == "ChatForge"
    assert home.parent == tmp_path


def test_blank_env_is_ignored(monkeypatch, tmp_path):
    monkeypatch.setenv("CHATFORGE_HOME", "   ")
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path))
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path))
    assert app_home() == tmp_path / "ChatForge"


def test_paths_layout(chatforge_home):
    paths = Paths.default()
    home = chatforge_home.resolve()
    assert paths.home == home
    assert paths.config_file == home / "config.toml"
    assert paths.state_file == home / "state.json"
    assert paths.conversation_file == home / "conversation.json"
    assert paths.downloads_file == home / "downloads.json"
    assert paths.models_dir == home / "models"
    assert paths.runtime_dir == home / "runtime"
    assert paths.cache_dir == home / "cache"
    assert paths.logs_dir == home / "logs"
    assert paths.webview_dir == home / "webview"
    assert paths.app_log == home / "logs" / "chatforge.log"
    assert paths.ovms_log == home / "logs" / "ovms.log"


def test_paths_are_frozen(chatforge_home):
    paths = Paths.default()
    try:
        paths.home = Path("x")  # type: ignore[misc]
    except AttributeError:
        return
    raise AssertionError("Paths must be immutable")


def test_ensure_dirs_creates_and_is_idempotent(tmp_path):
    paths = Paths.from_home(tmp_path / "fresh" / "ChatForge")
    paths.ensure_dirs()
    paths.ensure_dirs()
    for directory in (
        paths.home,
        paths.models_dir,
        paths.runtime_dir,
        paths.cache_dir,
        paths.logs_dir,
        paths.webview_dir,
    ):
        assert directory.is_dir()
    assert not paths.config_file.exists()
