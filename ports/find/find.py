"""ports/find - find core: planned queries, verified citations, sources panel.

Find-feature upgrade (user research: the loudest complaints about AI
search are wrong/invented citations, single-search shallowness, missing
source dates, and answers that forget the previous question). This
module owns the DECISIONS around a web find: how a question becomes a
small deterministic query plan, which citations survive verification,
what the sources panel shows, and how follow-ups resolve. Searching and
fetching themselves are integration-injected callables; this module
never touches the network.

REUSE BOUNDARY (not a parallel stack): the running app already owns
research/comparison fanout (cr_web.research), the production
claim/citation evidence gate (cr_citations.checked_answer), dated news
search (cr_web.news) and conversation context (cr_memory). This module
adds only what those do not have: a validated planning API, RECORDED
citation drops, the canonical sources panel, explicit recency windows,
and ordinal follow-up references. The citation support check is
injectable (support_fn); production wiring must be the existing
checked_answer gate - the shipped deterministic gate is a hardened
reference implementation for source-only testing. This module performs
no searching, fetching, news handling or conversation memory of its own.

Design invariants:

1. VERIFIED CITATIONS ONLY: a citation survives only when its URL is
   fetched through the injected fetcher AND the fetched content
   supports the claim. Unfetchable or unsupported citations are DROPPED
   and recorded - never presented.
2. SUPPORT IS MORE THAN SHARED TOKENS: the reference gate scores the
   best sentence-window, requires the claim's numbers to appear in it,
   and requires negation parity (claim negation words exactly match the
   window's) - "drug cures cancer" against "drug does not cure cancer"
   and "revenue 999 million" against "revenue 10 million" both refuse.
3. UNKNOWN REFUSES WITHOUT RETRY: a fetch error drops that citation;
   the module never retries, never substitutes a different URL.
4. ATOMIC VERIFICATION: all derived state (verified, dropped, sources
   panel, session) is computed and validated first; a source-cap
   violation refuses the WHOLE batch leaving prior plan state and the
   session byte-identical.
5. DETERMINISTIC PLANNING: the fallback decomposer is pure and REFUSES
   questions that decompose past the subquery cap (no silent subject
   drops); an injected planner is validated the same way.
6. RECENCY IS EXPLICIT + OWNER-LOCAL: today/yesterday are owner-local
   days (injected tz offset); this/last week are ISO Monday weeks;
   this/last month are calendar months; past week/month are rolling;
   latest/recent/news is a rolling 7 days. The window rides on the
   plan so the integration can bound its search.
7. OWNER-STRICT SESSIONS: follow-up references ("the second one")
   resolve against the OWNER's own last verified result only.
8. CAPS REFUSE, NEVER EVICT; OVERFLOW REJECTS, NEVER TRUNCATES.
9. READ-ONLY, NOT MOUNTED: no action paths, no network.
"""

import calendar
import re
import time

from urlcanon import canonical_url, UrlCanonError

MAX_QUERY = 500
MAX_SUBQUERIES = 4
MAX_SUBQUERY = 500  # >= MAX_QUERY: an undecomposed query passes through whole
MAX_CLAIMS = 20
MAX_CLAIM_TEXT = 1000
MAX_URL = 500
MAX_SOURCES = 10
MAX_PLANS_PER_OWNER = 50
MIN_SUPPORT_OVERLAP = 0.34  # fraction of claim tokens in the best window
DAY = 86400

ORDINALS = {"first": 1, "1st": 1, "second": 2, "2nd": 2, "third": 3,
            "3rd": 3, "fourth": 4, "4th": 4, "fifth": 5, "5th": 5,
            "sixth": 6, "6th": 6, "seventh": 7, "7th": 7, "eighth": 8,
            "8th": 8, "ninth": 9, "9th": 9, "tenth": 10, "10th": 10,
            "last": -1}

_STOP = frozenset("the a an and or of to in on for with is are was were "
                  "be been it its this that these those what who when "
                  "where which how why did do does can could should "
                  "would about from by at as".split())

