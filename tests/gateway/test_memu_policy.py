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
