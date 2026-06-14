#!/usr/bin/env python3
"""SQLite projection writer for the WhatsApp Web source daemon.

Reads newline-delimited JSON commands on stdin and writes one JSON response per
command on stdout. This keeps the Node daemon dependency-free while still using
Python's built-in sqlite3 module.
"""

from __future__ import annotations

import argparse
import json
import os
import sqlite3
import sys
import time
from pathlib import Path
from typing import Any


PENDING_REACTION_MAX_AGE_SECONDS = 7 * 24 * 60 * 60


def expand_path(value: str) -> Path:
    return Path(os.path.expandvars(os.path.expanduser(value))).resolve()


def now() -> int:
    return int(time.time())


def source_rank(source: str | None) -> int:
    value = source or ""
    if "message_edit" in value:
        return 50
    if value == "event:message":
        return 40
    if value == "event:message_create":
        return 30
    if value.startswith("backfill:"):
        return 20
    if "ciphertext" in value:
        return 10
    return 0


def connect(db_path: Path) -> sqlite3.Connection:
    db_path.parent.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(str(db_path))
    con.row_factory = sqlite3.Row
    con.execute("pragma journal_mode=wal")
    con.execute("pragma synchronous=normal")
    con.execute("pragma foreign_keys=on")
    init_schema(con)
    return con


def init_schema(con: sqlite3.Connection) -> None:
    con.executescript(
        """
        create table if not exists whatsapp_messages (
          msg_key text primary key,
          chat_id text not null,
          chat_local_id text not null,
          from_me integer not null,
          timestamp integer not null,
          type text not null,
          body text,
          author_id text,
          author_local_id text,
          from_id text,
          from_local_id text,
          to_id text,
          to_local_id text,
          has_media integer not null default 0,
          media_placeholder text,
          ack integer,
          revoked integer not null default 0,
          revoke_source text,
          source text not null,
          first_seen_at integer not null,
          updated_at integer not null,
          raw_json text not null,
          reactions text
        );

        create index if not exists whatsapp_messages_chat_time
          on whatsapp_messages(chat_id, timestamp, msg_key);

        create index if not exists whatsapp_messages_chat_local_time
          on whatsapp_messages(chat_local_id, timestamp, msg_key);

        create table if not exists whatsapp_chats (
          chat_id text primary key,
          chat_local_id text not null,
          name text,
          is_group integer not null default 0,
          last_timestamp integer,
          raw_json text,
          updated_at integer not null
        );

        create index if not exists whatsapp_chats_local
          on whatsapp_chats(chat_local_id);

        create table if not exists whatsapp_contacts (
          contact_id text primary key,
          contact_local_id text not null,
          name text,
          short_name text,
          push_name text,
          verified_name text,
          is_me integer not null default 0,
          is_user integer not null default 0,
          is_group integer not null default 0,
          raw_json text,
          updated_at integer not null
        );

        create index if not exists whatsapp_contacts_local
          on whatsapp_contacts(contact_local_id);

        create table if not exists whatsapp_metadata (
          key text primary key,
          value text not null,
          updated_at integer not null
        );

        create table if not exists whatsapp_pending_reactions (
          msg_key text not null,
          sender_local_id text not null,
          reaction text not null,
          updated_at integer not null,
          primary key (msg_key, sender_local_id)
        );
        """
    )
    existing_cols = {
        str(r[1])
        for r in con.execute("PRAGMA table_info(whatsapp_messages)").fetchall()
    }
    if "reactions" not in existing_cols:
        con.execute("ALTER TABLE whatsapp_messages ADD COLUMN reactions text")
    con.commit()


