from types import SimpleNamespace
from unittest.mock import patch

import subprocess

from gateway.config import Platform, PlatformConfig, load_gateway_config
from gateway.platforms.whatsapp import WhatsAppAdapter
from gateway.status import read_runtime_status


def test_whatsapp_web_source_defaults_enabled_headless():
    adapter = WhatsAppAdapter(PlatformConfig(enabled=True, extra={}))

    assert adapter._web_source_enabled is True
    assert adapter._web_source_headful is False


def test_whatsapp_web_source_config_bridged_from_yaml(tmp_path, monkeypatch):
    config_yaml = tmp_path / "config.yaml"
    config_yaml.write_text(
        "whatsapp:\n"
        "  web_source_enabled: true\n"
        "  web_source_db: /tmp/web-source.db\n"
        "  web_source_chromium_path: /usr/bin/chromium\n"
        "  web_source_contact_snapshot_interval: 30\n",
        encoding="utf-8",
    )
    with patch("gateway.config.get_hermes_home", return_value=tmp_path):
        with patch.dict("os.environ", {"WHATSAPP_ENABLED": "true"}, clear=False):
            config = load_gateway_config()

    extra = config.platforms[Platform.WHATSAPP].extra
    assert extra["web_source_enabled"] is True
    assert extra["web_source_db"] == "/tmp/web-source.db"
    assert extra["web_source_chromium_path"] == "/usr/bin/chromium"
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
                "web_source_chromium_path": "/usr/bin/chromium",
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
    assert popen.call_args.kwargs["env"]["PUPPETEER_EXECUTABLE_PATH"] == "/usr/bin/chromium"


def test_whatsapp_web_source_pairing_restarts_headful(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / "home"))
    script = tmp_path / "source-daemon.js"
    script.write_text("'use strict';\n", encoding="utf-8")
    (tmp_path / "node_modules").mkdir()
    status_path = tmp_path / "source-status.json"

    class ExitingProcess:
        pid = 1
        returncode = None

        def poll(self):
            return self.returncode

        def wait(self, timeout=None):
            self.returncode = 0
            return self.returncode

    proc1 = ExitingProcess()
    proc2 = SimpleNamespace(pid=2, poll=lambda: None)
    adapter = WhatsAppAdapter(
        PlatformConfig(
            enabled=True,
            extra={
                "web_source_enabled": True,
                "web_source_script": str(script),
                "web_source_status": str(status_path),
                "web_source_auth": str(tmp_path / "auth"),
            },
        )
    )
    adapter._running = True
    adapter._http_session = object()
    adapter._bridge_health = {"status": "connected", "mode": "bot"}

    with patch("subprocess.Popen", side_effect=[proc1, proc2]) as popen, \
         patch("gateway.platforms.whatsapp._terminate_bridge_process"):
        assert adapter._start_web_source() is True
        status_path.write_text('{"state":"pairing"}', encoding="utf-8")
        adapter._check_web_source_exit()

    assert adapter._web_source_pairing_headful is True
    assert "--headful" not in popen.call_args_list[0].args[0]
    assert "--headful" in popen.call_args_list[1].args[0]


def test_whatsapp_web_source_pairing_does_not_duplicate_when_stop_fails(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / "home"))
    script = tmp_path / "source-daemon.js"
    script.write_text("'use strict';\n", encoding="utf-8")
    (tmp_path / "node_modules").mkdir()
    status_path = tmp_path / "source-status.json"

    class StubbornProcess:
        pid = 1

        def poll(self):
            return None

        def wait(self, timeout=None):
            raise subprocess.TimeoutExpired("node", timeout)

    adapter = WhatsAppAdapter(
        PlatformConfig(
            enabled=True,
            extra={
                "web_source_enabled": True,
                "web_source_script": str(script),
                "web_source_status": str(status_path),
                "web_source_auth": str(tmp_path / "auth"),
            },
        )
    )
    adapter._running = True
    adapter._http_session = object()
    adapter._bridge_health = {"status": "connected", "mode": "bot"}

    with patch("subprocess.Popen", return_value=StubbornProcess()) as popen, \
         patch("gateway.platforms.whatsapp._terminate_bridge_process"):
        assert adapter._start_web_source() is True
        status_path.write_text('{"state":"pairing"}', encoding="utf-8")
        adapter._check_web_source_exit()

    assert popen.call_count == 1
    assert adapter._web_source_pairing_headful is False
    assert adapter._web_source_process is not None
    assert "could not stop cleanly" in adapter._web_source_error
    whatsapp = read_runtime_status()["platforms"]["whatsapp"]
    assert whatsapp["web_source"]["state"] == "degraded"


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
