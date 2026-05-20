from gateway.platforms.whatsapp_wal import WhatsAppGatewayWal


def test_append_pending_and_mark_processed(tmp_path):
    wal_path = tmp_path / "gateway_wal.jsonl"
    offset_path = tmp_path / "gateway_wal.offset"
    wal = WhatsAppGatewayWal(
        wal_path=wal_path,
        offset_path=offset_path,
        compact_every=100,
    )

    first = wal.append({"seq": 11, "chatId": "x", "body": "one"})
    second = wal.append({"seq": 12, "chatId": "x", "body": "two"})

    assert first is not None
    assert second is not None
    assert first["wal_seq"] == 1
    assert second["wal_seq"] == 2
    assert [row["bridge_seq"] for row in wal.pending()] == [11, 12]

    assert wal.mark_processed(1) is True
    assert [row["bridge_seq"] for row in wal.pending()] == [12]


def test_dedupe_by_bridge_seq_and_reload(tmp_path):
    wal_path = tmp_path / "gateway_wal.jsonl"
    offset_path = tmp_path / "gateway_wal.offset"
    wal = WhatsAppGatewayWal(
        wal_path=wal_path,
        offset_path=offset_path,
        compact_every=100,
    )

    first = wal.append({"seq": 99, "chatId": "x", "body": "one"})
    duplicate = wal.append({"seq": 99, "chatId": "x", "body": "one-again"})
    assert first is not None
    assert duplicate is None

    wal_reloaded = WhatsAppGatewayWal(
        wal_path=wal_path,
        offset_path=offset_path,
        compact_every=100,
    )
    assert wal_reloaded.append({"seq": 99, "chatId": "x", "body": "still-dup"}) is None
    assert len(wal_reloaded.pending()) == 1


def test_compact_drops_processed_prefix(tmp_path):
    wal_path = tmp_path / "gateway_wal.jsonl"
    offset_path = tmp_path / "gateway_wal.offset"
    wal = WhatsAppGatewayWal(
        wal_path=wal_path,
        offset_path=offset_path,
        compact_every=1,
    )

    wal.append({"seq": 1, "chatId": "x", "body": "one"})
    wal.append({"seq": 2, "chatId": "x", "body": "two"})
    wal.mark_processed(1)

    lines = [line for line in wal_path.read_text(encoding="utf-8").splitlines() if line.strip()]
    assert len(lines) == 1
    assert "\"bridge_seq\": 2" in lines[0]


def test_out_of_order_completion_waits_for_contiguous_prefix(tmp_path):
    wal_path = tmp_path / "gateway_wal.jsonl"
    offset_path = tmp_path / "gateway_wal.offset"
    wal = WhatsAppGatewayWal(
        wal_path=wal_path,
        offset_path=offset_path,
        compact_every=100,
    )

    wal.append({"seq": 11, "chatId": "x", "body": "one"})
    wal.append({"seq": 12, "chatId": "x", "body": "two"})

    assert wal.mark_processed(2) is True
    assert [row["wal_seq"] for row in wal.pending()] == [1, 2]

    assert wal.mark_processed(1) is True
    assert wal.pending() == []
