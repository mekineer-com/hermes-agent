"""Policy lookups read from ``~/.hermes/memu.json``.

Today this file holds per-WhatsApp-channel routing policy. It will likely grow
to carry other memU-adjacent operator settings (the launcher is the intended
editor). Read fresh on every event so a hand-edit or launcher write takes
effect without a Hermes restart.
"""

from __future__ import annotations

import json
import logging
from typing import Literal

from gateway.whatsapp_identity import canonical_whatsapp_identifier, normalize_whatsapp_identifier
from hermes_constants import get_hermes_home

logger = logging.getLogger(__name__)

WhatsAppChannelPolicy = Literal["full", "listen_only", "excluded"]


def _memu_json_path():
    return get_hermes_home() / "memu.json"


def _read_memu_config() -> dict:
    path = _memu_json_path()
    if not path.exists():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        logger.warning("memu_policy: failed to read %s: %s", path, exc)
        return {}
    return data if isinstance(data, dict) else {}


def whatsapp_channel_policy(chat_id: str) -> WhatsAppChannelPolicy:
    """Return the configured policy for a WhatsApp chat. Default is ``full``.

    Looks up the chat under ``whatsapp.channels.<chat_id>.policy`` using the
    canonical form of the identifier (collapses phone/LID variants). Returns
    ``full`` if the chat isn't listed or the file is missing/malformed.
    """
    raw = str(chat_id or "").strip()
    if not raw:
        return "full"
    config = _read_memu_config()
    channels = (
        config.get("whatsapp", {}).get("channels", {})
        if isinstance(config.get("whatsapp"), dict)
        else {}
    )
    if not isinstance(channels, dict) or not channels:
        return "full"

    # Try several identifier shapes: exact, canonical, and normalized.
    canonical = canonical_whatsapp_identifier(raw) or ""
    normalized = normalize_whatsapp_identifier(raw) or ""
    for candidate in (raw, canonical, normalized):
        if not candidate:
            continue
        entry = channels.get(candidate)
        if isinstance(entry, dict):
            policy = str(entry.get("policy") or "").strip().lower()
            if policy in {"full", "listen_only", "excluded"}:
                return policy  # type: ignore[return-value]
    return "full"
