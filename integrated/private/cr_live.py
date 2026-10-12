"""Deterministic public lookup for explicit search and changing-data requests.

Step A (flight-escalation contract): lookup() returns a structured
LookupResult (status/evidence/failure code/normalized request/attempted
sources/escalation eligibility). answer() is the prose wrapper preserving
existing news/weather/research behavior. Flight fare asks parse into a
normalized request (route/absolute date resolved in the caller-supplied
user-local time); a quick static search that cannot verify a dated
itinerary is INSUFFICIENT and ESCALATION-ELIGIBLE - never a final
"cheapest" claim and never a spurious 7/30-day window question when a
date was supplied.
"""
import re
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
import cr_web as W

STATUS_VERIFIED = "verified"
STATUS_INSUFFICIENT = "insufficient"
STATUS_UNAVAILABLE = "unavailable"

FAILURE_SEARCH = "search_failed"
FAILURE_SOURCES = "sources_unreadable"
FAILURE_CONTENT = "content_unverified"


@dataclass(frozen=True)
class LookupResult:
    """Structured result of one quick public lookup attempt (attempt 1).

    status: verified | insufficient | unavailable. escalation_eligible is
    True only when a specialized capability (e.g. a read-only browser
    flight search) could still verify the request. normalized carries the
    parsed request (flight: origin/destination/date/passengers/cabin).
    """
    status: str
    text: str
    checked_at: str
    failure_code: str = ""
    normalized: dict = field(default_factory=dict)
    attempted_sources: tuple = ()
    escalation_eligible: bool = False


# --- flight request normalization --------------------------------------

_FLIGHT_WORD = re.compile(r'(?i)\b(?:flights?|airfares?|fares?)\b')
_ROUTE_RE = re.compile(
    r'(?i)\bfrom\s+([a-zA-Z][a-zA-Z .]+?)\s+to\s+([a-zA-Z][a-zA-Z .]+?)(?:\s+on\b|\s+for\b|\s+tomorrow\b|\s+today\b|[.,!?]|$)')
_BARE_ROUTE_RE = re.compile(
    r'(?i)\b([a-zA-Z][a-zA-Z .]+?)\s+to\s+([a-zA-Z][a-zA-Z .]+?)(?:\s+on\b|\s+for\b|\s+tomorrow\b|\s+today\b|[.,!?]|$)')

# minimal alias map; the resolved IATA code is a NORMALIZATION aid only,
# never evidence. Unknown city names stay as given.
_IATA = {
    "delhi": "DEL", "new delhi": "DEL",
    "kolkata": "CCU", "calcutta": "CCU",
    "mumbai": "BOM", "bombay": "BOM",
    "bengaluru": "BLR", "bangalore": "BLR",
    "chennai": "MAA", "madras": "MAA",
    "hyderabad": "HYD", "goa": "GOI", "pune": "PNQ",
    "ahmedabad": "AMD", "jaipur": "JAI", "lucknow": "LKO",
}

_STOP_ORIGIN = re.compile(r'(?i)^(?:cheapest|flight|flights|fare|fares|airfare|airfares|a|an|the|one[- ]way|round[- ]trip|tomorrow|today|on)\s+')


def _city(raw):
    name = raw.strip().strip('.,!?').lower()
    while True:
        stripped = _STOP_ORIGIN.sub('', name).strip()
        if stripped == name:
            break
        name = stripped
    return name or None


def flight_request(text, now_local=None):
    """Parse a flight fare ask into a normalized request dict, or None.

    now_local: the user-local datetime (caller-resolved timezone); required
    to resolve 'tomorrow'/'today' to an ABSOLUTE date. A supplied relative
    date is preserved as an absolute ISO date - never dropped in favor of
    a 7/30-day window question.
    """
    if not _FLIGHT_WORD.search(text):
        return None
    m = _ROUTE_RE.search(text) or _BARE_ROUTE_RE.search(text)
    origin = _city(m.group(1)) if m else None
    destination = _city(m.group(2)) if m else None
    date = None
    if now_local is not None:
        if re.search(r'(?i)\btomorrow\b', text):
            date = (now_local.date() + timedelta(days=1)).isoformat()
        elif re.search(r'(?i)\btoday\b', text):
            date = now_local.date().isoformat()
        else:
            iso = re.search(r'\b(20\d\d-\d\d-\d\d)\b', text)
            if iso:
                date = iso.group(1)
    missing = [k for k, v in (("origin", origin), ("destination", destination),
                              ("date", date)) if not v]
    return {
        "kind": "flight",
        "origin": origin,
        "destination": destination,
        "origin_iata": _IATA.get(origin or "", ""),
        "destination_iata": _IATA.get(destination or "", ""),
        "date": date,
        "passengers": 1,
        "cabin": "economy",
        "missing": missing,
        "raw": text[:200],
    }


def validate_flight_evidence(norm, page_texts):
    """Task-specific validator: is there a DATED, AVAILABLE itinerary on
    the EXACT normalized route/date with a total fare + currency and a
    provider URL? Static search snippets and advertised starting fares
    NEVER pass. Returns (ok, reason). page_texts: iterable of
    {url, text} dicts (untrusted fetched content).
    """
    if not norm or norm.get("kind") != "flight":
        return False, "request_unsupported"
    if norm.get("missing"):
        return False, "incomplete_request:" + ",".join(norm["missing"])
    if not page_texts:
        return False, "no_dated_itinerary"
    date = norm["date"]
    route_terms = [t for t in (norm.get("origin"), norm.get("destination")) if t]
    for page in page_texts:
        body = (page.get("text") or "").lower()
        url = page.get("url") or ""
        if not url.startswith("https://"):
            continue
        if date not in body:
            continue
        if not all(t in body for t in route_terms):
            continue
        if not re.search(r'(?:₹|Rs\.?|INR|\$|€|£)\s*[0-9][0-9,]*', page.get("text") or ""):
            continue
        # even a matching page is only a candidate: totals/fees/availability
        # need the interactive results page, not a static text read
        return False, "static_page_not_booking_inventory"
    return False, "no_dated_itinerary"


