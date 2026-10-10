"""Synthetic acceptance tests for the voice adapter against a fake canonical
implementing REV3 semantics. No network, no provider, no routes."""
import sys, threading, time, pathlib
sys.path.insert(0, str(pathlib.Path(__file__).parent))
import pytest
from adapter import (VoiceAdapter, AdapterError, CommitUnknown, CapExceeded,
                     TransportTimeout, REVALIDATE_SECONDS)

ORIGIN = "https://app.example"


class FakeCanonical:
    """Minimal canonical engine with REV3 behaviors. Thread-safe."""
    def __init__(self, clock=lambda: 1000.0):
        self.clock = clock
        self.tickets = {}
        self.leases = {}
        self.finals = {}
        self.sessions_live = True
        self.revalidate_active = True
        self.bad_revalidate_binding = False
        self.loose_caps = False
        self.timeout_on = set()      # paths that raise TransportTimeout AFTER acting
        self.echo_mismatch = False
        self.n = 0
        self._mu = threading.Lock()

    def issue(self, origin=ORIGIN, mode="transcribe", ttl=120):
        with self._mu:
            self.n += 1
            t = f"ticket{self.n}"
            self.tickets[t] = {"origin": origin, "mode": mode,
                               "expires": self.clock() + ttl, "redeemed": False}
            return t

    def revoke_lease(self, lease_id):
        with self._mu:
            self.leases[lease_id]["revoked"] = True

    def __call__(self, method, path, headers, body):
        assert "ticket" not in path, "ticket must never travel in a URL"
        if not headers.get("Authorization", "").startswith("Bearer scoped-"):
            return 401, {"error_code": "unauthenticated"}
        if path == "/voice/internal/redeem":
            with self._mu:  # atomic claim: exactly one concurrent redeem wins
                b = self.tickets.get(body["ticket"])
                if not b or b["redeemed"]:
                    return 409, {"error_code": "already_redeemed"}
                if self.clock() >= b["expires"]:
                    return 410, {"error_code": "expired"}
                if (body["observed_origin"] != b["origin"]
                        or body["mode"] != b["mode"]):
                    return 403, {"error_code": "origin"}
                if not self.sessions_live:
                    return 401, {"error_code": "unauthenticated"}
                b["redeemed"] = True
                lid = f"lease{body['ticket']}"
                self.leases[lid] = {"revoked": False,
                                    "expires": self.clock() + 600,
                                    "request_id": "req-" + body["ticket"]}
            origin = "https://other.example" if self.echo_mismatch else b["origin"]
            return 200, {"version": 1, "lease_id": lid,
                         "request_id": "req-" + body["ticket"],
                         "conversation_id": "conv1", "mode": b["mode"],
                         "origin": origin, "expires_at": self.clock() + 600,
                         "revalidate_seconds": 15,
                         "audio_limits": ({"max_seconds": 999999,
                                           "max_bytes": 10**12}
                                          if self.loose_caps else
                                          {"max_seconds": 600,
                                           "max_bytes": 20000000})}
        lease = self.leases.get(body.get("lease_id"))
        live = (lease and not lease["revoked"] and self.sessions_live
                and self.clock() < lease["expires"])
        if path == "/voice/internal/revalidate":
            if not live:
                return 410, {"error_code": "revoked"}
            if path in self.timeout_on:
                raise TransportTimeout()
            return 200, {"version": 1, "active": self.revalidate_active,
                         "lease_id": body["lease_id"],
                         "request_id": ("wrong-request"
                                        if self.bad_revalidate_binding else
                                        lease["request_id"]),
                         "expires_at": lease["expires"],
                         "next_check_seconds": 15}
        # progress/final: liveness BEFORE duplicate detection (ruling b)
        if not live:
            return 410, {"error_code": "revoked"}
        if path == "/voice/internal/progress":
            return 200, {"accepted": True, "seq": body["seq"]}
        if path == "/voice/internal/final":
            import json
            key = ("owner", body["request_id"])
            digest = json.dumps(body, sort_keys=True)
            with self._mu:
                if key in self.finals:
                    if self.finals[key] == digest:
                        return 200, {"accepted": True, "duplicate": True}
                    return 409, {"error_code": "final_conflict"}
                self.finals[key] = digest  # commit happens here
            if path in self.timeout_on:
                raise TransportTimeout()  # commit landed, response lost
            return 201, {"accepted": True, "request_id": body["request_id"],
                         "seq": body["seq"]}
        return 404, {"error_code": "unknown"}


