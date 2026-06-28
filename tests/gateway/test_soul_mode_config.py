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


@pytest.fixture(autouse=True)
def _isolate_outbound_sent_path(tmp_path, monkeypatch):
    """Redirect the dedup record to a per-test temp dir so tests don't pollute ~/.hermes."""
    monkeypatch.setattr(GatewayRunner, "_OUTBOUND_SENT_PATH", tmp_path / "whatsapp" / "outbound_sent.json")


def test_resolve_soul_mode_agent_config_defaults_when_missing():
    out = GatewayRunner._resolve_soul_mode_agent_config({}, "agent:main:telegram:dm:123")
    assert out["enabled"] is False
    assert out["role"] == "standard"
    assert out["soul_id"] == ""
    assert out["user_id"] == ""
    assert out["memu_base_url"] == "http://127.0.0.1:8099"


def test_resolve_soul_mode_agent_config_reads_main_agent():
    cfg = {
        "timezone": "America/Lima",
        "whatsapp": {
            "reply_prefix": "✦ *SoulB*: ",
        },
        "soul_mode": {
            "agents": {
                "main": {
                    "enabled": True,
                    "role": "soul",
                    "soul_id": "SoulB",
                    "user_id": "user-1",
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
    assert out["soul_id"] == "SoulB"
    assert out["user_id"] == "user-1"
    assert out["timeout_seconds"] == 12.0
    assert "whatsapp_history_source" not in out
    assert "whatsapp_web_source_db" not in out
    assert "whatsapp_history_limit" not in out
    assert "timezone" not in out
    assert "whatsapp_reply_prefix" not in out


def test_resolve_soul_mode_agent_config_is_explicit_per_agent():
    cfg = {
        "soul_mode": {
            "agents": {
                "main": {"enabled": True, "role": "soul", "soul_id": "SoulB", "user_id": "user-1"}
            }
        }
    }
    out = GatewayRunner._resolve_soul_mode_agent_config(cfg, "agent:other:telegram:dm:123")
    assert out["enabled"] is False
    assert out["role"] == "standard"


def test_chat_id_from_whatsapp_conversation_id():
    assert GatewayRunner._chat_id_from_whatsapp_conversation_id("whatsapp:dm:12025550100@s.whatsapp.net") == "12025550100@s.whatsapp.net"
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
                        "soul_id": "SoulA",
                        "user_id": "user-1",
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
    monkeypatch.setattr(
        "gateway.run._load_gateway_config",
        lambda: {
            "soul_mode": {
                "agents": {
                    "main": {
                        "enabled": True,
                        "role": "soul",
                        "soul_id": "SoulA",
                        "user_id": "user-1",
                    }
                }
            }
        },
    )
    monkeypatch.setattr("agent.memu_client.MemuHttpClient", _Client)

    count = await runner._drain_whatsapp_memu_outbounds()

    assert count == 0
    assert claimed is False


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
                    "target_conversation_id": "whatsapp:dm:12025550100@s.whatsapp.net",
                    "origin_conversation_id": "whatsapp:dm:12025550100@s.whatsapp.net",
                    "response_text": "hello from SoulA",
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
                        "soul_id": "SoulA",
                        "user_id": "user-1",
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
            "user_id": "user-1",
            "soul_id": "SoulA",
            "outbound_id": "waout_1",
            "status": "failed",
            "provider_message_id": None,
            "error": "bridge down",
        }
    ]


@pytest.mark.asyncio
async def test_deliver_whatsapp_memu_outbound_skips_duplicate(monkeypatch, tmp_path):
    """If an outbound_id is already in the sent record, send is skipped and _mark('sent') is called."""
    sent: list = []
    marked: list[dict] = []

    class _Adapter:
        async def send(self, chat_id, text, metadata=None):
            sent.append((chat_id, text))
            return SendResult(success=True, message_id="wamid.dup")

    class _Client:
        def __init__(self, **_kwargs):
            pass

        def mark_whatsapp_outbound(self, **kwargs):
            marked.append(dict(kwargs))
            return {"ok": True}

    runner = object.__new__(GatewayRunner)
    runner.adapters = {Platform.WHATSAPP: _Adapter()}

    # Seed the sent record so waout_dup is known-delivered.
    sent_path = tmp_path / "whatsapp" / "outbound_sent.json"
    sent_path.parent.mkdir(parents=True)
    sent_path.write_text(json.dumps(["waout_dup"]))

    monkeypatch.setattr(GatewayRunner, "_OUTBOUND_SENT_PATH", sent_path)

    row = {
        "id": "waout_dup",
        "target": "respond",
        "target_conversation_id": "whatsapp:dm:12025550100@s.whatsapp.net",
        "origin_conversation_id": "whatsapp:dm:12025550100@s.whatsapp.net",
        "response_text": "hello",
    }
    cfg = {"user_id": "user-1", "soul_id": "SoulA"}

    await runner._deliver_whatsapp_memu_outbound(_Client(), cfg, row)

    assert sent == [], "send must not be called for a duplicate outbound"
    assert len(marked) == 1
    assert marked[0]["status"] == "sent"
    assert marked[0]["outbound_id"] == "waout_dup"


# ---------------------------------------------------------------------------
# Media-path (attachment) branch tests
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_deliver_whatsapp_memu_outbound_media_respond_success(monkeypatch, tmp_path):
    """respond target with media_path sends document and marks sent."""
    doc = tmp_path / "report.pdf"
    doc.write_bytes(b"%PDF-1.4 test")

    sent_docs: list[tuple] = []
    marked: list[dict] = []

    class _Adapter:
        async def send_document(self, chat_id, file_path, caption=None, **kw):
            sent_docs.append((chat_id, file_path, caption))
            return SendResult(success=True, message_id="wamid.doc1")

    class _Client:
        def __init__(self, **_kwargs):
            pass

        def claim_whatsapp_outbounds(self, **_kwargs):
            return [
                {
                    "id": "waout_doc1",
                    "target": "respond",
                    "target_conversation_id": "whatsapp:dm:12025550100@s.whatsapp.net",
                    "origin_conversation_id": "whatsapp:dm:12025550100@s.whatsapp.net",
                    "response_text": "here is the file",
                    "media_path": str(doc),
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
                        "soul_id": "SoulA",
                        "user_id": "user-1",
                        "memu_base_url": "http://127.0.0.1:8099",
                    }
                }
            }
        },
    )
    monkeypatch.setattr("agent.memu_client.MemuHttpClient", _Client)

    count = await runner._drain_whatsapp_memu_outbounds()

    assert count == 1
    assert sent_docs == [("12025550100@s.whatsapp.net", str(doc), "here is the file")]
    assert marked == [
        {
            "user_id": "user-1",
            "soul_id": "SoulA",
            "outbound_id": "waout_doc1",
            "status": "sent",
            "provider_message_id": "wamid.doc1",
            "error": None,
        }
    ]


