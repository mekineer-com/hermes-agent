import json

import pytest

from gateway.config import Platform
from gateway.platforms.base import SendResult
from gateway.run import GatewayRunner


@pytest.fixture(autouse=True)
def _isolate_outbound_sent_path(tmp_path, monkeypatch):
    monkeypatch.setattr(
        GatewayRunner,
        "_OUTBOUND_SENT_PATH",
        tmp_path / "whatsapp" / "outbound_sent.json",
    )


def _soul_cfg():
    return {
        "soul_mode": {
            "agents": {
                "main": {
                    "enabled": True,
                    "role": "soul",
                    "soul_id": "SoulA",
                    "user_id": "user-1",
                    "memu_base_url": "http://127.0.0.1:8099",
                }
            }
        }
    }


def test_resolve_soul_mode_agent_config_is_explicit_per_agent():
    out = GatewayRunner._resolve_soul_mode_agent_config(
        _soul_cfg(),
        "agent:other:telegram:dm:123",
    )

    assert out["enabled"] is False
    assert out["role"] == "standard"


@pytest.mark.asyncio
async def test_drain_whatsapp_memu_outbounds_sends_origin_reply(monkeypatch):
    sent = []
    marked = []

    class _Adapter:
        async def send(self, chat_id, text, metadata=None):
            sent.append((chat_id, text, dict(metadata or {})))
            return SendResult(success=True, message_id="wamid.1")

    class _Client:
        def __init__(self, **_kwargs):
            pass

        def claim_whatsapp_outbounds(self, **kwargs):
            assert kwargs["user_id"] == "user-1"
            assert kwargs["soul_id"] == "SoulA"
            return [
                {
                    "id": "waout_1",
                    "target": "respond",
                    "target_conversation_id": "whatsapp:dm:12025550100@s.whatsapp.net",
                    "origin_conversation_id": "whatsapp:dm:12025550100@s.whatsapp.net",
                    "response_text": "hello from SoulA",
                }
            ]

        def mark_whatsapp_outbound(self, **kwargs):
            marked.append(dict(kwargs))

    runner = object.__new__(GatewayRunner)
    runner.adapters = {Platform.WHATSAPP: _Adapter()}
    monkeypatch.setattr("gateway.run._load_gateway_config", _soul_cfg)
    monkeypatch.setattr("agent.memu_client.MemuHttpClient", _Client)

    count = await runner._drain_whatsapp_memu_outbounds()

    assert count == 1
    assert sent == [
        (
            "12025550100@s.whatsapp.net",
            "hello from SoulA",
            {"origin": "memu_free_turn", "outbound_id": "waout_1"},
        )
    ]
    assert marked == [
        {
            "user_id": "user-1",
            "soul_id": "SoulA",
            "outbound_id": "waout_1",
            "status": "sent",
            "provider_message_id": "wamid.1",
            "error": None,
        }
    ]


@pytest.mark.asyncio
async def test_drain_whatsapp_memu_outbounds_skips_claim_when_bridge_not_connected(monkeypatch):
    claimed = False

    class _Adapter:
        _running = True
        _http_session = None
        _bridge_health = {"status": "connecting"}

    class _Client:
        def __init__(self, **_kwargs):
            pass

        def claim_whatsapp_outbounds(self, **_kwargs):
            nonlocal claimed
            claimed = True
            return []

    runner = object.__new__(GatewayRunner)
    runner.adapters = {Platform.WHATSAPP: _Adapter()}
    monkeypatch.setattr("gateway.run._load_gateway_config", _soul_cfg)
    monkeypatch.setattr("agent.memu_client.MemuHttpClient", _Client)

    count = await runner._drain_whatsapp_memu_outbounds()

    assert count == 0
    assert claimed is False


@pytest.mark.asyncio
async def test_deliver_whatsapp_memu_outbound_skips_duplicate(tmp_path, monkeypatch):
    sent = []
    marked = []

    class _Adapter:
        async def send(self, chat_id, text, metadata=None):
            sent.append((chat_id, text))
            return SendResult(success=True, message_id="wamid.dup")

    class _Client:
        def mark_whatsapp_outbound(self, **kwargs):
            marked.append(dict(kwargs))

    sent_path = tmp_path / "whatsapp" / "outbound_sent.json"
    sent_path.parent.mkdir(parents=True)
    sent_path.write_text(json.dumps(["waout_dup"]), encoding="utf-8")
    monkeypatch.setattr(GatewayRunner, "_OUTBOUND_SENT_PATH", sent_path)

    runner = object.__new__(GatewayRunner)
    runner.adapters = {Platform.WHATSAPP: _Adapter()}

    await runner._deliver_whatsapp_memu_outbound(
        _Client(),
        {"user_id": "user-1", "soul_id": "SoulA"},
        {
            "id": "waout_dup",
            "target": "respond",
            "target_conversation_id": "whatsapp:dm:12025550100@s.whatsapp.net",
            "response_text": "hello",
        },
    )

    assert sent == []
    assert marked == [
        {
            "user_id": "user-1",
            "soul_id": "SoulA",
            "outbound_id": "waout_dup",
            "status": "sent",
            "provider_message_id": None,
            "error": None,
        }
    ]
