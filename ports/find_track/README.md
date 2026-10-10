# /ports/find_track - tracked topics: change detection, proactive cards

Find-feature upgrade, port 3 of 3. "Track <topic> - tell me when it
changes." The owner registers a topic; the integration re-runs the
search on its own schedule and feeds each RESULT SET here; the module
diffs it against the track's known items and raises a reviewable card
when something new appears. Cards are suggestions with evidence - they
never act. Stands alone.

## STATUS: STAGING MODULE - NOT MOUNTED, NOT LIVE

Complete as a tested module (20/20 tests) but NOT wired into the
running app. Named limits, exactly:

1. READ-ONLY BY CONSTRUCTION: no code path from a card to any action.
2. EVIDENCE IS VISIBLE: every card carries the new items (urls +
   titles) and when they were first seen. No card without new items.
3. DETERMINISTIC DIFFING: an item is its canonical url; new = not in
   the track's known set. Same state + same results -> same diff
   (determinism-tested).
4. ATOMIC RUNS: the whole result set is validated and diffed first; a
   bad item or an over-bounds run refuses the WHOLE run with zero
   effects - earlier valid items in the same batch are NOT committed
   (tested). Probe: 500 malformed runs refused, known-set untouched.
5. NO NAGGING: one open card per track; new findings extend the open
   card's evidence. A dismissed card re-raises only on items not in
   the known set - dismissed evidence is known, so stale replays never
   re-raise (probe: 0/300 re-raised; a genuinely fresh item does).
6. OWNER-STRICT: tracks, runs and cards keyed to the owning account;
   probe: u1 byte-identical after 100 u2 operations.
7. CAPS REFUSE, NEVER EVICT: MAX_TRACKS = 25 per owner,
   MAX_RUN_ITEMS = 50 per run, MAX_ITEMS = 10 evidence per card; a run
   that would exceed the card's evidence cap refuses whole, leaving
   the card and known-set exact (probe: 50/50 refused, known = 10).
8. OVERFLOW REJECTS, NEVER TRUNCATES: topic (200), url (500), title
   (200) over bound raise; exactly-max accepted (tested).
9. CLOCKS + IDS INJECTED; NOT MOUNTED, NO DELIVERY: the integration
   owns scheduling, searching and rendering.

## Integration surface

- `add(owner, topic, now)` -> tid; `remove(owner, tid)`; `list(owner)`
- `run(owner, tid, results, now)` -> card | None  (results: [{"url", "title"?}])
- `dismiss(owner, tid, now)`; `card(owner, tid)`

The integration owns: the re-search schedule (ports/schedule
territory), running the search, and delivering cards (ports/patterns
territory for proactive surfacing).
