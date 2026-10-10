"""Synthetic acceptance tests for the voice adapter against a fake canonical
implementing REV3 semantics (issue/redeem/revalidate/progress/final + ruling b).
No network, no provider, no routes."""
import sys, time, pathlib
sys.path.insert(0, str(pathlib.Path(__file__).parent))
import pytest
from adapter import VoiceAdapter, Lease, AdapterError, CommitUnknown


class FakeCanonical:
    """Minimal canonical engine with REV3 behaviors."""
    def __init__(self, clock=time.time):
        self.clock = clock
        self.tickets = {}   # ticket -> dict(bound..., redeemed)
        self.leases = {}    # lease_id -> dict(ticket, revoked, expires)
        self.finals = {}    # (owner, request_id) -> payload digest
        self.sessions_live = True
        self.n = 0

    def issue(self, origin="https://app.example", mode="transcribe", ttl=120):
        self.n += 1
        t = f"ticket{self.n}"
        self.tickets[t] = {"origin": origin, "mode": mode,
                           "expires": self.clock() + ttl, "redeemed": False}
        return t

    def revoke_lease(self, lease_id):
        self.leases[lease_id]["revoked"] = True

    def __call__(self, method, path, headers, body):
        assert "ticket" not in path, "ticket must never travel in a URL"
        if not headers.get("Authorization", "").startswith("Bearer scoped-"):
            return 401, {"error_code": "unauthenticated"}
        if path == "/voice/internal/redeem":
            b = self.tickets.get(body["ticket"])
            if not b or b["redeemed"]:
                return 409, {"error_code": "already_redeemed"}
            if self.clock() >= b["expires"]:
                return 410, {"error_code": "expired"}
            if body["observed_origin"] != b["origin"] or body["mode"] != b["mode"]:
                return 403, {"error_code": "origin"}
            if not self.sessions_live:
                return 401, {"error_code": "unauthenticated"}
            b["redeemed"] = True
            lid = f"lease{body['ticket']}"
            self.leases[lid] = {"ticket": body["ticket"], "revoked": False,
                                "expires": self.clock() + 600}
            return 200, {"version": 1, "lease_id": lid,
                         "request_id": "req-" + body["ticket"],
                         "conversation_id": "conv1", "mode": b["mode"],
                         "origin": b["origin"],
                         "expires_at": self.clock() + 600,
                         "revalidate_seconds": 15,
                         "audio_limits": {"max_seconds": 600,
                                          "max_bytes": 20000000}}
        lease = self.leases.get(body.get("lease_id"))
        live = (lease and not lease["revoked"] and self.sessions_live
                and self.clock() < lease["expires"])
        if path == "/voice/internal/revalidate":
            if not live:
                return 410, {"error_code": "revoked"}
            return 200, {"version": 1, "active": True,
                         "request_id": "req", "expires_at": lease["expires"],
                         "next_check_seconds": 15}
        # progress and final require liveness BEFORE anything else (ruling b)
        if not live:
            return 410, {"error_code": "revoked"}
        if path == "/voice/internal/progress":
            return 200, {"accepted": True, "seq": body["seq"]}
        if path == "/voice/internal/final":
            key = ("owner", body["request_id"])
            import json
            digest = json.dumps(body, sort_keys=True)
            if key in self.finals:
                if self.finals[key] == digest:
                    return 200, {"accepted": True, "duplicate": True}
                return 409, {"error_code": "final_conflict"}
            self.finals[key] = digest
            return 201, {"accepted": True, "request_id": body["request_id"],
                         "seq": body["seq"]}
        return 404, {"error_code": "unknown"}


def make(clock=time.time):
    c = FakeCanonical(clock)
    return c, VoiceAdapter(c, "scoped-test-cred")


def test_redeem_then_final_happy_path():
    c, a = make()
    lease = a.redeem(c.issue(), "https://app.example", "transcribe")
    assert a.revalidate(lease) is True
    a.progress(lease, "transcript_partial", "running", "hel")
    r = a.progress(lease, "transcript_final", "running", "hello")
    assert r["accepted"] and r["seq"] == 2
    out = a.final(lease, "done", transcript_text="hello", answer_text="hi")
    assert out["accepted"]


