"""OpenAlma-owned WhatsApp identity seams.

Upstream Hermes keeps ``canonical_whatsapp_identifier`` phone-preferred and
domain-stripping. OpenAlma needs durable, domain-preserving conversation IDs,
with LID preferred when the bridge has evidence that a phone and LID are the
same contact.
"""

from __future__ import annotations

import json
import logging
import re
from collections import defaultdict, deque
from pathlib import Path

from hermes_constants import get_hermes_home

from .whatsapp_identity import to_whatsapp_jid

logger = logging.getLogger(__name__)

_LID_MAPPING_RE = re.compile(r"^lid-mapping-(.+?)(?:_reverse)?\.json$")
_PHONE_DOMAINS = {"s.whatsapp.net", "c.us"}


def chat_id_from_whatsapp_conversation_id(conversation_id: str) -> str:
    raw = str(conversation_id or "").strip()
    for prefix in ("whatsapp:dm:", "whatsapp:group:"):
        if raw.startswith(prefix):
            return raw[len(prefix) :].strip()
    return ""


def canonical_whatsapp_jid(identifier: str) -> str:
    jid = _normalize_jid(identifier)
    if not jid:
        return ""
    if jid.endswith("@g.us") or jid == "status@broadcast":
        return jid

    aliases = _expand_jid_aliases(jid)
    return _preferred_jid(aliases, fallback=jid)


def _normalize_jid(value: str) -> str:
    raw = str(value or "").strip()
    if not raw:
        return ""
    if "@" in raw:
        prefix, _, domain = raw.partition("@")
        local = prefix.replace("+", "", 1).split(":", 1)[0].strip()
        domain = domain.strip()
        if not local or not domain:
            return raw
        return f"{local}@{domain}"
    return to_whatsapp_jid(raw)


def _local_id(jid: str) -> str:
    return str(jid or "").split("@", 1)[0]


def _domain(jid: str) -> str:
    return str(jid or "").split("@", 1)[1] if "@" in str(jid or "") else ""


def _phone_equivalents(jid: str) -> set[str]:
    local = _local_id(jid)
    domain = _domain(jid)
    if not local or domain not in _PHONE_DOMAINS:
        return set()
    return {f"{local}@s.whatsapp.net", f"{local}@c.us"}


def _ensure_lid_jid(value: str) -> str:
    local = str(value or "").strip().split("@", 1)[0].split(":", 1)[0]
    return f"{local}@lid" if local else ""


def _mapping_pair(raw_key: str, raw_mapped: object, *, reverse: bool) -> tuple[set[str], str] | None:
    key = str(raw_key or "").strip()
    if not key:
        return None
    mapped = str(raw_mapped or "").strip()
    if not mapped:
        return None
    if "@" in key:
        normalized = _normalize_jid(key)
        key_candidates = {normalized} | _phone_equivalents(normalized)
    elif reverse:
        key_candidates = {f"{key}@lid"}
    elif mapped.endswith("@s.whatsapp.net") or mapped.endswith("@c.us"):
        key_candidates = {f"{key}@lid"}
    else:
        key_candidates = {f"{key}@s.whatsapp.net", f"{key}@c.us"}

    if reverse:
        mapped_jid = to_whatsapp_jid(mapped)
    elif mapped.endswith("@s.whatsapp.net") or mapped.endswith("@c.us"):
        mapped_jid = _normalize_jid(mapped)
    else:
        mapped_jid = _ensure_lid_jid(mapped)
    if not mapped_jid:
        return None
    return key_candidates, mapped_jid


def _add_bidirectional_edge(graph: dict[str, set[str]], left: str, right: str) -> None:
    if not left or not right or left == right:
        return
    graph[left].add(right)
    graph[right].add(left)


def _load_alias_graph(session_dir: Path) -> dict[str, set[str]]:
    graph: dict[str, set[str]] = defaultdict(set)
    if not session_dir.exists():
        return graph

    for path in session_dir.glob("lid-mapping-*.json"):
        match = _LID_MAPPING_RE.match(path.name)
        if not match:
            continue
        try:
            mapped = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            logger.debug("whatsapp_seam: failed to read %s: %s", path, exc)
            continue
        pair = _mapping_pair(match.group(1), mapped, reverse=path.name.endswith("_reverse.json"))
        if pair is None:
            continue
        key_jids, mapped_jid = pair
        for key_jid in key_jids:
            _add_bidirectional_edge(graph, key_jid, mapped_jid)
            for equivalent in _phone_equivalents(key_jid):
                _add_bidirectional_edge(graph, key_jid, equivalent)
            for equivalent in _phone_equivalents(mapped_jid):
                _add_bidirectional_edge(graph, mapped_jid, equivalent)

    creds_path = session_dir / "creds.json"
    try:
        parsed = json.loads(creds_path.read_text(encoding="utf-8"))
    except (FileNotFoundError, OSError, json.JSONDecodeError):
        parsed = None
    me = parsed.get("me") if isinstance(parsed, dict) else None
    if isinstance(me, dict):
        phone_jid = _normalize_jid(me.get("id"))
        lid_jid = _normalize_jid(me.get("lid"))
        _add_bidirectional_edge(graph, phone_jid, lid_jid)

    return graph


def _expand_jid_aliases(jid: str) -> set[str]:
    graph = _load_alias_graph(get_hermes_home() / "whatsapp" / "session")
    aliases = set(_phone_equivalents(jid)) or {jid}
    aliases.add(jid)
    queue = deque(aliases)

    while queue:
        current = queue.popleft()
        for neighbor in graph.get(current, set()):
            if neighbor not in aliases:
                aliases.add(neighbor)
                queue.append(neighbor)
    return aliases


def _preferred_jid(aliases: set[str], *, fallback: str) -> str:
    lids = sorted(alias for alias in aliases if alias.endswith("@lid"))
    if lids:
        return lids[0]

    phones = sorted(alias for alias in aliases if alias.endswith("@s.whatsapp.net"))
    if phones:
        return phones[0]

    c_us = sorted(alias for alias in aliases if alias.endswith("@c.us"))
    if c_us:
        return c_us[0]

    return fallback
