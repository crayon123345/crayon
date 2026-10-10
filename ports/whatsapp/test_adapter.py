"""Synthetic tests for the WhatsApp staging adapter. No live Meta calls."""
import sys, pathlib, json, hmac, hashlib, threading, time
sys.path.insert(0, str(pathlib.Path(__file__).parent))
import pytest
from adapter import (WhatsAppStagingAdapter, WhatsAppError, ERROR_MAP,
                     RecipientBinding, JsonOutboxStore, OutboxCorrupt)

ME = "+918777566396"
OTHER = "+15551234567"
PNID = "test-phone-id"
WABA = "test-waba-id"
SECRET = "synthetic-app-secret"


class FakeMeta:
    def __init__(self, fail=None, delay=0, fail5=None):
        self.calls = []
        self.fail = fail or {}
        self.fail5 = fail5 or {}
        self.delay = delay
        self._lk = threading.Lock()
    def __call__(self, method, path, headers, body):
        assert path.startswith("/v23.0/test-phone-id/")
        assert "ticket" not in path
        if self.delay:
            time.sleep(self.delay)
        with self._lk:
            self.calls.append(body)
        to = body["to"]
        if to in self.fail:
            return 400, {"error": {"code": self.fail[to],
                                   "message": "synthetic failure"}}
        if to in self.fail5:
            return 500, {"error": {"code": 131000,
                                   "message": "synthetic 5xx"}}
        return 200, {"messages": [{"id": "wamid.test"}]}


OWNER = "owner:test"
RESOURCE = f"whatsapp:waba:{WABA}:phone:{PNID}"


def grant_fn(req):
    """A fully-bound synthetic grant: allowed, echoing key/recipient/
    owner/resource/action/content, with a live expiry."""
    return {"grant_id": "grant-1", "allowed": True,
            "idempotency_key": req.idempotency_key, "to": req.to,
            "owner": OWNER, "resource": RESOURCE, "action": req.action,
            "content_sha256": req.content_sha256,
            "expires_at": 9_999_999_999.0}


def make(now=[1000.0], fail=None, recips=(ME,), secret=SECRET, store=None,
         grant=grant_fn, waba=WABA, **mkw):
    m = FakeMeta(fail=fail, delay=mkw.pop("delay", 0),
             fail5=mkw.pop("fail5", None))
    bindings = [RecipientBinding(r, PNID, True) for r in recips]
    a = WhatsAppStagingAdapter(m, PNID, "test-token", bindings,
                               clock=lambda: now[0], app_secret=secret,
                               outbox_store=store, canonical_grant=grant,
                               waba_id=waba, owner=OWNER, **mkw)
    return m, a


def webhook(msgs, secret=SECRET, waba=WABA, pnid=PNID):
    body = json.dumps({"entry": [{"id": waba, "changes": [{"value": {
        "metadata": {"phone_number_id": pnid},
        "messages": msgs}}]}]}).encode()
    sig = "sha256=" + hmac.new(secret.encode(), body,
                               hashlib.sha256).hexdigest()
    return body, sig


def msg(mid="wam1", sender=ME, ts=1000, text="hi", type_="text"):
    m = {"id": mid, "from": sender, "timestamp": str(ts), "type": type_}
    if text is not None:
        m["text"] = {"body": text}
    return m


# -- pre-existing behavior --------------------------------------------------

def test_template_send_to_verified_recipient():
    m, a = make()
    out = a.send_template(ME, "hello_world")
    assert out.ok and out.wamid == "wamid.test"
    assert m.calls[0]["type"] == "template"
    assert m.calls[0]["to"] == ME


def test_unverified_recipient_refused_locally():
    m, a = make()
    with pytest.raises(WhatsAppError) as e:
        a.send_template(OTHER, "hello_world")
    assert e.value.code == 131030
    assert m.calls == []


def test_recipient_cap_enforced():
    with pytest.raises(ValueError):
        WhatsAppStagingAdapter(
            FakeMeta(), PNID, "t",
            [RecipientBinding(f"+1555000000{i}", PNID, True)
             for i in range(6)], waba_id=WABA, canonical_grant=grant_fn,
            owner=OWNER)