_NEG = frozenset("not no never neither none dont doesn't don't isnt "
                 "isn't aren't arent wasn't wasnt weren't werent won't "
                 "wont cannot can't cant without lack lacks lacking "
                 "fail fails failed hardly barely nor nobody nothing "
                 "nowhere".split())

_NUM = re.compile(r"\d+(?:\.\d+)?")


class FindError(Exception):
    pass


class UrlError(FindError):
    pass


class PlanLimitReached(FindError):
    pass


class SourceLimitReached(FindError):
    pass


def _tok(text):
    return [w for w in re.findall(r"[a-z0-9']+", (text or "").lower())
            if w not in _STOP and len(w) > 1]


def _canon(url):
    try:
        return canonical_url(url, max_url=MAX_URL, error=UrlError)
    except UrlCanonError as e:
        raise UrlError(str(e))


def _shift(now, tz_offset_minutes):
    """Owner-local epoch used for calendar boundaries."""
    return now + tz_offset_minutes * 60


def _month_edge(y, m):
    return calendar.timegm((y, m, 1, 0, 0, 0, 0, 1, 0))


def parse_window(query, now, tz_offset_minutes=0):
    """Resolve recency words to an [after, before) epoch window or None.
    Owner-local calendar semantics: today/yesterday are local days;
    this/last week are ISO Monday weeks; this/last month are calendar
    months; past week/month are rolling 7/30 days; latest/recent/news/
    current is a rolling 7 days."""
    q = query.lower()
    loc = _shift(now, tz_offset_minutes)
    day0 = int(loc // DAY) * DAY
    back = -tz_offset_minutes * 60
    if re.search(r'\b(today|tonight)\b', q):
        return (day0 + back, day0 + DAY + back)
    if re.search(r'\byesterday\b', q):
        return (day0 - DAY + back, day0 + back)
    g = time.gmtime(loc)
    if re.search(r'\bthis week\b', q):
        w0 = day0 - g.tm_wday * DAY
        return (w0 + back, w0 + 7 * DAY + back)
    if re.search(r'\blast week\b', q):
        w0 = day0 - g.tm_wday * DAY
        return (w0 - 7 * DAY + back, w0 + back)
    if re.search(r'\bpast week\b|\bpast 7 days\b', q):
        return (now - 7 * DAY, now)
    if re.search(r'\bthis month\b', q):
        return (_month_edge(g.tm_year, g.tm_mon) + back,
                _month_edge(g.tm_year + (g.tm_mon == 12), g.tm_mon % 12 + 1) + back)
    if re.search(r'\blast month\b', q):
        prev_y = g.tm_year - (g.tm_mon == 1)
        prev_m = (g.tm_mon - 2) % 12 + 1
        return (_month_edge(prev_y, prev_m) + back,
                _month_edge(g.tm_year, g.tm_mon) + back)
    if re.search(r'\bpast month\b|\bpast 30 days\b', q):
        return (now - 30 * DAY, now)
    if re.search(r'\b(latest|recent|recently|news|breaking|current)\b', q):
        return (now - 7 * DAY, now)
    m = re.search(r'\bin (20\d\d)\b', q)
    if m:
        y = int(m.group(1))
        return (_month_edge(y, 1), _month_edge(y + 1, 1))
    return None


def _fallback_decompose(query):
    """Deterministic decomposition: comparisons and conjunctions split,
    otherwise the query stands alone. REFUSES over-cap decompositions -
    silently dropping a requested subject would change the question."""
    q = query.strip()
    m = re.match(r'(?is)^(?:compare|comparison of)\s+(.+?)\s+'
                 r'(?:and|with|versus|vs\.?)\s+(.+)$', q)
    if m:
        parts = [m.group(1).strip(), m.group(2).strip()]
    else:
        parts = re.split(r'(?i)\s+and also\s+|\s*;\s*', q)
    out, seen = [], set()
    for p in parts:
        p = p.strip(" .?")
        if p and p.lower() not in seen:
            seen.add(p.lower())
            out.append(p)
    if not out:
        out = [q]
    if len(out) > MAX_SUBQUERIES:
        raise FindError("question decomposes into %d parts, cap is %d"
                        % (len(out), MAX_SUBQUERIES))
    return out


def _validate_subqueries(subs):
    if not isinstance(subs, (list, tuple)) or not subs:
        raise FindError("planner returned no subqueries")
    if len(subs) > MAX_SUBQUERIES:
        raise FindError("planner returned over %d subqueries" % MAX_SUBQUERIES)
    out = []
    for s in subs:
        if not isinstance(s, str) or not s.strip():
            raise FindError("planner returned an empty subquery")
        if len(s) > MAX_SUBQUERY:
            raise FindError("subquery over %d chars" % MAX_SUBQUERY)
        out.append(s.strip())
    return out


def _norm_words(text):
    return re.sub(r"\s+", " ",
                  re.sub(r"[^a-z0-9 ]", " ", (text or "").lower())).strip()


def reference_support(claim_text, content):
    """FAIL-CLOSED quote-evidence gate (stdlib, source-only default).
    A claim is supported ONLY when its full normalized text appears
    verbatim (case/punctuation-insensitive, whitespace-folded) in the
    fetched content - positive quote evidence. Paraphrase, shared-token
    similarity, numeric coincidence and role reorderings all refuse:
    "drug cures cancer" vs "drug causes cancer", "revenue 999 million"
    vs "revenue 10 million ... workforce 999 people", "Alice acquired
    Beta" vs "Beta acquired Alice". Paraphrase-level support is the
    production gate's job - wire it via support_fn (checked_answer
    adapter); anything the default cannot positively quote-verify is
    dropped, never verified."""
    claim = _norm_words(claim_text)
    body = _norm_words(content)
    if not claim or not body:
        return False
    return (" " + claim + " ") in (" " + body + " ")


class Plan(object):
    __slots__ = ("pid", "owner", "query", "subqueries", "window",
                 "created_at", "claims", "verified", "dropped", "sources")

    def __init__(self, pid, owner, query, subqueries, window, created_at):
        self.pid = pid
        self.owner = owner
        self.query = query
        self.subqueries = subqueries
        self.window = window
        self.created_at = created_at
        self.claims = []
        self.verified = []
        self.dropped = []
        self.sources = []


class MemFindStore(object):
    def __init__(self):
        self.plans = {}          # owner -> [Plan]
        self.sessions = {}       # owner -> last verified sources list


class Find(object):
    def __init__(self, store, id_factory=None):
        self.store = store
        self.id_factory = id_factory or (lambda p, s, n: "%s%d" % (p, s))

    def _plans(self, owner):
        return self.store.plans.setdefault(owner, [])

    def _plan(self, owner, pid):
        for p in self._plans(owner):
            if p.pid == pid:
                return p
        raise FindError("no such plan")

    def plan(self, owner, query, now, decompose_fn=None, tz_offset_minutes=0):
        """Build a query plan. Pure decision: subqueries + window.
        decompose_fn (optional) is validated; bad output refuses the
        whole plan with zero effects. tz_offset_minutes is the owner's
        local offset for calendar words (default UTC)."""
        if not owner or not isinstance(owner, str):
            raise FindError("owner required")
        if not isinstance(query, str) or not query.strip():
            raise FindError("query required")
        if len(query) > MAX_QUERY:
            raise FindError("query over %d chars" % MAX_QUERY)
        if not isinstance(tz_offset_minutes, int) or isinstance(tz_offset_minutes, bool):
            raise FindError("tz_offset_minutes must be int minutes")
        plans = self._plans(owner)
        if len(plans) >= MAX_PLANS_PER_OWNER:
            raise PlanLimitReached("plan cap %d reached" % MAX_PLANS_PER_OWNER)
        if decompose_fn is not None:
            subs = _validate_subqueries(decompose_fn(query))
        else:
            subs = _validate_subqueries(_fallback_decompose(query))
        window = parse_window(query, now, tz_offset_minutes)
        p = Plan(self.id_factory("pl", len(plans) + 1, now),
                 owner, query.strip(), subs, window, now)
        plans.append(p)
        return p

    def verify(self, owner, pid, claims, fetch_fn, now, support_fn=None):
        """Verify claimed citations against fetched content. Each claim:
        {"text": str, "url": str, "title": str?, "published": float?}.
        A citation survives only when fetch_fn(url) returns content that
        supports the claim (support_fn if given, else reference_support).
        Dropped citations are recorded with their reason. ATOMIC: all
        derived state is computed and validated first; any refusal
        (over-cap batch, source-cap) leaves prior plan state and the
        session byte-identical."""
        support = support_fn or reference_support
        p = self._plan(owner, pid)
        if not isinstance(claims, (list, tuple)):
            raise FindError("claims must be a list")
        if len(claims) > MAX_CLAIMS:
            raise FindError("claim batch over %d" % MAX_CLAIMS)
        for c in claims:
            if not isinstance(c, dict) or not isinstance(c.get("text"), str) \
                    or not c["text"].strip():
                raise FindError("each claim needs text")
            if len(c["text"]) > MAX_CLAIM_TEXT:
                raise FindError("claim text over %d chars" % MAX_CLAIM_TEXT)
            if not isinstance(c.get("url"), str):
                raise FindError("each claim needs a url")
            if len(c["url"]) > MAX_URL:
                raise FindError("url over %d chars" % MAX_URL)
        verified, dropped = [], []
        for c in claims:
            entry = {"text": c["text"].strip(), "url": c["url"].strip(),
                     "title": (c.get("title") or "").strip(),
                     "published": c.get("published")}
            try:
                canon = _canon(c["url"])
            except UrlError:
                entry["reason"] = "bad_url"
                dropped.append(entry)
                continue
            entry["canonical"] = canon
            try:
                content = fetch_fn(c["url"])
            except Exception:
                entry["reason"] = "fetch_failed"
                dropped.append(entry)
                continue
            if not isinstance(content, str) or not content.strip():
                entry["reason"] = "empty_content"
                dropped.append(entry)
                continue
            try:
                ok = bool(support(c["text"], content))
            except Exception:
                entry["reason"] = "support_error"
                dropped.append(entry)
                continue
            if not ok:
                entry["reason"] = "unsupported"
                dropped.append(entry)
                continue
            verified.append(entry)
        sources = self._build_sources(verified)  # raises BEFORE any commit
        p.claims = [dict(c) for c in claims]
        p.verified = verified
        p.dropped = dropped
        p.sources = sources
        self.store.sessions[owner] = [dict(s) for s in sources]
        return {"pid": pid, "verified": [dict(v) for v in verified],
                "dropped": [dict(d) for d in dropped],
                "sources": [dict(s) for s in sources]}

    def _build_sources(self, verified):
        seen, out = {}, []
        for v in verified:
            k = v["canonical"]
            if k in seen:
                continue
            seen[k] = True
            out.append({"url": v["url"], "canonical": k,
                        "title": v["title"], "published": v["published"]})
            if len(out) > MAX_SOURCES:
                raise SourceLimitReached("over %d distinct sources"
                                         % MAX_SOURCES)
        return out

    def resolve_reference(self, owner, text):
        """Resolve a follow-up reference against the owner's own last
        verified sources. Returns {"index": n, "source": {...}} or None.
        Owner-strict: never reads another owner's session."""
        if not isinstance(text, str):
            return None
        t = text.lower().strip()
        m = re.search(r'\b(' + '|'.join(ORDINALS) + r')\b\s*(?:one|result|source|link)?\b', t)
        if not m:
            return None
        sources = self.store.sessions.get(owner) or []
        if not sources:
            return None
        n = ORDINALS[m.group(1)]
        idx = n - 1 if n > 0 else len(sources) - 1
        if idx < 0 or idx >= len(sources):
            return None
        return {"index": idx + 1, "source": dict(sources[idx])}

    def sources(self, owner, pid):
        return [dict(s) for s in self._plan(owner, pid).sources]

    def session(self, owner):
        return [dict(s) for s in (self.store.sessions.get(owner) or [])]

    def export(self, owner):
        return [{"pid": p.pid, "query": p.query, "subqueries": p.subqueries,
                 "window": p.window, "created_at": p.created_at,
                 "verified": len(p.verified), "dropped": len(p.dropped)}
                for p in self._plans(owner)]
