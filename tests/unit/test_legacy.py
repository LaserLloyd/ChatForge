"""chatforge.legacy: an AI Chat install carried over to ChatForge."""

from __future__ import annotations

import json
import os
from pathlib import Path

import keyring
import pytest

from chatforge import legacy, secrets


def test_the_data_folder_is_renamed_once(tmp_path: Path) -> None:
    old = tmp_path / "AIChat"
    (old / "models").mkdir(parents=True)
    (old / "config.toml").write_text("[chat]\n", encoding="utf-8")
    home = tmp_path / "ChatForge"
    assert legacy.migrate_home(home) == home
    assert (home / "config.toml").read_text(encoding="utf-8") == "[chat]\n"
    assert (home / "models").is_dir() and not old.exists()
    # A later start leaves everything alone.
    old.mkdir()
    assert legacy.migrate_home(home) == home
    assert old.is_dir()


def test_nothing_to_move_without_an_old_folder(tmp_path: Path) -> None:
    home = tmp_path / "ChatForge"
    assert legacy.migrate_home(home) == home
    assert not home.exists()


def test_an_old_folder_that_cannot_move_is_used_in_place(tmp_path: Path, monkeypatch) -> None:
    old = tmp_path / "AIChat"
    old.mkdir()

    def locked(self: Path, target: Path) -> Path:
        raise PermissionError("in use")

    monkeypatch.setattr(Path, "rename", locked)
    assert legacy.migrate_home(tmp_path / "ChatForge") == old


def test_documents_move_and_the_saved_cards_follow(tmp_path: Path) -> None:
    old = tmp_path / "Documents" / "AI Chat"
    old.mkdir(parents=True)
    (old / "Plan.docx").write_bytes(b"docx")
    new = old.with_name("ChatForge")
    conversation = tmp_path / "conversation.json"
    saved = {
        "messages": [{"role": "tool", "content": json.dumps({"path": str(old / "Plan.docx")})}]
    }
    conversation.write_text(json.dumps(saved), encoding="utf-8")
    assert legacy.migrate_documents(new, conversation) is True
    assert (new / "Plan.docx").read_bytes() == b"docx" and not old.exists()
    content = json.loads(conversation.read_text(encoding="utf-8"))["messages"][0]["content"]
    assert json.loads(content)["path"] == str(new / "Plan.docx")
    assert legacy.migrate_documents(new, conversation) is False


def test_a_saved_key_moves_to_the_new_entry(fake_keyring) -> None:
    keyring.set_password(legacy.LEGACY_KEY_SERVICE, "minimax", "sk-old-key-0123456789")
    value, source = secrets.get_api_key("minimax", None)
    assert (value, source) == ("sk-old-key-0123456789", "keyring")
    assert keyring.get_password(secrets.SERVICE, "minimax") == "sk-old-key-0123456789"
    assert keyring.get_password(legacy.LEGACY_KEY_SERVICE, "minimax") is None
    assert secrets.SERVICE == "ChatForge"


def test_deleting_a_key_also_removes_the_old_copy(fake_keyring) -> None:
    keyring.set_password(legacy.LEGACY_KEY_SERVICE, "openai", "sk-old-key-0123456789")
    secrets.delete_api_key("openai")
    assert keyring.get_password(legacy.LEGACY_KEY_SERVICE, "openai") is None
    assert secrets.get_api_key("openai", None)[0] is None


@pytest.mark.skipif(os.name != "nt", reason="Task Scheduler")
def test_the_sign_in_task_is_replaced(tmp_path: Path, monkeypatch) -> None:
    from chatforge import autostart

    calls: list[tuple[str, ...]] = []
    enabled: list[Path] = []
    (tmp_path / "AIChat.vbs").write_text("old", encoding="utf-8")

    def schtasks(*args: str) -> tuple[int, str]:
        calls.append(args)
        return 0, ""

    monkeypatch.setattr(autostart, "_schtasks", schtasks)
    monkeypatch.setattr(autostart, "enable", lambda home=None, **kw: enabled.append(home))
    assert legacy.migrate_autostart(tmp_path) is True
    assert enabled == [tmp_path]
    assert ("/Delete", "/TN", "AI Chat", "/F") in calls
    assert not (tmp_path / "AIChat.vbs").exists()


@pytest.mark.skipif(os.name != "nt", reason="Task Scheduler")
def test_no_old_task_means_no_change(tmp_path: Path, monkeypatch) -> None:
    from chatforge import autostart

    monkeypatch.setattr(autostart, "_schtasks", lambda *a: (1, "ERROR: not found"))
    monkeypatch.setattr(autostart, "enable", lambda **kw: pytest.fail("enabled"))
    assert legacy.migrate_autostart(tmp_path) is False