def test_freeform_blocked_outside_window():
    m, a = make()
    with pytest.raises(WhatsAppError) as e:
        a.send_text(ME, "hello")
    assert e.value.code == 131047
    assert m.calls == []


def test_freeform_allowed_inside_window():
    m, a = make()
    a.note_inbound(ME)
    out = a.send_text(ME, "hello")
    assert out.ok
    assert m.calls[0]["type"] == "text"


def test_window_closes_after_24h():
    now = [1000.0]
    m, a = make(now=now)
    a.note_inbound(ME)
    now[0] += 24 * 3600 + 1
    with pytest.raises(WhatsAppError) as e:
        a.send_text(ME, "still there?")
    assert e.value.code == 131047


def test_141006_guidance_is_card_free():
    m, a = make(fail={ME: 141006})
    with pytest.raises(WhatsAppError) as e:
        a.send_template(ME, "hello_world")
    assert "Do NOT attach a card" in e.value.guidance


def test_131000_treated_as_generic_unknown():
    m, a = make(fail={ME: 131000})
    with pytest.raises(WhatsAppError) as e:
        a.send_template(ME, "hello_world")
    assert e.value.code == 131000
    assert "generic unknown" in e.value.guidance


def test_unknown_error_maps_safely():
    m, a = make(fail={ME: 139999})
    with pytest.raises(WhatsAppError) as e:
        a.send_template(ME, "hello_world")
    assert "official Cloud API error reference" in e.value.guidance


def test_binding_to_other_phone_number_rejected():
    with pytest.raises(ValueError):
        WhatsAppStagingAdapter(FakeMeta(), PNID, "t",
                               [RecipientBinding(ME, "other-pnid", True)],
                               waba_id=WABA, canonical_grant=grant_fn,
                               owner=OWNER)


def test_unconfirmed_binding_rejected():
    with pytest.raises(ValueError):
        WhatsAppStagingAdapter(FakeMeta(), PNID, "t",
                               [RecipientBinding(ME, PNID, False)],
                               waba_id=WABA, canonical_grant=grant_fn,
                               owner=OWNER)


def test_error_map_covers_block_chain():
    for code in (141006, 141010, 131000, 131030, 131047):
        assert code in ERROR_MAP


# -- finding 1: canonical permission handoff --------------------------------

def test_no_grant_function_no_transport():
    m, a = make(grant=None)
    with pytest.raises(WhatsAppError) as e:
        a.send_template(ME, "hello_world")
    assert e.value.code == "no_canonical_grant"
    assert m.calls == []  # transport never reached


def test_grant_must_echo_exact_request():
    def bad_grant(req):
        g = grant_fn(req)
        g["idempotency_key"] = "WRONG"
        return g
    m, a = make(grant=bad_grant)
    with pytest.raises(WhatsAppError) as e:
        a.send_template(ME, "hello_world")
    assert e.value.code == "bad_grant"
    assert m.calls == []


def test_granted_send_reaches_transport_once():
    seen = []
    def spy_grant(req):
        seen.append(req.idempotency_key)
        return grant_fn(req)
    m, a = make(grant=spy_grant)
    out = a.send_template(ME, "hello_world")
    assert out.ok and out.grant_id == "grant-1"
    assert seen and len(m.calls) == 1


# -- finding 2: outbox atomicity / unknown / concurrency --------------------

def test_idempotency_key_prevents_resend():
    m, a = make()
    r1 = a.send_template(ME, "hello_world", idempotency_key="k1")
    r2 = a.send_template(ME, "hello_world", idempotency_key="k1")
    assert r1.ok and r2.duplicate is True
    assert len(m.calls) == 1


