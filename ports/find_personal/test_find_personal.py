import sys, os, unittest
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from find_personal import (FindPersonal, MemPersonalStore,
                           FindPersonalError, RecordLimitReached,
                           MAX_RECORDS, MAX_KIND, MAX_TEXT, MAX_QUERY,
                           MAX_RESULTS)

T0 = 1791633600  # 2026-10-10T12:00:00Z
DAY = 86400


def ids(prefix, seq, now):
    return "%s_t%04d" % (prefix, seq)


def make():
    return FindPersonal(MemPersonalStore(), id_factory=ids)


class AddTests(unittest.TestCase):
    def test_add_returns_id_and_counts(self):
        f = make()
        rid = f.add("u1", "note", "visa appointment moved to friday", T0)
        self.assertEqual(rid, "rec_t0001")
        self.assertEqual(f.count("u1"), 1)

    def test_overflow_rejects_zero_effects(self):
        f = make()
        self.assertRaises(FindPersonalError, f.add, "u1", "k" * (MAX_KIND + 1), "text", T0)
        self.assertRaises(FindPersonalError, f.add, "u1", "note", "x" * (MAX_TEXT + 1), T0)
        self.assertEqual(f.count("u1"), 0)

    def test_exact_max_accepted(self):
        f = make()
        f.add("u1", "k" * MAX_KIND, "x" * MAX_TEXT, T0)
        self.assertEqual(f.count("u1"), 1)

    def test_empty_fields_reject(self):
        f = make()
        self.assertRaises(FindPersonalError, f.add, "", "note", "text", T0)
        self.assertRaises(FindPersonalError, f.add, "u1", "  ", "text", T0)
        self.assertRaises(FindPersonalError, f.add, "u1", "note", " ", T0)

    def test_cap_refuses_never_evicts(self):
        f = make()
        first = f.add("u1", "note", "record zero", T0)
        for i in range(1, MAX_RECORDS):
            f.add("u1", "note", "record %d" % i, T0 + i)
        self.assertRaises(RecordLimitReached, f.add, "u1", "note", "one more", T0)
        self.assertEqual(f.count("u1"), MAX_RECORDS)
        self.assertIn(first, f._records("u1"))  # first record not evicted


class FindTests(unittest.TestCase):
    def _seed(self, f):
        f.add("u1", "note", "visa appointment moved to friday afternoon", T0)
        f.add("u1", "link", "saved article about schengen visa fees", T0 + DAY)
        f.add("u1", "note", "gym routine updated with squats", T0 + 2 * DAY)

    def test_basic_match_with_evidence(self):
        f = make(); self._seed(f)
        r = f.find("u1", "visa appointment", T0)
        self.assertTrue(r)
        self.assertEqual(r[0]["kind"], "note")
        self.assertIn("rid", r[0])
        self.assertIn("visa", r[0]["snippet"].lower())
        self.assertIn("ts", r[0])

    def test_ranking_best_overlap_first(self):
        f = make(); self._seed(f)
        r = f.find("u1", "visa", T0)
        kinds = [m["kind"] for m in r]
        self.assertEqual(len(r), 2)
        self.assertEqual(set(kinds), {"note", "link"})

    def test_no_match_empty(self):
        f = make(); self._seed(f)
        self.assertEqual(f.find("u1", "kubernetes deployment", T0), [])

    def test_stopword_only_query_empty(self):
        f = make(); self._seed(f)
        self.assertEqual(f.find("u1", "what is the", T0), [])

    def test_query_overflow_rejects(self):
        f = make()
        self.assertRaises(FindPersonalError, f.find, "u1", "x" * (MAX_QUERY + 1), T0)

    def test_limit_bounds(self):
        f = make(); self._seed(f)
        self.assertRaises(FindPersonalError, f.find, "u1", "visa", T0, limit=0)
        self.assertRaises(FindPersonalError, f.find, "u1", "visa", T0, limit=MAX_RESULTS + 1)
        self.assertLessEqual(len(f.find("u1", "visa", T0, limit=1)), 1)

    def test_recency_tiebreak(self):
        f = make()
        f.add("u1", "note", "passport renewal form", T0)
        f.add("u1", "note", "passport renewal fee", T0 + DAY)
        r = f.find("u1", "passport renewal", T0)
        self.assertEqual(r[0]["ts"], T0 + DAY)  # newer first on equal score

    def test_determinism(self):
        a, b = make(), make()
        for f in (a, b):
            f.add("u1", "note", "alpha beta gamma", T0)
            f.add("u1", "note", "alpha beta delta", T0 + DAY)
        self.assertEqual(a.find("u1", "alpha beta", T0), b.find("u1", "alpha beta", T0))


