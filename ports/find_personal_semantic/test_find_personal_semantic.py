import sys, os, unittest
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from find_personal_semantic import (semantic_find as _raw_semantic_find,
                                    FindPersonalSemanticError,
                                    MAX_EMBED_RECORDS, MAX_RERANK,
                                    DEFAULT_SIM_THRESHOLD)


def semantic_find(*args, **kwargs):
    # pre-round-9 tests exercise the semantic path: they carry the
    # owner's processing consent. The consent gate itself is tested
    # separately via _raw_semantic_find.
    kwargs.setdefault("consent", True)
    return _raw_semantic_find(*args, **kwargs)
from find_personal import FindPersonal, MemPersonalStore, MAX_RESULTS
from models import ProviderConfig, CircuitBreaker, FunctionProvider, NoneProvider

T0 = 1791633600


class FakeClock:
    def __init__(self, t=1000.0):
        self.t = t
    def __call__(self):
        return self.t


def cfg(caps=("embed", "rerank")):
    return ProviderConfig(provider="local", enabled=frozenset(caps))


def engine_with(records):
    e = FindPersonal(MemPersonalStore())
    for i, (kind, text) in enumerate(records):
        e.add("owner1", kind, text, T0 + i)
    return e


def vec(x, y):
    return [float(x), float(y)]


def make(engine, embed_fn=None, rerank_fn=None, caps=("embed", "rerank")):
    breaker = CircuitBreaker(failure_threshold=2, cooldown_ms=30000,
                             clock=FakeClock())
    provider = FunctionProvider(embed_fn=embed_fn, rerank_fn=rerank_fn)
    return breaker, provider


class WiringTests(unittest.TestCase):
    def test_bad_arguments_refused(self):
        e = engine_with([("note", "hello world today")])
        b, p = make(e, embed_fn=lambda ts: [vec(1, 0)] * len(ts))
        with self.assertRaises(FindPersonalSemanticError):
            semantic_find("nope", cfg(), b, p, "owner1", "hello", T0)
        with self.assertRaises(FindPersonalSemanticError):
            semantic_find(e, "nope", b, p, "owner1", "hello", T0)
        with self.assertRaises(FindPersonalSemanticError):
            semantic_find(e, cfg(), "nope", p, "owner1", "hello", T0)
        with self.assertRaises(FindPersonalSemanticError):
            semantic_find(e, cfg(), b, object(), "owner1", "hello", T0)
        with self.assertRaises(FindPersonalSemanticError):
            semantic_find(e, cfg(), b, p, "owner1", "hello", T0,
                          sim_threshold=1.5)
        with self.assertRaises(FindPersonalSemanticError):
            semantic_find(e, cfg(), b, p, "owner1", "hello", T0,
                          sim_threshold="high")
        # engine.find still validates its own arguments
        from find_personal import FindPersonalError
        with self.assertRaises(FindPersonalError):
            semantic_find(e, cfg(), b, p, "", "hello", T0)


class DeterministicBaseTests(unittest.TestCase):
    def test_no_records_no_model_calls(self):
        e = FindPersonal(MemPersonalStore())
        calls = []
        b, p = make(e, embed_fn=lambda ts: calls.append(ts) or [])
        out = semantic_find(e, cfg(), b, p, "owner1", "anything", T0)
        self.assertEqual(out, [])
        self.assertEqual(calls, [])

    def test_deterministic_match_unchanged_and_annotated(self):
        e = engine_with([("note", "visa appointment confirmed for tuesday")])
        b, p = make(e, embed_fn=lambda ts: [vec(1, 0)] * len(ts))
        base = e.find("owner1", "visa appointment", T0)
        out = semantic_find(e, cfg(), b, p, "owner1", "visa appointment", T0)
        self.assertEqual(len(out), len(base))
        for k in ("rid", "kind", "source", "ts", "score", "matched_fields",
                  "snippet"):
            self.assertEqual(out[0][k], base[0][k])
        self.assertEqual(out[0]["retrieval"], "deterministic")

    def test_disabled_capabilities_return_deterministic_answer(self):
        e = engine_with([("note", "schengen consulate paperwork done")])
        calls = []
        b, p = make(e, embed_fn=lambda ts: calls.append(ts) or [vec(1, 0)] * len(ts))
        out = semantic_find(e, cfg(caps=()), b, p, "owner1",
                            "visa appointment", T0)
        self.assertEqual(out, [])
        self.assertEqual(calls, [])

    def test_none_provider_returns_deterministic_answer(self):
        e = engine_with([("note", "visa appointment confirmed for tuesday")])
        out = semantic_find(e, cfg(), CircuitBreaker(clock=FakeClock()),
                            NoneProvider(), "owner1", "visa appointment", T0)
        self.assertEqual(len(out), 1)
        self.assertEqual(out[0]["retrieval"], "deterministic")