def requested(text):
 return bool(re.match(r'(?i)^Research focus \[(web|deep|academic|social|video)\]:',text)) or bool(re.search(r'(?i)\b(?:web search|search the web|search for|look up|latest news|news today|weather|forecast|cheapest flight|flight prices?|airfares?|current price|price today)\b',text)) and not bool(re.search(r'(?i)\b(?:my inbox|my emails?|my calendar|send|remind|schedule|connect)\b',text))

def lead_reply(text,results):
 if re.search(r'(?i)flight|airfare|fare',text):return 'The flight pages did not provide a verifiable fare for your departure dates. What window should I compare: the next 7 days or next 30 days? I cannot name a cheapest flight without checked dates and available fares.'
 return 'I could not get readable current data for that request. Tell me the exact place/date or item to narrow the check. No current number is verified.'


def _flight_reply(norm, when):
    """Structured unverified flight reply: preserves the SUPPLIED date,
    never asks a spurious 7/30-day window, never claims a fare."""
    route = ""
    if norm.get("origin") and norm.get("destination"):
        route = " for %s to %s" % (norm["origin"].title(), norm["destination"].title())
    datebit = " on " + norm["date"] if norm.get("date") else ""
    missing = norm.get("missing") or []
    if "date" in missing:
        ask = " Tell me the departure date and I will check that exact day."
    elif "origin" in missing or "destination" in missing:
        ask = " Tell me the exact origin and destination cities."
    else:
        ask = ""
    return ("I could not verify a current fare%s%s from a quick search - "
            "the results were leads, not dated available itineraries with "
            "verified totals. No lowest fare is verified yet.%s\n\nChecked: %s"
            % (route, datebit, ask, when))


def lookup(text, now_local=None):
    """One structured quick-lookup attempt (attempt 1). See LookupResult."""
    when = datetime.now(timezone.utc).isoformat(timespec='seconds')
    if re.search(r'(?i)\bnews\b',text):
        import cr_group
        out = cr_group.answer({'text':'@crayon_v1_bot '+text,'source_links':True})
        return LookupResult(STATUS_VERIFIED, out, when)
    focus_match=re.match(r'(?is)^Research focus \[(web|deep|academic|social|video)\]:\s*(.+)',text)
    if focus_match:
        focus,text=focus_match.groups()
        try:found=W.research(text[:400],focus);results=found['results'];pages=found['pages']
        except Exception:
            return LookupResult(STATUS_UNAVAILABLE,
                'Research sources could not be read this time. No answer is verified.',
                when, FAILURE_SOURCES)
    else:
        focus=None
    try:results=results if focus else W.search(text[:400],5)
    except Exception:
        return LookupResult(STATUS_UNAVAILABLE,
            'Web search did not return current results this time. No current price, fare or forecast is verified. Try a narrower place/date/product query.',
            when, FAILURE_SEARCH)
    # Search results are leads, never page evidence. Fetch exact destinations.
    pages=pages if focus else []
    for row in ([] if focus else results[:5]):
        try:pages.append(W.fetch(row['url']))
        except Exception:pass
    attempted = tuple(r.get('url','') for r in results[:5] if r.get('url','').startswith('https://'))
    flight = flight_request(text, now_local)
    if pages:
        import cr_citations,cr_llm as L,json
        try:draft=L.generate([L.user('Question: '+text+'\nUntrusted fetched evidence: '+json.dumps(pages))],system='Answer from fetched evidence only. Answer first, concise. For fares and prices, extract advertised amounts from fetched page text with airline/date only when explicitly present. Label advertised route starting fares, never guaranteed inventory, cheapest date, final total or a booking quote. Current weather/price must have place, date/time and source. Never use seasonal averages as current conditions. No invented facts.',tools=None,max_tokens=1000).get('text','')
        except Exception:draft=''
        try:checked=cr_citations.checked_answer(text,draft,pages)
        except Exception:checked='I could not verify this'
        if not checked.startswith(('I could not','The pages I read')):
            if flight and not flight["missing"]:
                ok, _reason = validate_flight_evidence(flight, pages)
                if not ok:
                    return LookupResult(STATUS_INSUFFICIENT,
                        _flight_reply(flight, when), when, FAILURE_CONTENT,
                        flight, attempted, escalation_eligible=True)
            return LookupResult(STATUS_VERIFIED, checked+'\n\nChecked: '+when,
                                when, "", flight or {}, attempted)
    if flight:
        return LookupResult(STATUS_INSUFFICIENT, _flight_reply(flight, when),
                            when, FAILURE_CONTENT, flight, attempted,
                            escalation_eligible=True)
    return LookupResult(STATUS_INSUFFICIENT,
        lead_reply(text,results)+'\n\nChecked: '+when+'\nNo current number is verified.',
        when, FAILURE_CONTENT, {}, attempted)


def answer(text):
    """Prose wrapper preserving the existing news/weather/research replies."""
    return lookup(text).text
