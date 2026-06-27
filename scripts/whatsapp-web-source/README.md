# Hermes WhatsApp Web Source

Experimental production-facing WhatsApp Web source daemon.

It uses `whatsapp-web.js` to read decrypted WhatsApp Web messages and projects a normalized subset to SQLite. It does not send replies and does not mark chats seen.

After WhatsApp Web is ready, it snapshots contact/name models into `whatsapp_contacts`. This uses WhatsApp Web's internal contact collection directly instead of `client.getContacts()`, because one malformed device WID can make the higher-level API fail the whole batch.

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

Skip the contact/name snapshot for debugging:

```sh
node source-daemon.js --no-contact-snapshot
```

Contacts are refreshed every 15 minutes by default. Tune or disable the refresh:

```sh
node source-daemon.js --contact-snapshot-interval 300
node source-daemon.js --contact-snapshot-interval 0
```

Chromium/page memory diagnostics are written into the health file every 60 seconds by default. Tune or disable them:

```sh
node source-daemon.js --memory-diagnostics-interval 15
node source-daemon.js --memory-diagnostics-interval 0
```

Disable image/media/font request blocking for RAM/CPU A/B testing:

```sh
node source-daemon.js --no-resource-block
```

## Safety

- Never use unlimited backfill in normal operation.
- Do not run as a sender until the group explicitly chooses to replace the current send path.
- The projection DB stores plaintext WhatsApp messages locally.
