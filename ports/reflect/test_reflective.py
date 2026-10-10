"""Adversarial tests for /ports/reflect reflective mode.

Acceptance criteria under test (owner spec for P0 #2):
- thought-partner mode (Listen / Reflect / Plan) in the prompt contract
- ONE useful follow-up question, not a barrage
Plus: explicit command grammar (ambiguous phrasing falls through),
cross-owner isolation, sensitive follow-ups never injected, superseded
follow-ups never injected, no-nagging dedupe, deterministic post-filter.
Every test counts EFFECTS (store rows, question marks), not prose replies.
"""
import sys, os, unittest

sys.path.insert(0, os.path.dirname(__file__))
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "memory"))
from reflective import ReflectiveMode, MemModeStore
from continuity import Continuity, MemFactStore

A, B = "v_owner_a", "v_owner_b"


def fresh(with_facts=True):
    ms = MemModeStore()
    fs = MemFactStore() if with_facts else None
    return ReflectiveMode(ms, fs), ms, fs


def add_followup(cont, owner, text, sensitive=False, ts=1000):
    f, _ = cont.remember(owner, text, category="followup",
                         sensitive=sensitive, source_sid="chat-1",
                         source_ts=ts)
    return f


class TestModeCommands(unittest.TestCase):
    def test_default_off_and_empty_addendum(self):
        r, ms, fs = fresh()
        self.assertFalse(r.is_on(A))
        self.assertEqual(r.prompt_addendum(A), "")

    def test_reflect_on_and_contract_content(self):
        r, ms, fs = fresh()
        reply = r.handle_message(A, "reflect on")
        self.assertIsNotNone(reply)
        self.assertTrue(r.is_on(A))
        ad = r.prompt_addendum(A)
        for word in ("LISTEN", "REFLECT", "PLAN", "EXACTLY ONE"):
            self.assertIn(word, ad)

    def test_reflect_off_clears_state(self):
        r, ms, fs = fresh()
        r.handle_message(A, "reflect on")
        r.handle_message(A, "reflect off")
        self.assertFalse(r.is_on(A))
        self.assertEqual(r.prompt_addendum(A), "")
        self.assertIsNone(ms.get_last_fid(A))

    def test_aliases_case_insensitive(self):
        for cmd in ("Reflective Mode ON", "THOUGHT PARTNER ON"):
            r, ms, fs = fresh()
            self.assertIsNotNone(r.handle_message(A, cmd))
            self.assertTrue(r.is_on(A), cmd)

    def test_ambiguous_phrasing_falls_through(self):
        r, ms, fs = fresh()
        for msg in ("reflect on my day", "can you reflect on this?",
                    "I want a thought partner", "reflect", "reflection"):
            self.assertIsNone(r.handle_message(A, msg), msg)
            self.assertFalse(r.is_on(A), msg)

    def test_commands_create_no_facts(self):
        r, ms, fs = fresh()
        cont = Continuity(fs)
        before = len(fs.list(A))
        r.handle_message(A, "reflect on")
        r.handle_message(A, "reflect status")
        r.handle_message(A, "reflect off")
        self.assertEqual(len(fs.list(A)), before)  # zero memory effects

    def test_status_reports_state_and_count(self):
        r, ms, fs = fresh()
        cont = Continuity(fs)
        add_followup(cont, A, "ask how the interview went", ts=1000)
        add_followup(cont, A, "ask about the marathon plan", ts=2000)
        reply = r.handle_message(A, "reflect status")
        self.assertIn("OFF", reply)
        self.assertIn("2", reply)


class TestIsolation(unittest.TestCase):
    def test_cross_owner_mode_isolation(self):
        r, ms, fs = fresh()
        r.handle_message(A, "reflect on")
        self.assertTrue(r.is_on(A))
        self.assertFalse(r.is_on(B))
        self.assertEqual(r.prompt_addendum(B), "")
        r.handle_message(B, "reflect off")  # B's off must not touch A
        self.assertTrue(r.is_on(A))

    def test_cross_owner_followup_isolation(self):
        r, ms, fs = fresh()
        cont = Continuity(fs)
        add_followup(cont, B, "B's private follow-up", ts=1000)
        r.handle_message(A, "reflect on")
        self.assertNotIn("B's private follow-up", r.prompt_addendum(A))
        self.assertEqual(r.open_followups(A), [])


