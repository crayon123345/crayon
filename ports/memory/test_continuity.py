"""Adversarial tests for /ports/memory continuity.

Acceptance criteria under test (owner spec):
- corrected facts survive fresh chats
- no cross-user leakage
Plus: sensitive opt-in gate + purge, forget counts, export completeness,
provenance, idempotence, caps. Every test counts EFFECTS (store rows), not
prose replies.
"""
import sys, os, unittest

sys.path.insert(0, os.path.dirname(__file__))
from continuity import (Continuity, MemFactStore, FactError,
                        SensitiveNotAllowed, FactLimitReached,
                        MAX_FACTS_PER_OWNER)

A, B = "v_owner_a", "v_owner_b"


def fresh():
    return Continuity(MemFactStore())


class TestAcceptance(unittest.TestCase):
    def test_corrected_fact_survives_fresh_chat(self):
        c = fresh()
        c.remember(A, "I prefer tea", category="preference",
                   source_sid="chat-1", source_ts=1000)
        f, n = c.correct(A, "tea", "I prefer black coffee",
                         source_sid="chat-2", source_ts=2000)
        self.assertEqual(n, 1)
        # simulate fresh chat: only context_block crosses the boundary
        ctx = c.context_block(A)
        self.assertIn("black coffee", ctx)
        self.assertNotIn("prefer tea", ctx)
        # the old fact still exists in history but superseded
        hist = c.store.list(A, include_superseded=True)
        self.assertEqual(len(hist), 2)
        old = [h for h in hist if h.superseded_by][0]
        self.assertEqual(old.superseded_by, f.id)

    def test_no_cross_user_leakage(self):
        c = fresh()
        c.remember(A, "goal: run a marathon", category="goal",
                   source_sid="s", source_ts=1)
        c.remember(B, "I like rain", category="preference",
                   source_sid="s", source_ts=1)
        # B's views contain zero A data
        self.assertNotIn("marathon", c.context_block(B))
        exp_b = c.export(B)
        self.assertEqual(len(exp_b["owner_facts"]), 1)
        self.assertNotIn("marathon", str(exp_b))
        # B cannot delete/correct/read A's fact by id
        fa = c.store.list(A)[0]
        self.assertIsNone(c.store.get(B, fa.id))
        self.assertFalse(c.store.delete(B, fa.id))
        f2, n = c.correct(B, "marathon", "whatever", source_sid="s", source_ts=2)
        self.assertEqual(n, 0)  # matched nothing of B's
        self.assertEqual(c.store.count(A), 1)  # A untouched
        # forget with A's text hits nothing for B
        self.assertEqual(c.forget(B, "marathon"), 0)
        self.assertEqual(c.store.count(A), 1)
        # wipe B leaves A intact
        c.forget_all(B)
        self.assertEqual(c.store.count(A), 1)


class TestSensitive(unittest.TestCase):
    def test_sensitive_refused_without_optin_zero_effects(self):
        c = fresh()
        before = len(c.store._facts)
        with self.assertRaises(SensitiveNotAllowed):
            c.remember(A, "health: migraines", sensitive=True,
                       source_sid="s", source_ts=1)
        self.assertEqual(len(c.store._facts), before)  # zero writes
        r = c.handle_message(A, "remember sensitive: health: migraines",
                             source_sid="s", source_ts=1)
        self.assertIn("opt in", r)
        self.assertEqual(len(c.store._facts), before)  # still zero

    def test_sensitive_stored_after_optin_never_in_context(self):
        c = fresh()
        c.store.set_optin(A, True)
        c.remember(A, "health: migraines", sensitive=True,
                   source_sid="s", source_ts=1)
        self.assertEqual(c.store.count(A), 1)
        self.assertNotIn("migraines", c.context_block(A))
        self.assertIn("migraines", str(c.export(A)))  # but exportable

    def test_optout_purges_sensitive_only(self):
        c = fresh()
        c.store.set_optin(A, True)
        c.remember(A, "health: migraines", sensitive=True, source_sid="s", source_ts=1)
        c.remember(A, "I like rain", source_sid="s", source_ts=1)
        purged = c.set_sensitive_optin(A, False)
        self.assertEqual(purged, 1)
        self.assertEqual(c.store.count(A), 1)
        self.assertEqual(c.store.list(A)[0].text, "I like rain")
        # command path
        c.store.set_optin(A, True)
        c.remember(A, "secret2", sensitive=True, source_sid="s", source_ts=2)
        r = c.handle_message(A, "sensitive memory off", source_sid="s", source_ts=3)
        self.assertIn("Deleted 1", r)
        self.assertEqual(c.store.count(A), 1)

    def test_correction_of_sensitive_needs_optin(self):
        c = fresh()
        c.store.set_optin(A, True)
        c.remember(A, "secret thing", sensitive=True, source_sid="s", source_ts=1)
        c.set_sensitive_optin(A, True)  # still on
        f, n = c.correct(A, "secret", "secret thing v2", source_sid="s", source_ts=2)
        self.assertEqual(n, 1)
        self.assertTrue(f.sensitive)  # sensitivity preserved


