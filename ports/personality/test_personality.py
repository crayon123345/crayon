import sys, os, json, unittest
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from personality import (Personality, MemPersonalityStore,
                         PersonalityError, PRESETS, MAX_TRAITS)

T0 = 1791633600


def make():
    return Personality(MemPersonalityStore())


class PresetTests(unittest.TestCase):
    def test_all_presets_settable_and_rendered(self):
        s = make()
        for name in PRESETS:
            p = s.set_preset("v_a", name, now=T0)
            self.assertEqual(p.kind, "preset")
            prof = s.profile("v_a")
            self.assertEqual(prof["name"], name)
            self.assertEqual(prof["traits"], PRESETS[name])
            self.assertIn("tone only", prof["note"])

    def test_unknown_preset_refused_zero_effects(self):
        s = make()
        with self.assertRaises(PersonalityError):
            s.set_preset("v_a", "sassy", now=T0)
        self.assertIsNone(s.profile("v_a"))

    def test_no_profile_means_default_voice(self):
        s = make()
        self.assertIsNone(s.profile("v_a"))


class ClaimRejectionTests(unittest.TestCase):
    def test_capability_claims_rejected_in_traits(self):
        s = make()
        for bad in ("you can make calls", "I can book flights",
                    "always-on helper", "I have access to your mail",
                    "will buy things", "able to pay bills",
                    "can send messages", "24/7 assistant"):
            with self.assertRaises(PersonalityError, msg=bad):
                s.set_custom("v_a", "x", [bad], now=T0)
        self.assertIsNone(s.profile("v_a"))

    def test_capability_claim_rejected_in_name(self):
        s = make()
        with self.assertRaises(PersonalityError):
            s.set_custom("v_a", "agent who can call people", ["nice"],
                         now=T0)

    def test_pure_tone_accepted(self):
        s = make()
        p = s.set_custom("v_a", "coach", ["direct", "warm",
                                          "short sentences"], now=T0)
        self.assertEqual(p.traits, ["direct", "warm",
                                    "short sentences"])


class BoundsTests(unittest.TestCase):
    def test_trait_count_cap(self):
        s = make()
        with self.assertRaises(PersonalityError):
            s.set_custom("v_a", "x", ["t%d" % i
                                      for i in range(MAX_TRAITS + 1)],
                         now=T0)

    def test_empty_traits_refused(self):
        s = make()
        with self.assertRaises(PersonalityError):
            s.set_custom("v_a", "x", ["", "  "], now=T0)
        with self.assertRaises(PersonalityError):
            s.set_custom("v_a", "", ["nice"], now=T0)

    def test_overflow_zero_effects(self):
        s = make()
        s.set_preset("v_a", "warm", now=T0)
        with self.assertRaises(PersonalityError):
            s.set_custom("v_a", "x", ["t%d" % i
                                      for i in range(MAX_TRAITS + 5)],
                         now=T0)
        self.assertEqual(s.profile("v_a")["name"], "warm")


class IsolationAndRenderTests(unittest.TestCase):
    def test_cross_owner_zero_effects(self):
        s = make()
        s.set_preset("v_a", "formal", now=T0)
        self.assertIsNone(s.profile("v_b"))
        self.assertFalse(s.clear("v_b"))
        self.assertEqual(s.profile("v_a")["name"], "formal")
        s.set_preset("v_b", "playful", now=T0)
        self.assertEqual(s.profile("v_a")["name"], "formal")

    def test_render_is_pure(self):
        s = make()
        s.set_custom("v_a", "coach", ["direct", "warm"], now=T0)
        self.assertEqual(s.profile("v_a"), s.profile("v_a"))

    def test_set_replaces_clear_restores_default(self):
        s = make()
        s.set_preset("v_a", "warm", now=T0)
        s.set_preset("v_a", "concise", now=T0 + 1)
        self.assertEqual(s.profile("v_a")["name"], "concise")
        self.assertTrue(s.clear("v_a"))
        self.assertIsNone(s.profile("v_a"))


class GrammarTests(unittest.TestCase):
    def test_fallthrough(self):
        s = make()
        for msg in ("style", "set style", "set custom style x",
                    "my styles", "clear style", "personality on"):
            self.assertIsNone(s.handle_message("v_a", msg, now=T0))

    def test_chat_roundtrip(self):
        s = make()
        self.assertIn("default voice",
                      s.handle_message("v_a", "my style", now=T0))
        out = s.handle_message("v_a", "set style warm", now=T0)
        self.assertIn("warm", out)
        out = s.handle_message(
            "v_a", "set custom style coach: direct, warm, short",
            now=T0 + 1)
        self.assertIn("coach", out)
        out = s.handle_message("v_a", "my style", now=T0 + 2)
        self.assertIn("coach", out)
        out = s.handle_message(
            "v_a", "set custom style bot: I can book flights", now=T0 + 3)
        self.assertIn("cannot claim capabilities", out)
        exp = json.loads(s.handle_message("v_a", "export my style",
                                          now=T0 + 4))
        self.assertEqual(exp["profile"]["name"], "coach")
        out = s.handle_message("v_a", "clear my style", now=T0 + 5)
        self.assertIn("cleared", out)


if __name__ == "__main__":
    unittest.main()
