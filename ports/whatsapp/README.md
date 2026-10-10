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
- Inbound handling (synthetic): handle_inbound_webhook validates
  X-Hub-Signature-256 (HMAC-SHA256 over the exact request bytes with the
  app secret) and rejects replays (duplicate message ids, timestamps
  outside the 1h skew window) BEFORE any state change; only validated
  messages are normalized into InboundMessage and open the 24h window.
- Durable outbox + canonical handoff: every send is a CanonicalRequest
  with an idempotency key; the adapter itself performs the handoff (no
  separate executor), persists pending/sent/failed state plus the Meta
  receipt (wamid) in a JsonOutboxStore (atomic JSON file), and a repeated
  idempotency key returns the stored CanonicalResponse without re-sending.

## Not here

LIVE webhook intake (only the synthetic normalizer is here), live send
path, real tokens, business verification, any route to a non-test WABA.
Activation gates live with the canonical engine.

## Tests

`pytest ports/whatsapp` - 23 synthetic tests: owner-verified recipient
binding (wrong-WABA and unconfirmed bindings refused) + cap, window
logic, template vs free-form, 141006 card-free guidance, 131000-as-generic
-unknown, unknown-error fallback, inbound signature validation
(bad-signature drop, missing secret refusal), replay rejection (duplicate
message id, stale timestamp), durable outbox (idempotency-key dedupe,
persisted receipt, cross-instance durability via a fresh adapter on the
same store file, failed-send recording), canonical handoff fields.
