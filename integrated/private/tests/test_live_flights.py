"""Step A synthetic routing tests: the flight lookup contract + escalation
dispatch. NO network: cr_web / cr_llm / cr_citations / cr_group are stubbed.
"""
from datetime import datetime
from zoneinfo import ZoneInfo

import cr_live
import cr_agent as A

NOW_LOCAL = datetime(2026, 10, 12, 9, 0, tzinfo=ZoneInfo("Asia/Calcutta"))
ASK = "cheapest flight tomorrow Delhi to Kolkata"


def _stub_web(monkeypatch, pages=(), results=()):
    monkeypatch.setattr(cr_live.W, "search", lambda *a, **k: list(results))
    monkeypatch.setattr(cr_live.W, "fetch", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("no net")))
    monkeypatch.setattr(cr_live.W, "research", lambda *a, **k: {"results": list(results), "pages": list(pages)})


def test_flight_request_normalizes_absolute_date():
    n = cr_live.flight_request(ASK, NOW_LOCAL)
    assert n["kind"] == "flight"
    assert n["origin"] == "delhi" and n["destination"] == "kolkata"
    assert n["origin_iata"] == "DEL" and n["destination_iata"] == "CCU"
    assert n["date"] == "2026-10-13"  # tomorrow in the USER timezone
    assert n["missing"] == []


def test_flight_request_marks_missing_date_without_local_now():
    n = cr_live.flight_request(ASK, None)
    assert "date" in n["missing"]


def test_non_flight_text_is_not_a_flight_request():
    assert cr_live.flight_request("weather in Pune", NOW_LOCAL) is None


def test_lookup_flight_insufficient_and_escalation_eligible(monkeypatch):
    _stub_web(monkeypatch,
              results=[{"url": "https://example.com/fares", "title": "Flights DEL CCU"}])
    r = cr_live.lookup(ASK, NOW_LOCAL)
    assert r.status == cr_live.STATUS_INSUFFICIENT
    assert r.escalation_eligible is True
    assert r.failure_code == cr_live.FAILURE_CONTENT
    assert r.normalized["date"] == "2026-10-13"
    # the spurious 7/30-day window question is GONE; the supplied date is preserved
    assert "next 7 days" not in r.text and "next 30 days" not in r.text
    assert "2026-10-13" in r.text
    assert "Delhi" in r.text and "Kolkata" in r.text


def test_lookup_search_failure_is_unavailable(monkeypatch):
    monkeypatch.setattr(cr_live.W, "search",
                        lambda *a, **k: (_ for _ in ()).throw(RuntimeError("down")))
    r = cr_live.lookup("current price of silver today", NOW_LOCAL)
    assert r.status == cr_live.STATUS_UNAVAILABLE
    assert r.failure_code == cr_live.FAILURE_SEARCH
    assert r.escalation_eligible is False


def test_validator_rejects_static_starting_fare_pages():
    n = cr_live.flight_request(ASK, NOW_LOCAL)
    pages = [{"url": "https://example.com/fares",
              "text": "Delhi to Kolkata flights starting from Rs. 4,500 on 2026-10-13"}]
    ok, reason = cr_live.validate_flight_evidence(n, pages)
    assert ok is False  # static advertised fares are never verified inventory


def test_news_wrapper_behavior_preserved(monkeypatch):
    import cr_group
    monkeypatch.setattr(cr_group, "answer", lambda req: "NEWS-OK")
    r = cr_live.lookup("latest news today", NOW_LOCAL)
    assert r.status == cr_live.STATUS_VERIFIED and r.text == "NEWS-OK"
    assert cr_live.answer("latest news today") == "NEWS-OK"


def _stub_agent(monkeypatch, dispatched):
    monkeypatch.setattr(A.mem, "touch_user", lambda *a: None)
    monkeypatch.setattr(A.mem, "add_message", lambda *a: None)
    monkeypatch.setattr(A.mem, "extract_async", lambda *a: None)
    monkeypatch.setattr(A, "handle_confirmation", lambda *a: None)
    monkeypatch.setattr(A, "_history_contents", lambda *a: [])
    monkeypatch.setattr(A, "build_system", lambda *a, **k: "test")
    monkeypatch.setattr(A.T, "now_local", lambda uid: NOW_LOCAL)

    def fake_run(name, args, ctx):
        dispatched.append((name, args))
        return {"ok": False, "verified": False,
                "error": "capability not enabled"}
    monkeypatch.setattr(A.T, "run", fake_run)


def test_agent_dispatches_flight_escalation(monkeypatch):
    _stub_web(monkeypatch,
              results=[{"url": "https://example.com/fares", "title": "Flights DEL CCU"}])
    dispatched = []
    _stub_agent(monkeypatch, dispatched)
    reply, meta = A.respond(123, 456, ASK)
    names = [n for n, _ in dispatched]
    assert "search_flights" in names  # escalation dispatched, attempt 2
    norm = dict(dispatched)[ "search_flights" ]
    assert norm["origin"] == "delhi" and norm["destination"] == "kolkata"
    assert norm["date"] == "2026-10-13"
    # capability not enabled yet -> the honest unverified reply stands
    assert "2026-10-13" in reply
    assert "next 7 days" not in reply and "cheapest" not in reply.lower().split("fare")[0][-12:]
    assert meta["tools"] == ["live_public_lookup"]  # no false tool credit


def test_agent_returns_verified_lookup_directly(monkeypatch):
    import cr_group
    monkeypatch.setattr(cr_group, "answer", lambda req: "NEWS-OK")
    dispatched = []
    _stub_agent(monkeypatch, dispatched)
    reply, meta = A.respond(123, 456, "latest news today")
    assert reply == "NEWS-OK"
    assert dispatched == []  # no escalation for a verified quick result
