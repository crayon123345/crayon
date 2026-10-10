# /ports/find - find core: planned queries, verified citations, sources panel

Find-feature upgrade. User research on AI search products (ChatGPT
search, Perplexity, Gemini) shows the loudest complaints are: wrong or
invented citations, single-search shallowness, missing source dates,
and answers that forget the previous question. This module owns the
DECISIONS around a web find; searching and fetching are
integration-injected callables - it never touches the network.

## STATUS: STAGING MODULE - NOT MOUNTED, NOT LIVE

Complete as a tested module (28/28 tests) but NOT wired into the
running app. Named limits, exactly:

1. VERIFIED CITATIONS ONLY: a citation survives only when its URL is
   fetched through the injected fetcher AND the fetched content
   supports the claim (token-overlap >= 0.34 of claim tokens).
   Unfetchable or unsupported citations are DROPPED and recorded with
   their reason - never presented. (Probe: 1000/1000 fabricated
   citations dropped, zero presented.)
2. UNKNOWN REFUSES WITHOUT RETRY: a fetch error drops that citation
   (probe: 20/20 dropped, exactly 20 fetch calls - no retries) and the
   module never substitutes a different URL.
3. DETERMINISTIC PLANNING: the fallback decomposer is pure
   (compare/X-and-Y splitting, dedup, cap 4). An injected planner's
   output is validated - over 4 subqueries, empty entries, or
   over-length entries REFUSE the whole plan with zero effects.
4. RECENCY IS EXPLICIT: today/yesterday/this week/this month/
   latest/in <year> resolve to concrete [after, before) windows
   against the injected clock, carried on the plan so the integration
   can bound its search.
5. OWNER-STRICT SESSIONS: follow-up references (first..tenth, last)
   resolve only against the owner's own last verified sources. Probe:
   u1 byte-identical after 200 u2 operations (150 cap-refusals); u2
   sees no session.
6. CAPS REFUSE, NEVER EVICT: MAX_PLANS_PER_OWNER = 50, MAX_CLAIMS = 20
   per batch, MAX_SOURCES = 10 per panel - overflow raises with zero
   effects (over-cap claim batches leave the plan untouched; tested).
7. OVERFLOW REJECTS, NEVER TRUNCATES: query (500), subquery (500),
   claim text (1000), url (500) over bound raise; truncation could
   alias distinct requests. Exactly-max boundary accepted (tested).
8. URL DEDUP IS CANONICAL: lowercase scheme+host, www stripped,
   fragments and tracking params (utm_*, gclid, fbclid, ...) removed,
   trailing slashes folded - two citations of one page show once.
9. READ-ONLY, NOT MOUNTED: no action paths. The integration runs
   searches/fetches and renders; this module owns state and decisions.

## Integration surface (for the /integrated wiring)

- `plan(owner, query, now, decompose_fn=None)` -> Plan(subqueries, window)
- `verify(owner, pid, claims, fetch_fn, now)` -> {verified, dropped, sources}
- `resolve_reference(owner, text)` -> {"index", "source"} | None
- `sources(owner, pid)` / `session(owner)` / `export(owner)`

The integration owns: running the subqueries against its search
providers, fetching pages, rendering verified answers with their
sources panel (titles, URLs, published dates), and any follow-up
grammar. Claims it cannot fetch MUST NOT be rendered as cited.
