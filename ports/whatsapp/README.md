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
- CONTENT-BOUND IDEMPOTENCY, COMPARED BEFORE ANY MUTATION: the same
  idempotency key with DIFFERENT content is idempotency_conflict on EVERY
  path (fresh, held, AND failed-reclaim) - the prior record is never
  overwritten and no second effect occurs. Same key + same content
  returns the stored response with duplicate:true and no new transport
  effect.
- EXPLICIT RETRY ONLY: a failed send is never resent by an ordinary
  resend call (explicit_retry_required, zero transport). Re-claiming a
  failed key requires retry=True - a deliberate act that increments
  attempts and preserves the full prior-attempt history atomically inside
  the claim.
- PER-CODE FAILURE CLASSIFICATION WITH ENFORCEMENT: 4xx failures are
  classified by META ERROR CODE (not by status class) from the official
  Cloud API error-codes reference
  (https://developers.facebook.com/documentation/business-messaging/whatsapp/support/error-codes,
  live-checked 2026-10-10). RULE: every code's behavior traces to a
  quoted reference line; anything unsourceable is UNKNOWN - no retry
  permitted. A 4xx is always a definitive RESPONSE (the message
  certainly did not send); the class governs whether ANY resend is
  permitted, and the claim path ENFORCES it (zero transport on refusal):
  RETRYABLE (explicit retry=True only - there is NO automatic retry
  path; 131000's retry is diagnostic-explicit, never automatic):
  "80007 ... Try again later or reduce the frequency or amount of API
  queries the app is making." / "130429 ... Try again later or reduce
  the frequency with which the app sends messages." / "131056 ... Wait
  and retry the operation, if you intend to send messages to the same
  phone number." / "131000 Message failed to send due to an unknown
  error. Try again. If the error persists, open a Direct Support
  ticket." / 131049 retryable ONLY after the encoded 24h wait: "If you
  do receive this error code and suspect it is due to the limit, wait
  at least 24 hours before resending the template message. Doing so
  will only result in another error response since the limit may be in
  effect for differing periods of time." - retry=True inside the wait is
  refused (retry_wait_not_elapsed, zero transport; enforced by test).
  DEFINITIVE (retry=True REFUSED - definitive_failure_no_retry, zero
  transport; the same content is never resent): "130403 ... Do not
  retry. Unblock the WhatsApp user to resume sending messages to
  them." / "131050 ... Do not retry sending messages to this user as
  they will not be received." / "131047 ... Send the recipient a
  template message instead." (plus parameter/permission/config codes
  100, 130472, 131005, 131008, 131009, 131021, 131026, 131042, 131045,
  131051, 132001).
  UNKNOWN (unlisted or out-of-messaging-scope): persisted as its OWN
  outbox state unknown_failure - never collapsed into "failed" - and NO
  retry is permitted, explicit or otherwise (unknown_failure_no_retry,
  zero transport; diagnose from the actual Meta response first). In
  this bucket: 141006 and 141010 (absent from the current error-codes
  reference, live-checked); 131048 (reference line "Check your quality
  status in the WhatsApp Manager..." carries NO retry instruction);
  131064 ("...automatically lifted after the enforcement period"
  carries NO retry instruction); 131030 (its quotable source is the
  CALLING troubleshooting doc - calling-scope, cannot classify
  MESSAGING behavior); 133016 (registration/deregistration scope).
  The class and any reference-stated wait are recorded on the outbox
  record and carried in the error guidance. The card-free 141006
  guidance above comes from the Cloud API overview "Test resources"
  section (quoted there), NOT from the error-codes reference.

- Inbound handling (synthetic): X-Hub-Signature-256 HMAC validation, then
  ALL-OR-NOTHING validation of the WHOLE entry set - resource binding
  (entry WABA id AND metadata phone_number_id) plus per-message shape and
  time (parseable timestamp, <=5min future skew, within Meta's documented
  SEVEN-DAY webhook retry horizon,
  https://developers.facebook.com/documentation/business-messaging/whatsapp/webhooks/overview/).
  ANY failure rejects the entire batch with ZERO state change - verified
  byte-identical store afterwards. Application is then ONE ATOMIC STORE
  TRANSACTION for the whole entry set: dedupe marks, monotonic window
  extends, durable receipts and horizon pruning commit together or not at
  all (verified: exactly one locked write for a multi-message batch).
  Dedupe by message id over the retry horizon is PERSISTED (survives
  restart, tested). Status receipts are persisted DURABLY as rcpt:<wamid>
  rows and never open windows.
- !!! RECEIPT STORAGE IS NOT RECONCILIATION !!! The durable raw receipt
  rows (rcpt:<wamid>, exposed via receipt(wamid)/statuses) are ONLY
  storage. The adapter performs NO reconciliation: matching receipts to
  outbox records, confirming delivery of 'unknown' sends, or turning a
  receipt into any send decision is EXCLUSIVELY the canonical engine's
  job at activation. A stored receipt never changes adapter behavior.
  Media messages normalize with their type and NO fetched content: media
  download is an explicit SCAFFOLD LIMITATION (no media fetch exists).
- The 24h service window is MONOTONIC and PERSISTED: an older-timestamped
  inbound only ever extends a window forward - it can never regress or
  close a newer open window (tested at T vs T-1h, and against clock
  skew).
- RESOURCE-PARTITIONED STATE: all dedupe marks and windows are namespaced
  by the exact resource (WABA id + phone number id). Two adapters sharing
  ONE store file never inherit each other's windows or dedupe marks -
  a window opened under resource A never authorizes a send under
  resource B (tested: cross-resource in_window false, free-form refused
  with zero transport, same message id delivered independently per
  resource).

## Not here

LIVE webhook intake (only the synthetic normalizer is here), live send
path, real tokens, business verification, any route to a non-test WABA.
Activation gates live with the canonical engine.

## Tests

`pytest ports/whatsapp` - 79 synthetic tests, the review-fix repros
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
beyond-horizon whole-batch rejection. Review-3 repros: failed-key
changed-content conflict with NO record overwrite and NO second effect
(content compared before any mutation); plain resend of a failed key
refused (explicit_retry_required, zero transport) then retry=True
re-claiming with attempts=2 and prior failure preserved in history;
per-code classification parametrized over 7 codes (3 retryable, 3
definitive, 1 unlisted-fail-closed); whole-batch application in exactly
ONE locked store write (spy-verified) with dedupe+window atomic, and a
rejected batch leaving the store byte-identical; status receipts durable
across a fresh instance on the same store file (receipt lookup by wamid,
still no window). Classification-tightening repros: unknown-class
(141006/141010/unlisted) failures refuse EVEN an explicit retry
(unknown_failure_no_retry, zero further effects), and the parametrized
per-code matrix now asserts retryable/definitive/unknown across 15
codes with state separation (unknown_failure is its own persisted
state). Enforcement repros: 130403 and 131050 refuse retry=True with
zero new effects; 131049's 24h wait is encoded on the record and
enforced (refused at +1h, permitted at +24h1s, attempts=2).
