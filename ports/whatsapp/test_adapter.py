"""Synthetic tests for the WhatsApp staging adapter. No live Meta calls."""
import sys, pathlib
sys.path.insert(0, str(pathlib.Path(__file__).parent))
import pytest
from adapter import WhatsAppStagingAdapter, WhatsAppError, ERROR_MAP

ME = "+918777566396"
OTHER = "+15551234567"


class FakeMeta:
    def __init__(self, clock=lambda: 1000.0, fail=None):
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


def make(now=[1000.0], fail=None, recips=(ME,)):
    m = FakeMeta(clock=lambda: now[0], fail=fail)
    a = WhatsAppStagingAdapter(m, "test-phone-id", "test-token", recips,
                               clock=lambda: now[0])
    return m, a


def test_template_send_to_verified_recipient():
    m, a = make()
    out = a.send_template(ME, "hello_world")
    assert out["messages"][0]["id"] == "wamid.test"
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
        WhatsAppStagingAdapter(FakeMeta(), "p", "t",
                               [f"+1555000000{i}" for i in range(6)])


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
    assert out["messages"]
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
    assert a.send_template(ME, "hello_world")["messages"]


def test_141006_guidance_is_card_free():
    m, a = make(fail={ME: 141006})
    with pytest.raises(WhatsAppError) as e:
        a.send_template(ME, "hello_world")
    assert e.value.code == 141006
    assert "Do NOT attach a card" in e.value.guidance
    assert "exempt" in e.value.guidance


def test_131000_profile_guidance():
    m, a = make(fail={ME: 131000})
    with pytest.raises(WhatsAppError) as e:
        a.send_template(ME, "hello_world")
    assert "Business Info" in e.value.guidance


def test_unknown_error_maps_safely():
    m, a = make(fail={ME: 139999})
    with pytest.raises(WhatsAppError) as e:
        a.send_template(ME, "hello_world")
    assert "official Cloud API error reference" in e.value.guidance


def test_error_map_covers_block_chain():
    for code in (141006, 141010, 131000, 131030, 131047):
        assert code in ERROR_MAP
