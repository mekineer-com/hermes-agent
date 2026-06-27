# OpenAlma Hermes v2026.6.19 Port Inventory

Base: upstream release `v2026.6.19^{}` (`2bd1977d8`).
Source: local `main` fork delta from merge-base `32c3f06a5`.

Statuses: `copy`, `hunk-port`, `take-release`, `waive`, `pending`.

## Slice 1 Copy

Fork-owned modules/scripts copied from `main` in Slice 1:

- `INDEX.md`
- `agent/memu_client.py`
- `agent/soul_mode.py`
- `agent/whatsapp_bridge_client.py`
- `gateway/channel_directory.py`
- `gateway/contact_store.py`
- `gateway/memu_policy.py`
- `gateway/platforms/whatsapp_wal.py`
- `gateway/whatsapp_identity.py`
- `gateway/whatsapp_known_contacts.py`
- `gateway/whatsapp_seam.py`
- `scripts/backfill_whatsapp_identity_jids.py`
- `scripts/whatsapp-bridge/`
- `scripts/whatsapp-web-source/`
- direct tests for the copied helper modules/scripts

## Remaining Decisions

Shared files stay `pending` until their slice ports the narrow OpenAlma seam.

- `.github/workflows/deploy-site.yml`: pending
- `.gitignore`: hunk-port
- `INDEX.md`: copy
- `acp_registry/agent.json`: pending
- `agent/memu_client.py`: copy
- `agent/soul_mode.py`: copy
- `agent/whatsapp_bridge_client.py`: copy
- `gateway/channel_directory.py`: copy
- `gateway/config.py`: hunk-port
- `gateway/contact_store.py`: copy
- `gateway/memu_policy.py`: copy
- `gateway/mirror.py`: pending
- `gateway/platforms/base.py`: pending
- `gateway/platforms/whatsapp.py`: pending
- `gateway/platforms/whatsapp_wal.py`: copy
- `gateway/run.py`: pending
- `gateway/session.py`: pending
- `gateway/status.py`: pending
- `gateway/whatsapp_identity.py`: copy
- `gateway/whatsapp_known_contacts.py`: copy
- `gateway/whatsapp_seam.py`: copy
- `hermes_cli/gateway.py`: pending
- `hermes_cli/main.py`: pending
- `hermes_state.py`: hunk-port
- `run_agent.py`: pending
- `scripts/backfill_whatsapp_identity_jids.py`: copy
- `scripts/whatsapp-bridge/allowlist.js`: copy
- `scripts/whatsapp-bridge/bridge.js`: copy
- `scripts/whatsapp-bridge/bridge_contract.test.mjs`: copy
- `scripts/whatsapp-bridge/bridge_fs.js`: copy
- `scripts/whatsapp-bridge/durable_queue.js`: copy
- `scripts/whatsapp-bridge/durable_queue.test.mjs`: copy
- `scripts/whatsapp-bridge/history_ingest.js`: copy
- `scripts/whatsapp-bridge/history_ingest.test.mjs`: copy
- `scripts/whatsapp-bridge/known_state.js`: copy
- `scripts/whatsapp-bridge/known_state.test.mjs`: copy
- `scripts/whatsapp-bridge/lid_identity.js`: copy
- `scripts/whatsapp-bridge/lid_identity.test.mjs`: copy
- `scripts/whatsapp-bridge/media_retry_cache.js`: copy
- `scripts/whatsapp-bridge/media_retry_cache.test.mjs`: copy
- `scripts/whatsapp-bridge/message_ingest.js`: copy
- `scripts/whatsapp-bridge/message_ingest.test.mjs`: copy
- `scripts/whatsapp-bridge/package-lock.json`: copy
- `scripts/whatsapp-bridge/package.json`: copy
- `scripts/whatsapp-bridge/presence_unread.js`: copy
- `scripts/whatsapp-bridge/presence_unread.test.mjs`: copy
- `scripts/whatsapp-bridge/sent_message_store.js`: copy
- `scripts/whatsapp-bridge/sent_message_store.test.mjs`: copy
- `scripts/whatsapp-bridge/socket_lifecycle.js`: copy
- `scripts/whatsapp-bridge/socket_lifecycle.test.mjs`: copy
- `scripts/whatsapp-web-source/README.md`: copy
- `scripts/whatsapp-web-source/backfill-manager.js`: copy
- `scripts/whatsapp-web-source/backfill-manager.test.mjs`: copy
- `scripts/whatsapp-web-source/browser-options.js`: copy
- `scripts/whatsapp-web-source/browser-options.test.mjs`: copy
- `scripts/whatsapp-web-source/contact-manager.js`: copy
- `scripts/whatsapp-web-source/contact-manager.test.mjs`: copy
- `scripts/whatsapp-web-source/daemon-utils.js`: copy
- `scripts/whatsapp-web-source/memory-diagnostics.js`: copy
- `scripts/whatsapp-web-source/memory-diagnostics.test.mjs`: copy
- `scripts/whatsapp-web-source/normalization.js`: copy
- `scripts/whatsapp-web-source/normalization.test.mjs`: copy
- `scripts/whatsapp-web-source/package-lock.json`: copy
- `scripts/whatsapp-web-source/package.json`: copy
- `scripts/whatsapp-web-source/page-hooks.js`: copy
- `scripts/whatsapp-web-source/page-hooks.test.mjs`: copy
- `scripts/whatsapp-web-source/source-daemon.js`: copy
- `scripts/whatsapp-web-source/source-daemon.test.mjs`: copy
- `scripts/whatsapp-web-source/status-writer.js`: copy
- `scripts/whatsapp-web-source/status-writer.test.mjs`: copy
- `scripts/whatsapp-web-source/store-writer.js`: copy
- `scripts/whatsapp-web-source/store-writer.test.mjs`: copy
- `scripts/whatsapp-web-source/store.py`: copy
- `scripts/whatsapp-web-source/test_store.py`: copy
- `tests/agent/test_memu_client.py`: copy
- `tests/gateway/restart_test_helpers.py`: pending
- `tests/gateway/test_agent_cache.py`: pending
- `tests/gateway/test_channel_directory.py`: copy
- `tests/gateway/test_internal_event_bypass_pairing.py`: pending
- `tests/gateway/test_memu_policy.py`: copy
- `tests/gateway/test_notice_delivery.py`: pending
- `tests/gateway/test_pending_drain_race.py`: pending
- `tests/gateway/test_restart_drain.py`: pending
- `tests/gateway/test_restart_resume_pending.py`: pending
- `tests/gateway/test_run_cleanup_progress.py`: pending
- `tests/gateway/test_run_progress_topics.py`: pending
- `tests/gateway/test_run_regressions.py`: pending
- `tests/gateway/test_session.py`: pending
- `tests/gateway/test_session_race_guard.py`: pending
- `tests/gateway/test_soul_mode_config.py`: pending
- `tests/gateway/test_status.py`: pending
- `tests/gateway/test_whatsapp_connect.py`: pending
- `tests/gateway/test_whatsapp_contact_store.py`: copy
- `tests/gateway/test_whatsapp_formatting.py`: pending
- `tests/gateway/test_whatsapp_group_gating.py`: pending
- `tests/gateway/test_whatsapp_history_persist.py`: pending
- `tests/gateway/test_whatsapp_known_contacts.py`: copy
- `tests/gateway/test_whatsapp_reply_prefix.py`: pending
- `tests/gateway/test_whatsapp_seam.py`: copy
- `tests/gateway/test_whatsapp_wal.py`: copy
- `tests/gateway/test_whatsapp_web_source_runtime.py`: pending
- `tests/hermes_cli/test_gateway_service.py`: pending
- `tests/run_agent/test_soul_mode.py`: pending
- `tests/scripts/test_backfill_whatsapp_identity_jids.py`: copy
- `tests/test_hermes_state.py`: hunk-port
- `tools/xai_http.py`: pending

