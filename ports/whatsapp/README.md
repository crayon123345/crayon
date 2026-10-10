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
- CANONICAL PERMISSION HANDOFF, FULLY BOUND: every send first asks the
  injected canonical grant function for permission. The grant must
  (a) carry allowed:true - anything else (false OR absent) refuses with
  ZERO transport; (b) echo-bind ALL SIX of: idempotency key, recipient,
  OWNER (constructor-required principal), RESOURCE (exact
  whatsapp:waba:<id>:phone:<id> string), ACTION (send:<type>), CONTENT
  (sha256 of the exact send body); (c) carry a finite, unexpired
  expires_at (NaN/Infinity/non-numeric rejected). No grant function, no
  send. The adapter performs this handoff itself.
- CONTENT-BOUND IDEMPOTENCY: the same idempotency key with DIFFERENT
  content is idempotency_conflict - NEVER a stale success replay. Same
  key + same content returns the stored response with duplicate:true and
  no new transport effect.
- Durable outbox with ATOMIC CLAIM and ATOMIC PUBLISH: exclusive flock on
  a sidecar lock file around read-modify-write, write-temp-then-rename
  with fsync(file)+fsync(dir), reload on every op. FAILS CLOSED
  (OutboxCorrupt) on unparseable content AND on an existing-but-EMPTY
  file - an empty store is a crash artifact, never a fresh start, so a
  crash can never silently reset state into a blind resend. States:
  claimed -> sent | failed | unknown. A LOST transport response OR a Meta
  5xx is UNKNOWN (the request may have landed): never blind-retried,
  resolution via receipts; only 4xx is definitively failed and explicitly
  re-claimable. A "claimed" record left by ANOTHER adapter instance
  (crash recovery) is treated as unknown - a new instance never
  resurrects it into a resend (zero new effects, tested). Same-key
  concurrent sends within one instance share the claim holder's terminal
  outcome: exactly one effect, proven by threaded tests.
- Inbound handling (synthetic): X-Hub-Signature-256 HMAC validation, then
  ALL-OR-NOTHING validation of the WHOLE entry set - resource binding
  (entry WABA id AND metadata phone_number_id) plus per-message shape and
  time (parseable timestamp, <=5min future skew, within Meta's documented
  SEVEN-DAY webhook retry horizon,
  https://developers.facebook.com/documentation/business-messaging/whatsapp/webhooks/overview/).
  ANY failure rejects the entire batch with ZERO state change - a batch
  never partially mutates. Application is then idempotent: dedupe by
  message id over the retry horizon, PERSISTED in the durable store
  (dedupe and window state survive restart, tested). Status receipts are
  recorded explicitly (adapter.statuses) and never open windows; media
  messages normalize with their type and NO fetched content (the adapter
  does not download media).
- The 24h service window is MONOTONIC and PERSISTED: an older-timestamped
  inbound only ever extends a window forward - it can never regress or
  close a newer open window (tested at T vs T-1h, and against clock
  skew).

## Not here

LIVE webhook intake (only the synthetic normalizer is here), live send
path, real tokens, business verification, any route to a non-test WABA.
Activation gates live with the canonical engine.

## Tests

`pytest ports/whatsapp` - 55 synthetic tests, the review-fix repros
counting EFFECTS (transport calls), not just state: owner-verified
recipient binding + cap, window logic, template vs free-form, 141006
card-free guidance, 131000-as-generic-unknown, signature validation,
grant handoff (no-grant refusal; allowed:false AND missing-allowed
refused with zero transport; parametrized echo-binding violations -
owner/resource/action/content-sha256/expires_at incl. NaN/Infinity - all
refused with zero transport; expired grant refused), content-bound
idempotency (same key + different content = idempotency_conflict, old
success never replayed, transport count stays 1; same content =
duplicate:true, one effect), atomic publish (temp+rename, no leftover
tmp files), empty-store-after-crash fail-closed (file left untouched),
crash-recovery pending claim never resurrected (dead-instance claim ->
send_unknown, ZERO new transport effects), 5xx -> unknown with no blind
retry (one effect ever), 4xx definitive + explicit reclaim (two explicit
attempts), lost-response -> unknown (exactly one effect), same-key
threaded concurrency -> exactly one Meta call, durable replay dedupe AND
window persistence across a fresh adapter on the same store file,
status receipts recorded explicitly (never messages, never windows),
media normalized with type and no content, batch all-or-nothing
(wrong-resource or invalid message anywhere -> whole batch rejected, the
GOOD message not recorded, replayed cleanly afterwards), monotonic window
(older inbound never regresses; note_inbound never regresses under clock
skew), corrupt-store fail-closed, wrong-WABA / wrong-phone-id rejection,
2-day delayed-retry dedupe with no window, far-future and
beyond-horizon whole-batch rejection.