class SemanticRecallTests(unittest.TestCase):
    def test_semantic_add_for_paraphrase_miss(self):
        e = engine_with([("note", "schengen consulate paperwork finalized")])
        # query embedding points along x; record embedding identical
        b, p = make(e, embed_fn=lambda ts: [vec(1, 0)] * len(ts))
        out = semantic_find(e, cfg(caps=("embed",)), b, p, "owner1",
                            "visa appointment", T0)
        self.assertEqual(len(out), 1)
        self.assertEqual(out[0]["retrieval"], "semantic")
        self.assertEqual(out[0]["score"], 1.0)
        self.assertEqual(out[0]["matched_fields"], [])
        self.assertTrue(out[0]["snippet"])

    def test_semantic_threshold_boundary(self):
        e = engine_with([("note", "schengen consulate paperwork finalized")])
        # cos between (1,0) and (x,y): tune to exactly threshold
        import math
        t = DEFAULT_SIM_THRESHOLD
        y = math.sqrt(1 - t * t) / t  # cos = t
        b, p = make(e, embed_fn=lambda ts: [vec(1, 0)] + [vec(1, y)] * (len(ts) - 1))
        out = semantic_find(e, cfg(caps=("embed",)), b, p, "owner1",
                            "visa appointment", T0)
        self.assertEqual(len(out), 1)  # exactly at threshold -> added
        b2, p2 = make(e, embed_fn=lambda ts: [vec(1, 0)] + [vec(1, y * 1.05)] * (len(ts) - 1))
        out2 = semantic_find(e, cfg(caps=("embed",)), b2, p2, "owner1",
                             "visa appointment", T0)
        self.assertEqual(out2, [])  # just below -> dropped

    def test_deterministic_match_not_duplicated_by_semantic(self):
        e = engine_with([("note", "visa appointment confirmed for tuesday")])
        b, p = make(e, embed_fn=lambda ts: [vec(1, 0)] * len(ts))
        out = semantic_find(e, cfg(caps=("embed",)), b, p, "owner1",
                            "visa appointment", T0)
        self.assertEqual(len(out), 1)
        self.assertEqual(out[0]["retrieval"], "deterministic")

    def test_semantic_adds_order_by_similarity(self):
        e = engine_with([("note", "schengen consulate paperwork finalized"),
                         ("note", "embassy renewal forms submitted early")])
        # records are embedded most-recent-first; key vectors by text
        def embed(ts):
            return [vec(1, 0)] + [vec(1, 0.1 if t.startswith("schengen") else 0.5)
                                  for t in ts[1:]]
        b, p = make(e, embed_fn=embed)
        out = semantic_find(e, cfg(caps=("embed",)), b, p, "owner1",
                            "visa appointment", T0)
        self.assertEqual(len(out), 2)
        self.assertGreater(out[0]["score"], out[1]["score"])
        self.assertTrue(out[0]["snippet"].startswith("schengen"))

    def test_zero_vector_matches_nothing(self):
        e = engine_with([("note", "schengen consulate paperwork finalized")])
        b, p = make(e, embed_fn=lambda ts: [vec(0, 0)] * len(ts))
        out = semantic_find(e, cfg(caps=("embed",)), b, p, "owner1",
                            "visa appointment", T0)
        self.assertEqual(out, [])

    def test_owner_strict_recall(self):
        e = engine_with([("note", "schengen consulate paperwork finalized")])
        e.add("owner2", "note", "embassy visa renewal done", T0)
        embedded = []
        b, p = make(e, embed_fn=lambda ts: embedded.append(list(ts)) or [vec(1, 0)] * len(ts))
        semantic_find(e, cfg(caps=("embed",)), b, p, "owner1",
                      "visa appointment", T0)
        self.assertTrue(all("embassy visa renewal" not in t for t in embedded[0]))


