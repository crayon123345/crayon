import sys, os, unittest
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from find_support_nli import (make_support_fn, FindSupportNliError,
                              MAX_NLI_PREMISES)
from models import (ModelsError, ProviderConfig, CircuitBreaker,
                    FunctionProvider, NoneProvider)


class FakeClock:
    def __init__(self, t=1000.0):
        self.t = t
    def __call__(self):
        return self.t
    def advance(self, s):
        self.t += s


def cfg(enabled=True, threshold=0.9, timeout_ms=2000):
    return ProviderConfig(provider="local",
                          enabled=frozenset({"nli"}) if enabled else frozenset(),
                          timeout_ms=timeout_ms,
                          nli_entailment_min=threshold)


def make(nli_fn, enabled=True, threshold=0.9, failure_threshold=2):
    breaker = CircuitBreaker(failure_threshold=failure_threshold,
                             cooldown_ms=30000, clock=FakeClock())
    provider = FunctionProvider(nli_fn=nli_fn)
    return make_support_fn(cfg(enabled, threshold), breaker, provider), breaker


class WiringTests(unittest.TestCase):
    def test_bad_arguments_refused(self):
        b = CircuitBreaker(clock=FakeClock())
        p = FunctionProvider(nli_fn=lambda a, b2: {"entailment": 1.0})
        with self.assertRaises(FindSupportNliError):
            make_support_fn("nope", b, p)
        with self.assertRaises(FindSupportNliError):
            make_support_fn(cfg(), "nope", p)
        with self.assertRaises(FindSupportNliError):
            make_support_fn(cfg(), b, object())
        with self.assertRaises(FindSupportNliError):
            make_support_fn(cfg(), b, p, threshold=1.5)
        with self.assertRaises(FindSupportNliError):
            make_support_fn(cfg(), b, p, threshold="high")
        with self.assertRaises(FindSupportNliError):
            make_support_fn(cfg(), b, p, threshold=True)

    def test_threshold_defaults_to_config(self):
        p = FunctionProvider(nli_fn=lambda a, b: {"entailment": 0.89})
        s = make_support_fn(cfg(threshold=0.9), CircuitBreaker(clock=FakeClock()), p)
        # 0.89 < 0.9 default -> refuse
        self.assertFalse(s("revenue was 10 million dollars.",
                           "something else entirely. turnover stood at 10 million dollars overall."))


class DeterministicBaseTests(unittest.TestCase):
    def test_verbatim_claim_needs_no_model_call(self):
        calls = []
        def nli(a, b):
            calls.append(1)
            return {"entailment": 0.0}
        s, _ = make(nli)
        claim = "group revenue was 10 million dollars last year"
        content = "group revenue was 10 million dollars last year."
        self.assertTrue(s(claim, content))
        self.assertEqual(calls, [])

    def test_model_cannot_downgrade_a_verbatim_verdict(self):
        s, _ = make(lambda a, b: {"entailment": 0.0})
        self.assertTrue(s("revenue was 10 million dollars",
                          "revenue was 10 million dollars."))


