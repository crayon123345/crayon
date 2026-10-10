"""ports/find_track - tracked topics: known-set diffing, plain diff facts.

Find-feature upgrade, port 3 of 3. "Track <topic> - tell me when
something new appears." The owner registers a topic; the integration
re-runs the search on its own schedule (ports/schedule territory) and
feeds each RESULT SET here; the module diffs it against the track's
known items and returns PLAIN DIFF FACTS. There is NO card state
machine here: surfacing, open/dismiss decisions, re-raise policy and
delivery belong to ports/patterns, which consumes these facts. One
authoritative decision state, owned by patterns.

Design invariants:

1. DIFF FACTS ONLY: run() returns {"tid", "new": [...]} or None (a
   baseline run returns {"tid", "baseline": True, "seeded": n, "new":
   []}). No cards, no dismissal, no reopening - those are patterns'
   lifecycle. The no-nagging property is structural: a seen URL is in
   the known set forever, so a stale replay produces an EMPTY diff
   forever.
   BASELINE IS EXPLICIT: a track's FIRST accepted run seeds the known
   set without diffing - an initial result set of any size (up to
   MAX_RUN_ITEMS / MAX_KNOWN_ITEMS) is a baseline, never a "diff" and
   never blocked by MAX_DIFF_ITEMS (round-3: no first-run deadlock).
2. EVIDENCE IS VISIBLE: every diff item carries its canonical identity,
   the original url, title and a per-item first_seen timestamp.
3. DETERMINISTIC DIFFING: an item is its canonical url (scheme + port
   preserved); new = not in the track's known set. Same state + same
   results -> same diff (determinism-tested). URL-DISCOVERY SCOPE,
   stated honestly: this detects NEWLY SEEN URL IDENTITIES ONLY - a
   changed title or body at an already-known URL produces no diff.
4. ATOMIC RUNS: the whole result set is validated and diffed first; a
   bad item, an over-bounds run, or a diff that would exceed the
   output cap refuses the WHOLE run with zero effects - earlier valid
   items in the same batch are NOT committed.
5. OWNER-STRICT: tracks, runs and known sets keyed to the owning
   account; a second owner's activity leaves the first byte-identical.
6. IDS NEVER REUSED: monotonic per-owner sequence; remove + add cannot
   overwrite a live track; injected collisions refuse before
   assignment.
7. CAPS REFUSE, NEVER EVICT: MAX_TRACKS per owner, MAX_RUN_ITEMS per
   run, MAX_DIFF_ITEMS per returned diff, MAX_KNOWN_ITEMS per track;
   overflow raises zero-effects.
8. OVERFLOW REJECTS, NEVER TRUNCATES: over-length topic/url/title
   raises; truncation could alias distinct items.
9. CLOCKS + IDS INJECTED; NOT MOUNTED: no timers, no cron, no network,
   no delivery - run() is invoked from outside.
"""

import os
import sys

# Shared URL identity: the corrected canonicalizer lives in
# ports/find/urlcanon.py (single source for both find ports).
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                "..", "find"))
from urlcanon import canonical_url as _shared_canonical_url, UrlCanonError

MAX_TRACKS = 25
MAX_TOPIC = 200
MAX_DIFF_ITEMS = 10  # new items returned per run
MAX_RUN_ITEMS = 50   # result-set size per run
MAX_URL = 500
MAX_TITLE = 200
MAX_KNOWN_ITEMS = 500  # known-set cap per track; refuse-whole, never evict


class TrackError(Exception):
    pass


class TrackLimitReached(TrackError):
    pass


class DiffLimitReached(TrackError):
    pass


class KnownLimitReached(TrackError):
    pass


class NoMatch(TrackError):
    pass


def canonical_url(url):
    try:
        return _shared_canonical_url(url, max_url=MAX_URL, error=TrackError)
    except UrlCanonError as e:
        raise TrackError(str(e))


class Track(object):
    __slots__ = ("tid", "owner", "topic", "created_at", "last_run_at",
                 "seeded", "known")

    def __init__(self, tid, owner, topic, now):
        self.tid = tid
        self.owner = owner
        self.topic = topic
        self.created_at = now
        self.last_run_at = None
        self.seeded = False  # first accepted run is the baseline
        self.known = set()  # canonical urls seen in any accepted run


class MemTrackStore(object):
    def __init__(self):
        self.tracks = {}  # owner -> {tid: Track}
        self.seq = {}     # owner -> monotonic id sequence (never reused)


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
        seq = self.store.seq[owner] = self.store.seq.get(owner, 0) + 1
        tid = self.id_factory("trk", seq, now)
        if tid in tracks:
            self.store.seq[owner] -= 1  # nothing consumed on refusal
            raise TrackError("track id collision: %s" % tid)
        tracks[tid] = Track(tid, owner, topic.strip(), now)
        return tid

    def remove(self, owner, tid):
        tracks = self._tracks(owner)
        if tid not in tracks:
            raise NoMatch("no such track")
        del tracks[tid]

    def run(self, owner, tid, results, now):
        """Diff one result set against the track's known set. results:
        [{"url": str, "title": str?}]. Atomic: over-bounds input, any
        invalid item, or a diff over MAX_DIFF_ITEMS refuses the WHOLE
        run with zero effects. The FIRST accepted run is a baseline:
        it seeds the known set and returns {"tid", "baseline": True,
        "seeded": n, "new": []} - MAX_DIFF_ITEMS never blocks a
        baseline. Later runs return {"tid", "new": [...]} when new
        items were found - plain diff facts for ports/patterns to
        surface - else None."""
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
        if len(t.known) + len(seen - t.known) > MAX_KNOWN_ITEMS:
            raise KnownLimitReached("known-set cap %d reached"
                                    % MAX_KNOWN_ITEMS)
        if not t.seeded:
            # BASELINE: the first accepted run seeds the known set
            # without diffing - a big initial result set is a baseline,
            # never a diff and never blocked by MAX_DIFF_ITEMS
            t.known |= seen
            t.seeded = True
            t.last_run_at = now
            return {"tid": tid, "topic": t.topic, "baseline": True,
                    "seeded": len(items), "new": []}
        new = [it for it in items if it["canonical"] not in t.known]
        if len(new) > MAX_DIFF_ITEMS:
            raise DiffLimitReached("diff over %d items" % MAX_DIFF_ITEMS)
        # commit: whole run becomes known exactly once
        t.known |= seen
        t.last_run_at = now
        if not new:
            return None
        for it in new:
            it["first_seen"] = now
        return {"tid": tid, "topic": t.topic, "new": new}

    def list(self, owner):
        return [{"tid": t.tid, "topic": t.topic, "created_at": t.created_at,
                 "last_run_at": t.last_run_at, "seeded": t.seeded,
                 "known": len(t.known)}
                for t in self._tracks(owner).values()]