def test_lost_response_is_unknown_never_blind_retried():
    class LostMeta(FakeMeta):
        def __call__(self, *args):
            super().__call__(*args)
            raise OSError("response lost after send")
    m = LostMeta()
    a = WhatsAppStagingAdapter(m, PNID, "t",
                               [RecipientBinding(ME, PNID, True)],
                               app_secret=SECRET, canonical_grant=grant_fn,
                               waba_id=WABA, owner=OWNER)
    with pytest.raises(WhatsAppError) as e:
        a.send_template(ME, "hello_world", idempotency_key="k-lost")
    assert e.value.code == "send_unknown"
    assert a.outbox_record("k-lost")["state"] == "unknown"
    # Meta actually received it (one effect). A retry must NOT resend:
    with pytest.raises(WhatsAppError) as e2:
        a.send_template(ME, "hello_world", idempotency_key="k-lost")
    assert e2.value.code == "send_unknown"
    assert len(m.calls) == 1   # exactly one effect, ever


def test_same_key_concurrent_sends_exactly_one_effect():
    m, a = make(delay=0.05)   # force overlap
    results, errors = [], []
    barrier = threading.Barrier(3)
    def race():
        barrier.wait()
        try:
            results.append(a.send_template(ME, "hello_world",
                                           idempotency_key="k-race"))
        except WhatsAppError as e:
            errors.append(e)
    ts = [threading.Thread(target=race) for _ in range(2)]
    [t.start() for t in ts]
    barrier.wait()
    [t.join() for t in ts]
    assert len(m.calls) == 1                      # exactly one effect
    assert len(results) + len(errors) == 2
    if len(results) == 2:
        assert sum(1 for r in results if not r.duplicate) == 1
        assert sum(1 for r in results if r.duplicate) == 1


def test_failed_send_requires_explicit_retry_then_reclaims():
    m, a = make(fail={ME: 130429})
    with pytest.raises(WhatsAppError):
        a.send_template(ME, "hello_world", idempotency_key="k-f")
    assert a.outbox_record("k-f")["state"] == "failed"
    # a plain re-send is NOT a retry: refused without touching Meta
    m.fail.clear()
    with pytest.raises(WhatsAppError) as e:
        a.send_template(ME, "hello_world", idempotency_key="k-f")
    assert e.value.code == "explicit_retry_required"
    assert len(m.calls) == 1                     # zero new effects
    # the EXPLICIT retry path: attempts increment, history preserved
    r = a.send_template(ME, "hello_world", idempotency_key="k-f", retry=True)
    assert r.ok and not r.duplicate
    rec = a.outbox_record("k-f")
    assert rec["attempts"] == 2 and rec["state"] == "sent"
    assert len(rec["history"]) == 1
    assert rec["history"][0]["state"] == "failed"
    assert rec["history"][0]["failure_class"] == "retryable"  # 130429
    assert len(m.calls) == 2                     # two DELIBERATE attempts


# -- finding 3: locked atomic store, fail closed on corruption --------------

def test_outbox_persists_receipt_cross_instance(tmp_path):
    path = tmp_path / "outbox.json"
    m, a = make(store=JsonOutboxStore(path))
    a.send_template(ME, "hello_world", idempotency_key="k-durable")
    m2, a2 = make(store=JsonOutboxStore(path))
    r = a2.send_template(ME, "hello_world", idempotency_key="k-durable")
    assert r.duplicate is True and len(m2.calls) == 0


def test_corrupt_store_fails_closed_never_resets(tmp_path):
    path = tmp_path / "outbox.json"
    path.write_text("{not json")
    with pytest.raises(OutboxCorrupt):
        JsonOutboxStore(path)
    assert path.read_text() == "{not json"   # untouched, not reset


def test_failed_send_recorded_with_error():
    m, a = make(fail={ME: 130429})
    with pytest.raises(WhatsAppError):
        a.send_template(ME, "hello_world", idempotency_key="k-fail")
    rec = a.outbox_record("k-fail")
    assert rec["state"] == "failed" and rec["last_error"]["code"] == 130429


# -- finding 4: inbound resource binding ------------------------------------

def test_inbound_wrong_waba_rejected_no_window():
    now = [1000.0]
    m, a = make(now=now)
    body, sig = webhook([msg(ts=1000)], waba="someone-elses-waba")
    with pytest.raises(WhatsAppError) as e:
        a.handle_inbound_webhook(body, sig)
    assert e.value.code == "wrong_resource"
    assert not a.in_window(ME)


