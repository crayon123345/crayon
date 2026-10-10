import sys, os, calendar, unittest
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from find import (Find, MemFindStore, FindError, PlanLimitReached,
                  SourceLimitReached, parse_window, reference_support,
                  MAX_QUERY, MAX_SUBQUERIES, MAX_CLAIMS, MAX_SOURCES,
                  MAX_PLANS_PER_OWNER, DAY)
from urlcanon import canonical_url, UrlCanonError

T0 = 1791633600  # 2026-10-10T12:00:00Z, a Saturday


def ids(prefix, seq, now):
    return "%s_t%03d" % (prefix, seq)


def make():
    return Find(MemFindStore(), id_factory=ids)


def fetch_ok(text):
    return lambda url: "page content about " + text + " and more detail here"


class PlanTests(unittest.TestCase):
    def test_basic_plan_single_subquery(self):
        f = make()
        p = f.plan("u1", "best budget phones", T0)
        self.assertEqual(p.subqueries, ["best budget phones"])
        self.assertIsNone(p.window)

    def test_compare_splits_two(self):
        f = make()
        p = f.plan("u1", "compare pixel 9a and iphone 16e", T0)
        self.assertEqual(p.subqueries, ["pixel 9a", "iphone 16e"])

    def test_empty_query_rejects(self):
        f = make()
        self.assertRaises(FindError, f.plan, "u1", "   ", T0)

    def test_overflow_query_rejects_zero_effects(self):
        f = make()
        before = f.export("u1")
        self.assertRaises(FindError, f.plan, "u1", "x" * (MAX_QUERY + 1), T0)
        self.assertEqual(f.export("u1"), before)

    def test_exact_max_query_accepted(self):
        f = make()
        p = f.plan("u1", "x" * MAX_QUERY, T0)
        self.assertEqual(len(p.query), MAX_QUERY)

    def test_fifth_subject_refuses_not_drops(self):
        f = make()
        self.assertRaises(FindError, f.plan, "u1",
                          "alpha; beta; gamma; delta; epsilon", T0)
        self.assertEqual(f.export("u1"), [])  # nothing planned

    def test_four_subjects_accepted(self):
        f = make()
        p = f.plan("u1", "alpha; beta; gamma; delta", T0)
        self.assertEqual(len(p.subqueries), 4)

    def test_injected_planner_validated(self):
        f = make()
        p = f.plan("u1", "q", T0, decompose_fn=lambda q: ["a", "b", "c"])
        self.assertEqual(p.subqueries, ["a", "b", "c"])
        for bad in ([], ["a"] * (MAX_SUBQUERIES + 1), [""], ["x" * 501], [42]):
            self.assertRaises(FindError, f.plan, "u1", "q", T0,
                              decompose_fn=lambda q, b=bad: b)

    def test_plan_cap_refuses(self):
        f = make()
        for i in range(MAX_PLANS_PER_OWNER):
            f.plan("u1", "q%d" % i, T0)
        self.assertRaises(PlanLimitReached, f.plan, "u1", "one more", T0)

    def test_owner_strict_plans(self):
        f = make()
        f.plan("u1", "mine", T0)
        self.assertEqual(len(f.export("u2")), 0)

    def test_bad_tz_rejects(self):
        f = make()
        self.assertRaises(FindError, f.plan, "u1", "q", T0, tz_offset_minutes="ist")
        self.assertRaises(FindError, f.plan, "u1", "q", T0, tz_offset_minutes=True)


