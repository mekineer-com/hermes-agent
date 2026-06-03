from pathlib import Path


def test_gateway_sender_id_initialized_before_whatsapp_only_assignment() -> None:
    source = Path("gateway/run.py").read_text(encoding="utf-8")
    marker = (
        '            _sender_id = ""\n'
        "            if source.platform == Platform.WHATSAPP:\n"
        '                _sender_id = str(_event_raw.get("senderId") or "").strip()\n'
    )

    assert marker in source
