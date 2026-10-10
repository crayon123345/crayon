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

# Per-CODE failure classification, grounded in the official Cloud API
# error-codes reference
# (https://developers.facebook.com/documentation/business-messaging/whatsapp/support/error-codes,
# live-checked 2026-10-10). RULE: every code's behavior traces to a quoted
# reference line; anything unsourceable is UNKNOWN - no retry permitted.
# A 4xx response is always a definitive RESPONSE (the message certainly
# did not send); the class governs whether ANY resend is permitted:
# - retryable: the reference says try-again-later; EXPLICIT retry only
#   (no automatic retry path exists), after any reference-stated wait.
# - definitive: the reference says do-not-retry / fix-the-cause; retry=True
#   is REFUSED (zero transport) - the same content is never resent.
# - unknown: unlisted or out-of-messaging-scope codes; persisted as its
#   OWN state (unknown_failure), never collapsed into "failed"; NO retry
#   permitted, explicit or otherwise.
RETRYABLE_CODES = frozenset({
    80007,    # "Try again later or reduce the frequency or amount of API
              #  queries the app is making."
    130429,   # "Try again later or reduce the frequency with which the
              #  app sends messages."
    131056,   # "Wait and retry the operation, if you intend to send
              #  messages to the same phone number."
    131000,   # "Try again. If the error persists, open a Direct Support
              #  ticket." (diagnostic EXPLICIT retry only, never automatic)
})
# Codes the reference allows only AFTER a stated wait: code -> seconds.
RETRY_AFTER_SECONDS = {
    131049: 24 * 3600,  # "...wait at least 24 hours before resending the
                        #  template message. Doing so will only result in
                        #  another error response..."
}
DEFINITIVE_CODES = frozenset({
    100,      # unsupported/misspelled parameters
    130403,   # "Do not retry. Unblock the WhatsApp user to resume
              #  sending messages to them."
    130472,   # marketing experiment exclusion
    131005,   # permission not granted
    131008,   # missing required parameter
    131009,   # invalid parameter value
    131021,   # sender == recipient
    131026,   # undeliverable (not a WhatsApp number etc.)
    131042,   # payment method error
    131045,   # phone number registration error
    131047,   # "Send the recipient a template message instead."
    131050,   # "Do not retry sending messages to this user as they will
              #  not be received."
    131051,   # unsupported message type
    132001,   # template does not exist in this language
})
# NOT classified for messaging (treated as unknown-no-retry for sends):
# - 141006 / 141010: absent from the current error-codes reference.
# - 131048: reference line ("Check your quality status in the WhatsApp
#   Manager...") carries NO retry instruction.
# - 131064: "automatically lifted after the enforcement period" carries
#   NO retry instruction.
# - 131030: its quotable source is the CALLING troubleshooting doc -
#   calling-scope, cannot classify MESSAGING behavior.
# - 133016: registration/deregistration scope, not messaging.


def failure_class(code):
    """'retryable' / 'definitive' / 'unknown' per the official error-codes
    reference. Unlisted or out-of-scope codes are UNKNOWN: no retry
    permitted (fail closed with an honest label)."""
    if code in RETRYABLE_CODES or code in RETRY_AFTER_SECONDS:
        return "retryable"
    if code in DEFINITIVE_CODES:
        return "definitive"
    return "unknown"