def _merge_pending_reactions(con: sqlite3.Connection, msg_key: str, ts: int) -> None:
    pending = con.execute(
        "select sender_local_id, reaction from whatsapp_pending_reactions where msg_key = ?",
        (msg_key,),
    ).fetchall()
    if not pending:
        return
    row = con.execute(
        "select reactions from whatsapp_messages where msg_key = ?",
        (msg_key,),
    ).fetchone()
    if row is None:
        return
    reactions: dict[str, str] = {}
    raw = row["reactions"]
    if raw:
        reactions = json.loads(raw)
    for item in pending:
        reaction = str(item["reaction"] or "").strip()
        if reaction:
            reactions[str(item["sender_local_id"])] = reaction
    con.execute(
        "update whatsapp_messages set reactions = ?, updated_at = ? where msg_key = ?",
        (json.dumps(reactions, ensure_ascii=False) if reactions else None, ts, msg_key),
    )
    con.execute("delete from whatsapp_pending_reactions where msg_key = ?", (msg_key,))


def upsert_message(con: sqlite3.Connection, row: dict[str, Any]) -> dict[str, Any]:
    ts = now()
    existing = con.execute(
        "select msg_key, source, body, raw_json from whatsapp_messages where msg_key = ?",
        (row["msg_key"],),
    ).fetchone()
    existing_source = existing["source"] if existing else None
    incoming_wins = source_rank(row.get("source")) >= source_rank(existing_source)
    con.execute(
        """
        insert into whatsapp_messages (
          msg_key, chat_id, chat_local_id, from_me, timestamp, type, body,
          author_id, author_local_id, from_id, from_local_id, to_id, to_local_id,
          has_media, media_placeholder, ack, revoked, revoke_source, source,
          first_seen_at, updated_at, raw_json
        ) values (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        on conflict(msg_key) do update set
          chat_id=case when ? then excluded.chat_id else whatsapp_messages.chat_id end,
          chat_local_id=case when ? then excluded.chat_local_id else whatsapp_messages.chat_local_id end,
          from_me=case when ? then excluded.from_me else whatsapp_messages.from_me end,
          timestamp=case when ? then excluded.timestamp else whatsapp_messages.timestamp end,
          type=case when ? then excluded.type else whatsapp_messages.type end,
          body=case
            when ? and excluded.body is not null and excluded.body != '' then excluded.body
            when (whatsapp_messages.body is null or whatsapp_messages.body = '')
                 and excluded.body is not null and excluded.body != '' then excluded.body
            else whatsapp_messages.body
          end,
          author_id=case when ? then coalesce(excluded.author_id, whatsapp_messages.author_id) else coalesce(whatsapp_messages.author_id, excluded.author_id) end,
          author_local_id=case when ? then coalesce(excluded.author_local_id, whatsapp_messages.author_local_id) else coalesce(whatsapp_messages.author_local_id, excluded.author_local_id) end,
          from_id=case when ? then coalesce(excluded.from_id, whatsapp_messages.from_id) else coalesce(whatsapp_messages.from_id, excluded.from_id) end,
          from_local_id=case when ? then coalesce(excluded.from_local_id, whatsapp_messages.from_local_id) else coalesce(whatsapp_messages.from_local_id, excluded.from_local_id) end,
          to_id=case when ? then coalesce(excluded.to_id, whatsapp_messages.to_id) else coalesce(whatsapp_messages.to_id, excluded.to_id) end,
          to_local_id=case when ? then coalesce(excluded.to_local_id, whatsapp_messages.to_local_id) else coalesce(whatsapp_messages.to_local_id, excluded.to_local_id) end,
          has_media=case when ? then excluded.has_media else whatsapp_messages.has_media end,
          media_placeholder=case when ? then coalesce(excluded.media_placeholder, whatsapp_messages.media_placeholder) else coalesce(whatsapp_messages.media_placeholder, excluded.media_placeholder) end,
          ack=coalesce(excluded.ack, whatsapp_messages.ack),
          revoked=case when whatsapp_messages.revoked = 1 then 1 else excluded.revoked end,
          revoke_source=coalesce(whatsapp_messages.revoke_source, excluded.revoke_source),
          source=case when ? then excluded.source else whatsapp_messages.source end,
          updated_at=excluded.updated_at,
          raw_json=case when ? then excluded.raw_json else whatsapp_messages.raw_json end
        """,
        (
            row["msg_key"],
            row["chat_id"],
            row["chat_local_id"],
            int(bool(row["from_me"])),
            int(row["timestamp"]),
            row["type"],
            row.get("body"),
            row.get("author_id"),
            row.get("author_local_id"),
            row.get("from_id"),
            row.get("from_local_id"),
            row.get("to_id"),
            row.get("to_local_id"),
            int(bool(row.get("has_media"))),
            row.get("media_placeholder"),
            row.get("ack"),
            int(bool(row.get("revoked"))),
            row.get("revoke_source"),
            row["source"],
            ts,
            ts,
            json.dumps(row.get("raw", row), ensure_ascii=False, sort_keys=True),
            int(incoming_wins),
            int(incoming_wins),
            int(incoming_wins),
            int(incoming_wins),
            int(incoming_wins),
            int(incoming_wins),
            int(incoming_wins),
            int(incoming_wins),
            int(incoming_wins),
            int(incoming_wins),
            int(incoming_wins),
            int(incoming_wins),
            int(incoming_wins),
            int(incoming_wins),
            int(incoming_wins),
            int(incoming_wins),
        ),
    )
    _merge_pending_reactions(con, row["msg_key"], ts)
    con.commit()
    return {"status": "ok", "action": "insert" if existing is None else "update", "msg_key": row["msg_key"]}


