"""Deterministic public lookup for explicit search and changing-data requests.

Step A (flight-escalation contract, review-#1 fold): lookup() returns a
structured LookupResult. The flight request is NORMALIZED BEFORE any
network attempt and is preserved on EVERY exit (including search/research
failures). ALL flight requests pass the task validator; an incomplete or
unsupported request is a clarification/not-verified result, NEVER a
generic verified one. Escalation eligibility is reserved for COMPLETE,
SUPPORTED flight asks whose quick path failed with content/search/source
failure (denial/unreadable) - never for missing fields, invalid dates,
conflicts, or unsupported expressions. The static evidence guard is
STEP-A STATIC-ONLY: fetched page text can never prove dated available
inventory; the positive validated-inventory contract is reserved for the
interactive browser adapter (steps B/C).
"""
import re
from dataclasses import dataclass, field
from datetime import date as _date, datetime, timedelta, timezone
from types import MappingProxyType
import cr_web as W

STATUS_VERIFIED = "verified"
STATUS_INSUFFICIENT = "insufficient"
STATUS_UNAVAILABLE = "unavailable"

FAILURE_SEARCH = "search_failed"
FAILURE_SOURCES = "sources_unreadable"
FAILURE_CONTENT = "content_unverified"
FAILURE_INCOMPLETE = "incomplete_request"
FAILURE_UNSUPPORTED = "request_unsupported"

_FAILURE_CODES = frozenset({
    "", FAILURE_SEARCH, FAILURE_SOURCES, FAILURE_CONTENT,
    FAILURE_INCOMPLETE, FAILURE_UNSUPPORTED})


@dataclass(frozen=True)
class LookupResult:
    """Structured result of one quick public lookup attempt (attempt 1).

    Invariants (enforced): status/failure_code are closed enums; a
    verified result carries no failure code; escalation eligibility
    requires a non-verified status with a failure code; normalized is an
    immutable mapping; attempted_sources/evidence are tuples.
    """
    status: str
    text: str
    checked_at: str
    failure_code: str = ""
    normalized: dict = field(default_factory=dict)
    attempted_sources: tuple = ()
    evidence: tuple = ()
    escalation_eligible: bool = False

    def __post_init__(self):
        if self.status not in (STATUS_VERIFIED, STATUS_INSUFFICIENT,
                               STATUS_UNAVAILABLE):
            raise ValueError("bad status")
        if self.failure_code not in _FAILURE_CODES:
            raise ValueError("bad failure_code")
        if self.status == STATUS_VERIFIED and self.failure_code:
            raise ValueError("verified carries no failure code")
        if self.escalation_eligible and (
                self.status == STATUS_VERIFIED or not self.failure_code):
            raise ValueError("eligibility requires a non-verified failure")
        object.__setattr__(self, "normalized",
                           MappingProxyType(dict(self.normalized)))
        object.__setattr__(self, "attempted_sources",
                           tuple(self.attempted_sources))
        object.__setattr__(self, "evidence", tuple(self.evidence))


# --- flight request normalization --------------------------------------

_FLIGHT_WORD = re.compile(r'(?i)\b(?:flights?|airfares?|fares?)\b')
_ROUTE_RE = re.compile(
    r'(?i)\bfrom\s+([a-zA-Z][a-zA-Z .]+?)\s+to\s+([a-zA-Z][a-zA-Z .]+?)'
    r'(?:\s+on\b|\s+for\b|\s+tomorrow\b|\s+today\b|\s+next\b|\s+last\b|[.,!?]|$)')
_BARE_ROUTE_RE = re.compile(
    r'(?i)\b([a-zA-Z][a-zA-Z .]+?)\s+to\s+([a-zA-Z][a-zA-Z .]+?)'
    r'(?:\s+on\b|\s+for\b|\s+tomorrow\b|\s+today\b|\s+next\b|\s+last\b|[.,!?]|$)')
_ISO_DATE_RE = re.compile(r'\b(20\d\d)-(\d{1,2})-(\d{1,2})\b')
_NEXT_REL_RE = re.compile(
    r'(?i)\bnext\s+(?:week|month|mon(?:day)?|tue(?:sday)?|wed(?:nesday)?|'
    r'thu(?:rsday)?|fri(?:day)?|sat(?:urday)?|sun(?:day)?)\b')