class TestCommands(unittest.TestCase):
    def test_remember_command_with_category_and_provenance(self):
        c = fresh()
        r = c.handle_message(A, "remember that #person my sister is Riya",
                             source_sid="chat-9", source_ts=4242)
        self.assertIn("person", r)
        f = c.store.list(A)[0]
        self.assertEqual(f.category, "person")
        self.assertEqual(f.source_sid, "chat-9")
        self.assertEqual(f.source_ts, 4242)
        self.assertIn("sister is Riya", f.source_excerpt)

    def test_remember_idempotent(self):
        c = fresh()
        c.handle_message(A, "remember I prefer tea", source_sid="s", source_ts=1)
        r = c.handle_message(A, "remember   I prefer   tea ", source_sid="s", source_ts=2)
        self.assertIn("already have", r)
        self.assertEqual(c.store.count(A), 1)

    def test_correct_command(self):
        c = fresh()
        c.handle_message(A, "remember my trainer is Sam", source_sid="s", source_ts=1)
        r = c.handle_message(A, "correct my trainer is Sam to my trainer is Alex",
                             source_sid="s", source_ts=2)
        self.assertIn("1 previous", r)
        ctx = c.context_block(A)
        self.assertIn("Alex", ctx)
        self.assertNotIn("Sam", ctx)

    def test_correct_no_match_creates_new(self):
        c = fresh()
        r = c.handle_message(A, "correct nothinghere to brand new fact",
                             source_sid="s", source_ts=1)
        self.assertIn("new fact", r)
        self.assertEqual(c.store.count(A), 1)

    def test_forget_counts_and_scopes(self):
        c = fresh()
        c.handle_message(A, "remember I like rain", source_sid="s", source_ts=1)
        c.handle_message(A, "remember I like rain in April", source_sid="s", source_ts=2)
        c.handle_message(A, "remember I like tea", source_sid="s", source_ts=3)
        r = c.handle_message(A, "forget rain", source_sid="s", source_ts=4)
        self.assertIn("Forgot 2", r)
        self.assertEqual(c.store.count(A), 1)
        r = c.handle_message(A, "forget zzz-no-match", source_sid="s", source_ts=5)
        self.assertIn("No stored fact", r)

    def test_forget_everything(self):
        c = fresh()
        c.handle_message(A, "remember one", source_sid="s", source_ts=1)
        c.handle_message(A, "remember two", source_sid="s", source_ts=2)
        r = c.handle_message(A, "forget everything", source_sid="s", source_ts=3)
        self.assertIn("2 fact(s) deleted", r)
        self.assertEqual(c.store.count(A), 0)

    def test_list_and_export_commands(self):
        c = fresh()
        c.handle_message(A, "remember #goal finish the deck", source_sid="s7", source_ts=99)
        r = c.handle_message(A, "what do you remember about me?", source_sid="s7", source_ts=100)
        self.assertIn("finish the deck", r)
        r = c.handle_message(A, "export my memory", source_sid="s7", source_ts=101)
        import json
        data = json.loads(r)
        self.assertEqual(data["format"], "crayon-memory-v1")
        self.assertEqual(data["owner_facts"][0]["source"]["chat"], "s7")
        self.assertEqual(data["owner_facts"][0]["source"]["message_ts"], 99)

    def test_non_command_passes_through(self):
        c = fresh()
        self.assertIsNone(c.handle_message(A, "how is the weather?"))
        self.assertIsNone(c.handle_message(A, ""))
        self.assertIsNone(c.handle_message(A, "I remembered to call mom"))


