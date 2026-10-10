import sys, os, json, unittest
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from patterns import (Patterns, MemPatternStore, Rule, PatternError,
                      ObservationLimitReached, CardLimitReached, NoMatch,
                      AmbiguousMatch, MAX_OBSERVATIONS, MAX_CARDS_PER_OWNER)

T0 = 1791633600  # 2026-10-10T12:00:00Z
DAY = 86400


def ids(prefix, seq, now):
    return "%s_t%03d" % (prefix, seq)


def make(rules=None):
    return Patterns(MemPatternStore(), rules=rules, id_factory=ids)


def rule(**kw):
    d = dict(rid="r1", kind="gym", window=7 * DAY, min_count=3,
             cooldown=3 * DAY, title="Often {kind} x{count}")
    d.update(kw)
    return Rule(**d)


class OptInTests(unittest.TestCase):
    def test_off_by_default_observe_noop(self):
        s = make()
        for i in range(10):
            self.assertIsNone(s.observe("v_a", "gym", ts=T0 + i))
        self.assertEqual(s.store.count_obs("v_a"), 0)
        self.assertEqual(s.scan("v_a", now=T0 + 100), [])

    def test_wipe_deletes_everything(self):
        s = make([rule()])
        s.enable("v_a")
        for i in range(3):
            s.observe("v_a", "gym", ts=T0 + i * DAY)
        s.scan("v_a", now=T0 + 3 * DAY)
        self.assertTrue(s.store.list_cards("v_a"))
        s.wipe("v_a")
        self.assertEqual(s.store.count_obs("v_a"), 0)
        self.assertEqual(s.store.list_cards("v_a"), [])
        self.assertFalse(s.store.is_enabled("v_a"))


class DetectionTests(unittest.TestCase):
    def setUp(self):
        self.s = make([rule()])
        self.s.enable("v_a")

    def test_below_min_count_no_card(self):
        self.s.observe("v_a", "gym", ts=T0)
        self.s.observe("v_a", "gym", ts=T0 + DAY)
        self.assertEqual(self.s.scan("v_a", now=T0 + 2 * DAY), [])

    def test_card_at_min_count_with_evidence(self):
        for i in range(3):
            self.s.observe("v_a", "gym", ts=T0 + i * DAY)
        cards = self.s.scan("v_a", now=T0 + 3 * DAY)
        self.assertEqual(len(cards), 1)
        c = cards[0]
        self.assertEqual(c.evidence_count, 3)
        self.assertEqual(c.first_seen, T0)
        self.assertEqual(c.last_seen, T0 + 2 * DAY)
        self.assertEqual(c.state, "open")
        self.assertIn("gym", c.title)

    def test_window_excludes_old_observations(self):
        for i in range(2):
            self.s.observe("v_a", "gym", ts=T0 - 10 * DAY + i)  # outside
        self.s.observe("v_a", "gym", ts=T0)
        self.assertEqual(self.s.scan("v_a", now=T0), [])

    def test_determinism_same_input_same_cards(self):
        def build():
            s = make([rule()])
            s.enable("v_a")
            for i in range(4):
                s.observe("v_a", "gym", ts=T0 + i * 1000)
            return [c.to_dict() for c in s.scan("v_a", now=T0 + 4000)]
        self.assertEqual(build(), build())

    def test_no_duplicate_open_card(self):
        for i in range(6):
            self.s.observe("v_a", "gym", ts=T0 + i * 100)
        first = self.s.scan("v_a", now=T0 + 600)
        second = self.s.scan("v_a", now=T0 + 700)
        self.assertEqual(len(first), 1)
        self.assertEqual(second, [])

    def test_snoozed_card_not_reraised_until_due(self):
        for i in range(3):
            self.s.observe("v_a", "gym", ts=T0 + i)
        self.s.scan("v_a", now=T0 + 3)
        self.s.snooze("v_a", "gym", 7, now=T0 + 4)
        self.assertEqual(self.s.scan("v_a", now=T0 + 5), [])
        # after snooze expiry AND new evidence... same rule may re-raise
        for i in range(3):
            self.s.observe("v_a", "gym", ts=T0 + 8 * DAY + i)
        cards = self.s.scan("v_a", now=T0 + 8 * DAY + 3)
        self.assertEqual(len(cards), 1)

    def test_dismissal_cooldown_then_reraise(self):
        for i in range(3):
            self.s.observe("v_a", "gym", ts=T0 + i)
        self.s.scan("v_a", now=T0 + 3)
        self.s.dismiss("v_a", "gym", now=T0 + 4)
        for i in range(3):
            self.s.observe("v_a", "gym", ts=T0 + 10 + i)
        self.assertEqual(self.s.scan("v_a", now=T0 + 13), [])  # cooling
        for i in range(3):
            self.s.observe("v_a", "gym", ts=T0 + 4 * DAY + i)
        cards = self.s.scan("v_a", now=T0 + 4 * DAY + 3)
        self.assertEqual(len(cards), 1)


    def test_accepted_card_never_reraises(self):
        # probe catch: accepted cards must suppress re-raising, or a
        # scan storm re-nags the owner forever
        s2 = make([rule()])
        s2.enable("v_a")
        for i in range(3):
            s2.observe("v_a", "gym", ts=T0 + i)
        s2.scan("v_a", now=T0 + 3)
        s2.accept("v_a", "gym", now=T0 + 4)
        for i in range(3):
            s2.observe("v_a", "gym", ts=T0 + 10 + i)
        for k in range(100):
            self.assertEqual(s2.scan("v_a", now=T0 + 20 + k), [])


