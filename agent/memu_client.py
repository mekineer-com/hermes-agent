"""memU HTTP client for Hermes soul-mode integration.

HTTP-first integration against mcp-memu-server's explicit integration routes.
The wrapper keeps payload normalization in one place and provides consistent
error handling for run_agent.
"""

from __future__ import annotations

import json
import logging
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime
from typing import Any

logger = logging.getLogger(__name__)


class MemuClientError(RuntimeError):
    """Raised when memU integration calls fail."""

    def __init__(
        self,
        message: str,
        *,
        status_code: int | None = None,
        response_body: str | None = None,
    ) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.response_body = response_body


def _content_to_text(content: Any) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts: list[str] = []
        for part in content:
            if isinstance(part, dict) and part.get("type") == "text":
                txt = str(part.get("text") or "").strip()
                if txt:
                    parts.append(txt)
        return "\n".join(parts)
    return str(content or "")


def _parse_iso_datetime(value: str) -> datetime | None:
    raw = str(value or "").strip()
    if not raw:
        return None
    try:
        # Accept trailing Z and local ISO inputs.
        return datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError:
        return None


def normalize_history_for_memu(
    history: list[dict[str, Any]] | None,
    *,
    user_name: str = "",
    soul_name: str = "",
) -> list[dict[str, Any]]:
    """Normalize Hermes conversation history into memU-compatible messages."""
    if not isinstance(history, list):
        return []

    role_to_name = {}
    if soul_name:
        role_to_name["assistant"] = soul_name

    normalized: list[dict[str, Any]] = []
    for msg in history:
        if not isinstance(msg, dict):
            continue
        role = str(msg.get("role") or "").strip().lower()
        if role not in {"user", "assistant"}:
            continue

        text = _content_to_text(msg.get("content")).strip()
        if not text:
            continue

        out: dict[str, Any] = {
            "role": role,
            "content": text,
        }
        # Preserve explicit participant names from transcript rows.
        # Never force-fill user names from session metadata: in multi-chat
        # scenarios that can stamp the wrong person across a DM transcript.
        name = str(msg.get("name") or "").strip()
        if not name:
            name = role_to_name.get(role, "")
        if name:
            out["name"] = name

        ts_ms = msg.get("ts_ms")
        if isinstance(ts_ms, int):
            out["ts_ms"] = ts_ms
        elif isinstance(ts_ms, float):
            out["ts_ms"] = int(ts_ms)
        else:
            ts_seconds = msg.get("timestamp")
            if isinstance(ts_seconds, (int, float)):
                out["ts_ms"] = int(float(ts_seconds) * 1000.0)
            else:
                dt = _parse_iso_datetime(msg.get("created_at"))
                if dt is not None:
                    out["ts_ms"] = int(dt.timestamp() * 1000.0)

        normalized.append(out)

    return normalized