def test_inbound_wrong_phone_number_id_rejected():
    now = [1000.0]
    m, a = make(now=now)
    body, sig = webhook([msg(ts=1000)], pnid="other-phone-id")
    with pytest.raises(WhatsAppError) as e:
        a.handle_inbound_webhook(body, sig)
    assert e.value.code == "wrong_resource"
    assert not a.in_window(ME)


def test_inbound_valid_signature_normalizes_and_opens_window():
    now = [1000.0]
    m, a = make(now=now)
    body, sig = webhook([msg(ts=1000)])
    out = a.handle_inbound_webhook(body, sig)
    assert len(out) == 1 and out[0].sender == ME
    assert a.in_window(ME)


def test_inbound_bad_signature_dropped():
    m, a = make()
    body, _ = webhook([msg()])
    with pytest.raises(WhatsAppError) as e:
        a.handle_inbound_webhook(body, "sha256=deadbeef")
    assert e.value.code == "bad_signature"
    assert not a.in_window(ME)


def test_inbound_requires_app_secret():
    a = WhatsAppStagingAdapter(FakeMeta(), PNID, "t",
                               [RecipientBinding(ME, PNID, True)],
                               waba_id=WABA, canonical_grant=grant_fn,
                               owner=OWNER)
    body, sig = webhook([msg()])
    with pytest.raises(WhatsAppError) as e:
        a.handle_inbound_webhook(body, sig)
    assert e.value.code == "no_app_secret"


# -- finding 5: retry-horizon dedupe + timestamp safety ---------------------

def test_delayed_retry_within_horizon_deduped_by_id():
    now = [10_000_000.0]
    m, a = make(now=now)
    old = now[0] - 2 * 24 * 3600      # 2 days old: a legitimate slow retry
    body, sig = webhook([msg(mid="wam-old", ts=old)])
    first = a.handle_inbound_webhook(body, sig)
    second = a.handle_inbound_webhook(body, sig)
    assert len(first) == 1 and second == []
    assert not a.in_window(ME)        # 2d-old message opens NO window


def test_far_future_timestamp_rejected_no_window():
    now = [1000.0]
    m, a = make(now=now)
    body, sig = webhook([msg(mid="wam-future", ts=1000 + 3600)])
    with pytest.raises(WhatsAppError) as e:
        a.handle_inbound_webhook(body, sig)
    assert e.value.code == "invalid_message"
    assert not a.in_window(ME)


def test_beyond_retry_horizon_rejected():
    now = [10_000_000.0]
    m, a = make(now=now)
    ancient = now[0] - 8 * 24 * 3600  # past Meta's 7-day retry horizon
    body, sig = webhook([msg(mid="wam-ancient", ts=ancient)])
    with pytest.raises(WhatsAppError) as e:
        a.handle_inbound_webhook(body, sig)
    assert e.value.code == "invalid_message"
    assert not a.in_window(ME)


# ==========================================================================
# REVIEW 5478487276 fix set - adversarial repros counting EFFECTS
# (transport calls), mirroring their probes.
# ==========================================================================

# -- 1: allowed:false grant is refused with ZERO transport ------------------

def test_allowed_false_grant_refused_zero_transport():
    def denying(req):
        g = grant_fn(req)
        g["allowed"] = False
        return g
    m, a = make(grant=denying)
    with pytest.raises(WhatsAppError) as e:
        a.send_template(ME, "hello_world")
    assert e.value.code == "grant_denied"
    assert len(m.calls) == 0                     # zero effects
    assert a.outbox_record is not None


def test_missing_allowed_flag_refused_zero_transport():
    def sloppy(req):
        g = grant_fn(req)
        del g["allowed"]
        return g
    m, a = make(grant=sloppy)
    with pytest.raises(WhatsAppError) as e:
        a.send_template(ME, "hello_world")
    assert e.value.code == "grant_denied"
    assert len(m.calls) == 0


# -- 2: grant must echo-bind owner/resource/action/content/expiry -----------

