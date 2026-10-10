# /ports/find_track - tracked topics: known-set diffing, plain diff facts

Find-feature upgrade, port 3 of 3. "Track <topic> - tell me when
something new appears." The owner registers a topic; the integration
re-runs the search on its own schedule and feeds each RESULT SET here;
the module diffs it against the track's known items and returns PLAIN
DIFF FACTS. Stands alone.

## SINGLE LIFECYCLE BOUNDARY (round-2 rework)

This port owns ONLY the track registry and known-set diffing. There is
NO card state machine here: surfacing, open/dismiss/snooze decisions,
re-raise policy and delivery belong to ports/patterns, which consumes
these diff facts; recurring runs and check-ins belong to
ports/schedule (cr_tools.schedule_task). One authoritative decision
state, owned by patterns. The no-nagging property is structural, not a
second lifecycle: a seen URL is in the known set forever, so a stale
replay produces an EMPTY diff forever (probe: 300/300 no re-report; a
genuinely fresh URL produces one).

## STATUS: STAGING MODULE - NOT MOUNTED, NOT LIVE

Complete as a tested module (25/25 tests) but NOT wired into the
running app. Named limits, exactly:

1. DIFF FACTS ONLY: run() returns {"tid", "topic", "new": [...]} or
   None. No cards, no dismissal, no reopening (module surface probe:
   no dismiss/card methods exist).
2. EVIDENCE IS VISIBLE: every diff item carries canonical identity,
   original url, title and a per-item first_seen timestamp.
3. DETERMINISTIC DIFFING, URL-DISCOVERY SCOPE: an item is its
   canonical url (scheme + port preserved - http://a.example:8080/x
   and https://a.example:9090/x are DIFFERENT items, tested; IPv6
   brackets preserved). This detects NEWLY SEEN URL IDENTITIES ONLY:
   a changed title or body at an already-known URL produces no diff
   (stated limit). Same state + same results -> same diff
   (determinism-tested).
4. ATOMIC RUNS: a bad item, an over-bounds run, or a diff over
   MAX_DIFF_ITEMS refuses the WHOLE run with zero effects - earlier
   valid items in the batch are NOT committed (tested; probe: 500/500
   mixed batches refused, known-set still empty).
5. OWNER-STRICT: probe - u1 byte-identical after 100 u2 ops.
6. IDS NEVER REUSED: monotonic per-owner sequence; remove + add cannot
   overwrite a live track (probe: 200 cycles, zero loss); injected
   collisions refuse before assignment (tested).
7. CAPS REFUSE, NEVER EVICT: MAX_TRACKS = 25, MAX_RUN_ITEMS = 50,
   MAX_DIFF_ITEMS = 10 (probe: 50/50 over-cap diffs refused whole).
8. OVERFLOW REJECTS, NEVER TRUNCATES: topic 200 / url 500 / title 200;
   malformed ports raise the declared error (tested).
9. CLOCKS + IDS INJECTED; NOT MOUNTED: no timers, no cron, no network,
   no delivery - run() is invoked from outside.

## Integration surface

- `add(owner, topic, now)` -> tid; `remove(owner, tid)`; `list(owner)`
- `run(owner, tid, results, now)` -> {"tid", "topic", "new": [...]} | None

The integration owns: the re-search schedule (ports/schedule), running
the search, and mapping diff facts into ports/patterns cards
(surfacing + dismissal + delivery are patterns' lifecycle).
URL identity comes from ports/find/urlcanon.py - the single corrected
canonicalizer shared with ports/find.
