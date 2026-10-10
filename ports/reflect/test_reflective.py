"""Adversarial tests for /ports/reflect reflective mode.

Acceptance criteria under test (owner spec for P0 #2):
- thought-partner mode (Listen / Reflect / Plan) in the prompt contract
- ONE useful follow-up question, not a barrage
Plus: explicit command grammar (ambiguous phrasing falls through),
cross-owner isolation, sensitive follow-ups never injected, superseded
follow-ups never injected, GLOBAL no-nagging, deterministic recency by
source time, and the deterministic one-question guard incl. contiguous
punctuation. Every test counts EFFECTS (store rows, question marks),
not prose replies.
"""
import sys, os, unittest

sys.path.insert(0, os.path.dirname(__file__))
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "memory"))
from reflective import ReflectiveMode, MemModeStore
from continuity import Continuity, MemFactStore, Fact

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
        self.assertEqual(ms.get_surfaced(A), [])

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


class TestRecencySemantics(unittest.TestCase):
    """Recency = greatest source_ts (when the user said it), then storage
    time (created), then id for pure determinism. Ids carry no semantics."""

    def test_recency_is_source_time_not_storage_order(self):
        r, ms, fs = fresh()
        cont = Continuity(fs)
        # Stored FIRST but said EARLIER (small source_ts):
        fs.put(Fact(A, "followup", "stored first, said earlier", False,
                    "c", 1000, "", fid="f_e", created=900))
        # Stored SECOND but said LATER (large source_ts):
        fs.put(Fact(A, "followup", "stored second, said later", False,
                    "c", 9000, "", fid="f_l", created=100))
        r.handle_message(A, "reflect on")
        self.assertIn("stored second, said later", r.prompt_addendum(A))

    def test_pick_is_deterministic_across_repeated_calls(self):
        r, ms, fs = fresh()
        fs.put(Fact(A, "followup", "alpha", False, "c", 5000, "",
                    fid="f_a1", created=100))
        fs.put(Fact(A, "followup", "beta", False, "c", 5000, "",
                    fid="f_b2", created=100))    # exact tie on both times
        picks = {r.pick_followup(A).id for _ in range(10)}
        self.assertEqual(len(picks), 1)          # stable under a pure call

    def test_equal_source_ts_newer_storage_wins(self):
        r, ms, fs = fresh()
        # Inject with EXPLICIT storage times: source_ts ties, so the created
        # leg decides. (continuity.remember stamps created=now at 1s
        # resolution; controlling created removes clock dependence.)
        fs.put(Fact(A, "followup", "stored first", False, "c", 5000, "",
                    fid="f_old", created=100))
        fs.put(Fact(A, "followup", "stored later", False, "c", 5000, "",
                    fid="f_new", created=200))
        picks = r.open_followups(A)
        self.assertEqual(picks[0].text, "stored later")  # created tie-break

    def test_exact_tie_breaks_by_id_for_determinism_only(self):
        r, ms, fs = fresh()
        # Same source_ts AND same created: the id leg stabilizes the order.
        # Semantics live in the timestamp legs; id only makes exact ties
        # deterministic. Fixed ids make the expectation explicit:
        # reverse sort => higher id first.
        fs.put(Fact(A, "followup", "tie aaa", False, "c", 5000, "",
                    fid="f_aaa", created=100))
        fs.put(Fact(A, "followup", "tie zzz", False, "c", 5000, "",
                    fid="f_zzz", created=100))
        picks = r.open_followups(A)
        self.assertEqual([f.id for f in picks], ["f_zzz", "f_aaa"])
        # and the pick is stable across repeated pure calls
        self.assertEqual({r.pick_followup(A).id for _ in range(10)},
                         {"f_zzz"})


