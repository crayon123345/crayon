"""ports/find_personal_semantic - embedding recall + rerank adapter for
ports/find_personal.

The deterministic token-overlap matcher in ports/find_personal misses
paraphrase recall ("visa" vs a record saying "Schengen appointment").
This adapter adds a GUARDED semantic path through pretrained embedding
and reranker models: embeddings RECALL extra candidate records and a
cross-encoder RERANKER may reorder the candidate set. The deterministic
result is the base and is never weakened: semantic recall can only ADD
real records, never drop, fabricate or alter one, and every model
failure returns exactly the deterministic answer.

REUSE BOUNDARY (not a parallel stack): ports/find_personal owns
records, deterministic matching, snippets and result shape - this
adapter imports its internals (same cross-port pattern as
ports/find_track importing ports/find/urlcanon.py) and adds no matching
logic of its own beyond vector similarity and merge ordering.
ports/models owns the provider seam; every model call goes through
models.guarded_call. The models themselves (candidate: BAAI/
bge-small-en-v1.5, MIT, for embeddings; cross-encoder/
ms-marco-MiniLM-L6-v2, Apache-2.0, for reranking) are
integration-injected callables - this module ships no weights,
downloads nothing and touches no network.

PRIVACY BOUNDARY: with provider "hosted", record text and queries leave
the machine. Wiring hosted embed/rerank for an owner's personal records
requires the private-hosted consent boundary FIRST - that is an
integration and owner decision, not something this module can grant.
The default provider ("none") keeps everything deterministic and local.

Design invariants:

1. DETERMINISTIC BASE PRESERVED: every deterministic match stays in the
   result with its original fields; semantic recall only ADDS records.
   With no working model the matches are exactly FindPersonal.find's,
   each annotated retrieval="deterministic".
2. NO FABRICATED MATCHES: semantic additions are built only from the
   owner's actual stored records, carrying rid/kind/source/ts/snippet
   like any match, annotated retrieval="semantic" (deterministic
   matches carry "deterministic"; reranked order changes no content).
3. FAIL-CLOSED ON MODEL FAILURE: timeout, exception, open breaker,
   disabled/unavailable capability or an out-of-contract response
   (wrong vector count, ragged/non-numeric/non-finite dimensions,
   wrong score count) yields no semantic change. One batched embed
   call and one batched rerank call per query at most; no retries.
4. BOUNDED MODEL USE: at most MAX_EMBED_RECORDS most recent records
   (ts desc, rid asc) are embedded per query and MAX_RERANK candidates
   are reranked; records beyond the embed cap get no semantic score
   (fail-closed direction - the deterministic path still covers them).
5. NO STATE: the adapter caches nothing and creates no state; record
   reads use the store's read path only; breaker state is caller-owned.
8. FAIL-CLOSED CONSENT GATE: semantic_find's consent argument defaults
   to False; only the exact value True enables provider processing of
   personal records - without it the provider is never called and the
   result is exactly the deterministic base (round-9). Hosted wiring
   additionally requires the platform consent boundary (above).
9. SELECT FIRST, THEN RERANK INSIDE THE WINDOW: the result limit is
   applied before reranking; deterministic matches lead the union, so
   no rerank order can push a deterministic match out of the result
   (round-9).
6. DETERMINISTIC TIE-BREAKS: equal similarity or rerank scores order
   by ts desc then rid asc; same inputs + same model output -> same
   ordering.
7. READ-ONLY, NOT MOUNTED: no network, no downloads, no action paths.
"""

import math
import os
import sys

# Records, deterministic matching and result shape are owned by
# ports/find_personal; the provider seam is owned by ports/models.
_HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(_HERE, "..", "find_personal"))
sys.path.insert(0, os.path.join(_HERE, "..", "models"))

from find_personal import (FindPersonal, FindPersonalError, MAX_RESULTS,
                           _snippet)
from models import (ModelsError, CapabilityUnavailable, ProviderConfig,
                    CircuitBreaker, guarded_call)

MAX_EMBED_RECORDS = 200
MAX_RERANK = 50
DEFAULT_SIM_THRESHOLD = 0.55  # to be tuned by the benchmark harness


class FindPersonalSemanticError(ModelsError):
    """Adapter wiring error: invalid arguments to semantic_find."""


def _cosine(a, b):
    dot = 0.0
    na = 0.0
    nb = 0.0
    for x, y in zip(a, b):
        dot += x * y
        na += x * x
        nb += y * y
    if na == 0.0 or nb == 0.0:
        return 0.0  # a zero vector carries no direction: no match
    return dot / (math.sqrt(na) * math.sqrt(nb))


def _call_backend(fn):
    """Run one provider call. A backend must signal failures with
    ordinary exceptions or CapabilityUnavailable; a ModelsError escaping
    the backend is treated as a backend failure (drop path), never as a
    caller bug that would crash the querying find (round-9, review
    5480642386: same conversion as find_support_nli)."""
    try:
        return fn()
    except CapabilityUnavailable:
        raise
    except ModelsError as exc:
        raise CapabilityUnavailable("backend raised ModelsError: %s" % exc)


def _check_vectors(raw, expected):
    if not isinstance(raw, (list, tuple)) or len(raw) != expected:
        raise CapabilityUnavailable("embed response vector count mismatch")
    out = []
    dim = None
    for vec in raw:
        if not isinstance(vec, (list, tuple)) or not vec:
            raise CapabilityUnavailable("embed vector must be a non-empty list")
        vals = []
        for v in vec:
            if isinstance(v, bool) or not isinstance(v, (int, float)) \
                    or v != v or v in (float("inf"), float("-inf")):
                raise CapabilityUnavailable("embed vector has non-finite value")
            vals.append(float(v))
        if dim is None:
            dim = len(vals)
        elif len(vals) != dim:
            raise CapabilityUnavailable("embed vectors are ragged")
        out.append(vals)
    return out


