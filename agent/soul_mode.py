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
    timeout_seconds: float = 45.0
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


def configure(
    *,
    enabled: bool = False,
    role: str = "standard",
    soul_id: str = "",
    user_id: str = "",
    memu_base_url: str = "http://127.0.0.1:8099",
    use_memu_turn: bool = True,
    timeout_seconds: float = 45.0,
) -> SoulModeConfig:
    role_norm = str(role or "standard").strip().lower()
    try:
        timeout = float(timeout_seconds)
    except (TypeError, ValueError):
        timeout = 45.0
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


def _load_history(agent: Any, conversation_history: List[Dict[str, Any]] | None) -> list[dict[str, Any]]:
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

        from gateway.whatsapp_identity import canonical_whatsapp_identifier
        conversation_id = build_conversation_id(
            platform=str(getattr(agent, "platform", "") or ""),
            chat_id=str(getattr(agent, "_chat_id", "") or ""),
            thread_id=str(getattr(agent, "_thread_id", "") or ""),
            chat_type=str(getattr(agent, "_chat_type", "") or ""),
            gateway_session_key=str(getattr(agent, "_gateway_session_key", "") or ""),
            session_id=str(getattr(agent, "session_id", "") or ""),
            canonical_whatsapp_fn=canonical_whatsapp_identifier,
        )

        history = _load_history(agent, conversation_history)
        memu_message = coerce_message_text(user_message)
        if not memu_message:
            raise MemuClientError("memU turn requires non-empty user message")

        platform = str(getattr(agent, "platform", "") or "").strip().lower()
        chat_type = str(getattr(agent, "_chat_type", "") or "").strip().lower()
        channel_mode = "group" if (platform == "whatsapp" and chat_type != "dm") else "direct"

        turn_out = client.memu_turn(
            conversation_id=conversation_id,
            user_id=config.user_id,
            soul_id=config.soul_id,
            message=memu_message,
            history=history,
            history_user_name=str(getattr(agent, "_user_name", "") or ""),
            run_apimw=True,
            apply_turn_maintenance=True,
            debug=False,
            channel_mode=channel_mode,
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
            messages.append({"role": "assistant", "content": ""})
            agent._save_trajectory(messages, summarize_for_log(user_message), True)
            agent._cleanup_task_resources(task_id)
            agent._persist_session(messages, conversation_history)
            agent.clear_interrupt()
            agent._stream_callback = None
            _invoke_hook_safe("on_session_end", session_id=agent.session_id, completed=True, interrupted=False, model=agent.model, platform=platform)
            return {
                "final_response": "",
                "last_reasoning": None,
                "messages": messages,
                "api_calls": 0,
                "completed": True,
                "turn_exit_reason": "soul_mode_listen",
                "partial": False,
                "interrupted": False,
                "response_previewed": False,
                "model": agent.model,
                "provider": agent.provider,
                "base_url": agent.base_url,
            }

        final_response = str(turn_out.get("response") or "").strip()
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
