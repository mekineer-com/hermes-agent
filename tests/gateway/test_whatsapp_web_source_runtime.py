from types import SimpleNamespace
from unittest.mock import patch

from gateway.config import Platform, PlatformConfig, load_gateway_config
from gateway.platforms.whatsapp import WhatsAppAdapter
from gateway.status import read_runtime_status


def test_whatsapp_web_source_config_bridged_from_yaml(tmp_path, monkeypatch):
    config_yaml = tmp_path / "config.yaml"
    config_yaml.write_text(
        "whatsapp:\n"
        "  web_source_enabled: true\n"
        "  web_source_db: /tmp/web-source.db\n"
        "  web_source_contact_snapshot_interval: 30\n",
        encoding="utf-8",
    )
    with patch("gateway.config.get_hermes_home", return_value=tmp_path):
        with patch.dict("os.environ", {"WHATSAPP_ENABLED": "true"}, clear=False):
            config = load_gateway_config()

    extra = config.platforms[Platform.WHATSAPP].extra
    assert extra["web_source_enabled"] is True
    assert extra["web_source_db"] == "/tmp/web-source.db"
    assert extra["web_source_contact_snapshot_interval"] == 30


def test_whatsapp_web_source_command_uses_configured_paths(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / "home"))
    script = tmp_path / "source-daemon.js"
    script.write_text("'use strict';\n", encoding="utf-8")
    (tmp_path / "node_modules").mkdir()
    db_path = tmp_path / "projection.db"
    status_path = tmp_path / "source-status.json"
    auth_path = tmp_path / "auth"
    proc = SimpleNamespace(pid=12345, poll=lambda: None)

    adapter = WhatsAppAdapter(
        PlatformConfig(
            enabled=True,
            extra={
                "web_source_enabled": True,
                "web_source_script": str(script),
                "web_source_db": str(db_path),
                "web_source_status": str(status_path),
                "web_source_auth": str(auth_path),
                "web_source_client_id": "siri-source",
                "web_source_backfill_limit": 25,
                "web_source_contact_snapshot_interval": 60,
            },
        )
    )
    adapter._running = True
    adapter._http_session = object()
    adapter._bridge_health = {"status": "connected", "mode": "bot"}

    with patch("subprocess.Popen", return_value=proc) as popen:
        assert adapter._start_web_source() is True

    command = popen.call_args.args[0]
    assert command[:2] == ["node", str(script)]
    assert command[command.index("--db") + 1] == str(db_path)
    assert command[command.index("--status") + 1] == str(status_path)
    assert command[command.index("--auth") + 1] == str(auth_path)
    assert command[command.index("--client-id") + 1] == "siri-source"
    assert command[command.index("--backfill-limit") + 1] == "25"
    assert command[command.index("--contact-snapshot-interval") + 1] == "60"


def test_whatsapp_web_source_missing_script_marks_degraded(tmp_path, monkeypatch):
    hermes_home = tmp_path / "home"
    monkeypatch.setenv("HERMES_HOME", str(hermes_home))
    adapter = WhatsAppAdapter(
        PlatformConfig(
            enabled=True,
            extra={
                "web_source_enabled": True,
                "web_source_script": str(tmp_path / "missing.js"),
                "web_source_status": str(tmp_path / "status.json"),
            },
        )
    )
    adapter._running = True
    adapter._http_session = object()
    adapter._bridge_health = {"status": "connected", "mode": "bot"}

    assert adapter._start_web_source() is False

    state = read_runtime_status()
    whatsapp = state["platforms"]["whatsapp"]
    assert whatsapp["state"] == "degraded"
    assert whatsapp["bridge"]["state"] == "ready"
    assert whatsapp["web_source"]["state"] == "degraded"
    assert "missing.js" in whatsapp["web_source"]["error"]


def test_whatsapp_web_source_missing_dependencies_marks_degraded_without_install(tmp_path, monkeypatch):
    hermes_home = tmp_path / "home"
    monkeypatch.setenv("HERMES_HOME", str(hermes_home))
    script = tmp_path / "source-daemon.js"
    script.write_text("'use strict';\n", encoding="utf-8")
    adapter = WhatsAppAdapter(
        PlatformConfig(
            enabled=True,
            extra={
                "web_source_enabled": True,
                "web_source_script": str(script),
                "web_source_status": str(tmp_path / "status.json"),
            },
        )
    )
    adapter._running = True
    adapter._http_session = object()
    adapter._bridge_health = {"status": "connected", "mode": "bot"}

    with patch("subprocess.run") as mock_run:
        assert adapter._start_web_source() is False

    mock_run.assert_not_called()
    state = read_runtime_status()
    whatsapp = state["platforms"]["whatsapp"]
    assert whatsapp["state"] == "degraded"
    assert whatsapp["web_source"]["state"] == "degraded"
    assert "npm install" in whatsapp["web_source"]["error"]