_PAX_RE = re.compile(
    r'(?i)\b(\d+)\s*(?:adults?|passengers?|people|travell?ers)\b')
_CABIN_RE = re.compile(r'(?i)\b(business|first|premium economy|economy)\b')

# resolved IATA codes are a NORMALIZATION aid only, never evidence
_IATA = {
    "delhi": "DEL", "new delhi": "DEL",
    "kolkata": "CCU", "calcutta": "CCU",
    "mumbai": "BOM", "bombay": "BOM",
    "bengaluru": "BLR", "bangalore": "BLR",
    "chennai": "MAA", "madras": "MAA",
    "hyderabad": "HYD", "goa": "GOI", "pune": "PNQ",
    "ahmedabad": "AMD", "jaipur": "JAI", "lucknow": "LKO",
}

_STOP_ORIGIN = re.compile(
    r'(?i)^(?:cheapest|cheap|find|book|search|show|get|flight|flights|fare|'
    r'fares|airfare|airfares|a|an|the|one[- ]way|round[- ]trip|return|'
    r'tomorrow|today|on)\s+')


def _city(raw):
    name = raw.strip().strip('.,!?').lower()
    while True:
        stripped = _STOP_ORIGIN.sub('', name).strip()
        if stripped == name:
            break
        name = stripped
    return name or None


def _parse_date(text, now_local):
    """Return (iso_date_or_None, state, note). state: resolved | absent |
    needs_clock | invalid | unsupported | conflict. An EXPLICIT ISO date
    is validated against the real calendar INDEPENDENT of the clock and
    beats relative words (including negated ones); a relative word in
    direct conflict with the explicit date is a conflict."""
    iso = _ISO_DATE_RE.search(text)
    rel_tomorrow = bool(re.search(r'(?i)(?<!not )\btomorrow\b', text))
    rel_today = bool(re.search(r'(?i)(?<!not )\btoday\b', text))
    if iso:
        try:
            d = _date(int(iso.group(1)), int(iso.group(2)),
                      int(iso.group(3)))
        except ValueError:
            return None, "invalid", iso.group(0)
        if rel_tomorrow or rel_today:
            return d.isoformat(), "conflict", "explicit-vs-relative"
        return d.isoformat(), "resolved", ""
    if _NEXT_REL_RE.search(text):
        return None, "unsupported", "next-relative"
    if rel_tomorrow or rel_today:
        if now_local is None:
            return None, "needs_clock", "relative-without-clock"
        base = now_local.date()
        return ((base + timedelta(days=1)).isoformat() if rel_tomorrow
                else base.isoformat()), "resolved", ""
    return None, "absent", ""


def flight_request(text, now_local=None):
    """Parse a flight fare ask into a normalized request dict, or None.

    now_local: the caller-resolved USER-LOCAL datetime; needed ONLY for
    relative dates. Explicit ISO dates resolve without it. Passengers and
    cabin parse from the text; roundtrip/return needs a return date
    (missing); multi-city and next-relative dates are UNSUPPORTED - never
    silently coerced to 1 adult / economy / one-way.
    """
    if not _FLIGHT_WORD.search(text):
        return None
    m = _ROUTE_RE.search(text) or _BARE_ROUTE_RE.search(text)
    origin = _city(m.group(1)) if m else None
    destination = _city(m.group(2)) if m else None
    date, date_state, date_note = _parse_date(text, now_local)
    pax = _PAX_RE.search(text)
    cabin = _CABIN_RE.search(text)
    roundtrip = bool(re.search(r'(?i)\b(?:round[- ]trip|return)\b', text))
    nonstop = bool(re.search(r'(?i)\b(?:non[- ]?stop|direct)\b', text))
    unsupported = []
    if date_state in ("invalid", "unsupported", "conflict"):
        unsupported.append("date:" + date_state +
                           ("(" + date_note + ")" if date_note else ""))
    if re.search(r'(?i)\bmulti[- ]?city\b', text):
        unsupported.append("multi-city")
    missing = [k for k, v in (("origin", origin), ("destination", destination))
               if not v]
    if date_state in ("absent", "needs_clock"):
        missing.append("date")
    if roundtrip:
        missing.append("return_date")
    return {
        "kind": "flight",
        "origin": origin,
        "destination": destination,
        "origin_iata": _IATA.get(origin or "", ""),
        "destination_iata": _IATA.get(destination or "", ""),
        "date": date,
        "date_state": date_state,
        "passengers": int(pax.group(1)) if pax else 1,
        "cabin": cabin.group(1).lower() if cabin else "economy",
        "trip": "roundtrip" if roundtrip else "oneway",
        "nonstop": nonstop,
        "missing": missing,
        "unsupported": unsupported,
        "raw": text[:200],
    }


