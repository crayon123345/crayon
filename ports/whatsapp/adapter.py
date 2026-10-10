"""Synthetic WhatsApp Cloud API staging adapter (Meta TEST WABA only).

STAGING ONLY: transport is injected; no live Meta calls, no unofficial
libraries, no automation of any personal number, no paid routes.

Architecture (rework 2 after cross-review):

- CANONICAL PERMISSION HANDOFF, FULLY BOUND: every outbound send first asks
  the injected canonical grant function for permission. The grant must
  (a) carry allowed:true - anything else refuses with ZERO transport;
  (b) echo-bind ALL of: idempotency key, recipient, OWNER, RESOURCE
  (the exact test WABA + phone id), ACTION (send:<type>), and CONTENT
  (sha256 of the exact send body); (c) carry a finite, unexpired
  expires_at. No grant function, no send.
- CONTENT-BOUND IDEMPOTENCY: the same idempotency key with DIFFERENT
  content is a conflict error, never a stale success. Same key + same
  content shares the original outcome (exactly one effect).
- DURABLE OUTBOX, ATOMIC PUBLISH: the JSON store is written
  temp-file-then-rename (fsync file + dir) under an exclusive flock on a
  sidecar lock file, and FAILS CLOSED on corruption - including an
  existing-but-EMPTY store file, which is a crash artifact (a healthy
  store always contains at least "{}"), never a fresh start.
- States: claimed -> sent | failed | unknown. Only 4xx is definitively
  failed (explicitly re-claimable). A LOST RESPONSE or a 5xx is UNKNOWN:
  the request may have landed, so it is never blind-retried. A "claimed"
  record left by ANOTHER adapter instance (crash recovery) is unknown
  too - a new instance never resurrects it into a resend.
- Inbound webhooks: signature, then ALL-OR-NOTHING validation of the
  whole entry set (resource binding + per-message shape/time) BEFORE any
  state change; then idempotent application. Dedupe by message id over
  Meta's documented 7-day retry horizon, PERSISTED in the durable store
  (survives restart). Status receipts are recorded explicitly and never
  open windows; media messages normalize with their type and no fetched
  content (honestly: the adapter does not download media).
- The 24h service window is MONOTONIC and PERSISTED: an older-timestamped
  inbound can only extend a window forward, never regress or close a
  newer one, and the state survives restart.
- owner_confirmed on a RecipientBinding is required constructor input,
  not proof by itself; the webhook resource binding is the inbound-side
  enforcement.
- Error mapping never invents a payment requirement. 141006 is Meta's
  generic business-level block label; per the official Cloud API overview
  ("Test resources"), test WABAs "don't require a payment method on file
  in order to send template messages" - card-free fixes first. 131000 is
  a GENERIC unknown error and is treated as such.
"""
import hashlib
import hmac
import json
import math
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
             "Complete business info; test WABAs are exempt from payment "
             "methods per the official overview."),
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
    """The durable store cannot be parsed (or is a crash artifact).
    Fail closed; never reset, never silently start over."""


class StoreConflict(Exception):
    """Same idempotency key, different content. Never a stale success."""


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
        self.content_sha256 = hashlib.sha256(
            json.dumps(body, sort_keys=True,
                       separators=(",", ":")).encode()).hexdigest()

    @property
    def action(self):
        return f"send:{self.type}"


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


def _strict_finite(value):
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    v = float(value)
    return v if math.isfinite(v) else None