@pytest.mark.parametrize("field,bad", [
    ("owner", "owner:someone-else"),
    ("resource", "whatsapp:waba:other-waba:phone:test-phone-id"),
    ("action", "send:other"),
    ("content_sha256", "0" * 64),
    ("expires_at", "not-a-number"),
    ("expires_at", float("nan")),
    ("expires_at", float("inf")),
])
def test_grant_binding_violations_refused_zero_transport(field, bad):
    def broken(req):
        g = grant_fn(req)
        g[field] = bad
        return g
    m, a = make(grant=broken)
    with pytest.raises(WhatsAppError) as e:
        a.send_template(ME, "hello_world")
    assert e.value.code == "bad_grant"
    assert len(m.calls) == 0                     # zero effects


def test_expired_grant_refused_zero_transport():
    def expired(req):
        g = grant_fn(req)
        g["expires_at"] = 500.0                  # clock is 1000.0
        return g
    m, a = make(grant=expired)
    with pytest.raises(WhatsAppError) as e:
        a.send_template(ME, "hello_world")
    assert e.value.code == "grant_expired"
    assert len(m.calls) == 0


def test_fully_bound_grant_sends_exactly_once():
    m, a = make()
    out = a.send_template(ME, "hello_world")
    assert out.ok and len(m.calls) == 1


# -- 3: atomic publish + crash recovery never resurrects a resend -----------

def test_store_publish_is_atomic_temp_then_rename(tmp_path):
    path = str(tmp_path / "outbox.json")
    store = JsonOutboxStore(path)
    store.put("send:k", {"state": "sent", "request": {}})
    # no temp files left behind, and the store is always valid JSON
    import os, glob
    assert glob.glob(path + ".tmp.*") == []
    assert json.loads(open(path).read())["send:k"]["state"] == "sent"


def test_empty_store_file_after_crash_fails_closed(tmp_path):
    # crash artifact: truncate-in-place would leave an EMPTY file; that
    # must NOT be treated as a fresh store (their two-effect repro).
    path = str(tmp_path / "outbox.json")
    open(path, "w").close()
    with pytest.raises(OutboxCorrupt):
        JsonOutboxStore(path)
    assert open(path).read() == ""            # left untouched, never reset


def test_crash_recovery_pending_claim_never_resent(tmp_path):
    # Instance A claims and crashes BEFORE recording an outcome.
    path = str(tmp_path / "outbox.json")
    store = JsonOutboxStore(path)
    full_body = {"messaging_product": "whatsapp",
                 "recipient_type": "individual", "to": ME, "type": "template",
                 "template": {"name": "hello_world",
                              "language": {"code": "en_US"}}}
    store.claim("send:k-crash",
                {"state": "claimed",
                 "request": {"to": ME, "type": "template", "body": full_body},
                 "grant_id": "g", "claimant": "dead-instance",
                 "attempts": 1, "claimed_at": 1}, "dead-instance")
    # Instance B (new adapter, same store) retries the same key+content:
    m = FakeMeta()
    req_body = {"to": ME, "type": "template"}
    a = WhatsAppStagingAdapter(m, PNID, "t",
                               [RecipientBinding(ME, PNID, True)],
                               app_secret=SECRET, canonical_grant=grant_fn,
                               waba_id=WABA, owner=OWNER,
                               outbox_store=JsonOutboxStore(path))
    # same key + same content -> "held", claimant is the dead instance
    with pytest.raises(WhatsAppError) as e:
        a.send_template(ME, "hello_world", idempotency_key="k-crash")
    assert e.value.code == "send_unknown"
    assert len(m.calls) == 0        # ZERO new effects: never resurrected


# -- 4: 5xx is unknown (no blind retry); only 4xx is definitive -------------

def test_5xx_is_unknown_never_blind_retried():
    m, a = make(fail5={ME: True})
    with pytest.raises(WhatsAppError) as e:
        a.send_template(ME, "hello_world", idempotency_key="k-5xx")
    assert e.value.code == "send_unknown"
    assert a.outbox_record("k-5xx")["state"] == "unknown"
    with pytest.raises(WhatsAppError) as e2:
        a.send_template(ME, "hello_world", idempotency_key="k-5xx")
    assert e2.value.code == "send_unknown"
    assert len(m.calls) == 1        # exactly one effect, ever