class TestNoNagging(unittest.TestCase):
    def test_each_candidate_surfaces_exactly_once_then_none(self):
        r, ms, fs = fresh()
        cont = Continuity(fs)
        add_followup(cont, A, "first thing", ts=1000)
        add_followup(cont, A, "second thing", ts=2000)
        r.handle_message(A, "reflect on")
        ad1 = r.prompt_addendum(A)
        self.assertIn("second thing", ad1)      # most recent source first
        ad2 = r.prompt_addendum(A)
        self.assertIn("first thing", ad2)       # the remaining candidate
        self.assertNotIn("second thing", ad2)
        ad3 = r.prompt_addendum(A)              # BOTH surfaced: no cycling
        self.assertNotIn("first thing", ad3)
        self.assertNotIn("second thing", ad3)

    def test_single_candidate_not_resurfaced(self):
        r, ms, fs = fresh()
        cont = Continuity(fs)
        add_followup(cont, A, "only thing", ts=1000)
        r.handle_message(A, "reflect on")
        self.assertIn("only thing", r.prompt_addendum(A))
        for _ in range(4):
            self.assertNotIn("only thing", r.prompt_addendum(A))

    def test_mode_toggle_resets_history(self):
        r, ms, fs = fresh()
        cont = Continuity(fs)
        add_followup(cont, A, "only thing", ts=1000)
        r.handle_message(A, "reflect on")
        r.prompt_addendum(A)
        r.handle_message(A, "reflect off")
        r.handle_message(A, "reflect on")
        self.assertIn("only thing", r.prompt_addendum(A))  # fresh session

    def test_corrected_followup_is_surfacable_again(self):
        r, ms, fs = fresh()
        cont = Continuity(fs)
        add_followup(cont, A, "ask about the old plan", ts=1000)
        r.handle_message(A, "reflect on")
        r.prompt_addendum(A)                    # surfaces old plan
        cont.correct(A, "old plan", "ask about the new plan",
                     source_sid="chat-1", source_ts=2000)
        ad = r.prompt_addendum(A)
        self.assertIn("ask about the new plan", ad)  # new id: fresh
        self.assertNotIn("old plan", ad)


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

    # --- regression: review finding 1 (contiguous / repeated punctuation) ---

    def test_contiguous_questions_no_whitespace(self):
        out = ReflectiveMode.enforce_one_question("Why?How?")
        self.assertEqual(out, "Why?")
        self.assertEqual(out.count("?"), 1)

    def test_repeated_marks_collapse(self):
        out = ReflectiveMode.enforce_one_question("Really??")
        self.assertEqual(out, "Really?")
        self.assertEqual(out.count("?"), 1)

    def test_contiguous_mix_keeps_statements(self):
        out = ReflectiveMode.enforce_one_question("Why?How?That is fair.")
        self.assertEqual(out, "Why? That is fair.")

    def test_triple_marks_plus_contiguous(self):
        out = ReflectiveMode.enforce_one_question("Seriously???How so?Ok.")
        self.assertEqual(out.count("?"), 1)
        self.assertIn("Seriously?", out)
        self.assertIn("Ok.", out)

    # --- regression: review finding round 2 (mixed terminal punctuation) ---

    def test_mixed_terminal_punctuation_capped(self):
        cases = {"Really?!?": "Really?!", "Why?.?": "Why?.",
                 "Question?!!??": "Question?!!", "What??!": "What?!",
                 "Huh?!??!": "Huh?!!"}
        for t, want in cases.items():
            out = ReflectiveMode.enforce_one_question(t)
            self.assertEqual(out, want, t)
            self.assertEqual(out.count("?"), 1, t)

    def test_single_mark_interrobang_byte_identical(self):
        # one "?" inside the first question sentence, no repeats: untouched
        self.assertEqual(ReflectiveMode.enforce_one_question("What!?"),
                         "What!?")

    def test_exhaustive_punctuation_run_invariant(self):
        # Every terminal-punctuation run up to length 4 over {?, !, .},
        # alone and joined with/without whitespace: at most one "?" survives.
        import itertools
        runs = ["".join(c) for n in (1, 2, 3, 4)
                for c in itertools.product("?!.", repeat=n)]
        for run in runs:
            t = "Word%s" % run
            out = ReflectiveMode.enforce_one_question(t)
            self.assertLessEqual(out.count("?"), 1, t)
        for r1 in runs[::7]:            # sampled cross-product keeps it fast
            for r2 in runs[::11]:
                for sep in (" ", ""):
                    t = "First%s%sSecond%s Tail." % (r1, sep, r2)
                    out = ReflectiveMode.enforce_one_question(t)
                    self.assertLessEqual(out.count("?"), 1, t)


if __name__ == "__main__":
    unittest.main()