def make(clock=lambda: 1000.0):
    c = FakeCanonical(clock)
    return c, VoiceAdapter(c, "scoped-test-cred", clock=clock,
                           allowed_origins={ORIGIN, "https://other.example"})


def test_redeem_then_final_happy_path():
    c, a = make()
    lease = a.redeem(c.issue(), ORIGIN, "transcribe")
    assert a.revalidate(lease) is True
    a.progress(lease, "transcript_partial", "running", "hel")
    r = a.progress(lease, "transcript_final", "running", "hello")
    assert r["accepted"] and r["seq"] == 2
    out = a.final(lease, "done", transcript_text="hello", answer_text="hi")
    assert out["accepted"]


def test_concurrent_redeem_exactly_one_wins():
    c, a = make()
    t = c.issue()
    barrier = threading.Barrier(3)
    results, errors = [], []

    def race():
        barrier.wait()
        try:
            results.append(a.redeem(t, ORIGIN, "transcribe"))
        except AdapterError as e:
            errors.append(e)

    threads = [threading.Thread(target=race) for _ in range(2)]
    [th.start() for th in threads]
    barrier.wait()
    [th.join() for th in threads]
    assert len(results) == 1 and len(errors) == 1
    assert errors[0].status == 409


def test_sequential_second_redeem_rejected():
    c, a = make()
    t = c.issue()
    a.redeem(t, ORIGIN, "transcribe")
    with pytest.raises(AdapterError) as e:
        a.redeem(t, ORIGIN, "transcribe")
    assert e.value.status == 409


def test_exact_expiry_boundary():
    now = [1000.0]
    c, a = make(clock=lambda: now[0])
    t_ok = c.issue(ttl=120)
    now[0] += 119.9
    a.redeem(t_ok, ORIGIN, "transcribe")  # still valid just inside
    t_edge = c.issue(ttl=120)
    now[0] += 120.0  # exactly expires_at -> expired
    with pytest.raises(AdapterError) as e:
        a.redeem(t_edge, ORIGIN, "transcribe")
    assert e.value.status == 410


def test_origin_mismatch_rejected():
    c, a = make()
    with pytest.raises(AdapterError) as e:
        a.redeem(c.issue(), "https://other.example", "transcribe")
    assert e.value.status == 403


def test_null_and_non_https_origin_rejected_locally():
    c, a = make()
    t1, t2 = c.issue(), c.issue()
    with pytest.raises(AdapterError):
        a.redeem(t1, None, "transcribe")
    with pytest.raises(AdapterError):
        a.redeem(t2, "http://app.example", "transcribe")
    # neither ticket was consumed (rejected before transport)
    assert not c.tickets[t1]["redeemed"] and not c.tickets[t2]["redeemed"]


def test_mode_mismatch_rejected():
    c, a = make()
    with pytest.raises(AdapterError):
        a.redeem(c.issue(mode="speak"), ORIGIN, "live")


def test_redeem_binding_echo_validated():
    c, a = make()
    c.echo_mismatch = True
    with pytest.raises(AdapterError) as e:
        a.redeem(c.issue(), ORIGIN, "transcribe")
    assert "bad_binding" in e.value.code


def test_revocation_closes_progress_and_final():
    c, a = make()
    lease = a.redeem(c.issue(), ORIGIN, "transcribe")
    c.revoke_lease(lease.lease_id)
    with pytest.raises(AdapterError) as e:
        a.revalidate(lease)
    assert e.value.status == 410 and not lease.active
    assert lease.closed_reason == "denied"
    with pytest.raises(AdapterError):
        a.progress(lease, "progress", "running")


def test_session_revoke_denies_immediately_not_after_15s():
    c, a = make()
    lease = a.redeem(c.issue(), ORIGIN, "transcribe")
    c.sessions_live = False
    with pytest.raises(AdapterError):
        a.revalidate(lease)


def test_revalidate_timeout_fails_closed():
    c, a = make()
    lease = a.redeem(c.issue(), ORIGIN, "transcribe")
    c.timeout_on.add("/voice/internal/revalidate")
    with pytest.raises(AdapterError) as e:
        a.revalidate(lease)
    assert e.value.status == 503
    assert not lease.active
    assert lease.closed_reason == "validation_unavailable"


def test_revalidate_active_false_closes():
    c, a = make()
    lease = a.redeem(c.issue(), ORIGIN, "transcribe")
    c.revalidate_active = False
    with pytest.raises(AdapterError):
        a.revalidate(lease)
    assert not lease.active


def test_active_drive_loop_closes_on_revocation():
    c, a = make()
    lease = a.redeem(c.issue(), ORIGIN, "transcribe")
    c.revoke_lease(lease.lease_id)
    with pytest.raises(AdapterError):
        a.drive_once(lease)
    assert not lease.active


