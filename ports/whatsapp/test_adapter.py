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
    def __init__(self, fail=None, delay=0):
        self.calls = []
        self.fail = fail or {}
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
        return 200, {"messages": [{"id": "wamid.test"}]}


def grant_fn(req):
    return {"grant_id": "grant-1", "idempotency_key": req.idempotency_key,
            "to": req.to}


def make(now=[1000.0], fail=None, recips=(ME,), secret=SECRET, store=None,
         grant=grant_fn, waba=WABA, **mkw):
    m = FakeMeta(fail=fail, delay=mkw.pop("delay", 0))
    bindings = [RecipientBinding(r, PNID, True) for r in recips]
    a = WhatsAppStagingAdapter(m, PNID, "test-token", bindings,
                               clock=lambda: now[0], app_secret=secret,
                               outbox_store=store, canonical_grant=grant,
                               waba_id=waba, **mkw)
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
             for i in range(6)], waba_id=WABA, canonical_grant=grant_fn)


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
                               waba_id=WABA, canonical_grant=grant_fn)


def test_unconfirmed_binding_rejected():
    with pytest.raises(ValueError):
        WhatsAppStagingAdapter(FakeMeta(), PNID, "t",
                               [RecipientBinding(ME, PNID, False)],
                               waba_id=WABA, canonical_grant=grant_fn)


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
        return {"grant_id": "g", "idempotency_key": "WRONG", "to": req.to}
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
                               waba_id=WABA)
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


def test_failed_send_may_be_retried_explicitly():
    m, a = make(fail={ME: 130429})
    with pytest.raises(WhatsAppError):
        a.send_template(ME, "hello_world", idempotency_key="k-f")
    assert a.outbox_record("k-f")["state"] == "failed"
    m.fail.clear()
    r = a.send_template(ME, "hello_world", idempotency_key="k-f")
    assert r.ok and not r.duplicate
    assert a.outbox_record("k-f")["attempts"] == 1  # re-claimed record
    assert a.outbox_record("k-f")["state"] == "sent"


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
                               waba_id=WABA, canonical_grant=grant_fn)
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
    assert a.handle_inbound_webhook(body, sig) == []
    assert not a.in_window(ME)


def test_beyond_retry_horizon_skipped():
    now = [10_000_000.0]
    m, a = make(now=now)
    ancient = now[0] - 8 * 24 * 3600  # past Meta's 7-day retry horizon
    body, sig = webhook([msg(mid="wam-ancient", ts=ancient)])
    assert a.handle_inbound_webhook(body, sig) == []
    assert not a.in_window(ME)