def mark_revoked(con: sqlite3.Connection, row: dict[str, Any]) -> dict[str, Any]:
    msg_key = row["msg_key"]
    ts = now()
    cur = con.execute(
        """
        update whatsapp_messages
        set revoked = 1,
            type = case when type = 'ciphertext' then 'revoked' else type end,
            revoke_source = ?,
            updated_at = ?,
            raw_json = ?
        where msg_key = ?
        """,
        (
            row.get("source", "event:revoke"),
            ts,
            json.dumps(row.get("raw", row), ensure_ascii=False, sort_keys=True),
            msg_key,
        ),
    )
    con.commit()
    return {"status": "ok", "action": "revoke", "msg_key": msg_key, "matched": cur.rowcount}


def mark_missing_in_chat_window(con: sqlite3.Connection, row: dict[str, Any]) -> dict[str, Any]:
    chat_id = str(row.get("chat_id") or "").strip()
    chat_local_id = str(row.get("chat_local_id") or "").strip()
    min_timestamp = int(row["min_timestamp"])
    updated_before = int(row["updated_before"])
    present_msg_keys = [
        str(value or "").strip()
        for value in row.get("present_msg_keys", [])
        if str(value or "").strip()
    ]
    source = str(row.get("source") or "reconcile:missing")
    ts = now()

    if not chat_id and not chat_local_id:
        return {"status": "ok", "action": "reconcile_missing", "matched": 0}

    con.execute("create temp table if not exists present_backfill_keys (msg_key text primary key)")
    con.execute("delete from present_backfill_keys")
    con.executemany(
        "insert or ignore into present_backfill_keys (msg_key) values (?)",
        [(msg_key,) for msg_key in present_msg_keys],
    )
    cur = con.execute(
        """
        update whatsapp_messages
        set revoked = 1,
            revoke_source = ?,
            updated_at = ?,
            raw_json = ?
        where revoked = 0
          and timestamp >= ?
          and updated_at < ?
          and (chat_id = ? or chat_local_id = ?)
          and not exists (
            select 1
            from present_backfill_keys p
            where p.msg_key = whatsapp_messages.msg_key
          )
        """,
        (
            source,
            ts,
            json.dumps(
                {
                    "source": source,
                    "chat_id": chat_id,
                    "chat_local_id": chat_local_id,
                    "min_timestamp": min_timestamp,
                    "present_msg_keys": len(present_msg_keys),
                },
                ensure_ascii=False,
                sort_keys=True,
            ),
            min_timestamp,
            updated_before,
            chat_id,
            chat_local_id,
        ),
    )
    con.execute("delete from present_backfill_keys")
    con.commit()
    return {"status": "ok", "action": "reconcile_missing", "matched": cur.rowcount}