## Additional Release Fixes During Port

- `gateway/platforms/whatsapp_common.py`: hunk-port — fixed release JID suffix normalization and DM allowlist matching used by OpenAlma WhatsApp.
- `tests/gateway/test_whatsapp_common_openalma.py`: copy — focused regression tests for the shared WhatsApp mixin change.

## Partial WhatsApp Adapter Port

- `gateway/platforms/whatsapp.py`: pending — bridge pidfile cleanup, config-backed mode, outbound JID normalization, self-DM private notice, chunked-send result IDs, chat-name fallback, gateway WAL, deliveryMode dispatch, contact-store updates, web-source supervisor/runtime-status, and not-paired setup path hunk-ported; setup mode now owns the session lock and reconnects when pairing creates `creds.json`.
- `gateway/run.py`: pending — narrow WhatsApp runtime-status restamp hunk added so generic connected writes do not hide degraded/setup child status.
- `gateway/status.py`: pending — WhatsApp runtime-status details parameter hunk-ported for web-source status.
- `tests/gateway/test_whatsapp_connect.py`: pending — bridge pidfile cleanup, gateway WAL crash-window tests, and not-paired setup-mode test ported; remaining WhatsApp adapter coverage still pending.
- `tests/gateway/test_whatsapp_web_source_runtime.py`: copy — focused web-source supervisor/runtime-status coverage ported.
- `tests/gateway/test_whatsapp_formatting.py`: pending — outbound mode/JID/private-notice focused tests ported; remaining WhatsApp adapter coverage still pending.

- `tests/gateway/test_whatsapp_config_openalma.py`: copy — focused YAML bridge coverage for OpenAlma WhatsApp adapter config.