def retry_after_seconds(code):
    """Reference-stated wait before an explicit retry, else 0."""
    return RETRY_AFTER_SECONDS.get(code, 0)


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

    def claim(self, key, record, claimant, allow_retry=False, now=None):
        """Atomic claim. Content is compared BEFORE any mutation, on every
        path: same key + DIFFERENT content raises StoreConflict with the
        prior record untouched (never overwritten, never a second effect).
        Returns "claimed_fresh"; "claimed_retry" (RETRYABLE-class failed
        prior + explicit allow_retry + any reference-stated wait elapsed;
        attempts/history carried forward atomically); "failed_blocked"
        (retryable prior, no explicit retry); "definitive_blocked"
        (definitive prior - the reference says do not resend);
        "unknown_blocked" (unknown_failure prior - never resent); "wait_blocked"
        (reference-stated wait not yet elapsed); or "held"."""
        def fn(rows):
            prior = rows.get(key)
            if prior is None:
                rows[key] = record
                return "claimed_fresh"
            if prior.get("request") != record.get("request"):
                raise StoreConflict(key)  # compare first: ZERO mutation
            if prior.get("state") == "unknown_failure":
                return "unknown_blocked"  # never resent, even explicit
            if prior.get("state") == "failed":
                fclass = prior.get("failure_class")
                if fclass == "definitive":
                    return "definitive_blocked"  # reference: do not resend
                if not allow_retry:
                    return "failed_blocked"
                wait = prior.get("retry_after_seconds", 0)
                if wait and now is not None and \
                        now < prior.get("finished_at", 0) + wait:
                    return "wait_blocked"  # reference-stated wait not met
                record["attempts"] = prior.get("attempts", 0) + 1
                record["history"] = prior.get("history", []) + [{
                    "state": prior.get("state"),
                    "last_error": prior.get("last_error"),
                    "failure_class": prior.get("failure_class"),
                    "finished_at": prior.get("finished_at")}]
                rows[key] = record
                return "claimed_retry"
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

    def apply_inbound(self, messages, statuses, now, window_senders,
                      horizon, ns):
        """ONE atomic op for a whole webhook entry set: dedupe marks,
        monotonic window extends, durable receipts, and horizon pruning
        all commit together or not at all. All state is namespaced by ns
        (resource: WABA+phone id) - a window or dedupe mark under one
        resource NEVER applies to another, even on a shared store file.
        Returns the accepted (non-duplicate) messages."""
        seen_p, win_p, rcpt_p = (f"seen:{ns}:", f"win:{ns}:",
                                 f"rcpt:{ns}:")
        def fn(rows):
            # prune dedupe + receipt horizon inside the same transaction
            for k in [k for k in rows
                      if k.startswith((seen_p, rcpt_p))
                      and now - (rows[k] or {}).get("ts", 0) >= horizon]:
                del rows[k]
            for k in [k for k in rows
                      if k.startswith(f"inb:{ns}:")
                      and (rows[k] or {}).get("handoff") == "done"
                      and now - (rows[k] or {}).get("ts", 0) >= horizon]:
                del rows[k]  # only ACKED inbox rows prune
            accepted = []
            for m in messages:
                k = seen_p + m["id"]
                if k in rows:
                    continue  # duplicate delivery: idempotent
                rows[k] = {"ts": now}
                sender = m.get("from", "")
                if sender in window_senders:
                    ts = int(float(str(m["timestamp"])))
                    wk = win_p + sender
                    prior = rows.get(wk)
                    if prior is None or ts > prior.get("ts", 0):
                        rows[wk] = {"ts": ts}  # monotonic: forward only
                # LOST-INBOUND GUARD: the normalized payload is persisted
                # DURABLY in the SAME transaction as the seen-mark, with a
                # handoff-pending state - a crash after the mark but before
                # the canonical handoff NEVER loses the message; unacked
                # inbox records are replayable after restart.
                rows[f"inb:{ns}:" + m["id"]] = {
                    "ts": now, "handoff": "pending",
                    "payload": {"from": m.get("from", ""),
                                "id": m["id"],
                                "timestamp": m.get("timestamp"),
                                "type": m.get("type", "unknown"),
                                "text": (m.get("text") or {}).get("body")}}
                accepted.append(m)
            for st in statuses:
                sid = st.get("id")
                if sid:
                    send_key = next(
                        (k for k in rows
                         if k.startswith(f"send:{ns}:")
                         and (rows[k] or {}).get("receipt", {})
                         .get("wamid") == sid), None)
                    rows[rcpt_p + sid] = {
                        "ts": now, "status": st, "send_key": send_key}
            return accepted
        return self._mutate(fn)

    def inbox_pending(self, ns):
        """Unacked inbound payloads for this namespace, oldest first."""
        return self._mutate(lambda rows: [
            dict(v["payload"], _mid=k[len(f"inb:{ns}:"):])
            for k, v in sorted(rows.items(),
                               key=lambda kv: (kv[1] or {}).get("ts", 0))
            if k.startswith(f"inb:{ns}:")
            and (v or {}).get("handoff") == "pending"])

    def inbox_ack(self, ns, mid):
        """Atomic handoff ack: pending -> done. Done rows are pruned at
        the dedupe horizon; pending rows are NEVER pruned (owed handoff)."""
        def fn(rows):
            k = f"inb:{ns}:" + mid
            if k in rows:
                rows[k] = dict(rows[k], handoff="done")
        self._mutate(fn)

    def receipt(self, wamid):
        return self._mutate(lambda rows: rows.get("rcpt:" + wamid))


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

    def claim(self, key, record, claimant, allow_retry=False, now=None):
        with self._lk:
            prior = self._rows.get(key)
            if prior is None:
                self._rows[key] = record
                return "claimed_fresh"
            if prior.get("request") != record.get("request"):
                raise StoreConflict(key)  # compare first: ZERO mutation
            if prior.get("state") == "unknown_failure":
                return "unknown_blocked"  # never resent, even explicit
            if prior.get("state") == "failed":
                fclass = prior.get("failure_class")
                if fclass == "definitive":
                    return "definitive_blocked"
                if not allow_retry:
                    return "failed_blocked"
                wait = prior.get("retry_after_seconds", 0)
                if wait and now is not None and \
                        now < prior.get("finished_at", 0) + wait:
                    return "wait_blocked"
                record["attempts"] = prior.get("attempts", 0) + 1
                record["history"] = prior.get("history", []) + [{
                    "state": prior.get("state"),
                    "last_error": prior.get("last_error"),
                    "failure_class": prior.get("failure_class"),
                    "finished_at": prior.get("finished_at")}]
                self._rows[key] = record
                return "claimed_retry"
            return "held"

    def put(self, key, record):
        with self._lk:
            self._rows[key] = record

    def delete(self, key):
        with self._lk:
            self._rows.pop(key, None)

    def apply_inbound(self, messages, statuses, now, window_senders,
                      horizon, ns):
        seen_p, win_p, rcpt_p = (f"seen:{ns}:", f"win:{ns}:",
                                 f"rcpt:{ns}:")
        with self._lk:
            for k in [k for k in self._rows
                      if k.startswith((seen_p, rcpt_p))
                      and now - (self._rows[k] or {}).get("ts", 0) >= horizon]:
                del self._rows[k]
            for k in [k for k in self._rows
                      if k.startswith(f"inb:{ns}:")
                      and (self._rows[k] or {}).get("handoff") == "done"
                      and now - (self._rows[k] or {}).get("ts", 0) >= horizon]:
                del self._rows[k]  # only ACKED inbox rows prune
            accepted = []
            for m in messages:
                k = seen_p + m["id"]
                if k in self._rows:
                    continue
                self._rows[k] = {"ts": now}
                sender = m.get("from", "")
                if sender in window_senders:
                    ts = int(float(str(m["timestamp"])))
                    wk = win_p + sender
                    prior = self._rows.get(wk)
                    if prior is None or ts > prior.get("ts", 0):
                        self._rows[wk] = {"ts": ts}
                accepted.append(m)
                self._rows[f"inb:{ns}:" + m["id"]] = {
                    "ts": now, "handoff": "pending",
                    "payload": {"from": m.get("from", ""),
                                "id": m["id"],
                                "timestamp": m.get("timestamp"),
                                "type": m.get("type", "unknown"),
                                "text": (m.get("text") or {}).get("body")}}
            for st in statuses:
                sid = st.get("id")
                if sid:
                    send_key = next(
                        (k for k in self._rows
                         if k.startswith(f"send:{ns}:")
                         and (self._rows[k] or {}).get("receipt", {})
                         .get("wamid") == sid), None)
                    self._rows[rcpt_p + sid] = {
                        "ts": now, "status": st, "send_key": send_key}
            return accepted

    def receipt(self, wamid):
        with self._lk:
            return self._rows.get("rcpt:" + wamid)

    def inbox_pending(self, ns):
        with self._lk:
            return [dict(v["payload"], _mid=k[len(f"inb:{ns}:"):])
                    for k, v in sorted(self._rows.items(),
                                       key=lambda kv: (kv[1] or {}).get("ts", 0))
                    if k.startswith(f"inb:{ns}:")
                    and (v or {}).get("handoff") == "pending"]

    def inbox_ack(self, ns, mid):
        with self._lk:
            k = f"inb:{ns}:" + mid
            if k in self._rows:
                self._rows[k] = dict(self._rows[k], handoff="done")


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

    @property
    def statuses(self):
        """Durable receipt log (rcpt: rows in the store). Receipts are
        never messages and never open windows. RECONCILIATION BOUNDARY:
        the adapter only persists raw status receipts keyed by wamid and
        exposes them; matching receipts to outbox records and resolving
        'unknown' sends is the canonical engine's job at activation."""
        pre = f"rcpt:{self._ns}:"
        # store.receipt() re-applies the "rcpt:" prefix, so strip only that
        return [self._outbox.receipt(k[5:])["status"]
                for k in self._outbox.keys_with_prefix(pre)
                if self._outbox.receipt(k[5:])]

    def receipt(self, wamid):
        """Durable receipt lookup by Meta message id (wamid), scoped to
        THIS owner+resource: a wamid recorded under another resource is
        invisible here (a wamid from A means nothing to B's outbox)."""
        rec = self._outbox.receipt(f"{self._ns}:{wamid}")
        return rec.get("status") if rec else None

    def inbox_pending(self):
        """Unacked inbound payloads for THIS owner+resource, oldest
        first. Durable across restart: a crash after the atomic
        seen-mark but before the canonical handoff NEVER loses the
        message - replay these and inbox_ack each on handoff."""
        return self._outbox.inbox_pending(self._ns)

    def inbox_ack(self, message_id):
        """Mark one inbound payload handed off to canonical. Duplicate
        webhook deliveries after ack are deduped by the seen-mark and
        produce ZERO re-handoff."""
        self._outbox.inbox_ack(self._ns, message_id)

    @property
    def resource(self):
        return f"whatsapp:waba:{self._waba_id}:phone:{self._pnid}"

    @property
    def _ns(self):
        """Store namespace for ALL adapter state (outbox sends, dedupe
        marks, windows, receipts): owner + the exact resource. Two
        adapters sharing one store file NEVER share sends, windows,
        dedupe marks or receipts across resources OR owners - a stale
        success under resource A is invisible to resource B, and a
        window under owner X is closed for owner Y."""
        return f"{self._owner}:{self._waba_id}:{self._pnid}"

    # -- durable replay/window state ---------------------------------------
    def _window_get(self, sender):
        rec = self._outbox.get(f"win:{self._ns}:{sender}")
        return rec.get("ts") if rec else None

    def _window_extend(self, sender, ts):
        """MONOTONIC: a window only moves forward. An older-timestamped
        inbound never regresses or closes a newer open window."""
        prior = self._window_get(sender)
        if prior is None or ts > prior:
            self._outbox.put(f"win:{self._ns}:{sender}", {"ts": ts})

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

        # PASS 2: ONE atomic store transaction for the whole entry set -
        # dedupe marks, monotonic window extends, durable receipts and
        # horizon pruning commit together or not at all. Per-message
        # outcomes inside are idempotent drops (duplicate delivery),
        # never failures.
        all_msgs = [m for entry in entries for change in
                    entry.get("changes", [])
                    for m in change.get("value", {}).get("messages", [])]
        all_statuses = [st for entry in entries for change in
                        entry.get("changes", [])
                        for st in change.get("value", {}).get("statuses")
                        or []]
        accepted = self._outbox.apply_inbound(
            all_msgs, all_statuses, now, set(self._bindings),
            RETRY_HORIZON_SECONDS, self._ns)
        out = []
        for m in accepted:
            ts = int(float(str(m["timestamp"])))
            sender = m.get("from", "")
            text = (m.get("text") or {}).get("body")
            # Media: normalized with its type; content is NOT downloaded
            # (the adapter has no media fetch) - honest scaffold limit.
            out.append(InboundMessage(sender, m["id"], ts,
                                      m.get("type", "unknown"), text))
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
    def send_text(self, recipient, text, idempotency_key=None, retry=False):
        self._require_bound(recipient)
        if not self.in_window(recipient):
            raise WhatsAppError(131047, ERROR_MAP[131047][1])
        req = CanonicalRequest(recipient, "text",
                               {"messaging_product": "whatsapp",
                                "recipient_type": "individual",
                                "to": recipient, "type": "text",
                                "text": {"body": text}},
                               idempotency_key)
        return self._dispatch(req, retry=retry)

    def send_template(self, recipient, template, language="en_US",
                      idempotency_key=None, retry=False):
        self._require_bound(recipient)
        req = CanonicalRequest(recipient, "template",
                               {"messaging_product": "whatsapp",
                                "recipient_type": "individual",
                                "to": recipient, "type": "template",
                                "template": {"name": template,
                                             "language": {"code": language}}},
                               idempotency_key)
        return self._dispatch(req, retry=retry)

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
                    "sent", "failed", "unknown", "unknown_failure"):
                return rec
            time.sleep(0.005)
        return self._outbox.get(key) or {"state": "in_flight"}

    def _dispatch(self, req, retry=False):
        grant = self._grant(req)  # every refusal lands before the store
        key = f"send:{self._ns}:" + req.idempotency_key
        record = {"state": "claimed",
                  "request": {"to": req.to, "type": req.type,
                              "body": req.body},
                  "grant_id": grant.get("grant_id"),
                  "claimant": self._instance,
                  "attempts": 1,
                  "claimed_at": self._clock()}
        try:
            result = self._outbox.claim(key, record, self._instance,
                                        allow_retry=retry,
                                        now=self._clock())
        except StoreConflict:
            raise WhatsAppError(
                "idempotency_conflict",
                "this idempotency key was already used with DIFFERENT "
                "content - conflict, never a stale success; pick a new "
                "key") from None
        if result == "unknown_blocked":
            raise WhatsAppError(
                "unknown_failure_no_retry",
                "this key's prior failure is UNKNOWN-outcome (the official "
                "error-codes reference does not clearly classify the "
                "code) - NO retry is permitted, explicit or otherwise; "
                "diagnose from the actual Meta response first")
        if result == "failed_blocked":
            raise WhatsAppError(
                "explicit_retry_required",
                "this key's prior attempt failed; re-sending requires the "
                "EXPLICIT retry flag (retry=True) so a resend is always a "
                "deliberate act, with attempts/history preserved")
        if result == "definitive_blocked":
            raise WhatsAppError(
                "definitive_failure_no_retry",
                "this key's prior failure is DEFINITIVE per the official "
                "error-codes reference (do-not-retry / fix-the-cause) - "
                "the same content is NEVER resent; fix the cause and use "
                "a new idempotency key for changed content")
        if result == "wait_blocked":
            raise WhatsAppError(
                "retry_wait_not_elapsed",
                "the official reference requires waiting before resending "
                "this code (e.g. 131049: at least 24 hours) - the wait "
                "has not elapsed; no resend")
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
            record["finished_at"] = self._clock()
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
            record["finished_at"] = self._clock()
            self._outbox.put(key, record)
            raise WhatsAppError(
                "send_unknown",
                f"Meta {status} after the request may have landed - "
                "treated as UNKNOWN; resolve via receipts before any "
                "retry; never blind resend")
        if status >= 400:
            # 4xx: a definitive RESPONSE (the message certainly did not
            # send); the PER-CODE class governs whether any resend is
            # permitted, and claim() enforces it (zero transport).
            err = (data or {}).get("error", {})
            code = err.get("code", 0)
            fclass = failure_class(code)
            # Unknown-class failures persist as their OWN state
            # (unknown_failure), never collapsed into "failed".
            record["state"] = ("unknown_failure" if fclass == "unknown"
                               else "failed")
            record["failure_class"] = fclass
            record["retry_after_seconds"] = retry_after_seconds(code)
            record["last_error"] = {"code": code,
                                    "message": err.get("message")}
            record["finished_at"] = self._clock()
            self._outbox.put(key, record)
            meaning, guidance = ERROR_MAP.get(
                code, (err.get("message", "unknown Meta error"),
                       "Check the official Cloud API error reference."))
            cls_note = {"retryable":
                        "Retryable per the official error-codes reference "
                        "(back off, then an EXPLICIT retry may resend the "
                        "same content).",
                        "definitive":
                        "Definitive per the official error-codes reference "
                        "(fix the cause first; do not resend unchanged).",
                        "unknown":
                        "UNKNOWN outcome: the official error-codes "
                        "reference does not clearly classify this code - "
                        "no retry is permitted; diagnose from the actual "
                        "response."}[fclass]
            raise WhatsAppError(code, f"{meaning} - {guidance} {cls_note}")
        wamid = ((data or {}).get("messages") or [{}])[0].get("id")
        if not isinstance(wamid, str) or not wamid.strip():
            # FALSE COMPLETION GUARD: an HTTP 200 without a valid message
            # id is NOT a success - Meta's contract returns messages[0].id
            # on a real send. Never persist SENT without a bound wamid;
            # the outcome is UNKNOWN (the request may have landed).
            record["state"] = "unknown"
            record["last_error"] = {"code": "success_without_wamid",
                                    "message": repr(data)}
            record["finished_at"] = self._clock()
            self._outbox.put(key, record)
            raise WhatsAppError(
                "send_unknown",
                "Meta returned 2xx WITHOUT a message id - NOT a confirmed "
                "send; treated as UNKNOWN (no blind retry; resolve via "
                "receipts)")
        record["state"] = "sent"
        record["response"] = {"status": status, "wamid": wamid, "raw": data}
        record["receipt"] = {"wamid": wamid, "sent_at": self._clock()}
        record["finished_at"] = self._clock()
        self._outbox.put(key, record)
        return CanonicalResponse(True, status, wamid=wamid, raw=data,
                                 grant_id=grant.get("grant_id"))

    def outbox_record(self, idempotency_key):
        return self._outbox.get(f"send:{self._ns}:" + idempotency_key)

    def _require_bound(self, recipient):
        if recipient not in self._bindings:
            raise WhatsAppError(131030, ERROR_MAP[131030][1])
