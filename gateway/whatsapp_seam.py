"""Fork-only WhatsApp identity seam.

This module owns the emit/session path for our fork. It is intentionally
separate from ``gateway/whatsapp_identity.py`` so upstream changes to that
file can be merged without touching our resolver.

Exports:
- :func:`resolve_whatsapp_jid` — domain-preserving, LID-preferred resolver
  used by ``session.py`` and ``soul_mode.py``.
- :func:`chat_id_from_whatsapp_conversation_id` — parse a ``whatsapp:dm:<jid>``
  or ``whatsapp:group:<jid>`` conversation ID back to the bare JID (moved from
  ``gateway/run.py``).
"""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)

from hermes_constants import get_hermes_home


def resolve_whatsapp_jid(jid: str) -> str:
    """Return a stable, domain-preserving WhatsApp JID for *jid*.

    Resolution order:
    1. Keep the ``@domain`` suffix — never strip it.
    2. Expand aliases via ``lid-mapping-*.json`` files in the session dir.
    3. Return the LID JID (``<n>@lid``) when one is known; else the phone JID
       (``<n>@s.whatsapp.net``).  Groups pass through unchanged.

    If *jid* is empty or no mapping data exists, the normalised input JID is
    returned as-is (fail-loud: callers see exactly what they passed in).
    """
    jid = str(jid or "").strip()
    if not jid:
        return ""

    # Groups are not identity-merged — return as-is.
    if jid.endswith("@g.us"):
        return jid

    session_dir = get_hermes_home() / "whatsapp" / "session"
    return _resolve(jid, session_dir)


def _normalize_jid(raw: str) -> str:
    """Strip device suffix and leading '+', keep the @domain.

    ``15551234567:47@s.whatsapp.net`` → ``15551234567@s.whatsapp.net``
    ``+15551234567@s.whatsapp.net``  → ``15551234567@s.whatsapp.net``
    ``15551234567``                   → ``15551234567``  (bare, no domain)
    """
    raw = str(raw or "").strip().lstrip("+")
    local, _, domain = raw.partition("@")
    local = local.split(":")[0]  # strip :device
    if domain:
        return f"{local}@{domain}"
    return local


def _bare(jid: str) -> str:
    """Return the numeric local part of a JID (no domain, no device)."""
    return jid.split("@")[0].split(":")[0].lstrip("+")


def _read_mapping(path: Path) -> Optional[str]:
    """Read a ``lid-mapping-*.json`` file and return the mapped raw value, or None."""
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
        if isinstance(value, str):
            return value
    except (OSError, json.JSONDecodeError) as exc:
        logger.debug("whatsapp_seam: failed to read %s: %s", path, exc)
    return None


def _resolve(jid: str, session_dir: Path) -> str:
    """Core resolver: returns LID JID if known, else phone JID, else input."""
    norm = _normalize_jid(jid)
    bare = _bare(norm)

    if not bare:
        return jid

    # Collect all bare aliases reachable from this JID.
    seen: set[str] = set()
    queue: list[str] = [bare]
    all_jids: dict[str, str] = {}  # bare → full JID (first seen wins per bare)

    # Record the input JID itself.
    all_jids[bare] = norm

    while queue:
        current = queue.pop(0)
        if current in seen:
            continue
        seen.add(current)

        for suffix in ("", "_reverse"):
            mapping_path = session_dir / f"lid-mapping-{current}{suffix}.json"
            if not mapping_path.exists():
                continue
            raw = _read_mapping(mapping_path)
            if raw is None:
                continue
            mapped_norm = _normalize_jid(raw)
            mapped_bare = _bare(mapped_norm)
            if mapped_bare and mapped_bare not in all_jids:
                all_jids[mapped_bare] = mapped_norm
            if mapped_bare and mapped_bare not in seen:
                queue.append(mapped_bare)

    # Prefer LID; fall back to phone (@s.whatsapp.net or @c.us); else bare.
    lid_jid: Optional[str] = None
    phone_jid: Optional[str] = None
    for full in all_jids.values():
        if full.endswith("@lid") and lid_jid is None:
            lid_jid = full
        elif (full.endswith("@s.whatsapp.net") or full.endswith("@c.us")) and phone_jid is None:
            phone_jid = full

    return lid_jid or phone_jid or norm


def chat_id_from_whatsapp_conversation_id(conversation_id: str) -> str:
    """Parse a ``whatsapp:dm:<jid>`` or ``whatsapp:group:<jid>`` string.

    Returns the bare JID portion (everything after the prefix), or ``""``
    if the string does not match either prefix.

    Moved from ``gateway/run.py`` (was ``_chat_id_from_whatsapp_conversation_id``).
    """
    raw = str(conversation_id or "").strip()
    for prefix in ("whatsapp:dm:", "whatsapp:group:"):
        if raw.startswith(prefix):
            return raw[len(prefix):].strip()
    return ""
