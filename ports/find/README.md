# /ports/find - find core: planned queries, verified citations, sources panel

Find-feature upgrade, port 1 of 3. User research on AI search products
(ChatGPT search, Perplexity, Gemini) shows the loudest complaints are
wrong or invented citations, single-search shallowness, missing source
dates, and answers that forget the previous question. This module owns
the DECISIONS around a web find; searching and fetching are
integration-injected callables - it never touches the network.

## REUSE BOUNDARY (not a parallel stack)

The running app already owns: research/comparison fanout
(cr_web.research), the production fetched-page claim/citation gate
(cr_citations.checked_answer), dated news search (cr_web.news), and
conversation context (cr_memory). This module adds ONLY what those do
not have: a validated planning API, RECORDED citation drops, the
canonical sources panel, explicit owner-local recency windows, and
ordinal follow-up references. The citation support check is injectable
(`support_fn`); production wiring must use the existing checked_answer
gate - the shipped `reference_support` is a hardened stdlib reference
for source-only testing. No searching, fetching, news handling or
conversation memory lives here. URL identity comes from the shared
ports/find/urlcanon.py (also imported by ports/find_track).

## STATUS: STAGING MODULE - NOT MOUNTED, NOT LIVE

Complete as a tested module (44/44 tests) but NOT wired into the
running app. Named limits, exactly:

1. VERIFIED CITATIONS ONLY: a citation survives only when its URL is
   fetched AND the content supports the claim. Drops are recorded with
   reason (bad_url / fetch_failed / empty_content / unsupported /
   support_error) - never presented, never retried, never substituted.
   Probes: 1000/1000 fabricated disjoint-token citations dropped; 500/
   500 contradiction + numeric near-miss citations dropped.
2. SUPPORT IS MORE THAN SHARED TOKENS: the reference gate scores the
   best sentence-window, requires every numeric token in the claim in
   that window, and requires exact negation parity - "drug cures
   cancer" vs "drug does not cure cancer" and "999 million" vs
   "10 million" both refuse (tested both directions).
3. ATOMIC VERIFICATION: verified/dropped/sources/session are computed
   and validated BEFORE any commit; a source-cap violation refuses the
   whole batch leaving prior plan state and the session byte-identical
   (regression-tested; probe: 200/200 refused, identical every time).
4. DETERMINISTIC PLANNING: fallback decomposer REFUSES questions that
   decompose past 4 subqueries (no silent subject drops - "alpha;
   beta; gamma; delta; epsilon" refuses, tested); injected planner
   output validated identically.
5. RECENCY IS EXPLICIT + OWNER-LOCAL: today/yesterday are owner-local
   days via injected tz offset (UTC default); this/last week are ISO
   Monday weeks; this/last month are calendar months; past week/month
   are rolling 7/30 days; latest/recent/news is rolling 7 days. Each
   distinct, boundary-tested.
6. URL IDENTITY PRESERVES SCHEME + PORT: http://a.example:8080/x and
   https://a.example:9090/x are DIFFERENT resources (tested); default
   ports fold; www/fragments/tracking params/trailing slashes strip.
   Redirect/canonical equivalence is the integration's to resolve
   before feeding urls.
7. OWNER-STRICT SESSIONS: first..tenth/last follow-ups resolve only
   against the owner's own last verified sources; session reads return
   deep snapshots (caller mutation cannot corrupt state, tested).
8. CAPS REFUSE, NEVER EVICT; OVERFLOW REJECTS, NEVER TRUNCATES:
   plans 50/owner, claims 20/batch, sources 10/panel; query 500,
   subquery 500, claim 1000, url 500; exactly-max accepted.
9. READ-ONLY, NOT MOUNTED: no action paths, no network.

## Integration surface

- `plan(owner, query, now, decompose_fn=None, tz_offset_minutes=0)`
- `verify(owner, pid, claims, fetch_fn, now, support_fn=None)` -
  production: support_fn = checked_answer-backed gate
- `resolve_reference(owner, text)`, `sources(owner, pid)`,
  `session(owner)`, `export(owner)`

The integration owns: running subqueries (cr_web.search/research),
fetching pages, rendering verified answers with the sources panel, and
follow-up grammar. Claims it cannot fetch MUST NOT render as cited.