class WindowTests(unittest.TestCase):
    def test_today(self):
        day0 = int(T0 // DAY) * DAY
        self.assertEqual(parse_window("news today", T0), (day0, day0 + DAY))

    def test_today_owner_local_ist(self):
        # 2026-10-10T18:30:00Z is already Oct 11 00:00 in IST (+330)
        now = 1791657000
        loc = now + 330 * 60
        day0 = int(loc // DAY) * DAY
        w = parse_window("today", now, tz_offset_minutes=330)
        self.assertEqual(w, (day0 - 330 * 60, day0 + DAY - 330 * 60))
        self.assertNotEqual(w, parse_window("today", now))  # UTC "today" differs

    def test_yesterday(self):
        day0 = int(T0 // DAY) * DAY
        self.assertEqual(parse_window("what happened yesterday", T0),
                         (day0 - DAY, day0))

    def test_this_week_vs_last_week_distinct(self):
        # T0 is Saturday; ISO week starts Monday
        g_day0 = int(T0 // DAY) * DAY
        import time as _t
        wday = _t.gmtime(T0).tm_wday
        w0 = g_day0 - wday * DAY
        self.assertEqual(parse_window("this week", T0), (w0, w0 + 7 * DAY))
        self.assertEqual(parse_window("last week", T0), (w0 - 7 * DAY, w0))
        self.assertEqual(parse_window("past week", T0), (T0 - 7 * DAY, T0))

    def test_this_month_vs_last_month_distinct(self):
        # T0 = Oct 10 2026
        self.assertEqual(parse_window("this month", T0),
                         (calendar.timegm((2026,10,1,0,0,0,0,1,0)),
                          calendar.timegm((2026,11,1,0,0,0,0,1,0))))
        self.assertEqual(parse_window("last month", T0),
                         (calendar.timegm((2026,9,1,0,0,0,0,1,0)),
                          calendar.timegm((2026,10,1,0,0,0,0,1,0))))

    def test_latest_is_now_anchored_rolling_7d(self):
        # exactly 7 days, anchored at now - no future upper bound
        self.assertEqual(parse_window("latest ai news", T0), (T0 - 7 * DAY, T0))
        self.assertEqual(parse_window("past month", T0), (T0 - 30 * DAY, T0))

    def test_explicit_year(self):
        w = parse_window("budget announcements in 2025", T0)
        self.assertEqual(w, (calendar.timegm((2025,1,1,0,0,0,0,1,0)),
                             calendar.timegm((2026,1,1,0,0,0,0,1,0))))

    def test_no_window(self):
        self.assertIsNone(parse_window("python tuple syntax", T0))


class SupportGateTests(unittest.TestCase):
    def test_contradiction_refused(self):
        self.assertFalse(reference_support(
            "The drug cures cancer",
            "The drug does not cure cancer in trials."))

    def test_negation_match_supported(self):
        self.assertTrue(reference_support(
            "The drug does not cure cancer",
            "The drug does not cure cancer in trials."))

    def test_numeric_mismatch_refused(self):
        self.assertFalse(reference_support(
            "Acme revenue was 999 million dollars",
            "Acme revenue was 10 million dollars this year."))

    def test_numeric_match_supported(self):
        self.assertTrue(reference_support(
            "Acme revenue was 999 million dollars",
            "Acme revenue was 999 million dollars this year."))

    def test_irrelevant_shared_tokens_refused(self):
        self.assertFalse(reference_support(
            "quantum breakthrough announced",
            "page content about cooking recipes and more detail here"))

    def test_synonym_contradiction_refused(self):
        # fail-closed: paraphrase is not quote evidence
        self.assertFalse(reference_support(
            "The drug cures cancer", "The drug causes cancer in trials."))

    def test_role_reversal_refused(self):
        self.assertFalse(reference_support(
            "Alice acquired Beta", "Beta acquired Alice last week."))

    def test_numeric_misattribution_refused(self):
        self.assertFalse(reference_support(
            "Acme revenue was 999 million dollars",
            "Acme revenue was 10 million dollars; its workforce was 999 people."))

    def test_plain_support(self):
        self.assertTrue(reference_support(
            "python released version", "page content about python released version"))

    def test_hedged_containment_refused(self):
        # verbatim containment inside a hedging frame is NOT support
        self.assertFalse(reference_support(
            "the drug cures cancer",
            "Claims that the drug cures cancer are false."))

    def test_hedge_parity_match_supported(self):
        self.assertTrue(reference_support(
            "officials deny the rumor",
            "officials deny the rumor in a statement today."))

    def test_numeric_continuation_refused(self):
        # the claim truncated a quantity expression mid-way
        self.assertFalse(reference_support(
            "revenue was 10 million",
            "revenue was 10 million dollars lower than expected."))

    def test_min_evidence_refused(self):
        self.assertFalse(reference_support("yes", "yes it does not"))
        self.assertFalse(reference_support("the", "the drug cures cancer."))
        self.assertFalse(reference_support("", "some content here"))

    def test_numeric_tokens_exact_separators(self):
        # round-4: NO unfolding - separators are load-bearing
        self.assertFalse(reference_support(
            "price 1,500", "the price 1.500 was listed."))
        self.assertFalse(reference_support(
            "price 1.500", "the price 1,500 was listed."))
        self.assertTrue(reference_support(
            "price 1,500", "the price 1,500 was listed."))
        self.assertFalse(reference_support(
            "price 1,500", "the price 2,500 was listed."))

    def test_numeric_tokens_exact_sign(self):
        self.assertFalse(reference_support(
            "temperature was 5 degrees", "temperature was -5 degrees overnight."))
        self.assertFalse(reference_support(
            "temperature was -5 degrees", "temperature was 5 degrees today."))
        self.assertTrue(reference_support(
            "temperature was -5 degrees", "temperature was -5 degrees overnight."))

    def test_numeric_tokens_exact_percent(self):
        self.assertFalse(reference_support(
            "the stock rose 5", "the stock rose 5% today."))
        self.assertFalse(reference_support(
            "the stock rose 5%", "the stock rose 5 today."))
        self.assertTrue(reference_support(
            "the stock rose 5%", "the stock rose 5% today."))

    def test_numeric_tokens_exact_range_vs_decimal(self):
        self.assertFalse(reference_support(
            "rates rose 3.5 percent", "rates rose 3-5 percent last year."))
        self.assertFalse(reference_support(
            "rates rose 3-5 percent", "rates rose 3.5 percent last year."))
        self.assertTrue(reference_support(
            "rates rose 3-5 percent", "rates rose 3-5 percent last year."))

    def test_quote_spanning_sentences_refused(self):
        self.assertFalse(reference_support(
            "alpha beta gamma", "alpha beta. gamma arrives."))


class VerifyTests(unittest.TestCase):
    def _plan(self, f):
        return f.plan("u1", "q", T0)

    def test_supported_claim_survives(self):
        f = make(); p = self._plan(f)
        r = f.verify("u1", p.pid, [{"text": "python released version", "url": "https://a.com/1"}],
                     fetch_ok("python released version"), T0)
        self.assertEqual(len(r["verified"]), 1)
        self.assertEqual(len(r["dropped"]), 0)

    def test_unsupported_claim_dropped(self):
        f = make(); p = self._plan(f)
        r = f.verify("u1", p.pid, [{"text": "quantum breakthrough announced", "url": "https://a.com/1"}],
                     fetch_ok("cooking recipes"), T0)
        self.assertEqual(len(r["verified"]), 0)
        self.assertEqual(r["dropped"][0]["reason"], "unsupported")

    def test_fetch_failure_drops_without_retry(self):
        f = make(); p = self._plan(f)
        calls = []
        def bad(url):
            calls.append(url)
            raise RuntimeError("boom")
        r = f.verify("u1", p.pid, [{"text": "anything here", "url": "https://a.com/1"}], bad, T0)
        self.assertEqual(len(calls), 1)
        self.assertEqual(r["dropped"][0]["reason"], "fetch_failed")

    def test_bad_url_dropped(self):
        f = make(); p = self._plan(f)
        r = f.verify("u1", p.pid, [{"text": "x y z", "url": "ftp://nope"}], fetch_ok("x"), T0)
        self.assertEqual(r["dropped"][0]["reason"], "bad_url")

    def test_support_fn_injected(self):
        f = make(); p = self._plan(f)
        seen = []
        def gate(claim, content):
            seen.append(claim)
            return False  # gate refuses everything
        r = f.verify("u1", p.pid, [{"text": "python released version", "url": "https://a.com/1"}],
                     fetch_ok("python released version"), T0, support_fn=gate)
        self.assertEqual(seen, ["python released version"])
        self.assertEqual(len(r["verified"]), 0)
        self.assertEqual(r["dropped"][0]["reason"], "unsupported")

    def test_support_fn_error_drops_recorded(self):
        f = make(); p = self._plan(f)
        def boom(claim, content):
            raise RuntimeError("gate down")
        r = f.verify("u1", p.pid, [{"text": "python released version", "url": "https://a.com/1"}],
                     fetch_ok("python released version"), T0, support_fn=boom)
        self.assertEqual(r["dropped"][0]["reason"], "support_error")

    def test_claim_batch_cap_refuses_zero_effects(self):
        f = make(); p = self._plan(f)
        claims = [{"text": "t%d content" % i, "url": "https://a.com/%d" % i} for i in range(MAX_CLAIMS + 1)]
        self.assertRaises(FindError, f.verify, "u1", p.pid, claims, fetch_ok("t"), T0)
        self.assertEqual(p.verified, [])

    def test_claim_overflow_rejects(self):
        f = make(); p = self._plan(f)
        self.assertRaises(FindError, f.verify, "u1", p.pid,
                          [{"text": "x" * 1001, "url": "https://a.com"}], fetch_ok("x"), T0)
        self.assertRaises(FindError, f.verify, "u1", p.pid,
                          [{"text": "ok", "url": "https://a.com/" + "x" * 500}], fetch_ok("ok"), T0)

    def test_sources_dedup_canonical(self):
        f = make(); p = self._plan(f)
        claims = [
            {"text": "python released version", "url": "https://www.a.com/x?utm_source=news#frag", "title": "T"},
            {"text": "python released version", "url": "https://a.com/x", "title": "T"},
        ]
        r = f.verify("u1", p.pid, claims, fetch_ok("python released version"), T0)
        self.assertEqual(len(r["sources"]), 1)
        self.assertEqual(r["sources"][0]["canonical"], "https://a.com/x")

    def test_source_cap_atomic_zero_effects(self):
        f = make(); p = self._plan(f)
        # seed a valid state: one verified source
        f.verify("u1", p.pid, [{"text": "python released version", "url": "https://a.com/1"}],
                 fetch_ok("python released version"), T0)
        snap = (list(p.claims), list(p.verified), list(p.dropped),
                list(p.sources), list(f.session("u1")))
        # now an over-cap batch: MAX_SOURCES+1 distinct sources
        claims = [{"text": "shared topic words", "url": "https://s%d.com/a" % i}
                  for i in range(MAX_SOURCES + 1)]
        self.assertRaises(SourceLimitReached, f.verify, "u1", p.pid, claims,
                          fetch_ok("shared topic words"), T0)
        after = (list(p.claims), list(p.verified), list(p.dropped),
                 list(p.sources), list(f.session("u1")))
        self.assertEqual(snap, after)  # prior state byte-identical

    def test_canonical_url_rules(self):
        self.assertEqual(canonical_url("HTTPS://WWW.A.com/B/?utm_x=1&gclid=z&keep=2#f"),
                         "https://a.com/B?keep=2")
        self.assertRaises(UrlCanonError, canonical_url, "notaurl")
        self.assertRaises(UrlCanonError, canonical_url, "x" * 501)

    def test_malformed_port_recorded_bad_url(self):
        f = make(); p = f.plan("u1", "q", T0)
        r = f.verify("u1", p.pid, [{"text": "x y z", "url": "https://a.example:bad/x"}],
                     fetch_ok("x y z"), T0)
        self.assertEqual(r["dropped"][0]["reason"], "bad_url")  # no ValueError escape

    def test_ipv6_brackets_preserved(self):
        self.assertEqual(canonical_url("https://[::1]:8080/x"), "https://[::1]:8080/x")
        self.assertEqual(canonical_url("https://[2001:db8::1]/a"), "https://[2001:db8::1]/a")

    def test_scheme_and_port_are_identity(self):
        a = canonical_url("http://a.example:8080/x")
        b = canonical_url("https://a.example:9090/x")
        self.assertNotEqual(a, b)
        self.assertEqual(a, "http://a.example:8080/x")
        # default ports fold away
        self.assertEqual(canonical_url("https://a.example:443/x"), "https://a.example/x")
        self.assertEqual(canonical_url("http://a.example:80/x"), "http://a.example/x")
        # scheme alone still distinguishes
        self.assertNotEqual(canonical_url("http://a.example/x"),
                            canonical_url("https://a.example/x"))

    def test_query_order_and_blank_flag_equivalence(self):
        self.assertEqual(canonical_url("https://a.com/x?b=2&a=1"),
                         canonical_url("https://a.com/x?a=1&b=2"))
        self.assertEqual(canonical_url("https://a.com/x?id"),
                         canonical_url("https://a.com/x?id="))

    def test_host_trailing_dot_and_tilde_equivalence(self):
        self.assertEqual(canonical_url("https://example.com./x"),
                         canonical_url("https://example.com/x"))
        self.assertEqual(canonical_url("https://a.com/a%7Eb"),
                         canonical_url("https://a.com/a~b"))

    def test_stated_equivalence_limits(self):
        # interior duplicate slashes stay distinct
        self.assertNotEqual(canonical_url("https://a.com/x//y"),
                            canonical_url("https://a.com/x/y"))
        # encoded reserved chars stay encoded
        self.assertNotEqual(canonical_url("https://a.com/a%2Fb"),
                            canonical_url("https://a.com/a/b"))


class SessionTests(unittest.TestCase):
    def _seed(self, f):
        p = f.plan("u1", "q", T0)
        claims = [{"text": "topic alpha words", "url": "https://a.com/1", "title": "A"},
                  {"text": "topic beta words", "url": "https://b.com/2", "title": "B"}]
        def fe(url):
            return "topic alpha words" if "a.com" in url else "topic beta words"
        f.verify("u1", p.pid, claims, fe, T0)

    def test_ordinal_followup(self):
        f = make(); self._seed(f)
        r = f.resolve_reference("u1", "tell me more about the second one")
        self.assertEqual(r["index"], 2)
        self.assertEqual(r["source"]["canonical"], "https://b.com/2")

    def test_last_reference(self):
        f = make(); self._seed(f)
        r = f.resolve_reference("u1", "open the last result")
        self.assertEqual(r["index"], 2)

    def test_out_of_range_none(self):
        f = make(); self._seed(f)
        self.assertIsNone(f.resolve_reference("u1", "the ninth one"))

    def test_owner_strict_session(self):
        f = make(); self._seed(f)
        self.assertIsNone(f.resolve_reference("u2", "the first one"))
        self.assertEqual(f.session("u2"), [])

    def test_no_reference_none(self):
        f = make(); self._seed(f)
        self.assertIsNone(f.resolve_reference("u1", "what is the weather"))

    def test_time_words_are_not_ordinals(self):
        f = make(); self._seed(f)
        self.assertIsNone(f.resolve_reference("u1", "what happened last week"))
        self.assertIsNone(f.resolve_reference("u1", "news from last month"))
        # a real ordinal reference still resolves
        self.assertEqual(f.resolve_reference("u1", "open the last result")["index"], 2)

    def test_session_snapshot_mutation_safe(self):
        f = make(); self._seed(f)
        s = f.session("u1")
        s.clear()
        self.assertEqual(len(f.session("u1")), 2)


class DeterminismTests(unittest.TestCase):
    def test_same_input_same_plan(self):
        a, b = make(), make()
        pa = a.plan("u1", "compare x and y today", T0)
        pb = b.plan("u1", "compare x and y today", T0)
        self.assertEqual((pa.subqueries, pa.window), (pb.subqueries, pb.window))


if __name__ == "__main__":
    unittest.main()
