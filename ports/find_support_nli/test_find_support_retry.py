import sys, os, unittest
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from find_support_nli import (make_support_fn, make_retrying_support_fn,
                              FindSupportNliError, MAX_NLI_PREMISES)
from models import (ModelsError, ProviderConfig, CircuitBreaker,
                    FunctionProvider)


class FakeClock:
    def __init__(self, t=1000.0):
        self.t = t
    def __call__(self):
        return self.t


def cfg(enabled=True, threshold=0.9):
    return ProviderConfig(provider="local",
                          enabled=frozenset({"nli"}) if enabled else frozenset(),
                          nli_entailment_min=threshold)


def make_retry(nli_fn, enabled=True, threshold=0.9):
    breaker = CircuitBreaker(failure_threshold=2, cooldown_ms=30000,
                             clock=FakeClock())
    provider = FunctionProvider(nli_fn=nli_fn)
    return make_retrying_support_fn(cfg(enabled, threshold), breaker, provider)


HIGH = {"entailment": 0.99, "contradiction": 0.0}
LOW = {"entailment": 0.10, "contradiction": 0.0}


class WiringTests(unittest.TestCase):
    def test_bad_arguments_refused(self):
        b = CircuitBreaker(clock=FakeClock())
        p = FunctionProvider(nli_fn=lambda a, b2: dict(HIGH))
        with self.assertRaises(FindSupportNliError):
            make_retrying_support_fn("nope", b, p)
        with self.assertRaises(FindSupportNliError):
            make_retrying_support_fn(cfg(), "nope", p)
        with self.assertRaises(FindSupportNliError):
            make_retrying_support_fn(cfg(), b, object())
        with self.assertRaises(FindSupportNliError):
            make_retrying_support_fn(cfg(), b, p, threshold=1.5)
        with self.assertRaises(FindSupportNliError):
            make_retrying_support_fn(cfg(), b, p, threshold=True)


class RestrictionGuardTests(unittest.TestCase):
    TRAP_CLAIM = "children enter for free"
    TRAP_CONTENT = "children under five enter for free."

    def test_restricted_premise_never_verifies(self):
        # Even a maximal entailment score cannot override a restriction
        # the claim omits ("under five").
        s = make_retry(lambda a, b: dict(HIGH))
        self.assertFalse(s(self.TRAP_CLAIM, self.TRAP_CONTENT))

    def test_guard_is_retry_variant_only(self):
        # Characterization: the plain variant keeps its published
        # behavior - the model alone decides. The guard is opt-in via
        # the retrying variant.
        s = make_support_fn(cfg(), CircuitBreaker(clock=FakeClock()),
                            FunctionProvider(nli_fn=lambda a, b: dict(HIGH)))
        self.assertTrue(s(self.TRAP_CLAIM, self.TRAP_CONTENT))

    def test_matching_restriction_in_claim_is_allowed(self):
        # When the claim itself carries the restriction, no gap exists.
        s = make_retry(lambda a, b: dict(HIGH))
        self.assertTrue(s("children under five enter for free",
                          "children under five enter for free as part of the family pass."))

    def test_phrase_marker_gap_refused(self):
        s = make_retry(lambda a, b: dict(HIGH))
        self.assertFalse(s("members can bring guests",
                           "members can bring up to two guests on weekends."))


class RetryPassTests(unittest.TestCase):
    def test_split_evidence_verified_on_retry(self):
        # Neither sentence alone entails; the pair window does.
        def nli(premise, hypothesis):
            if "morning" in premise and "night" in premise:
                return dict(HIGH)
            return dict(LOW)
        s = make_retry(nli)
        self.assertTrue(s("the medicine is taken twice a day",
                          "take one tablet in the morning. take one more at night."))

    def test_single_sentence_still_verified_first_pass(self):
        seen = []
        def nli(premise, hypothesis):
            seen.append(premise)
            return dict(HIGH) if "friday" in premise else dict(LOW)
        s = make_retry(nli)
        self.assertTrue(s("the office closes on friday",
                          "the office closes on friday for maintenance work each week."))
        # No re-segmented variants needed: first-pass hit stops early.
        self.assertTrue(all("morning" not in p for p in seen))

    def test_refusal_when_no_variant_entails(self):
        s = make_retry(lambda a, b: dict(LOW))
        self.assertFalse(s("the museum charges nothing for entry",
                           "the museum shop sells postcards and books about art."))

    def test_budget_shared_across_passes(self):
        calls = []
        def nli(premise, hypothesis):
            calls.append(premise)
            return dict(LOW)
        s = make_retry(nli)
        content = " ".join("filler sentence number %d with enough tokens." % i
                           for i in range(40))
        s("a claim with enough evidence tokens inside", content)
        self.assertLessEqual(len(calls), MAX_NLI_PREMISES)


