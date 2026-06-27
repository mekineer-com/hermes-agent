import importlib.util
import json
from pathlib import Path


SCRIPT_PATH = Path(__file__).resolve().parents[2] / "scripts" / "backfill_whatsapp_identity_jids.py"
spec = importlib.util.spec_from_file_location("backfill_whatsapp_identity_jids", SCRIPT_PATH)
backfill = importlib.util.module_from_spec(spec)
assert spec.loader is not None
spec.loader.exec_module(backfill)


def test_merge_policy_entries_preserves_metadata_from_all_aliases():
    merged = backfill.merge_policy_entries([
        {"policy": "full", "memorize": True, "display_name": "Annie Gottlieb", "source": "phone"},
        {"policy": "listen_only", "memorize": False, "lid_jid": "270699038040215@lid", "source": "lid"},
    ])

    assert merged == {
        "display_name": "Annie Gottlieb",
        "source": ["phone", "lid"],
        "lid_jid": "270699038040215@lid",
        "policy": "listen_only",
        "memorize": True,
    }


def test_channel_directory_rekey_keeps_name_when_lid_entry_exists_first(tmp_path):
    session_dir = tmp_path / "whatsapp" / "session"
    session_dir.mkdir(parents=True)
    (session_dir / "lid-mapping-19192593287.json").write_text(
        json.dumps("270699038040215"),
        encoding="utf-8",
    )
    directory = tmp_path / "channel_directory.json"
    directory.write_text(
        json.dumps({
            "platforms": {
                "whatsapp": [
                    {
                        "id": "270699038040215@lid",
                        "name": "270699038040215@lid",
                        "type": "dm",
                        "thread_id": None,
                    },
                    {
                        "id": "19192593287@s.whatsapp.net",
                        "name": "Annie Gottlieb",
                        "type": "dm",
                        "thread_id": None,
                    },
                ]
            }
        }),
        encoding="utf-8",
    )

    data, stats, collisions = backfill.migrate_channel_directory(
        directory,
        backfill.IdentityMap(session_dir),
    )

    assert stats == {"channel_ids_changed": 1, "channel_collisions": 1}
    assert collisions == ["270699038040215@lid"]
    assert data["platforms"]["whatsapp"] == [
        {
            "id": "270699038040215@lid",
            "name": "Annie Gottlieb",
            "type": "dm",
            "thread_id": None,
        }
    ]


def test_channel_directory_rekey_uses_known_contact_name_for_lid_placeholder(tmp_path):
    session_dir = tmp_path / "whatsapp" / "session"
    session_dir.mkdir(parents=True)
    (session_dir / "lid-mapping-19192593287.json").write_text(
        json.dumps("270699038040215"),
        encoding="utf-8",
    )
    (tmp_path / "whatsapp" / "known_contacts.json").write_text(
        json.dumps({
            "contacts": [
                {"id": "19192593287@s.whatsapp.net", "display_name": "Annie Gottlieb"}
            ]
        }),
        encoding="utf-8",
    )
    directory = tmp_path / "channel_directory.json"
    directory.write_text(
        json.dumps({
            "platforms": {
                "whatsapp": [
                    {
                        "id": "270699038040215@lid",
                        "name": "270699038040215@lid",
                        "type": "dm",
                        "thread_id": None,
                    },
                ]
            }
        }),
        encoding="utf-8",
    )

    data, stats, collisions = backfill.migrate_channel_directory(
        directory,
        backfill.IdentityMap(session_dir),
    )

    assert stats == {"channel_ids_changed": 0, "channel_collisions": 0}
    assert collisions == []
    assert data["platforms"]["whatsapp"] == [
        {
            "id": "270699038040215@lid",
            "name": "Annie Gottlieb",
            "type": "dm",
            "thread_id": None,
        }
    ]
