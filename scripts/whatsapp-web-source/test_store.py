import json
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

import store


def row(msg_key="m1", source="event:message", body="hello", msg_type="chat", **overrides):
    data = {
        "msg_key": msg_key,
        "chat_id": "123@c.us",
        "chat_local_id": "123",
        "from_me": False,
        "timestamp": 100,
        "type": msg_type,
        "body": body,
        "author_id": None,
        "author_local_id": "",
        "from_id": "123@c.us",
        "from_local_id": "123",
        "to_id": "15133278228@c.us",
        "to_local_id": "15133278228",
        "has_media": False,
        "media_placeholder": None,
        "ack": 0,
        "revoked": False,
        "revoke_source": None,
        "source": source,
        "raw": {"source": source, "type": msg_type},
    }
    data.update(overrides)
    return data


class StoreTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.con = store.connect(Path(self.tmp.name) / "web_source.db")

    def tearDown(self):
        self.con.close()
        self.tmp.cleanup()

    def fetch_message(self):
        return self.con.execute(
            """
            select msg_key, chat_id, chat_local_id, from_me, timestamp, type, body,
                   author_id, from_id, to_id, has_media, source, ack, revoked
            from whatsapp_messages where msg_key = 'm1'
            """
        ).fetchone()

    def test_ciphers_do_not_degrade_decrypted_message(self):
        store.upsert_message(self.con, row())
        store.upsert_message(
            self.con,
            row(source="event:message_ciphertext", body="", msg_type="ciphertext"),
        )

        got = self.fetch_message()
        self.assertEqual(got["type"], "chat")
        self.assertEqual(got["body"], "hello")
        self.assertEqual(got["source"], "event:message")

    def test_lower_rank_event_cannot_clobber_stable_fields(self):
        store.upsert_message(
            self.con,
            row(chat_id="123@s.whatsapp.net", chat_local_id="123", timestamp=100, has_media=True),
        )
        store.upsert_message(
            self.con,
            row(
                source="event:message_ciphertext",
                body="",
                msg_type="ciphertext",
                chat_id="456@c.us",
                chat_local_id="456",
                from_me=True,
                timestamp=99,
                has_media=False,
            ),
        )

        got = self.fetch_message()
        self.assertEqual(got["chat_id"], "123@s.whatsapp.net")
        self.assertEqual(got["from_me"], 0)
        self.assertEqual(got["timestamp"], 100)
        self.assertEqual(got["has_media"], 1)

    def test_edit_enriches_body_and_source(self):
        store.upsert_message(self.con, row())
        store.upsert_message(self.con, row(source="event:message_edit", body="edited"))

        got = self.fetch_message()
        self.assertEqual(got["body"], "edited")
        self.assertEqual(got["source"], "event:message_edit")

    def test_lower_rank_event_cannot_clobber_edited_body_or_sender_ids(self):
        store.upsert_message(
            self.con,
            row(
                source="event:message_edit",
                body="edited",
                author_id="author-phone@c.us",
                from_id="from-phone@c.us",
                to_id="to-phone@c.us",
            ),
        )
        store.upsert_message(
            self.con,
            row(
                source="event:message_create",
                body="stale",
                author_id="author-lid@lid",
                from_id="from-lid@lid",
                to_id="to-lid@lid",
            ),
        )

        got = self.fetch_message()
        self.assertEqual(got["body"], "edited")
        self.assertEqual(got["author_id"], "author-phone@c.us")
        self.assertEqual(got["from_id"], "from-phone@c.us")
        self.assertEqual(got["to_id"], "to-phone@c.us")
        self.assertEqual(got["source"], "event:message_edit")

    def test_ack_and_revoke_update_existing_row(self):
        store.upsert_message(self.con, row())
        store.update_ack(self.con, {"msg_key": "m1", "ack": 2})
        store.mark_revoked(self.con, {"msg_key": "m1", "source": "event:message_revoke_everyone"})

        got = self.fetch_message()
        self.assertEqual(got["ack"], 2)
        self.assertEqual(got["revoked"], 1)

    def test_metadata_round_trip(self):
        self.assertIsNone(store.get_metadata(self.con, "backfill:k")["value"])

        store.set_metadata(self.con, "backfill:k", "1")
        got = store.get_metadata(self.con, "backfill:k")

        self.assertEqual(got["value"], "1")
        self.assertIsInstance(got["updated_at"], int)

    def test_contact_upsert_keeps_existing_names_when_later_snapshot_is_sparse(self):
        store.upsert_contact(
            self.con,
            {
                "contact_id": "140063262396533@lid",
                "contact_local_id": "140063262396533",
                "name": "Raquel Scarone",
                "short_name": "Raquel",
                "push_name": "Raquel",
                "verified_name": None,
                "is_me": False,
                "is_user": True,
                "is_group": False,
                "raw": {"id": "140063262396533@lid", "name": "Raquel Scarone"},
            },
        )
        store.upsert_contact(
            self.con,
            {
                "contact_id": "140063262396533@lid",
                "contact_local_id": "140063262396533",
                "name": None,
                "short_name": None,
                "push_name": None,
                "verified_name": None,
                "is_me": False,
                "is_user": True,
                "is_group": False,
                "raw": {"id": "140063262396533@lid"},
            },
        )

        got = self.con.execute(
            """
            select contact_id, contact_local_id, name, short_name, push_name
            from whatsapp_contacts where contact_id = '140063262396533@lid'
            """
        ).fetchone()
        self.assertEqual(got["contact_local_id"], "140063262396533")
        self.assertEqual(got["name"], "Raquel Scarone")
        self.assertEqual(got["short_name"], "Raquel")
        self.assertEqual(got["push_name"], "Raquel")

    def test_in_scope_contact_ids_include_active_chat_and_senders_only(self):
        store.upsert_message(
            self.con,
            row(
                msg_key="old",
                chat_id="old-group@g.us",
                chat_local_id="old-group",
                from_id="old-sender@c.us",
                from_local_id="old-sender",
                timestamp=90,
            ),
        )
        store.upsert_message(
            self.con,
            row(
                msg_key="new",
                chat_id="new-group@g.us",
                chat_local_id="new-group",
                from_id="new-sender@c.us",
                from_local_id="new-sender",
                author_id="group-author@c.us",
                author_local_id="group-author",
                timestamp=110,
            ),
        )

        got = store.in_scope_contact_ids(self.con, 100)

        self.assertEqual(got["contact_ids"], [
            "15133278228@c.us",
            "group-author@c.us",
            "new-group@g.us",
            "new-sender@c.us",
        ])
        self.assertEqual(got["contact_local_ids"], [
            "15133278228",
            "group-author",
            "new-group",
            "new-sender",
        ])

    def test_prune_scope_keeps_only_contacts_and_chats_with_active_messages(self):
        store.upsert_message(
            self.con,
            row(
                msg_key="active",
                chat_id="active@g.us",
                chat_local_id="active",
                from_id="sender@c.us",
                from_local_id="sender",
                timestamp=110,
            ),
        )
        store.upsert_chat(
            self.con,
            {
                "chat_id": "active@g.us",
                "chat_local_id": "active",
                "name": "Active",
                "is_group": True,
                "last_timestamp": 110,
                "raw": {},
            },
        )
        store.upsert_chat(
            self.con,
            {
                "chat_id": "old@g.us",
                "chat_local_id": "old",
                "name": "Old",
                "is_group": True,
                "last_timestamp": 90,
                "raw": {},
            },
        )
        for contact_id, local_id, is_me in [
            ("active@g.us", "active", False),
            ("sender@c.us", "sender", False),
            ("old@c.us", "old", False),
            ("me@c.us", "me", True),
        ]:
            store.upsert_contact(
                self.con,
                {
                    "contact_id": contact_id,
                    "contact_local_id": local_id,
                    "name": contact_id,
                    "short_name": None,
                    "push_name": None,
                    "verified_name": None,
                    "is_me": is_me,
                    "is_user": True,
                    "is_group": contact_id.endswith("@g.us"),
                    "raw": {},
                },
            )

        backup_path = Path(self.tmp.name) / "web_source.db.bak-prune-test"
        result = store.prune_scope(self.con, 100, str(backup_path))
        chats = [
            row["chat_id"]
            for row in self.con.execute("select chat_id from whatsapp_chats order by chat_id")
        ]
        contacts = [
            row["contact_id"]
            for row in self.con.execute("select contact_id from whatsapp_contacts order by contact_id")
        ]

        self.assertEqual(result["deleted_chats"], 1)
        self.assertEqual(result["deleted_contacts"], 1)
        self.assertEqual(result["backup_path"], str(backup_path))
        self.assertTrue(backup_path.exists())
        self.assertEqual(chats, ["active@g.us"])
        self.assertEqual(contacts, ["active@g.us", "me@c.us", "sender@c.us"])
        with sqlite3.connect(backup_path) as backup:
            old_contact = backup.execute(
                "select contact_id from whatsapp_contacts where contact_id = 'old@c.us'"
            ).fetchone()
        self.assertIsNotNone(old_contact)

    def test_malformed_json_returns_error_without_stale_request_id(self):
        proc = subprocess.Popen(
            [sys.executable, str(Path(__file__).with_name("store.py")), "--db", str(Path(self.tmp.name) / "writer.db")],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        assert proc.stdin is not None
        assert proc.stdout is not None
        proc.stdin.write(json.dumps({"request_id": 7, "op": "ping"}) + "\n")
        proc.stdin.write("{bad json\n")
        proc.stdin.flush()

        first = json.loads(proc.stdout.readline())
        second = json.loads(proc.stdout.readline())
        proc.stdin.close()
        proc.wait(timeout=5)
        proc.stdout.close()
        assert proc.stderr is not None
        proc.stderr.close()

        self.assertEqual(first["status"], "ok")
        self.assertEqual(first["request_id"], 7)
        self.assertEqual(second["status"], "error")
        self.assertNotIn("request_id", second)


if __name__ == "__main__":
    unittest.main()