class TestLimitsAndCaps(unittest.TestCase):
    def test_fact_cap_refuses_without_silent_eviction(self):
        c = fresh()
        c.store.set_optin(A, True)
        for i in range(MAX_FACTS_PER_OWNER):
            c.remember(A, "fact number %d" % i, source_sid="s", source_ts=i)
        with self.assertRaises(FactLimitReached):
            c.remember(A, "one too many", source_sid="s", source_ts=999999)
        self.assertEqual(c.store.count(A), MAX_FACTS_PER_OWNER)

    def test_text_and_excerpt_capped(self):
        c = fresh()
        f, _ = c.remember(A, "x" * 900, source_sid="s", source_ts=1,
                          source_excerpt="y" * 900)
        self.assertEqual(len(f.text), 500)
        self.assertEqual(len(f.source_excerpt), 200)

    def test_export_includes_superseded_history(self):
        c = fresh()
        c.remember(A, "v1", source_sid="s", source_ts=1)
        c.correct(A, "v1", "v2", source_sid="s", source_ts=2)
        exp = c.export(A)
        self.assertEqual(len(exp["owner_facts"]), 2)
        sup = [f for f in exp["owner_facts"] if f["superseded_by"]]
        self.assertEqual(len(sup), 1)

    def test_followup_category(self):
        c = fresh()
        c.handle_message(A, "remember #followup ask Riya about her exam results",
                         source_sid="s", source_ts=1)
        f = c.store.list(A)[0]
        self.assertEqual(f.category, "followup")
        self.assertIn("followup", c.context_block(A))


class TestReviewRegressions(unittest.TestCase):
    def test_category_then_sensitive_marker_not_swallowed(self):
        # regression: "remember #goal sensitive: x" used to store the
        # sensitive marker as PLAINTEXT in a non-sensitive fact
        c = fresh()
        r = c.handle_message(A, "remember #goal sensitive: my therapy schedule",
                             source_sid="s", source_ts=1)
        self.assertIn("opt in", r)               # refused without opt-in
        self.assertEqual(c.store.count(A), 0)    # zero writes
        c.store.set_optin(A, True)
        r = c.handle_message(A, "remember #goal sensitive: my therapy schedule",
                             source_sid="s", source_ts=2)
        f = c.store.list(A)[0]
        self.assertTrue(f.sensitive)
        self.assertEqual(f.category, "goal")
        self.assertEqual(f.text, "my therapy schedule")  # marker not in text
        self.assertNotIn("therapy", c.context_block(A))

    def test_sensitive_then_category_marker_order(self):
        c = fresh()
        c.store.set_optin(A, True)
        c.handle_message(A, "remember sensitive: #person my therapist is Dr K",
                         source_sid="s", source_ts=1)
        f = c.store.list(A)[0]
        self.assertTrue(f.sensitive)
        self.assertEqual(f.category, "person")
        self.assertEqual(f.text, "my therapist is Dr K")

    def test_correct_at_cap_returns_message_not_exception(self):
        # regression: FactLimitReached escaped the correct command handler
        c = fresh()
        for i in range(MAX_FACTS_PER_OWNER):
            c.remember(A, "cap filler %d" % i, source_sid="s", source_ts=i)
        r = c.handle_message(A, "correct cap filler 3 to replacement fact",
                             source_sid="s", source_ts=999999)
        self.assertIn("memory is full", r)
        # zero effects: no new fact, nothing superseded
        self.assertEqual(c.store.count(A), MAX_FACTS_PER_OWNER)
        self.assertEqual([f for f in c.store.list(A, include_superseded=True)
                          if f.superseded_by], [])


if __name__ == "__main__":
    unittest.main(verbosity=2)
