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


def _jid_type(jid: str) -> str:
    """Return 'lid', 'phone', or 'unknown' for a JID or bare id."""
    if jid.endswith("@lid"):
        return "lid"
    if jid.endswith("@s.whatsapp.net") or jid.endswith("@c.us"):
        return "phone"
    return "unknown"


def _to_full_jid(bare: str, id_type: str, input_domain: str) -> str:
    """Reconstruct a full JID from a bare id and its known type.

    - 'lid'   → ``<bare>@lid``
    - 'phone' → ``<bare>@s.whatsapp.net`` (unless input was @c.us, preserve that)
    - 'unknown' → preserve the original input domain if available, else bare
    """
    if id_type == "lid":
        return f"{bare}@lid"
    if id_type == "phone":
        # Preserve @c.us when the input itself was @c.us and this is the same bare id.
        if input_domain == "@c.us":
            return f"{bare}@c.us"
        return f"{bare}@s.whatsapp.net"
    # unknown: if we have the original domain, use it; otherwise return bare.
    if input_domain:
        return f"{bare}{input_domain}"
    return bare


def _resolve(jid: str, session_dir: Path) -> str:
    """Core resolver: returns LID JID if known, else phone JID, else input.

    Identity type is determined from the *filename scheme*, not the file content,
    because the bridge writes bare (domain-less) values:
    - ``lid-mapping-{phone}.json``         → current is PHONE, mapped value is LID
    - ``lid-mapping-{lid}_reverse.json``   → current is LID,   mapped value is PHONE
    """
    norm = _normalize_jid(jid)
    bare = _bare(norm)

    if not bare:
        return jid

    # Domain of the original input (e.g. "@s.whatsapp.net"), used for @c.us preservation.
    _, _, input_domain_part = norm.partition("@")
    input_domain = f"@{input_domain_part}" if input_domain_part else ""

    # bare → full JID (first seen wins per bare).  Seed with the typed input.
    input_type = _jid_type(norm)
    all_jids: dict[str, str] = {bare: _to_full_jid(bare, input_type, input_domain)}

    seen: set[str] = set()
    queue: list[str] = [bare]

    while queue:
        current = queue.pop(0)
        if current in seen:
            continue
        seen.add(current)

        # current's own type comes from what we already know about it.
        current_full = all_jids.get(current, current)
        current_type = _jid_type(current_full)

        # Forward file: lid-mapping-{current}.json → current is PHONE, content is LID.
        fwd_path = session_dir / f"lid-mapping-{current}.json"
        if fwd_path.exists():
            raw = _read_mapping(fwd_path)
            if raw is not None:
                mapped_bare = _bare(raw)
                # File scheme says: forward file → content is a LID.
                mapped_type = "lid"
                if mapped_bare and mapped_bare not in all_jids:
                    all_jids[mapped_bare] = _to_full_jid(mapped_bare, mapped_type, input_domain)
                if mapped_bare and mapped_bare not in seen:
                    queue.append(mapped_bare)
                # Also refine current's type: forward file implies current is a phone.
                if current_type == "unknown" and current not in all_jids:
                    all_jids[current] = _to_full_jid(current, "phone", input_domain)

        # Reverse file: lid-mapping-{current}_reverse.json → current is LID, content is PHONE.
        rev_path = session_dir / f"lid-mapping-{current}_reverse.json"
        if rev_path.exists():
            raw = _read_mapping(rev_path)
            if raw is not None:
                mapped_bare = _bare(raw)
                # File scheme says: reverse file → content is a PHONE.
                mapped_type = "phone"
                if mapped_bare and mapped_bare not in all_jids:
                    all_jids[mapped_bare] = _to_full_jid(mapped_bare, mapped_type, input_domain)
                if mapped_bare and mapped_bare not in seen:
                    queue.append(mapped_bare)
                # Also refine current's type: reverse file implies current is a LID.
                if current_type == "unknown":
                    existing = all_jids.get(current)
                    if existing is None or _jid_type(existing) == "unknown":
                        all_jids[current] = _to_full_jid(current, "lid", input_domain)

    # Prefer LID; fall back to phone (@s.whatsapp.net or @c.us); else norm.
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
