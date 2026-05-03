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
