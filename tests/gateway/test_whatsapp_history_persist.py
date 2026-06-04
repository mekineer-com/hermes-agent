from types import SimpleNamespace
import threading
from unittest.mock import AsyncMock

import pytest

from gateway.config import GatewayConfig, Platform, PlatformConfig
from gateway.platforms.base import MessageEvent
from gateway.session import SessionSource, SessionStore
from hermes_state import SessionDB


def _runner(tmp_path, monkeypatch, *, soul_id="Siri", active_since=None):
    from gateway.run import GatewayRunner

    db = SessionDB(tmp_path / "state.db")
    if active_since is not None:
        with db._lock:
            db._conn.execute(
                "INSERT OR REPLACE INTO souls(soul_id, active_since) VALUES (?, ?)",
                (soul_id, active_since),
            )
    config = GatewayConfig(
        platforms={Platform.WHATSAPP: PlatformConfig(enabled=True)},
        sessions_dir=tmp_path / "sessions",
    )
    runner = object.__new__(GatewayRunner)
    runner.config = config
    runner._session_db = db
    store = object.__new__(SessionStore)
    store.sessions_dir = config.sessions_dir
    store.config = config
    store._entries = {}
    store._loaded = False
    store._lock = threading.Lock()
    store._has_active_processes_fn = None
    store._db = db
    runner.session_store = store
    runner.adapters = {}
    monkeypatch.setattr(
        "gateway.run._load_gateway_config",
        lambda: {
            "soul_mode": {
                "agents": {
                    "main": {
                        "enabled": True,
                        "role": "soul",
                        "soul_id": soul_id,
                        "user_id": "marcos",
                    }
                }
            }
        },
    )
    return runner, db


def _event(*, text="hello", role_hint="user", timestamp=1780233002):
    return MessageEvent(
        text=text,
        message_id="m1",
        source=SessionSource(
            platform=Platform.WHATSAPP,
            chat_id="15133278228@s.whatsapp.net",
            chat_name="Marcos",
            chat_type="dm",
            user_id="15133278228@s.whatsapp.net",
            user_name="Marcos",
        ),
        raw_message={
            "eventType": "history_message",
            "deliveryMode": "persist_only",
            "triggerAgent": False,
            "messageId": "m1",
            "chatId": "15133278228@s.whatsapp.net",
            "senderId": "15133278228@s.whatsapp.net",
            "senderName": "Marcos",
            "timestamp": timestamp,
            "speakerRoleHint": role_hint,
        },
        internal=True,
    )


def test_whatsapp_history_assistant_row_uses_active_soul_identity(tmp_path, monkeypatch):
    runner, db = _runner(tmp_path, monkeypatch, active_since=1780160400)

    runner._persist_whatsapp_history_event(
        _event(text="She said yes", role_hint="assistant", timestamp=1780225816)
    )

    session_id = next(iter(runner.session_store._entries.values())).session_id
    messages = db.get_messages(session_id)
    assert len(messages) == 1
    assert messages[0]["role"] == "assistant"
    assert messages[0]["sender_name"] == "Siri"
    assert messages[0]["sender_id"] == "soul:Siri"
    assert messages[0]["source_chat_id"] == "15133278228@s.whatsapp.net"
    assert messages[0]["source_message_id"] == "m1"


def test_whatsapp_history_respects_soul_active_since(tmp_path, monkeypatch):
    runner, db = _runner(tmp_path, monkeypatch, active_since=1780230000)

    runner._persist_whatsapp_history_event(
        _event(text="too old", role_hint="user", timestamp=1780220000)
    )

    assert db.message_count() == 0


def test_whatsapp_history_drops_missing_timestamp(tmp_path, monkeypatch):
    runner, db = _runner(tmp_path, monkeypatch, active_since=1780230000)

    runner._persist_whatsapp_history_event(
        _event(text="missing ts", role_hint="user", timestamp=None)
    )

    assert db.message_count() == 0


