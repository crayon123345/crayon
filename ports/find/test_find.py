import sys, os, unittest
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from find import (Find, MemFindStore, FindError, PlanLimitReached,
                  SourceLimitReached, parse_window, canonical_url,
                  MAX_QUERY, MAX_SUBQUERIES, MAX_CLAIMS, MAX_SOURCES,
                  MAX_PLANS_PER_OWNER, DAY)

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


class WindowTests(unittest.TestCase):
    def test_today(self):
        day0 = int(T0 // DAY) * DAY
        self.assertEqual(parse_window("news today", T0), (day0, day0 + DAY))

    def test_yesterday(self):
        day0 = int(T0 // DAY) * DAY
        self.assertEqual(parse_window("what happened yesterday", T0),
                         (day0 - DAY, day0))

    def test_latest_is_7d(self):
        day0 = int(T0 // DAY) * DAY
        self.assertEqual(parse_window("latest ai news", T0),
                         (day0 - 7 * DAY, day0 + DAY))

    def test_explicit_year(self):
        w = parse_window("budget announcements in 2025", T0)
        import calendar
        self.assertEqual(w, (calendar.timegm((2025,1,1,0,0,0,0,1,0)),
                             calendar.timegm((2026,1,1,0,0,0,0,1,0))))

    def test_no_window(self):
        self.assertIsNone(parse_window("python tuple syntax", T0))


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
        self.assertEqual(len(calls), 1)  # no retry
        self.assertEqual(r["dropped"][0]["reason"], "fetch_failed")

    def test_bad_url_dropped(self):
        f = make(); p = self._plan(f)
        r = f.verify("u1", p.pid, [{"text": "x y z", "url": "ftp://nope"}], fetch_ok("x"), T0)
        self.assertEqual(r["dropped"][0]["reason"], "bad_url")

    def test_claim_batch_cap_refuses_zero_effects(self):
        f = make(); p = self._plan(f)
        claims = [{"text": "t%d content" % i, "url": "https://a.com/%d" % i} for i in range(MAX_CLAIMS + 1)]
        self.assertRaises(FindError, f.verify, "u1", p.pid, claims, fetch_ok("t"), T0)
        self.assertEqual(p.verified, [])  # zero effects

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

    def test_source_cap_refuses(self):
        f = make(); p = self._plan(f)
        claims = [{"text": "shared topic words", "url": "https://s%d.com/a" % i}
                  for i in range(MAX_SOURCES + 1)]
        self.assertRaises(SourceLimitReached, f.verify, "u1", p.pid, claims,
                          fetch_ok("shared topic words"), T0)

    def test_canonical_url_rules(self):
        self.assertEqual(canonical_url("HTTPS://WWW.A.com/B/?utm_x=1&gclid=z&keep=2#f"),
                         "https://a.com/B?keep=2")
        self.assertRaises(FindError, canonical_url, "notaurl")
        self.assertRaises(FindError, canonical_url, "x" * 501)


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


class DeterminismTests(unittest.TestCase):
    def test_same_input_same_plan(self):
        a, b = make(), make()
        pa = a.plan("u1", "compare x and y today", T0)
        pb = b.plan("u1", "compare x and y today", T0)
        self.assertEqual((pa.subqueries, pa.window), (pb.subqueries, pb.window))


if __name__ == "__main__":
    unittest.main()
