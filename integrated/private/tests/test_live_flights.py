"""Step A synthetic routing tests (review-#1 fold): flight lookup contract
+ escalation dispatch. NO network: cr_web / cr_llm / cr_citations / cr_group
are stubbed unless a test exercises the REAL checker with only LLM/web
stubbed.
"""
from datetime import datetime
from zoneinfo import ZoneInfo

import pytest

import cr_live
import cr_agent as A

NOW_LOCAL = datetime(2026, 10, 12, 9, 0, tzinfo=ZoneInfo("Asia/Calcutta"))
ASK = "cheapest flight tomorrow Delhi to Kolkata"


def _stub_web(monkeypatch, pages=(), results=()):
    monkeypatch.setattr(cr_live.W, "search", lambda *a, **k: list(results))
    monkeypatch.setattr(cr_live.W, "fetch", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("no net")))
    monkeypatch.setattr(cr_live.W, "research", lambda *a, **k: {"results": list(results), "pages": list(pages)})


def _boom(*a, **k):
    raise RuntimeError("down")


# --- normalization ------------------------------------------------------

def test_flight_request_normalizes_absolute_date():
    n = cr_live.flight_request(ASK, NOW_LOCAL)
    assert n["kind"] == "flight"
    assert n["origin"] == "delhi" and n["destination"] == "kolkata"
    assert n["origin_iata"] == "DEL" and n["destination_iata"] == "CCU"
    assert n["date"] == "2026-10-13"  # tomorrow in the USER timezone
    assert n["date_state"] == "resolved"
    assert n["missing"] == [] and n["unsupported"] == []


def test_relative_date_needs_clock():
    n = cr_live.flight_request(ASK, None)
    assert n["date"] is None and n["date_state"] == "needs_clock"
    assert "date" in n["missing"]


def test_explicit_iso_date_resolves_without_clock():
    n = cr_live.flight_request("flight Delhi to Kolkata on 2026-10-20", None)
    assert n["date"] == "2026-10-20" and n["date_state"] == "resolved"
    assert n["missing"] == []


def test_invalid_iso_date_is_unsupported():
    n = cr_live.flight_request("flight Delhi to Kolkata on 2026-99-99", NOW_LOCAL)
    assert n["date"] is None
    assert any(u.startswith("date:invalid") for u in n["unsupported"])


def test_explicit_date_beats_negated_relative():
    n = cr_live.flight_request(
        "flight Delhi to Kolkata on 2026-10-20, not tomorrow", NOW_LOCAL)
    assert n["date"] == "2026-10-20"
    assert n["unsupported"] == []


def test_explicit_plus_relative_is_a_conflict():
    n = cr_live.flight_request(
        "flight Delhi to Kolkata on 2026-10-20 tomorrow", NOW_LOCAL)
    assert n["date"] == "2026-10-20"
    assert any(u.startswith("date:conflict") for u in n["unsupported"])


def test_next_friday_is_unsupported_and_does_not_contaminate_route():
    n = cr_live.flight_request("flights Delhi to Kolkata next Friday", NOW_LOCAL)
    assert n["destination"] == "kolkata"
    assert n["date"] is None
    assert any(u.startswith("date:unsupported") for u in n["unsupported"])


def test_passengers_cabin_roundtrip_nonstop_parsed():
    n = cr_live.flight_request(
        "flights Delhi to Kolkata tomorrow 2 adults business nonstop", NOW_LOCAL)
    assert n["passengers"] == 2 and n["cabin"] == "business"
    assert n["nonstop"] is True
    r = cr_live.flight_request("round-trip flights Delhi to Kolkata tomorrow", NOW_LOCAL)
    assert r["trip"] == "roundtrip" and "return_date" in r["missing"]


def test_find_flights_phrasing_routes_and_normalizes():
    n = cr_live.flight_request("find flights Delhi to Kolkata tomorrow", NOW_LOCAL)
    assert n["origin"] == "delhi" and n["destination"] == "kolkata"
    assert cr_live.requested("find flights Delhi to Kolkata tomorrow") is True
    assert cr_live.requested("flight from Delhi to Kolkata tomorrow") is True


def test_non_flight_text_is_not_a_flight_request():
    assert cr_live.flight_request("weather in Pune", NOW_LOCAL) is None


# --- lookup contract ----------------------------------------------------

def test_lookup_flight_insufficient_and_escalation_eligible(monkeypatch):
    _stub_web(monkeypatch,
              results=[{"url": "https://example.com/fares", "title": "Flights DEL CCU"}])
    r = cr_live.lookup(ASK, NOW_LOCAL)
    assert r.status == cr_live.STATUS_INSUFFICIENT
    assert r.escalation_eligible is True
    assert r.failure_code == cr_live.FAILURE_CONTENT
    assert r.normalized["date"] == "2026-10-13"
    assert "next 7 days" not in r.text and "next 30 days" not in r.text
    assert "2026-10-13" in r.text
    assert "Delhi" in r.text and "Kolkata" in r.text


def test_incomplete_flight_with_real_checker_is_never_verified(monkeypatch):
    """Peer's blocker-1 probe: a missing-date flight ask + a starting-fare
    page that the REAL cr_citations checker would pass must NOT verify."""
    _stub_web(monkeypatch,
              pages=[{"url": "https://example.com/fares",
                      "text": "Delhi to Kolkata flights starting from Rs. 4,500"}],
              results=[{"url": "https://example.com/fares", "title": "Fares"}])
    import cr_citations
    monkeypatch.setattr(cr_citations, "checked_answer",
                        lambda *a, **k: "Fares start at Rs. 4,500.")
    import cr_llm
    monkeypatch.setattr(cr_llm, "generate", lambda *a, **k: {"text": "x"})
    n = cr_live.flight_request("cheapest flight Delhi to Kolkata", NOW_LOCAL)
    assert "date" in n["missing"]
    r = cr_live.lookup("cheapest flight Delhi to Kolkata", NOW_LOCAL)
    assert r.status == cr_live.STATUS_INSUFFICIENT  # NEVER generic verified
    assert r.escalation_eligible is False
    assert r.failure_code == cr_live.FAILURE_INCOMPLETE
    assert "departure date" in r.text


