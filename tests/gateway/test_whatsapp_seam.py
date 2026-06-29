import json
import os
from pathlib import Path

from agent import soul_mode
from gateway import whatsapp_seam
from gateway.whatsapp_seam import canonical_whatsapp_jid, chat_id_from_whatsapp_conversation_id, whatsapp_jid_aliases


def test_canonical_whatsapp_jid_preserves_domain_without_mapping(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))

    assert canonical_whatsapp_jid("15551234567@s.whatsapp.net") == "15551234567@s.whatsapp.net"
    assert canonical_whatsapp_jid("15551234567@c.us") == "15551234567@c.us"
    assert canonical_whatsapp_jid("999999999999999@lid") == "999999999999999@lid"
    assert canonical_whatsapp_jid("120363000000000000@g.us") == "120363000000000000@g.us"


def test_canonical_whatsapp_jid_strips_device_suffix(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))

    assert canonical_whatsapp_jid("15551234567:47@s.whatsapp.net") == "15551234567@s.whatsapp.net"


def test_canonical_whatsapp_jid_upgrades_phone_via_forward_bridge_mapping(tmp_path, monkeypatch):
    session_dir = tmp_path / "whatsapp" / "session"
    session_dir.mkdir(parents=True)
    (session_dir / "lid-mapping-15551234567.json").write_text(json.dumps("999999999999999"), encoding="utf-8")
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))

    assert canonical_whatsapp_jid("15551234567@s.whatsapp.net") == "999999999999999@lid"
    assert canonical_whatsapp_jid("15551234567@c.us") == "999999999999999@lid"
    assert whatsapp_jid_aliases("999999999999999@lid") >= {
        "15551234567@s.whatsapp.net",
        "15551234567@c.us",
        "999999999999999@lid",
    }


def test_canonical_whatsapp_jid_caches_alias_graph(tmp_path, monkeypatch):
    session_dir = tmp_path / "whatsapp" / "session"
    session_dir.mkdir(parents=True)
    (session_dir / "lid-mapping-15551234567.json").write_text(json.dumps("999999999999999"), encoding="utf-8")
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    whatsapp_seam._load_alias_graph_cached.cache_clear()
    whatsapp_seam._signature_cache.clear()

    assert canonical_whatsapp_jid("15551234567@s.whatsapp.net") == "999999999999999@lid"

    def fail_read_text(self: Path, *args, **kwargs):
        raise AssertionError(f"alias graph cache miss for {self}")

    monkeypatch.setattr(Path, "read_text", fail_read_text)

    assert canonical_whatsapp_jid("15551234567@c.us") == "999999999999999@lid"


def test_canonical_whatsapp_jid_cache_invalidates_when_mapping_file_rewritten(tmp_path, monkeypatch):
    session_dir = tmp_path / "whatsapp" / "session"
    session_dir.mkdir(parents=True)
    mapping = session_dir / "lid-mapping-15551234567.json"
    mapping.write_text(json.dumps("111111111111111"), encoding="utf-8")
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    whatsapp_seam._load_alias_graph_cached.cache_clear()
    whatsapp_seam._signature_cache.clear()
    monotonic = [1.0]
    monkeypatch.setattr(whatsapp_seam.time, "monotonic", lambda: monotonic[0])

    assert canonical_whatsapp_jid("15551234567@s.whatsapp.net") == "111111111111111@lid"

    mapping.write_text(json.dumps("222222222222222"), encoding="utf-8")
    os.utime(mapping, ns=(2_000_000_000, 2_000_000_000))
    monotonic[0] += whatsapp_seam._SIGNATURE_TTL_SECONDS + 0.1

    assert canonical_whatsapp_jid("15551234567@s.whatsapp.net") == "222222222222222@lid"


def test_canonical_whatsapp_jid_upgrades_phone_via_reverse_bridge_mapping(tmp_path, monkeypatch):
    session_dir = tmp_path / "whatsapp" / "session"
    session_dir.mkdir(parents=True)
    (session_dir / "lid-mapping-999999999999999_reverse.json").write_text(json.dumps("15551234567"), encoding="utf-8")
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))

    assert canonical_whatsapp_jid("15551234567@s.whatsapp.net") == "999999999999999@lid"
    assert canonical_whatsapp_jid("999999999999999@lid") == "999999999999999@lid"


def test_canonical_whatsapp_jid_uses_creds_self_alias(tmp_path, monkeypatch):
    session_dir = tmp_path / "whatsapp" / "session"
    session_dir.mkdir(parents=True)
    (session_dir / "creds.json").write_text(
        json.dumps({"me": {"id": "15551234567:1@s.whatsapp.net", "lid": "999999999999999@lid"}}),
        encoding="utf-8",
    )
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))

    assert canonical_whatsapp_jid("15551234567@s.whatsapp.net") == "999999999999999@lid"


def test_chat_id_from_whatsapp_conversation_id_strips_known_prefixes():
    assert (
        chat_id_from_whatsapp_conversation_id("whatsapp:dm:15551234567@s.whatsapp.net")
        == "15551234567@s.whatsapp.net"
    )
    assert chat_id_from_whatsapp_conversation_id("whatsapp:group:120363@g.us") == "120363@g.us"
    assert chat_id_from_whatsapp_conversation_id("telegram:123") == ""
    assert chat_id_from_whatsapp_conversation_id(None) == ""


def test_build_conversation_id_emits_lid_when_mapping_exists(tmp_path, monkeypatch):
    session_dir = tmp_path / "whatsapp" / "session"
    session_dir.mkdir(parents=True)
    (session_dir / "lid-mapping-15551234567.json").write_text(json.dumps("999999999999999"), encoding="utf-8")
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))

    assert soul_mode.build_conversation_id(
        platform="whatsapp",
        chat_id="15551234567@s.whatsapp.net",
        chat_type="dm",
        gateway_session_key="agent:main:whatsapp:dm:15551234567@s.whatsapp.net",
        canonical_whatsapp_fn=canonical_whatsapp_jid,
    ) == "whatsapp:dm:999999999999999@lid"
