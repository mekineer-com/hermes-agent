# Hermes WhatsApp Web Source

Experimental production-facing WhatsApp Web source daemon.

It uses `whatsapp-web.js` to read decrypted WhatsApp Web messages and projects a normalized subset to SQLite. It does not send replies, does not mark chats seen, and is not wired into Hermes prompt history yet.

## Install

```sh
cd hermes-agent/scripts/whatsapp-web-source
npm install
```

In production it uses the npm `whatsapp-web.js` dependency. During local development from `~/apps-codex`, set `HERMES_WWEBJS_LOCAL=1` to force the sibling `wwebjs` checkout.

## Run

```sh
node source-daemon.js
```

Defaults:

- Auth profile: `~/.hermes/whatsapp/wwebjs_auth/session-memu-web-source`
- Projection DB: `~/.hermes/whatsapp/web_source.db`
- Health file: `~/.hermes/whatsapp/web_source_status.json`

Backfill one bounded chat window:

```sh
node source-daemon.js --backfill-chat 16467326349@c.us --backfill-limit 100 --exit-after-backfill
```

## Safety

- Never use unlimited backfill in normal operation.
- Do not run as a sender until the group explicitly chooses to replace the current send path.
- The projection DB stores plaintext WhatsApp messages locally.