def test_search_failure_preserves_normalized_and_eligibility(monkeypatch):
    monkeypatch.setattr(cr_live.W, "search", _boom)
    r = cr_live.lookup(ASK, NOW_LOCAL)
    assert r.status == cr_live.STATUS_UNAVAILABLE
    assert r.failure_code == cr_live.FAILURE_SEARCH
    assert r.normalized["origin"] == "delhi"
    assert r.normalized["destination"] == "kolkata"
    assert r.normalized["date"] == "2026-10-13"  # route/date NOT dropped
    assert r.escalation_eligible is True  # denial/unreadable, complete ask


def test_research_failure_preserves_normalized(monkeypatch):
    monkeypatch.setattr(cr_live.W, "research", _boom)
    r = cr_live.lookup("Research focus [web]: " + ASK, NOW_LOCAL)
    assert r.status == cr_live.STATUS_UNAVAILABLE
    assert r.failure_code == cr_live.FAILURE_SOURCES
    assert r.normalized["date"] == "2026-10-13"


def test_non_flight_search_failure(monkeypatch):
    monkeypatch.setattr(cr_live.W, "search", _boom)
    r = cr_live.lookup("current price of silver today", NOW_LOCAL)
    assert r.status == cr_live.STATUS_UNAVAILABLE
    assert r.failure_code == cr_live.FAILURE_SEARCH
    assert r.escalation_eligible is False


def test_unsupported_flight_is_not_eligible(monkeypatch):
    _stub_web(monkeypatch,
              results=[{"url": "https://example.com/f", "title": "F"}])
    r = cr_live.lookup("flights Delhi to Kolkata next Friday", NOW_LOCAL)
    assert r.status == cr_live.STATUS_INSUFFICIENT
    assert r.escalation_eligible is False
    assert "exact date" in r.text


def test_static_guard_never_verifies_and_typed_static_only():
    n = cr_live.flight_request(ASK, NOW_LOCAL)
    pages = [{"url": "https://example.com/fares",
              "text": "Delhi to Kolkata 2026-10-13 Rs. 4,500 total available"}]
    ok, reason = cr_live.validate_static_flight_evidence(n, pages)
    assert ok is False  # positive validated-inventory contract reserved for B/C
    ok2, _ = cr_live.validate_static_flight_evidence(n, [])
    assert ok2 is False


def test_result_invariants_enforced():
    with pytest.raises(ValueError):
        cr_live.LookupResult("bogus", "t", "now")
    with pytest.raises(ValueError):
        cr_live.LookupResult(cr_live.STATUS_VERIFIED, "t", "now", "content_unverified")
    with pytest.raises(ValueError):
        cr_live.LookupResult(cr_live.STATUS_VERIFIED, "t", "now",
                             escalation_eligible=True)
    r = cr_live.LookupResult(cr_live.STATUS_INSUFFICIENT, "t", "now",
                             cr_live.FAILURE_CONTENT, {"a": 1})
    with pytest.raises(TypeError):
        r.normalized["a"] = 2  # immutable


def test_news_wrapper_behavior_preserved(monkeypatch):
    import cr_group
    monkeypatch.setattr(cr_group, "answer", lambda req: "NEWS-OK")
    r = cr_live.lookup("latest news today", NOW_LOCAL)
    assert r.status == cr_live.STATUS_VERIFIED and r.text == "NEWS-OK"
    assert cr_live.answer("latest news today") == "NEWS-OK"


def test_news_unavailable_prose_is_not_upgraded(monkeypatch):
    import cr_group
    monkeypatch.setattr(cr_group, "answer",
                        lambda req: "I could not verify any news source.")
    r = cr_live.lookup("latest news today", NOW_LOCAL)
    assert r.status == cr_live.STATUS_INSUFFICIENT


# --- agent dispatch -----------------------------------------------------

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
    norm = dict(dispatched)["search_flights"]
    assert norm["origin"] == "delhi" and norm["destination"] == "kolkata"
    assert norm["date"] == "2026-10-13"
    # capability not enabled yet -> the honest unverified reply stands
    assert "2026-10-13" in reply
    assert "next 7 days" not in reply
    assert meta["tools"] == ["live_public_lookup"]  # no false tool credit


def test_agent_no_dispatch_for_incomplete_flight(monkeypatch):
    _stub_web(monkeypatch,
              results=[{"url": "https://example.com/fares", "title": "F"}])
    dispatched = []
    _stub_agent(monkeypatch, dispatched)
    reply, meta = A.respond(123, 456, "cheapest flight Delhi to Kolkata")
    assert dispatched == []  # no wrong eligible dispatch on missing date
    assert "departure date" in reply


def test_agent_returns_verified_lookup_directly(monkeypatch):
    import cr_group
    monkeypatch.setattr(cr_group, "answer", lambda req: "NEWS-OK")
    dispatched = []
    _stub_agent(monkeypatch, dispatched)
    reply, meta = A.respond(123, 456, "latest news today")
    assert reply == "NEWS-OK"
    assert dispatched == []  # no escalation for a verified quick result