class RoundOneRegressionTests(unittest.TestCase):
    def test_ts_none_rejected_zero_effects(self):
        f = make()
        for bad in (None, "now", float("nan"), float("inf"), float("-inf"), True):
            self.assertRaises(FindPersonalError, f.add, "u1", "note", "text", bad)
        self.assertEqual(f.count("u1"), 0)

    def test_id_collision_refused_no_loss(self):
        f = FindPersonal(MemPersonalStore(), id_factory=lambda p, s, n: "same")
        f.add("u1", "note", "first record", T0)
        self.assertRaises(FindPersonalError, f.add, "u1", "note", "second record", T0)
        self.assertEqual(f.count("u1"), 1)
        self.assertEqual(f.find("u1", "first record", T0)[0]["rid"], "same")

    def test_metadata_only_match_named_not_scored(self):
        f = make()
        f.add("u1", "note", "completely unrelated text", T0, source="visa")
        r = f.find("u1", "visa", T0)
        self.assertEqual(len(r), 1)
        self.assertEqual(r[0]["score"], 0.0)
        self.assertEqual(r[0]["matched_fields"], ["source"])
        self.assertIsNone(r[0]["snippet"])  # no text evidence to quote

    def test_text_match_names_fields_and_outranks_metadata(self):
        f = make()
        f.add("u1", "note", "completely unrelated text", T0, source="visa")
        f.add("u1", "note", "visa fees went up", T0 + DAY)
        r = f.find("u1", "visa", T0)
        self.assertEqual(r[0]["matched_fields"], ["text"])
        self.assertGreater(r[0]["score"], 0.0)
        self.assertEqual(r[1]["matched_fields"], ["source"])

    def test_remove_one_record(self):
        f = make()
        rid = f.add("u1", "note", "temporary record", T0)
        self.assertTrue(f.remove("u1", rid))
        self.assertFalse(f.remove("u1", rid))  # already gone
        self.assertEqual(f.find("u1", "temporary record", T0), [])

    def test_remove_owner_strict(self):
        f = make()
        rid = f.add("u1", "note", "mine", T0)
        self.assertFalse(f.remove("u2", rid))
        self.assertEqual(f.count("u1"), 1)


class OwnerStrictTests(unittest.TestCase):
    def test_second_owner_isolated(self):
        f = make()
        f.add("u1", "note", "private medical appointment", T0)
        self.assertEqual(f.find("u2", "medical appointment", T0), [])
        self.assertEqual(f.count("u2"), 0)

    def test_wipe_owner_strict(self):
        f = make()
        f.add("u1", "note", "keep this record", T0)
        f.add("u2", "note", "delete this record", T0)
        n = f.wipe("u2")
        self.assertEqual(n, 1)
        self.assertEqual(f.count("u2"), 0)
        self.assertEqual(f.count("u1"), 1)
        self.assertEqual(f.find("u1", "keep record", T0)[0]["kind"], "note")

    def test_wipe_empty_zero(self):
        f = make()
        self.assertEqual(f.wipe("u1"), 0)


if __name__ == "__main__":
    unittest.main()
