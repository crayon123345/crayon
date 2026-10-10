"""Adversarial tests for /ports/goals durable goal/task state.

Trait acceptance criteria under test (Muse goal pages + Dots always-on
responsibility):
- durable goals with steps, blockers, next actions
- EXPLICIT progress/blocker/completion states, inspectable
Plus: deterministic matching (ambiguity refused with ZERO effects),
cross-owner isolation, caps (refuse, never evict), explicit command
grammar (near-misses fall through), delete vs abandon, provenance.
Every test counts EFFECTS (store rows, step states), not prose replies.
"""
import sys, os, json, unittest

sys.path.insert(0, os.path.dirname(__file__))
from goals import (Goals, Goal, MemGoalStore, GoalError, AmbiguousMatch,
                   NoMatch, GoalLimitReached, MAX_GOALS_PER_OWNER,
                   MAX_STEPS_PER_GOAL)

A, B = "v_owner_a", "v_owner_b"


def fresh():
    return Goals(MemGoalStore())


def make_goal(g, owner=A, title="Learn guitar", steps=("Buy tuner",
                                                       "Practice daily")):
    g.handle_message(owner, "add goal %s" % title)
    for s in steps:
        g.handle_message(owner, "goal %s add step %s" % (title, s))


class TestGoalLifecycle(unittest.TestCase):
    def test_add_goal_with_provenance(self):
        g = fresh()
        goal = g.add_goal(A, "Learn guitar", source_sid="chat-1",
                          source_ts=1000, source_excerpt="add goal Learn...")
        self.assertEqual(goal.status, "active")
        d = goal.to_dict()
        self.assertEqual(d["source"]["chat"], "chat-1")
        self.assertEqual(d["source"]["message_ts"], 1000)

    def test_duplicate_open_goal_refused(self):
        g = fresh()
        make_goal(g)
        n = len(g.store.list(A))
        reply = g.handle_message(A, "add goal learn GUITAR")
        self.assertIn("already exists", reply)
        self.assertEqual(len(g.store.list(A)), n)   # zero effects

    def test_status_transitions(self):
        g = fresh()
        make_goal(g)
        for cmd, want in (("goal guitar pause", "paused"),
                          ("goal guitar resume", "active"),
                          ("goal guitar done", "done")):
            g.handle_message(A, cmd)
            self.assertEqual(g._match_goal(A, "guitar").status, want)

    def test_abandon_keeps_record_delete_removes(self):
        g = fresh()
        make_goal(g, title="Goal one", steps=())
        make_goal(g, title="Goal two", steps=())
        g.handle_message(A, "goal one abandon")
        self.assertEqual(g._match_goal(A, "one").status, "abandoned")
        self.assertEqual(len(g.store.list(A)), 2)   # record kept
        g.handle_message(A, "goal two delete")
        self.assertEqual(len(g.store.list(A)), 1)   # record gone

    def test_steps_not_added_to_closed_goal(self):
        g = fresh()
        make_goal(g)
        g.handle_message(A, "goal guitar done")
        reply = g.handle_message(A, "goal guitar add step late step")
        self.assertIn("done", reply)
        self.assertEqual(len(g._match_goal(A, "guitar").steps), 2)


class TestStepsAndStates(unittest.TestCase):
    def test_step_states_and_blocker_reason(self):
        g = fresh()
        make_goal(g)
        g.handle_message(A, "goal guitar step 1 doing")
        g.handle_message(A, "goal guitar step 2 blocked: no time")
        goal = g._match_goal(A, "guitar")
        self.assertEqual(goal.steps[0].state, "doing")
        self.assertEqual(goal.steps[1].state, "blocked")
        self.assertEqual(goal.steps[1].blocker, "no time")
        # unblocking clears the reason
        g.handle_message(A, "goal guitar step 2 pending")
        self.assertIsNone(goal.steps[1].blocker)

    def test_blocking_without_reason_refused(self):
        g = fresh()
        make_goal(g)
        reply = g.handle_message(A, "goal guitar step 1 blocked")
        self.assertIn("reason", reply)
        self.assertEqual(g._match_goal(A, "guitar").steps[0].state,
                         "pending")                       # zero effects

    def test_step_by_number_and_substring(self):
        g = fresh()
        make_goal(g)
        g.handle_message(A, "goal guitar step tuner done")
        self.assertEqual(g._match_goal(A, "guitar").steps[0].state, "done")
        g.handle_message(A, "goal guitar step #2 done")
        self.assertEqual(g._match_goal(A, "guitar").steps[1].state, "done")

    def test_completion_progress_counts(self):
        g = fresh()
        make_goal(g)
        g.handle_message(A, "goal guitar step 1 done")
        na = g.next_actions(A)
        self.assertEqual(na[0]["progress"], "1/2")
        self.assertEqual(na[0]["next"], "Practice daily")
        self.assertEqual(na[0]["next_n"], 2)


