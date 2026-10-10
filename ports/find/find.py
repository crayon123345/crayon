"""ports/find - find core: planned queries, verified citations, sources panel.

Find-feature upgrade (user research: the loudest complaints about AI
search are wrong/invented citations, single-search shallowness, missing
source dates, and answers that forget the previous question). This
module owns the DECISIONS around a web find: how a question becomes a
small deterministic query plan, which citations survive verification,
what the sources panel shows, and how follow-ups resolve. Searching and
fetching themselves are integration-injected callables; this module
never touches the network.

Design invariants:

1. VERIFIED CITATIONS ONLY: a citation survives only when its URL is
   fetched through the injected fetcher AND the fetched content
   supports the claim (token-overlap check). Unfetchable or
   unsupported citations are DROPPED and recorded - never presented.
2. UNKNOWN REFUSES WITHOUT RETRY: a fetch error drops that citation;
   the module never retries, never substitutes a different URL.
3. DETERMINISTIC PLANNING: the fallback decomposer is pure; an injected
   planner is validated (1..4 subqueries, each within bounds) and
   out-of-bounds output REFUSES the whole plan with zero effects.
4. RECENCY IS EXPLICIT: "latest"/"today"/"this week" etc. resolve to a
   concrete (after, before) window against the injected clock; the
   window rides on the plan so the integration can bound its search.
5. OWNER-STRICT SESSIONS: follow-up references ("the second one")
   resolve against the OWNER's own last verified result only. A second
   owner's session is invisible and untouched.
6. CAPS REFUSE, NEVER EVICT: plan/claim/source bounds raise with zero
   effects - nothing is silently dropped to make room.
7. OVERFLOW REJECTS, NEVER TRUNCATES: over-length query, subquery,
   claim or URL raises; truncation could alias distinct requests.
8. READ-ONLY, NOT MOUNTED: no action paths. The integration runs
   searches/fetches and renders; this module owns state and decisions.
"""

import calendar
import re
import time
from urllib.parse import urlparse, parse_qsl, urlencode, urlunparse

MAX_QUERY = 500
MAX_SUBQUERIES = 4
MAX_SUBQUERY = 500  # >= MAX_QUERY: an undecomposed query passes through whole
MAX_CLAIMS = 20
MAX_CLAIM_TEXT = 1000
MAX_URL = 500
MAX_SOURCES = 10
MAX_PLANS_PER_OWNER = 50
MIN_SUPPORT_OVERLAP = 0.34  # fraction of claim tokens found in fetched text
DAY = 86400

_TRACKING_PARAMS = re.compile(r'(?i)^(utm_|fbclid|gclid|mc_|ref$|ref_)')

ORDINALS = {"first": 1, "1st": 1, "second": 2, "2nd": 2, "third": 3,
            "3rd": 3, "fourth": 4, "4th": 4, "fifth": 5, "5th": 5,
            "sixth": 6, "6th": 6, "seventh": 7, "7th": 7, "eighth": 8,
            "8th": 8, "ninth": 9, "9th": 9, "tenth": 10, "10th": 10,
            "last": -1}

_STOP = frozenset("the a an and or of to in on for with is are was were "
                  "be been it its this that these those what who when "
                  "where which how why did do does can could should "
                  "would about from by at as".split())


class FindError(Exception):
    pass


class PlanLimitReached(FindError):
    pass


class SourceLimitReached(FindError):
    pass


def _tok(text):
    return [w for w in re.findall(r"[a-z0-9]+", (text or "").lower())
            if w not in _STOP and len(w) > 1]


def canonical_url(url):
    """Canonical form for dedup: lowercase scheme+host, no fragment,
    no tracking params, no trailing slash on path."""
    if not isinstance(url, str) or not url or len(url) > MAX_URL:
        raise FindError("url missing or over %d chars" % MAX_URL)
    p = urlparse(url.strip())
    if p.scheme not in ("http", "https") or not p.hostname:
        raise FindError("url must be absolute http(s)")
    host = p.hostname.lower()
    if host.startswith("www."):
        host = host[4:]
    q = urlencode([(k, v) for k, v in parse_qsl(p.query)
                   if not _TRACKING_PARAMS.match(k)])
    path = p.path.rstrip("/")
    return urlunparse(("https", host, path, "", q, ""))


