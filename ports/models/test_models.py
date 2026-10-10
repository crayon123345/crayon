import sys, os, time, unittest
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from models import (ModelsError, CapabilityUnavailable, CallTimeout,
                    ProviderConfig, CircuitBreaker, NoneProvider,
                    FunctionProvider, get_provider, from_env, guarded_call,
                    CAPABILITIES, PROVIDERS, MAX_BATCH)


class FakeClock:
    def __init__(self, t=1000.0):
        self.t = t
    def __call__(self):
        return self.t
    def advance(self, s):
        self.t += s


def enabled(*caps):
    return ProviderConfig(provider="local", enabled=frozenset(caps))


class ConfigTests(unittest.TestCase):
    def test_defaults_are_deterministic_off(self):
        c = ProviderConfig()
        self.assertEqual(c.provider, "none")
        self.assertEqual(c.enabled, frozenset())
        self.assertIsNone(c.cache_dir)
        self.assertEqual(c.timeout_ms, 2000)

    def test_unknown_provider_refused(self):
        with self.assertRaises(ModelsError):
            ProviderConfig(provider="openai")

    def test_each_declared_provider_accepted(self):
        for p in PROVIDERS:
            self.assertEqual(ProviderConfig(provider=p).provider, p)

    def test_unknown_capability_refused(self):
        with self.assertRaises(ModelsError):
            ProviderConfig(enabled=frozenset({"embed", "summarize"}))

    def test_unhashable_enabled_refused_as_models_error(self):
        with self.assertRaises(ModelsError):
            ProviderConfig(enabled=[["embed"]])

    def test_non_iterable_enabled_refused(self):
        with self.assertRaises(ModelsError):
            ProviderConfig(enabled=7)

    def test_timeout_bounds(self):
        with self.assertRaises(ModelsError):
            ProviderConfig(timeout_ms=0)
        with self.assertRaises(ModelsError):
            ProviderConfig(timeout_ms=60001)
        with self.assertRaises(ModelsError):
            ProviderConfig(timeout_ms=1.5)
        with self.assertRaises(ModelsError):
            ProviderConfig(timeout_ms=True)
        self.assertEqual(ProviderConfig(timeout_ms=1).timeout_ms, 1)

    def test_threshold_and_cooldown_bounds(self):
        with self.assertRaises(ModelsError):
            ProviderConfig(failure_threshold=0)
        with self.assertRaises(ModelsError):
            ProviderConfig(cooldown_ms=-1)
        self.assertEqual(ProviderConfig(cooldown_ms=0).cooldown_ms, 0)

    def test_nli_threshold_bounds(self):
        with self.assertRaises(ModelsError):
            ProviderConfig(nli_entailment_min=1.5)
        with self.assertRaises(ModelsError):
            ProviderConfig(nli_entailment_min=-0.1)
        with self.assertRaises(ModelsError):
            ProviderConfig(nli_entailment_min="high")
        self.assertEqual(ProviderConfig(nli_entailment_min=0).nli_entailment_min, 0.0)

    def test_cache_dir_validation(self):
        with self.assertRaises(ModelsError):
            ProviderConfig(cache_dir="")
        with self.assertRaises(ModelsError):
            ProviderConfig(cache_dir=123)
        with self.assertRaises(ModelsError):
            ProviderConfig(cache_dir="x" * 513)
        self.assertEqual(ProviderConfig(cache_dir="/tmp/m").cache_dir, "/tmp/m")

    def test_config_is_frozen(self):
        c = ProviderConfig()
        with self.assertRaises(Exception):
            c.provider = "local"
        self.assertEqual(c.provider, "none")


