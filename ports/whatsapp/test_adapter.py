"""Synthetic tests for the WhatsApp staging adapter. No live Meta calls."""
import sys, pathlib, json, hmac, hashlib
sys.path.insert(0, str(pathlib.Path(__file__).parent))
import pytest
from adapter import (WhatsAppStagingAdapter, WhatsAppError, ERROR_MAP,
                     RecipientBinding, JsonOutboxStore)

ME = "+918777566396"
OTHER = "+15551234567"
PNID = "test-phone-id"
SECRET = "synthetic-app-secret"


class FakeMeta:
    def __init__(self, fail=None):
        self.calls = []
        self.fail = fail or {}
    def __call__(self, method, path, headers, body):
        assert path.startswith("/v23.0/test-phone-id/")
        assert "ticket" not in path
        self.calls.append(body)
        to = body["to"]
        if to in self.fail:
            return 400, {"error": {"code": self.fail[to],
                                   "message": "synthetic failure"}}
        return 200, {"messages": [{"id": "wamid.test"}]}


def make(now=[1000.0], fail=None, recips=(ME,), secret=SECRET, store=None):
    m = FakeMeta(fail=fail)
    bindings = [RecipientBinding(r, PNID, True) for r in recips]
    a = WhatsAppStagingAdapter(m, PNID, "test-token", bindings,
                               clock=lambda: now[0], app_secret=secret,
                               outbox_store=store)
    return m, a


def webhook(msgs, secret=SECRET):
    body = json.dumps({"entry": [{"changes": [{"value": {
        "messages": msgs}}]}]}).encode()
    sig = "sha256=" + hmac.new(secret.encode(), body,
                               hashlib.sha256).hexdigest()
    return body, sig


def msg(mid="wam1", sender=ME, ts=1000, text="hi", type_="text"):
    m = {"id": mid, "from": sender, "timestamp": str(ts), "type": type_}
    if text is not None:
        m["text"] = {"body": text}
    return m


# -- pre-existing behavior (updated to canonical response shape) -----------

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
    assert m.calls == []  # never reached Meta


def test_recipient_cap_enforced():
    with pytest.raises(ValueError):
        WhatsAppStagingAdapter(
            FakeMeta(), PNID, "t",
            [RecipientBinding(f"+1555000000{i}", PNID, True)
             for i in range(6)])


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


def test_template_always_allowed_outside_window():
    m, a = make()
    assert a.send_template(ME, "hello_world").ok


def test_141006_guidance_is_card_free():
    m, a = make(fail={ME: 141006})
    with pytest.raises(WhatsAppError) as e:
        a.send_template(ME, "hello_world")
    assert e.value.code == 141006
    assert "Do NOT attach a card" in e.value.guidance
    assert "exempt" in e.value.guidance


def test_unknown_error_maps_safely():
    m, a = make(fail={ME: 139999})
    with pytest.raises(WhatsAppError) as e:
        a.send_template(ME, "hello_world")
    assert "official Cloud API error reference" in e.value.guidance


def test_error_map_covers_block_chain():
    for code in (141006, 141010, 131000, 131030, 131047):
        assert code in ERROR_MAP


# -- finding 5: 131000 is a GENERIC unknown error ---------------------------

def test_131000_treated_as_generic_unknown():
    m, a = make(fail={ME: 131000})
    with pytest.raises(WhatsAppError) as e:
        a.send_template(ME, "hello_world")
    assert e.value.code == 131000
    assert "generic unknown" in e.value.guidance
    assert "profile" not in e.value.guidance.lower().split("not evidence")[0] \
        or "NOT evidence" in e.value.guidance


# -- finding 4: owner-verified test-WABA recipient binding ------------------

def test_binding_to_other_phone_number_rejected():
    with pytest.raises(ValueError) as e:
        WhatsAppStagingAdapter(FakeMeta(), PNID, "t",
                               [RecipientBinding(ME, "someone-elses-pnid",
                                                 True)])
    assert "different" in str(e.value)


