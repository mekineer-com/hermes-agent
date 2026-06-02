#!/usr/bin/env python3
from __future__ import annotations

import argparse
import importlib.util
import json
import os
import shutil
import sqlite3
import sys
import tempfile
from collections import defaultdict, deque
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

WHATSAPP_IDENTITY_PATH = ROOT / "gateway" / "whatsapp_identity.py"
IDENTITY_SPEC = importlib.util.spec_from_file_location("gateway_whatsapp_identity", WHATSAPP_IDENTITY_PATH)
if IDENTITY_SPEC is None or IDENTITY_SPEC.loader is None:
    raise RuntimeError(f"failed to load WhatsApp identity module from {WHATSAPP_IDENTITY_PATH}")
IDENTITY_MODULE = importlib.util.module_from_spec(IDENTITY_SPEC)
IDENTITY_SPEC.loader.exec_module(IDENTITY_MODULE)
canonical_whatsapp_identifier = IDENTITY_MODULE.canonical_whatsapp_identifier
normalize_whatsapp_identifier = IDENTITY_MODULE.normalize_whatsapp_identifier


DM_MARKER = ":whatsapp:dm:"


@dataclass(frozen=True)
class SessionRecord:
    key: str
    target_key: str
    token: str
    canonical: str
    suffix: str
    session_id: str
    entry: dict[str, Any]


def _atomic_write_text(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=str(path.parent), prefix=f".{path.name}.", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(text)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp, path)
    finally:
        if os.path.exists(tmp):
            os.unlink(tmp)


