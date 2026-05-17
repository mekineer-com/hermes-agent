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


def test_handle_turn_listens_when_response_target_listen(soul_agent):
    """response_target=listen means the soul ran the turn but chose silence."""
    mock_client = MagicMock()
    mock_client.memu_turn.return_value = {
        "ok": True,
        "response": "",
        "response_target": "listen",
        "response_peer": "",
    }
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
    assert result["final_response"] == ""


def test_handle_turn_silent_when_peer_mismatch_dropped_response(soul_agent):
    """memu drops response_text to "" when the soul names the wrong peer.
    soul_mode must treat that as a silent exit, not an error."""
    mock_client = MagicMock()
    mock_client.memu_turn.return_value = {
        "ok": True,
        "response": "",  # memu's peer-mismatch guard cleared it
        "response_target": "respond",
        "response_peer": "Alice",
    }
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
    assert result["final_response"] == ""


def test_handle_turn_routes_private_to_self_dm_on_whatsapp(soul_agent, monkeypatch):
    """response_target=private on WhatsApp routes the reply to the human's
    self-DM via the bridge, and the agent path exits silently so the
    gateway does NOT also send to the originating chat."""
    soul_agent.platform = "whatsapp"
    soul_agent._chat_id = "247789598601266@lid"
    soul_agent._chat_type = "dm"
    soul_agent._chat_name = "Alice"

    mock_client = MagicMock()
    mock_client.memu_turn.return_value = {
        "ok": True,
        "response": "aside for you",
        "response_target": "private",
        "response_peer": "",
    }
    soul_agent._soul_config._client = mock_client

    from agent import whatsapp_bridge_client
    monkeypatch.setattr(whatsapp_bridge_client, "read_self_dm_jid", lambda: "15133278228@s.whatsapp.net")
    sent = []
    monkeypatch.setattr(
        whatsapp_bridge_client,
        "send_text",
        lambda chat_id, text, **_kw: (sent.append((chat_id, text)) or True),
    )

    result = soul_mode.handle_turn(
        soul_agent, soul_agent._soul_config,
        user_message="hi",
        conversation_history=[],
        messages=[{"role": "user", "content": "hi"}],
        task_id="test-task",
        original_user_message="hi",
        summarize_for_log=lambda x: str(x)[:50],
    )

    assert sent == [("15133278228@s.whatsapp.net", "aside for you")]
    assert result["completed"] is True
    assert result["final_response"] == ""  # gateway's default send path stays quiet


# --- Phase 2: conversation routing ---


def test_conversation_id_stable_across_calls():
    """Same inputs must produce the same conversation ID every time."""
    kwargs = dict(platform="telegram", chat_id="12345", thread_id="17585")
    first = soul_mode.build_conversation_id(**kwargs)
    second = soul_mode.build_conversation_id(**kwargs)
    third = soul_mode.build_conversation_id(**kwargs)
    assert first == second == third


def test_conversation_id_different_contacts_different_ids():
    """Two WhatsApp contacts must get distinct conversation IDs."""
    id_alice = soul_mode.build_conversation_id(
        platform="whatsapp",
        chat_id="alice@lid",
        chat_type="dm",
        gateway_session_key="agent:main:whatsapp:dm:15550001111",
    )
    id_bob = soul_mode.build_conversation_id(
        platform="whatsapp",
        chat_id="bob@lid",
        chat_type="dm",
        gateway_session_key="agent:main:whatsapp:dm:15550002222",
    )
    assert id_alice != id_bob
    assert id_alice == "whatsapp:dm:15550001111"
    assert id_bob == "whatsapp:dm:15550002222"


def test_conversation_id_cron_uses_job_name():
    """Cron platform should use chat_id as the job name in the ID."""
    result = soul_mode.build_conversation_id(
        platform="cron",
        chat_id="nightly-digest",
    )
    assert result == "cron:nightly-digest"

    # Fallback to gateway_session_key when chat_id is empty
    result_fallback = soul_mode.build_conversation_id(
        platform="cron",
        chat_id="",
        gateway_session_key="cron-session-abc",
    )
    assert result_fallback == "cron:cron-session-abc"


# --- Phase 3: history handoff to memU ---


def test_handle_turn_passes_history_to_memu(soul_agent):
    """SessionDB history must be forwarded to memu_turn so memU can detect sleep gaps."""
    db_history = [
        {"role": "user", "content": "good night", "ts_ms": 1714600000000},
        {"role": "assistant", "content": "sleep well", "ts_ms": 1714600001000},
        {"role": "user", "content": "good morning", "ts_ms": 1714632000000},
    ]

    mock_db = MagicMock()
    mock_db.get_messages.return_value = db_history
    soul_agent._session_db = mock_db

    mock_client = MagicMock()
    mock_client.memu_turn.return_value = {"ok": True, "response": "morning!"}
    soul_agent._soul_config._client = mock_client

    soul_mode.handle_turn(
        soul_agent, soul_agent._soul_config,
        user_message="good morning",
        conversation_history=[],
        messages=[{"role": "user", "content": "good morning"}],
        task_id="test-task",
        original_user_message="good morning",
        summarize_for_log=lambda x: str(x)[:50],
    )

    mock_client.memu_turn.assert_called_once()
    call_kwargs = mock_client.memu_turn.call_args[1]
    assert call_kwargs["history"] == db_history
