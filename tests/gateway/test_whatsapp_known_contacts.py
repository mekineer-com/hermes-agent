import json

from gateway.whatsapp_known_contacts import load_known_whatsapp_names


def test_known_chats_does_not_use_group_last_sender_as_group_name(tmp_path):
    whatsapp_dir = tmp_path / "whatsapp"
    whatsapp_dir.mkdir()
    (whatsapp_dir / "known_chats.json").write_text(
        json.dumps({
            "chats": [
                {
                    "id": "120363424209497293@g.us",
                    "is_group": True,
                    "name": "",
                    "last_sender_name": "Test Contact",
                },
                {
                    "id": "12025550188@s.whatsapp.net",
                    "is_group": False,
                    "name": "",
                    "last_sender_name": "Test Contact",
                },
            ]
        }),
        encoding="utf-8",
    )

    names = load_known_whatsapp_names(tmp_path)

    assert "120363424209497293@g.us" not in names
    assert names["12025550188@s.whatsapp.net"] == "Test Contact"
