import sys, os, unittest
_HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, _HERE)
sys.path.insert(0, os.path.join(_HERE, "..", "find"))
sys.path.insert(0, os.path.join(_HERE, "..", "find_support_nli"))
sys.path.insert(0, os.path.join(_HERE, "..", "models"))

from model_benchmarks import (BenchmarkError, validate_citation_probes,
                              validate_retrieval_probes,
                              run_citation_benchmark,
                              run_retrieval_benchmark, format_report,
                              METRIC_DEFS)
from find import reference_support
from find_support_nli import make_support_fn
from models import ProviderConfig, CircuitBreaker, FunctionProvider


class FakeClock:
    def __call__(self):
        return 1000.0


CITATION_PROBES = [
    # verbatim quote evidence: supported
    {"pid": "v1", "claim": "revenue was 10 million dollars",
     "content": "group revenue was 10 million dollars last year.",
     "label": "supported"},
    # paraphrase: supported (only a semantic gate can catch it)
    {"pid": "p1", "claim": "the hotel costs $200 a night",
     "content": "our rooms run $200 a night including taxes.",
     "label": "supported"},
    # sign flip: unsupported
    {"pid": "s1", "claim": "the change was $5",
     "content": "the change was -$5 according to the ledger.",
     "label": "unsupported"},
    # currency mismatch: unsupported
    {"pid": "c1", "claim": "the room costs €200",
     "content": "the room costs $200 per night.", "label": "unsupported"},
    # contradiction framing: unsupported
    {"pid": "n1", "claim": "the drug cures cancer",
     "content": "claims that the drug cures cancer are false.",
     "label": "unsupported"},
    # disjoint fabrication: unsupported
    {"pid": "f1", "claim": "the mayor resigned on tuesday",
     "content": "local bakery wins award for sourdough.",
     "label": "unsupported"},
]


def nli_support(score_fn):
    """score_fn(premise, hypothesis) -> entailment in [0, 1]."""
    cfg = ProviderConfig(provider="local", enabled=frozenset({"nli"}))
    breaker = CircuitBreaker(clock=FakeClock())
    provider = FunctionProvider(
        nli_fn=lambda a, b: {"entailment": score_fn(a, b)})
    return make_support_fn(cfg, breaker, provider)


def realistic_nli(premise, hypothesis):
    # stand-in for a real NLI model: high entailment only for the
    # genuinely entailed paraphrase pair in CITATION_PROBES
    return 0.97 if "rooms run $200" in premise else 0.1


class ValidationTests(unittest.TestCase):
    def test_citation_probe_validation(self):
        with self.assertRaises(BenchmarkError):
            validate_citation_probes([])
        with self.assertRaises(BenchmarkError):
            validate_citation_probes("nope")
        with self.assertRaises(BenchmarkError):
            validate_citation_probes([{"pid": "x", "claim": "c",
                                       "content": "c", "label": "maybe"}])
        with self.assertRaises(BenchmarkError):
            validate_citation_probes([{"pid": "x", "claim": "", "content": "c",
                                       "label": "supported"}])
        with self.assertRaises(BenchmarkError):
            validate_citation_probes([
                {"pid": "x", "claim": "a b", "content": "c", "label": "supported"},
                {"pid": "x", "claim": "a b", "content": "c", "label": "supported"}])
        out = validate_citation_probes(CITATION_PROBES)
        self.assertEqual(len(out), 6)

    def test_retrieval_probe_validation(self):
        base = {"pid": "q1", "query": "visa",
                "records": [{"rid": "r1", "text": "schengen appointment"}],
                "expected_rid": "r1"}
        self.assertEqual(len(validate_retrieval_probes([base])), 1)
        with self.assertRaises(BenchmarkError):
            validate_retrieval_probes([dict(base, expected_rid="nope")])
        with self.assertRaises(BenchmarkError):
            validate_retrieval_probes([dict(base, records=[])])
        with self.assertRaises(BenchmarkError):
            validate_retrieval_probes([dict(base, records=[{"rid": "r1", "text": "a"},
                                                           {"rid": "r1", "text": "b"}])])

    def test_callable_and_k_validation(self):
        with self.assertRaises(BenchmarkError):
            run_citation_benchmark("nope", CITATION_PROBES)
        with self.assertRaises(BenchmarkError):
            run_retrieval_benchmark(None, [], k=1)
        with self.assertRaises(BenchmarkError):
            run_retrieval_benchmark(lambda q, r: [], [{"pid": "q", "query": "a",
                                                       "records": [{"rid": "r", "text": "t"}],
                                                       "expected_rid": "r"}], k=0)