class MemuHttpClient:
    """Thin HTTP client for mcp-memu-server integration endpoints."""

    def __init__(
        self,
        *,
        base_url: str,
        timeout_seconds: float = 45.0,
    ) -> None:
        raw = str(base_url or "").strip()
        if not raw:
            raise ValueError("memU base_url is required")
        self.base_url = raw.rstrip("/")
        self.timeout_seconds = float(timeout_seconds)

    def _post(self, path: str, payload: dict[str, Any]) -> dict[str, Any]:
        url = urllib.parse.urljoin(self.base_url + "/", path.lstrip("/"))
        request = urllib.request.Request(
            url,
            data=json.dumps(payload).encode("utf-8"),
            method="POST",
            headers={
                "Content-Type": "application/json",
                "Accept": "application/json",
            },
        )
        try:
            with urllib.request.urlopen(request, timeout=self.timeout_seconds) as response:
                raw = response.read().decode("utf-8", errors="replace")
        except urllib.error.HTTPError as exc:
            body = ""
            try:
                body = exc.read().decode("utf-8", errors="replace")
            except Exception:
                body = ""
            raise MemuClientError(
                f"memU HTTP {exc.code}: {exc.reason}",
                status_code=int(exc.code),
                response_body=body,
            ) from exc
        except urllib.error.URLError as exc:
            raise MemuClientError(f"memU request failed: {exc}") from exc

        if not raw:
            return {}
        try:
            parsed = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise MemuClientError("memU returned invalid JSON", response_body=raw) from exc

        if not isinstance(parsed, dict):
            raise MemuClientError(
                f"memU returned non-object response: {type(parsed).__name__}",
                response_body=raw,
            )
        return parsed

    def memu_turn(
        self,
        *,
        conversation_id: str,
        user_id: str,
        soul_id: str,
        message: str,
        history: list[dict[str, Any]] | None = None,
        history_user_name: str | None = None,
        user_name: str | None = None,
        soul_card: str | None = None,
        run_apimw: bool = True,
        apply_turn_maintenance: bool = True,
        debug: bool = False,
        temperature: float | None = None,
        channel_mode: str | None = None,
        chat_name: str | None = None,
        chat_type: str | None = None,
    ) -> dict[str, Any]:
        speaker_name = str(user_name or "").strip() or str(history_user_name or "").strip()
        payload: dict[str, Any] = {
            "conversation_id": str(conversation_id or "").strip(),
            "user_id": str(user_id or "").strip(),
            "soul_id": str(soul_id or "").strip(),
            "message": str(message or "").strip(),
            "history": normalize_history_for_memu(
                history,
                user_name=str(history_user_name or "").strip() or str(user_id or ""),
                soul_name=str(soul_id or ""),
            ),
            "run_apimw": bool(run_apimw),
            "apply_turn_maintenance": bool(apply_turn_maintenance),
            "debug": bool(debug),
        }
        if speaker_name:
            payload["user_name"] = speaker_name
        if soul_card:
            payload["soul_card"] = str(soul_card)
        if temperature is not None:
            payload["temperature"] = float(temperature)
        if channel_mode:
            payload["channel_mode"] = str(channel_mode)
        # chat_name / chat_type identify the originating chat (e.g. "Alice" / "dm").
        # memu uses them to render "Current chat:" in the turn prompt and to
        # validate the soul's response_peer against the actual chat.
        chat_name_clean = str(chat_name or "").strip()
        if chat_name_clean:
            payload["chat_name"] = chat_name_clean
        chat_type_clean = str(chat_type or "").strip()
        if chat_type_clean:
            payload["chat_type"] = chat_type_clean

        return self._post("/integration/memu/turn", payload)

    def append_message_only(
        self,
        *,
        conversation_id: str,
        user_id: str,
        soul_id: str,
        message: str,
        user_name: str | None = None,
        role: str = "user",
    ) -> dict[str, Any]:
        """Ingest one message into memU's messages table without engaging the soul.

        Used for listen-only WhatsApp channels: the soul stores the message
        for memorize + cross-chat context, but doesn't produce a response.
        """
        cid = str(conversation_id or "").strip()
        if not cid:
            raise MemuClientError("append_message_only requires conversation_id")
        payload: dict[str, Any] = {
            "user_id": str(user_id or "").strip(),
            "soul_id": str(soul_id or "").strip(),
            "message": str(message or "").strip(),
            "role": str(role or "user").strip(),
        }
        speaker_name = str(user_name or "").strip()
        if speaker_name:
            payload["user_name"] = speaker_name
        path = f"/conversation/{urllib.parse.quote(cid, safe='')}/messages/append"
        return self._post(path, payload)

    def memu_retrieve(
        self,
        *,
        user_id: str,
        soul_id: str,
        query: str | None = None,
        queries: list[dict[str, Any]] | None = None,
        conversation_id: str | None = None,
        method: str | None = None,
        top_k: int | None = None,
        as_of: str | None = None,
        history: list[dict[str, Any]] | None = None,
        soul_card: str | None = None,
        debug: bool = False,
    ) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "user_id": str(user_id or "").strip(),
            "soul_id": str(soul_id or "").strip(),
            "debug": bool(debug),
        }
        if query is not None:
            payload["query"] = str(query)
        if queries is not None:
            payload["queries"] = list(queries)
        if conversation_id is not None:
            payload["conversation_id"] = str(conversation_id)
        if method is not None:
            payload["method"] = str(method)
        if top_k is not None:
            payload["top_k"] = int(top_k)
        if as_of is not None:
            payload["as_of"] = str(as_of)
        if history is not None:
            payload["history"] = normalize_history_for_memu(history)
        if soul_card:
            payload["soul_card"] = str(soul_card)
        return self._post("/integration/memu/retrieve", payload)

    def memu_memorize(
        self,
        *,
        user_id: str,
        soul_id: str,
        conversation: list[dict[str, Any]],
        conversation_id: str | None = None,
        force: bool = False,
        time_zone: str | None = None,
        time_zone_offset_min: int | None = None,
    ) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "user_id": str(user_id or "").strip(),
            "soul_id": str(soul_id or "").strip(),
            "conversation": normalize_history_for_memu(conversation),
            "force": bool(force),
        }
        if conversation_id is not None:
            payload["conversation_id"] = str(conversation_id)
        if time_zone is not None:
            payload["time_zone"] = str(time_zone)
        if time_zone_offset_min is not None:
            payload["time_zone_offset_min"] = int(time_zone_offset_min)
        return self._post("/integration/memu/memorize", payload)

    def memu_consolidate(
        self,
        *,
        conversation_id: str,
        user_id: str,
        soul_id: str,
    ) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "conversation_id": str(conversation_id or "").strip(),
            "user_id": str(user_id or "").strip(),
            "soul_id": str(soul_id or "").strip(),
        }
        return self._post("/integration/memu/consolidate", payload)

    def memu_intentions(
        self,
        *,
        user_id: str,
        soul_id: str,
        status: str = "active",
    ) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "user_id": str(user_id or "").strip(),
            "soul_id": str(soul_id or "").strip(),
            "status": str(status or "active"),
        }
        return self._post("/integration/memu/intentions", payload)