class DecisionTests(unittest.TestCase):
    def setUp(self):
        self.s = make([rule()])
        self.s.enable("v_a")
        for i in range(3):
            self.s.observe("v_a", "gym", ts=T0 + i)
        self.s.scan("v_a", now=T0 + 3)

    def test_accept_returns_card_and_acts_on_nothing(self):
        before_obs = self.s.store.count_obs("v_a")
        c = self.s.accept("v_a", "gym", now=T0 + 10)
        self.assertEqual(c.state, "accepted")
        # zero side effects: no new observations, no other cards
        self.assertEqual(self.s.store.count_obs("v_a"), before_obs)
        self.assertEqual(len(self.s.store.list_cards("v_a")), 1)

    def test_ambiguous_and_empty_refuse_zero_effects(self):
        s = make([rule(rid="r1", kind="gym"), rule(rid="r2", kind="run")])
        s.enable("v_a")
        for i in range(3):
            s.observe("v_a", "gym", ts=T0 + i)
            s.observe("v_a", "run", ts=T0 + i)
        s.scan("v_a", now=T0 + 3)
        with self.assertRaises(NoMatch):
            s.accept("v_a", "", now=T0 + 4)
        # both titles contain "Often" -> ambiguous
        with self.assertRaises(AmbiguousMatch):
            s.accept("v_a", "often", now=T0 + 4)
        states = [c.state for c in s.store.list_cards("v_a")]
        self.assertEqual(states, ["open", "open"])

    def test_decided_cards_not_redecidable(self):
        self.s.dismiss("v_a", "gym", now=T0 + 4)
        with self.assertRaises(NoMatch):
            self.s.accept("v_a", "gym", now=T0 + 5)

    def test_snooze_bounds(self):
        with self.assertRaises(PatternError):
            self.s.snooze("v_a", "gym", 0, now=T0)
        with self.assertRaises(PatternError):
            self.s.snooze("v_a", "gym", 91, now=T0)


