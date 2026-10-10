# /ports/model_benchmarks - dev-only benchmark harness for the model series

Honest, reproducible numbers for the pretrained-model adapters: citation
support (ports/find_support_nli) and personal retrieval
(ports/find_personal_semantic). Labeled probe sets run through an
injected support_fn / find_fn and standard metrics come out. Model
endpoints are integration-injected callables, so a run can use the
deterministic baseline, a local model, or (in dev only, within free
hosted credits) a hosted endpoint. This module never touches the
network, downloads no weights, makes no paid calls.

## REUSE BOUNDARY (not a parallel stack)

Verdict logic stays in ports/find + ports/find_support_nli; retrieval
logic stays in ports/find_personal + ports/find_personal_semantic. The
harness CALLS those callables; only metric computation and probe-set
validation live here.

## STATUS: DEV-ONLY HARNESS - NOT MOUNTED, NOT LIVE

Complete as a tested module (21/21 tests, 10/10 repeat runs stable).
Named limits, exactly:

1. HONEST NUMBERS ONLY: metrics are computed from recorded per-probe
   outcomes; the report carries probe counts, coverage, per-label
   slices AND per-label error counts, every per-probe outcome, metric
   definitions and the exact config note. No aggregation hides a
   failing slice (round-9).
2. ERRORS COUNT AGAINST, NEVER FOR: a callable exception or
   out-of-contract return (non-bool verdict, non-list ranking) is an
   "error" outcome - excluded from the correct counts, included in the
   denominators, never silently treated as correct. An INVALID RANKING
   is an error too (round-9): duplicate rids or rids outside the
   probe's record set would otherwise fabricate recall@1/MRR.
3. LABELED PROBES, VALIDATED: missing/unknown labels, empty texts,
   duplicate probe or record ids and expected_rid outside the records
   all refuse the whole run with zero effects.
4. DETERMINISM: probe order never affects metrics (order-reversal
   probe test).
5. NO PARITY CLAIMS: this harness measures OUR gates against LABELED
   probes. Comparisons with other products require their own labeled
   runs and are out of scope here.
6. COST BOUNDARY (round-9, review 5480639560): injected callables
   carry NO cost enforcement of their own - the harness cannot see
   what a callable spends. Wiring a hosted endpoint (even a free tier)
   therefore needs an INDEPENDENT approval/budget gate in the
   integration layer: an explicit owner decision per endpoint, a
   documented call budget, and hard refusal once it is spent. Free
   tiers lapse into paid silently; this module never makes that call.
7. READ-ONLY, DEV-ONLY: no network, no downloads, no state between
   runs.

## Metrics

- citation_precision: of claims the gate verified, fraction labeled
  supported.
- citation_recall: of claims labeled supported, fraction verified.
- citation_false_verify_rate: of claims labeled unsupported, fraction
  wrongly verified (the fail-closed headline metric).
- retrieval_recall_at_k / retrieval_mrr: standard ranking metrics;
  errors count as failures.

## Files

- model_benchmarks.py - validate_citation_probes,
  validate_retrieval_probes, run_citation_benchmark,
  run_retrieval_benchmark, format_report.
- test_model_benchmarks.py - 17 tests incl. the shipped citation probe
  set run against the deterministic baseline (precision 1.0, recall
  0.5) and the NLI adapter with a realistic entailment stand-in
  (precision 1.0, recall 1.0, false-verify 0.0) - fixture numbers, not
  model claims; real model numbers must come from measured runs.
