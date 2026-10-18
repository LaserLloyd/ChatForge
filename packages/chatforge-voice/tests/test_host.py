"""The HostLink subclass: ChatForge's folders, settings and credential store."""

from __future__ import annotations

from pathlib import Path

import pytest
from conftest import make_settings
from forge_voice_core import fetch_job, hostlink, models, profile_bridge, rig

from chatforge_voice.host import ChatForgeHost
from chatforge_voice.secrets_store import KeyringSecretStore


class FakeKeyring:
    def __init__(self):
        self.store = {}

    def get_password(self, service, name):
        return self.store.get((service, name))

    def set_password(self, service, name, value):
        self.store[(service, name)] = value

    def delete_password(self, service, name):
        if (service, name) not in self.store:
            raise KeyError(name)
        del self.store[(service, name)]


@pytest.fixture
def host(tmp_path):
    kr = FakeKeyring()
    h = ChatForgeHost(tmp_path / "voice", make_settings, KeyringSecretStore(backend=kr))
    h.kr = kr
    hostlink.install(h)
    return h


def test_every_folder_is_under_the_addons_data_dir(host, tmp_path):
    base = tmp_path / "voice"
    assert host.data_dir() == base
    assert host.models_root() == base / "models" and host.voices_dir() == base / "voices"
    assert host.state_dir() == base / "state" and host.has_state_dir()
    assert host.legacy_token_path().parent == base / "state"  # never the home-folder default
    assert models.root() == base / "models" and models.speech_dir() == base / "models" / "speech"
    assert models.chatterbox_dir() == base / "models" / "chatterbox"


def test_an_explicit_models_folder_wins(host, tmp_path, monkeypatch):
    monkeypatch.setenv("CHATFORGE_VOICE_MODELS_DIR", str(tmp_path / "big-disk"))
    assert models.root() == tmp_path / "big-disk"


def test_identity_and_unused_hooks(host):
    assert host.client_name == "chatforge-voice" and host.client_version
    assert host.power() is None and host.router_mounted() is False and host.bot_voice("x") == ""
    assert fetch_job._router_mounted() is False


def test_settings_and_schema_come_from_the_addon(host):
    s = hostlink.get().load_settings()
    assert s.enabled is True and s.switched_on
    ids = {r["id"] for r in host.settings_schema_json()["settings"]}
    assert {"enabled", "hotkey", "stt_provider", "rt.stt_engine"} <= ids


def test_voices_are_stored_in_the_addons_folder(host, tmp_path):
    assert Path(profile_bridge.store().root) == tmp_path / "voice" / "voices"


def test_the_rig_token_goes_to_the_credential_store_and_no_file_is_written(
    host, tmp_path, monkeypatch
):
    monkeypatch.delenv(rig.TOKEN_ENV, raising=False)
    rig.write_token("sekret-token-123")
    assert any(service == "ChatForge Voice" for service, _name in host.kr.store)
    assert rig.read_token().value == "sekret-token-123"
    assert not list((tmp_path / "voice" / "state").glob("*.env"))
    assert rig.clear_token() and rig.read_token().value == ""


def test_a_broken_credential_store_reads_as_empty_and_set_reports_failure():
    class Broken:
        def get_password(self, *a):
            raise RuntimeError("locked")

        def set_password(self, *a):
            raise RuntimeError("locked sekret")

        def delete_password(self, *a):
            raise RuntimeError("locked")

    s = KeyringSecretStore(backend=Broken())
    assert s.get("x") is None
    s.delete("x")
    with pytest.raises(RuntimeError) as e:
        s.set("x", "sekret")
    assert "sekret" not in str(e.value)