def test_4xx_is_definitive_response_and_explicitly_reclaimable():
    m, a = make(fail={ME: 131047})
    with pytest.raises(WhatsAppError) as e:
        a.send_template(ME, "hello_world", idempotency_key="k-4xx")
    assert "Definitive" in e.value.guidance     # per-code classification
    rec = a.outbox_record("k-4xx")
    assert rec["state"] == "failed" and rec["failure_class"] == "definitive"
    m.fail.clear()
    out = a.send_template(ME, "hello_world", idempotency_key="k-4xx",
                          retry=True)
    assert out.ok and len(m.calls) == 2   # two EXPLICIT attempts


# -- 5: replay dedupe + window state survive restart -------------------------

def test_replay_dedupe_survives_restart(tmp_path):
    path = str(tmp_path / "outbox.json")
    now = [1000.0]
    m1, a1 = make(now=now, store=JsonOutboxStore(path))
    body, sig = webhook([msg(mid="wam-durable", ts=1000)])
    assert len(a1.handle_inbound_webhook(body, sig)) == 1
    # new instance on the SAME store: the retry is still a duplicate
    m2, a2 = make(now=now, store=JsonOutboxStore(path))
    assert a2.handle_inbound_webhook(body, sig) == []   # zero re-delivery
    assert a2.in_window(ME)                              # window persisted


# -- 6: statuses and media are explicit --------------------------------------

def test_status_receipts_recorded_never_messages_never_windows():
    now = [1000.0]
    m, a = make(now=now)
    payload = {"entry": [{"id": WABA, "changes": [{"value": {
        "metadata": {"phone_number_id": PNID},
        "statuses": [{"id": "wamid.test", "status": "delivered",
                      "timestamp": "1000",
                      "recipient_id": ME}]}}]}]}
    body = json.dumps(payload).encode()
    sig = "sha256=" + hmac.new(SECRET.encode(), body,
                               hashlib.sha256).hexdigest()
    assert a.handle_inbound_webhook(body, sig) == []   # not messages
    assert len(a.statuses) == 1                        # explicit record
    assert a.statuses[0]["status"] == "delivered"
    assert not a.in_window(ME)                         # receipts open nothing


def test_media_message_normalized_with_type_no_content():
    now = [1000.0]
    m, a = make(now=now)
    body, sig = webhook([msg(mid="wam-img", ts=1000, text=None,
                             type_="image")])
    out = a.handle_inbound_webhook(body, sig)
    assert len(out) == 1
    assert out[0].type == "image" and out[0].text is None
    assert a.in_window(ME)     # a real customer message opens the window


# -- 7: batch all-or-nothing -------------------------------------------------

def test_batch_with_wrong_resource_mutates_nothing():
    now = [1000.0]
    m, a = make(now=now)
    payload = {"entry": [
        {"id": WABA, "changes": [{"value": {
            "metadata": {"phone_number_id": PNID},
            "messages": [msg(mid="wam-good", ts=1000)]}}]},
        {"id": "OTHER-WABA", "changes": [{"value": {
            "metadata": {"phone_number_id": PNID},
            "messages": [msg(mid="wam-evil", ts=1000)]}}]}]}
    body = json.dumps(payload).encode()
    sig = "sha256=" + hmac.new(SECRET.encode(), body,
                               hashlib.sha256).hexdigest()
    with pytest.raises(WhatsAppError) as e:
        a.handle_inbound_webhook(body, sig)
    assert e.value.code == "wrong_resource"
    # ZERO partial mutation: the GOOD message was not recorded either
    assert not a.in_window(ME)
    good_only = webhook([msg(mid="wam-good", ts=1000)])
    assert len(a.handle_inbound_webhook(*good_only)) == 1  # not a duplicate


