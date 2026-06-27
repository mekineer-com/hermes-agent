from unittest.mock import patch

from gateway.config import Platform, load_gateway_config


def test_whatsapp_openalma_keys_bridge_from_yaml(tmp_path, monkeypatch):
    monkeypatch.setenv("WHATSAPP_ENABLED", "true")
    (tmp_path / "config.yaml").write_text(
        "whatsapp:\n"
        "  bridge_port: 3999\n"
        "  bridge_script: /tmp/bridge.js\n"
        "  session_path: /tmp/wa-session\n"
        "  max_message_age_seconds: 0\n"
        "  mode: bot\n"
        "  web_source_enabled: true\n"
        "  web_source_db: /tmp/web-source.db\n"
        "  web_source_chromium_path: /usr/bin/chromium\n"
        "  web_source_contact_snapshot_interval: 30\n"
        "  web_source_disable_service_workers: true\n"
        "  web_source_resource_block: false\n",
        encoding="utf-8",
    )

    with patch("gateway.config.get_hermes_home", return_value=tmp_path):
        config = load_gateway_config()

    extra = config.platforms[Platform.WHATSAPP].extra
    assert extra["bridge_port"] == 3999
    assert extra["bridge_script"] == "/tmp/bridge.js"
    assert extra["session_path"] == "/tmp/wa-session"
    assert extra["max_message_age_seconds"] == 0
    assert extra["mode"] == "bot"
    assert extra["web_source_enabled"] is True
    assert extra["web_source_db"] == "/tmp/web-source.db"
    assert extra["web_source_chromium_path"] == "/usr/bin/chromium"
    assert extra["web_source_contact_snapshot_interval"] == 30
    assert extra["web_source_disable_service_workers"] is True
    assert extra["web_source_resource_block"] is False
