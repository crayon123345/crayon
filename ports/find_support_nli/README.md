# /ports/find_support_nli - NLI support_fn adapter for ports/find

A candidate production support_fn for ports/find's citation gate. The
deterministic reference gate stays the base verdict; this adapter adds a
GUARDED semantic upgrade path through a pretrained NLI model (natural
language inference: does this content sentence entail this claim), so a
paraphrased source can verify a claim the verbatim gate cannot quote.
The model can never override the gate's safeguards and never grants
permission for anything.

## REUSE BOUNDARY (not a parallel stack)

ports/find owns every gate safeguard - sentence boundaries, tokenization,
exact numeric tokens, negation/hedge sets, minimum evidence - and this
adapter IMPORTS those internals (same cross-port pattern as
ports/find_track importing ports/find/urlcanon.py). ports/models owns
the provider seam; every model call goes through models.guarded_call.
The NLI model itself (candidate: MoritzLaurer/DeBERTa-v3-base-mnli-
fever-anli, MIT, or cross-encoder/nli-deberta-v3-xsmall, Apache-2.0 -
license verified against the live HF model cards 2026-10-11; per-revision
re-verification belongs to the integration wiring PR) is an
integration-injected callable. This module ships no weights, downloads
nothing, touches no network.

## STATUS: STAGING MODULE - NOT MOUNTED, NOT LIVE

Complete as a tested module (31/31 tests, 10/10 repeat runs stable) but
NOT wired into the running app. Named limits, exactly:

1. DETERMINISTIC BASE FIRST: a claim the reference gate verifies returns
   True with ZERO model calls (probe: verbatim claim, model returning
   0.0, still True, zero calls made). The model is consulted only for
   claims the verbatim gate cannot verify.
2. SAFEGUARDS GATE THE MODEL: a semantic verify needs the gate's minimum
   evidence, EXACT numeric-token presence (sign/currency/% load-bearing:
   "$5" != "-$5", "€200" != "$200" refuse even at entailment 0.99),
   negation/hedge parity, AND entailment >= threshold (default
   config.nli_entailment_min = 0.9; boundary inclusive).
3. FAIL-CLOSED ON MODEL FAILURE: exception, timeout, open breaker,
   disabled/unavailable capability, or an out-of-contract response
   (non-mapping, missing/out-of-range/non-numeric entailment) all drop
   the citation exactly as the deterministic default would. A backend
   that raises ModelsError drops the citation rather than crashing the
   verifying batch. One attempt per candidate sentence, no retries.
4. BOUNDED MODEL USE: at most MAX_NLI_PREMISES (25) safeguard-passing
   sentences are scored per claim, in content order; sentences failing
   safeguards spend no model call; beyond the cap sentences are not
   scored (fail-closed direction).
5. NORMALIZED TEXT: premise and hypothesis are the gate's normalized
   lowercase token text, so sentence-boundary semantics stay
   single-owned by ports/find. Production wiring may substitute raw
   sentence text under the same boundary rules.
6. NO STATE: the adapter holds no verdicts and caches nothing; breaker
   state lives in the caller-owned models.CircuitBreaker.
7. READ-ONLY, NOT MOUNTED: no network, no downloads, no action paths.
   RSS/memory-fit claims for any real NLI model must be MEASURED by the
   integration wiring before any fit claim is made.

## Files

- find_support_nli.py - make_support_fn(config, breaker, provider,
  threshold=None) returning support_fn(claim_text, content) -> bool.
- test_find_support_nli.py - 26 tests: wiring validation, deterministic
  base short-circuit, semantic upgrade + threshold boundary, sign/
  currency/number/negation/hedge safeguard probes at high entailment,
  failure paths (exception, timeout, bad response shapes, open breaker,
  disabled capability, backend ModelsError), premise cap and
  safeguard-call-spending probes.