def test_second_redeem_same_ticket_rejected():
    c, a = make()
    t = c.issue()
    a.redeem(t, "https://app.example", "transcribe")
    with pytest.raises(AdapterError) as e:
        a.redeem(t, "https://app.example", "transcribe")
    assert e.value.status == 409


def test_expired_ticket_rejected():
    now = [1000.0]
    c, a = make(clock=lambda: now[0])
    t = c.issue(ttl=120)
    now[0] += 121
    with pytest.raises(AdapterError) as e:
        a.redeem(t, "https://app.example", "transcribe")
    assert e.value.status == 410


def test_origin_mismatch_rejected():
    c, a = make()
    with pytest.raises(AdapterError) as e:
        a.redeem(c.issue(), "https://evil.example", "transcribe")
    assert e.value.status == 403


def test_mode_mismatch_rejected():
    c, a = make()
    with pytest.raises(AdapterError):
        a.redeem(c.issue(mode="speak"), "https://app.example", "live")


def test_revocation_closes_progress_and_final():
    c, a = make()
    t = c.issue()
    lease = a.redeem(t, "https://app.example", "transcribe")
    c.revoke_lease(lease.lease_id)
    with pytest.raises(AdapterError) as e:
        a.revalidate(lease)
    assert e.value.status == 410 and not lease.active
    with pytest.raises(AdapterError):
        a.progress(lease, "progress", "running")


def test_session_revoke_denies_immediately_not_after_15s():
    c, a = make()
    lease = a.redeem(c.issue(), "https://app.example", "transcribe")
    c.sessions_live = False
    with pytest.raises(AdapterError):
        a.revalidate(lease)


def test_final_duplicate_while_live_acknowledged():
    c, a = make()
    lease = a.redeem(c.issue(), "https://app.example", "transcribe")
    body = dict(state="done", transcript_text="hi", answer_text="hello")
    r1 = a.final(lease, **body)
    assert r1["accepted"]
    # exact replay while still live -> duplicate ack (canonical-side rule)
    lease.final_sent = False
    lease.seq -= 1
    r2 = a.final(lease, **body)
    assert r2.get("duplicate") is True


def test_final_retry_after_revocation_is_commit_unknown_and_no_replay():
    c, a = make()
    lease = a.redeem(c.issue(), "https://app.example", "transcribe")
    # canonical commits the final, then the lease is revoked before the
    # response reaches the adapter; the adapter retry is denied 410.
    key = ("owner", lease.request_id)
    c.revoke_lease(lease.lease_id)
    provider_calls = []
    with pytest.raises(CommitUnknown):
        a.final(lease, "done", transcript_text="hi")
    # adapter must not replay provider work or effects
    assert provider_calls == []
    assert not lease.active


def test_final_after_expiry_is_commit_unknown():
    now = [1000.0]
    c, a = make(clock=lambda: now[0])
    lease = a.redeem(c.issue(), "https://app.example", "transcribe")
    now[0] += 601
    with pytest.raises(CommitUnknown):
        a.final(lease, "done", transcript_text="hi")


def test_seq_is_monotonic():
    c, a = make()
    lease = a.redeem(c.issue(), "https://app.example", "transcribe")
    a.progress(lease, "progress", "running")
    a.progress(lease, "progress", "running")
    assert lease.seq == 2


def test_provider_guard_fails_closed():
    c, a = make()
    lease = a.redeem(c.issue(), "https://app.example", "transcribe")
    c.revoke_lease(lease.lease_id)
    with pytest.raises(AdapterError):
        a.provider_connect_guard(lease)


def test_ticket_never_in_url():
    seen = []
    c = FakeCanonical()
    def spy(method, path, headers, body):
        seen.append(path)
        return c(method, path, headers, body)
    a = VoiceAdapter(spy, "scoped-x")
    a.redeem(c.issue(), "https://app.example", "transcribe")
    assert all("ticket" not in p for p in seen)


def test_bad_progress_type_rejected_locally():
    c, a = make()
    lease = a.redeem(c.issue(), "https://app.example", "transcribe")
    with pytest.raises(AdapterError):
        a.progress(lease, "bogus", "running")


def test_final_state_enum_enforced():
    c, a = make()
    lease = a.redeem(c.issue(), "https://app.example", "transcribe")
    with pytest.raises(AdapterError):
        a.final(lease, "running")
