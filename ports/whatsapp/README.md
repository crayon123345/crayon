# /ports/whatsapp - synthetic staging adapter (Meta TEST WABA only)

Transport-injected Cloud API client for the Meta test WhatsApp Business
account. STAGING ONLY: no live Meta calls, no unofficial libraries, no
automation of any personal number, no paid routes.

## Hard rules

- Sends only to verified test recipients. The recipient-list binding is
  official: Meta's own error reference lists 131030 "Recipient phone number
  is not in allowed list ... Only occurs when using public test numbers
  (PTNs)" - developers.facebook.com/documentation/business-messaging/
  whatsapp/calling/troubleshooting/. Unverified numbers are refused locally
  before any API call.
- Recipient CAP: the "max 5" value is UI-observed in the app dashboard
  (consistent third-party docs) but is NOT pinned in a fetchable official
  doc page - it stays a configurable constant
  (DEFAULT_MAX_TEST_RECIPIENTS=5), to be re-pinned against the live
  dashboard during activation. Treat 5 as an unverified assumption.
- 24h customer service window: OFFICIAL - "When a WhatsApp user messages
  you or calls you, a 24-hour timer called a customer service window
  starts... When the window closes, you can only send pre-approved template
  messages." (developers.facebook.com/documentation/business-messaging/
  whatsapp/messages/send-messages). Free-form only inside the window,
  templates any time.
- Error guidance never invents a payment requirement. 141006 is Meta's
  generic business-level block label; per the official overview ("Test
  resources"), test WABAs "don't require a payment method on file in order
  to send template messages" (developers.facebook.com/docs/whatsapp/
  cloud-api/overview/). Card-free fixes first: complete the business
  profile (Legal Name, Country, Website), then re-check health_status.

## Not here

Webhook intake, live send path, real tokens, business verification, any
route to a non-test WABA. Activation gates live with the canonical engine.

## Tests

`pytest ports/whatsapp` - 11 synthetic tests: recipient binding + cap,
window logic, template vs free-form, 141006/131000 card-free guidance,
unknown-error fallback.