class CitationBenchmarkTests(unittest.TestCase):
    def test_deterministic_baseline_numbers(self):
        rep = run_citation_benchmark(reference_support, CITATION_PROBES)
        # verbatim v1 verifies; paraphrase p1 missed; 4 unsupported all refuse
        self.assertEqual(rep["true_positives"], 1)
        self.assertEqual(rep["false_positives"], 0)
        self.assertEqual(rep["citation_precision"], 1.0)
        self.assertAlmostEqual(rep["citation_recall"], 0.5)
        self.assertEqual(rep["citation_false_verify_rate"], 0.0)
        self.assertEqual(rep["errors"], 0)
        self.assertEqual(rep["probes"], 6)

    def test_nli_gate_improves_recall_without_false_verifies(self):
        rep = run_citation_benchmark(nli_support(realistic_nli), CITATION_PROBES)
        self.assertEqual(rep["true_positives"], 2)   # v1 + paraphrase p1
        self.assertEqual(rep["false_positives"], 0)  # safeguards hold
        self.assertEqual(rep["citation_precision"], 1.0)
        self.assertEqual(rep["citation_recall"], 1.0)
        self.assertEqual(rep["citation_false_verify_rate"], 0.0)

    def test_verifying_everything_tanks_precision(self):
        rep = run_citation_benchmark(lambda c, ct: True, CITATION_PROBES)
        self.assertAlmostEqual(rep["citation_precision"], 2 / 6.0)
        self.assertEqual(rep["citation_false_verify_rate"], 1.0)

    def test_errors_counted_separately_never_correct(self):
        def flaky(claim, content):
            if "hotel" in claim:
                raise RuntimeError("endpoint down")
            return reference_support(claim, content)
        rep = run_citation_benchmark(flaky, CITATION_PROBES)
        self.assertEqual(rep["errors"], 1)
        self.assertEqual(rep["true_positives"], 1)  # only v1
        err = [o for o in rep["outcomes"] if o["outcome"] == "error"]
        self.assertEqual(err[0]["pid"], "p1")

    def test_non_bool_verdict_is_error(self):
        rep = run_citation_benchmark(lambda c, ct: "yes", CITATION_PROBES)
        self.assertEqual(rep["errors"], 6)
        self.assertEqual(rep["true_positives"], 0)

    def test_determinism(self):
        a = run_citation_benchmark(nli_support(realistic_nli), CITATION_PROBES)
        b = run_citation_benchmark(nli_support(realistic_nli), list(reversed(CITATION_PROBES)))
        self.assertEqual(a["citation_precision"], b["citation_precision"])
        self.assertEqual(a["citation_recall"], b["citation_recall"])
        self.assertEqual(a["citation_false_verify_rate"],
                         b["citation_false_verify_rate"])