def test_batch_with_invalid_message_mutates_nothing():
    now = [1000.0]
    m, a = make(now=now)
    body, sig = webhook([msg(mid="wam-good2", ts=1000),
                         msg(mid="wam-bad", ts=1000 + 99999)])
    with pytest.raises(WhatsAppError) as e:
        a.handle_inbound_webhook(body, sig)
    assert e.value.code == "invalid_message"
    assert not a.in_window(ME)
    good_only = webhook([msg(mid="wam-good2", ts=1000)])
    assert len(a.handle_inbound_webhook(*good_only)) == 1


# -- 8: content-bound idempotency --------------------------------------------

def test_same_key_different_content_is_conflict_never_stale_success():
    m, a = make()
    out = a.send_template(ME, "hello_world", idempotency_key="k-content")
    assert out.ok and len(m.calls) == 1                # one effect
    with pytest.raises(WhatsAppError) as e:
        a.send_template(ME, "different_template",
                        idempotency_key="k-content")
    assert e.value.code == "idempotency_conflict"
    assert len(m.calls) == 1          # the OLD success was NOT replayed
    rec = a.outbox_record("k-content")
    assert rec["request"]["body"]["template"]["name"] == "hello_world"


def test_same_key_same_content_returns_stored_response_no_new_effect():
    m, a = make()
    out1 = a.send_template(ME, "hello_world", idempotency_key="k-same")
    out2 = a.send_template(ME, "hello_world", idempotency_key="k-same")
    assert out2.ok and out2.duplicate and out2.wamid == out1.wamid
    assert len(m.calls) == 1          # exactly one effect


# -- 10: window state is monotonic -------------------------------------------

def test_older_inbound_never_regresses_window():
    now = [10_000.0]
    m, a = make(now=now)
    new_body, new_sig = webhook([msg(mid="wam-new", ts=10_000)])
    assert len(a.handle_inbound_webhook(new_body, new_sig)) == 1
    # an OLDER inbound (valid signature, different id, within horizon)
    old_body, old_sig = webhook([msg(mid="wam-older", ts=9_000)])
    assert len(a.handle_inbound_webhook(old_body, old_sig)) == 1
    now[0] = 10_000 + 23 * 3600       # 23h after the NEWER message
    assert a.in_window(ME)            # window anchored at 10000, not 9000
    now[0] = 10_000 + 25 * 3600       # 25h after the newer message
    assert not a.in_window(ME)


def test_note_inbound_never_regresses_window():
    now = [10_000.0]
    m, a = make(now=now)
    a.note_inbound(ME)
    now[0] = 5_000.0                  # clock skew backwards
    a.note_inbound(ME)                # must not regress the window
    now[0] = 10_000 + 23 * 3600
    assert a.in_window(ME)


# ==========================================================================
# REVIEW 3 fix set (their repros on 6c6bf32) - effects counted, not state
# ==========================================================================

# -- 1: failed-reclaim compares content BEFORE any mutation ------------------

def test_failed_key_changed_content_conflict_no_overwrite_no_second_effect():
    m, a = make(fail={ME: 131000})
    with pytest.raises(WhatsAppError):
        a.send_template(ME, "hello_world", idempotency_key="k-fc")
    assert len(m.calls) == 1                     # one effect so far
    before = dict(a.outbox_record("k-fc"))
    with pytest.raises(WhatsAppError) as e:
        a.send_template(ME, "changed_template", idempotency_key="k-fc",
                        retry=True)
    assert e.value.code == "idempotency_conflict"
    assert len(m.calls) == 1                     # NO second effect
    after = a.outbox_record("k-fc")
    assert after["request"] == before["request"]  # record NOT overwritten
    assert after["state"] == "failed"
    assert after["attempts"] == before["attempts"]


# -- 2: explicit retry parameter with preserved attempts/history -------------
# (covered by test_failed_send_requires_explicit_retry_then_reclaims:
#  plain resend refused with ZERO new effects; retry=True increments
#  attempts and appends the prior failure to history)

# -- 3: per-code classification, not per-class -------------------------------