class FromEnvTests(unittest.TestCase):
    def test_empty_env_gives_defaults(self):
        self.assertEqual(from_env({}), ProviderConfig())

    def test_full_parse(self):
        c = from_env({
            "CRAYON_MODEL_PROVIDER": "local",
            "CRAYON_MODEL_CACHE_DIR": "/cache",
            "CRAYON_MODEL_TIMEOUT_MS": "1500",
            "CRAYON_MODEL_FAILURE_THRESHOLD": "5",
            "CRAYON_MODEL_COOLDOWN_MS": "1000",
            "CRAYON_MODEL_ENABLED": "embed, nli",
            "CRAYON_MODEL_NLI_ENTAILMENT_MIN": "0.75",
        })
        self.assertEqual(c.provider, "local")
        self.assertEqual(c.enabled, frozenset({"embed", "nli"}))
        self.assertEqual(c.timeout_ms, 1500)
        self.assertEqual(c.failure_threshold, 5)
        self.assertEqual(c.nli_entailment_min, 0.75)

    def test_bad_int_names_field(self):
        with self.assertRaises(ModelsError) as ctx:
            from_env({"CRAYON_MODEL_TIMEOUT_MS": "soon"})
        self.assertIn("CRAYON_MODEL_TIMEOUT_MS", str(ctx.exception))

    def test_bad_float_refused(self):
        with self.assertRaises(ModelsError):
            from_env({"CRAYON_MODEL_NLI_ENTAILMENT_MIN": "high"})

    def test_unknown_provider_via_env_refused(self):
        with self.assertRaises(ModelsError):
            from_env({"CRAYON_MODEL_PROVIDER": "grok"})

    def test_unknown_capability_via_env_refused(self):
        with self.assertRaises(ModelsError):
            from_env({"CRAYON_MODEL_ENABLED": "embed,magic"})

    def test_unknown_keys_ignored(self):
        c = from_env({"CRAYON_MODEL_SECRET": "x", "OTHER": "y"})
        self.assertEqual(c, ProviderConfig())

    def test_empty_cache_dir_becomes_none(self):
        self.assertIsNone(from_env({"CRAYON_MODEL_CACHE_DIR": ""}).cache_dir)

    def test_non_mapping_refused(self):
        with self.assertRaises(ModelsError):
            from_env([("CRAYON_MODEL_PROVIDER", "local")])

    def test_non_string_value_refused(self):
        with self.assertRaises(ModelsError):
            from_env({"CRAYON_MODEL_PROVIDER": 3})

    def test_read_creates_no_state(self):
        env = {"CRAYON_MODEL_PROVIDER": "local"}
        from_env(env)
        self.assertEqual(env, {"CRAYON_MODEL_PROVIDER": "local"})