@pytest.mark.asyncio
async def test_deliver_whatsapp_memu_outbound_media_missing_marks_failed(monkeypatch, tmp_path):
    """If media_path does not exist, mark failed without sending."""
    marked: list[dict] = []
    sent_docs: list = []

    class _Adapter:
        async def send_document(self, *args, **kw):
            sent_docs.append(args)
            return SendResult(success=True, message_id="wamid.x")

    class _Client:
        def __init__(self, **_kwargs):
            pass

        def claim_whatsapp_outbounds(self, **_kwargs):
            return [
                {
                    "id": "waout_miss",
                    "target": "respond",
                    "target_conversation_id": "whatsapp:dm:12025550100@s.whatsapp.net",
                    "origin_conversation_id": "whatsapp:dm:12025550100@s.whatsapp.net",
                    "response_text": "",
                    "media_path": "/nonexistent/path/file.pdf",
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
                        "soul_id": "SoulA",
                        "user_id": "user-1",
                        "memu_base_url": "http://127.0.0.1:8099",
                    }
                }
            }
        },
    )
    monkeypatch.setattr("agent.memu_client.MemuHttpClient", _Client)

    await runner._drain_whatsapp_memu_outbounds()

    assert sent_docs == [], "send_document must not be called for a missing file"
    assert len(marked) == 1
    assert marked[0]["status"] == "failed"
    assert marked[0]["error"] == "attachment missing"
