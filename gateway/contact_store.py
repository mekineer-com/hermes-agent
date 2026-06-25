"""Evidence-preserving WhatsApp contact store.

Persists to ``~/.hermes/whatsapp/contact_store.json``.  Each record is keyed
by the best-known JID (LID when one is known, phone JID otherwise).  Evidence
rows are appended, never overwritten.  The preferred identity is computed from
the evidence at read time, not stored as truth.

Feed sources:
- Gateway WAL events (live ingest + replay)
- ``lid-mapping-*.json`` files in the bridge session directory

Hook points in ``gateway/platforms/whatsapp.py``:
- Instantiate alongside the WAL (``__init__``).
- Call ``ingest_event(msg_data)`` where ``wal.append(msg_data)`` runs (~1697).
- Call ``ingest_event(event_data)`` in ``_replay_gateway_wal`` (~1977).
"""

from __future__ import annotations

import json
import logging
import os
import re
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)

_LID_MAPPING_RE = re.compile(r"^lid-mapping-(.+?)(?:_reverse)?\.json$")


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _normalize_jid(raw: str) -> str:
    """Strip device suffix and leading '+'; keep @domain."""
    raw = str(raw or "").strip().lstrip("+")
    local, _, domain = raw.partition("@")
    local = local.split(":")[0]
    if domain:
        return f"{local}@{domain}"
    return local


def _bare(jid: str) -> str:
    return jid.split("@")[0].split(":")[0].lstrip("+")