@pytest.mark.parametrize("code,klass", [
    (130429, "retryable"),     # "Try again later" (official guidance)
    (131056, "retryable"),     # "Wait and retry the operation"
    (131000, "retryable"),     # "Try again. If the error persists..."
    (131047, "definitive"),    # send a template instead
    (131030, "definitive"),    # not in allowed list
    (131050, "definitive"),    # "Do not retry"
    (139999, "unknown"),       # unlisted code: unknown, NO retry permitted
    (141006, "unknown"),       # absent from the current error-codes ref
    (141010, "unknown"),       # absent from the current error-codes ref
])
def test_failure_classified_per_meta_error_code(code, klass):
    m, a = make(fail={ME: code})
    with pytest.raises(WhatsAppError):
        a.send_template(ME, "hello_world", idempotency_key=f"k-{code}")
    rec = a.outbox_record(f"k-{code}")
    assert rec["state"] == "failed"
    assert rec["failure_class"] == klass
    assert len(m.calls) == 1


# -- 4: batch acceptance is ONE atomic store transaction ---------------------

def test_batch_applied_in_single_store_transaction(tmp_path):
    path = str(tmp_path / "outbox.json")
    store = JsonOutboxStore(path)
    now = [1000.0]
    m, a = make(now=now, store=store)
    writes = []
    orig = store._mutate
    def spy(fn):
        writes.append(1)
        return orig(fn)
    store._mutate = spy
    body, sig = webhook([msg(mid="wam-a1", ts=1000),
                         msg(mid="wam-a2", ts=1000),
                         msg(mid="wam-a3", ts=1000)])
    out = a.handle_inbound_webhook(body, sig)
    assert len(out) == 3
    assert len(writes) == 1        # the WHOLE batch = one locked write
    # and the dedupe marks + window landed atomically
    m2, a2 = make(now=now, store=JsonOutboxStore(path))
    assert a2.handle_inbound_webhook(body, sig) == []
    assert a2.in_window(ME)


def test_rejected_batch_writes_nothing(tmp_path):
    path = str(tmp_path / "outbox.json")
    store = JsonOutboxStore(path)
    now = [1000.0]
    m, a = make(now=now, store=store)
    before = open(path).read()
    body, sig = webhook([msg(mid="wam-z1", ts=1000),
                         msg(mid="wam-z2", ts=1000 + 99999)])
    with pytest.raises(WhatsAppError):
        a.handle_inbound_webhook(body, sig)
    assert open(path).read() == before   # byte-identical: zero mutation


# -- 5: durable receipts + reconciliation boundary ---------------------------

def test_unknown_class_failure_never_retried_even_explicitly():
    m, a = make(fail={ME: 141006})
    with pytest.raises(WhatsAppError) as e:
        a.send_template(ME, "hello_world", idempotency_key="k-unk")
    assert "UNKNOWN" in e.value.guidance
    assert a.outbox_record("k-unk")["failure_class"] == "unknown"
    with pytest.raises(WhatsAppError) as e2:
        a.send_template(ME, "hello_world", idempotency_key="k-unk",
                        retry=True)
    assert e2.value.code == "unknown_failure_no_retry"
    assert len(m.calls) == 1         # zero further effects, ever


def test_status_receipts_durable_across_restart(tmp_path):
    path = str(tmp_path / "outbox.json")
    now = [1000.0]
    m1, a1 = make(now=now, store=JsonOutboxStore(path))
    payload = {"entry": [{"id": WABA, "changes": [{"value": {
        "metadata": {"phone_number_id": PNID},
        "statuses": [{"id": "wamid.durable", "status": "read",
                      "timestamp": "1000", "recipient_id": ME}]}}]}]}
    body = json.dumps(payload).encode()
    sig = "sha256=" + hmac.new(SECRET.encode(), body,
                               hashlib.sha256).hexdigest()
    assert a1.handle_inbound_webhook(body, sig) == []
    # fresh instance on the same store: receipt survives
    m2, a2 = make(now=now, store=JsonOutboxStore(path))
    assert a2.receipt("wamid.durable")["status"] == "read"
    assert len(a2.statuses) == 1
    assert not a2.in_window(ME)           # receipts still open nothing
