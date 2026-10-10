"""ports/find_personal - find across the owner's own records.

Find-feature upgrade, port 2 of 3. Generic assistants cannot answer
"what was that link I saved" or "when did I last talk about the visa" -
the answers live in the owner's own data. The integration feeds RECORDS
(explicit owner-data entries: notes, journal lines, saved links,
conversation snippets) and this module answers natural-language find
queries over them with ranked, evidence-carrying matches. Stands alone.

Design invariants:

1. EVIDENCE IS VISIBLE: every match carries the record id, a snippet,
   the record kind and its timestamp. No match without a record.
2. DETERMINISTIC RANKING: pure token-overlap score with a recency
   tie-break; same records + same query -> same ordered matches
   (determinism-tested). No model calls, no fuzzy inference.
3. OWNER-STRICT: records and queries are keyed to the owning account;
   a second owner's records and wipe leave the first byte-identical.
4. EXPLICIT FEED, FULL WIPE: records exist only because the
   integration fed them; wipe() deletes every record the owner has.
5. CAPS REFUSE, NEVER EVICT: MAX_RECORDS per owner; overflow raises
   with zero effects - history is never silently dropped.
6. OVERFLOW REJECTS, NEVER TRUNCATES: over-length kind/text/query
   raises; truncation could alias distinct records.
7. CLOCKS + IDS INJECTED: every time-dependent method takes `now`.
8. READ-ONLY, NOT MOUNTED: matches are returned, never rendered or
   acted on; the integration owns display.
"""

import re

MAX_RECORDS = 1000
MAX_KIND = 40
MAX_TEXT = 2000
MAX_QUERY = 500
MAX_SNIPPET = 240
MAX_RESULTS = 10
MIN_SCORE = 0.20  # minimum fraction of query tokens present in a record

_STOP = frozenset("the a an and or of to in on for with is are was were "
                  "be been it its this that these those what who when "
                  "where which how why did do does can could should "
                  "would about from by at as i me my we our you your".split())


class FindPersonalError(Exception):
    pass


class RecordLimitReached(FindPersonalError):
    pass


class NoRecord(FindPersonalError):
    pass


def _check_owner(owner):
    if not owner or not isinstance(owner, str):
        raise FindPersonalError("owner required")


def _check_rid(rid):
    if not rid or not isinstance(rid, str):
        raise FindPersonalError("rid required")


def _tok(text):
    return [w for w in re.findall(r"[a-z0-9]+", (text or "").lower())
            if w not in _STOP and len(w) > 1]


def _snippet(text, terms, width=MAX_SNIPPET):
    """First window containing a query term, ellipsized. Deterministic."""
    low = text.lower()
    pos = min((low.find(t) for t in terms if low.find(t) >= 0),
              default=0)
    start = max(0, pos - 40)
    out = text[start:start + width].strip()
    if start > 0:
        out = "..." + out
    if start + width < len(text):
        out += "..."
    return out


class Record(object):
    __slots__ = ("rid", "owner", "kind", "text", "source", "ts",
                 "text_tokens", "meta_tokens")

    def __init__(self, rid, owner, kind, text, source, ts):
        self.rid = rid
        self.owner = owner
        self.kind = kind
        self.text = text
        self.source = source
        self.ts = ts
        # ranking reads TEXT tokens only; kind/source are metadata and
        # are reported as matched_fields, never scored as text evidence
        self.text_tokens = set(_tok(text))
        self.meta_tokens = {"kind": set(_tok(kind)), "source": set(_tok(source))}


class MemPersonalStore(object):
    def __init__(self):
        self.records = {}  # owner -> {rid: Record}
        self.seq = {}      # owner -> monotonic id sequence (never reused)


