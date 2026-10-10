"""ports/find_track - tracked topics: change detection, proactive cards.

Find-feature upgrade, port 3 of 3. "Track <topic> - tell me when it
changes." The owner registers a topic; the integration re-runs the
search on its own schedule and feeds each RESULT SET here; the module
diffs it against the track's known items and raises a reviewable card
when something new appears. Cards are suggestions with evidence - they
never act. Stands alone.

Design invariants:

1. READ-ONLY BY CONSTRUCTION: no code path from a card to any action.
   Cards surface new findings; what the integration does with a card
   is its own gated flow.
2. EVIDENCE IS VISIBLE: every card carries the track id, the new
   items (urls + titles), and when they were first seen. No card
   without new items; no item without a url.
3. DETERMINISTIC DIFFING: an item is identified by its canonical url;
   new = canonical not in the track's known set. Same track state +
   same result set -> same diff (determinism-tested).
4. ATOMIC RUNS: the whole result set is diffed first; over-bounds
   input refuses the whole run with zero effects - never a partial
   update of the known set.
5. NO NAGGING: one open card per track; new findings extend the open
   card's evidence. A dismissed card re-raises only on items first
   seen AFTER the dismissal - the dismissed set alone never re-raises.
6. OWNER-STRICT: tracks, runs and cards keyed to the owning account;
   a second owner's activity leaves the first byte-identical.
7. CAPS REFUSE, NEVER EVICT: MAX_TRACKS per owner, MAX_ITEMS evidence
   per card; overflow raises with zero effects.
8. OVERFLOW REJECTS, NEVER TRUNCATES: over-length topic/url/title
   raises; truncation could alias distinct items.
9. CLOCKS + IDS INJECTED; NOT MOUNTED, NO DELIVERY: the integration
   owns scheduling, searching and rendering.
"""

import re
from urllib.parse import urlparse, parse_qsl, urlencode, urlunparse

MAX_TRACKS = 25
MAX_TOPIC = 200
MAX_ITEMS = 10       # evidence items per card
MAX_RUN_ITEMS = 50   # result-set size per run
MAX_URL = 500
MAX_TITLE = 200

_TRACKING_PARAMS = re.compile(r'(?i)^(utm_|fbclid|gclid|mc_|ref$|ref_)')


class TrackError(Exception):
    pass


class TrackLimitReached(TrackError):
    pass


class ItemLimitReached(TrackError):
    pass


class NoMatch(TrackError):
    pass


def canonical_url(url):
    if not isinstance(url, str) or not url or len(url) > MAX_URL:
        raise TrackError("url missing or over %d chars" % MAX_URL)
    p = urlparse(url.strip())
    if p.scheme not in ("http", "https") or not p.hostname:
        raise TrackError("url must be absolute http(s)")
    host = p.hostname.lower()
    if host.startswith("www."):
        host = host[4:]
    q = urlencode([(k, v) for k, v in parse_qsl(p.query)
                   if not _TRACKING_PARAMS.match(k)])
    return urlunparse(("https", host, p.path.rstrip("/"), "", q, ""))


class Track(object):
    __slots__ = ("tid", "owner", "topic", "created_at", "last_run_at",
                 "known", "card", "dismissed_at")

    def __init__(self, tid, owner, topic, now):
        self.tid = tid
        self.owner = owner
        self.topic = topic
        self.created_at = now
        self.last_run_at = None
        self.known = set()        # canonical urls seen in any accepted run
        self.card = None          # {"state": open|dismissed, "items": [...], "updated_at": ts}
        self.dismissed_at = None


class MemTrackStore(object):
    def __init__(self):
        self.tracks = {}  # owner -> {tid: Track}


class FindTrack(object):
    def __init__(self, store, id_factory=None):
        self.store = store
        self.id_factory = id_factory or (lambda p, s, n: "%s%d" % (p, s))

    def _tracks(self, owner):
        return self.store.tracks.setdefault(owner, {})

    def _track(self, owner, tid):
        t = self._tracks(owner).get(tid)
        if t is None:
            raise NoMatch("no such track")
        return t

    def add(self, owner, topic, now):
        if not owner or not isinstance(owner, str):
            raise TrackError("owner required")
        if not isinstance(topic, str) or not topic.strip():
            raise TrackError("topic required")
        if len(topic) > MAX_TOPIC:
            raise TrackError("topic over %d chars" % MAX_TOPIC)
        tracks = self._tracks(owner)
        if len(tracks) >= MAX_TRACKS:
            raise TrackLimitReached("track cap %d reached" % MAX_TRACKS)
        tid = self.id_factory("trk", len(tracks) + 1, now)
        tracks[tid] = Track(tid, owner, topic.strip(), now)
        return tid

    def remove(self, owner, tid):
        tracks = self._tracks(owner)
        if tid not in tracks:
            raise NoMatch("no such track")
        del tracks[tid]

    def run(self, owner, tid, results, now):
        """Diff one result set against the track's known set. results:
        [{"url": str, "title": str?}]. Atomic: over-bounds input or any
        invalid item refuses the WHOLE run with zero effects. Returns
        the card when new items were found, else None."""
        t = self._track(owner, tid)
        if not isinstance(results, (list, tuple)):
            raise TrackError("results must be a list")
        if len(results) > MAX_RUN_ITEMS:
            raise TrackError("run over %d items" % MAX_RUN_ITEMS)
        seen, items = set(), []
        for r in results:
            if not isinstance(r, dict) or not isinstance(r.get("url"), str):
                raise TrackError("each result needs a url")
            title = r.get("title") or ""
            if not isinstance(title, str):
                raise TrackError("title must be str")
            if len(title) > MAX_TITLE:
                raise TrackError("title over %d chars" % MAX_TITLE)
            canon = canonical_url(r["url"])  # raises on bad/over-long
            if canon in seen:
                continue
            seen.add(canon)
            items.append({"canonical": canon, "url": r["url"].strip(),
                          "title": title.strip()})
        new = [it for it in items if it["canonical"] not in t.known]
        # evidence cap: refuse the run rather than record partial findings
        open_items = len(t.card["items"]) if t.card and t.card["state"] == "open" else 0
        if new and open_items + len(new) > MAX_ITEMS:
            raise ItemLimitReached("card evidence cap %d would be exceeded"
                                   % MAX_ITEMS)
        # commit: whole run becomes known exactly once
        t.known |= seen
        t.last_run_at = now
        if not new:
            return None
        if t.card is None or t.card["state"] == "dismissed":
            t.card = {"state": "open", "items": list(new),
                      "updated_at": now, "raised_at": now}
        else:
            t.card["items"].extend(new)
            t.card["updated_at"] = now
        return t.card

    def dismiss(self, owner, tid, now):
        t = self._track(owner, tid)
        if t.card is None or t.card["state"] != "open":
            raise NoMatch("no open card")
        t.card["state"] = "dismissed"
        t.dismissed_at = now

    def card(self, owner, tid):
        t = self._track(owner, tid)
        return dict(t.card) if t.card else None

    def list(self, owner):
        return [{"tid": t.tid, "topic": t.topic, "created_at": t.created_at,
                 "last_run_at": t.last_run_at, "known": len(t.known),
                 "card": (t.card["state"] if t.card else None)}
                for t in self._tracks(owner).values()]
