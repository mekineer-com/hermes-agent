"""Soul-mode turn delegation — all memU integration logic for Hermes.

When an agent is configured as a soul, the Hermes tool loop is bypassed
and the turn is delegated to memU via HTTP. This module owns all soul-mode
state, config, and turn execution so run_agent.py stays minimal.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

from agent.memu_client import MemuClientError, MemuHttpClient

logger = logging.getLogger(__name__)


@dataclass
class SoulModeConfig:
    enabled: bool = False
    role: str = "standard"
    soul_id: str = ""
    user_id: str = ""
    memu_base_url: str = "http://127.0.0.1:8099"
    use_memu_turn: bool = True
    timeout_seconds: float = 90.0
    _client: MemuHttpClient | None = field(default=None, repr=False)
    _session_started: bool = field(default=False, repr=False)

    def is_active(self) -> bool:
        return (
            self.enabled
            and self.role == "soul"
            and bool(self.soul_id)
            and bool(self.user_id)
            and self.use_memu_turn
        )

    def get_client(self) -> MemuHttpClient:
        if self._client is None:
            self._client = MemuHttpClient(
                base_url=self.memu_base_url,
                timeout_seconds=self.timeout_seconds,
            )
        return self._client


def _is_truthy(val: Any, default: bool = False) -> bool:
    if val is None:
        return default
    if isinstance(val, str):
        return val.strip().lower() in {"true", "1", "yes", "on"}
    return bool(val)


def resolve_agent_config(user_config: dict | None, session_key: str) -> dict[str, Any]:
    """Resolve per-agent soul-mode config from the user's config.yaml.

    Config shape (all optional):
        soul_mode:
          agents:
            main:
              enabled: true
              role: soul
              soul_id: Echo
              user_id: marcos
              memu_base_url: http://127.0.0.1:8099
              use_memu_turn: true
              timeout_seconds: 90
    """
    default_cfg = {
        "enabled": False,
        "role": "standard",
        "soul_id": "",
        "user_id": "",
        "memu_base_url": "http://127.0.0.1:8099",
        "use_memu_turn": True,
        "timeout_seconds": 90.0,
    }
    cfg = user_config if isinstance(user_config, dict) else {}
    soul_mode = cfg.get("soul_mode")
    if not isinstance(soul_mode, dict):
        return default_cfg
    agents_cfg = soul_mode.get("agents")
    if not isinstance(agents_cfg, dict):
        return default_cfg

    agent_name = "main"
    if isinstance(session_key, str) and session_key.startswith("agent:"):
        parts = session_key.split(":")
        if len(parts) >= 2 and parts[1]:
            agent_name = parts[1]
    agent_cfg = agents_cfg.get(agent_name)
    if not isinstance(agent_cfg, dict):
        return default_cfg

    role_raw = str(agent_cfg.get("role", "") or "").strip().lower()
    role = "soul" if role_raw == "soul" else "standard"
    has_enabled_key = "enabled" in agent_cfg
    enabled = _is_truthy(agent_cfg.get("enabled")) if has_enabled_key else (role == "soul")
    if role == "standard":
        enabled = False

    out = dict(default_cfg)
    out["enabled"] = bool(enabled)
    out["role"] = role
    out["soul_id"] = str(agent_cfg.get("soul_id") or "").strip()
    out["user_id"] = str(agent_cfg.get("user_id") or "").strip()
    memu_base_url = str(agent_cfg.get("memu_base_url") or "").strip()
    if memu_base_url:
        out["memu_base_url"] = memu_base_url
    out["use_memu_turn"] = _is_truthy(agent_cfg.get("use_memu_turn"), default=True)
    try:
        out["timeout_seconds"] = float(agent_cfg.get("timeout_seconds", 90.0))
    except (TypeError, ValueError):
        out["timeout_seconds"] = 90.0
    return out


def configure(
    *,
    enabled: bool = False,
    role: str = "standard",
    soul_id: str = "",
    user_id: str = "",
    memu_base_url: str = "http://127.0.0.1:8099",
    use_memu_turn: bool = True,
    timeout_seconds: float = 90.0,
) -> SoulModeConfig:
    role_norm = str(role or "standard").strip().lower()
    try:
        timeout = float(timeout_seconds)
    except (TypeError, ValueError):
        timeout = 90.0
    return SoulModeConfig(
        enabled=bool(enabled),
        role="soul" if role_norm == "soul" else "standard",
        soul_id=str(soul_id or "").strip(),
        user_id=str(user_id or "").strip(),
        memu_base_url=str(memu_base_url or "http://127.0.0.1:8099").strip(),
        use_memu_turn=bool(use_memu_turn),
        timeout_seconds=timeout,
    )


def build_conversation_id(
    *,
    platform: str,
    chat_id: str,
    thread_id: str = "",
    chat_type: str = "",
    gateway_session_key: str = "",
    session_id: str = "",
    canonical_whatsapp_fn: Any = None,
) -> str:
    platform = str(platform or "unknown").strip().lower() or "unknown"
    chat_id = str(chat_id or "").strip()
    thread_id = str(thread_id or "").strip()
    chat_type = str(chat_type or "").strip().lower()

    if platform == "cron":
        if chat_id:
            return f"cron:{chat_id}"
        if gateway_session_key:
            return f"cron:{gateway_session_key}"
        return f"cron:{session_id}"

    if platform == "whatsapp":
        session_key = str(gateway_session_key or "").strip()
        if session_key:
            parts = session_key.split(":")
            if len(parts) >= 5 and parts[0] == "agent" and parts[1] == "main" and parts[2] == "whatsapp":
                if parts[3] == "dm" and canonical_whatsapp_fn is not None:
                    canonical = canonical_whatsapp_fn(parts[4])
                    if canonical:
                        parts[4] = canonical
                return "whatsapp:" + ":".join(parts[3:])
        if chat_id and chat_type == "dm" and canonical_whatsapp_fn is not None:
            canonical = canonical_whatsapp_fn(chat_id)
            if canonical:
                chat_id = canonical

    if chat_id:
        if thread_id:
            return f"{platform}:{chat_id}:{thread_id}"
        return f"{platform}:{chat_id}"
    if gateway_session_key:
        return str(gateway_session_key)
    return f"{platform}:{session_id}"


def coerce_message_text(user_message: Any) -> str:
    if isinstance(user_message, str):
        return user_message.strip()
    if isinstance(user_message, list):
        text_parts: list[str] = []
        image_count = 0
        for part in user_message:
            if not isinstance(part, dict):
                continue
            part_type = str(part.get("type") or "").strip().lower()
            if part_type == "text":
                text_value = part.get("text")
                if isinstance(text_value, str):
                    text_stripped = text_value.strip()
                    if text_stripped:
                        text_parts.append(text_stripped)
            elif part_type == "image_url":
                image_count += 1
        if text_parts:
            return "\n".join(text_parts)
        if image_count:
            suffix = "s" if image_count != 1 else ""
            return f"[User sent {image_count} image{suffix}]"
        return ""
    return str(user_message or "").strip()


def _load_history(
    agent: Any,
    conversation_history: List[Dict[str, Any]] | None,
    config: SoulModeConfig,
) -> list[dict[str, Any]]:
    platform = str(getattr(agent, "platform", "") or "").strip().lower()
    if platform == "whatsapp":
        return []

    db = getattr(agent, "_session_db", None)
    if db and agent.session_id:
        try:
            db_history = db.get_messages(agent.session_id)
            if isinstance(db_history, list) and db_history:
                return db_history
        except Exception:
            logger.debug("memU: failed to load SessionDB history for %s", agent.session_id, exc_info=True)
    return list(conversation_history or [])


def _emit_session_start_if_needed(agent: Any, config: SoulModeConfig, conversation_history: List[Dict[str, Any]] | None) -> None:
    if config._session_started:
        return
    if conversation_history:
        config._session_started = True
        return
    try:
        from hermes_cli.plugins import invoke_hook as _invoke_hook
        _invoke_hook(
            "on_session_start",
            session_id=agent.session_id,
            model=agent.model,
            platform=getattr(agent, "platform", None) or "",
        )
    except Exception as exc:
        logger.warning("on_session_start hook failed: %s", exc)
    config._session_started = True


def _invoke_hook_safe(hook_name: str, **kwargs) -> None:
    try:
        from hermes_cli.plugins import invoke_hook as _invoke_hook
        _invoke_hook(hook_name, **kwargs)
    except Exception as exc:
        logger.warning("%s hook failed: %s", hook_name, exc)


def _make_failed_result(agent: Any, error_msg: str, error_detail: str, messages: list) -> dict:
    return {
        "final_response": error_msg,
        "last_reasoning": None,
        "messages": messages,
        "api_calls": 0,
        "completed": False,
        "turn_exit_reason": "soul_mode_error",
        "partial": False,
        "interrupted": False,
        "response_previewed": False,
        "model": agent.model,
        "provider": agent.provider,
        "base_url": agent.base_url,
        "failed": True,
        "error": error_detail,
    }


def _route_whatsapp_notice_to_self_dm(text: str, conversation_id: str, notice_kind: str) -> bool:
    from agent.whatsapp_bridge_client import read_self_dm_jid, send_text

    try:
        self_dm = read_self_dm_jid()
    except Exception as exc:
        logger.warning(
            "Soul %s not routed (self-DM lookup failed) for %s: %s",
            notice_kind,
            conversation_id,
            exc,
        )
        return False

    if not self_dm:
        logger.warning(
            "Soul %s not routed (self_dm missing) for %s",
            notice_kind,
            conversation_id,
        )
        return False

    try:
        ok = bool(send_text(self_dm, text))
    except Exception as exc:
        logger.warning(
            "Soul %s not routed (send failed self_dm=%r) for %s: %s",
            notice_kind,
            self_dm,
            conversation_id,
            exc,
        )
        return False

    if ok:
        logger.info(
            "Soul routed %s to self-DM %s (from %s)",
            notice_kind,
            self_dm,
            conversation_id,
        )
        return True

    logger.warning(
        "Soul %s not routed (self_dm=%r); silent exit for %s",
        notice_kind,
        self_dm,
        conversation_id,
    )
    return False


def _silent_listen_result(
    agent: Any,
    config: SoulModeConfig,
    messages: list,
    conversation_history: List[Dict[str, Any]] | None,
    user_message: Any,
    task_id: str,
    summarize_for_log: Any,
    platform: str,
    *,
    exit_reason: str = "soul_mode_listen",
) -> dict:
    """Shared exit for any turn where the soul should not respond.

    Used by the natural SPEAK/LISTEN gate and by memu.json channel-policy
    branches (``excluded``, ``listen_only``).
    """
    messages.append({"role": "assistant", "content": ""})
    agent._save_trajectory(messages, summarize_for_log(user_message), True)
    agent._cleanup_task_resources(task_id)
    agent._persist_session(messages, conversation_history)
    agent.clear_interrupt()
    agent._stream_callback = None
    _invoke_hook_safe(
        "on_session_end",
        session_id=agent.session_id,
        completed=True,
        interrupted=False,
        model=agent.model,
        platform=platform,
    )
    return {
        "final_response": "",
        "last_reasoning": None,
        "messages": messages,
        "api_calls": 0,
        "completed": True,
        "turn_exit_reason": exit_reason,
        "partial": False,
        "interrupted": False,
        "response_previewed": False,
        "model": agent.model,
        "provider": agent.provider,
        "base_url": agent.base_url,
    }


def handle_turn(
    agent: Any,
    config: SoulModeConfig,
    *,
    user_message: Any,
    conversation_history: List[Dict[str, Any]] | None,
    messages: list,
    task_id: str,
    original_user_message: Any,
    summarize_for_log: Any,
) -> dict:
    """Execute a soul-mode turn. Returns the complete run_conversation result dict."""

    _emit_session_start_if_needed(agent, config, conversation_history)

    try:
        client = config.get_client()

        from gateway.whatsapp_seam import canonical_whatsapp_jid
        conversation_id = build_conversation_id(
            platform=str(getattr(agent, "platform", "") or ""),
            chat_id=str(getattr(agent, "_chat_id", "") or ""),
            thread_id=str(getattr(agent, "_thread_id", "") or ""),
            chat_type=str(getattr(agent, "_chat_type", "") or ""),
            gateway_session_key=str(getattr(agent, "_gateway_session_key", "") or ""),
            session_id=str(getattr(agent, "session_id", "") or ""),
            canonical_whatsapp_fn=canonical_whatsapp_jid,
        )

        history = _load_history(agent, conversation_history, config)
        memu_message = coerce_message_text(original_user_message)
        if not memu_message:
            raise MemuClientError("memU turn requires non-empty user message")

        platform = str(getattr(agent, "platform", "") or "").strip().lower()
        chat_type = str(getattr(agent, "_chat_type", "") or "").strip().lower()
        chat_name = str(getattr(agent, "_chat_name", "") or "").strip()
        channel_mode = "group" if (platform == "whatsapp" and chat_type != "dm") else "direct"
        sender_display_name = str(getattr(agent, "_user_name", "") or "")
        ext_msg_id = str(getattr(agent, "_external_message_id", "") or "").strip() or None
        memorize_chat: bool | None = None
        allow_public_response = True

        # WhatsApp channel policy (memu.json): "excluded" → drop silently.
        # "listen_only" → run the soul turn, but forbid public replies.
        if platform == "whatsapp":
            from gateway.memu_policy import whatsapp_channel_settings
            policy, memorize_chat = whatsapp_channel_settings(str(getattr(agent, "_chat_id", "") or ""))
            if policy == "excluded":
                logger.info("Soul excluded for %s (memu.json policy)", conversation_id)
                return _silent_listen_result(
                    agent, config, messages, conversation_history, user_message,
                    task_id, summarize_for_log, platform, exit_reason="soul_mode_excluded",
                )
            if policy == "listen_only":
                logger.info("Soul listen_only for %s (memu.json policy)", conversation_id)
                allow_public_response = False

        turn_out = client.memu_turn(
            conversation_id=conversation_id,
            user_id=config.user_id,
            soul_id=config.soul_id,
            message=memu_message,
            history=history,
            history_user_name=sender_display_name,
            user_name=sender_display_name,
            debug=False,
            channel_mode=channel_mode,
            chat_name=chat_name,
            chat_type=chat_type,
            memorize_chat=memorize_chat,
            external_message_id=ext_msg_id,
            allow_public_response=allow_public_response,
        )

        turn_ok = turn_out.get("ok", True)
        if isinstance(turn_ok, str):
            turn_ok = turn_ok.strip().lower() not in {"false", "0", "no", "off"}
        if not bool(turn_ok):
            raise MemuClientError(
                "memU turn returned ok=false",
                response_body=json.dumps(turn_out, default=str),
            )

        if not turn_out.get("should_respond", True):
            logger.info("Soul chose LISTEN for %s (channel_mode=%s)", conversation_id, channel_mode)
            return _silent_listen_result(
                agent, config, messages, conversation_history, user_message,
                task_id, summarize_for_log, platform,
            )

        response_target = str(turn_out.get("response_target") or "respond").strip().lower()
        response_text = str(turn_out.get("response") or "").strip()

        # Post-turn LISTEN/OBSERVE: the soul ran the turn and chose silence.
        if response_target in {"listen", "observe"}:
            logger.info("Soul chose response_target=%s for %s", response_target, conversation_id)
            return _silent_listen_result(
                agent, config, messages, conversation_history, user_message,
                task_id, summarize_for_log, platform,
            )

        # PRIVATE on WhatsApp: route to the human's self-DM (their own
        # number's chat-with-self) instead of the originating chat. Other
        # platforms have no self-DM concept and fall through to the default
        # respond path below.
        if response_target == "private" and platform == "whatsapp" and response_text:
            _route_whatsapp_notice_to_self_dm(
                response_text,
                conversation_id,
                "PRIVATE reply",
            )
            return _silent_listen_result(
                agent, config, messages, conversation_history, user_message,
                task_id, summarize_for_log, platform,
            )

        final_response = response_text
        if not final_response:
            raise MemuClientError(
                "memU turn returned empty response",
                response_body=json.dumps(turn_out, default=str),
            )

    except MemuClientError as exc:
        logger.error(
            "Soul-mode memU turn failed: session=%s status=%s error=%s",
            agent.session_id or "none",
            getattr(exc, "status_code", None),
            exc,
        )
        agent._save_trajectory(messages, summarize_for_log(user_message), False)
        agent._cleanup_task_resources(task_id)
        agent._persist_session(messages, conversation_history)
        agent.clear_interrupt()
        _invoke_hook_safe("on_session_end", session_id=agent.session_id, completed=False, interrupted=False, model=agent.model, platform=getattr(agent, "platform", None) or "")
        agent._stream_callback = None
        error_msg = (
            f"memU turn failed: {exc}"
            if getattr(exc, "status_code", None) is None
            else f"memU turn failed (HTTP {exc.status_code}): {exc}"
        )
        if platform == "whatsapp":
            _route_whatsapp_notice_to_self_dm(
                error_msg,
                conversation_id,
                "memU failure notice",
            )
            return _silent_listen_result(
                agent, config, messages, conversation_history, user_message,
                task_id, summarize_for_log, platform, exit_reason="soul_mode_memu_error_private_notice",
            )
        return _make_failed_result(agent, error_msg, str(exc), messages)

    except Exception as exc:
        logger.exception("Soul-mode memU turn crashed for session=%s", agent.session_id or "none")
        agent._save_trajectory(messages, summarize_for_log(user_message), False)
        agent._cleanup_task_resources(task_id)
        agent._persist_session(messages, conversation_history)
        agent.clear_interrupt()
        _invoke_hook_safe("on_session_end", session_id=agent.session_id, completed=False, interrupted=False, model=agent.model, platform=getattr(agent, "platform", None) or "")
        agent._stream_callback = None
        error_msg = f"memU turn failed: {type(exc).__name__}: {exc}"
        if str(getattr(agent, "platform", "") or "").strip().lower() == "whatsapp":
            conv_id = (
                locals().get("conversation_id")
                or str(getattr(agent, "_gateway_session_key", "") or "")
                or str(getattr(agent, "session_id", "") or "unknown")
            )
            _route_whatsapp_notice_to_self_dm(
                error_msg,
                str(conv_id),
                "memU failure notice",
            )
            return _silent_listen_result(
                agent, config, messages, conversation_history, user_message,
                task_id, summarize_for_log, "whatsapp", exit_reason="soul_mode_memu_error_private_notice",
            )
        return _make_failed_result(agent, error_msg, f"{type(exc).__name__}: {exc}", messages)

    messages.append({"role": "assistant", "content": final_response})
    agent._save_trajectory(messages, summarize_for_log(user_message), True)
    agent._cleanup_task_resources(task_id)
    agent._persist_session(messages, conversation_history)
    agent.clear_interrupt()
    agent._stream_callback = None

    _invoke_hook_safe("post_llm_call", session_id=agent.session_id, user_message=original_user_message, assistant_response=final_response, conversation_history=list(messages), model=agent.model, platform=getattr(agent, "platform", None) or "")
    _invoke_hook_safe("on_session_end", session_id=agent.session_id, completed=True, interrupted=False, model=agent.model, platform=getattr(agent, "platform", None) or "")

    return {
        "final_response": final_response,
        "last_reasoning": None,
        "messages": messages,
        "api_calls": 0,
        "completed": True,
        "turn_exit_reason": "soul_mode_memu_turn",
        "partial": False,
        "interrupted": False,
        "response_previewed": False,
        "model": agent.model,
        "provider": agent.provider,
        "base_url": agent.base_url,
        "input_tokens": agent.session_input_tokens,
        "output_tokens": agent.session_output_tokens,
        "cache_read_tokens": getattr(agent, "session_cache_read_tokens", 0),
    }
