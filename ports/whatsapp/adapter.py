"""Synthetic WhatsApp Cloud API staging adapter (Meta TEST WABA only).

STAGING ONLY: transport is injected; no live Meta calls, no unofficial
libraries, no automation of any personal number, no paid routes.

Architecture (rework after cross-review):
- CANONICAL PERMISSION HANDOFF: every outbound send first asks the
  injected canonical grant function for permission against the verified
  resource; the grant must echo the request's idempotency key and
  recipient. Only a granted send reaches transport. No grant function,
  no send. The adapter performs this handoff itself.
- DURABLE OUTBOX with atomic claim: sends are claimed under an exclusive
  file lock before any transport call. States: claimed -> sent | failed |
  unknown. A LOST RESPONSE (transport exception after the request may
  have landed) is UNKNOWN - never blind-retried; resolution is via
  receipts. A same-key concurrent caller waits for the claim holder's
  terminal state and shares its outcome: exactly one effect. A
  definitively FAILED send (Meta 4xx) may be retried explicitly.
- The JSON store is atomic and locked (flock + truncate/write + fsync,
  reload on every op) and FAILS CLOSED on corruption (OutboxCorrupt) -
  it never silently resets.
- Inbound webhooks: X-Hub-Signature-256 validation, then RESOURCE binding
  (entry WABA id + metadata phone_number_id must match the configured
  owner test WABA/phone id), then replay handling: dedupe by message id
  over Meta's 7-day webhook retry horizon; far-future timestamps are
  rejected; a window opens only from a valid, in-horizon timestamp
  (the 24h window math itself rejects old timestamps).
- owner_confirmed on a RecipientBinding is required constructor input,
  not proof by itself; the webhook resource binding above is the
  inbound-side enforcement.
- Error mapping never invents a payment requirement. 141006 is Meta's
  generic business-level block label; per the official Cloud API overview
  ("Test resources"), test WABAs "don't require a payment method on file
  in order to send template messages" - card-free fixes first. 131000 is
  a GENERIC unknown error and is treated as such.
"""
import hashlib
import hmac
import json
import os
import threading
import time
import uuid

DEFAULT_MAX_TEST_RECIPIENTS = 5
WINDOW_SECONDS = 24 * 3600
# Meta documents webhook retries/duplicates up to SEVEN days (see README):
RETRY_HORIZON_SECONDS = 7 * 24 * 3600
MAX_FUTURE_SKEW_SECONDS = 300

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


class OutboxCorrupt(Exception):
    """The durable outbox cannot be parsed. Fail closed; never reset."""


class RecipientBinding:
    """An owner-verified binding of one test recipient to one test WABA
    phone number id."""

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
    def __init__(self, ok, status, wamid=None, error_code=None, raw=None,
                 duplicate=False, grant_id=None):
        self.ok = ok
        self.status = status
        self.wamid = wamid
        self.error_code = error_code
        self.raw = raw or {}
        self.duplicate = duplicate
        self.grant_id = grant_id


class InboundMessage:
    def __init__(self, sender, message_id, timestamp, type_, text=None):
        self.sender = sender
        self.message_id = message_id
        self.timestamp = timestamp
        self.type = type_
        self.text = text


class JsonOutboxStore:
    """Durable JSON outbox: exclusive-flock read-modify-write on every op
    (cross-instance safe), fsync before close, FAIL CLOSED on corruption."""

    def __init__(self, path):
        self.path = str(path)
        self._mem_lock = threading.Lock()
        # fail closed at construction if an existing file is corrupt
        if os.path.exists(self.path):
            self._read_rows()

    def _read_rows(self):
        with open(self.path) as f:
            raw = f.read()
        if not raw.strip():
            return {}
        try:
            return json.loads(raw)
        except ValueError:
            raise OutboxCorrupt(
                f"outbox store {self.path} is corrupt - refusing to "
                "continue (never silently reset)") from None

    def _mutate(self, fn):
        import fcntl
        with self._mem_lock:
            with open(self.path, "a+") as f:
                fcntl.flock(f, fcntl.LOCK_EX)
                try:
                    f.seek(0)
                    raw = f.read()
                    if raw.strip():
                        try:
                            rows = json.loads(raw)
                        except ValueError:
                            raise OutboxCorrupt(
                                f"outbox store {self.path} is corrupt - "
                                "refusing to continue")
                    else:
                        rows = {}
                    result = fn(rows)
                    f.seek(0)
                    f.truncate()
                    json.dump(rows, f)
                    f.flush()
                    os.fsync(f.fileno())
                    return result
                finally:
                    fcntl.flock(f, fcntl.LOCK_UN)

    def get(self, key):
        return self._mutate(lambda rows: rows.get(key))

    def claim(self, key, record):
        """Atomically insert if absent OR if the prior record is a
        definitively-failed send being retried. Returns True iff WE hold
        the claim."""
        def fn(rows):
            prior = rows.get(key)
            if prior is None or prior.get("state") == "failed":
                rows[key] = record
                return True
            return False
        return self._mutate(fn)

    def put(self, key, record):
        def fn(rows):
            rows[key] = record
        self._mutate(fn)


