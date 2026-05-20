"""Durable WAL for WhatsApp gateway ingress.

Stores polled bridge events durably before bridge ack so gateway restarts
cannot drop messages between poll and background processing completion.
"""

from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path
from typing import Any, Dict, List, Optional


class WhatsAppGatewayWal:
    """Append-only WAL with offset checkpoint and periodic compaction."""

    def __init__(
        self,
        *,
        wal_path: Path,
        offset_path: Path,
        compact_every: int = 100,
    ) -> None:
        self._wal_path = wal_path
        self._offset_path = offset_path
        self._compact_every = max(1, int(compact_every))
        self._wal_path.parent.mkdir(parents=True, exist_ok=True)
        self._processed_up_to = self._read_processed_offset()
        self._next_wal_seq = self._processed_up_to + 1
        self._bridge_seq_to_wal_seq: dict[int, int] = {}
        self._completed_out_of_order: set[int] = set()
        self._since_compaction = 0
        self._reload_index()

    @property
    def processed_up_to(self) -> int:
        return self._processed_up_to

    def append(self, event: Dict[str, Any]) -> Optional[Dict[str, Any]]:
        """Append an event to WAL unless bridge_seq is already tracked."""
        bridge_seq = self._coerce_bridge_seq(event.get("seq"))
        if bridge_seq is None:
            return None
        if bridge_seq in self._bridge_seq_to_wal_seq:
            return None
        row = {
            "wal_seq": self._next_wal_seq,
            "bridge_seq": bridge_seq,
            "event": event,
        }
        self._next_wal_seq += 1
        self._append_row(row)
        self._bridge_seq_to_wal_seq[bridge_seq] = int(row["wal_seq"])
        return row

    def mark_processed(self, wal_seq: Any) -> bool:
        """Advance processed offset when WAL rows are completed contiguously."""
        parsed = self._coerce_non_negative_int(wal_seq)
        if parsed is None or parsed <= self._processed_up_to:
            return False
        if parsed > self._processed_up_to + 1:
            self._completed_out_of_order.add(parsed)
            return True
        advanced = 1
        self._processed_up_to = parsed
        while (self._processed_up_to + 1) in self._completed_out_of_order:
            next_seq = self._processed_up_to + 1
            self._completed_out_of_order.remove(next_seq)
            self._processed_up_to = next_seq
            advanced += 1
        self._write_processed_offset()
        self._since_compaction += advanced
        if self._since_compaction >= self._compact_every:
            self.compact()
        return True

    def pending(self) -> List[Dict[str, Any]]:
        """Return WAL rows above processed offset in wal_seq order."""
        rows = self._read_rows()
        return [
            row
            for row in rows
            if self._coerce_non_negative_int(row.get("wal_seq"), allow_zero=False)
            and int(row["wal_seq"]) > self._processed_up_to
        ]

    def compact(self) -> None:
        """Drop processed prefix and rebuild bridge_seq index."""
        pending_rows = self.pending()
        self._write_rows_atomically(pending_rows)
        self._reload_index()
        self._since_compaction = 0

    def _reload_index(self) -> None:
        self._bridge_seq_to_wal_seq = {}
        max_wal_seq = self._processed_up_to
        for row in self._read_rows():
            wal_seq = self._coerce_non_negative_int(row.get("wal_seq"), allow_zero=False)
            bridge_seq = self._coerce_bridge_seq(row.get("bridge_seq"))
            if wal_seq is None or bridge_seq is None:
                continue
            max_wal_seq = max(max_wal_seq, wal_seq)
            if wal_seq > self._processed_up_to:
                self._bridge_seq_to_wal_seq[bridge_seq] = wal_seq
        self._next_wal_seq = max_wal_seq + 1

    def _read_processed_offset(self) -> int:
        if not self._offset_path.exists():
            return 0
        try:
            raw = self._offset_path.read_text(encoding="utf-8").strip()
        except OSError:
            return 0
        parsed = self._coerce_non_negative_int(raw)
        return parsed or 0

    def _write_processed_offset(self) -> None:
        self._write_text_atomically(self._offset_path, f"{self._processed_up_to}\n")

    def _append_row(self, row: Dict[str, Any]) -> None:
        line = json.dumps(row, ensure_ascii=False) + "\n"
        with self._wal_path.open("a", encoding="utf-8") as handle:
            handle.write(line)
            handle.flush()
            os.fsync(handle.fileno())

    def _read_rows(self) -> List[Dict[str, Any]]:
        if not self._wal_path.exists():
            return []
        try:
            raw = self._wal_path.read_text(encoding="utf-8")
        except OSError:
            return []
        rows: list[Dict[str, Any]] = []
        for line in raw.splitlines():
            stripped = line.strip()
            if not stripped:
                continue
            try:
                parsed = json.loads(stripped)
            except json.JSONDecodeError:
                continue
            if isinstance(parsed, dict):
                rows.append(parsed)
        return rows

    def _write_rows_atomically(self, rows: List[Dict[str, Any]]) -> None:
        data = "".join(json.dumps(row, ensure_ascii=False) + "\n" for row in rows)
        self._write_text_atomically(self._wal_path, data)

    @staticmethod
    def _coerce_non_negative_int(value: Any, *, allow_zero: bool = True) -> Optional[int]:
        try:
            parsed = int(value)
        except (TypeError, ValueError):
            return None
        if parsed < 0:
            return None
        if not allow_zero and parsed == 0:
            return None
        return parsed

    @classmethod
    def _coerce_bridge_seq(cls, value: Any) -> Optional[int]:
        return cls._coerce_non_negative_int(value, allow_zero=False)

    @staticmethod
    def _write_text_atomically(path: Path, data: str) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp_path = tempfile.mkstemp(
            dir=str(path.parent),
            prefix=f".{path.name}.",
            suffix=".tmp",
        )
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                handle.write(data)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(tmp_path, path)
            dir_fd = os.open(str(path.parent), os.O_RDONLY)
            try:
                os.fsync(dir_fd)
            finally:
                os.close(dir_fd)
        except BaseException:
            try:
                os.unlink(tmp_path)
            except OSError:
                pass
            raise
