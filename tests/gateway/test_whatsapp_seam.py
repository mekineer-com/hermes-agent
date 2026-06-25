"""Tests for gateway.whatsapp_seam — fork-only resolver and chat-id parser."""

import json
import pytest

from gateway.whatsapp_seam import resolve_whatsapp_jid, chat_id_from_whatsapp_conversation_id
from agent import soul_mode


# ---------------------------------------------------------------------------
# resolve_whatsapp_jid
# ---------------------------------------------------------------------------

class TestResolveWhatsappJid:
    def test_empty_returns_empty(self, tmp_path, monkeypatch):
        monkeypatch.setenv("HERMES_HOME", str(tmp_path))
        assert resolve_whatsapp_jid("") == ""

    def test_phone_jid_no_mapping_returns_as_is(self, tmp_path, monkeypatch):
        monkeypatch.setenv("HERMES_HOME", str(tmp_path))
        result = resolve_whatsapp_jid("15551234567@s.whatsapp.net")
        assert result == "15551234567@s.whatsapp.net"

    def test_lid_no_mapping_returns_as_is(self, tmp_path, monkeypatch):
        monkeypatch.setenv("HERMES_HOME", str(tmp_path))
        result = resolve_whatsapp_jid("999999999999999@lid")
        assert result == "999999999999999@lid"

    def test_group_jid_passes_through(self, tmp_path, monkeypatch):
        monkeypatch.setenv("HERMES_HOME", str(tmp_path))
        result = resolve_whatsapp_jid("120363000000000000@g.us")
        assert result == "120363000000000000@g.us"

    def test_device_suffix_stripped(self, tmp_path, monkeypatch):
        monkeypatch.setenv("HERMES_HOME", str(tmp_path))
        result = resolve_whatsapp_jid("15551234567:47@s.whatsapp.net")
        assert result == "15551234567@s.whatsapp.net"

    def test_lid_input_preferred_when_mapping_exists(self, tmp_path, monkeypatch):
        """LID input with a forward mapping → returns LID (LID-preferred)."""
        session_dir = tmp_path / "whatsapp" / "session"
        session_dir.mkdir(parents=True)
        (session_dir / "lid-mapping-999999999999999.json").write_text(
            json.dumps("15551234567@s.whatsapp.net"), encoding="utf-8"
        )
        monkeypatch.setenv("HERMES_HOME", str(tmp_path))

        result = resolve_whatsapp_jid("999999999999999@lid")
        assert result == "999999999999999@lid"

    def test_phone_input_finds_lid_via_reverse_mapping(self, tmp_path, monkeypatch):
        """Phone input with a reverse mapping → returns LID (LID-preferred)."""
        session_dir = tmp_path / "whatsapp" / "session"
        session_dir.mkdir(parents=True)
        (session_dir / "lid-mapping-15551234567_reverse.json").write_text(
            json.dumps("999999999999999@lid"), encoding="utf-8"
        )
        monkeypatch.setenv("HERMES_HOME", str(tmp_path))

        result = resolve_whatsapp_jid("15551234567@s.whatsapp.net")
        assert result == "999999999999999@lid"

    def test_both_forms_resolve_to_same_lid(self, tmp_path, monkeypatch):
        """LID and phone forms both resolve to the LID JID — no history split."""
        session_dir = tmp_path / "whatsapp" / "session"
        session_dir.mkdir(parents=True)
        (session_dir / "lid-mapping-999999999999999.json").write_text(
            json.dumps("15551234567@s.whatsapp.net"), encoding="utf-8"
        )
        (session_dir / "lid-mapping-15551234567_reverse.json").write_text(
            json.dumps("999999999999999@lid"), encoding="utf-8"
        )
        monkeypatch.setenv("HERMES_HOME", str(tmp_path))

        lid_result = resolve_whatsapp_jid("999999999999999@lid")
        phone_result = resolve_whatsapp_jid("15551234567@s.whatsapp.net")
        assert lid_result == phone_result == "999999999999999@lid"

    def test_c_us_domain_preserved(self, tmp_path, monkeypatch):
        """@c.us JID is preserved when no mapping exists."""
        monkeypatch.setenv("HERMES_HOME", str(tmp_path))
        result = resolve_whatsapp_jid("15551234567@c.us")
        assert result == "15551234567@c.us"

    def test_corrupt_mapping_file_skipped(self, tmp_path, monkeypatch):
        """A corrupt mapping file is skipped; input is returned unchanged."""
        session_dir = tmp_path / "whatsapp" / "session"
        session_dir.mkdir(parents=True)
        (session_dir / "lid-mapping-999999999999999.json").write_text(
            "not valid json{{{", encoding="utf-8"
        )
        monkeypatch.setenv("HERMES_HOME", str(tmp_path))
        result = resolve_whatsapp_jid("999999999999999@lid")
        assert result == "999999999999999@lid"

    def test_missing_session_dir_returns_input(self, tmp_path, monkeypatch):
        """No session dir → returns the normalised input JID."""
        monkeypatch.setenv("HERMES_HOME", str(tmp_path))
        result = resolve_whatsapp_jid("15551234567@s.whatsapp.net")
        assert result == "15551234567@s.whatsapp.net"