class TestFollowupGrounding(unittest.TestCase):
    def test_open_followup_injected_when_on(self):
        r, ms, fs = fresh()
        cont = Continuity(fs)
        add_followup(cont, A, "ask how the interview went", ts=1000)
        self.assertEqual(r.prompt_addendum(A), "")  # off: nothing
        r.handle_message(A, "reflect on")
        ad = r.prompt_addendum(A)
        self.assertIn("ask how the interview went", ad)

    def test_sensitive_followup_never_injected(self):
        r, ms, fs = fresh()
        cont = Continuity(fs)
        cont.set_sensitive_optin(A, True)
        add_followup(cont, A, "secret medical worry", sensitive=True, ts=1000)
        add_followup(cont, A, "ask about the marathon plan", ts=2000)
        r.handle_message(A, "reflect on")
        ad = r.prompt_addendum(A)
        self.assertNotIn("secret medical worry", ad)
        self.assertIn("ask about the marathon plan", ad)

    def test_superseded_followup_not_injected(self):
        r, ms, fs = fresh()
        cont = Continuity(fs)
        add_followup(cont, A, "ask about the old plan", ts=1000)
        cont.correct(A, "old plan", "ask about the new plan",
                     source_sid="chat-1", source_ts=2000)
        r.handle_message(A, "reflect on")
        ad = r.prompt_addendum(A)
        self.assertNotIn("old plan", ad)
        self.assertIn("ask about the new plan", ad)

    def test_most_recent_wins_and_dedupe_no_nagging(self):
        r, ms, fs = fresh()
        cont = Continuity(fs)
        f1 = add_followup(cont, A, "first thing", ts=1000)
        f2 = add_followup(cont, A, "second thing", ts=2000)
        r.handle_message(A, "reflect on")
        ad1 = r.prompt_addendum(A)
        self.assertIn("second thing", ad1)      # most recent first
        ad2 = r.prompt_addendum(A)
        self.assertIn("first thing", ad2)       # dedupe: other candidate
        self.assertNotIn("second thing", ad2)

    def test_single_candidate_not_resurfaced(self):
        r, ms, fs = fresh()
        cont = Continuity(fs)
        add_followup(cont, A, "only thing", ts=1000)
        r.handle_message(A, "reflect on")
        self.assertIn("only thing", r.prompt_addendum(A))
        self.assertNotIn("only thing", r.prompt_addendum(A))  # no nagging

    def test_non_followup_categories_not_injected(self):
        r, ms, fs = fresh()
        cont = Continuity(fs)
        cont.remember(A, "I prefer tea", category="preference",
                      source_sid="c", source_ts=1000)
        cont.remember(A, "run a marathon", category="goal",
                      source_sid="c", source_ts=2000)
        r.handle_message(A, "reflect on")
        ad = r.prompt_addendum(A)
        self.assertNotIn("tea", ad)
        self.assertNotIn("marathon", ad)

    def test_no_fact_store_still_works(self):
        r, ms, fs = fresh(with_facts=False)
        r.handle_message(A, "reflect on")
        ad = r.prompt_addendum(A)
        self.assertIn("LISTEN", ad)             # contract still present
        self.assertNotIn("open follow-up worth", ad)


class TestOneQuestionGuard(unittest.TestCase):
    def test_zero_or_one_question_byte_identical(self):
        for t in ("no questions here.", "just one question here?"):
            self.assertEqual(ReflectiveMode.enforce_one_question(t), t)
            self.assertLessEqual(ReflectiveMode.question_count(t), 1)

    def test_barrage_reduced_to_one(self):
        t = ("That sounds hard. What happened first? Then what did you do? "
             "And how did it end?")
        out = ReflectiveMode.enforce_one_question(t)
        self.assertEqual(out.count("?"), 1)
        self.assertIn("What happened first?", out)

    def test_later_statements_kept_later_questions_dropped(self):
        t = ("I hear you. How are you feeling? That takes courage. "
             "What will you do next? You have options.")
        out = ReflectiveMode.enforce_one_question(t)
        self.assertEqual(out.count("?"), 1)
        self.assertIn("That takes courage.", out)
        self.assertIn("You have options.", out)
        self.assertNotIn("What will you do next?", out)

    def test_guard_never_invents_content(self):
        t = "One. Two? Three? Four? Five."
        out = ReflectiveMode.enforce_one_question(t)
        for word in out.split():
            self.assertIn(word, t.split())


if __name__ == "__main__":
    unittest.main()