def validate_static_flight_evidence(norm, page_texts):
    """STEP-A STATIC-ONLY guard. Static fetched page text can NEVER prove
    a dated, AVAILABLE itinerary with a verified total fare - advertised
    starting fares are not inventory. The positive validated-inventory
    contract (interactive results page, observed totals, provider URL,
    checked time) is RESERVED for the steps-B/C browser adapter. An
    https URL is a shape check, NOT provider-identity proof. Always
    returns (False, reason) at this step.
    """
    if not norm or norm.get("kind") != "flight":
        return False, FAILURE_UNSUPPORTED
    if norm.get("unsupported"):
        return False, FAILURE_UNSUPPORTED
    if norm.get("missing"):
        return False, FAILURE_INCOMPLETE
    if not page_texts:
        return False, "no_dated_itinerary"
    return False, "static_page_not_booking_inventory"


def _flight_eligible(norm, failure_code):
    """Escalation is appropriate ONLY for a COMPLETE, SUPPORTED flight ask
    whose quick path failed with denial/unreadable/unverified content -
    never for missing fields or unsupported expressions."""
    return (bool(norm) and norm.get("kind") == "flight"
            and not norm.get("missing") and not norm.get("unsupported")
            and failure_code in (FAILURE_CONTENT, FAILURE_SEARCH,
                                 FAILURE_SOURCES))


def requested(text):
 return bool(re.match(r'(?i)^Research focus \[(web|deep|academic|social|video)\]:',text)) or bool(re.search(r'(?i)\b(?:web search|search the web|search for|look up|latest news|news today|weather|forecast|cheapest flight|flight prices?|airfares?|current price|price today)\b',text) or re.search(r'(?i)\bflights?\s+(?:from\s+)?[a-z][a-z ]*\s+to\s+[a-z]',text)) and not bool(re.search(r'(?i)\b(?:my inbox|my emails?|my calendar|send|remind|schedule|connect)\b',text))

def lead_reply(text,results):
 if re.search(r'(?i)flight|airfare|fare',text):return 'The flight pages did not provide a verifiable fare for your departure dates. What window should I compare: the next 7 days or next 30 days? I cannot name a cheapest flight without checked dates and available fares.'
 return 'I could not get readable current data for that request. Tell me the exact place/date or item to narrow the check. No current number is verified.'


def _flight_reply(norm, when):
    """Structured unverified flight reply: preserves the SUPPLIED date,
    asks only for what is actually missing/unsupported, never claims a
    fare and never asks a spurious 7/30-day window."""
    route = ""
    if norm.get("origin") and norm.get("destination"):
        route = " for %s to %s" % (norm["origin"].title(),
                                   norm["destination"].title())
    datebit = " on " + norm["date"] if norm.get("date") else ""
    ask = ""
    unsupported = norm.get("unsupported") or []
    missing = norm.get("missing") or []
    if any(u.startswith("date:conflict") for u in unsupported):
        ask = " You gave both an exact date and a relative one - tell me which departure date to use."
    elif any(u.startswith("date:invalid") for u in unsupported):
        ask = " That date is not a real calendar date - give me an exact date (YYYY-MM-DD) or say today/tomorrow."
    elif any(u.startswith("date:unsupported") for u in unsupported):
        ask = " I can't resolve that relative date yet - give me an exact date (YYYY-MM-DD) or say today/tomorrow."
    elif "multi-city" in unsupported:
        ask = " I can only check one-way or return trips between two cities."
    elif "date" in missing:
        ask = " Tell me the departure date and I will check that exact day."
    elif "origin" in missing or "destination" in missing:
        ask = " Tell me the exact origin and destination cities."
    elif "return_date" in missing:
        ask = " Tell me the return date for the round trip."
    return ("I could not verify a current fare%s%s from a quick search - "
            "the results were leads, not dated available itineraries with "
            "verified totals. No lowest fare is verified yet.%s\n\nChecked: %s"
            % (route, datebit, ask, when))