class JsonOutboxStore:
    """Durable JSON store: exclusive flock on a sidecar lock file around
    read-modify-write, ATOMIC PUBLISH via temp-file + fsync + rename +
    dir fsync, reload on every op. FAILS CLOSED (OutboxCorrupt) on
    unparseable content - and on an existing-but-empty file, which is a
    crash artifact (a healthy store always holds at least "{}"), never a
    fresh start. A crash mid-write can therefore never truncate the real
    store nor present an empty one as new."""

    def __init__(self, path):
        self.path = str(path)
        self._lock_path = self.path + ".lock"
        self._mem_lock = threading.Lock()
        if not os.path.exists(self.path):
            self._mutate(lambda rows: None)  # initializes to "{}"
        else:
            self._read_rows()  # fail closed at construction

    def _read_rows(self):
        with open(self.path) as f:
            raw = f.read()
        if not raw.strip():
            raise OutboxCorrupt(
                f"store {self.path} exists but is EMPTY - a crash "
                "artifact, not a fresh store; refusing to continue "
                "(never silently start over)")
        try:
            return json.loads(raw)
        except ValueError:
            raise OutboxCorrupt(
                f"store {self.path} is corrupt - refusing to continue "
                "(never silently reset)") from None

    def _mutate(self, fn):
        import fcntl
        with self._mem_lock:
            with open(self._lock_path, "a") as lk:
                fcntl.flock(lk, fcntl.LOCK_EX)
                try:
                    rows = self._read_rows() if os.path.exists(self.path) \
                        else {}
                    result = fn(rows)
                    tmp = f"{self.path}.tmp.{os.getpid()}"
                    with open(tmp, "w") as t:
                        json.dump(rows, t)
                        t.flush()
                        os.fsync(t.fileno())
                    os.replace(tmp, self.path)
                    dfd = os.open(os.path.dirname(self.path) or ".",
                                  os.O_RDONLY)
                    try:
                        os.fsync(dfd)
                    finally:
                        os.close(dfd)
                    return result
                finally:
                    fcntl.flock(lk, fcntl.LOCK_UN)

    def get(self, key):
        return self._mutate(lambda rows: rows.get(key))

    def keys_with_prefix(self, prefix):
        return self._mutate(
            lambda rows: [k for k in rows if k.startswith(prefix)])

    def claim(self, key, record, claimant):
        """Atomically insert if absent OR if the prior record is a
        definitively-failed send being retried. Returns "claimed" iff WE
        hold the claim, "held" if the same content was already claimed.
        Same key + DIFFERENT content raises StoreConflict."""
        def fn(rows):
            prior = rows.get(key)
            if prior is None or prior.get("state") == "failed":
                rows[key] = record
                return "claimed"
            if prior.get("request") != record.get("request"):
                raise StoreConflict(key)
            return "held"
        return self._mutate(fn)

    def put(self, key, record):
        def fn(rows):
            rows[key] = record
        self._mutate(fn)

    def delete(self, key):
        def fn(rows):
            rows.pop(key, None)
        self._mutate(fn)


class MemOutboxStore:
    """In-memory store with the same contract (staging default;
    NOT durable - replay/window state dies with the process)."""

    def __init__(self):
        self._rows = {}
        self._lk = threading.Lock()

    def get(self, key):
        with self._lk:
            return self._rows.get(key)

    def keys_with_prefix(self, prefix):
        with self._lk:
            return [k for k in self._rows if k.startswith(prefix)]

    def claim(self, key, record, claimant):
        with self._lk:
            prior = self._rows.get(key)
            if prior is None or prior.get("state") == "failed":
                self._rows[key] = record
                return "claimed"
            if prior.get("request") != record.get("request"):
                raise StoreConflict(key)
            return "held"

    def put(self, key, record):
        with self._lk:
            self._rows[key] = record

    def delete(self, key):
        with self._lk:
            self._rows.pop(key, None)


