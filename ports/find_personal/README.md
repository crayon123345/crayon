# /ports/find_personal - find across the owner's own records

Find-feature upgrade, port 2 of 3. Generic assistants cannot answer
"what was that link I saved" or "when did I last mention the visa" -
the answers live in the owner's own data. The integration feeds RECORDS
(explicit owner-data entries: notes, journal lines, saved links,
conversation snippets) and this module answers natural-language find
queries with ranked, evidence-carrying matches. Stands alone.

## STATUS: STAGING MODULE - NOT MOUNTED, NOT LIVE

Complete as a tested module (35/35 tests) but NOT wired into the
running app. Named limits, exactly:

1. EVIDENCE IS VISIBLE: every match carries record id, snippet, kind
   and timestamp. No match without a record.
2. DETERMINISTIC RANKING, TEXT EVIDENCE ONLY: the score is pure
   token-overlap over the record TEXT (min 0.20 of query tokens) with
   recency tie-break and stable id order; same input -> same order
   (determinism-tested). kind/source metadata never score as text: a
   metadata-only hit sorts last with a small deterministic score
   strictly below MIN_SCORE (ranked, never outranking text), names its
   matched_fields (["kind"]/["source"]) and quotes NO snippet (probe:
   500/500 metadata-only hits correctly named, no fabricated text
   evidence). No model calls, no fuzzy inference.
3. OWNER-STRICT: records and queries are keyed to the owning account.
   Probe: u1 byte-identical after 100 u2 adds + finds + a u2 wipe.
4. EXPLICIT FEED, FULL WIPE: records exist only because the
   integration fed them; wipe(owner) deletes every record and returns
   the count (owner-scoped, tested).
5. CAPS REFUSE, NEVER EVICT: MAX_RECORDS = 1000 per owner; overflow
   raises with zero effects and the first record is never silently
   dropped (probe: 100 past-cap attempts refused, record 1 intact).
   Injected-id collisions REFUSE before assignment - no silent
   overwrite (probe: 200 constant-id runs, zero record loss). Record
   ids come from a MONOTONIC per-owner sequence: remove + re-add never
   reuses an id, so the documented edit flow cannot collide with a
   surviving record (their exact rec1/rec2/remove/re-add case,
   regression-tested; probe: 0/200 failures). Timestamps must be
   finite numbers; None/str/NaN/inf feeds refuse with zero effects
   (probe: 500/500).
6. OVERFLOW REJECTS, NEVER TRUNCATES: kind (40), text (2000), query
   (500) over bound raise; probe: 500 overflow adds refused, zero
   leaked. Exactly-max accepted (tested).
7. CLOCKS + IDS INJECTED: every time-dependent method takes `now`.
8. READ-ONLY, NOT MOUNTED: matches are returned, never rendered or
   acted on; the integration owns display.

## FEED / PROVENANCE / DELETION ADAPTER CONTRACT (review-required)

Records are COPIES of owner data living in the source systems
(journal entries, saved facts, goals, conversation history). Those
systems own the originals, consent and lifecycle; this module must
never become a shadow store that survives them. The integration
(feeding adapter) MUST:

1. FEED only data the owner can already see in the source system,
   keeping kind/source/ts provenance exact.
2. PROPAGATE EDITS + FORGETS: edits go through replace(owner, rid,
   ...) - ATOMIC: the new content is fully validated BEFORE the old
   record is touched, and any refusal preserves the old record
   untouched (probe: 500/500 failed replacements lose nothing). An
   edit therefore can never leave the record missing. Forgets go
   through remove(owner, rid). A source deletion that does not reach
   this module is an adapter breach - stale copies must be impossible
   by construction.
3. PROPAGATE WIPES: a source-system wipe of the covered data calls
   wipe(owner) here.
4. Never feed this module directly from live sources at query time;
   records enter only through the explicit feed path.

## Integration surface

- `add(owner, kind, text, ts, source="")` -> rid
- `replace(owner, rid, kind, text, ts, source="")` -> True  (ATOMIC edit)
- `remove(owner, rid)` -> bool  (forget propagation)
- `find(owner, query, now, limit=10)` -> [{rid, kind, source, ts,
  score, matched_fields, snippet}]
- `wipe(owner)` -> count removed; `count(owner)`

The integration owns: which owner data becomes records (explicit,
visible), rendering matches (metadata-only matches must be labelled by
their matched_fields), and any grammar ("find my ...").