def lookup(text, now_local=None):
    """One structured quick-lookup attempt (attempt 1). The flight request
    is normalized BEFORE the attempt and preserved on EVERY exit."""
    when = datetime.now(timezone.utc).isoformat(timespec='seconds')
    flight = flight_request(text, now_local)
    norm = flight if flight else {}
    if re.search(r'(?i)\bnews\b',text):
        import cr_group
        out = cr_group.answer({'text':'@crayon_v1_bot '+text,'source_links':True})
        # preserve wrapper behavior WITHOUT upgrading unverified news prose
        if not (out or '').strip() or out.startswith(('I could not','The pages I read')):
            return LookupResult(STATUS_INSUFFICIENT, out, when,
                                FAILURE_CONTENT, norm)
        return LookupResult(STATUS_VERIFIED, out, when, "", norm)
    focus_match=re.match(r'(?is)^Research focus \[(web|deep|academic|social|video)\]:\s*(.+)',text)
    if focus_match:
        focus,query=focus_match.groups()
        try:found=W.research(query[:400],focus);results=found['results'];pages=found['pages']
        except Exception:
            return LookupResult(STATUS_UNAVAILABLE,
                'Research sources could not be read this time. No answer is verified.',
                when, FAILURE_SOURCES, norm, (),
                escalation_eligible=_flight_eligible(flight, FAILURE_SOURCES))
    else:
        focus=None
    try:results=results if focus else W.search(text[:400],5)
    except Exception:
        return LookupResult(STATUS_UNAVAILABLE,
            'Web search did not return current results this time. No current price, fare or forecast is verified. Try a narrower place/date/product query.',
            when, FAILURE_SEARCH, norm, (),
            escalation_eligible=_flight_eligible(flight, FAILURE_SEARCH))
    # Search results are leads, never page evidence. Fetch exact destinations.
    pages=pages if focus else []
    for row in ([] if focus else results[:5]):
        try:pages.append(W.fetch(row['url']))
        except Exception:pass
    attempted = tuple(r.get('url','') for r in results[:5] if r.get('url','').startswith('https://'))
    if pages:
        import cr_citations,cr_llm as L,json
        try:draft=L.generate([L.user('Question: '+text+'\nUntrusted fetched evidence: '+json.dumps(pages))],system='Answer from fetched evidence only. Answer first, concise. For fares and prices, extract advertised amounts from fetched page text with airline/date only when explicitly present. Label advertised route starting fares, never guaranteed inventory, cheapest date, final total or a booking quote. Current weather/price must have place, date/time and source. Never use seasonal averages as current conditions. No invented facts.',tools=None,max_tokens=1000).get('text','')
        except Exception:draft=''
        try:checked=cr_citations.checked_answer(text,draft,pages)
        except Exception:checked='I could not verify this'
        if not checked.startswith(('I could not','The pages I read')):
            if flight:
                # EVERY flight request goes through the task validator;
                # static pages never verify a fare (step-A static-only
                # guard); incomplete/unsupported asks are clarifications,
                # NEVER a generic verified result
                validate_static_flight_evidence(flight, pages)
                return LookupResult(
                    STATUS_INSUFFICIENT, _flight_reply(flight, when), when,
                    FAILURE_INCOMPLETE if (flight["missing"] or flight["unsupported"]) else FAILURE_CONTENT,
                    flight, attempted,
                    escalation_eligible=_flight_eligible(flight, FAILURE_CONTENT))
            return LookupResult(STATUS_VERIFIED, checked+'\n\nChecked: '+when,
                                when, "", norm, attempted,
                                evidence=attempted)
    if flight:
        incomplete = bool(flight["missing"] or flight["unsupported"])
        return LookupResult(
            STATUS_INSUFFICIENT, _flight_reply(flight, when), when,
            FAILURE_INCOMPLETE if incomplete else FAILURE_CONTENT,
            flight, attempted,
            escalation_eligible=False if incomplete else _flight_eligible(flight, FAILURE_CONTENT))
    return LookupResult(STATUS_INSUFFICIENT,
        lead_reply(text,results)+'\n\nChecked: '+when+'\nNo current number is verified.',
        when, FAILURE_CONTENT, norm, attempted)


def answer(text):
    """Prose wrapper preserving the existing news/weather/research replies."""
    return lookup(text).text
