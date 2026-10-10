# Crayon WhatsApp test transport

WhatsApp and Telegram use the same handler, not the same personal identity. A WhatsApp number is its own user ID. Memory, Google access, drafts and reminders never merge with a Telegram user. Only configured allowlisted numbers get replies; group messages are ignored. No Telegram group behavior or private-data rule changes.

## Current setup state

Pratham's Facebook account and developer registration are complete. His portfolio name is `Crayon AI Agent`, app draft `Crayon`, contact `teamalmostinstinct@gmail.com`. Meta rejected portfolio creation because the Facebook account is too new and explicitly said try again in an hour. No portfolio, app, WABA, test number or webhook subscription is confirmed yet. The older Uttiya app/number is a different setup, not this owner's current resources.

Calendar stays on the owner's existing Google connection. The dedicated email is for Meta setup only. No calendar migration is part of this work.

## Plug in the reviewed Meta test resources

1. Create the reviewed portfolio/app with the WhatsApp use case after Meta's cooldown. Keep the app in development/test mode. Use only the Meta-provided TEST WABA and TEST phone number. Do not attach a real number, card, payment method or paid tier.
2. Configure server environment values privately: `WHATSAPP_ACCESS_TOKEN`, `WHATSAPP_APP_SECRET`, `WHATSAPP_VERIFY_TOKEN`, `WHATSAPP_PHONE_NUMBER_ID`, `WHATSAPP_GRAPH_VERSION` and `CRAYON_OWNER_WA_ID` (or explicit `WHATSAPP_ALLOWED_IDS`). `.env.example` has blank resource/recipient IDs so another person's assets cannot be selected accidentally. No secrets in repo, chat, screenshots or logs.
3. Deploy and verify health. Existing database initialization creates additive `whatsapp_inbox` and `whatsapp_outbox` tables. If storage is unavailable, signed messages get 503 for provider retry, not a false success.
4. Meta webhook callback is the deployment base URL plus `/whatsapp/webhook`. GET verification checks the exact verify token; POST checks `X-Hub-Signature-256` against exact raw bytes. Subscribe the WABA/app to `messages`, which includes inbound messages AND delivery status events. Confirm the current WABA subscription rather than assuming a saved callback is enough.
5. Add the owner's reviewed number as a test recipient. Any personal SMS OTP comes from the owner. Send only reviewed test content. API accepted wamid is not delivery proof: correlate `sent`, `delivered`, `read` or `failed` status by wamid and recipient.
6. Done means a real inbound owner message and a bot reply visibly received, with its matching delivery status. An HTTP 200 or unit test alone is not two-way proof.

## Transport and retry behavior

- Only the configured phone-number ID and allowlisted sender are accepted. Group payloads never become private chats.
- Inbound wamids are uniquely persisted before ACK. Duplicate webhooks, including after restart, do not queue a second handler.
- Queued work resumes after restart. Processing leases are not blindly replayed: a crashed or failed handler can have partially sent a reply or made an approved action. It becomes `uncertain`, for inspection rather than automatic duplication. This intentionally trades automatic recovery for avoiding duplicate effects.
- Text and interactive replies use the existing private handler. Plain-text formatting matches Telegram; no reactions or user-message deletion are claimed. Incoming media gets a text-only notice. Existing explicit chart/file output remains supported.
- Outbound sends require an allowlisted recipient, durable storage and an inbound message within 24 hours. No template or paid conversation-opening path is implemented. Outside-window reminders fail honestly, not silently or via paid fallback.
- Send success requires an actual provider wamid. Store only recipient/status/timestamp/numeric error code in the outbox, never provider error prose. A status can race the API response; only locally accepted sends appear in delivery readback. Status updates cannot regress delivered/read to sent or failed.
- Completed/uncertain inbox bodies and profile names are erased. Transport metadata expires after seven days. `/delete_my_data` also clears the person's transport rows. The shared memory handler keeps its existing data policy.
- Phone-number users remain separate from the owner Telegram ID. Owner-only calendar/computer beta access is not widened by adding WhatsApp.

## Tests and remaining proof

Automated tests cover signature bytes, handshake, malformed JSON, wrong number/group rejection, allowlist, retry dedupe, storage failure, queued/uncertain recovery, send failure, status filtering/correlation, formatting/redaction and the no-template/no-window guard. Live Meta provisioning and two-way delivery remain pending until the dashboard and actual transport confirm them.