class MemOutboxStore:
    """In-memory store with the same atomic-claim contract (staging
    default; NOT durable)."""

    def __init__(self):
        self._rows = {}
        self._lk = threading.Lock()

    def get(self, key):
        with self._lk:
            return self._rows.get(key)

    def claim(self, key, record):
        with self._lk:
            prior = self._rows.get(key)
            if prior is None or prior.get("state") == "failed":
                self._rows[key] = record
                return True
            return False

    def put(self, key, record):
        with self._lk:
            self._rows[key] = record


class WhatsAppStagingAdapter:
    """Transport-injected test-WABA sender with canonical grant handoff
    and a durable, atomic outbox.

    transport(method, path, headers, body) -> (status, dict)
    canonical_grant(request) -> grant dict; REQUIRED. The adapter asks
    canonical for permission against the verified resource on every send;
    the grant must echo the request idempotency key and recipient.
    waba_id: the owner-verified test WABA id (inbound resource binding).
    """

    def __init__(self, transport, phone_number_id, token, bindings,
                 clock=time.time, max_recipients=DEFAULT_MAX_TEST_RECIPIENTS,
                 app_secret=None, outbox_store=None, canonical_grant=None,
                 waba_id=None, send_wait_seconds=30):
        if not phone_number_id or not token:
            raise ValueError("phone_number_id and token required")
        if not waba_id:
            raise ValueError("waba_id required (owner-verified test WABA)")
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
        self._grant_fn = canonical_grant
        self._waba_id = waba_id
        self._wait = send_wait_seconds
        self._last_inbound = {}
        self._seen_ids = {}   # message_id -> first-seen local ts
        self._outbox = outbox_store if outbox_store is not None \
            else MemOutboxStore()

    # -- inbound ----------------------------------------------------------
    def handle_inbound_webhook(self, raw_body, signature_header):
        """Signature -> resource binding -> replay/dedupe -> normalize."""
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
        # prune the dedupe horizon
        self._seen_ids = {m: t for m, t in self._seen_ids.items()
                          if now - t < RETRY_HORIZON_SECONDS}
        for entry in payload.get("entry", []):
            # Resource binding: a signed payload for a DIFFERENT WABA is
            # not ours - reject it, open nothing.
            if entry.get("id") != self._waba_id:
                raise WhatsAppError(
                    "wrong_resource",
                    "webhook entry WABA id does not match the configured "
                    "owner test WABA - dropped")
            for change in entry.get("changes", []):
                value = change.get("value", {})
                meta = value.get("metadata") or {}
                if meta.get("phone_number_id") != self._pnid:
                    raise WhatsAppError(
                        "wrong_resource",
                        "webhook metadata phone_number_id does not match "
                        "the configured test phone id - dropped")
                for msg in value.get("messages", []):
                    mid = msg.get("id")
                    if not mid or mid in self._seen_ids:
                        continue  # duplicate delivery / replay
                    try:
                        ts = int(float(str(msg.get("timestamp", 0))))
                    except (TypeError, ValueError):
                        continue  # invalid time: no window, no record
                    if ts > now + MAX_FUTURE_SKEW_SECONDS:
                        continue  # far-future: reject, open nothing
                    if now - ts > RETRY_HORIZON_SECONDS:
                        continue  # beyond Meta's documented retry horizon
                    self._seen_ids[mid] = now
                    sender = msg.get("from", "")
                    text = (msg.get("text") or {}).get("body")
                    norm = InboundMessage(sender, mid, ts,
                                          msg.get("type", "unknown"), text)
                    if sender in self._bindings:
                        # window math itself rejects >24h-old timestamps
                        self._last_inbound[sender] = ts
                    out.append(norm)
        return out

    def note_inbound(self, recipient):
        self._require_bound(recipient)
        self._last_inbound[recipient] = self._clock()

    def in_window(self, recipient):
        ts = self._last_inbound.get(recipient)
        return ts is not None and self._clock() - ts < WINDOW_SECONDS

    # -- outbound ---------------------------------------------------------
    def send_text(self, recipient, text, idempotency_key=None):
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
        self._require_bound(recipient)
        req = CanonicalRequest(recipient, "template",
                               {"messaging_product": "whatsapp",
                                "recipient_type": "individual",
                                "to": recipient, "type": "template",
                                "template": {"name": template,
                                             "language": {"code": language}}},
                               idempotency_key)
        return self._dispatch(req)

    def _grant(self, req):
        """Canonical permission handoff: no grant, no transport."""
        if self._grant_fn is None:
            raise WhatsAppError(
                "no_canonical_grant",
                "no canonical grant function configured - the adapter "
                "cannot send without a canonical permission handoff")
        grant = self._grant_fn(req)
        if (not isinstance(grant, dict)
                or grant.get("idempotency_key") != req.idempotency_key
                or grant.get("to") != req.to):
            raise WhatsAppError(
                "bad_grant",
                "canonical grant does not echo this exact request "
                "(idempotency key + recipient) - refused before transport")
        return grant

    def _await_terminal(self, key):
        deadline = time.time() + self._wait
        while time.time() < deadline:
            rec = self._outbox.get(key)
            if rec is not None and rec.get("state") in (
                    "sent", "failed", "unknown"):
                return rec
            time.sleep(0.005)
        return self._outbox.get(key) or {"state": "in_flight"}

    def _dispatch(self, req):
        grant = self._grant(req)
        record = {"state": "claimed",
                  "request": {"to": req.to, "type": req.type,
                              "body": req.body},
                  "grant_id": grant.get("grant_id"),
                  "attempts": 1,
                  "claimed_at": self._clock()}
        if not self._outbox.claim(req.idempotency_key, record):
            # Someone else holds/held this key: share their outcome,
            # never produce a second effect.
            prior = self._await_terminal(req.idempotency_key)
            state = prior.get("state")
            if state == "sent":
                r = prior["response"]
                return CanonicalResponse(True, r["status"],
                                         wamid=r.get("wamid"),
                                         raw=r.get("raw"), duplicate=True,
                                         grant_id=prior.get("grant_id"))
            if state == "unknown":
                raise WhatsAppError(
                    "send_unknown",
                    "a prior attempt's transport response was lost; the "
                    "message may or may not have been sent - resolve via "
                    "receipts before any retry; never blind resend")
            if state == "failed":
                err = prior.get("last_error") or {}
                raise WhatsAppError(err.get("code", "failed"),
                                    "prior attempt failed definitively: "
                                    + str(err.get("message")))
            raise WhatsAppError("send_in_flight",
                                "an identical send is still in flight")
        try:
            status, data = self._t(
                "POST", f"/v23.0/{self._pnid}/messages",
                {"Authorization": f"Bearer {self._tok}"}, req.body)
        except Exception as e:
            # LOST RESPONSE: outcome unknown. Never blind retry.
            record["state"] = "unknown"
            record["last_error"] = f"transport_lost: {e!r}"
            self._outbox.put(req.idempotency_key, record)
            raise WhatsAppError(
                "send_unknown",
                "transport response lost; the message may or may not have "
                "been sent - resolve via receipts before any retry; never "
                "blind resend") from e
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
        return CanonicalResponse(True, status, wamid=wamid, raw=data,
                                 grant_id=grant.get("grant_id"))

    def outbox_record(self, idempotency_key):
        return self._outbox.get(idempotency_key)

    def _require_bound(self, recipient):
        if recipient not in self._bindings:
            raise WhatsAppError(131030, ERROR_MAP[131030][1])
