"""Evidence-preserving WhatsApp contact store for OpenAlma."""

from __future__ import annotations

import json
import logging
import os
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .whatsapp_identity import to_whatsapp_jid
from .whatsapp_known_contacts import is_placeholder_whatsapp_name
from .whatsapp_seam import canonical_whatsapp_jid, whatsapp_jid_aliases

logger = logging.getLogger(__name__)

_ID_FIELDS = (
    "chatId",
    "senderId",
    "participant",
    "remoteJid",
    "quotedParticipant",
    "quotedRemoteJid",
)


class WhatsAppContactStore:
    def __init__(self, *, store_path: Path):
        self.store_path = store_path
        self._data: dict[str, Any] | None = None

    def update_from_event(self, event: dict[str, Any], *, source: str = "gateway_wal") -> None:
        if not isinstance(event, dict):
            return

        changed = False
        for field in _ID_FIELDS:
            if self._record_id(event.get(field), source=source, raw_field=field, event=event):
                changed = True
        for field in ("mentionedIds", "botIds"):
            values = event.get(field)
            if isinstance(values, list):
                for value in values:
                    if self._record_id(value, source=source, raw_field=field, event=event):
                        changed = True
        if changed:
            self._save()

    def _record_id(
        self,
        value: Any,
        *,
        source: str,
        raw_field: str,
        event: dict[str, Any],
    ) -> bool:
        jid = _normalize_contact_jid(value)
        if not jid:
            return False
        display = self._display_for_field(raw_field, event)
        return self._upsert_evidence(
            jid,
            {
                "source": source,
                "raw_field": raw_field,
                "raw_id": str(value or "").strip(),
                "jid": jid,
                "display": display,
            },
        )

    def _upsert_evidence(self, jid: str, evidence: dict[str, Any]) -> bool:
        data = self._load()
        record = self._record_for_jid(jid)
        now = _utc_now()
        changed = False

        aliases = set(record.setdefault("aliases", []))
        for alias in whatsapp_jid_aliases(jid) or {jid, canonical_whatsapp_jid(jid)}:
            if alias and alias not in aliases:
                aliases.add(alias)
                changed = True
        record["aliases"] = sorted(aliases)
        preferred = _preferred_contact_jid(record, aliases)
        if preferred and record.get("preferred_jid") != preferred:
            record["preferred_jid"] = preferred
            changed = True
        record.setdefault("first_seen_at", now)
        if record.get("last_seen_at") != now:
            changed = True
        record["last_seen_at"] = now

        evidence = {key: value for key, value in evidence.items() if value not in ("", None, [], {})}
        evidence.setdefault("first_seen_at", now)
        evidence["last_seen_at"] = now
        rows = record.setdefault("evidence", [])
        existing_row = next((row for row in rows if isinstance(row, dict) and _same_evidence(row, evidence)), None)
        if existing_row is None:
            rows.append(evidence)
            changed = True
        elif existing_row.get("last_seen_at") != now:
            existing_row["last_seen_at"] = now
            changed = True

        display = evidence.get("display")
        if isinstance(display, dict):
            labels = record.setdefault("display", {})
            for key, value in display.items():
                if value and labels.get(key) != value:
                    labels[key] = value
                    changed = True

        self._move_record_to_preferred(record)
        if _refresh_contact_columns(record):
            changed = True
        data["updated_at"] = now
        return changed

    def _record_for_jid(self, jid: str) -> dict[str, Any]:
        data = self._load()
        preferred = canonical_whatsapp_jid(jid) or jid
        contacts = data.setdefault("contacts", {})
        for key, record in list(contacts.items()):
            aliases = set(record.get("aliases") or [])
            if jid == key or preferred == key or jid in aliases or preferred in aliases:
                return record
        record = {"preferred_jid": preferred, "aliases": sorted({jid, preferred}), "evidence": []}
        contacts[preferred] = record
        return record

    def _move_record_to_preferred(self, record: dict[str, Any]) -> None:
        data = self._load()
        contacts = data.setdefault("contacts", {})
        aliases = set(record.get("aliases") or [])
        preferred = _preferred_contact_jid(record, aliases)
        if not preferred:
            return
        aliases.add(preferred)
        record["preferred_jid"] = preferred
        record["aliases"] = sorted(aliases)
        for key, value in list(contacts.items()):
            if value is record:
                continue
            value_aliases = set(value.get("aliases") or [])
            if key == preferred or aliases.intersection(value_aliases):
                _merge_records(record, value)
                aliases = set(record.get("aliases") or [])
                del contacts[key]
        existing = contacts.get(preferred)
        if existing is not None and existing is not record:
            _merge_records(existing, record)
            record = existing
        for key, value in list(contacts.items()):
            if value is record and key != preferred:
                del contacts[key]
        contacts[preferred] = record
        _refresh_contact_columns(record)

    def _display_for_field(self, raw_field: str, event: dict[str, Any]) -> dict[str, str]:
        if raw_field == "chatId":
            return {"chat_name": str(event.get("chatName") or "").strip()}
        if raw_field in {"senderId", "participant"}:
            return {"sender_name": str(event.get("senderName") or "").strip()}
        return {}

    def _load(self) -> dict[str, Any]:
        if self._data is not None:
            return self._data
        try:
            parsed = json.loads(self.store_path.read_text(encoding="utf-8"))
        except (FileNotFoundError, OSError, json.JSONDecodeError) as exc:
            if not isinstance(exc, FileNotFoundError):
                logger.warning("contact_store: ignoring unreadable %s: %s", self.store_path, exc)
            parsed = {}
        if not isinstance(parsed, dict):
            parsed = {}
        parsed.setdefault("version", 1)
        parsed.setdefault("contacts", {})
        if _refresh_all_contact_columns(parsed):
            self._data = parsed
            self._save()
        self._data = parsed
        return parsed

    def _save(self) -> None:
        data = self._load()
        serialized = json.dumps(data, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
        _write_text_atomically(self.store_path, serialized)


def _normalize_contact_jid(value: Any) -> str:
    raw = str(value or "").strip()
    if not raw or raw == "status@broadcast":
        return ""
    return to_whatsapp_jid(raw)


def _same_evidence(left: dict[str, Any], right: dict[str, Any]) -> bool:
    left_core = {key: value for key, value in left.items() if key not in {"first_seen_at", "last_seen_at"}}
    right_core = {key: value for key, value in right.items() if key not in {"first_seen_at", "last_seen_at"}}
    return left_core == right_core


def _merge_records(target: dict[str, Any], source: dict[str, Any]) -> None:
    target["aliases"] = sorted(set(target.get("aliases") or []) | set(source.get("aliases") or []))
    target["first_seen_at"] = min(
        str(target.get("first_seen_at") or _utc_now()),
        str(source.get("first_seen_at") or _utc_now()),
    )
    target["last_seen_at"] = max(
        str(target.get("last_seen_at") or ""),
        str(source.get("last_seen_at") or ""),
    )
    target.setdefault("evidence", [])
    for row in source.get("evidence") or []:
        if not isinstance(row, dict):
            continue
        duplicate = any(
            _same_evidence(existing, row)
            for existing in target["evidence"]
            if isinstance(existing, dict)
        )
        if not duplicate:
            target["evidence"].append(row)
    target_display = target.setdefault("display", {})
    for key, value in (source.get("display") or {}).items():
        target_display.setdefault(key, value)
    _refresh_contact_columns(target)


def _refresh_all_contact_columns(data: dict[str, Any]) -> bool:
    contacts = data.get("contacts")
    if not isinstance(contacts, dict):
        data["contacts"] = {}
        return True
    changed = False
    for record in contacts.values():
        if isinstance(record, dict) and _refresh_contact_columns(record):
            changed = True
    return changed


def _refresh_contact_columns(record: dict[str, Any]) -> bool:
    before = {key: record.get(key) for key in _CONTACT_COLUMNS}
    aliases = _contact_aliases(record)
    lid_jids = sorted(alias for alias in aliases if alias.endswith("@lid"))
    phone_jids = sorted(alias for alias in aliases if alias.endswith("@s.whatsapp.net"))
    legacy_jids = sorted(
        alias
        for alias in aliases
        if "@" in alias
        and not alias.endswith(("@lid", "@s.whatsapp.net", "@g.us"))
    )
    display_names = _observed_display_names(record)

    _set_or_remove(record, "id", str(record.get("preferred_jid") or "").strip())
    _set_or_remove(record, "lid_jid", lid_jids[0] if lid_jids else "")
    _set_or_remove(record, "phone_jid", phone_jids[0] if phone_jids else "")
    _set_or_remove(record, "legacy_jids", legacy_jids)
    _set_or_remove(record, "bare_phone", _bare_phone(phone_jids, legacy_jids))
    _set_or_remove(record, "display_name", display_names[0] if display_names else "")
    _set_or_remove(record, "observed_names", display_names)

    return before != {key: record.get(key) for key in _CONTACT_COLUMNS}


_CONTACT_COLUMNS = (
    "id",
    "lid_jid",
    "phone_jid",
    "legacy_jids",
    "bare_phone",
    "display_name",
    "observed_names",
)


def _contact_aliases(record: dict[str, Any]) -> set[str]:
    aliases = {str(alias).strip() for alias in record.get("aliases") or [] if str(alias).strip()}
    for row in record.get("evidence") or []:
        if not isinstance(row, dict):
            continue
        for key in ("jid", "raw_id", "lid_jid", "phone_jid"):
            jid = _normalize_contact_jid(row.get(key))
            if jid:
                aliases.add(jid)
    preferred = str(record.get("preferred_jid") or "").strip()
    if preferred:
        aliases.add(preferred)
    return aliases


def _preferred_contact_jid(record: dict[str, Any], aliases: set[str]) -> str:
    current = str(record.get("preferred_jid") or "").strip()
    if current:
        return canonical_whatsapp_jid(current) or current
    lids = sorted(alias for alias in aliases if str(alias).endswith("@lid"))
    if lids:
        return canonical_whatsapp_jid(lids[0]) or lids[0]
    phones = sorted(alias for alias in aliases if str(alias).endswith("@s.whatsapp.net"))
    if phones:
        return canonical_whatsapp_jid(phones[0]) or phones[0]
    return canonical_whatsapp_jid(next(iter(aliases), "")) or ""


def _observed_display_names(record: dict[str, Any]) -> list[str]:
    out: list[str] = []
    for value in (record.get("display") or {}).values():
        _append_display_name(out, value)
    for row in record.get("evidence") or []:
        if not isinstance(row, dict):
            continue
        display = row.get("display")
        if isinstance(display, dict):
            for value in display.values():
                _append_display_name(out, value)
    return out


def _append_display_name(out: list[str], value: Any) -> None:
    name = str(value or "").strip()
    if name and not is_placeholder_whatsapp_name(name) and name not in out:
        out.append(name)


def _bare_phone(phone_jids: list[str], legacy_jids: list[str]) -> str:
    for jid in [*phone_jids, *legacy_jids]:
        local = jid.split("@", 1)[0]
        if local.isdigit():
            return local
    return ""


def _set_or_remove(record: dict[str, Any], key: str, value: Any) -> None:
    if value in ("", None, [], {}):
        record.pop(key, None)
    else:
        record[key] = value


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _write_text_atomically(path: Path, data: str) -> None:
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
