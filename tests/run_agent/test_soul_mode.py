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


def test_run_conversation_delegates_to_memu_turn(soul_agent):
    history = [{"role": "assistant", "content": "previous"}]
    with (
        patch.object(soul_agent, "_ensure_db_session", return_value=None),
        patch.object(soul_agent, "_restore_primary_runtime", return_value=None),
        patch.object(soul_agent, "_run_soul_turn", return_value=("hello from memu", {"ok": True})) as run_turn,
        patch.object(soul_agent, "_save_trajectory", return_value=None),
        patch.object(soul_agent, "_cleanup_task_resources", return_value=None),
        patch.object(soul_agent, "_persist_session", return_value=None),
    ):
        result = soul_agent.run_conversation("hi", conversation_history=history)

    run_turn.assert_called_once_with(user_message="hi", conversation_history=history)
    assert result["final_response"] == "hello from memu"
    assert result["completed"] is True
    assert result["api_calls"] == 0
    assert result["turn_exit_reason"] == "soul_mode_memu_turn"
    assert result["messages"][-1]["role"] == "assistant"
    assert result["messages"][-1]["content"] == "hello from memu"


def test_run_conversation_returns_failed_result_on_memu_error(soul_agent):
    with (
        patch.object(soul_agent, "_ensure_db_session", return_value=None),
        patch.object(soul_agent, "_restore_primary_runtime", return_value=None),
        patch.object(
            soul_agent,
            "_run_soul_turn",
            side_effect=MemuClientError("upstream error", status_code=502),
        ),
    ):
        result = soul_agent.run_conversation("hi")

    assert result["completed"] is False
    assert result["failed"] is True
    assert result["turn_exit_reason"] == "soul_mode_error"
    assert "memU turn failed" in result["final_response"]


def test_build_memu_conversation_id_readable_defaults(soul_agent):
    assert soul_agent._build_memu_conversation_id() == "telegram:12345"
    soul_agent.platform = "cron"
    soul_agent._chat_id = "daily-reminder"
    assert soul_agent._build_memu_conversation_id() == "cron:daily-reminder"


def test_build_memu_conversation_id_includes_thread_when_present(soul_agent):
    soul_agent.platform = "telegram"
    soul_agent._chat_id = "-1002285219667"
    soul_agent._thread_id = "17585"
    assert soul_agent._build_memu_conversation_id() == "telegram:-1002285219667:17585"


def test_build_memu_conversation_id_uses_whatsapp_gateway_key_for_dm_alias_stability(soul_agent):
    soul_agent.platform = "whatsapp"
    soul_agent._chat_type = "dm"
    soul_agent._chat_id = "999999999999999@lid"
    soul_agent._gateway_session_key = "agent:main:whatsapp:dm:15551234567"
    assert soul_agent._build_memu_conversation_id() == "whatsapp:dm:15551234567"


def test_build_memu_conversation_id_uses_whatsapp_gateway_key_for_group_per_user_isolation(soul_agent):
    soul_agent.platform = "whatsapp"
    soul_agent._chat_type = "group"
    soul_agent._chat_id = "120363000000000000@g.us"
    soul_agent._gateway_session_key = "agent:main:whatsapp:group:120363000000000000@g.us:15551234567"
    assert (
        soul_agent._build_memu_conversation_id()
        == "whatsapp:group:120363000000000000@g.us:15551234567"
    )


def test_run_soul_turn_uses_text_from_multimodal_parts(soul_agent):
    mock_client = MagicMock()
    mock_client.memu_turn.return_value = {"ok": True, "response": "hello from memu"}
    with patch.object(soul_agent, "_get_memu_client", return_value=mock_client):
        response_text, _ = soul_agent._run_soul_turn(
            user_message=[
                {"type": "text", "text": "Look at this"},
                {"type": "image_url", "image_url": {"url": "data:image/png;base64,abc"}},
            ],
            conversation_history=[],
        )

    assert response_text == "hello from memu"
    assert mock_client.memu_turn.call_args.kwargs["message"] == "Look at this"


def test_run_soul_turn_raises_when_memu_ok_false(soul_agent):
    mock_client = MagicMock()
    mock_client.memu_turn.return_value = {"ok": False, "response": "should not pass"}
    with patch.object(soul_agent, "_get_memu_client", return_value=mock_client):
        with pytest.raises(MemuClientError, match="ok=false"):
            soul_agent._run_soul_turn(user_message="hi", conversation_history=[])


def test_run_conversation_soul_mode_emits_session_start_hook_once(soul_agent):
    history = []
    with (
        patch.object(soul_agent, "_ensure_db_session", return_value=None),
        patch.object(soul_agent, "_restore_primary_runtime", return_value=None),
        patch.object(soul_agent, "_run_soul_turn", return_value=("hello from memu", {"ok": True})),
        patch.object(soul_agent, "_save_trajectory", return_value=None),
        patch.object(soul_agent, "_cleanup_task_resources", return_value=None),
        patch.object(soul_agent, "_persist_session", return_value=None),
        patch("hermes_cli.plugins.invoke_hook") as invoke_hook,
    ):
        soul_agent.run_conversation("hi", conversation_history=history)

    started_calls = [c for c in invoke_hook.call_args_list if c.args and c.args[0] == "on_session_start"]
    assert len(started_calls) == 1
