import sys, os, unittest
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from find_track import (FindTrack, MemTrackStore, TrackError,
                        TrackLimitReached, DiffLimitReached, KnownLimitReached, NoMatch,
                        canonical_url, MAX_TRACKS, MAX_TOPIC,
                        MAX_DIFF_ITEMS, MAX_KNOWN_ITEMS, MAX_RUN_ITEMS, MAX_URL, MAX_TITLE)

T0 = 1791633600
HOUR = 3600


def ids(prefix, seq, now):
    return "%s_t%03d" % (prefix, seq)


def make():
    return FindTrack(MemTrackStore(), id_factory=ids)


def item(n, host="news.example"):
    return {"url": "https://%s/story-%d" % (host, n), "title": "Story %d" % n}


class AddRemoveTests(unittest.TestCase):
    def test_add_and_list(self):
        f = make()
        tid = f.add("u1", "india ai policy", T0)
        self.assertEqual(tid, "trk_t001")
        self.assertEqual(f.list("u1")[0]["topic"], "india ai policy")

    def test_topic_overflow_rejects(self):
        f = make()
        self.assertRaises(TrackError, f.add, "u1", "x" * (MAX_TOPIC + 1), T0)
        self.assertEqual(f.list("u1"), [])

    def test_exact_max_topic_accepted(self):
        f = make()
        f.add("u1", "x" * MAX_TOPIC, T0)
        self.assertEqual(len(f.list("u1")), 1)

    def test_empty_topic_rejects(self):
        f = make()
        self.assertRaises(TrackError, f.add, "u1", "  ", T0)

    def test_track_cap_refuses(self):
        f = make()
        for i in range(MAX_TRACKS):
            f.add("u1", "topic %d" % i, T0)
        self.assertRaises(TrackLimitReached, f.add, "u1", "one more", T0)

    def test_remove_and_missing(self):
        f = make()
        tid = f.add("u1", "t", T0)
        f.remove("u1", tid)
        self.assertEqual(f.list("u1"), [])
        self.assertRaises(NoMatch, f.remove, "u1", tid)
        self.assertRaises(NoMatch, f.run, "u1", tid, [], T0)


