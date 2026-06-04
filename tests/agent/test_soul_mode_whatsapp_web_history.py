import sqlite3
from types import SimpleNamespace

import pytest

from agent.memu_client import MemuClientError
from agent.soul_mode import configure, _load_history
from gateway.status import read_runtime_status
from hermes_state import SessionDB


def _init_web_source_db(path):
    con = sqlite3.connect(path)
    con.executescript(
        """
        create table whatsapp_messages (
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
        create table whatsapp_contacts (
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
        """
    )
    return con


def _insert_message(
    con,
    *,
    msg_key,
    body,
    timestamp,
    from_me=False,
    author_id=None,
    from_id="140063262396533@lid",
    chat_id="18322935409-1579788049@g.us",
):
    con.execute(
        """
        insert into whatsapp_messages (
          msg_key, chat_id, chat_local_id, from_me, timestamp, type, body,
          author_id, author_local_id, from_id, from_local_id, to_id, to_local_id,
          has_media, source, first_seen_at, updated_at, raw_json
        )
        values (?, ?, ?, ?, ?, 'chat', ?, ?, '', ?, '', null, '', 0, 'backfill:test', ?, ?, '{}')
        """,
        (
            msg_key,
            chat_id,
            chat_id.split("@", 1)[0],
            int(from_me),
            timestamp,
            body,
            author_id,
            from_id,
            timestamp,
            timestamp,
        ),
    )


def _agent(tmp_path, web_db, *, current_source_message_id=""):
    state_db = SessionDB(tmp_path / "state.db")
    with state_db._lock:
        state_db._conn.execute(
            "insert into souls(soul_id, active_since) values (?, ?)",
            ("Siri", 200),
        )
    return SimpleNamespace(
        platform="whatsapp",
        session_id="s1",
        _session_db=state_db,
        _chat_id="18322935409-1579788049@g.us",
        _user_name="Marcos",
        _gateway_source_message_id=current_source_message_id,
    )


def _web_config(web_db, **kwargs):
    data = {
        "enabled": True,
        "role": "soul",
        "soul_id": "Siri",
        "user_id": "marcos",
        "whatsapp_history_source": "web_source",
        "whatsapp_web_source_db": str(web_db),
        "whatsapp_reply_prefix": "✦ *Siri*: ",
    }
    data.update(kwargs)
    return configure(**data)


def test_whatsapp_web_history_applies_active_since_and_contact_names(tmp_path):
    web_db = tmp_path / "web_source.db"
    con = _init_web_source_db(web_db)
    con.execute(
        """
        insert into whatsapp_contacts (
          contact_id, contact_local_id, name, short_name, push_name, updated_at
        ) values ('140063262396533@lid', '140063262396533', 'Raquel Scarone', 'Raquel', 'Raquel', 1)
        """
    )
    _insert_message(con, msg_key="old", body="too old", timestamp=199, author_id="140063262396533@lid")
    _insert_message(con, msg_key="new", body="Hello Siri", timestamp=201, author_id="140063262396533@lid")
    con.commit()
    con.close()

    config = _web_config(web_db)

    history = _load_history(_agent(tmp_path, web_db), [], config)

    assert [m["content"] for m in history] == ["Hello Siri"]
    assert history[0]["role"] == "user"
    assert history[0]["sender_name"] == "Raquel"


def test_whatsapp_web_history_excludes_current_turn_and_uses_delivered_assistant_id(tmp_path):
    web_db = tmp_path / "web_source.db"
    con = _init_web_source_db(web_db)
    _insert_message(
        con,
        msg_key="true_18322935409-1579788049@g.us_3EB0CURRENT_114628432556258@lid",
        body="current message",
        timestamp=202,
        author_id="114628432556258@lid",
        from_me=True,
    )
    _insert_message(
        con,
        msg_key="true_18322935409-1579788049@g.us_3EB0SIRI_114628432556258@lid",
        body="✦ *Siri*: I am listening.",
        timestamp=203,
        from_me=True,
        from_id="114628432556258@lid",
    )
    con.commit()
    con.close()

    config = _web_config(web_db)
    agent = _agent(tmp_path, web_db, current_source_message_id="3EB0CURRENT")
    agent._session_db.create_session(session_id="s1", source="whatsapp", user_id="marcos")
    agent._session_db.append_message(
        session_id="s1",
        role="assistant",
        content="I am listening.",
        source_chat_id="18322935409-1579788049@g.us",
        source_message_id="3EB0SIRI",
        timestamp=203,
    )

    history = _load_history(
        agent,
        [],
        config,
    )

    assert [(m["role"], m["content"], m["sender_name"]) for m in history] == [
        ("assistant", "I am listening.", "Siri")
    ]