def update_ack(con: sqlite3.Connection, row: dict[str, Any]) -> dict[str, Any]:
    msg_key = row["msg_key"]
    ts = now()
    cur = con.execute(
        "update whatsapp_messages set ack = ?, updated_at = ? where msg_key = ?",
        (row.get("ack"), ts, msg_key),
    )
    con.commit()
    return {"status": "ok", "action": "ack", "msg_key": msg_key, "matched": cur.rowcount}


def get_metadata(con: sqlite3.Connection, key: str) -> dict[str, Any]:
    row = con.execute(
        "select value, updated_at from whatsapp_metadata where key = ?",
        (key,),
    ).fetchone()
    if row is None:
        return {"status": "ok", "value": None, "updated_at": None}
    return {"status": "ok", "value": row["value"], "updated_at": row["updated_at"]}


def set_metadata(con: sqlite3.Connection, key: str, value: str) -> dict[str, Any]:
    ts = now()
    con.execute(
        """
        insert into whatsapp_metadata (key, value, updated_at)
        values (?, ?, ?)
        on conflict(key) do update set
          value=excluded.value,
          updated_at=excluded.updated_at
        """,
        (key, value, ts),
    )
    con.commit()
    return {"status": "ok", "action": "set_metadata", "key": key}


def upsert_chat(con: sqlite3.Connection, row: dict[str, Any]) -> dict[str, Any]:
    ts = now()
    con.execute(
        """
        insert into whatsapp_chats (chat_id, chat_local_id, name, is_group, last_timestamp, raw_json, updated_at)
        values (?, ?, ?, ?, ?, ?, ?)
        on conflict(chat_id) do update set
          chat_local_id=excluded.chat_local_id,
          name=coalesce(excluded.name, whatsapp_chats.name),
          is_group=excluded.is_group,
          last_timestamp=coalesce(excluded.last_timestamp, whatsapp_chats.last_timestamp),
          raw_json=excluded.raw_json,
          updated_at=excluded.updated_at
        """,
        (
            row["chat_id"],
            row["chat_local_id"],
            row.get("name"),
            int(bool(row.get("is_group"))),
            row.get("last_timestamp"),
            json.dumps(row.get("raw", row), ensure_ascii=False, sort_keys=True),
            ts,
        ),
    )
    con.commit()
    return {"status": "ok", "action": "upsert_chat", "chat_id": row["chat_id"]}


def upsert_contact(con: sqlite3.Connection, row: dict[str, Any]) -> dict[str, Any]:
    ts = now()
    con.execute(
        """
        insert into whatsapp_contacts (
          contact_id, contact_local_id, name, short_name, push_name, verified_name,
          is_me, is_user, is_group, raw_json, updated_at
        )
        values (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        on conflict(contact_id) do update set
          contact_local_id=excluded.contact_local_id,
          name=coalesce(excluded.name, whatsapp_contacts.name),
          short_name=coalesce(excluded.short_name, whatsapp_contacts.short_name),
          push_name=coalesce(excluded.push_name, whatsapp_contacts.push_name),
          verified_name=coalesce(excluded.verified_name, whatsapp_contacts.verified_name),
          is_me=excluded.is_me,
          is_user=excluded.is_user,
          is_group=excluded.is_group,
          raw_json=excluded.raw_json,
          updated_at=excluded.updated_at
        """,
        (
            row["contact_id"],
            row["contact_local_id"],
            row.get("name"),
            row.get("short_name"),
            row.get("push_name"),
            row.get("verified_name"),
            int(bool(row.get("is_me"))),
            int(bool(row.get("is_user"))),
            int(bool(row.get("is_group"))),
            json.dumps(row.get("raw", row), ensure_ascii=False, sort_keys=True),
            ts,
        ),
    )
    con.commit()
    return {"status": "ok", "action": "upsert_contact", "contact_id": row["contact_id"]}