class TestInspectableState(unittest.TestCase):
    def test_next_actions_explicit(self):
        g = fresh()
        make_goal(g, title="Learn guitar")
        make_goal(g, title="Ship report", steps=("Draft", "Review"))
        g.handle_message(A, "goal guitar step 1 done")
        g.handle_message(A, "goal report step 1 blocked: waiting on data")
        na = g.next_actions(A)
        by_title = {i["title"]: i for i in na}
        # guitar: step 1 done -> next is step 2
        self.assertEqual(by_title["Learn guitar"]["next"], "Practice daily")
        self.assertEqual(by_title["Learn guitar"]["progress"], "1/2")
        # report: step 1 blocked (with reason), next is step 2
        self.assertEqual(by_title["Ship report"]["next"], "Review")
        self.assertEqual(by_title["Ship report"]["blocked"],
                         [{"n": 1, "text": "Draft",
                           "reason": "waiting on data"}])

    def test_next_actions_deterministic_order(self):
        g = fresh()
        # Inject with EXPLICIT created (store sorts by created then id;
        # created has 1s resolution via the command surface, so ordering
        # tests control it directly).
        for i, t in enumerate(("Zeta goal", "Alpha goal", "Mid goal")):
            goal = Goal(A, t, gid="g_%d" % i, created=100 + i)
            goal.steps.append(__import__("goals").Step(1, "step one"))
            g.store.put(goal)
        na1 = [i["title"] for i in g.next_actions(A)]
        na2 = [i["title"] for i in g.next_actions(A)]
        self.assertEqual(na1, na2)                        # stable
        self.assertEqual(na1, ["Zeta goal", "Alpha goal",
                               "Mid goal"])               # created order

    def test_paused_and_done_goals_not_in_next_actions(self):
        g = fresh()
        make_goal(g, title="Active one")
        make_goal(g, title="Paused one")
        g.handle_message(A, "goal paused pause")
        na = g.next_actions(A)
        self.assertEqual([i["title"] for i in na], ["Active one"])

    def test_render_goal_shows_all_states(self):
        g = fresh()
        make_goal(g)
        g.handle_message(A, "goal guitar step 1 done")
        g.handle_message(A, "goal guitar step 2 blocked: no time")
        out = g.render_goal(g._match_goal(A, "guitar"))
        self.assertIn("[x] 1.", out)
        self.assertIn("[!] 2.", out)
        self.assertIn("blocked: no time", out)


class TestMatching(unittest.TestCase):
    def test_ambiguous_goal_match_zero_effects(self):
        g = fresh()
        make_goal(g, title="Learn guitar", steps=())
        make_goal(g, title="Learn piano", steps=())
        reply = g.handle_message(A, "goal learn add step scales")
        self.assertIn("more specific", reply)
        self.assertEqual(len(g._match_goal(A, "guitar").steps), 0)
        self.assertEqual(len(g._match_goal(A, "piano").steps), 0)

    def test_ambiguous_step_match_zero_effects(self):
        g = fresh()
        make_goal(g, steps=("Call the bank monday", "Call the bank tuesday"))
        reply = g.handle_message(A, "goal guitar step bank done")
        self.assertIn("more specific", reply.lower() + reply)
        states = [s.state for s in g._match_goal(A, "guitar").steps]
        self.assertEqual(states, ["pending", "pending"])

    def test_no_match_is_clean_error(self):
        g = fresh()
        make_goal(g)
        self.assertIn("no goal matches",
                      g.handle_message(A, "goal saxophone done"))
        self.assertIn("no step",
                      g.handle_message(A, "goal guitar step 9 done"))


class TestIsolation(unittest.TestCase):
    def test_cross_owner_zero_effects(self):
        g = fresh()
        make_goal(g, A, "A's goal")
        make_goal(g, B, "B's goal", steps=("B step",))
        # B attacks A's goal by exact title
        reply = g.handle_message(B, "goal A's goal delete")
        self.assertIn("no goal matches", reply)
        self.assertEqual(len(g.store.list(A)), 1)
        # A's state invisible in B's views
        self.assertNotIn("A's goal", g.handle_message(B, "my goals"))
        self.assertEqual([i["title"] for i in g.next_actions(B)],
                         ["B's goal"])
        # exports isolated
        self.assertEqual(len(g.export(B)["goals"]), 1)


class TestGrammarAndCaps(unittest.TestCase):
    def test_near_miss_phrasings_fall_through(self):
        g = fresh()
        for msg in ("my goal in life is to be kind", "goal!",
                    "what is my goal", "add a goal sometime",
                    "goal guitar", "next", "what next please"):
            self.assertIsNone(g.handle_message(A, msg), msg)
        self.assertEqual(g.store.count(A), 0)

    def test_commands_create_expected_effects_only(self):
        g = fresh()
        g.handle_message(A, "my goals")          # read-only
        g.handle_message(A, "what's next")       # read-only
        g.handle_message(A, "export my goals")   # read-only
        self.assertEqual(g.store.count(A), 0)

    def test_goal_cap_refuses_without_evicting(self):
        g = fresh()
        for i in range(MAX_GOALS_PER_OWNER):
            g.add_goal(A, "goal %d" % i)
        self.assertEqual(g.store.count(A), MAX_GOALS_PER_OWNER)
        with self.assertRaises(GoalLimitReached):
            g.add_goal(A, "one too many")
        self.assertEqual(g.store.count(A), MAX_GOALS_PER_OWNER)

    def test_step_cap_refused(self):
        g = fresh()
        goal = g.add_goal(A, "big goal")
        for i in range(MAX_STEPS_PER_GOAL):
            g.add_step(A, "big", "step %d" % i)
        with self.assertRaises(GoalError):
            g.add_step(A, "big", "one too many")
        self.assertEqual(len(g._match_goal(A, "big").steps),
                         MAX_STEPS_PER_GOAL)

    def test_export_shape(self):
        g = fresh()
        make_goal(g)
        data = g.export(A)
        self.assertEqual(data["format"], "crayon-goals-v1")
        self.assertEqual(data["goals"][0]["steps"][0]["text"], "Buy tuner")
        json.dumps(data)


if __name__ == "__main__":
    unittest.main()
