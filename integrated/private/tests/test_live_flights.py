"""Step A synthetic routing tests (review-#2 fold): flight lookup contract
+ escalation dispatch. NO network: cr_web / cr_llm / cr_group are stubbed;
the CITATION CHECKER RUNS REAL with only the LLM endpoint stubbed.
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


def test_negated_relative_with_newline():
    n = cr_live.flight_request(
        "flight Delhi to Kolkata on 2026-10-20 not\ntomorrow", NOW_LOCAL)
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


def test_spelled_out_passenger_count_parses():
    n = cr_live.flight_request(
        "flights Delhi to Kolkata two adults tomorrow", NOW_LOCAL)
    assert n["passengers"] == 2
    assert n["destination"] == "kolkata"  # 'two' never contaminates the city


def test_children_are_unsupported_not_defaulted():
    n = cr_live.flight_request(
        "flights Delhi to Kolkata two adults and a child tomorrow", NOW_LOCAL)
    assert n["passengers"] == 2 and n["destination"] == "kolkata"
    assert "passengers:children-infants" in n["unsupported"]


def test_zero_adults_is_invalid_not_zero_eligible():
    n = cr_live.flight_request("flight Delhi to Kolkata tomorrow 0 adults", NOW_LOCAL)
    assert any(u.startswith("passengers:invalid") for u in n["unsupported"])


def test_cabin_does_not_contaminate_destination():
    n = cr_live.flight_request(
        "cheapest flight Delhi to Kolkata business class tomorrow", NOW_LOCAL)
    assert n["destination"] == "kolkata"
    assert n["cabin"] == "business"


def test_negated_cabin_uses_the_positive_one():
    n = cr_live.flight_request(
        "flight Delhi to Kolkata tomorrow not business, economy only", NOW_LOCAL)
    assert n["cabin"] == "economy"


def test_only_negated_cabin_is_unsupported_not_defaulted():
    n = cr_live.flight_request(
        "flight Delhi to Kolkata tomorrow not business", NOW_LOCAL)
    assert n["cabin"] is None
    assert "cabin:only-negated" in n["unsupported"]


def test_negated_nonstop_stays_false():
    n = cr_live.flight_request(
        "flight Delhi to Kolkata tomorrow not nonstop", NOW_LOCAL)
    assert n["nonstop"] is False


def test_explicit_return_date_parsed():
    n = cr_live.flight_request(
        "flight Delhi to Kolkata on 2026-10-20 returning 2026-10-25", NOW_LOCAL)
    assert n["trip"] == "roundtrip"
    assert n["return_date"] == "2026-10-25"
    assert n["missing"] == [] and n["unsupported"] == []


def test_return_before_departure_is_unsupported():
    n = cr_live.flight_request(
        "flight Delhi to Kolkata on 2026-10-20 returning 2026-10-12", NOW_LOCAL)
    assert any(u.startswith("date:return-order") for u in n["unsupported"])


def test_returning_next_relative_is_unsupported():
    n = cr_live.flight_request(
        "flight Delhi to Kolkata on 2026-10-20 returning next Friday", NOW_LOCAL)
    assert any(u.startswith("date:return-unsupported") for u in n["unsupported"])


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


def test_incomplete_flight_never_hits_network_and_is_never_verified(monkeypatch):
    """Blocker D: an incomplete ask resolves BEFORE the attempt - no
    search call, no search-failure report, just the date clarification."""
    calls = []
    def boom(*a, **k):
        calls.append(1)
        raise RuntimeError("down")
    monkeypatch.setattr(cr_live.W, "search", boom)
    monkeypatch.setattr(cr_live.W, "research", boom)
    r = cr_live.lookup("cheapest flight Delhi to Kolkata", NOW_LOCAL)
    assert calls == []  # NO network before clarification
    assert r.status == cr_live.STATUS_INSUFFICIENT
    assert r.failure_code == cr_live.FAILURE_INCOMPLETE
    assert r.escalation_eligible is False
    assert "departure date" in r.text


def test_unsupported_flight_never_hits_network(monkeypatch):
    calls = []
    def boom(*a, **k):
        calls.append(1)
        raise RuntimeError("down")
    monkeypatch.setattr(cr_live.W, "search", boom)
    r = cr_live.lookup("flights Delhi to Kolkata next Friday", NOW_LOCAL)
    assert calls == []
    assert r.status == cr_live.STATUS_INSUFFICIENT
    assert r.failure_code == cr_live.FAILURE_UNSUPPORTED
    assert r.escalation_eligible is False
    assert "exact date" in r.text


def test_real_checker_negative_complete_ask(monkeypatch):
    """REAL cr_citations.checked_answer runs (only the LLM endpoint is
    stubbed); its rejection drives INSUFFICIENT + eligible for a
    complete, supported ask."""
    page_text = ("Delhi to Kolkata flights are listed here. "
                 "IndiGo 6E-223 departs 06:15 daily from terminal 1.")
    _stub_web(monkeypatch,
              pages=[{"url": "https://example.com/fares", "text": page_text}],
              results=[{"url": "https://example.com/fares", "title": "Fares"}])
    import cr_llm
    monkeypatch.setattr(cr_llm, "generate", lambda *a, **k: {"text": "draft"})
    monkeypatch.setattr(cr_llm, "ask_json", lambda *a, **k: {
        "claims": [{"claim": "Fares from Rs 9,999",
                    "url": "https://example.com/fares",
                    "quote": "this exact string is NOT in the page text"}],
        "missing": ""})
    r = cr_live.lookup(
        "Research focus [web]: cheapest flight Delhi to Kolkata on 2026-10-20",
        NOW_LOCAL)
    assert r.status == cr_live.STATUS_INSUFFICIENT
    assert r.failure_code == cr_live.FAILURE_CONTENT
    assert r.escalation_eligible is True  # complete+supported denial
    assert "9,999" not in r.text  # rejected claim never surfaces


def test_real_checker_positive_control_still_guard_blocked(monkeypatch):
    """Positive control: the REAL checker ACCEPTS a fully-quoted claim,
    and the STATIC-ONLY guard still blocks verification - the page URL
    lands in evidence, the claim text never ships as verified."""
    page_text = ("Delhi to Kolkata flights are listed here. "
                 "IndiGo 6E-223 departs 06:15 daily from terminal 1.")
    _stub_web(monkeypatch,
              pages=[{"url": "https://example.com/fares", "text": page_text}],
              results=[{"url": "https://example.com/fares", "title": "Fares"}])
    import cr_llm
    monkeypatch.setattr(cr_llm, "generate", lambda *a, **k: {"text": "draft"})
    monkeypatch.setattr(cr_llm, "ask_json", lambda *a, **k: {
        "claims": [{"claim": "IndiGo 6E-223 departs 06:15 daily",
                    "url": "https://example.com/fares",
                    "quote": "IndiGo 6E-223 departs 06:15 daily from terminal 1."}],
        "missing": ""})
    r = cr_live.lookup(
        "Research focus [web]: cheapest flight Delhi to Kolkata on 2026-10-20",
        NOW_LOCAL)
    assert r.status == cr_live.STATUS_INSUFFICIENT  # guard, not checker
    assert r.failure_code == cr_live.FAILURE_CONTENT
    assert r.escalation_eligible is True
    assert "https://example.com/fares" in r.fetched_pages  # fetched+considered
    assert "https://example.com/fares" in r.search_leads
    assert "IndiGo" not in r.text  # checker-passed claim is NOT a fare quote


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
    with pytest.raises(ValueError):
        # eligible with a non-denial failure code
        cr_live.LookupResult(cr_live.STATUS_INSUFFICIENT, "t", "now",
                             cr_live.FAILURE_INCOMPLETE,
                             escalation_eligible=True)
    with pytest.raises(ValueError):
        # eligible flight WITH missing fields
        cr_live.LookupResult(cr_live.STATUS_INSUFFICIENT, "t", "now",
                             cr_live.FAILURE_CONTENT,
                             {"kind": "flight", "missing": ["date"]},
                             escalation_eligible=True)
    with pytest.raises(ValueError):
        # non-verified flight WITHOUT a failure code
        cr_live.LookupResult(cr_live.STATUS_INSUFFICIENT, "t", "now", "",
                             {"kind": "flight", "missing": ["date"]})
    r = cr_live.LookupResult(cr_live.STATUS_INSUFFICIENT, "t", "now",
                             cr_live.FAILURE_CONTENT, {"a": 1})
    with pytest.raises(TypeError):
        r.normalized["a"] = 2  # immutable


def test_normalized_is_DEEP_immutable():
    n = cr_live.flight_request(ASK, NOW_LOCAL)
    r = cr_live.LookupResult(cr_live.STATUS_INSUFFICIENT, "t", "now",
                             cr_live.FAILURE_CONTENT, n,
                             escalation_eligible=True)
    assert isinstance(r.normalized["missing"], tuple)
    with pytest.raises(AttributeError):
        r.normalized["missing"].append("date")  # deep-frozen list
    with pytest.raises(TypeError):
        r.normalized["origin"] = "mumbai"


def test_news_wrapper_behavior_preserved(monkeypatch):
    import cr_group
    monkeypatch.setattr(cr_group, "answer", lambda req: "NEWS-OK")
    r = cr_live.lookup("latest news today", NOW_LOCAL)
    assert r.status == cr_live.STATUS_VERIFIED and r.text == "NEWS-OK"
    assert cr_live.answer("latest news today") == "NEWS-OK"


@pytest.mark.parametrize("prose", [
    "I could not verify any news source.",
    "The pages I read do not support a verified answer.",
    "No dated news results returned",
    "Public lookup failed this time",
    "The public search returned no readable sources",
])
def test_news_failure_prose_is_not_upgraded(monkeypatch, prose):
    import cr_group
    monkeypatch.setattr(cr_group, "answer", lambda req: prose)
    r = cr_live.lookup("latest news today", NOW_LOCAL)
    assert r.status == cr_live.STATUS_INSUFFICIENT


def test_mixed_news_flight_ask_is_flight_gated(monkeypatch):
    """Blocker C: 'latest news cheapest flight ...' must NOT verify via
    the news wrapper - it goes through the flight gate."""
    import cr_group
    monkeypatch.setattr(cr_group, "answer", lambda req: "NEWS-OK")
    monkeypatch.setattr(cr_live.W, "search", _boom)
    r = cr_live.lookup(
        "latest news cheapest flight Delhi to Kolkata tomorrow", NOW_LOCAL)
    assert r.status == cr_live.STATUS_UNAVAILABLE  # flight path, not news
    assert r.failure_code == cr_live.FAILURE_SEARCH
    assert r.normalized["origin"] == "delhi"
    assert r.normalized["destination"] == "kolkata"


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


def test_agent_no_dispatch_for_unsupported_constraints(monkeypatch):
    dispatched = []
    _stub_agent(monkeypatch, dispatched)
    reply, meta = A.respond(
        123, 456, "flights Delhi to Kolkata two adults and a child tomorrow")
    assert dispatched == []  # children: clarified, never dispatched
    assert "children or infants" in reply
    assert "how many adults" not in reply  # never propose dropping travelers


def test_agent_returns_verified_lookup_directly(monkeypatch):
    import cr_group
    monkeypatch.setattr(cr_group, "answer", lambda req: "NEWS-OK")
    dispatched = []
    _stub_agent(monkeypatch, dispatched)
    reply, meta = A.respond(123, 456, "latest news today")
    assert reply == "NEWS-OK"
    assert dispatched == []  # no escalation for a verified quick result


# --- review-#3 narrow vectors ------------------------------------------

def test_return_only_iso_is_never_stolen_as_departure():
    n = cr_live.flight_request(
        "flight Delhi to Kolkata returning 2026-10-25", NOW_LOCAL)
    assert n["date"] is None and "date" in n["missing"]
    assert n["return_date"] == "2026-10-25"
    assert n["unsupported"] == []


def test_outbound_relative_plus_return_iso_are_independent():
    n = cr_live.flight_request(
        "flight Delhi to Kolkata tomorrow returning 2026-10-25", NOW_LOCAL)
    assert n["date"] == "2026-10-13"  # tomorrow, NOT the return ISO
    assert n["date_state"] == "resolved"
    assert n["return_date"] == "2026-10-25"
    assert n["missing"] == [] and n["unsupported"] == []


def test_return_only_ask_clarifies_without_network(monkeypatch):
    calls = []
    def boom(*a, **k):
        calls.append(1)
        raise RuntimeError("down")
    monkeypatch.setattr(cr_live.W, "search", boom)
    r = cr_live.lookup("flight Delhi to Kolkata returning 2026-10-25", NOW_LOCAL)
    assert calls == []
    assert r.status == cr_live.STATUS_INSUFFICIENT
    assert r.failure_code == cr_live.FAILURE_INCOMPLETE
    assert "departure date" in r.text


def test_date_alternatives_are_unsupported_not_first_pick():
    n = cr_live.flight_request(
        "flight Delhi to Kolkata on 2026-10-20 or 2026-10-21", NOW_LOCAL)
    assert n["date"] is None
    assert any(u.startswith("date:alternatives") for u in n["unsupported"])


def test_party_phrasing_is_unsupported_not_one_adult():
    n = cr_live.flight_request(
        "flight Delhi to Kolkata tomorrow for me and my wife", NOW_LOCAL)
    assert "passengers:party-unparsed" in n["unsupported"]


def test_conflicting_passenger_counts_are_unsupported():
    n = cr_live.flight_request(
        "flight Delhi to Kolkata tomorrow 2 adults and 3 adults", NOW_LOCAL)
    assert any(u.startswith("passengers:conflict") for u in n["unsupported"])


def test_time_preference_is_unsupported_not_dropped():
    n = cr_live.flight_request(
        "flight Delhi to Kolkata tomorrow before 10am", NOW_LOCAL)
    assert any(u.startswith("time:preference") for u in n["unsupported"])


def test_via_routing_is_unsupported_and_not_in_destination():
    n = cr_live.flight_request(
        "flight Delhi to Kolkata via Mumbai tomorrow", NOW_LOCAL)
    assert n["destination"] == "kolkata"
    assert any(u.startswith("route:via") for u in n["unsupported"])


def test_airline_preference_is_unsupported_not_dropped():
    n = cr_live.flight_request(
        "IndiGo flight Delhi to Kolkata tomorrow", NOW_LOCAL)
    assert "airline:preference" in n["unsupported"]


def test_supplied_constraints_make_lookup_ineligible(monkeypatch):
    monkeypatch.setattr(cr_live.W, "search", _boom)
    for ask in ("flight Delhi to Kolkata tomorrow for me and my wife",
                "flight Delhi to Kolkata tomorrow 2 adults and 3 adults",
                "flight Delhi to Kolkata tomorrow before 10am",
                "flight Delhi to Kolkata via Mumbai tomorrow"):
        r = cr_live.lookup(ask, NOW_LOCAL)
        assert r.status == cr_live.STATUS_INSUFFICIENT, ask
        assert r.failure_code == cr_live.FAILURE_UNSUPPORTED, ask
        assert r.escalation_eligible is False, ask


def test_eligibility_requires_exact_typed_flight_shape():
    # bare kind marker is NOT a dispatchable request
    with pytest.raises(ValueError):
        cr_live.LookupResult(cr_live.STATUS_INSUFFICIENT, "t", "now",
                             cr_live.FAILURE_CONTENT, {"kind": "flight"},
                             escalation_eligible=True)
    # a non-flight kind can NEVER be eligible
    with pytest.raises(ValueError):
        cr_live.LookupResult(cr_live.STATUS_INSUFFICIENT, "t", "now",
                             cr_live.FAILURE_CONTENT, {"kind": "weather"},
                             escalation_eligible=True)
    # a flight shape with an invalid date string is not dispatchable
    with pytest.raises(ValueError):
        cr_live.LookupResult(
            cr_live.STATUS_INSUFFICIENT, "t", "now", cr_live.FAILURE_CONTENT,
            {"kind": "flight", "origin": "delhi", "destination": "kolkata",
             "date": "2026-99-99", "date_state": "resolved",
             "return_date": None, "passengers": 1, "cabin": "economy",
             "trip": "oneway", "nonstop": False,
             "missing": [], "unsupported": []},
            escalation_eligible=True)
    # the complete typed shape IS accepted
    n = cr_live.flight_request(ASK, NOW_LOCAL)
    r = cr_live.LookupResult(cr_live.STATUS_INSUFFICIENT, "t", "now",
                             cr_live.FAILURE_CONTENT, n,
                             escalation_eligible=True)
    assert r.escalation_eligible is True


def test_complete_roundtrip_ask_stays_eligible_on_denial(monkeypatch):
    monkeypatch.setattr(cr_live.W, "search", _boom)
    r = cr_live.lookup(
        "flight Delhi to Kolkata tomorrow returning 2026-10-25", NOW_LOCAL)
    assert r.status == cr_live.STATUS_UNAVAILABLE
    assert r.escalation_eligible is True  # complete+supported denial
    assert r.normalized["return_date"] == "2026-10-25"


# --- review-#4 narrow vectors ------------------------------------------

def test_shape_refuses_untyped_routes():
    base = {"kind": "flight", "origin": "delhi", "destination": "kolkata",
            "date": "2026-10-13", "date_state": "resolved",
            "return_date": None, "passengers": 1, "cabin": "economy",
            "trip": "oneway", "nonstop": False,
            "missing": [], "unsupported": []}
    bad_variants = [
        dict(base, origin=42),                       # truthy != typed
        dict(base, destination=["kolkata"]),         # list != str
        dict(base, return_date="2026-10-25"),        # oneway => return None
        dict(base, trip="roundtrip", return_date="2026-10-01"),  # before dep
        dict(base, missing=None),                    # None is not empty
        dict(base, unsupported=None),
        dict(base, origin="d"),                      # below min length
        dict(base, origin="delhi1"),                 # not city-shaped
    ]
    for bad in bad_variants:
        with pytest.raises(ValueError):
            cr_live.LookupResult(cr_live.STATUS_INSUFFICIENT, "t", "now",
                                 cr_live.FAILURE_CONTENT, bad,
                                 escalation_eligible=True)
    good = dict(base, trip="roundtrip", return_date="2026-10-20")
    r = cr_live.LookupResult(cr_live.STATUS_INSUFFICIENT, "t", "now",
                             cr_live.FAILURE_CONTENT, good,
                             escalation_eligible=True)
    assert r.escalation_eligible is True


def test_clock_time_constraint_is_unsupported():
    n = cr_live.flight_request(
        "flight Delhi to Kolkata tomorrow departing at 10:30", NOW_LOCAL)
    assert any(u.startswith("time:preference") for u in n["unsupported"])


def test_repeated_same_passenger_count_is_a_conflict():
    n = cr_live.flight_request(
        "flight Delhi to Kolkata tomorrow 2 adults and 2 adults", NOW_LOCAL)
    assert any(u.startswith("passengers:conflict") for u in n["unsupported"])


def test_leftover_constraint_fragments_are_refused():
    for ask in ("flight Delhi to Kolkata tomorrow with a layover",
                "flight Delhi to Kolkata tomorrow refundable",
                "flight Delhi to Kolkata tomorrow arriving at 6pm"):
        n = cr_live.flight_request(ask, NOW_LOCAL)
        assert n["unsupported"], ask


def test_supplied_time_party_constraints_make_lookup_ineligible(monkeypatch):
    monkeypatch.setattr(cr_live.W, "search", _boom)
    for ask in ("flight Delhi to Kolkata tomorrow departing at 10:30",
                "flight Delhi to Kolkata tomorrow 2 adults and 2 adults"):
        r = cr_live.lookup(ask, NOW_LOCAL)
        assert r.status == cr_live.STATUS_INSUFFICIENT, ask
        assert r.escalation_eligible is False, ask
