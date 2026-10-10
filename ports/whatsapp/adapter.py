"""Synthetic WhatsApp Cloud API staging adapter (Meta TEST WABA only).

STAGING ONLY: transport is injected; no live Meta calls, no unofficial
libraries, no automation of any personal number, no paid routes.

Hard rules encoded:
- Sends only through the Meta test phone number id configured for the app,
  and only to recipients BOUND to that test WABA by an owner-verified
  RecipientBinding (owner confirms the dashboard OTP themself).
- Outside the 24h customer service window only template messages may go;
  inside the window free-form text is allowed.
- Inbound webhooks are normalized through signature validation
  (X-Hub-Signature-256 HMAC) and replay rejection (duplicate message ids,
  stale/future timestamps) before any state changes.
- Outbound sends go through a DURABLE outbox: every send is a canonical
  request with an idempotency key; the outbox persists pending/sent/failed
  state plus Meta receipts (wamid), and a repeated idempotency key returns
  the stored result without re-sending. The adapter itself performs the
  canonical handoff - there is no separate executor.
- Error mapping never invents a payment requirement. 141006 is Meta's
  generic business-level block label; per the official Cloud API overview
  ("Test resources"), test WhatsApp Business accounts "don't require a
  payment method on file in order to send template messages" - so the
  card-free fixes (complete business profile, verify recipients) come
  first. 131000 is a GENERIC unknown error and is treated as such.
"""
import hashlib
import hmac
import json
import time
import uuid

# UI-observed dashboard limit for test numbers. NOT pinned in fetchable
# official docs; keep configurable. Recipient-list BINDING itself is
# official (see README sources).
DEFAULT_MAX_TEST_RECIPIENTS = 5
WINDOW_SECONDS = 24 * 3600
# Replay validation: inbound message timestamps outside this skew of the
# local clock are rejected as replays or forgeries.
MAX_INBOUND_SKEW_SECONDS = 3600

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
    131000: ("generic unknown Meta error (NOT evidence of an incomplete "
             "profile)",
             "Treat as unknown: log the full payload, consult the official "
             "Cloud API error reference, and retry diagnosis from the "
             "actual response - do not assume any specific cause."),
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


class RecipientBinding:
    """An owner-verified binding of one test recipient to one test WABA
    phone number id. Sends are refused unless the recipient is bound to
    THIS adapter's phone_number_id with owner_confirmed=True."""

    def __init__(self, number, phone_number_id, owner_confirmed):
        self.number = number
        self.phone_number_id = phone_number_id
        self.owner_confirmed = bool(owner_confirmed)


class CanonicalRequest:
    """The canonical outbound unit handed off by the adapter itself."""

    def __init__(self, to, type_, body, idempotency_key=None):
        self.idempotency_key = idempotency_key or str(uuid.uuid4())
        self.to = to
        self.type = type_
        self.body = body


class CanonicalResponse:
    """Canonical result of a dispatch: ok/status/wamid/error + raw body."""

    def __init__(self, ok, status, wamid=None, error_code=None, raw=None,
                 duplicate=False):
        self.ok = ok
        self.status = status
        self.wamid = wamid
        self.error_code = error_code
        self.raw = raw or {}
        self.duplicate = duplicate  # True when served from the outbox


class InboundMessage:
    """Normalized inbound unit produced only after signature + replay
    validation pass."""

    def __init__(self, sender, message_id, timestamp, type_, text=None):
        self.sender = sender
        self.message_id = message_id
        self.timestamp = timestamp
        self.type = type_
        self.text = text


class JsonOutboxStore:
    """Durable JSON-file outbox: pending/sent/failed + receipts survive
    process restarts. One file, rewritten atomically per mutation."""

    def __init__(self, path):
        self.path = str(path)
        try:
            with open(self.path) as f:
                self._rows = json.load(f)
        except (OSError, ValueError):
            self._rows = {}

    def get(self, key):
        return self._rows.get(key)

    def put(self, key, record):
        self._rows[key] = record
        tmp = self.path + ".tmp"
        with open(tmp, "w") as f:
            json.dump(self._rows, f)
        import os
        os.replace(tmp, self.path)