class BreakerTests(unittest.TestCase):
    def test_starts_closed(self):
        self.assertEqual(CircuitBreaker(clock=FakeClock()).state, "closed")

    def test_opens_after_threshold_consecutive_failures(self):
        b = CircuitBreaker(failure_threshold=2, clock=FakeClock())
        b.record_failure("boom")
        self.assertEqual(b.state, "closed")
        b.record_failure("boom")
        self.assertEqual(b.state, "open")
        self.assertFalse(b.allow())

    def test_success_resets_count(self):
        b = CircuitBreaker(failure_threshold=2, clock=FakeClock())
        b.record_failure()
        b.record_success()
        b.record_failure()
        self.assertEqual(b.state, "closed")

    def test_half_open_after_cooldown(self):
        clk = FakeClock()
        b = CircuitBreaker(failure_threshold=1, cooldown_ms=5000, clock=clk)
        b.record_failure()
        self.assertEqual(b.state, "open")
        clk.advance(4.9)
        self.assertEqual(b.state, "open")
        clk.advance(0.2)
        self.assertEqual(b.state, "half_open")
        self.assertTrue(b.allow())

    def test_half_open_failure_reopens_with_fresh_cooldown(self):
        clk = FakeClock()
        b = CircuitBreaker(failure_threshold=1, cooldown_ms=5000, clock=clk)
        b.record_failure()
        clk.advance(6)
        self.assertEqual(b.state, "half_open")
        b.record_failure("again")          # failed trial -> fresh cooldown
        self.assertEqual(b.state, "open")  # NOT immediately half-open again
        self.assertFalse(b.allow())        # a down backend is not hammered
        clk.advance(4.9)
        self.assertEqual(b.state, "open")
        clk.advance(0.2)
        self.assertEqual(b.state, "half_open")
        self.assertEqual(b.snapshot()["consecutive_failures"], 2)

    def test_zero_cooldown_half_open_immediately(self):
        b = CircuitBreaker(failure_threshold=1, cooldown_ms=0, clock=FakeClock())
        b.record_failure()
        self.assertEqual(b.state, "half_open")
        self.assertTrue(b.allow())

    def test_success_after_half_open_closes(self):
        clk = FakeClock()
        b = CircuitBreaker(failure_threshold=1, cooldown_ms=1000, clock=clk)
        b.record_failure()
        clk.advance(2)
        b.record_success()
        self.assertEqual(b.state, "closed")
        self.assertEqual(b.snapshot()["consecutive_failures"], 0)

    def test_invalid_args(self):
        with self.assertRaises(ModelsError):
            CircuitBreaker(failure_threshold=0)
        with self.assertRaises(ModelsError):
            CircuitBreaker(cooldown_ms=-5)
        with self.assertRaises(ModelsError):
            CircuitBreaker(clock=42)
        with self.assertRaises(ModelsError):
            CircuitBreaker(clock=lambda: "noon")
        with self.assertRaises(ModelsError):
            CircuitBreaker(clock=lambda: (_ for _ in ()).throw(RuntimeError()))

    def test_reason_type_validated(self):
        b = CircuitBreaker(clock=FakeClock())
        with self.assertRaises(ModelsError):
            b.record_failure(reason=123)

    def test_snapshot_is_a_copy(self):
        b = CircuitBreaker(clock=FakeClock())
        snap = b.snapshot()
        snap["consecutive_failures"] = 99
        snap["state"] = "open"
        self.assertEqual(b.snapshot()["consecutive_failures"], 0)
        self.assertEqual(b.state, "closed")

    def test_reads_do_not_mutate(self):
        clk = FakeClock()
        b = CircuitBreaker(failure_threshold=1, cooldown_ms=1000, clock=clk)
        b.record_failure("x")
        before = b.snapshot()
        b.state; b.allow(); b.snapshot()
        self.assertEqual(b.snapshot(), before)


class ProviderTests(unittest.TestCase):
    def test_none_provider_fails_closed_everywhere(self):
        p = NoneProvider()
        self.assertEqual(p.capabilities(), frozenset())
        with self.assertRaises(CapabilityUnavailable):
            p.embed(["x"])
        with self.assertRaises(CapabilityUnavailable):
            p.nli("a", "b")
        with self.assertRaises(CapabilityUnavailable):
            p.rerank("q", ["p"])

    def test_function_provider_capabilities(self):
        p = FunctionProvider(embed_fn=lambda ts: [[0.0] for _ in ts])
        self.assertEqual(p.capabilities(), frozenset({"embed"}))
        with self.assertRaises(CapabilityUnavailable):
            p.nli("a", "b")

    def test_non_callable_backend_refused(self):
        with self.assertRaises(ModelsError):
            FunctionProvider(nli_fn="model")

    def test_embed_delegates_and_validates(self):
        seen = []
        p = FunctionProvider(embed_fn=lambda ts: seen.append(list(ts)) or [[1.0] * len(ts)])
        self.assertEqual(p.embed(["a", "b"]), [[1.0, 1.0]])
        self.assertEqual(seen, [["a", "b"]])
        with self.assertRaises(ModelsError):
            p.embed("not-a-list")
        with self.assertRaises(ModelsError):
            p.embed(["ok", 5])
        with self.assertRaises(ModelsError):
            p.embed([""])
        with self.assertRaises(ModelsError):
            p.embed(["x"] * (MAX_BATCH + 1))

    def test_nli_validates_strings(self):
        p = FunctionProvider(nli_fn=lambda a, b: {"entailment": 0.9})
        with self.assertRaises(ModelsError):
            p.nli(None, "b")
        with self.assertRaises(ModelsError):
            p.nli("a", ["b"])
        with self.assertRaises(ModelsError):
            p.nli("", "b")
        self.assertEqual(p.nli("a", "b"), {"entailment": 0.9})

    def test_rerank_validates(self):
        p = FunctionProvider(rerank_fn=lambda q, ps: [0.5] * len(ps))
        with self.assertRaises(ModelsError):
            p.rerank("q", "passage")
        with self.assertRaises(ModelsError):
            p.rerank(3, ["p"])
        self.assertEqual(p.rerank("q", ["a", "b"]), [0.5, 0.5])

    def test_get_provider_factory(self):
        with self.assertRaises(ModelsError):
            get_provider("not-a-config")
        self.assertIsInstance(get_provider(ProviderConfig()), NoneProvider)
        with self.assertRaises(ModelsError):
            get_provider(ProviderConfig(provider="local"))
        with self.assertRaises(ModelsError):
            get_provider(ProviderConfig(provider="hosted"), backend=object())
        fp = FunctionProvider(nli_fn=lambda a, b: 1)
        self.assertIs(get_provider(ProviderConfig(provider="local"), backend=fp), fp)