def _load_json(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def _normalize_local(value: Any) -> str:
    return normalize_whatsapp_identifier(str(value or ""))


def _jid_from_local(local: str) -> str:
    return f"{local}@s.whatsapp.net"


def _add_pair(
    *,
    phone_local: str,
    lid_local: str,
    phone_to_lid: dict[str, str],
    lid_to_phone: dict[str, str],
    conflicts: list[str],
) -> None:
    if not phone_local or not lid_local:
        return
    existing_lid = phone_to_lid.get(phone_local)
    if existing_lid and existing_lid != lid_local:
        conflicts.append(f"phone {phone_local} maps to both {existing_lid} and {lid_local}")
        return
    existing_phone = lid_to_phone.get(lid_local)
    if existing_phone and existing_phone != phone_local:
        conflicts.append(f"lid {lid_local} maps to both {existing_phone} and {phone_local}")
        return
    phone_to_lid[phone_local] = lid_local
    lid_to_phone[lid_local] = phone_local


def collect_pairs(sync_events_path: Path, creds_path: Path) -> tuple[dict[str, str], list[str]]:
    phone_to_lid: dict[str, str] = {}
    lid_to_phone: dict[str, str] = {}
    conflicts: list[str] = []

    if sync_events_path.exists():
        for raw_line in sync_events_path.read_text(encoding="utf-8").splitlines():
            line = raw_line.strip()
            if not line:
                continue
            try:
                event = json.loads(line)
            except json.JSONDecodeError:
                continue
            if str(event.get("event") or "").strip().lower() != "contacts.upsert":
                continue
            contacts = event.get("contacts")
            if not isinstance(contacts, list):
                payload = event.get("payload")
                if isinstance(payload, dict):
                    contacts = payload.get("contacts")
            if not isinstance(contacts, list):
                continue
            for contact in contacts:
                if not isinstance(contact, dict):
                    continue
                lid_local = _normalize_local(contact.get("lid"))
                phone_local = _normalize_local(contact.get("jid"))
                _add_pair(
                    phone_local=phone_local,
                    lid_local=lid_local,
                    phone_to_lid=phone_to_lid,
                    lid_to_phone=lid_to_phone,
                    conflicts=conflicts,
                )

    if creds_path.exists():
        parsed = _load_json(creds_path)
        me = parsed.get("me") if isinstance(parsed, dict) else None
        if isinstance(me, dict):
            phone_local = _normalize_local(me.get("id"))
            lid_local = _normalize_local(me.get("lid"))
            if phone_local and lid_local and phone_local != lid_local:
                _add_pair(
                    phone_local=phone_local,
                    lid_local=lid_local,
                    phone_to_lid=phone_to_lid,
                    lid_to_phone=lid_to_phone,
                    conflicts=conflicts,
                )

    return phone_to_lid, conflicts


def _alias_graph(phone_to_lid: dict[str, str]) -> dict[str, set[str]]:
    graph: dict[str, set[str]] = defaultdict(set)
    for phone, lid in phone_to_lid.items():
        graph[phone].add(lid)
        graph[lid].add(phone)
    return graph


def _expand_aliases(identifier: str, graph: dict[str, set[str]]) -> set[str]:
    start = _normalize_local(identifier)
    if not start:
        return set()
    seen: set[str] = set()
    queue = deque([start])
    while queue:
        current = queue.popleft()
        if current in seen:
            continue
        seen.add(current)
        for nxt in graph.get(current, set()):
            if nxt not in seen:
                queue.append(nxt)
    return seen


def canonical_with_collected(identifier: str, graph: dict[str, set[str]]) -> str:
    normalized = _normalize_local(identifier)
    if not normalized:
        return ""
    canonical = _normalize_local(canonical_whatsapp_identifier(normalized))
    aliases = _expand_aliases(normalized, graph)
    if not aliases:
        return canonical or normalized
    collected = min(aliases, key=lambda candidate: (len(candidate), candidate))
    if not canonical:
        return collected
    return min({canonical, *aliases}, key=lambda candidate: (len(candidate), candidate))


def parse_dm_key(session_key: str) -> tuple[str, str, str] | None:
    key = str(session_key)
    idx = key.find(DM_MARKER)
    if idx < 0:
        return None
    prefix = key[: idx + len(DM_MARKER)]
    tail = key[idx + len(DM_MARKER) :]
    if not tail:
        return None
    token = tail.split(":", 1)[0]
    suffix = tail[len(token) :]
    return prefix, token, suffix


def _entry_platform_chat_type(entry: dict[str, Any]) -> tuple[str, str]:
    origin = entry.get("origin") if isinstance(entry.get("origin"), dict) else {}
    platform = str(entry.get("platform") or origin.get("platform") or "").strip().lower()
    chat_type = str(entry.get("chat_type") or origin.get("chat_type") or "").strip().lower()
    return platform, chat_type


def _is_whatsapp_dm_entry(entry: dict[str, Any]) -> bool:
    platform, chat_type = _entry_platform_chat_type(entry)
    return platform == "whatsapp" and chat_type == "dm"


def _load_message_counts(state_db_path: Path) -> dict[str, int]:
    if not state_db_path.exists():
        return {}
    con = sqlite3.connect(state_db_path)
    try:
        rows = con.execute(
            "SELECT session_id, COUNT(*) AS cnt FROM messages GROUP BY session_id"
        ).fetchall()
    finally:
        con.close()
    return {str(session_id): int(cnt) for session_id, cnt in rows}


def _pick_survivor(records: list[SessionRecord], message_counts: dict[str, int]) -> SessionRecord:
    preferred = [record for record in records if record.key == record.target_key]
    if preferred:
        return preferred[0]
    return max(
        records,
        key=lambda record: (message_counts.get(record.session_id, 0), record.session_id, record.key),
    )


def _merge_entry(survivor: dict[str, Any], others: list[dict[str, Any]], canonical_local: str) -> dict[str, Any]:
    merged = json.loads(json.dumps(survivor))
    merged_origin = merged.get("origin") if isinstance(merged.get("origin"), dict) else {}
    if not isinstance(merged_origin, dict):
        merged_origin = {}
    merged["origin"] = merged_origin

    def fill_text(path: tuple[str, ...]) -> None:
        current = merged
        for key in path[:-1]:
            value = current.get(key)
            if not isinstance(value, dict):
                value = {}
                current[key] = value
            current = value
        leaf = path[-1]
        existing = str(current.get(leaf) or "").strip()
        if existing:
            return
        for other in others:
            target = other
            for key in path[:-1]:
                target = target.get(key) if isinstance(target, dict) else None
                if target is None:
                    break
            value = str(target.get(leaf) or "").strip() if isinstance(target, dict) else ""
            if value:
                current[leaf] = value
                return

    fill_text(("display_name",))
    fill_text(("origin", "chat_name"))
    fill_text(("origin", "user_name"))

    canonical_jid = _jid_from_local(canonical_local)
    merged_origin["chat_id"] = canonical_jid
    merged_origin["user_id"] = canonical_jid
    merged["origin"] = merged_origin
    return merged


def _plan_lid_seed_ops(session_dir: Path, phone_to_lid: dict[str, str]) -> dict[str, tuple[Path, str]]:
    ops: dict[str, tuple[Path, str]] = {}
    for phone_local, lid_local in phone_to_lid.items():
        ops[f"lid-mapping-{phone_local}.json"] = (
            session_dir / f"lid-mapping-{phone_local}.json",
            lid_local,
        )
        ops[f"lid-mapping-{lid_local}_reverse.json"] = (
            session_dir / f"lid-mapping-{lid_local}_reverse.json",
            phone_local,
        )
    return ops


def _create_backup(hermes_home: Path) -> Path:
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    backup_dir = hermes_home / f"_backup_{stamp}_dm_identity"
    backup_dir.mkdir(parents=True, exist_ok=True)

    sessions_src = hermes_home / "sessions" / "sessions.json"
    state_db_src = hermes_home / "state.db"
    whatsapp_session_src = hermes_home / "whatsapp" / "session"

    if sessions_src.exists():
        target = backup_dir / "sessions" / "sessions.json"
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(sessions_src, target)
    if state_db_src.exists():
        shutil.copy2(state_db_src, backup_dir / "state.db")
    if whatsapp_session_src.exists():
        shutil.copytree(whatsapp_session_src, backup_dir / "whatsapp" / "session")
    return backup_dir


def _apply_db_remaps(state_db_path: Path, remaps: dict[str, str]) -> dict[str, int]:
    if not state_db_path.exists() or not remaps:
        return {}
    con = sqlite3.connect(state_db_path)
    changed: dict[str, int] = {}
    try:
        con.execute("BEGIN")
        for old_sid, new_sid in remaps.items():
            cur = con.execute(
                "UPDATE messages SET session_id = ? WHERE session_id = ?",
                (new_sid, old_sid),
            )
            changed[old_sid] = int(cur.rowcount or 0)
        con.commit()
    except Exception:
        con.rollback()
        raise
    finally:
        con.close()
    return changed


def run(*, hermes_home: Path, apply: bool) -> int:
    os.environ["HERMES_HOME"] = str(hermes_home)

    sessions_path = hermes_home / "sessions" / "sessions.json"
    state_db_path = hermes_home / "state.db"
    session_dir = hermes_home / "whatsapp" / "session"
    sync_events_path = hermes_home / "whatsapp" / "sync_events.jsonl"
    creds_path = session_dir / "creds.json"

    if not sessions_path.exists():
        raise FileNotFoundError(f"sessions index missing: {sessions_path}")

    phone_to_lid, conflicts = collect_pairs(sync_events_path, creds_path)
    if conflicts:
        raise RuntimeError("conflicting lid/phone mappings:\n- " + "\n- ".join(conflicts))
    graph = _alias_graph(phone_to_lid)

    sessions_index = _load_json(sessions_path)
    if not isinstance(sessions_index, dict):
        raise RuntimeError(f"sessions index must be an object: {sessions_path}")

    message_counts = _load_message_counts(state_db_path)
    lid_seed_ops = _plan_lid_seed_ops(session_dir, phone_to_lid)
    lid_seed_writes: list[tuple[Path, str]] = []
    for path, value in lid_seed_ops.values():
        current = ""
        if path.exists():
            try:
                current = _normalize_local(_load_json(path))
            except json.JSONDecodeError:
                current = ""
        if current != value:
            lid_seed_writes.append((path, value))

    records_by_target: dict[str, list[SessionRecord]] = defaultdict(list)
    for key, entry in sessions_index.items():
        if not isinstance(entry, dict):
            continue
        if not _is_whatsapp_dm_entry(entry):
            continue
        parsed = parse_dm_key(key)
        if parsed is None:
            continue
        prefix, token, suffix = parsed
        canonical_local = canonical_with_collected(token, graph)
        target_key = f"{prefix}{canonical_local}{suffix}"
        session_id = str(entry.get("session_id") or "").strip()
        if not session_id:
            continue
        records_by_target[target_key].append(
            SessionRecord(
                key=key,
                target_key=target_key,
                token=token,
                canonical=canonical_local,
                suffix=suffix,
                session_id=session_id,
                entry=entry,
            )
        )

    updated_sessions = dict(sessions_index)
    session_id_remaps: dict[str, str] = {}
    key_changes: list[tuple[str, str]] = []

    for target_key, records in records_by_target.items():
        survivor = _pick_survivor(records, message_counts)
        merged_entry = _merge_entry(
            survivor.entry,
            [record.entry for record in records if record.key != survivor.key],
            survivor.canonical,
        )
        merged_entry["session_id"] = survivor.session_id

        for record in records:
            if record.key != target_key and record.key in updated_sessions:
                updated_sessions.pop(record.key, None)
            if record.session_id != survivor.session_id:
                session_id_remaps[record.session_id] = survivor.session_id
            if record.key != target_key:
                key_changes.append((record.key, target_key))

        updated_sessions[target_key] = merged_entry

    # Dry-run counts for DB remap impact.
    remap_row_counts: dict[str, int] = {}
    if session_id_remaps and state_db_path.exists():
        con = sqlite3.connect(state_db_path)
        try:
            for old_sid in session_id_remaps:
                row = con.execute(
                    "SELECT COUNT(*) FROM messages WHERE session_id = ?",
                    (old_sid,),
                ).fetchone()
                remap_row_counts[old_sid] = int((row[0] if row else 0) or 0)
        finally:
            con.close()

    lid_tokens = set(phone_to_lid.values())
    remaining_lid_dm_keys = 0
    for key in updated_sessions:
        parsed = parse_dm_key(key)
        if not parsed:
            continue
        _, token, _ = parsed
        if token in lid_tokens:
            remaining_lid_dm_keys += 1

    print(f"pairs_found={len(phone_to_lid)}")
    print(f"seed_files_total={len(lid_seed_ops)}")
    print(f"seed_files_to_write={len(lid_seed_writes)}")
    print(f"session_key_changes={len(key_changes)}")
    print(f"session_id_remaps={len(session_id_remaps)}")
    print(f"remaining_lid_dm_keys_after_plan={remaining_lid_dm_keys}")
    if key_changes:
        print("key_changes_preview:")
        for old_key, new_key in sorted(key_changes)[:20]:
            print(f"  {old_key} -> {new_key}")
    if remap_row_counts:
        print("message_row_remap_counts:")
        for old_sid, row_count in sorted(remap_row_counts.items()):
            print(f"  {old_sid}: {row_count}")

    if not apply:
        print("mode=dry-run")
        return 0

    backup_dir = _create_backup(hermes_home)
    print(f"backup={backup_dir}")

    for path, value in lid_seed_writes:
        _atomic_write_text(path, json.dumps(value, ensure_ascii=False) + "\n")

    if updated_sessions != sessions_index:
        _atomic_write_text(
            sessions_path,
            json.dumps(updated_sessions, ensure_ascii=False, indent=2) + "\n",
        )

    applied_counts = _apply_db_remaps(state_db_path, session_id_remaps)
    print("mode=apply")
    print(f"seed_files_written={len(lid_seed_writes)}")
    print(f"sessions_json_updated={int(updated_sessions != sessions_index)}")
    print(f"message_rows_updated={sum(applied_counts.values())}")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Migrate WhatsApp DM LID/phone identity to canonical keys (dry-run by default)."
    )
    parser.add_argument(
        "--hermes-home",
        default=os.getenv("HERMES_HOME", "~/.hermes"),
        help="Hermes home directory (default: ~/.hermes)",
    )
    parser.add_argument(
        "--apply",
        action="store_true",
        help="Apply migration changes (default is dry-run only).",
    )
    args = parser.parse_args()
    hermes_home = Path(args.hermes_home).expanduser().resolve()
    return run(hermes_home=hermes_home, apply=bool(args.apply))


if __name__ == "__main__":
    raise SystemExit(main())
