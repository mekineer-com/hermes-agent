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
| WhatsApp platform adapter | `gateway/platforms/whatsapp.py` |
| Baileys bridge | `scripts/whatsapp-bridge/bridge.js` |
| Baileys classification helpers | `scripts/whatsapp-bridge/history_ingest.js` |
| Baileys durable queue | `scripts/whatsapp-bridge/durable_queue.js` |
| WhatsApp Web daemon | `scripts/whatsapp-web-source/source-daemon.js` |
| Web-source projection store | `scripts/whatsapp-web-source/store.py` |
| Web-source normalization | `scripts/whatsapp-web-source/normalization.js` |
| Web-source backfill/reconcile | `scripts/whatsapp-web-source/backfill-manager.js` |
| Web-source contact scope | `scripts/whatsapp-web-source/contact-manager.js` |
| Web-source memory diagnostics | `scripts/whatsapp-web-source/memory-diagnostics.js` |
| Web-source browser options | `scripts/whatsapp-web-source/browser-options.js` |
| Web-source page hooks | `scripts/whatsapp-web-source/page-hooks.js` |
| WhatsApp ID normalization | `gateway/whatsapp_identity.py` |
| SQLite session store | `hermes_state.py` |

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
  - checks `SessionDB.message_source_key_has_response`
  - live rows should be skipped only after the source message has an assistant response
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

## WhatsApp Test Map

Run these after WhatsApp gateway or web-source changes:

```sh
cd hermes-agent
./.venv/bin/python -m pytest -q -o addopts='' \
  tests/gateway/test_whatsapp_history_persist.py \
  tests/gateway/test_session_race_guard.py \
  tests/gateway/test_run_cleanup_progress.py::test_whatsapp_raw_metadata_reaches_agent_without_nameerror \
  tests/test_hermes_state.py

cd scripts/whatsapp-web-source
npm run check && npm test
```

Bridge-focused checks:

```sh
cd hermes-agent
node --test scripts/whatsapp-bridge/history_ingest.test.mjs scripts/whatsapp-bridge/bridge_contract.test.mjs
node --check scripts/whatsapp-bridge/bridge.js
```