def test_whatsapp_exception_turn_persists_visible_error_pair(tmp_path, monkeypatch):
    runner, db = _runner(tmp_path, monkeypatch, active_since=1780160400)
    event = _event(text="Testing yet another way to connect to WhatsApp")
    session_entry = runner.session_store.get_or_create_session(event.source)

    runner._persist_whatsapp_exception_turn(
        session_entry=session_entry,
        source=event.source,
        raw_message=event.raw_message,
        message_text=event.text,
        error_response="Sorry, I encountered an error (NameError).\nname 'event' is not defined\nTry again.",
    )

    messages = db.get_messages(session_entry.session_id)
    assert [m["role"] for m in messages] == ["user", "assistant"]
    assert messages[0]["content"] == "Testing yet another way to connect to WhatsApp"
    assert messages[0]["sender_id"] == "15133278228@s.whatsapp.net"
    assert messages[0]["sender_name"] == "Marcos"
    assert messages[0]["source_chat_id"] == "15133278228@s.whatsapp.net"
    assert messages[0]["source_message_id"] == "m1"
    assert messages[0]["timestamp"] == 1780233002
    assert "NameError" in messages[1]["content"]
    assert messages[1]["source_chat_id"] == "15133278228@s.whatsapp.net"
    assert messages[1]["source_message_id"] is None


def test_whatsapp_exception_turn_dedupes_user_if_history_already_persisted(tmp_path, monkeypatch):
    runner, db = _runner(tmp_path, monkeypatch, active_since=1780160400)
    event = _event(text="Testing yet another way to connect to WhatsApp")
    session_entry = runner.session_store.get_or_create_session(event.source)

    runner._persist_whatsapp_history_event(event)
    runner._persist_whatsapp_exception_turn(
        session_entry=session_entry,
        source=event.source,
        raw_message=event.raw_message,
        message_text=event.text,
        error_response="Sorry, I encountered an error (NameError).\nname 'event' is not defined\nTry again.",
    )

    messages = db.get_messages(session_entry.session_id)
    assert [m["role"] for m in messages] == ["user", "assistant"]
    assert messages[0]["content"] == "Testing yet another way to connect to WhatsApp"
    assert messages[0]["source_message_id"] == "m1"
    assert "NameError" in messages[1]["content"]


@pytest.mark.asyncio
async def test_whatsapp_response_delivery_stamps_assistant_source_key(tmp_path, monkeypatch):
    runner, db = _runner(tmp_path, monkeypatch, active_since=1780160400)
    event = _event(text="fresh user", role_hint="user", timestamp=1780233002)
    session_entry = runner.session_store.get_or_create_session(event.source)
    db.append_message(
        session_entry.session_id,
        role="assistant",
        content="fresh answer",
        sender_id="soul:Siri",
        sender_name="Siri",
    )

    await runner._handle_response_delivery(
        event,
        SimpleNamespace(success=True, message_id="sent-wa-id"),
        "fresh answer",
    )

    messages = db.get_messages(session_entry.session_id)
    assert messages[-1]["source_chat_id"] == "15133278228@s.whatsapp.net"
    assert messages[-1]["source_message_id"] == "sent-wa-id"


@pytest.mark.asyncio
async def test_whatsapp_response_delivery_does_not_stamp_unmatched_assistant(tmp_path, monkeypatch):
    runner, db = _runner(tmp_path, monkeypatch, active_since=1780160400)
    event = _event(text="fresh user", role_hint="user", timestamp=1780233002)
    session_entry = runner.session_store.get_or_create_session(event.source)
    db.append_message(
        session_entry.session_id,
        role="assistant",
        content="previous answer",
        sender_id="soul:Siri",
        sender_name="Siri",
    )

    await runner._handle_response_delivery(
        event,
        SimpleNamespace(success=True, message_id="command-reply-id"),
        "New session started.",
    )

    messages = db.get_messages(session_entry.session_id)
    assert messages[-1]["source_chat_id"] is None
    assert messages[-1]["source_message_id"] is None


@pytest.mark.asyncio
async def test_whatsapp_persist_only_dispatch_marks_wal_only_after_success():
    from gateway.platforms.whatsapp import WhatsAppAdapter

    adapter = object.__new__(WhatsAppAdapter)
    marked = []
    adapter._gateway_wal = SimpleNamespace(mark_processed=lambda seq: marked.append(seq))
    adapter._message_handler = AsyncMock(side_effect=RuntimeError("db locked"))
    event = _event()
    event.raw_message["wal_seq"] = 7

    with pytest.raises(RuntimeError):
        await adapter._dispatch_built_message_event(event)
    assert marked == []

    adapter._message_handler = AsyncMock(return_value=None)
    await adapter._dispatch_built_message_event(event)
    assert marked == [7]