class EmbedFailureTests(unittest.TestCase):
    def test_embed_exception_returns_deterministic_answer(self):
        e = engine_with([("note", "visa appointment confirmed for tuesday")])
        def boom(ts):
            raise RuntimeError("endpoint down")
        b, p = make(e, embed_fn=boom)
        out = semantic_find(e, cfg(caps=("embed",)), b, p, "owner1",
                            "visa appointment", T0)
        self.assertEqual(len(out), 1)
        self.assertEqual(out[0]["retrieval"], "deterministic")
        self.assertEqual(b.snapshot()["consecutive_failures"], 1)

    def test_bad_vector_shapes_are_backend_failures(self):
        e = engine_with([("note", "schengen consulate paperwork finalized")])
        bads = [None, "nope", [vec(1, 0)],  # count mismatch
                [vec(1, 0), vec(1, 0), vec(1, 0)],  # count mismatch
                [vec(1, 0), ["a", "b"]],
                [vec(1, 0), vec(1, float("nan"))],
                [vec(1, 0), vec(1, float("inf"))],
                [vec(1, 0), [1.0, True]],
                [vec(1, 0), [1, 0, 3]],  # ragged
                [vec(1, 0), []]]
        for bad in bads:
            b, p = make(e, embed_fn=lambda ts, _bad=bad: _bad)
            out = semantic_find(e, cfg(caps=("embed",)), b, p, "owner1",
                                "visa appointment", T0)
            self.assertEqual(out, [])
            self.assertEqual(b.snapshot()["last_reason"],
                             "CapabilityUnavailable")


class RerankTests(unittest.TestCase):
    def test_rerank_reorders_union(self):
        e = engine_with([("note", "visa appointment confirmed for tuesday"),
                         ("note", "schengen consulate paperwork finalized")])
        b, p = make(e,
                      embed_fn=lambda ts: [vec(1, 0)] * len(ts),
                      rerank_fn=lambda q, ps: [0.1, 0.9])  # 2nd wins
        out = semantic_find(e, cfg(), b, p, "owner1", "visa appointment", T0)
        self.assertEqual(len(out), 2)
        self.assertTrue(out[0]["snippet"].startswith("schengen"))
        self.assertEqual(out[0]["retrieval"], "semantic")
        self.assertEqual(out[1]["retrieval"], "deterministic")

    def test_rerank_failure_keeps_deterministic_first(self):
        e = engine_with([("note", "visa appointment confirmed for tuesday"),
                         ("note", "schengen consulate paperwork finalized")])
        def boom(q, ps):
            raise RuntimeError("reranker cold")
        b, p = make(e, embed_fn=lambda ts: [vec(1, 0)] * len(ts),
                    rerank_fn=boom)
        out = semantic_find(e, cfg(), b, p, "owner1", "visa appointment", T0)
        self.assertEqual(out[0]["retrieval"], "deterministic")
        self.assertEqual(b.snapshot()["consecutive_failures"], 1)

    def test_bad_rerank_shapes_are_backend_failures(self):
        e = engine_with([("note", "visa appointment confirmed for tuesday"),
                         ("note", "schengen consulate paperwork finalized")])
        for bad in (None, "x", [0.5], [0.5, 0.6, 0.7], [0.5, "high"],
                    [0.5, float("nan")], [0.5, True]):
            b, p = make(e, embed_fn=lambda ts: [vec(1, 0)] * len(ts),
                        rerank_fn=lambda q, ps, _bad=bad: _bad)
            out = semantic_find(e, cfg(), b, p, "owner1",
                                "visa appointment", T0)
            self.assertEqual(out[0]["retrieval"], "deterministic")
            self.assertEqual(b.snapshot()["last_reason"],
                             "CapabilityUnavailable")

    def test_membership_never_changes_under_rerank(self):
        e = engine_with([("note", "visa appointment confirmed for tuesday"),
                         ("note", "schengen consulate paperwork finalized"),
                         ("note", "unrelated grocery list for the week")])
        b, p = make(e, embed_fn=lambda ts: [vec(1, 0)] * len(ts),
                    rerank_fn=lambda q, ps: [0.01 * i for i in range(len(ps))])
        out = semantic_find(e, cfg(), b, p, "owner1", "visa appointment", T0,
                            limit=10)
        rids = sorted(m["rid"] for m in out)
        # union = 1 deterministic + 2 semantic (all records embedded equal)
        self.assertEqual(len(rids), 3)

    def test_single_candidate_skips_rerank(self):
        e = engine_with([("note", "visa appointment confirmed for tuesday")])
        calls = []
        b, p = make(e, rerank_fn=lambda q, ps: calls.append(1) or [0.9])
        out = semantic_find(e, cfg(caps=("rerank",)), b, p, "owner1",
                            "visa appointment", T0)
        self.assertEqual(calls, [])


