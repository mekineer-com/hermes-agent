from types import SimpleNamespace

from agent.conversation_loop import run_conversation
from agent.turn_context import TurnContext


def test_run_conversation_delegates_active_soul_mode(monkeypatch):
    agent = SimpleNamespace(
        api_mode="chat_completions",
        _soul_config=SimpleNamespace(is_active=lambda: True),
        session_id="s1",
    )
    ctx = TurnContext(
        user_message="hello",
        original_user_message="hello",
        messages=[{"role": "user", "content": "hello"}],
        conversation_history=[],
        active_system_prompt="SYSTEM",
        effective_task_id="task-1",
        turn_id="turn-1",
        current_turn_user_idx=0,
    )
    calls = []

    monkeypatch.setattr(
        "agent.conversation_loop.build_turn_context",
        lambda *a, **k: ctx,
    )

    def fake_handle_turn(agent_arg, config_arg, **kwargs):
        calls.append((agent_arg, config_arg, kwargs))
        return {"final_response": "hi", "messages": ctx.messages}

    monkeypatch.setattr(
        "agent.conversation_loop._soul_mode.handle_turn",
        fake_handle_turn,
    )

    result = run_conversation(agent, "hello")

    assert result["final_response"] == "hi"
    assert calls[0][0] is agent
    assert calls[0][1] is agent._soul_config
    assert calls[0][2]["messages"] is ctx.messages
    assert calls[0][2]["task_id"] == "task-1"