def parse_window(query, now):
    """Resolve recency words to an (after, before) window or None.
    Boundaries are inclusive-exclusive [after, before) on epoch days."""
    q = query.lower()
    day0 = int(now // DAY) * DAY
    if re.search(r'\b(today|tonight)\b', q):
        return (day0, day0 + DAY)
    if re.search(r'\byesterday\b', q):
        return (day0 - DAY, day0)
    if re.search(r'\b(this week|past week|last week)\b', q):
        return (day0 - 7 * DAY, day0 + DAY)
    if re.search(r'\b(this month|past month|last month)\b', q):
        return (day0 - 30 * DAY, day0 + DAY)
    if re.search(r'\b(latest|recent|recently|news|breaking|current)\b', q):
        return (day0 - 7 * DAY, day0 + DAY)
    m = re.search(r'\bin (20\d\d)\b', q)
    if m:
        after = calendar.timegm((int(m.group(1)), 1, 1, 0, 0, 0, 0, 1, 0))
        before = calendar.timegm((int(m.group(1)) + 1, 1, 1, 0, 0, 0, 0, 1, 0))
        return (after, before)
    return None


def _fallback_decompose(query):
    """Deterministic decomposition: comparisons and conjunctions split,
    otherwise the query stands alone. Never more than 4 parts."""
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
    return out[:MAX_SUBQUERIES] or [q]


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

    def _next_seq(self, owner):
        return len(self._plans(owner)) + 1

    def plan(self, owner, query, now, decompose_fn=None):
        """Build a query plan. Pure decision: subqueries + window.
        decompose_fn (optional) is validated; bad output refuses the
        whole plan with zero effects."""
        if not owner or not isinstance(owner, str):
            raise FindError("owner required")
        if not isinstance(query, str) or not query.strip():
            raise FindError("query required")
        if len(query) > MAX_QUERY:
            raise FindError("query over %d chars" % MAX_QUERY)
        plans = self._plans(owner)
        if len(plans) >= MAX_PLANS_PER_OWNER:
            raise PlanLimitReached("plan cap %d reached" % MAX_PLANS_PER_OWNER)
        if decompose_fn is not None:
            subs = _validate_subqueries(decompose_fn(query))
        else:
            subs = _validate_subqueries(_fallback_decompose(query))
        window = parse_window(query, now)
        p = Plan(self.id_factory("pl", self._next_seq(owner), now),
                 owner, query.strip(), subs, window, now)
        plans.append(p)
        return p

    def verify(self, owner, pid, claims, fetch_fn, now):
        """Verify claimed citations against fetched content. Each claim:
        {"text": str, "url": str, "title": str?, "published": float?}.
        A citation survives only when fetch_fn(url) returns content
        whose tokens support the claim text. Dropped citations are
        recorded with their reason. Atomic: over-cap input refuses the
        whole batch with zero effects."""
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
                canon = canonical_url(c["url"])
            except FindError:
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
            ct, ft = set(_tok(c["text"])), set(_tok(content))
            if not ct or len(ct & ft) / float(len(ct)) < MIN_SUPPORT_OVERLAP:
                entry["reason"] = "unsupported"
                dropped.append(entry)
                continue
            verified.append(entry)
        p.claims = list(claims)
        p.verified = verified
        p.dropped = dropped
        p.sources = self._build_sources(verified)
        self.store.sessions[owner] = list(p.sources)
        return {"pid": pid, "verified": verified, "dropped": dropped,
                "sources": p.sources}

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
        return {"index": idx + 1, "source": sources[idx]}

    def sources(self, owner, pid):
        return list(self._plan(owner, pid).sources)

    def session(self, owner):
        return list(self.store.sessions.get(owner) or [])

    def export(self, owner):
        return [{"pid": p.pid, "query": p.query, "subqueries": p.subqueries,
                 "window": p.window, "created_at": p.created_at,
                 "verified": len(p.verified), "dropped": len(p.dropped)}
                for p in self._plans(owner)]