class CapAndLimitTests(unittest.TestCase):
    def test_embed_cap_bounds_records_embedded(self):
        recs = [("note", "filler record number %d about nothing" % i)
                for i in range(MAX_EMBED_RECORDS + 10)]
        e = engine_with(recs)
        sizes = []
        b, p = make(e, embed_fn=lambda ts: sizes.append(len(ts)) or [vec(1, 0)] * len(ts))
        semantic_find(e, cfg(caps=("embed",)), b, p, "owner1",
                      "visa appointment", T0)
        self.assertEqual(sizes, [MAX_EMBED_RECORDS + 1])  # +1 for the query

    def test_embed_cap_prefers_recent_records(self):
        recs = [("note", "old record %d" % i) for i in range(MAX_EMBED_RECORDS)]
        e = engine_with(recs)
        e.add("owner1", "note", "newest schengen consulate paperwork", T0 + 10000)
        b, p = make(e, embed_fn=lambda ts: [vec(1, 0)] * len(ts))
        out = semantic_find(e, cfg(caps=("embed",)), b, p, "owner1",
                            "visa appointment", T0)
        self.assertTrue(any(m["snippet"].startswith("newest") for m in out))

    def test_limit_trims_output(self):
        recs = [("note", "schengen consulate variant %d paperwork" % i)
                for i in range(8)]
        e = engine_with(recs)
        b, p = make(e, embed_fn=lambda ts: [vec(1, 0)] * len(ts))
        out = semantic_find(e, cfg(caps=("embed",)), b, p, "owner1",
                            "visa appointment", T0, limit=3)
        self.assertEqual(len(out), 3)


if __name__ == "__main__":
    unittest.main()


