# /ports/find_personal_semantic - embedding recall + rerank adapter for ports/find_personal

The deterministic token-overlap matcher in ports/find_personal misses
paraphrase recall ("visa appointment" vs a record saying "Schengen
consulate paperwork"). This adapter adds a GUARDED semantic path through
pretrained models: embeddings RECALL extra candidate records and a
cross-encoder RERANKER may reorder the candidate set. The deterministic
result is the base and is never weakened.

## REUSE BOUNDARY (not a parallel stack)

ports/find_personal owns records, deterministic matching, snippets and
result shape - this adapter imports its internals (same cross-port
pattern as ports/find_track importing ports/find/urlcanon.py) and adds
only vector similarity and merge ordering. ports/models owns the
provider seam; every model call goes through models.guarded_call.
Candidate models (licenses verified against live HF model cards
2026-10-11): BAAI/bge-small-en-v1.5 (MIT) for embeddings,
cross-encoder/ms-marco-MiniLM-L6-v2 (Apache-2.0) for reranking. This
module ships no weights, downloads nothing, touches no network.

## PRIVACY BOUNDARY

With provider "hosted", record text and queries leave the machine.
Wiring hosted embed/rerank for an owner's personal records requires the
private-hosted consent boundary FIRST - an integration and owner
decision this module cannot grant. The default provider ("none") keeps
everything deterministic and local. FAIL-CLOSED CONSENT GATE (round-9):
semantic_find's consent argument defaults to False and only the exact
value True enables provider processing - without it the provider is
never called and the result is exactly the deterministic base
(regression-tested: default/False/1/"yes"/None all make zero provider
calls).

## STATUS: STAGING MODULE - NOT MOUNTED, NOT LIVE

Complete as a tested module (26/26 tests, 10/10 repeat runs stable) but
NOT wired into the running app. Named limits, exactly:

1. DETERMINISTIC BASE PRESERVED: every deterministic match stays in the
   result with its original fields, annotated retrieval="deterministic";
   semantic recall only ADDS records (retrieval="semantic"). With no
   working model the matches are exactly FindPersonal.find's with that
   annotation.
2. NO FABRICATED MATCHES: additions are built only from the owner's
   actual stored records with rid/kind/source/ts/snippet. Owner-strict:
   only the querying owner's records are embedded (probe: a second
   owner's records never reach the embed call).
3. FAIL-CLOSED ON MODEL FAILURE: exception, timeout, open breaker,
   disabled/unavailable capability, a backend-raised ModelsError
   (converted to a backend failure like find_support_nli does -
   round-9, never crashes the find), or out-of-contract responses
   (wrong vector/score count, ragged/non-numeric/non-finite/boolean
   values, zero vectors match nothing) all leave the deterministic
   answer unchanged. One batched embed call and one batched rerank call per
   query at most; no retries.
4. BOUNDED MODEL USE: at most MAX_EMBED_RECORDS (200) most recent
   records (ts desc, rid asc) are embedded per query (+1 query vector);
   at most MAX_RERANK (50) candidates are reranked. Records beyond the
   embed cap get no semantic score; the deterministic path still covers
   them.
5. SELECT FIRST, THEN RERANK INSIDE THE WINDOW (round-9 fix): the
   result limit is applied BEFORE reranking. Deterministic matches
   lead the union, so they are always inside the selected window and
   NO rerank order can push one out of the result (regression-tested:
   10 base matches at the limit + a top-ranked semantic addition -
   all 10 survive). The reranker reorders only within the selected
   window; it may order semantic matches above deterministic ones
   (that is its purpose); ties keep the pre-rerank order. Single-
   candidate windows skip the rerank call entirely.
6. DETERMINISTIC TIE-BREAKS: similarity/score ties order by ts desc,
   rid asc. sim_threshold (default 0.55, boundary inclusive) is a
   placeholder to be TUNED by the benchmark harness with measured data -
   no quality claim is made for it yet.
7. NO STATE: nothing cached; record reads use the store's read path;
   breaker state is caller-owned.
8. READ-ONLY, NOT MOUNTED: no network, no downloads, no action paths.
   RSS/memory-fit claims for any real model must be MEASURED by the
   integration wiring before any fit claim is made.

## Files

- find_personal_semantic.py - semantic_find(engine, config, breaker,
  provider, owner, query, now, limit=None, sim_threshold=0.55).
- test_find_personal_semantic.py - 21 tests: wiring validation,
  deterministic base preservation/annotation, semantic recall +
  threshold boundary, no-duplication of deterministic hits, recall
  ordering, owner-strictness, embed/rerank failure + bad-shape probes,
  rerank reorder/membership/skip probes, embed cap + recency preference,
  limit trim.
