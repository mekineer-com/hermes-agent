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
          raw_json text not null
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

        create table if not exists whatsapp_routing_status (
          inbound_msg_key text primary key,
          chat_id text not null,
          chat_local_id text not null,
          status text not null,
          reply_msg_key text,
          reason text,
          updated_at integer not null
        );
        """
    )
    con.commit()


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
            when excluded.body is not null and excluded.body != '' then excluded.body
            else whatsapp_messages.body
          end,
          author_id=coalesce(excluded.author_id, whatsapp_messages.author_id),
          author_local_id=coalesce(excluded.author_local_id, whatsapp_messages.author_local_id),
          from_id=coalesce(excluded.from_id, whatsapp_messages.from_id),
          from_local_id=coalesce(excluded.from_local_id, whatsapp_messages.from_local_id),
          to_id=coalesce(excluded.to_id, whatsapp_messages.to_id),
          to_local_id=coalesce(excluded.to_local_id, whatsapp_messages.to_local_id),
          has_media=case when ? then excluded.has_media else whatsapp_messages.has_media end,
          media_placeholder=coalesce(excluded.media_placeholder, whatsapp_messages.media_placeholder),
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
        ),
    )
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


def update_ack(con: sqlite3.Connection, row: dict[str, Any]) -> dict[str, Any]:
    msg_key = row["msg_key"]
    ts = now()
    cur = con.execute(
        "update whatsapp_messages set ack = ?, updated_at = ? where msg_key = ?",
        (row.get("ack"), ts, msg_key),
    )
    con.commit()
    return {"status": "ok", "action": "ack", "msg_key": msg_key, "matched": cur.rowcount}


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


def handle(con: sqlite3.Connection, command: dict[str, Any]) -> dict[str, Any]:
    op = command.get("op")
    if op == "ping":
        return {"status": "ok", "time": now()}
    if op == "upsert_message":
        return upsert_message(con, command["row"])
    if op == "mark_revoked":
        return mark_revoked(con, command["row"])
    if op == "update_ack":
        return update_ack(con, command["row"])
    if op == "upsert_chat":
        return upsert_chat(con, command["row"])
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