class IsolationAndCapTests(unittest.TestCase):
    def test_cross_owner_zero_effects(self):
        s = make([rule()])
        s.enable("v_a")
        for i in range(5):
            s.observe("v_a", "gym", ts=T0 + i)
        s.scan("v_a", now=T0 + 5)
        self.assertEqual(s.store.list_cards("v_b"), [])
        self.assertIsNone(s.observe("v_b", "gym", ts=T0))
        with self.assertRaises(NoMatch):
            s.accept("v_b", "gym", now=T0 + 6)
        s.wipe("v_b")
        self.assertEqual(s.store.count_obs("v_a"), 5)

    def test_observation_cap_refuses(self):
        s = make()
        s.enable("v_a")
        for i in range(MAX_OBSERVATIONS):
            s.observe("v_a", "k%d" % i, ts=T0 + i)
        with self.assertRaises(ObservationLimitReached):
            s.observe("v_a", "overflow", ts=T0 + MAX_OBSERVATIONS)
        self.assertEqual(s.store.count_obs("v_a"), MAX_OBSERVATIONS)

    def test_card_cap_is_atomic_zero_effects(self):
        # round-1 review 5479332317: never write 30 of 31
        rules = [rule(rid="r%02d" % i, kind="kind%02d" % i)
                 for i in range(MAX_CARDS_PER_OWNER + 5)]
        s = make(rules)
        s.enable("v_a")
        for i in range(MAX_CARDS_PER_OWNER + 5):
            for j in range(3):
                s.observe("v_a", "kind%02d" % i, ts=T0 + i * 10 + j)
        with self.assertRaises(CardLimitReached):
            s.scan("v_a", now=T0 + 1000)
        self.assertEqual(s.store.count_cards("v_a"), 0)  # nothing written

    def test_under_cap_batch_writes_atomically(self):
        rules = [rule(rid="r%02d" % i, kind="kind%02d" % i)
                 for i in range(MAX_CARDS_PER_OWNER)]
        s = make(rules)
        s.enable("v_a")
        for i in range(MAX_CARDS_PER_OWNER):
            for j in range(3):
                s.observe("v_a", "kind%02d" % i, ts=T0 + i * 10 + j)
        cards = s.scan("v_a", now=T0 + 1000)
        self.assertEqual(len(cards), MAX_CARDS_PER_OWNER)

    def test_accept_and_dismiss_make_room(self):
        # cap counts live (open+snoozed); decided cards are history
        rules = [rule(rid="r%02d" % i, kind="kind%02d" % i)
                 for i in range(MAX_CARDS_PER_OWNER)]
        s = make(rules + [rule(rid="rnew", kind="newkind")])
        s.enable("v_a")
        for i in range(MAX_CARDS_PER_OWNER):
            for j in range(3):
                s.observe("v_a", "kind%02d" % i, ts=T0 + i * 10 + j)
        s.scan("v_a", now=T0 + 1000)
        for j in range(3):
            s.observe("v_a", "newkind", ts=T0 + 2000 + j)
        with self.assertRaises(CardLimitReached):
            s.scan("v_a", now=T0 + 2003)
        s.accept("v_a", "kind00", now=T0 + 2004)
        s.dismiss("v_a", "kind01", now=T0 + 2005)
        cards = s.scan("v_a", now=T0 + 2006)
        self.assertEqual(len(cards), 1)
        self.assertEqual(cards[0].signature, "rnew:newkind")

    def test_snoozed_reraise_needs_fresh_evidence(self):
        # round-1 review: no re-raise on the same stale evidence.
        # snooze 1 day << window 7 days, so the OLD observations are
        # still in-window at snooze expiry - without the fix the card
        # re-raises on stale evidence.
        s = make([rule()])
        s.enable("v_a")
        for i in range(3):
            s.observe("v_a", "gym", ts=T0 + i)
        s.scan("v_a", now=T0 + 3)
        s.snooze("v_a", "gym", 1, now=T0 + 4)
        # snooze expired, old evidence still in window, NO fresh obs
        self.assertEqual(s.scan("v_a", now=T0 + 2 * DAY), [])
        # one fresh observation -> re-raise (evidence ts > last_seen)
        s.observe("v_a", "gym", ts=T0 + 2 * DAY + 1)
        cards = s.scan("v_a", now=T0 + 2 * DAY + 2)
        self.assertEqual(len(cards), 1)
        self.assertEqual(cards[0].evidence_count, 4)


class GrammarTests(unittest.TestCase):
    def test_fallthrough(self):
        s = make()
        for msg in ("patterns", "pattern", "my pattern", "patterns onn",
                    "wipe patterns", "accept gym"):
            self.assertIsNone(s.handle_message("v_a", msg, now=T0))

    def test_chat_roundtrip(self):
        s = make([rule()])
        out = s.handle_message("v_a", "patterns on", now=T0)
        self.assertIn("on", out)
        for i in range(3):
            s.observe("v_a", "gym", ts=T0 + i)
        s.scan("v_a", now=T0 + 3)
        out = s.handle_message("v_a", "my patterns", now=T0 + 4)
        self.assertIn("gym", out)
        out = s.handle_message("v_a", "pattern gym show", now=T0 + 5)
        self.assertEqual(json.loads(out)["signature"], "r1:gym")
        out = s.handle_message("v_a", "pattern gym accept", now=T0 + 6)
        self.assertIn("Nothing happens by itself", out)
        exp = json.loads(s.handle_message("v_a", "export my patterns",
                                          now=T0 + 7))
        self.assertEqual(exp["version"], "crayon-patterns-v1")
        self.assertEqual(exp["cards"][0]["state"], "accepted")


if __name__ == "__main__":
    unittest.main()

    def test_accepted_card_never_reraises(self):
        # probe catch: accepted cards must suppress re-raising, or a
        # scan storm re-nags the owner forever
        s = make([rule()])
        s.enable("v_a")
        for i in range(3):
            s.observe("v_a", "gym", ts=T0 + i)
        s.scan("v_a", now=T0 + 3)
        s.accept("v_a", "gym", now=T0 + 4)
        for i in range(3):
            s.observe("v_a", "gym", ts=T0 + 10 + i)
        for k in range(100):
            assert s.scan("v_a", now=T0 + 20 + k) == []
