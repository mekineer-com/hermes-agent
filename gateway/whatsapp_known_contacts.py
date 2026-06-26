"""Read WhatsApp display names persisted by the bridge."""

from __future__ import annotations

import json
import re
from collections.abc import Callable
from pathlib import Path
from typing import Any

from .whatsapp_identity import to_whatsapp_jid

PHONE_DOMAINS = {"s.whatsapp.net", "c.us"}


def is_placeholder_whatsapp_name(value: Any) -> bool:
    raw = str(value or "").strip()
    if not raw:
        return True
    normalized = to_whatsapp_jid(raw)
    local = normalized.split("@", 1)[0]
    if not local or not re.fullmatch(r"\d+", local):
        return False
    return normalized in {
        local,
        f"{local}@lid",
        f"{local}@s.whatsapp.net",
        f"{local}@c.us",
    }


def load_known_whatsapp_names(
    home: Path,
    *,
    canonicalize: Callable[[Any], str] | None = None,
) -> dict[str, str]:
    """Return id/alias -> human display name from bridge known-state files."""
    root = home.expanduser() / "whatsapp"
    names: dict[str, str] = {}

    for chat in _rows(root / "known_chats.json", "chats"):
        is_group = bool(chat.get("is_group"))
        name = str(chat.get("name") or ("" if is_group else chat.get("last_sender_name")) or "").strip()
        _add_name(names, chat.get("id"), name, canonicalize=canonicalize, replace=False)

    for contact in _rows(root / "known_contacts.json", "contacts"):
        _add_name(
            names,
            contact.get("id"),
            str(contact.get("display_name") or "").strip(),
            canonicalize=canonicalize,
            replace=True,
        )

    return names


def known_whatsapp_name(names: dict[str, str], identifier: Any) -> str:
    for key in _identifier_keys(identifier, canonicalize=None):
        found = names.get(key)
        if found:
            return found
    return ""


def _rows(path: Path, key: str) -> list[dict[str, Any]]:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (FileNotFoundError, OSError, json.JSONDecodeError):
        return []
    rows = data.get(key) if isinstance(data, dict) else None
    return [row for row in rows if isinstance(row, dict)] if isinstance(rows, list) else []


def _add_name(
    names: dict[str, str],
    identifier: Any,
    name: str,
    *,
    canonicalize: Callable[[Any], str] | None,
    replace: bool,
) -> None:
    if is_placeholder_whatsapp_name(name):
        return
    for key in _identifier_keys(identifier, canonicalize=canonicalize):
        if replace or key not in names:
            names[key] = name


def _identifier_keys(
    identifier: Any,
    *,
    canonicalize: Callable[[Any], str] | None,
) -> set[str]:
    raw = str(identifier or "").strip()
    if not raw:
        return set()

    normalized = to_whatsapp_jid(raw)
    keys = {raw, normalized}
    keys.update(_phone_equivalents(normalized))
    if "@" in normalized:
        keys.add(normalized.split("@", 1)[0])

    if canonicalize is not None:
        canonical = str(canonicalize(normalized) or "").strip()
        if canonical:
            keys.add(canonical)
            keys.update(_phone_equivalents(canonical))
            if "@" in canonical:
                keys.add(canonical.split("@", 1)[0])

    return {key for key in keys if key}


def _phone_equivalents(jid: str) -> set[str]:
    if "@" not in jid:
        return set()
    local, domain = jid.split("@", 1)
    if domain not in PHONE_DOMAINS:
        return set()
    return {f"{local}@s.whatsapp.net", f"{local}@c.us"}