class SemanticUpgradeTests(unittest.TestCase):
    def test_paraphrase_verifies_on_confident_entailment(self):
        s, _ = make(lambda a, b: {"entailment": 0.97})
        self.assertTrue(s("the hotel costs $200 a night",
                          "our rooms run $200 a night including taxes."))

    def test_below_threshold_refuses(self):
        s, _ = make(lambda a, b: {"entailment": 0.5})
        self.assertFalse(s("the hotel costs $200 a night",
                           "our rooms run $200 a night including taxes."))

    def test_threshold_boundary_is_inclusive(self):
        s, _ = make(lambda a, b: {"entailment": 0.9})
        self.assertTrue(s("the hotel costs $200 a night",
                          "our rooms run $200 a night including taxes."))
        s2, _ = make(lambda a, b: {"entailment": 0.899})
        self.assertFalse(s2("the hotel costs $200 a night",
                            "our rooms run $200 a night including taxes."))

    def test_sign_mismatch_refuses_despite_entailment(self):
        s, _ = make(lambda a, b: {"entailment": 0.99})
        self.assertFalse(s("the change was $5",
                           "the change was -$5 according to the ledger."))
        self.assertFalse(s("the change was -$5",
                           "the change was $5 according to the ledger."))

    def test_currency_mismatch_refuses_despite_entailment(self):
        s, _ = make(lambda a, b: {"entailment": 0.99})
        self.assertFalse(s("the room costs €200",
                           "the room costs $200 per night."))

    def test_number_missing_from_sentence_refuses(self):
        s, _ = make(lambda a, b: {"entailment": 0.99})
        self.assertFalse(s("revenue grew 12 percent",
                           "revenue grew strongly last year overall."))

    def test_negation_parity_refuses(self):
        s, _ = make(lambda a, b: {"entailment": 0.99})
        self.assertFalse(s("the drug cures cancer",
                           "the drug does not cure cancer researchers found."))

    def test_hedge_parity_refuses(self):
        s, _ = make(lambda a, b: {"entailment": 0.99})
        self.assertFalse(s("the study shows coffee helps",
                           "the study allegedly shows coffee helps some people."))

    def test_low_evidence_claim_refuses(self):
        s, _ = make(lambda a, b: {"entailment": 1.0})
        self.assertFalse(s("yes", "yes we do offer that service."))

    def test_empty_inputs_refuse(self):
        calls = []
        def nli(a, b):
            calls.append(1)
            return {"entailment": 1.0}
        s, _ = make(nli)
        self.assertFalse(s("", "some content here."))
        self.assertFalse(s("a real claim", ""))
        self.assertFalse(s(None, "content"))
        self.assertFalse(s("claim", None))
        self.assertEqual(calls, [])

    def test_claim_tokens_form_the_hypothesis(self):
        seen = []
        def nli(premise, hypothesis):
            seen.append((premise, hypothesis))
            return {"entailment": 1.0}
        s, _ = make(nli)
        s("The Hotel Costs $200 a Night",
          "filler sentence one. our rooms run $200 a night including taxes.")
        self.assertTrue(seen)
        premise, hypothesis = seen[0] if seen[0][1] else seen[-1]
        self.assertEqual(hypothesis, "the hotel costs $200 a night")
        self.assertIn("$200", premise)


class FailurePathTests(unittest.TestCase):
    def test_provider_exception_drops_like_deterministic(self):
        def boom(a, b):
            raise RuntimeError("endpoint 500")
        s, breaker = make(boom)
        self.assertFalse(s("the hotel costs $200 a night",
                           "our rooms run $200 a night including taxes."))
        self.assertEqual(breaker.snapshot()["consecutive_failures"], 1)

    def test_bad_response_shapes_are_backend_failures(self):
        for bad in (None, "high", {"label": "entailment"},
                    {"entailment": 1.5}, {"entailment": -0.1},
                    {"entailment": "0.95"}, {"entailment": True}):
            s, breaker = make(lambda a, b, _bad=bad: _bad)
            self.assertFalse(s("the hotel costs $200 a night",
                               "our rooms run $200 a night including taxes."))
            self.assertEqual(breaker.snapshot()["last_reason"],
                             "CapabilityUnavailable")

    def test_disabled_capability_makes_no_model_calls(self):
        calls = []
        def nli(a, b):
            calls.append(1)
            return {"entailment": 1.0}
        s, _ = make(nli, enabled=False)
        self.assertFalse(s("the hotel costs $200 a night",
                           "our rooms run $200 a night including taxes."))
        self.assertEqual(calls, [])

    def test_none_provider_makes_no_verdict(self):
        s = make_support_fn(cfg(), CircuitBreaker(clock=FakeClock()),
                            NoneProvider())
        self.assertFalse(s("the hotel costs $200 a night",
                           "our rooms run $200 a night including taxes."))

    def test_open_breaker_short_circuits_semantic_path(self):
        calls = []
        def nli(a, b):
            calls.append(1)
            return {"entailment": 1.0}
        breaker = CircuitBreaker(failure_threshold=1, cooldown_ms=60000,
                                 clock=FakeClock())
        breaker.record_failure()
        s = make_support_fn(cfg(), breaker, FunctionProvider(nli_fn=nli))
        self.assertFalse(s("the hotel costs $200 a night",
                           "our rooms run $200 a night including taxes."))
        self.assertEqual(calls, [])

    def test_backend_raising_models_error_drops_not_crashes(self):
        def bad(a, b):
            raise ModelsError("backend miswired")
        s, breaker = make(bad)
        self.assertFalse(s("the hotel costs $200 a night",
                           "our rooms run $200 a night including taxes."))
        self.assertEqual(breaker.snapshot()["last_reason"], "CapabilityUnavailable")

    def test_slow_backend_times_out_to_drop(self):
        import time
        def slow(a, b):
            time.sleep(0.3)
            return {"entailment": 1.0}
        breaker = CircuitBreaker(failure_threshold=2, cooldown_ms=30000, clock=FakeClock())
        s = make_support_fn(cfg(timeout_ms=50), breaker,
                            FunctionProvider(nli_fn=slow))
        self.assertFalse(s("the hotel costs $200 a night",
                           "our rooms run $200 a night including taxes."))
        self.assertEqual(breaker.snapshot()["last_reason"], "CallTimeout")

    def test_one_attempt_per_sentence_no_retries(self):
        calls = []
        def boom(a, b):
            calls.append(1)
            raise ValueError("cold start")
        s, _ = make(boom)
        s("the hotel costs $200 a night",
          "our rooms run $200 a night including taxes.")
        self.assertEqual(len(calls), 1)