class WhatsAppStagingAdapter:
    """Transport-injected test-WABA sender with a fully-bound canonical
    grant handoff and a durable, atomic, content-bound outbox.

    transport(method, path, headers, body) -> (status, dict)
    canonical_grant(request) -> grant dict; REQUIRED. The grant must
    carry allowed:true, echo the request's idempotency key / recipient /
    owner / resource / action / content hash, and be unexpired.
    owner: the canonical principal this adapter acts for (grant binding).
    waba_id: the owner-verified test WABA id (inbound resource binding).
    """

    def __init__(self, transport, phone_number_id, token, bindings,
                 clock=time.time, max_recipients=DEFAULT_MAX_TEST_RECIPIENTS,
                 app_secret=None, outbox_store=None, canonical_grant=None,
                 waba_id=None, owner=None, send_wait_seconds=30):
        if not phone_number_id or not token:
            raise ValueError("phone_number_id and token required")
        if not waba_id:
            raise ValueError("waba_id required (owner-verified test WABA)")
        if not owner:
            raise ValueError("owner required (canonical grant binding)")
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
        self._owner = owner
        self._wait = send_wait_seconds
        self._instance = uuid.uuid4().hex
        self._outbox = outbox_store if outbox_store is not None \
            else MemOutboxStore()
        self.statuses = []  # explicit receipt log (never windows)

    @property
    def resource(self):
        return f"whatsapp:waba:{self._waba_id}:phone:{self._pnid}"

    # -- durable replay/window state ---------------------------------------
    def _seen_get(self, mid):
        return self._outbox.get(f"seen:{mid}")

    def _seen_put(self, mid, ts):
        self._outbox.put(f"seen:{mid}", {"ts": ts})

    def _seen_prune(self, now):
        for k in self._outbox.keys_with_prefix("seen:"):
            rec = self._outbox.get(k)
            if rec is None or now - rec.get("ts", 0) >= RETRY_HORIZON_SECONDS:
                self._outbox.delete(k)

    def _window_get(self, sender):
        rec = self._outbox.get(f"win:{sender}")
        return rec.get("ts") if rec else None

    def _window_extend(self, sender, ts):
        """MONOTONIC: a window only moves forward. An older-timestamped
        inbound never regresses or closes a newer open window."""
        prior = self._window_get(sender)
        if prior is None or ts > prior:
            self._outbox.put(f"win:{sender}", {"ts": ts})

    # -- inbound ----------------------------------------------------------
    def handle_inbound_webhook(self, raw_body, signature_header):
        """Signature -> WHOLE-BATCH validation (all-or-nothing, zero
        mutation on failure) -> idempotent application."""
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
        entries = payload.get("entry", [])
        now = self._clock()

        # PASS 1: validate the ENTIRE entry set. Any failure raises BEFORE
        # any state change: a batch never partially mutates.
        for entry in entries:
            if entry.get("id") != self._waba_id:
                raise WhatsAppError(
                    "wrong_resource",
                    "webhook entry WABA id does not match the configured "
                    "owner test WABA - whole batch dropped, no state change")
            for change in entry.get("changes", []):
                value = change.get("value", {})
                meta = value.get("metadata") or {}
                if meta.get("phone_number_id") != self._pnid:
                    raise WhatsAppError(
                        "wrong_resource",
                        "webhook metadata phone_number_id does not match "
                        "the configured test phone id - whole batch "
                        "dropped, no state change")
                for m in value.get("messages", []):
                    self._validate_message(m, now)

        # PASS 2: apply. Per-message outcomes here are idempotent drops
        # (duplicate delivery), never failures.
        self._seen_prune(now)
        out = []
        for entry in entries:
            for change in entry.get("changes", []):
                value = change.get("value", {})
                # Status receipts: recorded explicitly, never messages,
                # never windows.
                for st in value.get("statuses", []) or []:
                    self.statuses.append(st)
                for m in value.get("messages", []):
                    mid = m["id"]
                    if self._seen_get(mid) is not None:
                        continue  # duplicate delivery / retry: idempotent
                    ts = int(float(str(m["timestamp"])))
                    self._seen_put(mid, now)
                    sender = m.get("from", "")
                    text = (m.get("text") or {}).get("body")
                    # Media: normalized with its type; content is NOT
                    # downloaded (the adapter has no media fetch) - honest.
                    norm = InboundMessage(sender, mid, ts,
                                          m.get("type", "unknown"), text)
                    if sender in self._bindings:
                        self._window_extend(sender, ts)  # monotonic
                    out.append(norm)
        return out

    @staticmethod
    def _validate_message(m, now):
        mid = m.get("id")
        if not mid:
            raise WhatsAppError("invalid_message",
                                "message without an id - whole batch "
                                "dropped, no state change")
        try:
            ts = int(float(str(m.get("timestamp", ""))))
        except (TypeError, ValueError):
            raise WhatsAppError("invalid_message",
                                "unparseable timestamp - whole batch "
                                "dropped, no state change") from None
        if ts > now + MAX_FUTURE_SKEW_SECONDS:
            raise WhatsAppError("invalid_message",
                                "far-future timestamp - whole batch "
                                "dropped, no state change")
        if now - ts > RETRY_HORIZON_SECONDS:
            raise WhatsAppError("invalid_message",
                                "beyond Meta's 7-day retry horizon - whole "
                                "batch dropped, no state change")

    def note_inbound(self, recipient):
        self._require_bound(recipient)
        self._window_extend(recipient, self._clock())

    def in_window(self, recipient):
        ts = self._window_get(recipient)
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
        """Canonical permission handoff, fully bound: allowed flag, echo
        of key/recipient/owner/resource/action/content, and a live
        expiry. ANY refusal happens before transport (zero effects)."""
        if self._grant_fn is None:
            raise WhatsAppError(
                "no_canonical_grant",
                "no canonical grant function configured - the adapter "
                "cannot send without a canonical permission handoff")
        grant = self._grant_fn(req)
        if not isinstance(grant, dict):
            raise WhatsAppError("bad_grant",
                                "canonical grant is not an object - refused "
                                "before transport")
        if grant.get("allowed") is not True:
            raise WhatsAppError(
                "grant_denied",
                "canonical grant does not allow this send - refused before "
                "transport (zero effects)")
        echoes = {
            "idempotency_key": req.idempotency_key,
            "to": req.to,
            "owner": self._owner,
            "resource": self.resource,
            "action": req.action,
            "content_sha256": req.content_sha256,
        }
        for field, want in echoes.items():
            if grant.get(field) != want:
                raise WhatsAppError(
                    "bad_grant",
                    f"canonical grant does not echo-bind {field} - refused "
                    "before transport")
        exp = _strict_finite(grant.get("expires_at"))
        if exp is None:
            raise WhatsAppError(
                "bad_grant",
                "canonical grant has no finite expires_at - refused before "
                "transport")
        if exp <= self._clock():
            raise WhatsAppError(
                "grant_expired",
                "canonical grant is expired - refused before transport")
        return grant

    def _await_terminal(self, key):
        """Same-instance concurrency only: wait for OUR claim holder's
        terminal state and share the outcome (exactly one effect)."""
        deadline = time.time() + self._wait
        while time.time() < deadline:
            rec = self._outbox.get(key)
            if rec is not None and rec.get("state") in (
                    "sent", "failed", "unknown"):
                return rec
            time.sleep(0.005)
        return self._outbox.get(key) or {"state": "in_flight"}

    def _dispatch(self, req):
        grant = self._grant(req)  # every refusal lands before the store
        key = "send:" + req.idempotency_key
        record = {"state": "claimed",
                  "request": {"to": req.to, "type": req.type,
                              "body": req.body},
                  "grant_id": grant.get("grant_id"),
                  "claimant": self._instance,
                  "attempts": 1,
                  "claimed_at": self._clock()}
        try:
            result = self._outbox.claim(key, record, self._instance)
        except StoreConflict:
            raise WhatsAppError(
                "idempotency_conflict",
                "this idempotency key was already used with DIFFERENT "
                "content - conflict, never a stale success; pick a new "
                "key") from None
        if result == "held":
            prior = self._outbox.get(key)
            if (prior.get("state") == "claimed"
                    and prior.get("claimant") != self._instance):
                # Crash recovery: another (possibly dead) instance holds a
                # pending claim. Its outcome is UNKNOWN to us - never
                # resurrect it into a resend.
                raise WhatsAppError(
                    "send_unknown",
                    "a prior instance's send never recorded an outcome; "
                    "the message may or may not have been sent - resolve "
                    "via receipts before any retry; never blind resend")
            prior = self._await_terminal(key)
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
            record["last_error"] = {"code": "transport_lost",
                                    "message": repr(e)}
            self._outbox.put(key, record)
            raise WhatsAppError(
                "send_unknown",
                "transport response lost; the message may or may not have "
                "been sent - resolve via receipts before any retry; never "
                "blind resend") from e
        if status >= 500:
            # 5xx: Meta's side failed AFTER the request may have landed.
            # NOT definitive - outcome unknown, never blind retry.
            err = (data or {}).get("error", {})
            record["state"] = "unknown"
            record["last_error"] = {"code": err.get("code", status),
                                    "message": err.get("message")}
            self._outbox.put(key, record)
            raise WhatsAppError(
                "send_unknown",
                f"Meta {status} after the request may have landed - "
                "treated as UNKNOWN (only 4xx is definitive); resolve via "
                "receipts before any retry; never blind resend")
        if status >= 400:
            # 4xx: Meta definitively refused. Re-claimable explicitly.
            err = (data or {}).get("error", {})
            code = err.get("code", 0)
            record["state"] = "failed"
            record["last_error"] = {"code": code,
                                    "message": err.get("message")}
            self._outbox.put(key, record)
            meaning, guidance = ERROR_MAP.get(
                code, (err.get("message", "unknown Meta error"),
                       "Check the official Cloud API error reference."))
            raise WhatsAppError(code, f"{meaning} - {guidance}")
        wamid = ((data or {}).get("messages") or [{}])[0].get("id")
        record["state"] = "sent"
        record["response"] = {"status": status, "wamid": wamid, "raw": data}
        record["receipt"] = {"wamid": wamid, "sent_at": self._clock()}
        self._outbox.put(key, record)
        return CanonicalResponse(True, status, wamid=wamid, raw=data,
                                 grant_id=grant.get("grant_id"))

    def outbox_record(self, idempotency_key):
        return self._outbox.get("send:" + idempotency_key)

    def _require_bound(self, recipient):
        if recipient not in self._bindings:
            raise WhatsAppError(131030, ERROR_MAP[131030][1])
