# Hermes Agent Local Index

This is a local memU/Hermes orientation file. `AGENTS.md` remains the upstream
Hermes development guide; this file maps the local integration seams agents
touch often.

## Upstream Merge Rule

`gateway/run.py`, `cli.py`, and `run_agent.py` are large upstream-shared files.
Avoid structural extraction from them unless there is an active bug that cannot
be fixed safely in place.

Prefer:

- narrow patches at existing seams
- local helper modules for new behavior
- comments or mapping docs when the problem is orientation, not behavior
- tests that pin behavior before touching upstream-heavy files

Reason: moving large blocks out of upstream-shared files makes future upstream
pulls harder to merge and audit.

Also: Do not work on or cleanup Hermes code: Preserve upstream-diff cleanliness unless the code breaks OpenAlma.

## WhatsApp Architecture Map

Two WhatsApp paths are intentionally active:

- Baileys bridge -> Hermes gateway -> `state.db`
  - live delivery
  - processed-message/source-key ledger
  - gateway error/response delivery bookkeeping
- wwebjs web-source -> `web_source.db`
  - reconciled history
  - Prompt Inspector / memU WhatsApp history
  - deleted/revoked message projection

Do not silently fall back from configured `web_source` history to `state.db`.
Live turns may fail open with caller/empty history; non-live history reads should
fail visibly.

## WhatsApp Files

| Area | File |
|------|------|
| WhatsApp platform adapter | `gateway/platforms/whatsapp.py` — staleness gate in `_dispatch_built_message_event`; post-turn title generation skipped for soul-mode sessions; DM allowlist matching normalized (strips suffix variants) |
| Baileys bridge | `scripts/whatsapp-bridge/bridge.js` |
| Baileys classification helpers | `scripts/whatsapp-bridge/history_ingest.js` |
| Baileys durable queue | `scripts/whatsapp-bridge/durable_queue.js`; normalizes Baileys Long-shaped and millisecond timestamps to seconds before persisting queue.seen or replaying live upgrades |
| WhatsApp channel policy lookup | `gateway/memu_policy.py` — policy lookup + reverse-alias merge so aliases resolve to canonical policy |
| WhatsApp Web daemon | `scripts/whatsapp-web-source/source-daemon.js` |
| Web-source projection store | `scripts/whatsapp-web-source/store.py` |
| Web-source normalization | `scripts/whatsapp-web-source/normalization.js` |
| Web-source backfill/reconcile | `scripts/whatsapp-web-source/backfill-manager.js` |
| Web-source contact scope | `scripts/whatsapp-web-source/contact-manager.js` |
| Web-source memory diagnostics | `scripts/whatsapp-web-source/memory-diagnostics.js` |
| Web-source browser options | `scripts/whatsapp-web-source/browser-options.js` |
| Web-source page hooks | `scripts/whatsapp-web-source/page-hooks.js` |
| WhatsApp ID normalization | `gateway/whatsapp_identity.py` |
| WhatsApp identity seam | `gateway/whatsapp_seam.py` — OpenAlma-owned resolver: LID-preferred alias resolution, outbound `to_whatsapp_jid` normalization at the 5 adapter send sites, full JID preservation |
| WhatsApp contact store | `gateway/contact_store.py` — append-only evidence store; derives explicit columns (`id`, `lid_jid`, `phone_jid`, `legacy_jids`, `bare_phone`, `display_name`, `observed_names`); LID-preferred key stable when later phone evidence arrives; display-name derivation ignores numeric/JID placeholders; persists to `~/.hermes/whatsapp/contact_store.json` |
| Bridge known-name reader | `gateway/whatsapp_known_contacts.py` — shared Python reader for bridge `known_contacts.json` / `known_chats.json`; used by `channel_directory.py` and backfill to fill blank/numeric/JID-like names from bridge-known human names |
| Channel directory | `gateway/channel_directory.py` — fallback rows use bridge known names when session/directory names are blank or JID-like; LID-first collision during backfill merges human contact name from the phone entry |
| SQLite session store | `hermes_state.py` — includes `processed_source_keys` table (durable dedup ledger, keyed by `source_chat_id` + `source_message_id`) |
| Burst-debounce control | `gateway/platforms/base.py` `merge_pending_message_event` (~1092) |
| WhatsApp JID backfill | `scripts/backfill_whatsapp_identity_jids.py` — normalizes `sessions.json`, `memu.json`, `channel_directory.json`, and `state.db` session `user_id`s to canonical LID JIDs; skips `messages.source_chat_id` (unique-index constraint); rehearse on a copy before running live |

