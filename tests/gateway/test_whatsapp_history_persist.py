import logging
from types import SimpleNamespace
import threading
from unittest.mock import AsyncMock

import pytest

from gateway.config import GatewayConfig, Platform, PlatformConfig
from gateway.platforms.base import MessageEvent
from gateway.session import SessionSource, SessionStore
from gateway.session import build_session_key
from hermes_state import SessionDB


def _runner(tmp_path, monkeypatch, *, soul_id="Echo", active_since=None):
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
                        "user_id": "test-user",
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
            chat_id="12025550177@s.whatsapp.net",
            chat_name="Test User",
            chat_type="dm",
            user_id="12025550177@s.whatsapp.net",
            user_name="Test User",
        ),
        raw_message={
            "eventType": "history_message",
            "deliveryMode": "persist_only",
            "messageId": "m1",
            "chatId": "12025550177@s.whatsapp.net",
            "senderId": "12025550177@s.whatsapp.net",
            "senderName": "Test User",
            "timestamp": timestamp,
            "speakerRoleHint": role_hint,
        },
        internal=True,
    )


def _adapter_for_dispatch(max_age=300):
    from gateway.platforms.whatsapp import WhatsAppAdapter

    adapter = object.__new__(WhatsAppAdapter)
    adapter.config = PlatformConfig(enabled=True, extra={})
    adapter._max_message_age_seconds = max_age
    adapter._pending_text_batches = {}
    adapter._pending_text_batch_tasks = {}
    adapter._text_batch_delay_seconds = 3600
    adapter._text_batch_split_delay_seconds = 3600
    return adapter


def test_whatsapp_history_assistant_row_uses_active_soul_identity(tmp_path, monkeypatch):
    runner, db = _runner(tmp_path, monkeypatch, active_since=1780160400)

    runner._persist_whatsapp_history_event(
        _event(text="She said yes", role_hint="assistant", timestamp=1780225816)
    )

    session_id = next(iter(runner.session_store._entries.values())).session_id
    messages = db.get_messages(session_id)
    assert len(messages) == 1
    assert messages[0]["role"] == "assistant"
    assert messages[0]["sender_name"] == "Echo"
    assert messages[0]["sender_id"] == "soul:Echo"
    assert messages[0]["source_chat_id"] == "12025550177@s.whatsapp.net"
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


def test_whatsapp_source_key_processed_marker_round_trip(tmp_path, monkeypatch):
    _runner(tmp_path, monkeypatch, active_since=1780160400)
    db = SessionDB(tmp_path / "state.db")

    assert not db.message_source_key_is_processed(
        source_chat_id="12025550177@s.whatsapp.net",
        source_message_id="m1",
    )
    assert db.mark_message_source_key_processed(
        source_chat_id="12025550177@s.whatsapp.net",
        source_message_id="m1",
    )
    assert db.message_source_key_is_processed(
        source_chat_id="12025550177@s.whatsapp.net",
        source_message_id="m1",
    )


def test_whatsapp_delete_source_key_clears_processed_marker(tmp_path, monkeypatch):
    runner, db = _runner(tmp_path, monkeypatch, active_since=1780160400)
    event = _event(text="source row", role_hint="user", timestamp=1780233002)
    runner._persist_whatsapp_history_event(event)

    assert db.mark_message_source_key_processed(
        source_chat_id="12025550177@s.whatsapp.net",
        source_message_id="m1",
    )
    assert db.message_source_key_is_processed(
        source_chat_id="12025550177@s.whatsapp.net",
        source_message_id="m1",
    )

    db.delete_message_by_source_key(
        source_chat_id="12025550177@s.whatsapp.net",
        source_message_id="m1",
    )
    assert not db.message_source_key_is_processed(
        source_chat_id="12025550177@s.whatsapp.net",
        source_message_id="m1",
    )


@pytest.mark.asyncio
async def test_whatsapp_live_turn_marks_processed_from_event_source_key(tmp_path, monkeypatch):
    runner, db = _runner(tmp_path, monkeypatch, active_since=1780160400)
    event = _event(text="live turn", role_hint="user", timestamp=1780233002)
    event.raw_message["deliveryMode"] = "live"
    event.internal = False
    session_key = build_session_key(event.source)
    runner._session_run_generation = {session_key: 1}
    runner._running_agents = {}
    runner._running_agents_ts = {}
    runner._pending_messages = {}
    runner.adapters = {}
    runner.hooks = SimpleNamespace(emit=AsyncMock())
    runner._prepare_inbound_message_text = AsyncMock(return_value="live turn")
    runner._run_agent = AsyncMock(return_value={
        "final_response": "handled",
        "messages": [{"role": "user", "content": "live turn"}],
        "history_offset": 0,
        "api_calls": 1,
        "failed": False,
        "partial": False,
        "interrupted": False,
        "completed": True,
        "tools": [],
    })
    runner._should_send_voice_reply = lambda *args, **kwargs: False

    await runner._handle_message_with_agent(event, event.source, session_key, 1)

    assert db.message_source_key_is_processed(
        source_chat_id="12025550177@s.whatsapp.net",
        source_message_id="m1",
    )


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
    assert messages[0]["sender_id"] == "12025550177@s.whatsapp.net"
    assert messages[0]["sender_name"] == "Test User"
    assert messages[0]["source_chat_id"] == "12025550177@s.whatsapp.net"
    assert messages[0]["source_message_id"] == "m1"
    assert messages[0]["timestamp"] == 1780233002
    assert "NameError" in messages[1]["content"]
    assert messages[1]["source_chat_id"] == "12025550177@s.whatsapp.net"
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
        sender_id="soul:Echo",
        sender_name="Echo",
    )

    await runner._handle_response_delivery(
        event,
        SimpleNamespace(success=True, message_id="sent-wa-id"),
        "fresh answer",
    )

    messages = db.get_messages(session_entry.session_id)
    assert messages[-1]["source_chat_id"] == "12025550177@s.whatsapp.net"
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
        sender_id="soul:Echo",
        sender_name="Echo",
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