def in_scope_contact_ids(con: sqlite3.Connection, active_since: int) -> dict[str, Any]:
    rows = con.execute(
        """
        select from_id as contact_id, from_local_id as contact_local_id
        from whatsapp_messages
        where timestamp >= ? and from_id is not null and from_id != ''
        union
        select chat_id as contact_id, chat_local_id as contact_local_id
        from whatsapp_messages
        where timestamp >= ? and chat_id is not null and chat_id != ''
        union
        select to_id as contact_id, to_local_id as contact_local_id
        from whatsapp_messages
        where timestamp >= ? and to_id is not null and to_id != ''
        union
        select author_id as contact_id, author_local_id as contact_local_id
        from whatsapp_messages
        where timestamp >= ? and author_id is not null and author_id != ''
        """,
        (active_since, active_since, active_since, active_since),
    ).fetchall()
    ids = sorted({row["contact_id"] for row in rows if row["contact_id"]})
    local_ids = sorted({row["contact_local_id"] for row in rows if row["contact_local_id"]})
    return {"status": "ok", "contact_ids": ids, "contact_local_ids": local_ids}


def backup_database(con: sqlite3.Connection, backup_path: Path) -> None:
    backup_path.parent.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(str(backup_path)) as dest:
        con.backup(dest)


def prune_scope(
    con: sqlite3.Connection,
    active_since: int,
    backup_path: str | None = None,
) -> dict[str, Any]:
    resolved_backup_path: str | None = None
    if backup_path:
        backup = expand_path(backup_path)
        backup_database(con, backup)
        resolved_backup_path = str(backup)
    chat_cur = con.execute(
        """
        delete from whatsapp_chats
        where not exists (
          select 1
          from whatsapp_messages m
          where m.chat_id = whatsapp_chats.chat_id
            and m.timestamp >= ?
        )
        """,
        (active_since,),
    )
    contact_cur = con.execute(
        """
        delete from whatsapp_contacts
        where is_me = 0
          and contact_id not in (
            select contact_id from (
              select from_id as contact_id
              from whatsapp_messages
              where timestamp >= ? and from_id is not null and from_id != ''
              union
              select chat_id as contact_id
              from whatsapp_messages
              where timestamp >= ? and chat_id is not null and chat_id != ''
              union
              select to_id as contact_id
              from whatsapp_messages
              where timestamp >= ? and to_id is not null and to_id != ''
              union
              select author_id as contact_id
              from whatsapp_messages
              where timestamp >= ? and author_id is not null and author_id != ''
            )
          )
          and contact_local_id not in (
            select contact_local_id from (
              select from_local_id as contact_local_id
              from whatsapp_messages
              where timestamp >= ? and from_local_id is not null and from_local_id != ''
              union
              select chat_local_id as contact_local_id
              from whatsapp_messages
              where timestamp >= ? and chat_local_id is not null and chat_local_id != ''
              union
              select to_local_id as contact_local_id
              from whatsapp_messages
              where timestamp >= ? and to_local_id is not null and to_local_id != ''
              union
              select author_local_id as contact_local_id
              from whatsapp_messages
              where timestamp >= ? and author_local_id is not null and author_local_id != ''
            )
          )
        """,
        (
            active_since,
            active_since,
            active_since,
            active_since,
            active_since,
            active_since,
            active_since,
            active_since,
        ),
    )
    pending_cur = con.execute(
        """
        delete from whatsapp_pending_reactions
        where not exists (
          select 1
          from whatsapp_messages m
          where m.msg_key = whatsapp_pending_reactions.msg_key
        )
          and updated_at < ?
        """,
        (now() - PENDING_REACTION_MAX_AGE_SECONDS,),
    )
    con.commit()
    return {
        "status": "ok",
        "action": "prune_scope",
        "active_since": active_since,
        "deleted_chats": chat_cur.rowcount,
        "deleted_contacts": contact_cur.rowcount,
        "deleted_pending_reactions": pending_cur.rowcount,
        "backup_path": resolved_backup_path,
    }


