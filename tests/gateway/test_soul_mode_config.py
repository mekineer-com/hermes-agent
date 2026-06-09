import json
import sys
import types

import pytest

if "yaml" not in sys.modules:
    _yaml = types.ModuleType("yaml")
    _yaml.safe_load = lambda value, *args, **kwargs: json.loads(value) if isinstance(value, str) and value.strip().startswith("{") else {}
    _yaml.safe_dump = lambda value, *args, **kwargs: json.dumps(value)
    sys.modules["yaml"] = _yaml
if "dotenv" not in sys.modules:
    _dotenv = types.ModuleType("dotenv")
    _dotenv.load_dotenv = lambda *args, **kwargs: None
    sys.modules["dotenv"] = _dotenv

from gateway.run import GatewayRunner
from gateway.config import Platform
from gateway.platforms.base import SendResult


def test_resolve_soul_mode_agent_config_defaults_when_missing():
    out = GatewayRunner._resolve_soul_mode_agent_config({}, "agent:main:telegram:dm:123")
    assert out["enabled"] is False
    assert out["role"] == "standard"
    assert out["soul_id"] == ""
    assert out["user_id"] == ""
    assert out["memu_base_url"] == "http://127.0.0.1:8099"


def test_resolve_soul_mode_agent_config_reads_main_agent():
    cfg = {
        "whatsapp": {
            "reply_prefix": "✦ *Echo*: ",
        },
        "soul_mode": {
            "agents": {
                "main": {
                    "enabled": True,
                    "role": "soul",
                    "soul_id": "Echo",
                    "user_id": "marcos",
                    "memu_base_url": "http://127.0.0.1:8099",
                    "use_memu_turn": True,
                    "timeout_seconds": 12,
                    # WhatsApp history source is owned by mcp-memu-server.
                    # Legacy Hermes-side keys are ignored if still present.
                    "whatsapp_history_source": "web_source",
                    "whatsapp_web_source_db": "/tmp/web_source.db",
                    "whatsapp_history_limit": 42,
                }
            }
        }
    }
    out = GatewayRunner._resolve_soul_mode_agent_config(cfg, "agent:main:telegram:dm:123")
    assert out["enabled"] is True
    assert out["role"] == "soul"
    assert out["soul_id"] == "Echo"
    assert out["user_id"] == "marcos"
    assert out["timeout_seconds"] == 12.0
    assert "whatsapp_history_source" not in out
    assert "whatsapp_web_source_db" not in out
    assert "whatsapp_history_limit" not in out
    assert out["whatsapp_reply_prefix"] == "✦ *Echo*: "


def test_resolve_soul_mode_agent_config_is_explicit_per_agent():
    cfg = {
        "soul_mode": {
            "agents": {
                "main": {"enabled": True, "role": "soul", "soul_id": "Echo", "user_id": "marcos"}
            }
        }
    }
    out = GatewayRunner._resolve_soul_mode_agent_config(cfg, "agent:other:telegram:dm:123")
    assert out["enabled"] is False
    assert out["role"] == "standard"


def test_chat_id_from_whatsapp_conversation_id():
    assert GatewayRunner._chat_id_from_whatsapp_conversation_id("whatsapp:dm:151@s.whatsapp.net") == "151@s.whatsapp.net"
    assert GatewayRunner._chat_id_from_whatsapp_conversation_id("whatsapp:group:123@g.us") == "123@g.us"
    assert GatewayRunner._chat_id_from_whatsapp_conversation_id("telegram:123") == ""


@pytest.mark.asyncio
async def test_drain_whatsapp_memu_outbounds_sends_origin_reply(monkeypatch):
    sent: list[tuple[str, str, dict]] = []
    marked: list[dict] = []

    class _Adapter:
        async def send(self, chat_id, text, metadata=None):
            sent.append((chat_id, text, dict(metadata or {})))
            return SendResult(success=True, message_id="wamid.1")

    class _Client:
        def __init__(self, **_kwargs):
            pass

        def claim_whatsapp_outbounds(self, **kwargs):
            assert kwargs["user_id"] == "marcos"
            assert kwargs["soul_id"] == "Siri"
            return [
                {
                    "id": "waout_1",
                    "target": "respond",
                    "target_conversation_id": "whatsapp:dm:151@s.whatsapp.net",
                    "origin_conversation_id": "whatsapp:dm:151@s.whatsapp.net",
                    "response_text": "hello from Siri",
                }
            ]

        def mark_whatsapp_outbound(self, **kwargs):
            marked.append(dict(kwargs))
            return {"ok": True}

    runner = object.__new__(GatewayRunner)
    runner.adapters = {Platform.WHATSAPP: _Adapter()}
    monkeypatch.setattr(
        "gateway.run._load_gateway_config",
        lambda: {
            "soul_mode": {
                "agents": {
                    "main": {
                        "enabled": True,
                        "role": "soul",
                        "soul_id": "Siri",
                        "user_id": "marcos",
                        "memu_base_url": "http://127.0.0.1:8099",
                    }
                }
            }
        },
    )
    monkeypatch.setattr("agent.memu_client.MemuHttpClient", _Client)

    count = await runner._drain_whatsapp_memu_outbounds()

    assert count == 1
    assert sent == [
        (
            "151@s.whatsapp.net",
            "hello from Siri",
            {"origin": "memu_free_turn", "outbound_id": "waout_1"},
        )
    ]
    assert marked == [
        {
            "user_id": "marcos",
            "soul_id": "Siri",
            "outbound_id": "waout_1",
            "status": "sent",
            "provider_message_id": "wamid.1",
            "error": None,
        }
    ]


@pytest.mark.asyncio
async def test_drain_whatsapp_memu_outbounds_marks_send_result_failure(monkeypatch):
    marked: list[dict] = []

    class _Adapter:
        async def send(self, _chat_id, _text, metadata=None):
            return SendResult(success=False, error="bridge down")

    class _Client:
        def __init__(self, **_kwargs):
            pass

        def claim_whatsapp_outbounds(self, **_kwargs):
            return [
                {
                    "id": "waout_1",
                    "target": "respond",
                    "target_conversation_id": "whatsapp:dm:151@s.whatsapp.net",
                    "origin_conversation_id": "whatsapp:dm:151@s.whatsapp.net",
                    "response_text": "hello from Siri",
                }
            ]

        def mark_whatsapp_outbound(self, **kwargs):
            marked.append(dict(kwargs))
            return {"ok": True}

    runner = object.__new__(GatewayRunner)
    runner.adapters = {Platform.WHATSAPP: _Adapter()}
    monkeypatch.setattr(
        "gateway.run._load_gateway_config",
        lambda: {
            "soul_mode": {
                "agents": {
                    "main": {
                        "enabled": True,
                        "role": "soul",
                        "soul_id": "Siri",
                        "user_id": "marcos",
                    }
                }
            }
        },
    )
    monkeypatch.setattr("agent.memu_client.MemuHttpClient", _Client)

    count = await runner._drain_whatsapp_memu_outbounds()

    assert count == 1
    assert marked == [
        {
            "user_id": "marcos",
            "soul_id": "Siri",
            "outbound_id": "waout_1",
            "status": "failed",
            "provider_message_id": None,
            "error": "bridge down",
        }
    ]
