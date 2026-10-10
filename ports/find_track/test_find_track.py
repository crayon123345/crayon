import sys, os, unittest
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from find_track import (FindTrack, MemTrackStore, TrackError,
                        TrackLimitReached, ItemLimitReached, NoMatch,
                        canonical_url, MAX_TRACKS, MAX_TOPIC, MAX_ITEMS,
                        MAX_RUN_ITEMS, MAX_URL, MAX_TITLE)

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


class RunTests(unittest.TestCase):
    def test_first_run_raises_card_with_all_items(self):
        f = make()
        tid = f.add("u1", "t", T0)
        card = f.run("u1", tid, [item(1), item(2)], T0 + HOUR)
        self.assertEqual(len(card["items"]), 2)
        self.assertEqual(card["state"], "open")

    def test_second_run_no_new_no_card(self):
        f = make()
        tid = f.add("u1", "t", T0)
        f.run("u1", tid, [item(1)], T0 + HOUR)
        self.assertIsNone(f.run("u1", tid, [item(1)], T0 + 2 * HOUR))

    def test_new_items_extend_open_card(self):
        f = make()
        tid = f.add("u1", "t", T0)
        f.run("u1", tid, [item(1)], T0 + HOUR)
        card = f.run("u1", tid, [item(1), item(2)], T0 + 2 * HOUR)
        self.assertEqual(len(card["items"]), 2)

    def test_dismiss_then_only_fresh_items_reraise(self):
        f = make()
        tid = f.add("u1", "t", T0)
        f.run("u1", tid, [item(1)], T0 + HOUR)
        f.dismiss("u1", tid, T0 + 2 * HOUR)
        # same items again: dismissed set is known, no re-raise
        self.assertIsNone(f.run("u1", tid, [item(1)], T0 + 3 * HOUR))
        # genuinely new item re-raises a fresh card
        card = f.run("u1", tid, [item(1), item(2)], T0 + 4 * HOUR)
        self.assertEqual(len(card["items"]), 1)
        self.assertEqual(card["items"][0]["canonical"], "https://news.example/story-2")

    def test_dismiss_without_card_raises(self):
        f = make()
        tid = f.add("u1", "t", T0)
        self.assertRaises(NoMatch, f.dismiss, "u1", tid, T0)

    def test_run_cap_refuses_zero_effects(self):
        f = make()
        tid = f.add("u1", "t", T0)
        self.assertRaises(TrackError, f.run, "u1", tid,
                          [item(i) for i in range(MAX_RUN_ITEMS + 1)], T0)
        self.assertIsNone(f.list("u1")[0]["last_run_at"])  # zero effects

    def test_atomic_run_bad_item_refuses_all(self):
        f = make()
        tid = f.add("u1", "t", T0)
        bad = [item(1), {"url": "notaurl"}, item(2)]
        self.assertRaises(TrackError, f.run, "u1", tid, bad, T0)
        self.assertEqual(f.list("u1")[0]["known"], 0)  # item(1) NOT committed

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

    def test_evidence_cap_refuses_run(self):
        f = make()
        tid = f.add("u1", "t", T0)
        f.run("u1", tid, [item(i) for i in range(MAX_ITEMS)], T0 + HOUR)
        self.assertRaises(ItemLimitReached, f.run, "u1", tid,
                          [item(MAX_ITEMS)], T0 + 2 * HOUR)
        # refused run: the new item did not enter the known set
        self.assertEqual(f.list("u1")[0]["known"], MAX_ITEMS)

    def test_dedup_within_run_canonical(self):
        f = make()
        tid = f.add("u1", "t", T0)
        card = f.run("u1", tid, [{"url": "https://www.a.com/x?utm_source=n#f"},
                                 {"url": "https://a.com/x"}], T0)
        self.assertEqual(len(card["items"]), 1)

    def test_determinism(self):
        a, b = make(), make()
        for f in (a, b):
            tid = f.add("u1", "t", T0)
            f.run("u1", tid, [item(1), item(2)], T0 + HOUR)
            f.run("u1", tid, [item(2), item(3)], T0 + 2 * HOUR)
        self.assertEqual(a.card("u1", "trk_t001"), b.card("u1", "trk_t001"))
        self.assertEqual(a.list("u1"), b.list("u1"))


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


if __name__ == "__main__":
    unittest.main()