def apply_reaction(con: sqlite3.Connection, row: dict[str, Any]) -> dict[str, Any]:
    msg_key = str(row["msg_key"]).strip()
    sender_local_id = str(row["sender_local_id"]).strip()
    emoji = str(row.get("reaction") or "").strip()
    ts = now()
    existing = con.execute(
        "select reactions from whatsapp_messages where msg_key = ?",
        (msg_key,),
    ).fetchone()
    if existing is None:
        if emoji:
            con.execute(
                """
                insert into whatsapp_pending_reactions (msg_key, sender_local_id, reaction, updated_at)
                values (?, ?, ?, ?)
                on conflict(msg_key, sender_local_id) do update set
                  reaction = excluded.reaction,
                  updated_at = excluded.updated_at
                """,
                (msg_key, sender_local_id, emoji, ts),
            )
        else:
            con.execute(
                "delete from whatsapp_pending_reactions where msg_key = ? and sender_local_id = ?",
                (msg_key, sender_local_id),
            )
        con.commit()
        return {"status": "ok", "action": "pending_reaction", "msg_key": msg_key}
    reactions: dict[str, str] = {}
    raw = existing["reactions"]
    if raw:
        reactions = json.loads(raw)
    if emoji:
        reactions[sender_local_id] = emoji
    else:
        reactions.pop(sender_local_id, None)
    con.execute(
        "update whatsapp_messages set reactions = ?, updated_at = ? where msg_key = ?",
        (json.dumps(reactions, ensure_ascii=False) if reactions else None, ts, msg_key),
    )
    con.commit()
    return {"status": "ok", "action": "reaction", "msg_key": msg_key}


def handle(con: sqlite3.Connection, command: dict[str, Any]) -> dict[str, Any]:
    op = command.get("op")
    if op == "ping":
        return {"status": "ok", "time": now()}
    if op == "upsert_message":
        return upsert_message(con, command["row"])
    if op == "mark_revoked":
        return mark_revoked(con, command["row"])
    if op == "mark_missing_in_chat_window":
        return mark_missing_in_chat_window(con, command["row"])
    if op == "update_ack":
        return update_ack(con, command["row"])
    if op == "apply_reaction":
        return apply_reaction(con, command["row"])
    if op == "get_metadata":
        return get_metadata(con, str(command["key"]))
    if op == "set_metadata":
        return set_metadata(con, str(command["key"]), str(command["value"]))
    if op == "upsert_chat":
        return upsert_chat(con, command["row"])
    if op == "upsert_contact":
        return upsert_contact(con, command["row"])
    if op == "in_scope_contact_ids":
        return in_scope_contact_ids(con, int(command["active_since"]))
    if op == "prune_scope":
        return prune_scope(con, int(command["active_since"]), command.get("backup_path"))
    raise ValueError(f"unknown op: {op}")


def main() -> int:
    parser = argparse.ArgumentParser(description="WhatsApp Web source SQLite writer")
    parser.add_argument("--db", required=True, help="SQLite database path")
    args = parser.parse_args()

    con = connect(expand_path(args.db))
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        command: dict[str, Any] = {}
        try:
            command = json.loads(line)
            response = handle(con, command)
        except Exception as exc:  # This is the process boundary; return structured failure.
            response = {"status": "error", "error": f"{type(exc).__name__}: {exc}"}
        request_id = command.get("request_id")
        if request_id is not None:
            response["request_id"] = request_id
        print(json.dumps(response, ensure_ascii=False), flush=True)
    con.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