class GuardedCallTests(unittest.TestCase):
    def setUp(self):
        self.clk = FakeClock()
        self.breaker = CircuitBreaker(failure_threshold=2, cooldown_ms=1000,
                                      clock=self.clk)

    def test_disabled_capability_returns_fallback_without_calling(self):
        calls = []
        p = FunctionProvider(nli_fn=lambda a, b: calls.append(1) or 1)
        out = guarded_call(ProviderConfig(provider="local"), self.breaker, p,
                           "nli", lambda: p.nli("a", "b"), lambda: "det")
        self.assertEqual(out, "det")
        self.assertEqual(calls, [])

    def test_unavailable_capability_returns_fallback(self):
        p = FunctionProvider(embed_fn=lambda ts: [])
        out = guarded_call(enabled("nli"), self.breaker, p,
                           "nli", lambda: p.nli("a", "b"), lambda: "det")
        self.assertEqual(out, "det")

    def test_success_returns_value_and_records_success(self):
        p = FunctionProvider(nli_fn=lambda a, b: {"label": "entailment"})
        out = guarded_call(enabled("nli"), self.breaker, p,
                           "nli", lambda: p.nli("a", "b"), lambda: "det")
        self.assertEqual(out, {"label": "entailment"})
        self.assertEqual(self.breaker.snapshot()["consecutive_failures"], 0)

    def test_backend_exception_returns_fallback_and_counts_failure(self):
        def boom(a, b):
            raise RuntimeError("model exploded")
        p = FunctionProvider(nli_fn=boom)
        out = guarded_call(enabled("nli"), self.breaker, p,
                           "nli", lambda: p.nli("a", "b"), lambda: "det")
        self.assertEqual(out, "det")
        self.assertEqual(self.breaker.snapshot()["consecutive_failures"], 1)
        self.assertEqual(self.breaker.snapshot()["last_reason"], "RuntimeError")

    def test_timeout_returns_fallback_and_late_result_is_discarded(self):
        finished = []
        def slow(a, b):
            time.sleep(0.3)
            finished.append(1)
            return "late"
        cfg = ProviderConfig(provider="local", enabled=frozenset({"nli"}),
                             timeout_ms=50)
        p = FunctionProvider(nli_fn=slow)
        t0 = time.monotonic()
        out = guarded_call(cfg, self.breaker, p, "nli",
                           lambda: p.nli("a", "b"), lambda: "det")
        self.assertLess(time.monotonic() - t0, 0.28)
        self.assertEqual(out, "det")
        self.assertEqual(self.breaker.snapshot()["last_reason"], "CallTimeout")
        time.sleep(0.4)
        self.assertEqual(finished, [1])  # ran once, no retry, result unused

    def test_no_retry_single_attempt(self):
        calls = []
        def boom(a, b):
            calls.append(1)
            raise ValueError("bad weights")
        p = FunctionProvider(nli_fn=boom)
        for _ in range(2):
            guarded_call(enabled("nli"), self.breaker, p,
                         "nli", lambda: p.nli("a", "b"), lambda: "det")
        self.assertEqual(calls, [1, 1])  # one attempt per call, none retried

    def test_open_breaker_short_circuits(self):
        calls = []
        p = FunctionProvider(nli_fn=lambda a, b: calls.append(1) or 1)
        self.breaker.record_failure()
        self.breaker.record_failure()  # threshold 2 -> open
        out = guarded_call(enabled("nli"), self.breaker, p,
                           "nli", lambda: p.nli("a", "b"), lambda: "det")
        self.assertEqual(out, "det")
        self.assertEqual(calls, [])

    def test_half_open_trial_success_closes(self):
        p = FunctionProvider(nli_fn=lambda a, b: "ok")
        self.breaker.record_failure()
        self.breaker.record_failure()
        self.clk.advance(2)  # past cooldown
        out = guarded_call(enabled("nli"), self.breaker, p,
                           "nli", lambda: p.nli("a", "b"), lambda: "det")
        self.assertEqual(out, "ok")
        self.assertEqual(self.breaker.state, "closed")

    def test_argument_errors_raise_before_any_attempt(self):
        p = FunctionProvider(nli_fn=lambda a, b: 1)
        with self.assertRaises(ModelsError):
            guarded_call(enabled("nli"), self.breaker, p,
                         "magic", lambda: 1, lambda: "det")
        with self.assertRaises(ModelsError):
            guarded_call(enabled("nli"), self.breaker, p,
                         "nli", "not-callable", lambda: "det")
        with self.assertRaises(ModelsError):
            guarded_call(enabled("nli"), self.breaker, p,
                         "nli", lambda: 1, "not-callable")
        with self.assertRaises(ModelsError):
            guarded_call("bad-config", self.breaker, p,
                         "nli", lambda: 1, lambda: "det")
        with self.assertRaises(ModelsError):
            guarded_call(enabled("nli"), "bad-breaker", p,
                         "nli", lambda: 1, lambda: "det")
        # provider arg validation still fails closed as ModelsError
        with self.assertRaises(ModelsError):
            p.nli(None, "b")

    def test_fallback_exception_wrapped_not_raw(self):
        def boom(a, b):
            raise RuntimeError("backend down")
        p = FunctionProvider(nli_fn=boom)
        def bad_fallback():
            raise ValueError("deterministic bug")
        with self.assertRaises(ModelsError):
            guarded_call(enabled("nli"), self.breaker, p,
                         "nli", lambda: p.nli("a", "b"), bad_fallback)
        # a fallback that raises ModelsError passes through unchanged
        def models_fallback():
            raise ModelsError("clean")
        with self.assertRaises(ModelsError) as ctx:
            guarded_call(enabled("nli"), self.breaker, p,
                         "nli", lambda: p.nli("a", "b"), models_fallback)
        self.assertEqual(str(ctx.exception), "clean")

    def test_fallback_used_when_capability_off_even_with_working_backend(self):
        p = FunctionProvider(nli_fn=lambda a, b: "model-says-yes")
        out = guarded_call(ProviderConfig(provider="local"), self.breaker, p,
                           "nli", lambda: p.nli("a", "b"), lambda: "gate-says-no")
        self.assertEqual(out, "gate-says-no")

    def test_provider_without_capabilities_refused(self):
        with self.assertRaises(ModelsError):
            guarded_call(enabled("nli"), self.breaker, None,
                         "nli", lambda: 1, lambda: "det")
        with self.assertRaises(ModelsError):
            guarded_call(enabled("nli"), self.breaker, object(),
                         "nli", lambda: 1, lambda: "det")

    def test_capabilities_raising_backend_fails_closed(self):
        class BadCaps:
            def capabilities(self):
                raise RuntimeError("registry corrupt")
        with self.assertRaises(ModelsError):
            guarded_call(enabled("nli"), self.breaker, BadCaps(),
                         "nli", lambda: 1, lambda: "det")

    def test_capabilities_non_iterable_fails_closed(self):
        class WeirdCaps:
            def capabilities(self):
                return 42
        with self.assertRaises(ModelsError):
            guarded_call(enabled("nli"), self.breaker, WeirdCaps(),
                         "nli", lambda: 1, lambda: "det")

    def test_decreasing_clock_stays_open(self):
        class DownClock:
            def __init__(self):
                self.t = 100.0
            def __call__(self):
                self.t -= 1.0
                return self.t
        b = CircuitBreaker(failure_threshold=1, cooldown_ms=5000, clock=DownClock())
        b.record_failure()
        for _ in range(5):
            self.assertEqual(b.state, "open")
            self.assertFalse(b.allow())

    def test_validation_error_inside_call_propagates_not_fallback(self):
        # p.nli(None, ...) raises ModelsError from arg validation: that is a
        # caller bug, not a backend failure - it must raise, must NOT return
        # the fallback, and must NOT count against the breaker.
        p = FunctionProvider(nli_fn=lambda a, b: 1)
        with self.assertRaises(ModelsError):
            guarded_call(enabled("nli"), self.breaker, p,
                         "nli", lambda: p.nli(None, "b"), lambda: "det")
        self.assertEqual(self.breaker.snapshot()["consecutive_failures"], 0)

    def test_capability_unavailable_inside_call_is_a_backend_failure(self):
        class Flaky:
            def capabilities(self):
                return frozenset({"nli"})
            def nli(self, a, b):
                raise CapabilityUnavailable("weights evicted")
        out = guarded_call(enabled("nli"), self.breaker, Flaky(),
                           "nli", lambda: Flaky().nli("a", "b"), lambda: "det")
        self.assertEqual(out, "det")
        self.assertEqual(self.breaker.snapshot()["last_reason"], "CapabilityUnavailable")

    def test_enabled_given_as_plain_string_refused(self):
        with self.assertRaises(ModelsError):
            ProviderConfig(enabled="embed")

    def test_backend_base_exceptions_take_the_failure_path(self):
        # SystemExit / KeyboardInterrupt raised BY THE BACKEND on its worker
        # thread are provider failures: fallback answer, breaker counted
        # under the original exception's name.
        for exc in (SystemExit(2), KeyboardInterrupt()):
            p = FunctionProvider(nli_fn=lambda a, b, _e=exc: (_ for _ in ()).throw(_e))
            out = guarded_call(enabled("nli"), self.breaker, p,
                               "nli", lambda: p.nli("a", "b"), lambda: "det")
            self.assertEqual(out, "det")
            self.assertEqual(self.breaker.snapshot()["last_reason"],
                             type(exc).__name__)

    def test_backend_models_error_is_converted_by_adapter_not_module(self):
        # Policy: backends must not raise ModelsError; if one does, it is
        # boxed like any other backend failure (adapters such as
        # find_support_nli convert it to CapabilityUnavailable upstream).
        p = FunctionProvider(nli_fn=lambda a, b: 1)
        def call_with_buggy_backend():
            raise ModelsError("backend miswired")  # not module validation
        # ModelsError raised by the CALL lambda is indistinguishable from
        # module validation inside it, so it propagates as a caller bug...
        with self.assertRaises(ModelsError):
            guarded_call(enabled("nli"), self.breaker, p,
                         "nli", call_with_buggy_backend, lambda: "det")
        # ...while FunctionProvider's own arg validation also propagates
        with self.assertRaises(ModelsError):
            guarded_call(enabled("nli"), self.breaker, p,
                         "nli", lambda: p.nli(None, "b"), lambda: "det")
        self.assertEqual(self.breaker.snapshot()["consecutive_failures"], 0)

    def test_embed_roundtrip_through_guard(self):
        p = FunctionProvider(embed_fn=lambda ts: [[float(len(t))] for t in ts])
        out = guarded_call(enabled("embed"), self.breaker, p,
                           "embed", lambda: p.embed(["hi", "there"]),
                           lambda: [[0.0]])
        self.assertEqual(out, [[2.0], [5.0]])


if __name__ == "__main__":
    unittest.main()
