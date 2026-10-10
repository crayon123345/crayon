# /ports/whatsapp - synthetic staging adapter (Meta TEST WABA only)

Transport-injected Cloud API client for the Meta test WhatsApp Business
account. STAGING ONLY: no live Meta calls, no unofficial libraries, no
automation of any personal number, no paid routes.

## Hard rules

- Sends only to recipients with an OWNER-VERIFIED binding to THIS test
  WABA: every recipient is a RecipientBinding(number, phone_number_id,
  owner_confirmed) - the constructor refuses bindings for a different
  phone_number_id and any binding the owner has not confirmed (the owner
  confirms the dashboard OTP themself). Unbound numbers are refused
  locally before any API call. The recipient-list binding requirement is
  official, source observed and quoted verbatim:
  https://developers.facebook.com/documentation/business-messaging/whatsapp/calling/troubleshooting/
  ("Troubleshoot WhatsApp Calling Errors - Reference Guide") - error table:
  "131030 | Recipient phone number is not in allowed list. The error is
  thrown for both attempted calls and call permission requests. Only
  occurs when using public test numbers (PTNs). | Add a recipient phone
  number to recipient list and try again. | 400 Bad request".
  NOTE: this row sits in the CALLING troubleshooting doc (calls + call
  permission requests); for message sends the recipient-list mechanism is
  the same, but this page is the calling reference.
- Recipient CAP: the "max 5" value is UI-observed in the app dashboard
  (consistent third-party docs) but is NOT pinned in a fetchable official
  doc page - it stays a configurable constant
  (DEFAULT_MAX_TEST_RECIPIENTS=5), to be re-pinned against the live
  dashboard during activation. Treat 5 as an unverified assumption.
- 24h customer service window: OFFICIAL, source observed and quoted
  verbatim:
  https://developers.facebook.com/documentation/business-messaging/whatsapp/messages/send-messages
  ("Service messages | Developer Documentation", section "Customer service
  windows"):
  "When a WhatsApp user messages you or calls you, a 24-hour timer called
  a customer service window starts. If the user messages or calls you
  again before the timer expires, the timer resets to 24 hours."
  "While the window is open, you can send any of the following service
  message types to the user. When the window closes, you can only send
  pre-approved template messages."
  Free-form only inside the window, templates any time.
- Error guidance never invents a payment requirement. 141006 is Meta's
  generic business-level block label; per the official overview ("Test
  resources"), test WABAs "don't require a payment method on file in order
  to send template messages" (developers.facebook.com/docs/whatsapp/
  cloud-api/overview/). Card-free fixes first: complete the business
  profile (Legal Name, Country, Website), then re-check health_status.
- 131000 is a GENERIC unknown Meta error. It is NOT evidence of an
  incomplete business profile or of any other specific cause; the adapter
  reports it as unknown and points at the official error reference.
- CANONICAL PERMISSION HANDOFF: every send first asks the injected
  canonical grant function for permission against the verified resource;
  the grant must echo the exact request (idempotency key + recipient).
  No grant function, or a non-echoing grant, and transport is never
  reached. The adapter performs this handoff itself.
- Durable outbox with ATOMIC CLAIM (flock-guarded read-modify-write,
  fsync, reload on every op; fails closed as OutboxCorrupt on a corrupt
  file - never silently resets). States: claimed -> sent | failed |
  unknown. A LOST transport response is UNKNOWN: the message may or may
  not have been sent, so the adapter NEVER blind-retries it - resolution
  is via receipts. A same-key concurrent send shares the claim holder's
  terminal outcome: exactly one effect, proven by threaded tests. A
  definitively failed send (Meta 4xx) may be retried explicitly.
- Inbound handling (synthetic): X-Hub-Signature-256 HMAC validation, then
  RESOURCE binding (entry WABA id AND metadata phone_number_id must match
  the configured owner test WABA/phone id - a signed payload for a
  different resource is dropped and opens nothing), then replay handling:
  dedupe by message id over Meta's documented SEVEN-DAY webhook retry
  horizon
  (https://developers.facebook.com/documentation/business-messaging/whatsapp/webhooks/overview/);
  far-future timestamps (>5min skew) are rejected; a window opens only
  from a valid in-horizon timestamp, and the 24h window math itself
  refuses old timestamps.

## Not here

LIVE webhook intake (only the synthetic normalizer is here), live send
path, real tokens, business verification, any route to a non-test WABA.
Activation gates live with the canonical engine.

## Tests

`pytest ports/whatsapp` - 30 synthetic tests: owner-verified recipient
binding (wrong-WABA and unconfirmed bindings refused) + cap, window
logic, template vs free-form, 141006 card-free guidance, 131000-as-generic
-unknown, unknown-error fallback, inbound signature validation
(bad-signature drop, missing secret refusal), replay rejection (duplicate
message id, stale timestamp), durable outbox (idempotency-key dedupe,
persisted receipt, cross-instance durability via a fresh adapter on the
same store file, failed-send recording), canonical grant handoff (no-grant and
non-echoing-grant refusals reach zero transport), lost-response ->
unknown with no blind retry (exactly one effect ever), same-key threaded
concurrency -> exactly one Meta call, corrupt-store fail-closed,
wrong-WABA / wrong-phone-id webhook rejection, 2-day delayed-retry
dedupe with no window, far-future and beyond-horizon rejection.
