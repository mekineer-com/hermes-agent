#!/usr/bin/env python3
"""Backfill OpenAlma WhatsApp identity keys to full JIDs.

Dry-run by default. With ``--apply``, backs up every touched file first and
then updates:

- ``$HERMES_HOME/sessions/sessions.json``
- ``$HERMES_HOME/memu.json``
- ``$HERMES_HOME/channel_directory.json``
- ``$HERMES_HOME/state.db`` session/user source ids, if present

This script intentionally refuses to touch any database named ``Siri.db``.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import sqlite3
import tempfile
from collections import defaultdict
from datetime import datetime
from pathlib import Path
from typing import Any

LID_MAPPING_RE = re.compile(r"^lid-mapping-(.+?)(?:_reverse)?\.json$")
PHONE_DOMAINS = {"s.whatsapp.net", "c.us"}
SESSION_DM_PREFIX = "agent:main:whatsapp:dm:"
SESSION_GROUP_PREFIX = "agent:main:whatsapp:group:"


class IdentityMap:
    def __init__(self, session_dir: Path):
        self.alias_to_lid: dict[str, str] = {}
        self._load(session_dir)

    def canonical_jid(self, value: Any) -> str:
        raw = str(value or "").strip()
        if not raw:
            return ""
        jid = normalize_jid(raw)
        if jid.endswith("@g.us") or jid == "status@broadcast":
            return jid
        aliases = {jid}
        aliases.update(phone_equivalents(jid))
        local = jid.split("@", 1)[0] if "@" in jid else jid
        aliases.add(local)
        for alias in list(aliases):
            lid = self.alias_to_lid.get(alias)
            if lid:
                return lid
        if "@" in jid:
            return jid
        return to_phone_jid(jid)

    def _load(self, session_dir: Path) -> None:
        if not session_dir.exists():
            return
        for path in session_dir.glob("lid-mapping-*.json"):
            match = LID_MAPPING_RE.match(path.name)
            if not match:
                continue
            try:
                mapped = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                continue
            pair = mapping_pair(match.group(1), mapped, reverse=path.name.endswith("_reverse.json"))
            if pair is None:
                continue
            phone_jid, lid_jid = pair
            phone_local = phone_jid.split("@", 1)[0]
            lid_local = lid_jid.split("@", 1)[0]
            for alias in {
                phone_local,
                phone_jid,
                f"{phone_local}@c.us",
                lid_local,
                lid_jid,
            }:
                self.alias_to_lid[alias] = lid_jid


def normalize_jid(value: str) -> str:
    raw = str(value or "").strip()
    if not raw:
        return ""
    if "@" in raw:
        local, _, domain = raw.partition("@")
        local = local.replace("+", "", 1).split(":", 1)[0].strip()
        domain = domain.strip()
        if local and domain:
            return f"{local}@{domain}"
        return raw
    return raw.replace("+", "", 1).split(":", 1)[0]


def to_phone_jid(value: str) -> str:
    raw = normalize_jid(value)
    if not raw:
        return ""
    if "@" in raw:
        return raw
    digits = re.sub(r"\D+", "", raw)
    return f"{digits}@s.whatsapp.net" if digits else raw


def ensure_lid_jid(value: str) -> str:
    local = normalize_jid(value).split("@", 1)[0]
    return f"{local}@lid" if local else ""


def phone_equivalents(jid: str) -> set[str]:
    if "@" not in jid:
        return set()
    local, domain = jid.split("@", 1)
    if domain not in PHONE_DOMAINS:
        return set()
    return {f"{local}@s.whatsapp.net", f"{local}@c.us"}


def mapping_pair(raw_key: str, mapped: Any, *, reverse: bool) -> tuple[str, str] | None:
    key = normalize_jid(raw_key)
    mapped_str = normalize_jid(str(mapped or ""))
    if not key or not mapped_str:
        return None
    if reverse:
        return to_phone_jid(mapped_str), ensure_lid_jid(key)
    if mapped_str.endswith("@s.whatsapp.net") or mapped_str.endswith("@c.us"):
        return to_phone_jid(mapped_str), ensure_lid_jid(key)
    return to_phone_jid(key), ensure_lid_jid(mapped_str)


def load_json(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def atomic_write_json(path: Path, data: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_path = tempfile.mkstemp(dir=str(path.parent), prefix=f".{path.name}.", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(data, handle, ensure_ascii=False, indent=2)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp_path, path)
    except BaseException:
        try:
            os.unlink(tmp_path)
        except OSError:
            pass
        raise


def backup_file(path: Path, backup_dir: Path, home: Path) -> None:
    if not path.exists():
        return
    rel = path.expanduser().resolve().relative_to(home.expanduser().resolve())
    dest = backup_dir / rel
    dest.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(path, dest)


def rewrite_session_key(key: str, idmap: IdentityMap) -> str:
    raw = str(key or "")
    if raw.startswith(SESSION_DM_PREFIX):
        tail = raw[len(SESSION_DM_PREFIX) :]
        token, suffix = split_key_tail(tail)
        canonical = idmap.canonical_jid(token)
        return f"{SESSION_DM_PREFIX}{canonical}{suffix}" if canonical else raw
    if raw.startswith(SESSION_GROUP_PREFIX):
        tail = raw[len(SESSION_GROUP_PREFIX) :]
        parts = tail.split(":")
        if len(parts) < 2:
            return raw
        chat_id = parts[0]
        participant = idmap.canonical_jid(parts[1])
        if not participant:
            return raw
        rest = ":".join(parts[2:])
        suffix = f":{rest}" if rest else ""
        return f"{SESSION_GROUP_PREFIX}{chat_id}:{participant}{suffix}"
    return raw


def rewrite_session_key_for_entry(key: str, entry: dict[str, Any], idmap: IdentityMap) -> str:
    if ":legacy:" in str(key):
        return str(key)
    origin = entry.get("origin") if isinstance(entry.get("origin"), dict) else {}
    if str(origin.get("platform") or "").lower() != "whatsapp":
        return rewrite_session_key(key, idmap)

    chat_type = str(origin.get("chat_type") or "").lower()
    if chat_type == "dm":
        chat_id = idmap.canonical_jid(origin.get("chat_id"))
        if chat_id:
            return f"{SESSION_DM_PREFIX}{chat_id}"
    if chat_type == "group":
        chat_id = str(origin.get("chat_id") or "").strip()
        participant = idmap.canonical_jid(origin.get("user_id"))
        if chat_id and participant:
            return f"{SESSION_GROUP_PREFIX}{chat_id}:{participant}"
    return rewrite_session_key(key, idmap)


def split_key_tail(tail: str) -> tuple[str, str]:
    if ":" not in tail:
        return tail, ""
    token, suffix = tail.split(":", 1)
    return token, f":{suffix}"


def rewrite_origin(origin: dict[str, Any], idmap: IdentityMap) -> bool:
    changed = False
    if str(origin.get("platform") or "").lower() != "whatsapp":
        return False
    chat_type = str(origin.get("chat_type") or "").lower()
    if chat_type == "dm":
        for field in ("chat_id", "user_id"):
            canonical = idmap.canonical_jid(origin.get(field))
            if canonical and origin.get(field) != canonical:
                origin[field] = canonical
                changed = True
    elif chat_type == "group":
        canonical = idmap.canonical_jid(origin.get("user_id"))
        if canonical and origin.get("user_id") != canonical:
            origin["user_id"] = canonical
            changed = True
    return changed


def migrate_sessions(path: Path, idmap: IdentityMap) -> tuple[dict[str, Any] | None, dict[str, int], list[str]]:
    if not path.exists():
        return None, {}, []
    data = load_json(path)
    if not isinstance(data, dict):
        raise RuntimeError(f"sessions index must be an object: {path}")

    buckets: dict[str, list[tuple[str, dict[str, Any]]]] = defaultdict(list)
    changed = 0
    for old_key, entry in data.items():
        if not isinstance(entry, dict):
            buckets[str(old_key)].append((str(old_key), entry))
            continue
        origin = entry.get("origin") if isinstance(entry.get("origin"), dict) else None
        if origin is not None and rewrite_origin(origin, idmap):
            changed += 1
        new_key = rewrite_session_key_for_entry(str(old_key), entry, idmap)
        if new_key != old_key:
            changed += 1
        buckets[new_key].append((str(old_key), entry))

    migrated: dict[str, Any] = {}
    legacy = 0
    collisions = []
    for new_key, rows in buckets.items():
        rows_sorted = sorted(rows, key=lambda row: str(row[1].get("updated_at") or ""), reverse=True)
        for index, (old_key, entry) in enumerate(rows_sorted):
            target_key = new_key
            if index:
                legacy += 1
                sid = str(entry.get("session_id") or index).strip() or str(index)
                target_key = f"{new_key}:legacy:{sid}"
                collisions.append(f"{old_key} -> {target_key}")
            if isinstance(entry, dict):
                entry["session_key"] = target_key
            migrated[target_key] = entry
    if legacy:
        changed += legacy
    return migrated, {"session_entries_changed": changed, "legacy_entries": legacy}, collisions


def merge_policy_entries(entries: list[dict[str, Any]]) -> dict[str, Any]:
    policy = "full"
    memorize: bool | None = None
    for entry in entries:
        raw_policy = str(entry.get("policy") or "").strip().lower()
        if raw_policy == "excluded":
            policy = "excluded"
        elif raw_policy == "listen_only" and policy != "excluded":
            policy = "listen_only"
        elif raw_policy == "full" and policy not in {"excluded", "listen_only"}:
            policy = "full"
        if isinstance(entry.get("memorize"), bool):
            memorize = bool(entry["memorize"]) if memorize is not True else True
    out = dict(entries[-1]) if entries else {}
    out["policy"] = policy
    out["memorize"] = False if policy == "excluded" else (memorize if memorize is not None else policy != "excluded")
    return out


def migrate_memu(path: Path, idmap: IdentityMap) -> tuple[dict[str, Any] | None, dict[str, int], list[str]]:
    if not path.exists():
        return None, {}, []
    data = load_json(path)
    channels = data.get("whatsapp", {}).get("channels") if isinstance(data, dict) else None
    if not isinstance(channels, dict):
        return data, {"memu_keys_changed": 0, "memu_collisions": 0}, []

    buckets: dict[str, list[dict[str, Any]]] = defaultdict(list)
    changed = 0
    for key, entry in channels.items():
        canonical = idmap.canonical_jid(key)
        new_key = canonical or str(key)
        if new_key != key:
            changed += 1
        buckets[new_key].append(entry if isinstance(entry, dict) else {})

    collisions = [key for key, entries in buckets.items() if len(entries) > 1]
    data["whatsapp"]["channels"] = {
        key: merge_policy_entries(entries)
        for key, entries in sorted(buckets.items())
    }
    return data, {"memu_keys_changed": changed, "memu_collisions": len(collisions)}, collisions


def placeholder_channel_name(name: Any) -> bool:
    raw = str(name or "").strip()
    if not raw:
        return True
    normalized = normalize_jid(raw)
    local = normalized.split("@", 1)[0]
    if not local or not re.fullmatch(r"\d+", local):
        return False
    return normalized in {
        local,
        f"{local}@lid",
        f"{local}@s.whatsapp.net",
        f"{local}@c.us",
    }


def better_channel(existing: dict[str, Any], candidate: dict[str, Any]) -> dict[str, Any]:
    merged = dict(existing)
    new_name = str(candidate.get("name") or "")
    if placeholder_channel_name(existing.get("name")) and new_name and not placeholder_channel_name(new_name):
        merged["name"] = new_name
    for key, value in candidate.items():
        if key == "name":
            continue
        if merged.get(key) in (None, "") and value not in (None, ""):
            merged[key] = value
    return merged


def migrate_channel_directory(path: Path, idmap: IdentityMap) -> tuple[dict[str, Any] | None, dict[str, int], list[str]]:
    if not path.exists():
        return None, {}, []
    data = load_json(path)
    entries = data.get("platforms", {}).get("whatsapp") if isinstance(data, dict) else None
    if not isinstance(entries, list):
        return data, {"channel_ids_changed": 0, "channel_collisions": 0}, []

    changed = 0
    out: dict[str, dict[str, Any]] = {}
    collisions = []
    for entry in entries:
        if not isinstance(entry, dict):
            continue
        migrated = dict(entry)
        if migrated.get("type") == "dm":
            canonical = idmap.canonical_jid(migrated.get("id"))
            if canonical and canonical != migrated.get("id"):
                migrated["id"] = canonical
                changed += 1
        key = str(migrated.get("id") or "")
        if key in out:
            collisions.append(key)
            out[key] = better_channel(out[key], migrated)
        elif key:
            out[key] = migrated
    data["platforms"]["whatsapp"] = list(out.values())
    return data, {"channel_ids_changed": changed, "channel_collisions": len(collisions)}, collisions


def migrate_state_db(
    path: Path,
    idmap: IdentityMap,
    *,
    apply: bool,
    update_message_source_chat_ids: bool,
) -> dict[str, int]:
    if not path.exists():
        return {}
    if path.name == "Siri.db":
        raise RuntimeError(f"refusing to touch Siri.db: {path}")
    con = sqlite3.connect(path)
    try:
        con.row_factory = sqlite3.Row
        session_updates: list[tuple[str, str]] = []
        for row in con.execute("SELECT DISTINCT user_id FROM sessions WHERE source = 'whatsapp' AND user_id IS NOT NULL"):
            old = str(row["user_id"] or "")
            new = idmap.canonical_jid(old)
            if new and new != old:
                session_updates.append((new, old))
        message_updates: list[tuple[str, str]] = []
        if update_message_source_chat_ids:
            for row in con.execute("SELECT DISTINCT source_chat_id FROM messages WHERE source_chat_id IS NOT NULL"):
                old = str(row["source_chat_id"] or "")
                if not looks_whatsapp_id(old):
                    continue
                new = idmap.canonical_jid(old)
                if new and new != old:
                    message_updates.append((new, old))
        if apply:
            with con:
                con.executemany("UPDATE sessions SET user_id = ? WHERE user_id = ?", session_updates)
                if update_message_source_chat_ids:
                    con.executemany("UPDATE messages SET source_chat_id = ? WHERE source_chat_id = ?", message_updates)
        return {
            "state_session_user_ids_changed": len(session_updates),
            "state_message_source_ids_changed": len(message_updates),
            "state_message_source_ids_skipped": int(not update_message_source_chat_ids),
        }
    finally:
        con.close()


def looks_whatsapp_id(value: str) -> bool:
    raw = str(value or "")
    return raw.endswith(("@s.whatsapp.net", "@c.us", "@lid", "@g.us")) or bool(re.fullmatch(r"\d{8,16}", raw))


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--home", type=Path, default=Path(os.getenv("HERMES_HOME") or "~/.hermes").expanduser())
    parser.add_argument("--apply", action="store_true")
    parser.add_argument(
        "--update-message-source-chat-ids",
        action="store_true",
        help="Also rewrite messages.source_chat_id. Off by default because duplicate alias rows can violate the unique source-message index.",
    )
    args = parser.parse_args()

    home = args.home.expanduser().resolve()
    idmap = IdentityMap(home / "whatsapp" / "session")
    timestamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    backup_dir = home / "backups" / f"whatsapp-jid-backfill-{timestamp}"

    targets = {
        "sessions": home / "sessions" / "sessions.json",
        "memu": home / "memu.json",
        "channel_directory": home / "channel_directory.json",
        "state_db": home / "state.db",
    }

    sessions_data, sessions_stats, session_collisions = migrate_sessions(targets["sessions"], idmap)
    memu_data, memu_stats, memu_collisions = migrate_memu(targets["memu"], idmap)
    channel_data, channel_stats, channel_collisions = migrate_channel_directory(targets["channel_directory"], idmap)
    state_stats = migrate_state_db(
        targets["state_db"],
        idmap,
        apply=False,
        update_message_source_chat_ids=args.update_message_source_chat_ids,
    )

    stats = {}
    for part in (sessions_stats, memu_stats, channel_stats, state_stats):
        stats.update(part)
    print(json.dumps({"home": str(home), "apply": args.apply, "stats": stats}, indent=2, sort_keys=True))
    if session_collisions:
        print("session legacy entries:")
        for item in session_collisions:
            print(f"  {item}")
    if memu_collisions:
        print("memu key merges:")
        for item in memu_collisions:
            print(f"  {item}")
    if channel_collisions:
        print("channel directory merges:")
        for item in channel_collisions:
            print(f"  {item}")

    if not args.apply:
        return 0

    backup_dir.mkdir(parents=True, exist_ok=True)
    for path in targets.values():
        backup_file(path, backup_dir, home)
    if sessions_data is not None:
        atomic_write_json(targets["sessions"], sessions_data)
    if memu_data is not None:
        atomic_write_json(targets["memu"], memu_data)
    if channel_data is not None:
        atomic_write_json(targets["channel_directory"], channel_data)
    migrate_state_db(
        targets["state_db"],
        idmap,
        apply=True,
        update_message_source_chat_ids=args.update_message_source_chat_ids,
    )
    print(f"backups: {backup_dir}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
