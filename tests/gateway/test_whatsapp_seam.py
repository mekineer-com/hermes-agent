"""Tests for gateway.whatsapp_seam — fork-only resolver and chat-id parser."""

import json
import pytest

from gateway.whatsapp_seam import resolve_whatsapp_jid, chat_id_from_whatsapp_conversation_id
from agent import soul_mode
from agent.soul_mode import build_conversation_id


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
        """LID input with a forward mapping (bare phone content) → returns LID (LID-preferred).

        Bridge writes: lid-mapping-{phone}.json → bare LID content.
        When the input is already a LID, a forward file keyed by the LID's bare id
        would only exist if something were keyed on the LID — here we verify LID
        input without any matching forward file still returns LID.
        """
        session_dir = tmp_path / "whatsapp" / "session"
        session_dir.mkdir(parents=True)
        # Forward file keyed by PHONE (bare LID content) — bridge's real scheme.
        (session_dir / "lid-mapping-15551234567.json").write_text(
            json.dumps("999999999999999"), encoding="utf-8"
        )
        monkeypatch.setenv("HERMES_HOME", str(tmp_path))

        result = resolve_whatsapp_jid("999999999999999@lid")
        assert result == "999999999999999@lid"

    def test_phone_input_upgrade_via_forward_mapping(self, tmp_path, monkeypatch):
        """Keystone: phone JID + forward file (bare LID content) → resolver returns <lid>@lid.

        This is the upgrade that was broken: bridge writes lid-mapping-{phone}.json
        containing a bare LID string (no @lid domain). The resolver must infer the
        LID type from the filename scheme (forward → content is a LID), not the value.
        Input:  15551234567@s.whatsapp.net
        Output: 999999999999999@lid
        """
        session_dir = tmp_path / "whatsapp" / "session"
        session_dir.mkdir(parents=True)
        # Forward file: filename scheme says phone→LID; content is bare LID.
        (session_dir / "lid-mapping-15551234567.json").write_text(
            json.dumps("999999999999999"), encoding="utf-8"
        )
        monkeypatch.setenv("HERMES_HOME", str(tmp_path))

        result = resolve_whatsapp_jid("15551234567@s.whatsapp.net")
        assert result == "999999999999999@lid"

    def test_phone_input_finds_lid_via_reverse_mapping(self, tmp_path, monkeypatch):
        """Phone input + reverse file (bare phone content) → returns LID (LID-preferred).

        Bridge writes: lid-mapping-{lid}_reverse.json → bare phone content.
        Resolver infers: current id is the LID; mapped value is the phone.
        Input comes as phone JID → resolver walks phone→(reverse file on LID side,
        which requires first knowing LID bare).  For this case, we use the forward
        file scheme which is more common; reverse is exercised via both-forms test.
        Here we use a _reverse file keyed by the LID bare id, with bare phone content.
        """
        session_dir = tmp_path / "whatsapp" / "session"
        session_dir.mkdir(parents=True)
        # Forward file keyed by phone → bare LID content (the real bridge path).
        (session_dir / "lid-mapping-15551234567.json").write_text(
            json.dumps("999999999999999"), encoding="utf-8"
        )
        # Reverse file keyed by LID → bare phone content.
        (session_dir / "lid-mapping-999999999999999_reverse.json").write_text(
            json.dumps("15551234567"), encoding="utf-8"
        )
        monkeypatch.setenv("HERMES_HOME", str(tmp_path))

        result = resolve_whatsapp_jid("15551234567@s.whatsapp.net")
        assert result == "999999999999999@lid"

    def test_both_forms_resolve_to_same_lid(self, tmp_path, monkeypatch):
        """LID and phone forms both resolve to the LID JID — no history split.

        Fixtures use the real bridge scheme: bare values, typed by filename.
        """
        session_dir = tmp_path / "whatsapp" / "session"
        session_dir.mkdir(parents=True)
        # Forward: phone → bare LID.
        (session_dir / "lid-mapping-15551234567.json").write_text(
            json.dumps("999999999999999"), encoding="utf-8"
        )
        # Reverse: LID → bare phone.
        (session_dir / "lid-mapping-999999999999999_reverse.json").write_text(
            json.dumps("15551234567"), encoding="utf-8"
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
        """Phone JID + forward mapping (bare LID) → emits whatsapp:dm:<lid>@lid.

        Uses the real bridge file scheme: lid-mapping-{phone}.json with bare LID content.
        The resolver must upgrade phone→LID and build_conversation_id must emit the LID form.
        """
        session_dir = tmp_path / "whatsapp" / "session"
        session_dir.mkdir(parents=True)
        # Real bridge scheme: forward file, bare LID content.
        (session_dir / "lid-mapping-15551234567.json").write_text(
            json.dumps("999999999999999"), encoding="utf-8"
        )
        monkeypatch.setenv("HERMES_HOME", str(tmp_path))

        # Mirror real call site: canonical_whatsapp_fn=resolve_whatsapp_jid so
        # the session key's phone JID gets upgraded to LID via the mapping files.
        result = soul_mode.build_conversation_id(
            platform="whatsapp",
            chat_id="15551234567@s.whatsapp.net",
            chat_type="dm",
            gateway_session_key="agent:main:whatsapp:dm:15551234567@s.whatsapp.net",
            canonical_whatsapp_fn=resolve_whatsapp_jid,
        )
        assert result == "whatsapp:dm:999999999999999@lid"

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