def test_seconds_cap_enforced_incrementally():
    now = [1000.0]
    c, a = make(clock=lambda: now[0])
    lease = a.redeem(c.issue(), ORIGIN, "transcribe")
    now[0] += 599.9
    a.progress(lease, "progress", "running")  # still inside
    now[0] += 0.2  # past 600
    with pytest.raises(CapExceeded) as e:
        a.progress(lease, "progress", "running")
    assert e.value.which == "seconds" and not lease.active


def test_bytes_cap_enforced_incrementally():
    c, a = make()
    lease = a.redeem(c.issue(), ORIGIN, "transcribe")
    a.note_audio_bytes(lease, 19999999)
    with pytest.raises(CapExceeded) as e:
        a.note_audio_bytes(lease, 1)
    assert e.value.which == "bytes" and not lease.active
    with pytest.raises(AdapterError):
        a.progress(lease, "progress", "running")


def test_terminal_guard_progress_after_final_rejected():
    c, a = make()
    lease = a.redeem(c.issue(), ORIGIN, "transcribe")
    a.final(lease, "done", transcript_text="hi")
    with pytest.raises(AdapterError) as e:
        a.progress(lease, "progress", "running")
    assert e.value.code == "terminal"


def test_terminal_guard_conflicting_second_final_rejected():
    c, a = make()
    lease = a.redeem(c.issue(), ORIGIN, "transcribe")
    a.final(lease, "done", transcript_text="hi")
    with pytest.raises(AdapterError) as e:
        a.final(lease, "done", transcript_text="DIFFERENT")
    assert e.value.status == 409


def test_identical_final_retry_while_live_is_duplicate():
    c, a = make()
    lease = a.redeem(c.issue(), ORIGIN, "transcribe")
    r1 = a.final(lease, "done", transcript_text="hi", answer_text="hello")
    assert r1["accepted"]
    r2 = a.final(lease, "done", transcript_text="hi", answer_text="hello")
    assert r2.get("duplicate") is True


def test_real_lost_commit_is_commit_unknown_no_replay():
    c, a = make()
    lease = a.redeem(c.issue(), ORIGIN, "transcribe")
    c.timeout_on.add("/voice/internal/final")  # commit lands, response lost
    with pytest.raises(CommitUnknown):
        a.final(lease, "done", transcript_text="hi")
    # canonical DID commit; adapter closed and must not replay
    assert ("owner", lease.request_id) in c.finals
    assert not lease.active and lease.closed_reason == "commit_unknown"
    with pytest.raises(AdapterError):
        a.final(lease, "done", transcript_text="hi")  # closed, no replay


def test_final_denied_after_revocation_is_commit_unknown():
    c, a = make()
    lease = a.redeem(c.issue(), ORIGIN, "transcribe")
    c.revoke_lease(lease.lease_id)
    with pytest.raises(CommitUnknown):
        a.final(lease, "done", transcript_text="hi")
    assert not lease.active


def test_seq_is_monotonic():
    c, a = make()
    lease = a.redeem(c.issue(), ORIGIN, "transcribe")
    a.progress(lease, "progress", "running")
    a.progress(lease, "progress", "running")
    assert lease.seq == 2


def test_provider_guard_fails_closed():
    c, a = make()
    lease = a.redeem(c.issue(), ORIGIN, "transcribe")
    c.revoke_lease(lease.lease_id)
    with pytest.raises(AdapterError):
        a.provider_connect_guard(lease)


def test_ticket_never_in_url():
    seen = []
    c = FakeCanonical()
    def spy(method, path, headers, body):
        seen.append(path)
        return c(method, path, headers, body)
    a = VoiceAdapter(spy, "scoped-x", allowed_origins={ORIGIN})
    a.redeem(c.issue(), ORIGIN, "transcribe")
    assert all("ticket" not in p for p in seen)


def test_bad_progress_type_rejected_locally():
    c, a = make()
    lease = a.redeem(c.issue(), ORIGIN, "transcribe")
    with pytest.raises(AdapterError):
        a.progress(lease, "bogus", "running")


def test_final_state_enum_enforced():
    c, a = make()
    lease = a.redeem(c.issue(), ORIGIN, "transcribe")
    with pytest.raises(AdapterError):
        a.final(lease, "running")


def test_revalidate_seconds_matches_contract():
    c, a = make()
    lease = a.redeem(c.issue(), ORIGIN, "transcribe")
    assert lease.revalidate_seconds == REVALIDATE_SECONDS == 15