class FindPersonal(object):
    def __init__(self, store, id_factory=None):
        self.store = store
        self.id_factory = id_factory or (lambda p, s, n: "%s%d" % (p, s))

    def _records(self, owner):
        # write path only (add) - creates the owner's dict
        return self.store.records.setdefault(owner, {})

    def _records_read(self, owner):
        # read path (find/replace/count) - never creates state
        return self.store.records.get(owner) or {}

    def add(self, owner, kind, text, ts, source=""):
        """Feed one record. Overflow rejects; cap refuses zero-effects.
        A throwing id_factory changes NOTHING - it runs before any
        store mutation, and the sequence advances only on commit."""
        _check_owner(owner)
        if not isinstance(kind, str) or not kind.strip():
            raise FindPersonalError("kind required")
        if len(kind) > MAX_KIND:
            raise FindPersonalError("kind over %d chars" % MAX_KIND)
        if not isinstance(text, str) or not text.strip():
            raise FindPersonalError("text required")
        if len(text) > MAX_TEXT:
            raise FindPersonalError("text over %d chars" % MAX_TEXT)
        if not isinstance(source, str):
            raise FindPersonalError("source must be str")
        if isinstance(ts, bool) or not isinstance(ts, (int, float)) \
                or ts != ts or ts in (float("inf"), float("-inf")):
            raise FindPersonalError("ts must be a finite number")
        recs = self.store.records.get(owner)
        if recs is not None and len(recs) >= MAX_RECORDS:
            raise RecordLimitReached("record cap %d reached" % MAX_RECORDS)
        next_seq = self.store.seq.get(owner, 0) + 1
        rid = self.id_factory("rec", next_seq, ts)  # may throw: zero state
        if recs is not None and rid in recs:
            raise FindPersonalError("record id collision: %s" % rid)
        if recs is None:
            recs = self.store.records[owner] = {}
        self.store.seq[owner] = next_seq  # sequence advances on commit only
        recs[rid] = Record(rid, owner, kind.strip(), text.strip(),
                           source.strip(), ts)
        return rid

    def find(self, owner, query, now, limit=MAX_RESULTS):
        """Rank records against the query. Returns evidence-carrying
        matches, best first; empty list when nothing clears MIN_SCORE.
        `now` is the injected clock (invariant 7), validated here so a
        future recency weighting cannot receive a bogus one."""
        _check_owner(owner)
        if isinstance(now, bool) or not isinstance(now, (int, float)) \
                or now != now or now in (float("inf"), float("-inf")):
            raise FindPersonalError("now must be a finite number")
        if not isinstance(query, str) or not query.strip():
            raise FindPersonalError("query required")
        if len(query) > MAX_QUERY:
            raise FindPersonalError("query over %d chars" % MAX_QUERY)
        if isinstance(limit, bool) or not isinstance(limit, int):
            raise FindPersonalError("limit must be an int")
        if limit < 1 or limit > MAX_RESULTS:
            raise FindPersonalError("limit must be 1..%d" % MAX_RESULTS)
        terms = set(_tok(query))
        if not terms:
            return []
        scored = []
        meta_only = []
        for rec in self._records_read(owner).values():
            hit = len(terms & rec.text_tokens)
            matched = [fld for fld in ("kind", "source")
                       if terms & rec.meta_tokens[fld]]
            if hit:
                matched.insert(0, "text")
            score = hit / float(len(terms)) if hit else 0.0
            if hit and score >= MIN_SCORE:
                scored.append((score, rec.ts, rec.rid, rec, matched))
            elif matched:
                # metadata-only hit: no text evidence - a small
                # deterministic score strictly below MIN_SCORE, so it
                # ranks but never outranks a text hit
                meta_hit = len(terms & (rec.meta_tokens["kind"]
                                        | rec.meta_tokens["source"]))
                # ratio capped at 1.0 by the union -> strictly below
                mscore = (meta_hit / float(len(terms))) * MIN_SCORE * 0.5
                meta_only.append((mscore, rec.ts, rec.rid, rec, matched))
        # score desc, ts desc (recency tie-break), rid asc (stable)
        scored.sort(key=lambda s: (-s[0], -s[1], s[2]))
        meta_only.sort(key=lambda s: (-s[0], -s[1], s[2]))
        out = [{"rid": rec.rid, "kind": rec.kind, "source": rec.source,
                "ts": rec.ts, "score": round(score, 4),
                "matched_fields": matched,
                "snippet": _snippet(rec.text, terms)}
               for score, _ts, _rid, rec, matched in scored]
        out += [{"rid": rec.rid, "kind": rec.kind, "source": rec.source,
                 "ts": rec.ts, "score": round(mscore, 4),
                 "matched_fields": matched, "snippet": None}
                for mscore, _ts, _rid, rec, matched in meta_only]
        return out[:limit]

    def remove(self, owner, rid):
        """Delete one record by id (source-forget propagation).
        Returns True when a record was removed, False when absent."""
        _check_owner(owner)
        _check_rid(rid)
        recs = self.store.records.get(owner)
        if not recs or rid not in recs:
            return False
        del recs[rid]
        return True

    def replace(self, owner, rid, kind, text, ts, source=""):
        """ATOMIC edit (source-edit propagation): validate the new
        content fully BEFORE touching the old record; on success swap
        in place under the SAME rid; on any refusal the old record is
        preserved untouched. Returns True. Raises NoRecord when rid is
        absent - use add() for new records."""
        _check_owner(owner)
        _check_rid(rid)
        recs = self._records_read(owner)
        if rid not in recs:
            raise NoRecord("no such record")
        # full validation first - the old record survives any failure
        if not isinstance(kind, str) or not kind.strip():
            raise FindPersonalError("kind required")
        if len(kind) > MAX_KIND:
            raise FindPersonalError("kind over %d chars" % MAX_KIND)
        if not isinstance(text, str) or not text.strip():
            raise FindPersonalError("text required")
        if len(text) > MAX_TEXT:
            raise FindPersonalError("text over %d chars" % MAX_TEXT)
        if not isinstance(source, str):
            raise FindPersonalError("source must be str")
        if isinstance(ts, bool) or not isinstance(ts, (int, float)) \
                or ts != ts or ts in (float("inf"), float("-inf")):
            raise FindPersonalError("ts must be a finite number")
        old = recs[rid]
        recs[rid] = Record(rid, owner, kind.strip(), text.strip(),
                           source.strip(), ts)
        return True

    def wipe(self, owner):
        """Delete every record the owner has. Returns the count removed."""
        _check_owner(owner)
        recs = self.store.records.get(owner)
        if not recs:
            return 0
        n = len(recs)
        recs.clear()
        return n

    def count(self, owner):
        _check_owner(owner)
        return len(self._records_read(owner))