def test_unconfirmed_binding_rejected():
    with pytest.raises(ValueError) as e:
        WhatsAppStagingAdapter(FakeMeta(), PNID, "t",
                               [RecipientBinding(ME, PNID, False)])
    assert "owner-confirmed" in str(e.value)


def test_bound_recipient_sends_ok():
    m, a = make(recips=(ME, OTHER))
    assert a.send_template(OTHER, "hello_world").ok


# -- finding 1: normalized inbound with signature + replay validation -------

def test_inbound_valid_signature_normalizes_and_opens_window():
    now = [1000.0]
    m, a = make(now=now)
    body, sig = webhook([msg(ts=1000)])
    out = a.handle_inbound_webhook(body, sig)
    assert len(out) == 1 and out[0].sender == ME and out[0].text == "hi"
    assert a.in_window(ME)  # normalized inbound opened the service window


def test_inbound_bad_signature_dropped():
    m, a = make()
    body, _ = webhook([msg()])
    with pytest.raises(WhatsAppError) as e:
        a.handle_inbound_webhook(body, "sha256=deadbeef")
    assert e.value.code == "bad_signature"
    assert not a.in_window(ME)  # no state changed


def test_inbound_replay_duplicate_message_id_ignored():
    now = [1000.0]
    m, a = make(now=now)
    body, sig = webhook([msg(mid="wam-dup", ts=1000)])
    first = a.handle_inbound_webhook(body, sig)
    second = a.handle_inbound_webhook(body, sig)
    assert len(first) == 1 and second == []  # replay suppressed


def test_inbound_stale_timestamp_rejected():
    now = [10_000_000.0]
    m, a = make(now=now)
    body, sig = webhook([msg(mid="wam-old", ts=1000)])  # ancient
    assert a.handle_inbound_webhook(body, sig) == []
    assert not a.in_window(ME)


def test_inbound_requires_app_secret():
    m = FakeMeta()
    a = WhatsAppStagingAdapter(m, PNID, "t", [RecipientBinding(ME, PNID, True)])
    body, sig = webhook([msg()])
    with pytest.raises(WhatsAppError) as e:
        a.handle_inbound_webhook(body, sig)
    assert e.value.code == "no_app_secret"


# -- finding 2+3: durable outbox, idempotency, receipts, canonical handoff --

def test_idempotency_key_prevents_resend():
    m, a = make()
    r1 = a.send_template(ME, "hello_world", idempotency_key="k1")
    r2 = a.send_template(ME, "hello_world", idempotency_key="k1")
    assert r1.ok and r2.ok and r2.duplicate is True
    assert len(m.calls) == 1  # exactly one Meta call


def test_outbox_persists_receipt_and_state(tmp_path):
    store = JsonOutboxStore(tmp_path / "outbox.json")
    m, a = make(store=store)
    a.send_template(ME, "hello_world", idempotency_key="k-durable")
    rec = a.outbox_record("k-durable")
    assert rec["state"] == "sent"
    assert rec["receipt"]["wamid"] == "wamid.test"
    # a NEW adapter over the same store sees the record (durability)
    m2, a2 = make(store=JsonOutboxStore(tmp_path / "outbox.json"))
    r = a2.send_template(ME, "hello_world", idempotency_key="k-durable")
    assert r.duplicate is True and len(m2.calls) == 0


def test_failed_send_recorded_failed_with_error():
    store = {}
    m, a = make(fail={ME: 130429}, store=store)
    with pytest.raises(WhatsAppError):
        a.send_template(ME, "hello_world", idempotency_key="k-fail")
    rec = a.outbox_record("k-fail")
    assert rec["state"] == "failed" and rec["last_error"]["code"] == 130429


def test_canonical_response_carries_handoff_fields():
    m, a = make()
    r = a.send_template(ME, "hello_world", idempotency_key="k-canon")
    assert (r.ok, r.status, r.wamid) == (True, 200, "wamid.test")
    rec = a.outbox_record("k-canon")
    assert rec["request"]["to"] == ME and rec["request"]["type"] == "template"
