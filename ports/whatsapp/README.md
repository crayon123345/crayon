# /ports/whatsapp - synthetic staging adapter (Meta TEST WABA only)

Transport-injected Cloud API client for the Meta test WhatsApp Business
account. STAGING ONLY: no live Meta calls, no unofficial libraries, no
automation of any personal number, no paid routes.

## Hard rules

- Sends only to verified test recipients (max 5, owner-confirmed OTP in the
  app dashboard). Unverified numbers are refused locally before any API call.
- 24h customer-service-window logic: free-form text only inside the window,
  approved templates any time.
- Error guidance never invents a payment requirement. 141006 is Meta's
  generic business-level block label; per the official overview ("Test
  resources"), test WABAs "don't require a payment method on file in order
  to send template messages". Card-free fixes first: complete the business
  profile (Legal Name, Country, Website), then re-check health_status.

## Not here

Webhook intake, live send path, real tokens, business verification, any
route to a non-test WABA. Activation gates live with the canonical engine.

## Tests

`pytest ports/whatsapp` - 11 synthetic tests: recipient binding + cap,
window logic, template vs free-form, 141006/131000 card-free guidance,
unknown-error fallback.