def _check_scores(raw, expected):
    if not isinstance(raw, (list, tuple)) or len(raw) != expected:
        raise CapabilityUnavailable("rerank response score count mismatch")
    out = []
    for s in raw:
        if isinstance(s, bool) or not isinstance(s, (int, float)) \
                or s != s or s in (float("inf"), float("-inf")):
            raise CapabilityUnavailable("rerank score must be finite")
        out.append(float(s))
    return out


def _is_num(v):
    return (isinstance(v, (int, float)) and not isinstance(v, bool)
            and v == v and v not in (float("inf"), float("-inf")))


def semantic_find(engine, config, breaker, provider, owner, query, now,
                  limit=None, sim_threshold=DEFAULT_SIM_THRESHOLD,
                  consent=False):
    """FindPersonal.find plus guarded semantic recall and rerank.

    engine: a FindPersonal instance. config/breaker/provider: the
    models.ProviderConfig, caller-owned CircuitBreaker and a provider
    exposing capabilities(), embed() and rerank(). owner/query/now/limit
    are validated by FindPersonal.find itself. sim_threshold: cosine
    minimum in [0, 1] for a semantic addition (default
    DEFAULT_SIM_THRESHOLD, to be tuned by measured benchmarks).
    consent: the owner's consent for their personal records to be
    processed by the configured provider. FAIL-CLOSED default False:
    without it the provider is never called and the result is exactly
    the deterministic base. Hosted wiring additionally requires the
    platform consent boundary (module docstring)."""
    if not isinstance(engine, FindPersonal):
        raise FindPersonalSemanticError("engine must be a FindPersonal")
    if not isinstance(config, ProviderConfig):
        raise FindPersonalSemanticError("config must be a ProviderConfig")
    if not isinstance(breaker, CircuitBreaker):
        raise FindPersonalSemanticError("breaker must be a CircuitBreaker")
    if not callable(getattr(provider, "capabilities", None)):
        raise FindPersonalSemanticError("provider must expose capabilities()")
    if not _is_num(sim_threshold) or not (0.0 <= float(sim_threshold) <= 1.0):
        raise FindPersonalSemanticError("sim_threshold must be in [0, 1]")
    sim_threshold = float(sim_threshold)
    kwargs = {} if limit is None else {"limit": limit}
    effective_limit = kwargs.get("limit", MAX_RESULTS)

    base = engine.find(owner, query, now, **kwargs)
    base_by_rid = {m["rid"]: m for m in base}
    for m in base:
        m["retrieval"] = "deterministic"

    # Fail-closed consent gate (round-9 review patch): no consent ->
    # the provider never sees a personal record.
    if consent is not True:
        return base

    # Read path only - never creates state. Owner-strict: only this
    # owner's records are candidates.
    records = engine._records_read(owner)
    if not records:
        return base

    # --- semantic recall -------------------------------------------------
    additions = []
    if "embed" in config.enabled:
        # most recent MAX_EMBED_RECORDS records (ts desc, rid asc)
        recs = sorted(records.values(), key=lambda r: (-r.ts, r.rid))
        recs = recs[:MAX_EMBED_RECORDS]
        texts = [query] + [r.text for r in recs]

        def do_embed():
            return _check_vectors(
                _call_backend(lambda: provider.embed(list(texts))),
                len(texts))

        vectors = guarded_call(config, breaker, provider, "embed",
                               do_embed, lambda: None)
        if vectors is not None:
            qv = vectors[0]
            for rec, rv in zip(recs, vectors[1:]):
                if rec.rid in base_by_rid:
                    continue
                sim = _cosine(qv, rv)
                if sim >= sim_threshold:
                    additions.append((sim, rec))
            additions.sort(key=lambda s: (-s[0], -s[1].ts, s[1].rid))

    union = list(base)
    for sim, rec in additions:
        union.append({"rid": rec.rid, "kind": rec.kind,
                      "source": rec.source, "ts": rec.ts,
                      "score": round(sim, 4),
                      "matched_fields": [],
                      "snippet": _snippet(rec.text, set()),
                      "retrieval": "semantic"})

    # --- rerank (reorder only; membership never changes) -----------------
    # Round-9 blocker fix: SELECT FIRST, then rerank only inside the
    # selected window. Deterministic base matches lead the union, so
    # they are all inside the window (engine.find applied the same
    # limit) and no rerank order can push one out of the result.
    window = union[:effective_limit]
    if "rerank" in config.enabled and len(window) > 1:
        candidates = window[:MAX_RERANK]
        passages = []
        by_rid = {r.rid: r for r in records.values()}
        for m in candidates:
            rec = by_rid[m["rid"]]
            passages.append(rec.text)

        def do_rerank():
            return _check_scores(
                _call_backend(lambda: provider.rerank(query,
                                                      list(passages))),
                len(passages))

        scores = guarded_call(config, breaker, provider, "rerank",
                              do_rerank, lambda: None)
        if scores is not None:
            # stable sort: ties keep the pre-rerank (deterministic-base
            # first, then sim-ordered) arrangement
            order = sorted(range(len(candidates)),
                           key=lambda i: -scores[i])
            window = [candidates[i] for i in order] + window[MAX_RERANK:]

    return window