class RetrievalBenchmarkTests(unittest.TestCase):
    PROBES = [
        {"pid": "q1", "query": "visa appointment",
         "records": [{"rid": "r1", "text": "schengen consulate paperwork finalized"},
                     {"rid": "r2", "text": "grocery list for the weekend trip"}],
         "expected_rid": "r1"},
        {"pid": "q2", "query": "dentist",
         "records": [{"rid": "r3", "text": "dentist cleaning on friday"},
                     {"rid": "r4", "text": "bought new running shoes"}],
         "expected_rid": "r3"},
    ]

    def test_perfect_ranker(self):
        def find_fn(query, records):
            return [r["rid"] for r in sorted(records, key=lambda r: r["rid"])]
        # q1 expects r1 (first), q2 expects r3 (first after sort)
        rep = run_retrieval_benchmark(find_fn, self.PROBES, k=2)
        self.assertEqual(rep["retrieval_recall_at_k"], 1.0)
        self.assertEqual(rep["retrieval_mrr"], 1.0)

    def test_second_rank_halves_rr(self):
        def find_fn(query, records):
            rids = [r["rid"] for r in records]
            return list(reversed(rids))
        rep = run_retrieval_benchmark(find_fn, self.PROBES, k=2)
        self.assertEqual(rep["retrieval_recall_at_k"], 1.0)
        self.assertEqual(rep["retrieval_mrr"], 0.5)

    def test_missing_expected_counts_zero(self):
        rep = run_retrieval_benchmark(lambda q, rs: ["other"], self.PROBES, k=1)
        self.assertEqual(rep["retrieval_recall_at_k"], 0.0)
        self.assertEqual(rep["retrieval_mrr"], 0.0)

    def test_rank_beyond_k_misses_recall_but_counts_mrr(self):
        def find_fn(query, records):
            rids = [r["rid"] for r in records]
            return list(reversed(rids))
        rep = run_retrieval_benchmark(find_fn, self.PROBES, k=1)
        self.assertEqual(rep["retrieval_recall_at_k"], 0.0)
        self.assertEqual(rep["retrieval_mrr"], 0.5)

    def test_errors_count_against_metrics(self):
        def flaky(query, records):
            raise RuntimeError("down")
        rep = run_retrieval_benchmark(flaky, self.PROBES, k=2)
        self.assertEqual(rep["errors"], 2)
        self.assertEqual(rep["retrieval_recall_at_k"], 0.0)
        self.assertEqual(rep["retrieval_mrr"], 0.0)

    def test_bad_return_shape_is_error(self):
        rep = run_retrieval_benchmark(lambda q, rs: "r1", self.PROBES, k=2)
        self.assertEqual(rep["errors"], 2)
    def test_duplicate_rids_are_error_not_score(self):
        # round-9 (review 5480639560): ['r1','r1'] used to yield perfect
        # recall@1/MRR - an invalid ranking is an error outcome
        rep = run_retrieval_benchmark(lambda q, rs: ["r1", "r1"],
                                      self.PROBES, k=2)
        self.assertEqual(rep["errors"], len(self.PROBES))
        self.assertEqual(rep["retrieval_recall_at_k"], 0.0)
        self.assertEqual(rep["retrieval_mrr"], 0.0)

    def test_fabricated_rid_is_error_not_score(self):
        # ['r1','ghost'] used to count as a clean hit
        rep = run_retrieval_benchmark(lambda q, rs: ["r1", "ghost"],
                                      self.PROBES, k=2)
        self.assertEqual(rep["errors"], len(self.PROBES))
        self.assertEqual(rep["retrieval_recall_at_k"], 0.0)

    def test_report_shows_per_probe_and_label_errors(self):
        # round-9 (review 5480639560): per-probe outcomes, per-label
        # errors, coverage; false-verify separate from errors
        def flaky(claim, content):
            if "hotel" in claim:
                raise RuntimeError("endpoint down")
            return reference_support(claim, content)
        rep = run_citation_benchmark(flaky, CITATION_PROBES)
        text = format_report(rep, "probe run")
        self.assertIn("coverage: 5/6 answered", text)
        self.assertIn("label errors: supported", text)
        self.assertIn("per-probe outcomes:", text)
        self.assertIn("p1: error (label=supported)", text)
        self.assertIn("citation_false_verify_rate:", text)

    def test_report_shows_retrieval_per_probe(self):
        rep = run_retrieval_benchmark(lambda q, rs: ["r1", "ghost"],
                                      self.PROBES, k=2)
        text = format_report(rep, "probe run")
        self.assertIn("per-probe outcomes:", text)
        self.assertIn(": error rank=none", text)


class ReportTests(unittest.TestCase):
    def test_format_report_renders_definitions_and_slices(self):
        rep = run_citation_benchmark(nli_support(realistic_nli), CITATION_PROBES)
        text = format_report(rep, "deterministic + NLI adapter, threshold 0.9")
        self.assertIn("citation_precision: 1.0000", text)
        self.assertIn("probes: 6, errors: 0", text)
        self.assertIn("supported 2/2 verified", text)
        for key, definition in METRIC_DEFS.items():
            self.assertIn(definition, text)

    def test_format_report_validation(self):
        with self.assertRaises(BenchmarkError):
            format_report({"kind": "other"}, "x")
        with self.assertRaises(BenchmarkError):
            format_report(run_citation_benchmark(reference_support,
                                                 CITATION_PROBES), "")


if __name__ == "__main__":
    unittest.main()
