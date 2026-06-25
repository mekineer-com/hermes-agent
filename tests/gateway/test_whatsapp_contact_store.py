import json

from gateway.contact_store import WhatsAppContactStore
from gateway.whatsapp_identity import to_whatsapp_jid
from gateway.whatsapp_seam import canonical_whatsapp_jid


def test_to_whatsapp_jid_expands_bare_phone_and_preserves_lid():
    assert to_whatsapp_jid("+1 (555) 123-4567") == "15551234567@s.whatsapp.net"
    assert to_whatsapp_jid("999999999999999@lid") == "999999999999999@lid"
    assert to_whatsapp_jid("15551234567:47@s.whatsapp.net") == "15551234567@s.whatsapp.net"


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
    assert any(row.get("merge_reason") == "lid_phone_mapping" for row in record["evidence"])