class ContactStore:
    """Append-only evidence store for WhatsApp contacts."""

    def __init__(self, path: Path) -> None:
        self._path = path
        self._records: Dict[str, Dict[str, Any]] = {}  # key_jid → record
        self._load()

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def ingest_event(self, event: Dict[str, Any]) -> None:
        """Extract identity evidence from a WAL/bridge event and update store."""
        fields = {
            "chat_id": str(event.get("chatId") or event.get("chat_id") or "").strip(),
            "sender_id": str(event.get("senderId") or event.get("sender_id") or "").strip(),
            "participant": str(event.get("participant") or "").strip(),
            "remote_jid": str(event.get("remoteJid") or event.get("remote_jid") or "").strip(),
            "push_name": str(event.get("pushName") or event.get("push_name") or "").strip(),
            "verified_name": str(event.get("verifiedName") or event.get("verified_name") or "").strip(),
            "name": str(event.get("name") or "").strip(),
        }
        # Only process WhatsApp DM-shaped IDs (not groups).
        jids = [
            fields["chat_id"],
            fields["sender_id"],
            fields["participant"],
            fields["remote_jid"],
        ]
        for raw_jid in jids:
            if not raw_jid or raw_jid.endswith("@g.us"):
                continue
            norm = _normalize_jid(raw_jid)
            if norm:
                self._upsert(
                    observed_jid=norm,
                    raw_observed=raw_jid,
                    push_name=fields["push_name"],
                    verified_name=fields["verified_name"],
                    name=fields["name"],
                    source="wal",
                )

    def ingest_lid_mapping(self, mapping_path: Path) -> None:
        """Read a ``lid-mapping-*.json`` file and merge phone ↔ LID evidence."""
        m = _LID_MAPPING_RE.match(mapping_path.name)
        if m is None:
            return
        key_bare = m.group(1)
        try:
            raw_value = json.loads(mapping_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            logger.debug("contact_store: failed to read %s: %s", mapping_path, exc)
            return
        if not isinstance(raw_value, str):
            return
        mapped_norm = _normalize_jid(raw_value)
        key_norm = _normalize_jid(
            f"{key_bare}@lid" if "_reverse" not in mapping_path.name else raw_value
        )
        # Determine which is LID and which is phone.
        lid_jid = mapped_norm if mapped_norm.endswith("@lid") else None
        phone_jid = mapped_norm if not mapped_norm.endswith("@lid") else None
        if not lid_jid:
            lid_jid = key_norm if key_norm.endswith("@lid") else None
        if not phone_jid:
            phone_jid = key_norm if not key_norm.endswith("@lid") else None

        if lid_jid:
            self._upsert(
                observed_jid=lid_jid,
                raw_observed=raw_value,
                source="lid_mapping",
                merge_reason=f"lid-mapping: {mapping_path.name}",
            )
        if phone_jid:
            self._upsert(
                observed_jid=phone_jid,
                raw_observed=raw_value,
                source="lid_mapping",
                merge_reason=f"lid-mapping: {mapping_path.name}",
            )
        # If both sides are known, fold any phone-keyed record into LID record.
        if lid_jid and phone_jid:
            self._merge_phone_into_lid(lid_jid=lid_jid, phone_jid=phone_jid)

    def save(self) -> None:
        """Atomically persist the store to disk."""
        data = json.dumps(self._records, ensure_ascii=False, indent=2)
        _write_text_atomically(self._path, data)

    def get_preferred_jid(self, raw: str) -> str:
        """Return the LID JID for *raw* if known, else the phone JID, else *raw*."""
        norm = _normalize_jid(raw)
        bare = _bare(norm)
        # Search by key
        if norm in self._records:
            return norm
        # Search by bare local part
        for key, rec in self._records.items():
            for ev in rec.get("evidence", []):
                ev_bare = _bare(str(ev.get("jid", "")))
                if ev_bare == bare:
                    return key
        return norm

    # ------------------------------------------------------------------
    # Internal
    # ------------------------------------------------------------------

    def _load(self) -> None:
        if not self._path.exists():
            return
        try:
            raw = json.loads(self._path.read_text(encoding="utf-8"))
            if isinstance(raw, dict):
                self._records = raw
        except (OSError, json.JSONDecodeError) as exc:
            logger.warning("contact_store: corrupt store at %s, starting empty: %s", self._path, exc)
            self._records = {}

    def _upsert(
        self,
        *,
        observed_jid: str,
        raw_observed: str = "",
        push_name: str = "",
        verified_name: str = "",
        name: str = "",
        source: str = "wal",
        merge_reason: str = "",
    ) -> None:
        """Insert or update a record keyed by *observed_jid*."""
        if not observed_jid:
            return
        now = _now_iso()
        # Find existing record by this JID or by bare local part.
        key = self._find_key(observed_jid)
        if key is None:
            key = observed_jid
            self._records[key] = {
                "preferred_jid": observed_jid,
                "first_seen_at": now,
                "last_seen_at": now,
                "evidence": [],
            }
        rec = self._records[key]
        rec["last_seen_at"] = now

        # Append evidence row only if not duplicate.
        ev_entry: Dict[str, Any] = {
            "jid": observed_jid,
            "raw_observed": raw_observed or observed_jid,
            "source": source,
            "seen_at": now,
        }
        if push_name:
            ev_entry["push_name"] = push_name
        if verified_name:
            ev_entry["verified_name"] = verified_name
        if name:
            ev_entry["name"] = name
        if merge_reason:
            ev_entry["merge_reason"] = merge_reason

        # Idempotent: skip if identical content already recorded.
        sig = (observed_jid, source, push_name, verified_name, name, merge_reason)
        existing_sigs = {
            (e.get("jid"), e.get("source"), e.get("push_name", ""),
             e.get("verified_name", ""), e.get("name", ""), e.get("merge_reason", ""))
            for e in rec.get("evidence", [])
        }
        if sig not in existing_sigs:
            rec.setdefault("evidence", []).append(ev_entry)

        # Update preferred_jid: LID wins over phone.
        current_pref = rec.get("preferred_jid", key)
        if observed_jid.endswith("@lid") and not current_pref.endswith("@lid"):
            rec["preferred_jid"] = observed_jid

    def _find_key(self, jid: str) -> Optional[str]:
        """Find the record key that owns *jid* (exact match or same bare local)."""
        if jid in self._records:
            return jid
        bare = _bare(jid)
        for key in self._records:
            if _bare(key) == bare:
                return key
        return None

    def _merge_phone_into_lid(self, *, lid_jid: str, phone_jid: str) -> None:
        """Fold any phone-keyed record into the LID-keyed record."""
        phone_key = self._find_key(phone_jid)
        lid_key = self._find_key(lid_jid)

        if phone_key is None or phone_key == lid_key:
            return

        phone_rec = self._records.pop(phone_key)
        if lid_key is None:
            # No LID record yet — re-key the phone record under LID.
            phone_rec["preferred_jid"] = lid_jid
            self._records[lid_jid] = phone_rec
            return

        lid_rec = self._records[lid_key]
        # Keep earliest first_seen_at.
        if phone_rec.get("first_seen_at", "") < lid_rec.get("first_seen_at", ""):
            lid_rec["first_seen_at"] = phone_rec["first_seen_at"]
        # Merge evidence rows (idempotent).
        existing = {
            (e.get("jid"), e.get("source"), e.get("push_name", ""),
             e.get("verified_name", ""), e.get("name", ""), e.get("merge_reason", ""))
            for e in lid_rec.get("evidence", [])
        }
        for ev in phone_rec.get("evidence", []):
            sig = (ev.get("jid"), ev.get("source"), ev.get("push_name", ""),
                   ev.get("verified_name", ""), ev.get("name", ""), ev.get("merge_reason", ""))
            if sig not in existing:
                lid_rec.setdefault("evidence", []).append(ev)
                existing.add(sig)
        lid_rec["preferred_jid"] = lid_jid


def load_contact_store(hermes_home: Path) -> ContactStore:
    """Load (or create empty) the contact store from *hermes_home*."""
    path = hermes_home / "whatsapp" / "contact_store.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    return ContactStore(path)


def _write_text_atomically(path: Path, data: str) -> None:
    """Atomic write: temp file → fsync → os.replace → dir-fsync.

    Copied from ``whatsapp_wal.py`` pattern (lines 170–193).
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_path = tempfile.mkstemp(
        dir=str(path.parent),
        prefix=f".{path.name}.",
        suffix=".tmp",
    )
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp_path, path)
        dir_fd = os.open(str(path.parent), os.O_RDONLY)
        try:
            os.fsync(dir_fd)
        finally:
            os.close(dir_fd)
    except BaseException:
        try:
            os.unlink(tmp_path)
        except OSError:
            pass
        raise