class RoundNineReviewTests(unittest.TestCase):
    def test_rerank_never_drops_deterministic_match(self):
        # round-9 blocker (peer probe): 10 deterministic matches at the
        # limit + 1 semantic addition ranked top by the reranker - the
        # rerank used to push base rec1 out of union[:limit]. Selection
        # now happens BEFORE rerank, so membership is fixed and every
        # deterministic match survives.
        recs = [("note", "visa appointment number %d details" % i)
                for i in range(10)]
        recs.append(("note", "embassy interview slot booked"))
        e = engine_with(recs)

        def embed(texts):
            return [vec(1, 0) if "visa appointment" in t or
                    "embassy" in t else vec(0, 1) for t in texts]

        def rerank(query, passages):
            return [0.99 if "embassy" in p else 0.5 for p in passages]

        b, p = make(e, embed_fn=embed, rerank_fn=rerank)
        base = e.find("owner1", "visa appointment", T0, limit=10)
        self.assertEqual(len(base), 10)
        out = semantic_find(e, cfg(), b, p, "owner1", "visa appointment",
                            T0, limit=10)
        out_rids = {m["rid"] for m in out}
        for m in base:
            self.assertIn(m["rid"], out_rids)

    def test_rerank_reorders_within_window_only(self):
        # 3 base + 1 semantic addition, limit 5: the addition joins the
        # window and the reranker may put it first - membership of the
        # selected set is unchanged by the rerank
        recs = [("note", "visa appointment number %d details" % i)
                for i in range(3)]
        recs.append(("note", "embassy interview slot booked"))
        e = engine_with(recs)

        def embed(texts):
            return [vec(1, 0) if "visa appointment" in t or
                    "embassy" in t else vec(0, 1) for t in texts]

        def rerank(query, passages):
            return [0.99 if "embassy" in p else 0.5 for p in passages]

        b, p = make(e, embed_fn=embed, rerank_fn=rerank)
        out = semantic_find(e, cfg(), b, p, "owner1", "visa appointment",
                            T0, limit=5)
        self.assertEqual(len(out), 4)
        self.assertEqual(out[0]["retrieval"], "semantic")
        self.assertEqual(sum(1 for m in out
                             if m["retrieval"] == "deterministic"), 3)

    def test_consent_gate_fail_closed(self):
        # round-9 non-blocking fix: without explicit consent the
        # provider NEVER sees a personal record - default, False, and
        # any non-True value all return exactly the deterministic base
        e = engine_with([("note", "visa appointment tomorrow")])
        calls = []

        def embed(texts):
            calls.append(list(texts))
            return [vec(1, 0) for _ in texts]

        for kwargs in ({}, {"consent": False}, {"consent": 1},
                       {"consent": "yes"}, {"consent": None}):
            calls.clear()
            b, p = make(e, embed_fn=embed, caps=("embed",))
            out = _raw_semantic_find(e, cfg(caps=("embed",)), b, p,
                                     "owner1", "visa appointment", T0,
                                     **kwargs)
            self.assertEqual(calls, [], kwargs)
            self.assertEqual([m["rid"] for m in out],
                             [m["rid"] for m in
                              e.find("owner1", "visa appointment", T0)],
                             kwargs)

    def test_consent_true_enables_semantic_path(self):
        e = engine_with([("note", "embassy interview slot booked")])
        calls = []

        def embed(texts):
            calls.append(list(texts))
            return [vec(1, 0) for _ in texts]

        b, p = make(e, embed_fn=embed, caps=("embed",))
        out = _raw_semantic_find(e, cfg(caps=("embed",)), b, p,
                                 "owner1", "embassy", T0, consent=True)
        self.assertTrue(calls)
        self.assertTrue(out)


    def test_backend_models_error_drops_not_crashes(self):
        # round-9 (review 5480642386): a backend raising ModelsError is
        # a backend failure - the deterministic base comes back, the
        # error is counted on the breaker, and semantic_find NEVER
        # propagates it
        from models import ModelsError
        e = engine_with([("note", "visa appointment tomorrow"),
                         ("note", "embassy interview slot booked")])

        def bad_embed(texts):
            raise ModelsError("backend exploded")

        def bad_rerank(query, passages):
            raise ModelsError("backend exploded")

        base = e.find("owner1", "visa appointment", T0)
        b, p = make(e, embed_fn=bad_embed)
        out = semantic_find(e, cfg(), b, p, "owner1", "visa appointment",
                            T0)
        self.assertEqual([m["rid"] for m in out],
                         [m["rid"] for m in base])
        b2, p2 = make(e, embed_fn=lambda t: [vec(1, 0) for _ in t],
                      rerank_fn=bad_rerank)
        out2 = semantic_find(e, cfg(), b2, p2, "owner1",
                             "visa appointment", T0)
        self.assertTrue(out2)  # base still answered, no crash
