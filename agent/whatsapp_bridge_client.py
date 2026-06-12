"""Thin sync client for the WhatsApp bridge HTTP API.

Used by soul_mode when the soul's turn contract says
``response_target == "private"``: the bridge's connected account is also
the human's account (Marcos sends and the soul sends from the same
number), so PRIVATE means routing the soul's reply to the human's
self-DM (their own number chatting to itself) instead of the chat the
turn came from. WhatsApp surfaces a person under either a phone JID
(``15133278228@s.whatsapp.net``) or a privacy LID (``114628432556258@lid``);
the bridge records both in ``creds.json``'s ``me`` block and the phone
JID is what ``/send`` expects as ``chatId`` for a self-DM.

This module sits alongside ``memu_client.py`` as one of our added files;
it does not import or modify upstream ``gateway/platforms/whatsapp.py``.
"""
from __future__ import annotations

import json
import logging
import urllib.error
import urllib.request

from hermes_constants import get_hermes_home

logger = logging.getLogger(__name__)

_DEFAULT_BRIDGE_PORT = 3000


def _bridge_port() -> int:
    config_path = get_hermes_home() / "config.yaml"
    try:
        import yaml
    except ImportError:
        return _DEFAULT_BRIDGE_PORT
    try:
        data = yaml.safe_load(config_path.read_text(encoding="utf-8"))
    except (OSError, yaml.YAMLError):
        return _DEFAULT_BRIDGE_PORT
    whatsapp = data.get("whatsapp") if isinstance(data, dict) else None
    port = whatsapp.get("bridge_port") if isinstance(whatsapp, dict) else None
    if isinstance(port, int) and port > 0:
        return port
    return _DEFAULT_BRIDGE_PORT


def read_self_dm_jid() -> str:
    """Return the bridge account's phone JID for self-DM routing, or "".

    The bridge writes its connected-account identity to
    ``~/.hermes/whatsapp/session/creds.json`` under ``me.id`` in Baileys'
    ``<phone>:<device>@s.whatsapp.net`` format. The bridge's ``/send``
    endpoint accepts the same string with the device suffix stripped,
    which is the chatId form the user's self-DM lives under.
    """
    creds_path = get_hermes_home() / "whatsapp" / "session" / "creds.json"
    try:
        data = json.loads(creds_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return ""
    me = data.get("me") if isinstance(data, dict) else None
    if not isinstance(me, dict):
        return ""
    raw = str(me.get("id") or "").strip()
    if not raw:
        return ""
    # Baileys: "15133278228:10@s.whatsapp.net" → strip the device suffix.
    head, sep, tail = raw.partition(":")
    if sep and "@" in tail:
        suffix = tail.split("@", 1)[1]
        return f"{head}@{suffix}"
    return raw


def send_text(chat_id: str, text: str, *, timeout: float = 10.0) -> bool:
    """POST ``{chatId, message}`` to the bridge's ``/send`` endpoint.

    Returns True on HTTP 200 from the bridge. Logs and returns False on
    any failure — the caller is expected to silently log and continue,
    so a bridge outage never raises into the agent loop.
    """
    chat_id_clean = str(chat_id or "").strip()
    text_clean = str(text or "").strip()
    if not chat_id_clean or not text_clean:
        return False
    payload = json.dumps({"chatId": chat_id_clean, "message": text_clean}).encode("utf-8")
    url = f"http://127.0.0.1:{_bridge_port()}/send"
    # The bridge validates the Host header against loopback aliases —
    # urllib's default Host comes from the URL (127.0.0.1) which is accepted.
    req = urllib.request.Request(
        url,
        data=payload,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.status == 200
    except (urllib.error.URLError, OSError) as exc:
        logger.warning("whatsapp_bridge_client: send_text failed for %s: %s", chat_id_clean, exc)
        return False