class FailClosedTests(unittest.TestCase):
    CLAIM = "the museum charges nothing for entry"
    CONTENT = "admission to the museum is completely free for everyone."

    def test_backend_exception_refuses(self):
        def nli(a, b):
            raise ModelsError("boom")
        self.assertFalse(make_retry(nli)(self.CLAIM, self.CONTENT))

    def test_malformed_response_refuses(self):
        self.assertFalse(make_retry(lambda a, b: {"entailment": "high"})(self.CLAIM, self.CONTENT))
        self.assertFalse(make_retry(lambda a, b: ["not", "a", "dict"])(self.CLAIM, self.CONTENT))

    def test_contradiction_vetoes_in_both_passes(self):
        def nli(premise, hypothesis):
            return {"entailment": 0.99, "contradiction": 0.95}
        self.assertFalse(make_retry(nli)(self.CLAIM, self.CONTENT))

    def test_capability_disabled_is_deterministic_only(self):
        def nli(a, b):
            raise AssertionError("model must not be called")
        s = make_retry(nli, enabled=False)
        self.assertFalse(s(self.CLAIM, self.CONTENT))

    def test_verbatim_claim_needs_no_model(self):
        def nli(a, b):
            raise AssertionError("model must not be called")
        s = make_retry(nli)
        self.assertTrue(s("the museum is free", "the museum is free."))


if __name__ == "__main__":
    unittest.main()


class NumericMatchTests(unittest.TestCase):
    def test_time_unit_conversion_recovers_paraphrase(self):
        # "two hours" in the claim == "120 minutes" in the premise.
        s = make_retry(lambda a, b: dict(HIGH))
        self.assertTrue(s("the exam lasts two hours",
                          "students have 120 minutes to finish the exam."))

    def test_word_digit_equivalence(self):
        s = make_retry(lambda a, b: dict(HIGH))
        self.assertTrue(s("the exam lasts two hours",
                          "the exam lasts 2 hours in the main hall."))

    def test_quantity_mismatch_still_refuses(self):
        # 120 minutes is NOT three hours - conversion is exact, never
        # approximate, so a mismatched quantity refuses.
        s = make_retry(lambda a, b: dict(HIGH))
        self.assertFalse(s("the exam lasts three hours",
                           "students have 120 minutes to finish the exam."))

    def test_symbol_token_falls_back_to_exact_match(self):
        s = make_retry(lambda a, b: dict(HIGH))
        self.assertTrue(s("our rooms cost $200 a night",
                          "our rooms run $200 a night including taxes."))
        self.assertFalse(s("our rooms cost $300 a night",
                           "our rooms run $200 a night including taxes."))

    def test_unitless_claim_number_must_appear(self):
        s = make_retry(lambda a, b: dict(HIGH))
        self.assertFalse(s("the flight leaves at 7 pm",
                           "the flight leaves at 9 pm from the north terminal."))


class CountVetoTests(unittest.TestCase):
    def test_frequency_count_mismatch_refused(self):
        # ut02 class: "once daily" vs "two pills every morning".
        s = make_retry(lambda a, b: dict(HIGH))
        self.assertFalse(s("the pills are taken once daily",
                           "take two pills every morning."))

    def test_matching_count_verified(self):
        s = make_retry(lambda a, b: dict(HIGH))
        self.assertTrue(s("the recipe uses two cups of flour",
                          "the recipe needs 2 cups of flour and a pinch of salt."))

    def test_aggregation_falls_through_to_model(self):
        # "one tablet in the morning and one at night" has no single
        # count before a shared noun - the model decides (and here says
        # yes): the veto must NOT fire on aggregation.
        def nli(premise, hypothesis):
            return dict(HIGH) if "morning" in premise and "night" in premise else dict(LOW)
        s = make_retry(nli)
        self.assertTrue(s("the medicine is taken twice a day",
                          "take one tablet in the morning. take one more at night."))

    def test_time_expressions_not_counts(self):
        s = make_retry(lambda a, b: dict(HIGH))
        self.assertTrue(s("the concert starts at 7 pm",
                          "doors open at 6 and the concert begins at 7 pm sharp."))

    def test_different_noun_no_veto(self):
        # premise counts something the claim never mentions: no veto.
        s = make_retry(lambda a, b: dict(HIGH))
        self.assertTrue(s("the museum is free for children",
                          "the museum, which has 4 floors, is free for children."))