# -- REV4 rework regression tests (review findings 1-5) ---------------------

def test_f1_builtin_timeout_on_revalidate_fails_closed():
    c, a = make()
    lease = a.redeem(c.issue(), ORIGIN, "transcribe")
    def boom(method, path, headers, body):
        raise TimeoutError("ordinary builtin timeout")
    a._t = boom
    with pytest.raises(AdapterError) as e:
        a.revalidate(lease)
    assert e.value.status == 503 and not lease.active
    assert lease.closed_reason == "validation_unavailable"


def test_f1_oserror_on_revalidate_fails_closed():
    c, a = make()
    lease = a.redeem(c.issue(), ORIGIN, "transcribe")
    def boom(method, path, headers, body):
        raise OSError("connection reset")
    a._t = boom
    with pytest.raises(AdapterError) as e:
        a.revalidate(lease)
    assert e.value.status == 503 and not lease.active


def test_f1_oserror_on_final_is_commit_unknown_no_replay():
    c, a = make()
    lease = a.redeem(c.issue(), ORIGIN, "transcribe")
    def boom(method, path, headers, body):
        raise OSError("response lost")
    a._t = boom
    with pytest.raises(CommitUnknown):
        a.final(lease, "done", transcript_text="hi")
    assert not lease.active and lease.closed_reason == "commit_unknown"
    with pytest.raises(AdapterError):
        a.final(lease, "done", transcript_text="hi")


def test_f1_transport_failure_on_progress_closes():
    c, a = make()
    lease = a.redeem(c.issue(), ORIGIN, "transcribe")
    def boom(method, path, headers, body):
        raise TimeoutError()
    a._t = boom
    with pytest.raises(AdapterError):
        a.progress(lease, "progress", "running")
    assert not lease.active


def test_f2_strict_origin_parse_rejects():
    c, a = make()
    bad = ["https://", "https://user:pw@app.example",
           "https://app.example/path", "https://app.example/?q=1",
           "https://app.example#frag", "https://app.example.evil.com",
           "http://app.example", "", 42]
    for origin in bad:
        t = c.issue()
        with pytest.raises(AdapterError) as e:
            a.redeem(t, origin, "transcribe")
        assert e.value.code.startswith("bad_origin"), origin
        assert not c.tickets[t]["redeemed"]  # rejected before transport


def test_f2_origin_not_on_allowlist_rejected_locally():
    c, a = make()
    t = c.issue(origin="https://evil.example")
    with pytest.raises(AdapterError) as e:
        a.redeem(t, "https://evil.example", "transcribe")
    assert e.value.code == "bad_origin:not_allowlisted"
    assert not c.tickets[t]["redeemed"]


def test_f3_revalidate_binding_mismatch_closes():
    c, a = make()
    lease = a.redeem(c.issue(), ORIGIN, "transcribe")
    c.bad_revalidate_binding = True
    with pytest.raises(AdapterError) as e:
        a.revalidate(lease)
    assert e.value.code == "bad_binding:revalidate" and not lease.active
    assert lease.closed_reason == "bad_binding"


def test_f4_run_turn_wall_clock_cadence_under_blocking_work():
    now = [1000.0]
    c, a = make(clock=lambda: now[0])
    calls = []
    release = threading.Event()
    orig = c.__call__
    def spy(method, path, headers, body):
        if path == "/voice/internal/revalidate":
            calls.append(now[0])
            if len(calls) >= 3:
                release.set()   # unblock work exactly after the 3rd check
        return orig(method, path, headers, body)
    a._t = spy
    lease = a.redeem(c.issue(), ORIGIN, "transcribe")
    def work(l, cancel):
        release.wait(10)   # blocks far longer than one cadence interval
        return True
    done = {}
    def run():
        done["r"] = a.run_turn(lease, work,
                               sleep=lambda s: now.__setitem__(0, now[0] + s))
    th = threading.Thread(target=run)
    th.start()
    th.join(timeout=10)
    release.set()
    assert done.get("r") is True
    assert len(calls) >= 3, f"cadence skipped under blocking work: {calls}"
    gaps = [round(b - a_, 6) for a_, b in zip(calls, calls[1:])]
    assert all(abs(g - 15.0) < 1e-6 for g in gaps), f"non-wall-clock gaps: {gaps}"


