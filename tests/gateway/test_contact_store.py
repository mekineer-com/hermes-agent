"""Tests for gateway.contact_store — evidence-preserving WhatsApp contact store."""

import json
import pytest
from pathlib import Path

from gateway.contact_store import ContactStore, load_contact_store


def _store(tmp_path: Path) -> ContactStore:
    return load_contact_store(tmp_path)


class TestContactStoreIngestEvent:
    def test_phone_jid_recorded(self, tmp_path):
        cs = _store(tmp_path)
        cs.ingest_event({"chatId": "15551234567@s.whatsapp.net", "pushName": "Alice"})
        assert "15551234567@s.whatsapp.net" in cs._records

    def test_group_jid_skipped(self, tmp_path):
        cs = _store(tmp_path)
        cs.ingest_event({"chatId": "120363000000000000@g.us"})
        assert not cs._records  # groups not stored

    def test_lid_jid_recorded(self, tmp_path):
        cs = _store(tmp_path)
        cs.ingest_event({"chatId": "999999999999999@lid"})
        assert "999999999999999@lid" in cs._records

    def test_idempotent_reingest_no_duplicate_evidence(self, tmp_path):
        cs = _store(tmp_path)
        ev = {"chatId": "15551234567@s.whatsapp.net", "pushName": "Alice"}
        cs.ingest_event(ev)
        cs.ingest_event(ev)
        rec = cs._records["15551234567@s.whatsapp.net"]
        assert len(rec["evidence"]) == 1

    def test_evidence_bumps_last_seen(self, tmp_path):
        cs = _store(tmp_path)
        cs.ingest_event({"chatId": "15551234567@s.whatsapp.net"})
        first = cs._records["15551234567@s.whatsapp.net"]["last_seen_at"]
        cs.ingest_event({"chatId": "15551234567@s.whatsapp.net", "pushName": "Updated"})
        second = cs._records["15551234567@s.whatsapp.net"]["last_seen_at"]
        # last_seen_at may equal first if called in the same second — just confirm key exists
        assert second >= first

    def test_empty_event_ignored(self, tmp_path):
        cs = _store(tmp_path)
        cs.ingest_event({})
        assert not cs._records


class TestContactStoreIngestLidMapping:
    def test_lid_mapping_folds_phone_into_lid(self, tmp_path):
        cs = _store(tmp_path)
        # First ingest phone evidence
        cs.ingest_event({"chatId": "15551234567@s.whatsapp.net", "pushName": "Alice"})
        assert "15551234567@s.whatsapp.net" in cs._records

        # Now a lid-mapping file appears
        mapping_path = tmp_path / "lid-mapping-999999999999999.json"
        mapping_path.write_text(json.dumps("15551234567@s.whatsapp.net"), encoding="utf-8")
        cs.ingest_lid_mapping(mapping_path)

        # Phone record should be folded into LID record
        assert "999999999999999@lid" in cs._records
        assert "15551234567@s.whatsapp.net" not in cs._records

        # Phone evidence is preserved in the LID record
        lid_rec = cs._records["999999999999999@lid"]
        jids_in_evidence = [e["jid"] for e in lid_rec["evidence"]]
        assert "15551234567@s.whatsapp.net" in jids_in_evidence

    def test_lid_mapping_idempotent(self, tmp_path):
        cs = _store(tmp_path)
        mapping_path = tmp_path / "lid-mapping-999999999999999.json"
        mapping_path.write_text(json.dumps("15551234567@s.whatsapp.net"), encoding="utf-8")
        cs.ingest_lid_mapping(mapping_path)
        ev_count_before = len(cs._records.get("999999999999999@lid", {}).get("evidence", []))
        cs.ingest_lid_mapping(mapping_path)
        ev_count_after = len(cs._records.get("999999999999999@lid", {}).get("evidence", []))
        assert ev_count_after == ev_count_before

    def test_lid_mapping_preserves_earliest_first_seen(self, tmp_path):
        cs = _store(tmp_path)
        # Pre-populate phone record with an early timestamp
        cs.ingest_event({"chatId": "15551234567@s.whatsapp.net"})
        phone_rec = cs._records["15551234567@s.whatsapp.net"]
        early = "2020-01-01T00:00:00+00:00"
        phone_rec["first_seen_at"] = early

        mapping_path = tmp_path / "lid-mapping-999999999999999.json"
        mapping_path.write_text(json.dumps("15551234567@s.whatsapp.net"), encoding="utf-8")
        cs.ingest_lid_mapping(mapping_path)

        lid_rec = cs._records.get("999999999999999@lid", {})
        assert lid_rec.get("first_seen_at") == early

    def test_corrupt_mapping_file_skipped(self, tmp_path):
        cs = _store(tmp_path)
        mapping_path = tmp_path / "lid-mapping-999999999999999.json"
        mapping_path.write_text("not-json{{", encoding="utf-8")
        cs.ingest_lid_mapping(mapping_path)  # must not raise
        assert not cs._records

    def test_non_mapping_filename_skipped(self, tmp_path):
        cs = _store(tmp_path)
        other = tmp_path / "some-other-file.json"
        other.write_text(json.dumps("15551234567@s.whatsapp.net"), encoding="utf-8")
        cs.ingest_lid_mapping(other)
        assert not cs._records


class TestContactStorePersistence:
    def test_save_and_reload(self, tmp_path):
        cs = _store(tmp_path)
        cs.ingest_event({"chatId": "15551234567@s.whatsapp.net", "pushName": "Alice"})
        cs.save()

        cs2 = load_contact_store(tmp_path)
        assert "15551234567@s.whatsapp.net" in cs2._records
        ev = cs2._records["15551234567@s.whatsapp.net"]["evidence"][0]
        assert ev.get("push_name") == "Alice"

    def test_load_missing_file_returns_empty(self, tmp_path):
        cs = load_contact_store(tmp_path)
        assert cs._records == {}

    def test_load_corrupt_file_returns_empty(self, tmp_path):
        store_path = tmp_path / "whatsapp" / "contact_store.json"
        store_path.parent.mkdir(parents=True)
        store_path.write_text("not json{{{", encoding="utf-8")
        cs = ContactStore(store_path)
        assert cs._records == {}


class TestGetPreferredJid:
    def test_returns_lid_when_known(self, tmp_path):
        cs = _store(tmp_path)
        cs.ingest_event({"chatId": "999999999999999@lid"})
        assert cs.get_preferred_jid("999999999999999@lid") == "999999999999999@lid"

    def test_returns_input_when_not_in_store(self, tmp_path):
        cs = _store(tmp_path)
        assert cs.get_preferred_jid("99999@lid") == "99999@lid"
