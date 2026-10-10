# /ports/find_personal - find across the owner's own records

Find-feature upgrade, port 2 of 3. Generic assistants cannot answer
"what was that link I saved" or "when did I last mention the visa" -
the answers live in the owner's own data. The integration feeds RECORDS
(explicit owner-data entries: notes, journal lines, saved links,
conversation snippets) and this module answers natural-language find
queries with ranked, evidence-carrying matches. Stands alone.

## STATUS: STAGING MODULE - NOT MOUNTED, NOT LIVE

Complete as a tested module (16/16 tests) but NOT wired into the
running app. Named limits, exactly:

1. EVIDENCE IS VISIBLE: every match carries record id, snippet, kind
   and timestamp. No match without a record.
2. DETERMINISTIC RANKING: pure token-overlap score (min 0.20 of query
   tokens present) with recency tie-break and stable id order; same
   records + query -> same ordered matches (determinism-tested). No
   model calls, no fuzzy inference.
3. OWNER-STRICT: records and queries are keyed to the owning account.
   Probe: u1 byte-identical after 100 u2 adds + finds + a u2 wipe.
4. EXPLICIT FEED, FULL WIPE: records exist only because the
   integration fed them; wipe(owner) deletes every record and returns
   the count (owner-scoped, tested).
5. CAPS REFUSE, NEVER EVICT: MAX_RECORDS = 1000 per owner; overflow
   raises with zero effects and the first record is never silently
   dropped (probe: 100 past-cap attempts refused, record 1 intact).
6. OVERFLOW REJECTS, NEVER TRUNCATES: kind (40), text (2000), query
   (500) over bound raise; probe: 500 overflow adds refused, zero
   leaked. Exactly-max accepted (tested).
7. CLOCKS + IDS INJECTED: every time-dependent method takes `now`.
8. READ-ONLY, NOT MOUNTED: matches are returned, never rendered or
   acted on; the integration owns display.

## Integration surface

- `add(owner, kind, text, ts, source="")` -> rid
- `find(owner, query, now, limit=10)` -> [{rid, kind, source, ts, score, snippet}]
- `wipe(owner)` -> count removed; `count(owner)`

The integration owns: which owner data becomes records (explicit,
visible), rendering matches, and any grammar ("find my ...").
