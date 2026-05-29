"""Policy lookups read from ``~/.hermes/memu.json``.

Today this file holds per-WhatsApp-channel routing policy. It will likely grow
to carry other memU-adjacent operator settings (the launcher is the intended
editor). Read fresh on every event so a hand-edit or launcher write takes
effect without a Hermes restart.
"""

from __future__ import annotations

import json
import logging
from typing import Any, Literal

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


def _default_memorize_for_policy(policy: WhatsAppChannelPolicy) -> bool:
    return policy != "excluded"


def _read_whatsapp_channel_entry(chat_id: str) -> dict | None:
    raw = str(chat_id or "").strip()
    if not raw:
        return None
    config = _read_memu_config()
    channels = (
        config.get("whatsapp", {}).get("channels", {})
        if isinstance(config.get("whatsapp"), dict)
        else {}
    )
    if not isinstance(channels, dict) or not channels:
        return None

    canonical = canonical_whatsapp_identifier(raw) or ""
    normalized = normalize_whatsapp_identifier(raw) or ""
    for candidate in (raw, canonical, normalized):
        if not candidate:
            continue
        entry = channels.get(candidate)
        if isinstance(entry, dict):
            return entry
    return None


def whatsapp_channel_policy(chat_id: str) -> WhatsAppChannelPolicy:
    """Return the configured policy for a WhatsApp chat. Default is ``full``.

    Looks up the chat under ``whatsapp.channels.<chat_id>.policy`` using the
    canonical form of the identifier (collapses phone/LID variants). Returns
    ``full`` if the chat isn't listed or the file is missing/malformed.
    """
    entry = _read_whatsapp_channel_entry(chat_id)
    if isinstance(entry, dict):
        policy = str(entry.get("policy") or "").strip().lower()
        if policy in {"full", "listen_only", "excluded"}:
            return policy  # type: ignore[return-value]
    return "full"


def whatsapp_channel_memorize(chat_id: str) -> bool:
    """Return whether this chat should be extracted during memorize."""
    policy = whatsapp_channel_policy(chat_id)
    if policy == "excluded":
        return False
    entry = _read_whatsapp_channel_entry(chat_id)
    if isinstance(entry, dict):
        raw_memorize = entry.get("memorize")
        if isinstance(raw_memorize, bool):
            return raw_memorize
    return _default_memorize_for_policy(policy)


def whatsapp_channel_settings(chat_id: str) -> tuple[WhatsAppChannelPolicy, bool]:
    """Return (policy, memorize) for a WhatsApp chat from ``~/.hermes/memu.json``."""
    entry = _read_whatsapp_channel_entry(chat_id)
    policy: WhatsAppChannelPolicy = "full"
    if isinstance(entry, dict):
        raw_policy = str(entry.get("policy") or "").strip().lower()
        if raw_policy in {"full", "listen_only", "excluded"}:
            policy = raw_policy  # type: ignore[assignment]
    if policy == "excluded":
        return policy, False
    if isinstance(entry, dict) and isinstance(entry.get("memorize"), bool):
        return policy, bool(entry.get("memorize"))
    return policy, _default_memorize_for_policy(policy)


def _is_soul_mode_session(gateway: Any, session_key: str) -> bool:
    """Return True when the given gateway session resolves to soul mode."""
    resolved_session_key = str(session_key or "").strip()
    if not resolved_session_key:
        return False
    try:
        from agent.soul_mode import resolve_agent_config
        user_config = gateway._read_user_config()
        cfg = resolve_agent_config(user_config, resolved_session_key)
        return bool(cfg.get("enabled")) and str(cfg.get("role") or "").strip().lower() == "soul"
    except Exception as exc:
        logger.warning(
            "memu_policy: failed soul-mode resolve for session=%s: %s",
            resolved_session_key,
            exc,
        )
        return False


def should_skip_soul_mode_auto_resume(gateway: Any, session_key: str) -> bool:
    """True when startup auto-resume should not synthesize an internal event."""
    return _is_soul_mode_session(gateway, session_key)


def should_suppress_soul_mode_resume_note(gateway: Any, session_key: str) -> bool:
    """True when gateway resume/tool-tail system-note prepend should be skipped."""
    return _is_soul_mode_session(gateway, session_key)