def test_f4_run_turn_denial_during_blocking_work_closes():
    now = [1000.0]
    c, a = make(clock=lambda: now[0])
    lease = a.redeem(c.issue(), ORIGIN, "transcribe")
    release = threading.Event()
    def work(l, cancel):
        release.wait(10)
        return True
    outcome = {}
    def run():
        try:
            a.run_turn(lease, work,
                       sleep=lambda s: now.__setitem__(0, now[0] + s))
        except AdapterError as e:
            outcome["e"] = e
    th = threading.Thread(target=run)
    th.start()
    time.sleep(0.2)
    c.revoke_lease(lease.lease_id)   # next scheduled revalidate denies
    th.join(timeout=10); release.set()
    assert isinstance(outcome.get("e"), AdapterError)
    assert not lease.active


def test_f5_negative_increment_rejected():
    c, a = make()
    lease = a.redeem(c.issue(), ORIGIN, "transcribe")
    a.note_audio_bytes(lease, 1000)
    with pytest.raises(AdapterError) as e:
        a.note_audio_bytes(lease, -500)
    assert e.value.code == "bad_increment"
    assert lease.bytes_used == 1000 and lease.active


def test_f5_server_caps_clamped_to_contract_bounds():
    now = [1000.0]
    c, a = make(clock=lambda: now[0])
    c.loose_caps = True   # server "promises" 999999s / 1TB
    lease = a.redeem(c.issue(), ORIGIN, "transcribe")
    assert lease.audio_limits["max_seconds"] == 600
    assert lease.audio_limits["max_bytes"] == 20000000
    now[0] += 600.0       # contract bound, far under the server's 999999
    with pytest.raises(CapExceeded) as e:
        a.progress(lease, "progress", "running")
    assert e.value.which == "seconds"


# -- REV5 rework regression tests (re-review findings 6-7) ------------------

def test_f6_denied_startup_worker_never_runs():
    """First validation happens BEFORE the worker starts: a denied startup
    must produce ZERO effects - the work callable is never invoked."""
    c, a = make()
    lease = a.redeem(c.issue(), ORIGIN, "transcribe")
    c.revoke_lease(lease.lease_id)   # first drive_once will deny
    effects = []
    started = []
    def work(l, cancel):
        started.append(True)
        effects.append("effect")
        return True
    with pytest.raises(AdapterError):
        a.run_turn(lease, work)
    assert started == [] and effects == []
    assert not lease.active


def test_f6_revocation_during_blocked_work_cancels_and_joins():
    """Revocation mid-work: cancel is observed, the worker is joined inside
    run_turn, and NO effect is recorded after the lease closed."""
    now = [1000.0]
    c, a = make(clock=lambda: now[0])
    lease = a.redeem(c.issue(), ORIGIN, "transcribe")
    effects, closed_at = [], []
    def work(l, cancel):
        while not cancel.is_set():
            effects.append(now[0])
            time.sleep(0.005)
        effects.append("after-cancel-observed")  # in-flight cleanup only
        return True
    outcome = {}
    def run():
        try:
            a.run_turn(lease, work,
                       sleep=lambda s: now.__setitem__(0, now[0] + s))
        except AdapterError as e:
            outcome["e"] = e
    th = threading.Thread(target=run)
    th.start()
    deadline = time.time() + 10
    while len(effects) < 2 and time.time() < deadline:
        time.sleep(0.01)
    c.revoke_lease(lease.lease_id)
    th.join(timeout=10)
    assert not th.is_alive()          # run_turn returned (worker joined)
    assert isinstance(outcome.get("e"), AdapterError)
    assert not lease.active
    # Worker observed cancel and exited; only its own cleanup line may
    # follow, and NOTHING after run_turn returned:
    marker = len(effects)
    time.sleep(0.2)
    assert len(effects) == marker     # zero effects after close
    assert effects[-1] == "after-cancel-observed"


def test_f6_success_path_joins_worker_cleanly():
    c, a = make()
    lease = a.redeem(c.issue(), ORIGIN, "transcribe")
    alive = []
    def work(l, cancel):
        alive.append(threading.current_thread())
        return "turn-result"
    r = a.run_turn(lease, work)
    assert r == "turn-result"
    assert not alive[0].is_alive()    # joined before return


def test_f7_allowlist_required_no_implicit_trust():
    c = FakeCanonical()
    with pytest.raises(ValueError):
        VoiceAdapter(c, "scoped-x")                 # absent
    with pytest.raises(ValueError):
        VoiceAdapter(c, "scoped-x", allowed_origins=[])   # empty
    with pytest.raises(ValueError):
        VoiceAdapter(c, "scoped-x", allowed_origins=None)
    # no module-level default remains to smuggle authority back in
    import adapter as mod
    assert not hasattr(mod, "DEFAULT_ALLOWED_ORIGINS")