class DiffTests(unittest.TestCase):
    def test_first_run_is_baseline_no_diff(self):
        # round-3: the first accepted run SEEDS the known set without
        # diffing - returns an explicit baseline fact, never a diff
        f = make()
        tid = f.add("u1", "t", T0)
        d = f.run("u1", tid, [item(1), item(2)], T0 + HOUR)
        self.assertEqual(d, {"tid": tid, "topic": "t", "baseline": True,
                             "seeded": 2, "new": []})
        self.assertEqual(f.list("u1")[0]["known"], 2)
        self.assertTrue(f.list("u1")[0]["seeded"])

    def test_first_run_over_diff_cap_is_baseline_not_deadlock(self):
        # round-3 regression: an initial result set bigger than
        # MAX_DIFF_ITEMS is a baseline, NOT DiffLimitReached
        f = make()
        tid = f.add("u1", "t", T0)
        d = f.run("u1", tid, [item(i) for i in range(MAX_DIFF_ITEMS + 5)],
                  T0 + HOUR)
        self.assertTrue(d["baseline"])
        self.assertEqual(d["seeded"], MAX_DIFF_ITEMS + 5)
        self.assertEqual(f.list("u1")[0]["known"], MAX_DIFF_ITEMS + 5)

    def test_second_run_same_set_no_diff(self):
        f = make()
        tid = f.add("u1", "t", T0)
        f.run("u1", tid, [item(1)], T0 + HOUR)
        self.assertIsNone(f.run("u1", tid, [item(1)], T0 + 2 * HOUR))

    def test_only_new_items_reported(self):
        f = make()
        tid = f.add("u1", "t", T0)
        f.run("u1", tid, [item(1)], T0 + HOUR)
        d = f.run("u1", tid, [item(1), item(2)], T0 + 2 * HOUR)
        self.assertEqual(len(d["new"]), 1)
        self.assertEqual(d["new"][0]["canonical"], "https://news.example/story-2")

    def test_items_carry_first_seen(self):
        f = make()
        tid = f.add("u1", "t", T0)
        f.run("u1", tid, [item(1)], T0 + HOUR)  # baseline
        d2 = f.run("u1", tid, [item(1), item(2)], T0 + 2 * HOUR)
        self.assertEqual(d2["new"][0]["first_seen"], T0 + 2 * HOUR)

    def test_run_cap_refuses_zero_effects(self):
        f = make()
        tid = f.add("u1", "t", T0)
        self.assertRaises(TrackError, f.run, "u1", tid,
                          [item(i) for i in range(MAX_RUN_ITEMS + 1)], T0)
        self.assertIsNone(f.list("u1")[0]["last_run_at"])

    def test_atomic_run_bad_item_refuses_all(self):
        f = make()
        tid = f.add("u1", "t", T0)
        bad = [item(1), {"url": "notaurl"}, item(2)]
        self.assertRaises(TrackError, f.run, "u1", tid, bad, T0)
        self.assertEqual(f.list("u1")[0]["known"], 0)

    def test_title_overflow_rejects(self):
        f = make()
        tid = f.add("u1", "t", T0)
        self.assertRaises(TrackError, f.run, "u1", tid,
                          [{"url": "https://a.com/x", "title": "t" * (MAX_TITLE + 1)}], T0)

    def test_url_overflow_rejects(self):
        f = make()
        tid = f.add("u1", "t", T0)
        self.assertRaises(TrackError, f.run, "u1", tid,
                          [{"url": "https://a.com/" + "x" * MAX_URL}], T0)

    def test_malformed_port_rejects_cleanly(self):
        f = make()
        tid = f.add("u1", "t", T0)
        self.assertRaises(TrackError, f.run, "u1", tid,
                          [{"url": "https://a.example:bad/x"}], T0)

    def test_diff_cap_refuses_whole_run(self):
        f = make()
        tid = f.add("u1", "t", T0)
        f.run("u1", tid, [item(1000)], T0)  # baseline
        before = f.list("u1")[0]["known"]
        self.assertRaises(DiffLimitReached, f.run, "u1", tid,
                          [item(i) for i in range(MAX_DIFF_ITEMS + 1)], T0)
        self.assertEqual(f.list("u1")[0]["known"], before)  # nothing committed

    def test_dedup_within_run_canonical(self):
        f = make()
        tid = f.add("u1", "t", T0)
        d = f.run("u1", tid, [{"url": "https://www.a.com/x?utm_source=n#f"},
                              {"url": "https://a.com/x"}], T0)
        self.assertEqual(d["seeded"], 1)  # duplicates collapse even at baseline
        self.assertEqual(f.list("u1")[0]["known"], 1)

    def test_scheme_port_are_identity(self):
        f = make()
        tid = f.add("u1", "t", T0)
        d = f.run("u1", tid, [{"url": "http://a.example:8080/x"},
                              {"url": "https://a.example:9090/x"}], T0)
        self.assertEqual(d["seeded"], 2)  # distinct resources

    def test_ipv6_item(self):
        f = make()
        tid = f.add("u1", "t", T0)
        f.run("u1", tid, [{"url": "https://a.com/seed"}], T0)  # baseline
        d = f.run("u1", tid, [{"url": "https://a.com/seed"},
                              {"url": "https://[::1]:8080/x"}], T0)
        self.assertEqual(d["new"][0]["canonical"], "https://[::1]:8080/x")

    def test_empty_first_run_is_no_baseline(self):
        # round-4: an empty run on an unseeded track is a no-op - it
        # must NOT seed, or the first real result set would deadlock
        f = make()
        tid = f.add("u1", "t", T0)
        self.assertIsNone(f.run("u1", tid, [], T0 + HOUR))
        self.assertFalse(f.list("u1")[0]["seeded"])
        self.assertEqual(f.list("u1")[0]["known"], 0)
        # the first NON-empty run still baselines, even over diff cap
        d = f.run("u1", tid, [item(i) for i in range(MAX_DIFF_ITEMS + 1)],
                  T0 + 2 * HOUR)
        self.assertTrue(d["baseline"])
        self.assertEqual(d["seeded"], MAX_DIFF_ITEMS + 1)

    def test_empty_run_after_baseline_is_noop(self):
        f = make()
        tid = f.add("u1", "t", T0)
        f.run("u1", tid, [item(1)], T0 + HOUR)  # baseline
        self.assertIsNone(f.run("u1", tid, [], T0 + 2 * HOUR))
        self.assertEqual(f.list("u1")[0]["known"], 1)

    def test_known_cap_unreachable_in_one_run(self):
        # the run cap (50) is strictly below the known cap (500), so a
        # single run can never breach the known cap - the run cap fires
        # first; the known cap binds across runs (tested below)
        f = make()
        tid = f.add("u1", "t", T0)
        self.assertLess(MAX_RUN_ITEMS, MAX_KNOWN_ITEMS)
        self.assertRaises(TrackError, f.run, "u1", tid,
                          [item(i) for i in range(MAX_RUN_ITEMS + 1)], T0)
        self.assertFalse(f.list("u1")[0]["seeded"])

    def test_known_cap_refuses_across_runs(self):
        f = make()
        tid = f.add("u1", "t", T0)
        f.run("u1", tid, [item(i) for i in range(MAX_RUN_ITEMS)], T0)  # baseline
        n = MAX_RUN_ITEMS
        while n < MAX_KNOWN_ITEMS:
            f.run("u1", tid, [item(i) for i in range(n, n + 10)], T0)
            n += 10
        before = f.list("u1")[0]["known"]
        self.assertRaises(KnownLimitReached, f.run, "u1", tid,
                          [item(i) for i in range(n, n + 10)], T0)
        self.assertEqual(f.list("u1")[0]["known"], before)  # zero effects

    def test_determinism(self):
        a, b = make(), make()
        out = []
        for f in (a, b):
            tid = f.add("u1", "t", T0)
            f.run("u1", tid, [item(1), item(2)], T0 + HOUR)
            out.append(f.run("u1", tid, [item(2), item(3)], T0 + 2 * HOUR))
        self.assertEqual(out[0], out[1])
        self.assertEqual(a.list("u1"), b.list("u1"))

    def test_returned_diff_is_a_snapshot(self):
        f = make()
        tid = f.add("u1", "t", T0)
        d = f.run("u1", tid, [item(1)], T0)
        d["new"].clear()
        self.assertEqual(f.list("u1")[0]["known"], 1)


