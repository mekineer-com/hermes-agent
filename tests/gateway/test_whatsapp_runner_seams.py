from unittest.mock import MagicMock

import pytest

from gateway.config import Platform
from gateway.platforms.base import MessageEvent, MessageType
from gateway.run import GatewayRunner
from gateway.session import SessionSource


def _event(raw):
    return MessageEvent(
        text="hello",
        message_type=MessageType.TEXT,
        source=SessionSource(
            platform=Platform.WHATSAPP,
            chat_id="12025550199@lid",
            chat_type="dm",
            user_id="12025550199@lid",
            user_name="Test User",
        ),
        raw_message=raw,
    )


@pytest.mark.asyncio
async def test_whatsapp_persist_only_event_does_not_reach_agent_path():
    runner = GatewayRunner.__new__(GatewayRunner)
    runner._persist_whatsapp_history_event = MagicMock()
    runner._is_duplicate_whatsapp_source_message = MagicMock()

    result = await GatewayRunner._handle_message(
        runner,
        _event({"deliveryMode": "persist_only", "chatId": "c", "messageId": "m"}),
    )

    assert result is None
    runner._persist_whatsapp_history_event.assert_called_once()
    runner._is_duplicate_whatsapp_source_message.assert_not_called()


@pytest.mark.asyncio
async def test_whatsapp_revoke_event_does_not_reach_agent_path():
    runner = GatewayRunner.__new__(GatewayRunner)
    runner._apply_whatsapp_revoke = MagicMock()
    runner._persist_whatsapp_history_event = MagicMock()

    result = await GatewayRunner._handle_message(
        runner,
        _event({"deliveryMode": "revoke", "chatId": "c", "messageId": "m"}),
    )

    assert result is None
    runner._apply_whatsapp_revoke.assert_called_once()
    runner._persist_whatsapp_history_event.assert_not_called()


@pytest.mark.asyncio
async def test_duplicate_whatsapp_live_event_does_not_reach_agent_path():
    runner = GatewayRunner.__new__(GatewayRunner)
    runner._is_duplicate_whatsapp_source_message = MagicMock(return_value=True)

    result = await GatewayRunner._handle_message(
        runner,
        _event({"deliveryMode": "live", "chatId": "c", "messageId": "m"}),
    )

    assert result is None
    runner._is_duplicate_whatsapp_source_message.assert_called_once()