def test_whatsapp_web_history_does_not_infer_soul_from_prefix_without_delivery_id(tmp_path):
    web_db = tmp_path / "web_source.db"
    con = _init_web_source_db(web_db)
    _insert_message(
        con,
        msg_key="true_18322935409-1579788049@g.us_3EB0UNKNOWN_114628432556258@lid",
        body="✦ *Siri*: I was typed from the phone.",
        timestamp=203,
        from_me=True,
        from_id="114628432556258@lid",
    )
    con.commit()
    con.close()

    history = _load_history(_agent(tmp_path, web_db), [], _web_config(web_db))

    assert [(m["role"], m["content"], m["sender_name"]) for m in history] == [
        ("user", "✦ *Siri*: I was typed from the phone.", "Marcos")
    ]


def test_whatsapp_web_history_empty_result_does_not_fall_back_to_state_db(tmp_path):
    web_db = tmp_path / "web_source.db"
    con = _init_web_source_db(web_db)
    con.commit()
    con.close()
    agent = _agent(tmp_path, web_db)
    agent._session_db.create_session(session_id="s1", source="whatsapp", user_id="marcos")
    agent._session_db.append_message(
        session_id="s1",
        role="user",
        content="before Siri joined WhatsApp",
        timestamp=100,
    )
    agent._session_db.append_message(
        session_id="s1",
        role="user",
        content="after Siri joined WhatsApp",
        timestamp=201,
    )

    config = _web_config(web_db)

    history = _load_history(agent, [], config)

    assert history == []


def test_whatsapp_web_history_missing_db_fails_loud_and_marks_health(tmp_path, monkeypatch, caplog):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / "home"))
    web_db = tmp_path / "missing.db"
    config = _web_config(web_db)

    with pytest.raises(MemuClientError, match="WhatsApp web_source history failed"):
        _load_history(_agent(tmp_path, web_db), [], config)

    assert "refusing state_db fallback" in caplog.text
    state = read_runtime_status()
    whatsapp = state["platforms"]["whatsapp"]
    assert whatsapp["state"] == "degraded"
    assert whatsapp["soul_history"]["state"] == "degraded"
    assert whatsapp["soul_history"]["source"] == "web_source"


def test_whatsapp_web_history_allows_reply_prefix_changes_when_delivery_id_matches(tmp_path):
    web_db = tmp_path / "web_source.db"
    con = _init_web_source_db(web_db)
    _insert_message(
        con,
        msg_key="true_18322935409-1579788049@g.us_3EB0SIRI_114628432556258@lid",
        body="old prefix: I still know this is Siri.",
        timestamp=203,
        from_me=True,
        from_id="114628432556258@lid",
    )
    con.commit()
    con.close()
    config = _web_config(web_db, whatsapp_reply_prefix="⚕ *Hermes Agent*\n────────────\n")
    agent = _agent(tmp_path, web_db)
    agent._session_db.create_session(session_id="s1", source="whatsapp", user_id="marcos")
    agent._session_db.append_message(
        session_id="s1",
        role="assistant",
        content="I still know this is Siri.",
        source_chat_id="18322935409-1579788049@g.us",
        source_message_id="3EB0SIRI",
        timestamp=203,
    )

    history = _load_history(agent, [], config)

    assert [(m["role"], m["content"], m["sender_name"]) for m in history] == [
        ("assistant", "old prefix: I still know this is Siri.", "Siri")
    ]