# ---------------------------------------------------------------------------
# chat_id_from_whatsapp_conversation_id
# ---------------------------------------------------------------------------

class TestChatIdFromConversationId:
    def test_dm_prefix_stripped(self):
        assert chat_id_from_whatsapp_conversation_id("whatsapp:dm:15551234567@s.whatsapp.net") == "15551234567@s.whatsapp.net"

    def test_group_prefix_stripped(self):
        assert chat_id_from_whatsapp_conversation_id("whatsapp:group:120363@g.us") == "120363@g.us"

    def test_lid_form(self):
        assert chat_id_from_whatsapp_conversation_id("whatsapp:dm:999999999999999@lid") == "999999999999999@lid"

    def test_unknown_prefix_returns_empty(self):
        assert chat_id_from_whatsapp_conversation_id("telegram:dm:12345") == ""

    def test_empty_returns_empty(self):
        assert chat_id_from_whatsapp_conversation_id("") == ""

    def test_none_like_empty(self):
        assert chat_id_from_whatsapp_conversation_id(None) == ""  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# End-to-end: build_conversation_id emit contract with domain-bearing chat_id
# ---------------------------------------------------------------------------

class TestBuildConversationIdEmitContract:
    """Keystone: domain-bearing DM chat_id resolves via LID mapping at emit layer."""

    def test_dm_with_lid_mapping_emits_lid_preferred(self, tmp_path, monkeypatch):
        """Phone JID + reverse mapping → emits whatsapp:dm:<lid>@lid."""
        session_dir = tmp_path / "whatsapp" / "session"
        session_dir.mkdir(parents=True)
        (session_dir / "lid-mapping-15551234567_reverse.json").write_text(
            json.dumps("999999999999999@lid"), encoding="utf-8"
        )
        monkeypatch.setenv("HERMES_HOME", str(tmp_path))

        resolved = resolve_whatsapp_jid("15551234567@s.whatsapp.net")
        result = soul_mode.build_conversation_id(
            platform="whatsapp",
            chat_id=resolved,
            chat_type="dm",
            gateway_session_key="agent:main:whatsapp:dm:15551234567@s.whatsapp.net",
        )
        assert result == "whatsapp:dm:15551234567@s.whatsapp.net"

    def test_dm_without_mapping_emits_phone_domain(self, tmp_path, monkeypatch):
        """Phone JID with no mapping → emits whatsapp:dm:<phone>@s.whatsapp.net (domain preserved)."""
        monkeypatch.setenv("HERMES_HOME", str(tmp_path))

        resolved = resolve_whatsapp_jid("15551234567@s.whatsapp.net")
        result = soul_mode.build_conversation_id(
            platform="whatsapp",
            chat_id=resolved,
            chat_type="dm",
            gateway_session_key="agent:main:whatsapp:dm:15551234567@s.whatsapp.net",
        )
        assert result == "whatsapp:dm:15551234567@s.whatsapp.net"

    def test_dm_lid_preferred_via_gateway_session_key(self, tmp_path, monkeypatch):
        """LID session key with canonical_fn mapping → emits whatsapp:dm:<lid>@lid."""
        monkeypatch.setenv("HERMES_HOME", str(tmp_path))

        result = soul_mode.build_conversation_id(
            platform="whatsapp",
            chat_id="15551234567@s.whatsapp.net",
            chat_type="dm",
            gateway_session_key="agent:main:whatsapp:dm:999999999999999@lid",
            canonical_whatsapp_fn=lambda v: v,  # identity: LID is already canonical
        )
        assert result == "whatsapp:dm:999999999999999@lid"
