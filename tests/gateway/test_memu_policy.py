import json
from pathlib import Path

import pytest


@pytest.fixture()
def hermes_home(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    # Ensure gateway.memu_policy reads the per-test home, not the real ~/.hermes.
    from gateway import memu_policy as _module
    # Bust any module-level cache by re-importing lazily inside the function below.
    yield tmp_path


def _write_memu_json(hermes_home: Path, payload: dict) -> None:
    (hermes_home / "memu.json").write_text(json.dumps(payload), encoding="utf-8")


def test_whatsapp_channel_settings_merges_lid_and_phone_aliases(hermes_home):
    from gateway.memu_policy import whatsapp_channel_settings

    session_dir = hermes_home / "whatsapp" / "session"
    session_dir.mkdir(parents=True)
    (session_dir / "lid-mapping-16467326349.json").write_text(
        json.dumps("263801622552699"),
        encoding="utf-8",
    )
    (session_dir / "lid-mapping-263801622552699_reverse.json").write_text(
        json.dumps("16467326349"),
        encoding="utf-8",
    )
    _write_memu_json(
        hermes_home,
        {
            "whatsapp": {
                "channels": {
                    "16467326349@s.whatsapp.net": {
                        "policy": "listen_only",
                        "memorize": True,
                    },
                    "263801622552699@lid": {
                        "policy": "full",
                        "memorize": False,
                    },
                }
            }
        },
    )

    assert whatsapp_channel_settings("263801622552699@lid") == ("listen_only", True)


def test_whatsapp_channel_settings_excluded_alias_wins(hermes_home):
    from gateway.memu_policy import whatsapp_channel_settings

    session_dir = hermes_home / "whatsapp" / "session"
    session_dir.mkdir(parents=True)
    (session_dir / "lid-mapping-16467326349.json").write_text(
        json.dumps("263801622552699"),
        encoding="utf-8",
    )
    _write_memu_json(
        hermes_home,
        {
            "whatsapp": {
                "channels": {
                    "16467326349@s.whatsapp.net": {
                        "policy": "full",
                        "memorize": True,
                    },
                    "263801622552699@lid": {
                        "policy": "excluded",
                        "memorize": False,
                    },
                }
            }
        },
    )

    assert whatsapp_channel_settings("263801622552699@lid") == ("excluded", False)



def test_should_skip_soul_mode_auto_resume_uses_session_agent_name():
    from gateway.memu_policy import should_skip_soul_mode_auto_resume

    class _Gateway:
        def _read_user_config(self):
            return {
                "soul_mode": {
                    "agents": {
                        "main": {
                            "enabled": False,
                            "role": "standard",
                        },
                        "echo": {
                            "enabled": True,
                            "role": "soul",
                            "soul_id": "Echo",
                            "user_id": "marcos",
                        },
                    }
                }
            }

    assert should_skip_soul_mode_auto_resume(
        _Gateway(),
        "agent:echo:whatsapp:dm:15133278228",
    ) is True
    assert should_skip_soul_mode_auto_resume(
        _Gateway(),
        "agent:main:whatsapp:dm:15133278228",
    ) is False


def test_should_skip_soul_mode_auto_resume_logs_on_config_failure(caplog):
    from gateway.memu_policy import should_skip_soul_mode_auto_resume

    class _Gateway:
        def _read_user_config(self):
            raise RuntimeError("broken config")

    with caplog.at_level("WARNING"):
        out = should_skip_soul_mode_auto_resume(_Gateway(), "agent:main:whatsapp:dm:x")
    assert out is False
    assert "failed soul-mode resolve" in caplog.text