class WhatsAppStagingAdapter:
    """Transport-injected test-WABA sender with durable outbox.

    transport(method, path, headers, body) -> (status, dict)
    bindings: iterable of RecipientBinding (or dicts) for THIS
    phone_number_id; every binding must be owner_confirmed.
    app_secret: required for inbound webhook handling (signature).
    outbox_store: JsonOutboxStore-compatible; defaults to in-memory dict
    (staging default only - pass a real store for durability).
    """

    def __init__(self, transport, phone_number_id, token, bindings,
                 clock=time.time, max_recipients=DEFAULT_MAX_TEST_RECIPIENTS,
                 app_secret=None, outbox_store=None):
        if not phone_number_id or not token:
            raise ValueError("phone_number_id and token required")
        bound = {}
        for b in bindings:
            if isinstance(b, dict):
                b = RecipientBinding(b["number"], b["phone_number_id"],
                                     b.get("owner_confirmed", False))
            if b.phone_number_id != phone_number_id:
                raise ValueError(
                    f"binding for {b.number} targets a different "
                    f"phone_number_id ({b.phone_number_id}) - not this "
                    f"adapter's test WABA")
            if not b.owner_confirmed:
                raise ValueError(
                    f"binding for {b.number} is not owner-confirmed - the "
                    "owner must verify the dashboard OTP themself")
            bound[b.number] = b
        if len(bound) > max_recipients:
            raise ValueError(
                f"test WABA recipient list exceeds the configured cap "
                f"({max_recipients}; UI-observed default 5)")
        self._t = transport
        self._pnid = phone_number_id
        self._tok = token
        self._bindings = bound
        self._clock = clock
        self._secret = app_secret
        self._last_inbound = {}   # recipient -> ts of last user message
        self._seen_ids = set()    # replay guard (message ids)
        self._outbox = outbox_store if outbox_store is not None else {}
        # in-memory fallback mirrors the store interface
        if isinstance(self._outbox, dict):
            class _Mem:
                def __init__(s, d): s.d = d
                def get(s, k): return s.d.get(k)
                def put(s, k, r): s.d[k] = r
            self._outbox = _Mem(self._outbox)

    # -- inbound: signature + replay validation, then normalization -------
    def handle_inbound_webhook(self, raw_body, signature_header):
        """Validate X-Hub-Signature-256, reject replays, then normalize.
        raw_body: the exact request bytes. signature_header: the raw
        'sha256=...' header value. Raises WhatsAppError on any failure;
        returns a list of InboundMessage (duplicates excluded)."""
        if not self._secret:
            raise WhatsAppError("no_app_secret",
                                "inbound handling needs the app secret for "
                                "signature validation")
        expect = "sha256=" + hmac.new(self._secret.encode(), raw_body,
                                      hashlib.sha256).hexdigest()
        if not signature_header or not hmac.compare_digest(
                expect, signature_header):
            raise WhatsAppError("bad_signature",
                                "X-Hub-Signature-256 mismatch - dropped")
        try:
            payload = json.loads(raw_body)
        except ValueError:
            raise WhatsAppError("malformed", "webhook body is not JSON")
        out = []
        now = self._clock()
        for entry in payload.get("entry", []):
            for change in entry.get("changes", []):
                for msg in change.get("value", {}).get("messages", []):
                    mid = msg.get("id")
                    ts = int(msg.get("timestamp", 0))
                    if not mid or mid in self._seen_ids:
                        continue  # replay / duplicate delivery
                    if abs(now - ts) > MAX_INBOUND_SKEW_SECONDS:
                        continue  # stale or future-dated: replay guard
                    self._seen_ids.add(mid)
                    sender = msg.get("from", "")
                    text = (msg.get("text") or {}).get("body")
                    norm = InboundMessage(sender, mid, ts,
                                          msg.get("type", "unknown"), text)
                    if sender in self._bindings:
                        self._last_inbound[sender] = ts
                    out.append(norm)
        return out

    def note_inbound(self, recipient):
        """Manual equivalent for synthetic tests: record an inbound user
        message (opens the 24h service window)."""
        self._require_bound(recipient)
        self._last_inbound[recipient] = self._clock()

    def in_window(self, recipient):
        ts = self._last_inbound.get(recipient)
        return ts is not None and self._clock() - ts < WINDOW_SECONDS

    # -- outbound: canonical request -> durable outbox -> dispatch --------
    def send_text(self, recipient, text, idempotency_key=None):
        """Free-form text - only inside the customer service window."""
        self._require_bound(recipient)
        if not self.in_window(recipient):
            raise WhatsAppError(131047, ERROR_MAP[131047][1])
        req = CanonicalRequest(recipient, "text",
                               {"messaging_product": "whatsapp",
                                "recipient_type": "individual",
                                "to": recipient, "type": "text",
                                "text": {"body": text}},
                               idempotency_key)
        return self._dispatch(req)

    def send_template(self, recipient, template, language="en_US",
                      idempotency_key=None):
        """Template message - allowed outside the service window."""
        self._require_bound(recipient)
        req = CanonicalRequest(recipient, "template",
                               {"messaging_product": "whatsapp",
                                "recipient_type": "individual",
                                "to": recipient, "type": "template",
                                "template": {"name": template,
                                             "language": {"code": language}}},
                               idempotency_key)
        return self._dispatch(req)

    def _dispatch(self, req):
        """Canonical handoff (performed by the adapter itself - no separate
        executor): idempotent outbox lookup, then one Meta call, with the
        receipt persisted either way."""
        prior = self._outbox.get(req.idempotency_key)
        if prior is not None and prior.get("state") == "sent":
            r = prior["response"]
            return CanonicalResponse(True, r["status"], wamid=r.get("wamid"),
                                     raw=r.get("raw"), duplicate=True)
        record = {"state": "pending", "request": {
            "to": req.to, "type": req.type, "body": req.body},
            "attempts": (prior or {}).get("attempts", 0) + 1}
        self._outbox.put(req.idempotency_key, record)
        try:
            status, data = self._t(
                "POST", f"/v23.0/{self._pnid}/messages",
                {"Authorization": f"Bearer {self._tok}"}, req.body)
        except Exception as e:
            record["state"] = "failed"
            record["last_error"] = f"transport: {e!r}"
            self._outbox.put(req.idempotency_key, record)
            raise
        if status >= 400:
            err = (data or {}).get("error", {})
            code = err.get("code", 0)
            record["state"] = "failed"
            record["last_error"] = {"code": code,
                                    "message": err.get("message")}
            self._outbox.put(req.idempotency_key, record)
            meaning, guidance = ERROR_MAP.get(
                code, (err.get("message", "unknown Meta error"),
                       "Check the official Cloud API error reference."))
            raise WhatsAppError(code, f"{meaning} - {guidance}")
        wamid = ((data or {}).get("messages") or [{}])[0].get("id")
        record["state"] = "sent"
        record["response"] = {"status": status, "wamid": wamid, "raw": data}
        record["receipt"] = {"wamid": wamid, "sent_at": self._clock()}
        self._outbox.put(req.idempotency_key, record)
        return CanonicalResponse(True, status, wamid=wamid, raw=data)

    def outbox_record(self, idempotency_key):
        """Inspect a persisted outbox record (state, attempts, receipt)."""
        return self._outbox.get(idempotency_key)

    # -- helpers ----------------------------------------------------------
    def _require_bound(self, recipient):
        if recipient not in self._bindings:
            raise WhatsAppError(131030, ERROR_MAP[131030][1])
