import json

from gateway.contact_store import WhatsAppContactStore
from gateway.whatsapp_identity import to_whatsapp_jid
from gateway.whatsapp_seam import canonical_whatsapp_jid


def test_to_whatsapp_jid_expands_bare_phone_and_preserves_lid():
    assert to_whatsapp_jid("+1 (555) 123-4567") == "15551234567@s.whatsapp.net"
    assert to_whatsapp_jid("999999999999999@lid") == "999999999999999@lid"
    assert to_whatsapp_jid("15551234567:47@s.whatsapp.net") == "15551234567@s.whatsapp.net"
    assert to_whatsapp_jid("alice") == "alice"


def test_canonical_whatsapp_jid_prefers_lid_when_mapping_exists(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    session_dir = tmp_path / "whatsapp" / "session"
    session_dir.mkdir(parents=True)
    (session_dir / "lid-mapping-15551234567.json").write_text(json.dumps("999999999999999"), encoding="utf-8")

    assert canonical_whatsapp_jid("15551234567@s.whatsapp.net") == "999999999999999@lid"
    assert canonical_whatsapp_jid("999999999999999@lid") == "999999999999999@lid"


def test_contact_store_merges_phone_record_when_lid_mapping_arrives(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    root = tmp_path / "whatsapp"
    session_dir = root / "session"
    session_dir.mkdir(parents=True)
    store = WhatsAppContactStore(store_path=root / "contact_store.json", session_dir=session_dir)

    store.update_from_event(
        {
            "chatId": "15551234567@s.whatsapp.net",
            "senderId": "15551234567@s.whatsapp.net",
            "senderName": "Phone Contact",
            "chatName": "Phone Contact",
        }
    )
    (session_dir / "lid-mapping-15551234567.json").write_text(json.dumps("999999999999999"), encoding="utf-8")
    store.ingest_lid_mappings()

    data = json.loads((root / "contact_store.json").read_text(encoding="utf-8"))
    assert list(data["contacts"]) == ["999999999999999@lid"]
    record = data["contacts"]["999999999999999@lid"]
    assert set(record["aliases"]) >= {"15551234567@s.whatsapp.net", "999999999999999@lid"}
    assert record["id"] == "999999999999999@lid"
    assert record["lid_jid"] == "999999999999999@lid"
    assert record["phone_jid"] == "15551234567@s.whatsapp.net"
    assert record["bare_phone"] == "15551234567"
    assert record["display_name"] == "Phone Contact"
    assert record["observed_names"] == ["Phone Contact"]
    assert any(row.get("merge_reason") == "lid_phone_mapping" for row in record["evidence"])


def test_contact_store_merges_reverse_lid_mapping(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    root = tmp_path / "whatsapp"
    session_dir = root / "session"
    session_dir.mkdir(parents=True)
    store = WhatsAppContactStore(store_path=root / "contact_store.json", session_dir=session_dir)

    store.update_from_event({"chatId": "15551234567@s.whatsapp.net"})
    (session_dir / "lid-mapping-999999999999999_reverse.json").write_text(json.dumps("15551234567"), encoding="utf-8")
    store.ingest_lid_mappings()

    data = json.loads((root / "contact_store.json").read_text(encoding="utf-8"))
    assert list(data["contacts"]) == ["999999999999999@lid"]
    assert "15551234567@s.whatsapp.net" in data["contacts"]["999999999999999@lid"]["aliases"]


def test_contact_store_persists_event_updates(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    root = tmp_path / "whatsapp"
    session_dir = root / "session"
    session_dir.mkdir(parents=True)
    store_path = root / "contact_store.json"
    store = WhatsAppContactStore(store_path=store_path, session_dir=session_dir)

    store.update_from_event({"chatId": "15551234567@s.whatsapp.net", "chatName": "Phone Contact"})

    data = json.loads(store_path.read_text(encoding="utf-8"))
    record = data["contacts"]["15551234567@s.whatsapp.net"]
    assert record["display"]["chat_name"] == "Phone Contact"
    assert record["phone_jid"] == "15551234567@s.whatsapp.net"
    assert record["bare_phone"] == "15551234567"
    assert record["display_name"] == "Phone Contact"


def test_contact_store_display_name_ignores_numeric_placeholder(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    root = tmp_path / "whatsapp"
    session_dir = root / "session"
    session_dir.mkdir(parents=True)
    store_path = root / "contact_store.json"
    store = WhatsAppContactStore(store_path=store_path, session_dir=session_dir)

    store.update_from_event({"chatId": "999999999999999@lid", "chatName": "999999999999999"})
    store.update_from_event({"chatId": "999999999999999@lid", "chatName": "Annie Gottlieb"})

    data = json.loads(store_path.read_text(encoding="utf-8"))
    record = data["contacts"]["999999999999999@lid"]
    assert record["lid_jid"] == "999999999999999@lid"
    assert record["display_name"] == "Annie Gottlieb"
    assert record["observed_names"] == ["Annie Gottlieb"]
