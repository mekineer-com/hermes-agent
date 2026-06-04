import json
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