@pytest.mark.asyncio
async def test_whatsapp_stale_live_dispatch_marks_wal_without_waking_agent(caplog):
    from gateway.platforms.whatsapp import WhatsAppAdapter

    adapter = object.__new__(WhatsAppAdapter)
    adapter._max_message_age_seconds = 300
    marked = []
    adapter._gateway_wal = SimpleNamespace(mark_processed=lambda seq: marked.append(seq))
    adapter.handle_message = AsyncMock()
    event = _event(timestamp=1)
    event.raw_message["deliveryMode"] = "live"
    event.raw_message["wal_seq"] = 9

    with caplog.at_level(logging.INFO):
        await adapter._dispatch_built_message_event(event)

    adapter.handle_message.assert_not_awaited()
    assert marked == [9]
    assert "Dropping stale WhatsApp live message" in caplog.text


@pytest.mark.asyncio
async def test_whatsapp_fresh_live_dispatches_to_agent():
    adapter = _adapter_for_dispatch()
    adapter._gateway_wal = SimpleNamespace(mark_processed=lambda seq: None)
    adapter.handle_message = AsyncMock()
    event = _event(timestamp=9999999999)
    event.raw_message["deliveryMode"] = "live"
    event.raw_message["wal_seq"] = 10

    await adapter._dispatch_built_message_event(event)

    assert len(adapter._pending_text_batches) == 1
    adapter.handle_message.assert_not_awaited()
    for task in adapter._pending_text_batch_tasks.values():
        task.cancel()


@pytest.mark.asyncio
async def test_whatsapp_zero_max_age_disables_live_filter():
    adapter = _adapter_for_dispatch(max_age=0)
    adapter._gateway_wal = SimpleNamespace(mark_processed=lambda seq: None)
    adapter.handle_message = AsyncMock()
    event = _event(timestamp=1)
    event.raw_message["deliveryMode"] = "live"
    event.raw_message["wal_seq"] = 11

    await adapter._dispatch_built_message_event(event)

    assert len(adapter._pending_text_batches) == 1
    adapter.handle_message.assert_not_awaited()
    for task in adapter._pending_text_batch_tasks.values():
        task.cancel()


@pytest.mark.asyncio
async def test_whatsapp_revoke_dispatch_is_not_age_filtered():
    from gateway.platforms.whatsapp import WhatsAppAdapter

    adapter = object.__new__(WhatsAppAdapter)
    adapter._max_message_age_seconds = 300
    marked = []
    adapter._gateway_wal = SimpleNamespace(mark_processed=lambda seq: marked.append(seq))
    adapter._message_handler = AsyncMock(return_value=None)
    adapter.handle_message = AsyncMock()
    event = _event(timestamp=1)
    event.raw_message["eventType"] = "revoke"
    event.raw_message["deliveryMode"] = "revoke"
    event.raw_message["wal_seq"] = 12

    await adapter._dispatch_built_message_event(event)

    adapter.handle_message.assert_not_awaited()
    adapter._message_handler.assert_awaited_once_with(event)
    assert marked == [12]


@pytest.mark.asyncio
async def test_whatsapp_missing_mode_wal_row_is_non_live_and_acked():
    from gateway.platforms.whatsapp import WhatsAppAdapter

    adapter = object.__new__(WhatsAppAdapter)
    marked = []
    adapter._gateway_wal = SimpleNamespace(mark_processed=lambda seq: marked.append(seq))
    adapter._message_handler = AsyncMock(return_value=None)
    adapter.handle_message = AsyncMock()
    event = _event()
    event.raw_message.pop("eventType")
    event.raw_message.pop("deliveryMode")
    event.raw_message["wal_seq"] = 8

    await adapter._dispatch_built_message_event(event)

    adapter.handle_message.assert_not_awaited()
    adapter._message_handler.assert_awaited_once_with(event)
    assert marked == [8]