class IdTests(unittest.TestCase):
    def test_remove_add_never_reuses_ids(self):
        f = make()
        t1 = f.add("u1", "one", T0)
        t2 = f.add("u1", "two", T0)
        f.remove("u1", t1)
        t3 = f.add("u1", "three", T0)
        self.assertNotIn(t3, (t1, t2))
        self.assertEqual([x["tid"] for x in f.list("u1")], [t2, t3])

    def test_injected_collision_refused_no_loss(self):
        f = FindTrack(MemTrackStore(), id_factory=lambda p, s, n: "fixed")
        f.add("u1", "first", T0)
        self.assertRaises(TrackError, f.add, "u1", "second", T0)
        self.assertEqual([x["topic"] for x in f.list("u1")], ["first"])


class OwnerStrictTests(unittest.TestCase):
    def test_second_owner_isolated(self):
        f = make()
        tid = f.add("u1", "private topic", T0)
        f.run("u1", tid, [item(1)], T0)
        self.assertEqual(f.list("u2"), [])
        self.assertRaises(NoMatch, f.run, "u2", tid, [item(2)], T0)


class CanonicalTests(unittest.TestCase):
    def test_rules(self):
        self.assertEqual(canonical_url("HTTPS://WWW.A.com/B/?utm_x=1&keep=2#f"),
                         "https://a.com/B?keep=2")
        self.assertRaises(TrackError, canonical_url, "ftp://x")
        self.assertRaises(TrackError, canonical_url, "https://a.example:bad/x")


if __name__ == "__main__":
    unittest.main()