class CapTests(unittest.TestCase):
    def test_premise_cap_bounds_model_calls(self):
        calls = []
        def nli(a, b):
            calls.append(1)
            return {"entailment": 0.0}
        s, _ = make(nli)
        content = " ".join("our rooms run $200 a night option %d." % i
                           for i in range(MAX_NLI_PREMISES + 10))
        self.assertFalse(s("the hotel costs $200 a night", content))
        self.assertEqual(len(calls), MAX_NLI_PREMISES)

    def test_sentences_failing_safeguards_do_not_spend_model_calls(self):
        calls = []
        def nli(a, b):
            calls.append(1)
            return {"entailment": 1.0}
        s, _ = make(nli)
        # 30 sentences with the WRONG number: none may be scored
        content = " ".join("our rooms run $201 a night option %d." % i
                           for i in range(30))
        self.assertFalse(s("the hotel costs $200 a night", content))
        self.assertEqual(calls, [])

    def test_later_sentence_can_verify(self):
        def nli(premise, hypothesis):
            return {"entailment": 0.95 if "garden wing" in premise else 0.1}
        s, _ = make(nli)
        self.assertTrue(s("the hotel costs $200 a night",
                          "our rooms run $200 a night including taxes. "
                          "the garden wing costs $200 a night all in."))


if __name__ == "__main__":
    unittest.main()


class RoundNineReviewTests(unittest.TestCase):
    def test_contraction_negation_refused_zero_model_calls(self):
        # round-9 blocker (peer probe): "approve the plan" against
        # "The board didn't approve the plan." must refuse with ZERO
        # model calls - the reused gate marks contractions as negation
        for neg in ("didn't", "didnt", "wouldn't", "couldn't",
                    "shouldn't", "haven't", "can't", "don\u2019t"):
            calls = []
            def nli(a, b, _c=calls):
                _c.append((a, b))
                return {"entailment": 1.0}
            support, _ = make(nli)
            self.assertFalse(support(
                "approve the plan",
                "The board %s approve the plan." % neg), neg)
            self.assertEqual(calls, [], neg)

    def test_negated_sentence_never_scored(self):
        # with other content present the eager model may still verify
        # elsewhere - but the negated sentence itself must never reach
        # the model (gate safeguards constrain what gets scored)
        scored = []
        def nli(a, b):
            scored.append(a)
            return {"entailment": 1.0}
        support, _ = make(nli)
        support("approve the plan",
                "The board didn't approve the plan. Some other context "
                "about the board and the plan approval process.")
        self.assertTrue(all("didn't" not in p for p in scored))

    def test_contraction_parity_verifies_with_model(self):
        support, _ = make(lambda a, b: {"entailment": 0.95})
        self.assertTrue(support(
            "the board didn't approve the plan",
            "Earlier text. Sources say the board didn't approve the plan "
            "after a long meeting."))

    def test_contradiction_vetoes_entailment(self):
        # paraphrase claim (no verbatim base): entailed but contradicted
        # at/above threshold -> refuse
        support, _ = make(lambda a, b: {"entailment": 0.95,
                                        "contradiction": 0.97})
        self.assertFalse(support(
            "the board gave the plan its approval",
            "Earlier text. The board approved the plan on monday."))

    def test_contradiction_below_threshold_passes(self):
        support, _ = make(lambda a, b: {"entailment": 0.95,
                                        "contradiction": 0.10})
        self.assertTrue(support(
            "the board gave the plan its approval",
            "Earlier text. The board approved the plan on monday."))

    def test_bad_contradiction_drops(self):
        # strict validation: a present-but-invalid contradiction is a
        # backend failure - drop, never verify
        for bad in (True, "high", -0.1, 1.5, float("nan")):
            support, _ = make(lambda a, b, v=bad: {"entailment": 0.95,
                                                   "contradiction": v})
            self.assertFalse(support(
                "the board gave the plan its approval",
                "Earlier text. The board approved the plan on monday."),
                repr(bad))
