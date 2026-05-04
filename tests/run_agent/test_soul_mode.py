import sys
import types
from unittest.mock import MagicMock, patch

import pytest

from agent.memu_client import MemuClientError

if "dotenv" not in sys.modules:
    _dotenv = types.ModuleType("dotenv")
    _dotenv.load_dotenv = lambda *args, **kwargs: None
    sys.modules["dotenv"] = _dotenv
if "yaml" not in sys.modules:
    _yaml = types.ModuleType("yaml")
    _yaml.safe_load = lambda value, *args, **kwargs: {}
    _yaml.safe_dump = lambda value, *args, **kwargs: "{}"
    sys.modules["yaml"] = _yaml

from agent import soul_mode
from run_agent import AIAgent


def _make_tool_defs(*names: str) -> list:
    return [
        {
            "type": "function",
            "function": {
                "name": n,
                "description": f"{n} tool",
                "parameters": {"type": "object", "properties": {}},
            },
        }
        for n in names
    ]


@pytest.fixture()
def soul_agent():
    with (
        patch("run_agent.get_tool_definitions", return_value=_make_tool_defs("web_search")),
        patch("run_agent.check_toolset_requirements", return_value={}),
        patch("run_agent.OpenAI"),
    ):
        agent = AIAgent(
            api_key="test-key",
            base_url="https://openrouter.ai/api/v1",
            quiet_mode=True,
            skip_context_files=True,
            skip_memory=True,
            platform="telegram",
            chat_id="12345",
            soul_mode_enabled=True,
            soul_mode_role="soul",
            soul_mode_soul_id="Echo",
            soul_mode_user_id="marcos",
            soul_mode_memu_base_url="http://127.0.0.1:8099",
        )
        agent.client = MagicMock()
        return agent


def test_soul_config_is_active(soul_agent):
    assert soul_agent._soul_config.is_active()


def test_soul_config_not_active_when_disabled(soul_agent):
    soul_agent.configure_soul_mode(enabled=False, role="soul", soul_id="Echo", user_id="marcos", memu_base_url="http://127.0.0.1:8099")
    assert not soul_agent._soul_config.is_active()


def test_run_conversation_delegates_to_soul_mode(soul_agent):
    history = [{"role": "assistant", "content": "previous"}]
    mock_result = {
        "final_response": "hello from memu",
        "last_reasoning": None,
        "messages": [{"role": "user", "content": "hi"}, {"role": "assistant", "content": "hello from memu"}],
        "api_calls": 0,
        "completed": True,
        "turn_exit_reason": "soul_mode_memu_turn",
        "partial": False,
        "interrupted": False,
        "response_previewed": False,
        "model": soul_agent.model,
        "provider": soul_agent.provider,
        "base_url": soul_agent.base_url,
    }
    with (
        patch.object(soul_agent, "_ensure_db_session", return_value=None),
        patch.object(soul_agent, "_restore_primary_runtime", return_value=None),
        patch("agent.soul_mode.handle_turn", return_value=mock_result) as mock_handle,
    ):
        result = soul_agent.run_conversation("hi", conversation_history=history)

    mock_handle.assert_called_once()
    assert result["final_response"] == "hello from memu"
    assert result["completed"] is True
    assert result["turn_exit_reason"] == "soul_mode_memu_turn"


def test_run_conversation_returns_failed_result_on_memu_error(soul_agent):
    mock_result = {
        "final_response": "memU turn failed (HTTP 502): upstream error",
        "messages": [],
        "api_calls": 0,
        "completed": False,
        "failed": True,
        "turn_exit_reason": "soul_mode_error",
        "error": "upstream error",
    }
    with (
        patch.object(soul_agent, "_ensure_db_session", return_value=None),
        patch.object(soul_agent, "_restore_primary_runtime", return_value=None),
        patch("agent.soul_mode.handle_turn", return_value=mock_result),
    ):
        result = soul_agent.run_conversation("hi")

    assert result["completed"] is False
    assert result["failed"] is True
    assert result["turn_exit_reason"] == "soul_mode_error"


def test_build_conversation_id_readable_defaults():
    assert soul_mode.build_conversation_id(platform="telegram", chat_id="12345") == "telegram:12345"
    assert soul_mode.build_conversation_id(platform="cron", chat_id="daily-reminder") == "cron:daily-reminder"


def test_build_conversation_id_includes_thread():
    assert soul_mode.build_conversation_id(
        platform="telegram", chat_id="-1002285219667", thread_id="17585"
    ) == "telegram:-1002285219667:17585"


def test_build_conversation_id_whatsapp_gateway_key_dm():
    result = soul_mode.build_conversation_id(
        platform="whatsapp",
        chat_id="999999999999999@lid",
        chat_type="dm",
        gateway_session_key="agent:main:whatsapp:dm:15551234567",
    )
    assert result == "whatsapp:dm:15551234567"


def test_build_conversation_id_whatsapp_gateway_key_group():
    result = soul_mode.build_conversation_id(
        platform="whatsapp",
        chat_id="120363000000000000@g.us",
        chat_type="group",
        gateway_session_key="agent:main:whatsapp:group:120363000000000000@g.us:15551234567",
    )
    assert result == "whatsapp:group:120363000000000000@g.us:15551234567"


def test_coerce_message_text_string():
    assert soul_mode.coerce_message_text("hello") == "hello"


def test_coerce_message_text_multimodal():
    parts = [
        {"type": "text", "text": "Look at this"},
        {"type": "image_url", "image_url": {"url": "data:image/png;base64,abc"}},
    ]
    assert soul_mode.coerce_message_text(parts) == "Look at this"


def test_coerce_message_text_images_only():
    parts = [
        {"type": "image_url", "image_url": {"url": "data:image/png;base64,abc"}},
        {"type": "image_url", "image_url": {"url": "data:image/png;base64,def"}},
    ]
    assert soul_mode.coerce_message_text(parts) == "[User sent 2 images]"


def test_handle_turn_calls_memu_client(soul_agent):
    mock_client = MagicMock()
    mock_client.memu_turn.return_value = {"ok": True, "response": "hello from memu"}
    soul_agent._soul_config._client = mock_client

    result = soul_mode.handle_turn(
        soul_agent, soul_agent._soul_config,
        user_message="hi",
        conversation_history=[],
        messages=[{"role": "user", "content": "hi"}],
        task_id="test-task",
        original_user_message="hi",
        summarize_for_log=lambda x: str(x)[:50],
    )

    assert result["completed"] is True
    assert result["final_response"] == "hello from memu"
    mock_client.memu_turn.assert_called_once()


def test_handle_turn_raises_on_ok_false(soul_agent):
    mock_client = MagicMock()
    mock_client.memu_turn.return_value = {"ok": False, "response": "should not pass"}
    soul_agent._soul_config._client = mock_client

    result = soul_mode.handle_turn(
        soul_agent, soul_agent._soul_config,
        user_message="hi",
        conversation_history=[],
        messages=[{"role": "user", "content": "hi"}],
        task_id="test-task",
        original_user_message="hi",
        summarize_for_log=lambda x: str(x)[:50],
    )

    assert result["completed"] is False
    assert result["failed"] is True
