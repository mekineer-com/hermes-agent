from agent.memu_client import MemuHttpClient, normalize_history_for_memu


def test_normalize_history_for_memu_filters_and_keeps_timestamps():
    history = [
        {"role": "system", "content": "ignore this"},
        {"role": "user", "content": "hello", "timestamp": 1700000000},
        {"role": "assistant", "content": [{"type": "text", "text": "hi there"}], "created_at": "2026-05-03T10:00:00Z"},
        {"role": "tool", "content": "ignored"},
    ]

    out = normalize_history_for_memu(history)

    assert len(out) == 2
    assert out[0]["role"] == "user"
    assert out[0]["content"] == "hello"
    assert out[0]["ts_ms"] == 1700000000000
    assert out[1]["role"] == "assistant"
    assert out[1]["content"] == "hi there"
    assert isinstance(out[1]["ts_ms"], int)


def test_memu_turn_builds_expected_payload(monkeypatch):
    client = MemuHttpClient(base_url="http://127.0.0.1:8099")
    captured = {}

    def _fake_post(path, payload):
        captured["path"] = path
        captured["payload"] = payload
        return {"ok": True, "response": "hi"}

    monkeypatch.setattr(client, "_post", _fake_post)

    out = client.memu_turn(
        conversation_id="telegram:123",
        user_id="marcos",
        soul_id="Echo",
        message="hello",
        history=[{"role": "user", "content": "prior", "timestamp": 1700000000}],
    )

    assert out["ok"] is True
    assert captured["path"] == "/integration/memu/turn"
    payload = captured["payload"]
    assert payload["conversation_id"] == "telegram:123"
    assert payload["user_id"] == "marcos"
    assert payload["soul_id"] == "Echo"
    assert payload["message"] == "hello"
    assert payload["history"][0]["content"] == "prior"
    assert payload["history"][0]["ts_ms"] == 1700000000000


def test_memu_turn_does_not_force_fill_user_name_from_history_user_name(monkeypatch):
    client = MemuHttpClient(base_url="http://127.0.0.1:8099")
    captured = {}

    def _fake_post(path, payload):
        captured["path"] = path
        captured["payload"] = payload
        return {"ok": True, "response": "ok"}

    monkeypatch.setattr(client, "_post", _fake_post)

    out = client.memu_turn(
        conversation_id="whatsapp:dm:247789598601266",
        user_id="Marcos",
        soul_id="Echo",
        message="hello",
        history=[{"role": "user", "content": "prior"}],
        history_user_name="Liz Kalverda",
    )

    assert out["ok"] is True
    payload = captured["payload"]
    assert "name" not in payload["history"][0]


def test_normalize_history_for_memu_still_fills_assistant_name_from_soul_name():
    out = normalize_history_for_memu(
        [{"role": "assistant", "content": "hi"}],
        soul_name="Echo",
    )
    assert out == [{"role": "assistant", "content": "hi", "name": "Echo"}]
