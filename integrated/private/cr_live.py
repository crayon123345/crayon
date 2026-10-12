"""Deterministic public lookup for explicit search and changing-data requests.

Step A (flight-escalation contract, review-#3 fold): lookup() returns a
structured LookupResult. The flight request is normalized and RESOLVED
BEFORE any network attempt: an incomplete or unsupported request is a
clarification result with NO search/research call, NEVER a generic
verified one and never a search-failure report. Leg clauses bind FIRST:
a return clause is masked before the departure date parses, so a
return-only ISO is never stolen as the departure date; multiple explicit
departure dates are UNSUPPORTED alternatives, never a silent first pick.
EVERY supplied constraint is honored in the normalized dict or marked
missing/unsupported - party phrasing ("me and my wife"), departure-time
preferences, via routings, airline preferences and terminals are
conservatively UNSUPPORTED and clarified, NEVER silently defaulted to
1 adult / economy / one-way / any-time. A COMPLETE, SUPPORTED flight ask
is preserved on EVERY exit (search/research/content failure) with its
route/date/binding and failure code; escalation eligibility requires the
exact typed flight shape (kind + resolved route/date/pax/cabin/trip/
return) AND a denial/unreadable failure - enforced at construction and
at dispatch, never for other kinds. The static evidence guard is STEP-A
STATIC-ONLY: fetched page text can never prove dated available
inventory; the positive validated-inventory contract is reserved for the
interactive browser adapter (steps B/C). News wrapper behavior is
preserved but failure prose is never upgraded to verified; mixed
news+flight asks are flight-gated.
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
# eligibility is ONLY ever meaningful for denial/unreadable failures of a
# complete, supported request
_ELIGIBLE_FAILURES = frozenset(
    {FAILURE_CONTENT, FAILURE_SEARCH, FAILURE_SOURCES})

_CABINS = ("economy", "premium economy", "business", "first")


def _deep_freeze(value):
    if isinstance(value, (list, tuple)):
        return tuple(_deep_freeze(v) for v in value)
    if isinstance(value, dict):
        return MappingProxyType({k: _deep_freeze(v) for k, v in value.items()})
    return value


def _flight_shape_complete(norm):
    """The exact typed/validated flight-request shape: kind flight, no
    missing/unsupported fields, non-empty route, a resolved calendar-valid
    departure date, a valid return date iff roundtrip, passengers 1-9, a
    known cabin, typed trip/nonstop. Anything less is not dispatchable."""
    try:
        if not norm or norm.get("kind") != "flight":
            return False
        if norm.get("missing") or norm.get("unsupported"):
            return False
        if not norm.get("origin") or not norm.get("destination"):
            return False
        if norm.get("date_state") != "resolved":
            return False
        _date.fromisoformat(norm.get("date") or "")
        trip = norm.get("trip")
        if trip not in ("oneway", "roundtrip"):
            return False
        ret = norm.get("return_date")
        if trip == "roundtrip" and not ret:
            return False
        if ret is not None:
            _date.fromisoformat(ret)
        pax = norm.get("passengers")
        if not isinstance(pax, int) or isinstance(pax, bool) \
                or not (1 <= pax <= 9):
            return False
        if norm.get("cabin") not in _CABINS:
            return False
        if not isinstance(norm.get("nonstop"), bool):
            return False
        return True
    except (TypeError, ValueError):
        return False


@dataclass(frozen=True)
class LookupResult:
    """Structured result of one quick public lookup attempt (attempt 1).

    Invariants (enforced in __post_init__):
    - status and failure_code are closed enums; escalation_eligible is bool
    - a verified result carries no failure code and is never eligible
    - eligibility requires a non-verified status, a denial/unreadable
      failure code AND the complete typed flight shape (kind flight with
      a validated route/date/pax/cabin/trip/return) - a bare
      {'kind': 'flight'} dict or a non-flight kind can NEVER be eligible
    - normalized is DEEP immutable (lists -> tuples, dicts -> read-only
      proxies, recursively)
    - a flight-kind normalized request: non-verified results carry a
      failure code
    """
    status: str
    text: str
    checked_at: str
    failure_code: str = ""
    normalized: dict = field(default_factory=dict)
    search_leads: tuple = ()    # raw result URLs: LEADS, not attempt receipts
    fetched_pages: tuple = ()   # pages fetched and CONSIDERED - not verified-used
    escalation_eligible: bool = False

    def __post_init__(self):
        if self.status not in (STATUS_VERIFIED, STATUS_INSUFFICIENT,
                               STATUS_UNAVAILABLE):
            raise ValueError("bad status")
        if not isinstance(self.escalation_eligible, bool):
            raise ValueError("escalation_eligible must be bool")
        if self.failure_code not in _FAILURE_CODES:
            raise ValueError("bad failure_code")
        if self.status == STATUS_VERIFIED and (
                self.failure_code or self.escalation_eligible):
            raise ValueError("verified carries no failure/eligibility")
        if self.escalation_eligible and (
                self.status == STATUS_VERIFIED
                or self.failure_code not in _ELIGIBLE_FAILURES):
            raise ValueError("eligibility requires a denial/unreadable failure")
        frozen = _deep_freeze(dict(self.normalized))
        object.__setattr__(self, "normalized", frozen)
        object.__setattr__(self, "search_leads", tuple(self.search_leads))
        object.__setattr__(self, "fetched_pages", tuple(self.fetched_pages))
        if frozen.get("kind") == "flight":
            if self.status != STATUS_VERIFIED and not self.failure_code:
                raise ValueError("non-verified flight result needs a failure code")
        if self.escalation_eligible and not _flight_shape_complete(frozen):
            raise ValueError("eligibility requires the complete typed flight shape")


# --- flight request normalization --------------------------------------

_FLIGHT_WORD = re.compile(r'(?i)\b(?:flights?|airfares?|fares?)\b')
# A route is two city names; the destination capture STOPS at any supplied
# constraint word (lookahead - never consumed), so constraints never
# contaminate the city.
_ROUTE_END = (
    r'(?=\s+(?:on\b|for\b|tomorrow\b|today\b|tonight\b|day\s+after\b|'
    r'next\b|last\b|this\b|return(?:ing)?\b|round[- ]?trip\b|one[- ]?way\b|'
    r'non[- ]?stop\b|direct\b|economy\b|business\b|first\b|premium\b|'
    r'class\b|in\b|via\b|not\b|no\b|and\b|one\b|two\b|three\b|four\b|'
    r'five\b|six\b|seven\b|eight\b|nine\b|'
    r'indigo\b|vistara\b|spicejet\b|akasa\b|emirates\b|qatar\b|'
    r'lufthansa\b|airasia\b|\d)'
    r'|\s*$|\s*[,\.;:!?])')
_ROUTE_RE = re.compile(
    r'(?i)\bfrom\s+([a-zA-Z][a-zA-Z .]{1,38}?)\s+(?:to|into)\s+'
    r'([a-zA-Z][a-zA-Z .]{1,38}?)' + _ROUTE_END)
_BARE_ROUTE_RE = re.compile(
    r'(?i)\b([a-zA-Z][a-zA-Z .]{1,38}?)\s+(?:to|into)\s+'
    r'([a-zA-Z][a-zA-Z .]{1,38}?)' + _ROUTE_END)
_ISO_DATE_RE = re.compile(r'\b(20\d\d)-(\d{1,2})-(\d{1,2})\b')
_REL_RE = re.compile(r'(?i)\b(tomorrow|today|tonight)\b')
_NEXT_REL_RE = re.compile(
    r'(?i)\bnext\s+(?:week|month|mon(?:day)?|tue(?:sday)?|wed(?:nesday)?|'
    r'thu(?:rsday)?|fri(?:day)?|sat(?:urday)?|sun(?:day)?)\b')
_NUM_WORDS = {"one": 1, "two": 2, "three": 3, "four": 4, "five": 5,
              "six": 6, "seven": 7, "eight": 8, "nine": 9}
_PAX_RE = re.compile(
    r'(?i)\b(\d{1,2}|one|two|three|four|five|six|seven|eight|nine)\s+'
    r'(?:adults?|passengers?|people|travell?ers?|tickets?|seats?)\b')
_PAX_WORD_RE = re.compile(
    r'(?i)\b(?:adults?|passengers?|travell?ers?)\b')
_KID_RE = re.compile(r'(?i)\b(?:children|child|kids?|infants?)\b')
_PARTY_RE = re.compile(
    r'(?i)\b(?:me\s+and\b|and\s+my\s+\w+|my\s+(?:wife|husband|partner|'
    r'family|parents?|son|daughter|girlfriend|boyfriend)|with\s+my\s+\w+|'
    r'us\s+two)\b')
_CABIN_RE = re.compile(r'(?i)\b(premium economy|business|first|economy)\b')
_NONSTOP_RE = re.compile(r'(?i)\b(?:non[- ]?stop|direct)\b')
_RETURN_ISO_RE = re.compile(
    r'(?i)\breturn(?:ing)?\s+(?:on\s+)?(20\d\d-\d{1,2}-\d{1,2})\b')
_RETURN_NEXT_RE = re.compile(
    r'(?i)\breturn(?:ing)?\s+(?:on\s+)?next\s+\w+\b')
_RETURN_REL_RE = re.compile(
    r'(?i)\breturn(?:ing)?\s+(?:on\s+)?(tomorrow|today)\b')
_VIA_RE = re.compile(r'(?i)\bvia\s+([a-zA-Z][a-zA-Z .]{1,30})')
_AIRLINE_RE = re.compile(
    r'(?i)\b(?:indigo|air\s+india|vistara|spicejet|akasa|emirates|'
    r'qatar\s+airways|singapore\s+airlines|lufthansa|british\s+airways|'
    r'airasia)\b')
_TIME_PREF_RE = re.compile(
    r'(?i)\b(?:before|after|around|by)\s+\d{1,2}(?::\d{2})?\s*'
    r'(?:a\.?m\.?|p\.?m\.?)\b|'
    r'\b(?:morning|afternoon|evening|red[- ]?eye)\b')
_TERMINAL_RE = re.compile(r'(?i)\bterminal\s+\d+\b')

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
    r'fares|airfare|airfares|latest|news|tickets?|please|me|a|an|the|'
    r'one[- ]way|round[- ]trip|return|tomorrow|today|on)\s+')


def _city(raw):
    name = raw.strip().strip('.,!?').lower()
    while True:
        stripped = _STOP_ORIGIN.sub('', name).strip()
        if stripped == name:
            break
        name = stripped
    return name or None


def _negated(text, start):
    """True when the match at `start` is immediately preceded by not/no
    (any whitespace run, including newlines)."""
    return bool(re.search(r'(?i)\b(?:not|no)\s+$', text[:start]))


def _unnegated(pattern, text):
    for m in pattern.finditer(text):
        if not _negated(text, m.start()):
            return m
    return None


def _mask_return_clauses(text):
    """Blank out return-date clauses so the DEPARTURE parser can never
    steal a return date. Leg clauses bind first."""
    chars = list(text)
    for rx in (_RETURN_ISO_RE, _RETURN_NEXT_RE, _RETURN_REL_RE):
        for m in rx.finditer(text):
            for i in range(m.start(), m.end()):
                chars[i] = ' '
    return ''.join(chars)


def _parse_date(text, now_local):
    """Return (iso_date_or_None, state, note) for the DEPARTURE leg.
    state: resolved | absent | needs_clock | invalid | unsupported |
    conflict. Explicit non-negated ISO dates are validated against the
    real calendar INDEPENDENT of the clock; TWO OR MORE distinct explicit
    dates are UNSUPPORTED alternatives (clarify, never a silent first
    pick); a non-negated relative word alongside the explicit date is a
    CONFLICT."""
    dates = []
    for m in _ISO_DATE_RE.finditer(text):
        if _negated(text, m.start()):
            continue
        try:
            d = _date(int(m.group(1)), int(m.group(2)), int(m.group(3)))
        except ValueError:
            return None, "invalid", m.group(0)
        if d not in dates:
            dates.append(d)
    rel = _unnegated(_REL_RE, text)
    if len(dates) > 1:
        return None, "unsupported", "alternatives(" + "|".join(
            d.isoformat() for d in dates) + ")"
    if dates:
        if rel:
            return dates[0].isoformat(), "conflict", "explicit-vs-relative"
        return dates[0].isoformat(), "resolved", ""
    if _NEXT_REL_RE.search(text):
        return None, "unsupported", "next-relative"
    if rel:
        if now_local is None:
            return None, "needs_clock", "relative-without-clock"
        base = now_local.date()
        return ((base + timedelta(days=1)).isoformat()
                if rel.group(1).lower() == "tomorrow"
                else base.isoformat()), "resolved", ""
    return None, "absent", ""


def _parse_return_date(text, now_local, unsupported):
    """Return (return_date_or_None, returning). An explicit return date is
    calendar-validated; 'returning next Friday' is unsupported; 'returning
    tomorrow' resolves from the caller clock; a bare round-trip/return ask
    with no date leaves return_date MISSING (clarified, never dropped)."""
    m = _RETURN_ISO_RE.search(text)
    if m and not _negated(text, m.start()):
        try:
            return _date(*map(int, m.group(1).split('-'))).isoformat(), True
        except ValueError:
            unsupported.append("date:return-invalid(" + m.group(1) + ")")
            return None, True
    if _RETURN_NEXT_RE.search(text):
        unsupported.append("date:return-unsupported(next-relative)")
        return None, True
    rm = _RETURN_REL_RE.search(text)
    if rm and not _negated(text, rm.start()):
        if now_local is None:
            return None, True  # missing (needs clock)
        base = now_local.date()
        return ((base + timedelta(days=1)).isoformat()
                if rm.group(1).lower() == "tomorrow"
                else base.isoformat()), True
    return None, bool(re.search(r'(?i)\breturn(?:ing)?\b', text))


def flight_request(text, now_local=None):
    """Parse a flight fare ask into a normalized request dict, or None.

    now_local: the caller-resolved USER-LOCAL datetime; needed ONLY for
    relative dates. Leg clauses bind FIRST (return masked before the
    departure parse). EVERY supplied constraint is either honored in the
    normalized dict or marked missing/unsupported - NEVER silently
    defaulted: spelled-out passenger counts parse; children/infants,
    party phrasing, out-of-range or count-less counts, negated-only
    cabins, departure-time preferences, via routings, airline and
    terminal preferences are UNSUPPORTED; round-trip asks carry a parsed
    or MISSING return date; multiple explicit departure dates are
    UNSUPPORTED alternatives. Multi-city and next-relative dates are
    UNSUPPORTED.
    """
    if not _FLIGHT_WORD.search(text):
        return None
    m = _ROUTE_RE.search(text) or _BARE_ROUTE_RE.search(text)
    origin = _city(m.group(1)) if m else None
    destination = _city(m.group(2)) if m else None
    departure_text = _mask_return_clauses(text)
    date, date_state, date_note = _parse_date(departure_text, now_local)
    unsupported = []
    if date_state in ("invalid", "unsupported", "conflict"):
        if date_note.startswith("alternatives("):
            unsupported.append("date:" + date_note)  # note carries its own ()
        else:
            unsupported.append("date:" + date_state +
                               ("(" + date_note + ")" if date_note else ""))
    return_date, returning = _parse_return_date(text, now_local,
                                                unsupported)
    roundtrip = bool(re.search(r'(?i)\bround[- ]?trip\b', text)) or returning
    if return_date and date and return_date < date:
        unsupported.append("date:return-order(before-departure)")
    pax_all = _PAX_RE.findall(text)
    passengers = 1
    if pax_all:
        counts = []
        for raw_m in pax_all:
            raw = raw_m.lower()
            n = int(raw) if raw.isdigit() else _NUM_WORDS.get(raw, 0)
            if n < 1 or n > 9:
                unsupported.append("passengers:invalid(" + raw_m + ")")
            elif n not in counts:
                counts.append(n)
        if len(counts) > 1:
            unsupported.append("passengers:conflict(" + "|".join(
                str(c) for c in counts) + ")")
        elif counts:
            passengers = counts[0]
    elif _PAX_WORD_RE.search(text):
        unsupported.append("passengers:unparsed")
    if _KID_RE.search(text):
        unsupported.append("passengers:children-infants")
    if _PARTY_RE.search(text):
        unsupported.append("passengers:party-unparsed")
    cabin_m = _unnegated(_CABIN_RE, text)
    if cabin_m:
        cabin = cabin_m.group(1).lower()
    elif _CABIN_RE.search(text):
        cabin = None
        unsupported.append("cabin:only-negated")
    else:
        cabin = "economy"
    nonstop = bool(_unnegated(_NONSTOP_RE, text))
    tm = _TIME_PREF_RE.search(text)
    if tm:
        unsupported.append("time:preference(" + tm.group(0).strip() + ")")
    vm = _VIA_RE.search(text)
    if vm:
        unsupported.append("route:via(" + vm.group(1).strip().lower() + ")")
    if _AIRLINE_RE.search(text):
        unsupported.append("airline:preference")
    if _TERMINAL_RE.search(text):
        unsupported.append("airport:terminal")
    if re.search(r'(?i)\bmulti[- ]?city\b', text):
        unsupported.append("multi-city")
    missing = [k for k, v in (("origin", origin), ("destination", destination))
               if not v]
    if date_state in ("absent", "needs_clock"):
        missing.append("date")
    if roundtrip and not return_date and not any(
            u.startswith("date:return") for u in unsupported):
        missing.append("return_date")
    return {
        "kind": "flight",
        "origin": origin,
        "destination": destination,
        "origin_iata": _IATA.get(origin or "", ""),
        "destination_iata": _IATA.get(destination or "", ""),
        "date": date,
        "date_state": date_state,
        "return_date": return_date,
        "passengers": passengers,
        "cabin": cabin,
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
    """Escalation is offered ONLY for the COMPLETE typed flight shape
    failing with a denial/unreadable failure (content, search or
    sources). Enforced again by the LookupResult invariant."""
    return (_flight_shape_complete(norm)
            and failure_code in _ELIGIBLE_FAILURES)


def requested(text):
 return bool(re.match(r'(?i)^Research focus \[(web|deep|academic|social|video)\]:',text)) or bool(re.search(r'(?i)\b(?:web search|search the web|search for|look up|latest news|news today|weather|forecast|cheapest flight|flight prices?|airfares?|current price|price today)\b',text) or re.search(r'(?i)\bflights?\s+(?:from\s+)?[a-z][a-z ]*\s+to\s+[a-z]',text)) and not bool(re.search(r'(?i)\b(?:my inbox|my emails?|my calendar|send|remind|schedule|connect)\b',text))

def lead_reply(text,results):
 if re.search(r'(?i)flight|airfare|fare',text):return 'The flight pages did not provide a verifiable fare for your departure dates. What window should I compare: the next 7 days or next 30 days? I cannot name a cheapest flight without checked dates and available fares.'
 return 'I could not get readable current data for that request. Tell me the exact place/date or item to narrow the check. No current number is verified.'


def _flight_reply(norm, when):
    """Structured unverified flight reply: preserves the SUPPLIED date,
    asks only for what is actually missing/unsupported, never claims a
    fare, never asks a spurious 7/30-day window, and never proposes
    dropping travelers."""
    route = ""
    if norm.get("origin") and norm.get("destination"):
        route = " for %s to %s" % (norm["origin"].title(),
                                   norm["destination"].title())
    datebit = " on " + norm["date"] if norm.get("date") else ""
    ask = ""
    unsupported = list(norm.get("unsupported") or [])
    missing = list(norm.get("missing") or [])
    if any(u.startswith("date:conflict") for u in unsupported):
        ask = " You gave both an exact date and a relative one - tell me which departure date to use."
    elif any(u.startswith("date:invalid") for u in unsupported):
        ask = " That date is not a real calendar date - give me an exact date (YYYY-MM-DD) or say today/tomorrow."
    elif any(u.startswith("date:return-invalid") for u in unsupported):
        ask = " That return date is not a real calendar date - give me an exact return date (YYYY-MM-DD)."
    elif any(u.startswith("date:return-order") for u in unsupported):
        ask = " The return date is before the departure date - give me the correct dates."
    elif any(u.startswith("date:alternatives") for u in unsupported):
        ask = " You gave more than one departure date - tell me which one to use."
    elif any(u.startswith("date:unsupported") for u in unsupported):
        ask = " I can't resolve that relative date yet - give me an exact date (YYYY-MM-DD) or say today/tomorrow."
    elif any(u.startswith("date:return-unsupported") for u in unsupported):
        ask = " I can't resolve that relative return date yet - give me an exact return date (YYYY-MM-DD)."
    elif "multi-city" in unsupported:
        ask = " I can only check one-way or return trips between two cities."
    elif any(u.startswith("route:via") for u in unsupported):
        ask = " I can only check direct routings between two cities - no via stops yet."
    elif any(u.startswith("passengers:invalid") for u in unsupported):
        ask = " That passenger count does not look right - how many adults (1-9)?"
    elif any(u.startswith("passengers:conflict") for u in unsupported):
        ask = " You gave two different passenger counts - tell me which one."
    elif "passengers:unparsed" in unsupported or "passengers:party-unparsed" in unsupported:
        ask = " How many adults are travelling?"
    elif "passengers:children-infants" in unsupported:
        ask = (" I can't price trips with children or infants yet - "
               "child fares need ages, which this quick check can't handle.")
    elif "cabin:only-negated" in unsupported:
        ask = " Tell me the cabin you do want - economy, premium economy, business or first."
    elif any(u.startswith("time:preference") for u in unsupported):
        ask = " I can't filter by departure time yet - I can only check the whole day."
    elif "airline:preference" in unsupported:
        ask = " I can't filter by airline yet - I would check all airlines on the route."
    elif "airport:terminal" in unsupported:
        ask = " I can't filter by airport terminal yet."
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


_NEWS_FAILED = ("could not", "cannot", "no dated news", "no news",
                "public lookup failed", "no readable sources",
                "the pages i read", "unavailable", "failed", "no results")


def lookup(text, now_local=None):
    """One structured quick-lookup attempt (attempt 1). A flight request is
    normalized and RESOLVED BEFORE the attempt: incomplete/unsupported
    asks return a clarification result with NO network call; complete,
    supported asks are preserved on EVERY failure exit."""
    when = datetime.now(timezone.utc).isoformat(timespec='seconds')
    flight = flight_request(text, now_local)
    if flight is not None and (flight["missing"] or flight["unsupported"]):
        return LookupResult(
            STATUS_INSUFFICIENT, _flight_reply(flight, when), when,
            FAILURE_INCOMPLETE if flight["missing"] else FAILURE_UNSUPPORTED,
            flight, (), escalation_eligible=False)
    norm = flight if flight else {}
    if flight is None and re.search(r'(?i)\bnews\b',text):
        import cr_group
        out = cr_group.answer({'text':'@crayon_v1_bot '+text,'source_links':True})
        # preserve wrapper behavior WITHOUT upgrading unverified news prose
        low = (out or '').lower()
        if not low.strip() or any(mk in low for mk in _NEWS_FAILED):
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
        focus=None;pages=[]
    try:results=results if focus else W.search(text[:400],5)
    except Exception:
        return LookupResult(STATUS_UNAVAILABLE,
            'Web search did not return current results this time. No current price, fare or forecast is verified. Try a narrower place/date/product query.',
            when, FAILURE_SEARCH, norm, (),
            escalation_eligible=_flight_eligible(flight, FAILURE_SEARCH))
    # Search results are leads, never page evidence. Fetch exact destinations.
    for row in ([] if focus else results[:5]):
        try:pages.append(W.fetch(row['url']))
        except Exception:pass
    leads = tuple(r.get('url','') for r in results[:5] if r.get('url','').startswith('https://'))
    fetched = tuple(p.get('url','') for p in pages if p.get('url','').startswith('https://'))
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
                # guard); only complete+supported asks reach this branch
                validate_static_flight_evidence(flight, pages)
                return LookupResult(
                    STATUS_INSUFFICIENT, _flight_reply(flight, when), when,
                    FAILURE_CONTENT,
                    flight, leads, fetched,
                    escalation_eligible=_flight_eligible(flight, FAILURE_CONTENT))
            return LookupResult(STATUS_VERIFIED, checked+'\n\nChecked: '+when,
                                when, "", norm, leads,
                                fetched_pages=fetched)
    if flight:
        return LookupResult(
            STATUS_INSUFFICIENT, _flight_reply(flight, when), when,
            FAILURE_CONTENT, flight, leads, fetched,
            escalation_eligible=_flight_eligible(flight, FAILURE_CONTENT))
    return LookupResult(STATUS_INSUFFICIENT,
        lead_reply(text,results)+'\n\nChecked: '+when+'\nNo current number is verified.',
        when, FAILURE_CONTENT, norm, leads)


def answer(text):
    """Prose wrapper preserving the existing news/weather/research replies."""
    return lookup(text).text
