"""Synthetic WhatsApp Cloud API staging adapter (Meta TEST WABA only).

STAGING ONLY: transport is injected; no live Meta calls, no unofficial
libraries, no automation of any personal number, no paid routes.

Hard rules encoded:
- Sends only through the Meta test phone number id configured for the app,
  and only to verified test recipients (Meta allows up to 5).
- Outside the 24h customer service window only template messages may go;
  inside the window free-form text is allowed.
- Error mapping never invents a payment requirement. 141006 is Meta's
  generic business-level block label; per the official Cloud API overview
  ("Test resources"), test WhatsApp Business accounts "don't require a
  payment method on file in order to send template messages" - so the
  card-free fixes (complete business profile, verify recipients) come first.
"""
import time

# UI-observed dashboard limit for test numbers ("public test numbers" in
# Meta's troubleshooting doc). NOT pinned in fetchable official docs; keep
# configurable. Recipient-list BINDING itself is official (131030 docs).
DEFAULT_MAX_TEST_RECIPIENTS = 5
WINDOW_SECONDS = 24 * 3600

# code -> (meaning, card-free next step)
ERROR_MAP = {
    141006: ("business-level send block (generic payment-method label)",
             "Do NOT attach a card. Complete the business profile (Legal "
             "Name, Country, Website) in Meta Business Suite and re-check "
             "health_status; test WABAs are exempt from payment methods per "
             "the official overview."),
    141010: ("business not verified",
             "Complete business info; test-number sends to verified "
             "recipients do not require full verification."),
    131000: ("business profile incomplete",
             "Fill Legal Name, Country, Website in Meta Business Suite > "
             "Settings > Business Info."),
    131030: ("recipient not in the allowed test-recipient list",
             "Add and verify the number in App Dashboard > WhatsApp > API "
             "setup (owner confirms the WhatsApp OTP themself)."),
    131047: ("re-engagement: outside the 24h customer service window",
             "Send an approved template message instead of free-form text."),
    130429: ("rate limit hit", "Back off and retry later."),
    132001: ("template does not exist in this language",
             "Use an approved template name+language for this WABA."),
    131026: ("message undeliverable",
             "Check the recipient number has WhatsApp and can receive "
             "messages from the test number."),
}


class WhatsAppError(Exception):
    def __init__(self, code, guidance):
        super().__init__(f"{code}: {guidance}")
        self.code = code
        self.guidance = guidance


class WhatsAppStagingAdapter:
    """Transport-injected test-WABA sender.

    transport(method, path, headers, body) -> (status, dict)
    verified_recipients: iterable of E.164 numbers confirmed in the app
    dashboard (max 5). token: app-dashboard temporary/permanent token.
    """

    def __init__(self, transport, phone_number_id, token,
                 verified_recipients, clock=time.time,
                 max_recipients=DEFAULT_MAX_TEST_RECIPIENTS):
        recips = set(verified_recipients)
        if len(recips) > max_recipients:
            raise ValueError(
                f"test WABA recipient list exceeds the configured cap "
                f"({max_recipients}; UI-observed default 5)")
        if not phone_number_id or not token:
            raise ValueError("phone_number_id and token required")
        self._t = transport
        self._pnid = phone_number_id
        self._tok = token
        self._recips = recips
        self._clock = clock
        self._last_inbound = {}  # recipient -> ts of last user message

    def note_inbound(self, recipient):
        """Record an inbound user message (opens the 24h service window)."""
        self._require_bound(recipient)
        self._last_inbound[recipient] = self._clock()

    def in_window(self, recipient):
        ts = self._last_inbound.get(recipient)
        return ts is not None and self._clock() - ts < WINDOW_SECONDS

    def send_text(self, recipient, text):
        """Free-form text - only inside the customer service window."""
        self._require_bound(recipient)
        if not self.in_window(recipient):
            raise WhatsAppError(131047, ERROR_MAP[131047][1])
        return self._send({"messaging_product": "whatsapp",
                           "recipient_type": "individual",
                           "to": recipient, "type": "text",
                           "text": {"body": text}})

    def send_template(self, recipient, template, language="en_US"):
        """Template message - allowed outside the service window."""
        self._require_bound(recipient)
        return self._send({"messaging_product": "whatsapp",
                           "recipient_type": "individual",
                           "to": recipient, "type": "template",
                           "template": {"name": template,
                                        "language": {"code": language}}})

    def _require_bound(self, recipient):
        if recipient not in self._recips:
            raise WhatsAppError(131030, ERROR_MAP[131030][1])

    def _send(self, body):
        status, data = self._t(
            "POST", f"/v23.0/{self._pnid}/messages",
            {"Authorization": f"Bearer {self._tok}"}, body)
        if status >= 400:
            err = (data or {}).get("error", {})
            code = err.get("code", 0)
            meaning, guidance = ERROR_MAP.get(
                code, (err.get("message", "unknown Meta error"),
                       "Check the official Cloud API error reference."))
            raise WhatsAppError(code, f"{meaning} - {guidance}")
        return data