## `gateway/run.py` WhatsApp Seam Map

Keep these as a map unless a focused bug requires code movement.

Search targets:

- `_handle_message`
  - WhatsApp revoke pre-dispatch gate
  - WhatsApp persist-only/history pre-dispatch gate
  - WhatsApp duplicate source-key pre-dispatch gate
- `_apply_whatsapp_revoke`
  - removes matching source-key rows from `state.db`
  - rewrites JSON transcript backup if needed
- `_is_whatsapp_persist_only_event`
  - identifies history/persist-only events that should not wake Siri
- `_is_duplicate_whatsapp_source_message`
  - checks `processed_source_keys` table first (keyed by `source_chat_id` + `source_message_id`); falls back to legacy `SessionDB.message_source_key_has_response()` and promotes hits into the new table
  - live rows should be skipped only after the source message has an assistant response
- `_dispatch_built_message_event`
  - staleness gate: `live` rows older than `whatsapp.max_message_age_seconds` (config, default 300s, 0=off) are dropped, logged, and WAL-marked; `persist_only`/`revoke` still flow normally
- post-agent transcript persistence in `_handle_message`
  - adds WhatsApp sender/source/timestamp metadata to user rows
  - updates latest user sender in `state.db`
- `_persist_whatsapp_exception_turn`
  - stores user/error pair for failed live WhatsApp turns
  - must not duplicate the user row when history already persisted it
- `_persist_whatsapp_history_event`
  - stores persist-only/history rows into `state.db`
  - respects soul `active_since`
  - maps assistant history to `soul:<soul_id>`
- `_handle_response_delivery`
  - stamps delivered assistant WhatsApp message id onto the matching assistant row

## Agent Runtime Seam Map

Release `run_agent.py` delegates large pieces into `agent/`; do not port old
fork hunks back into `run_agent.py` when the live code now owns the seam in an
extracted module.

Search targets:

- `run_agent.py`
  - `AIAgent.__init__` forwards `soul_mode_cfg`.
  - `_flush_messages_to_session_db()` persists sender/source metadata and soul assistant identity.
  - `configure_soul_mode()` delegates to `agent.soul_mode`.
- `agent/agent_init.py`
  - initializes `_soul_config` from `soul_mode_cfg`.
- `agent/turn_context.py`
  - adds gateway sender/source/timestamp metadata to the current user row before early persistence.
- `agent/conversation_loop.py`
  - delegates active soul turns to `agent.soul_mode.handle_turn()` before the normal model runtime.

## WhatsApp Test Map

For normal OpenAlma fork validation, do not run the whole upstream suite first;
it includes optional ACP/GUI/dashboard tests that are noisy in this checkout.
Run the focused file-list gate instead:

```sh
cd hermes-agent
grep -Ev '^(#|$)' tests/openalma_gate.txt | xargs scripts/run_tests.sh
```

Run these after WhatsApp gateway or web-source changes:

```sh
cd hermes-agent
./.venv/bin/python -m pytest -q -o addopts='' \
  tests/gateway/test_whatsapp_history_persist.py \
  tests/gateway/test_session_race_guard.py \
  tests/gateway/test_run_cleanup_progress.py::test_whatsapp_raw_metadata_reaches_agent_without_nameerror \
  tests/test_hermes_state.py \
  tests/gateway/test_whatsapp_contact_store.py \
  tests/gateway/test_whatsapp_known_contacts.py \
  tests/gateway/test_channel_directory.py \
  tests/scripts/test_backfill_whatsapp_identity_jids.py

cd scripts/whatsapp-web-source
npm run check && npm test
```

Bridge-focused checks:

```sh
cd hermes-agent
node --test scripts/whatsapp-bridge/history_ingest.test.mjs scripts/whatsapp-bridge/bridge_contract.test.mjs
node --check scripts/whatsapp-bridge/bridge.js
```
