import pytest

from agent.memu_client import MemuClientError, MemuHttpClient, normalize_history_for_memu


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
    # When the caller omits chat_name / chat_type the keys must be absent
    # (legacy callers + tests should keep working).
    assert "chat_name" not in payload
    assert "chat_type" not in payload


def test_memu_turn_forwards_chat_name_and_chat_type(monkeypatch):
    """chat_name and chat_type must flow through to memu's payload so the
    server can render `Current chat:` in the turn prompt.
    """
    client = MemuHttpClient(base_url="http://127.0.0.1:8099")
    captured = {}

    def _fake_post(path, payload):
        captured["payload"] = payload
        return {"ok": True, "response": "hi"}

    monkeypatch.setattr(client, "_post", _fake_post)

    client.memu_turn(
        conversation_id="whatsapp:dm:19999999999",
        user_id="Marcos",
        soul_id="Echo",
        message="hi",
        chat_name="Alice",
        chat_type="dm",
    )

    payload = captured["payload"]
    assert payload["chat_name"] == "Alice"
    assert payload["chat_type"] == "dm"


def test_memu_turn_does_not_forward_timezone(monkeypatch):
    client = MemuHttpClient(base_url="http://127.0.0.1:8099")
    captured = {}

    def _fake_post(path, payload):
        captured["payload"] = payload
        return {"ok": True, "response": "hi"}

    monkeypatch.setattr(client, "_post", _fake_post)

    client.memu_turn(
        conversation_id="whatsapp:dm:19999999999",
        user_id="Marcos",
        soul_id="Siri",
        message="hi",
    )

    payload = captured["payload"]
    assert "time_zone" not in payload
    assert "time_zone_offset_min" not in payload


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


def test_normalize_history_for_memu_preserves_sender_name():
    out = normalize_history_for_memu(
        [{"role": "user", "content": "hello", "sender_name": "Raquel"}],
        soul_name="Siri",
    )
    assert out == [{"role": "user", "content": "hello", "name": "Raquel"}]


def test_memu_turn_passes_user_name_for_current_message_speaker(monkeypatch):
    client = MemuHttpClient(base_url="http://127.0.0.1:8099")
    captured = {}

    def _fake_post(path, payload):
        captured["payload"] = payload
        return {"ok": True, "response": "ok"}

    monkeypatch.setattr(client, "_post", _fake_post)

    client.memu_turn(
        conversation_id="whatsapp:dm:247789598601266",
        user_id="Marcos",
        soul_id="Echo",
        message="hi",
        user_name="Liz Kalverda",
    )

    assert captured["payload"]["user_name"] == "Liz Kalverda"


def test_memu_turn_falls_back_to_history_user_name_when_user_name_missing(monkeypatch):
    client = MemuHttpClient(base_url="http://127.0.0.1:8099")
    captured = {}

    def _fake_post(path, payload):
        captured["payload"] = payload
        return {"ok": True, "response": "ok"}

    monkeypatch.setattr(client, "_post", _fake_post)

    client.memu_turn(
        conversation_id="whatsapp:dm:247789598601266",
        user_id="Marcos",
        soul_id="Echo",
        message="hi",
        history_user_name="Marcos",
    )

    assert captured["payload"]["user_name"] == "Marcos"


def test_claim_whatsapp_outbounds_builds_expected_payload(monkeypatch):
    client = MemuHttpClient(base_url="http://127.0.0.1:8099")
    captured = {}

    def _fake_post(path, payload):
        captured["path"] = path
        captured["payload"] = payload
        return {"ok": True, "outbounds": [{"id": "waout_1"}]}

    monkeypatch.setattr(client, "_post", _fake_post)

    out = client.claim_whatsapp_outbounds(
        user_id="marcos",
        soul_id="Siri",
        claimed_by="hermes-test",
        limit=3,
    )

    assert out == [{"id": "waout_1"}]
    assert captured["path"] == "/integration/whatsapp/outbounds/claim"
    assert captured["payload"]["user_id"] == "marcos"
    assert captured["payload"]["soul_id"] == "Siri"
    assert captured["payload"]["claimed_by"] == "hermes-test"
    assert captured["payload"]["limit"] == 3


def test_mark_whatsapp_outbound_builds_expected_payload(monkeypatch):
    client = MemuHttpClient(base_url="http://127.0.0.1:8099")
    captured = {}

    def _fake_post(path, payload):
        captured["path"] = path
        captured["payload"] = payload
        return {"ok": True, "outbound": {"id": "waout_1", "status": "sent"}}

    monkeypatch.setattr(client, "_post", _fake_post)

    out = client.mark_whatsapp_outbound(
        user_id="marcos",
        soul_id="Siri",
        outbound_id="waout_1",
        status="sent",
        provider_message_id="wamid.1",
    )

    assert out["ok"] is True
    assert captured["path"] == "/integration/whatsapp/outbounds/mark"
    assert captured["payload"]["outbound_id"] == "waout_1"
    assert captured["payload"]["status"] == "sent"
    assert captured["payload"]["provider_message_id"] == "wamid.1"


def test_post_wraps_timeout_error_as_memu_client_error(monkeypatch):
    client = MemuHttpClient(base_url="http://127.0.0.1:8099")

    def _fake_urlopen(*_args, **_kwargs):
        raise TimeoutError("timed out")

    monkeypatch.setattr("urllib.request.urlopen", _fake_urlopen)

    with pytest.raises(MemuClientError, match=r"memU request timed out: timed out"):
        client._post("/integration/memu/turn", {"message": "hi"})
