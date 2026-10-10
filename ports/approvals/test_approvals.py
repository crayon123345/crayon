import sys, os, json, unittest
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from approvals import (Approvals, MemApprovalStore, ApprovalError,
                       PendingLimitReached, NoMatch, AmbiguousMatch,
                       REQUIRE_AUTO, REQUIRE_CARD, REQUIRE_REFUSE,
                       MAX_PENDING_PER_OWNER, params_digest)

T0 = 1791633600  # 2026-10-10T12:00:00Z


def ids(prefix, seq, now):
    return "%s_t%03d" % (prefix, seq)


def make(policy=None, ttl=900):
    return Approvals(MemApprovalStore(), policy=policy, card_ttl=ttl,
                     id_factory=ids)


class PolicyTests(unittest.TestCase):
    def test_unknown_action_requires_card_never_auto(self):
        s = make()
        r, req = s.request("v_a", "brand.new.action", "thing", now=T0)
        self.assertEqual(req, REQUIRE_CARD)
        self.assertEqual(r.state, "pending")

    def test_auto_action_stores_nothing(self):
        s = make(policy={"read.public": REQUIRE_AUTO})
        r, req = s.request("v_a", "read.public", "page", now=T0)
        self.assertIsNone(r)
        self.assertEqual(s.list_pending("v_a"), [])
        self.assertEqual(s.audit("v_a")[0]["event"], "auto_allowed")

    def test_refuse_action_denied_immediately(self):
        s = make(policy={"pay.money": REQUIRE_REFUSE})
        r, req = s.request("v_a", "pay.money", "vendor", now=T0)
        self.assertEqual(req, REQUIRE_REFUSE)
        self.assertEqual(r.state, "denied")
        # and no card can revive it - no pending request exists
        c = s.issue_card("v_a", "pay.money", "vendor", now=T0)
        with self.assertRaises(NoMatch):
            s.approve("v_a", "pay.money", c.id, now=T0)


class ExactnessTests(unittest.TestCase):
    def setUp(self):
        self.s = make()
        self.r, _ = self.s.request("v_a", "post.message", "#general",
                                   {"text": "hello"}, now=T0)

    def test_exact_card_approves(self):
        c = self.s.issue_card("v_a", "post.message", "#general",
                              {"text": "hello"}, now=T0)
        r = self.s.approve("v_a", "post.message", c.id, now=T0 + 10)
        self.assertEqual(r.state, "approved")

    def test_wrong_params_refused(self):
        c = self.s.issue_card("v_a", "post.message", "#general",
                              {"text": "HELLO"}, now=T0)
        with self.assertRaises(ApprovalError):
            self.s.approve("v_a", "post.message", c.id, now=T0 + 10)
        self.assertEqual(self.s.list_pending("v_a")[0].state, "pending")

    def test_wrong_target_refused(self):
        c = self.s.issue_card("v_a", "post.message", "#random",
                              {"text": "hello"}, now=T0)
        with self.assertRaises(ApprovalError):
            self.s.approve("v_a", "post.message", c.id, now=T0 + 10)

    def test_wrong_action_refused(self):
        c = self.s.issue_card("v_a", "delete.message", "#general",
                              {"text": "hello"}, now=T0)
        with self.assertRaises(ApprovalError):
            self.s.approve("v_a", "post.message", c.id, now=T0 + 10)

    def test_param_order_irrelevant_but_values_exact(self):
        c = self.s.issue_card("v_a", "post.message", "#general",
                              {"text": "hello", "extra": 1}, now=T0)
        with self.assertRaises(ApprovalError):  # extra key = not exact
            self.s.approve("v_a", "post.message", c.id, now=T0 + 10)


class SingleUseAndExpiryTests(unittest.TestCase):
    def test_card_is_single_use(self):
        s = make()
        r1, _ = s.request("v_a", "post.message", "#g", now=T0)
        c = s.issue_card("v_a", "post.message", "#g", now=T0)
        s.approve("v_a", "post.message", c.id, now=T0 + 10)
        # identical second request: replaying the same card refuses
        r2, _ = s.request("v_a", "post.message", "#g", now=T0 + 20)
        with self.assertRaises(ApprovalError):
            s.approve("v_a", "post.message", c.id, now=T0 + 30)
        self.assertEqual(s.list_pending("v_a")[0].id, r2.id)

    def test_expired_card_refused_and_marked(self):
        s = make(ttl=60)
        r, _ = s.request("v_a", "post.message", "#g", now=T0)
        c = s.issue_card("v_a", "post.message", "#g", now=T0)
        with self.assertRaises(ApprovalError):
            s.approve("v_a", "post.message", c.id, now=T0 + 61)
        self.assertEqual(s.store.get_card("v_a", c.id).state, "expired")
        self.assertEqual(s.list_pending("v_a")[0].state, "pending")

    def test_fresh_card_still_approves_after_expiry(self):
        s = make(ttl=60)
        s.request("v_a", "post.message", "#g", now=T0)
        c1 = s.issue_card("v_a", "post.message", "#g", now=T0)
        with self.assertRaises(ApprovalError):
            s.approve("v_a", "post.message", c1.id, now=T0 + 61)
        c2 = s.issue_card("v_a", "post.message", "#g", now=T0 + 62)
        r = s.approve("v_a", "post.message", c2.id, now=T0 + 63)
        self.assertEqual(r.state, "approved")

    def test_sweep_expired(self):
        s = make(ttl=60)
        c = s.issue_card("v_a", "a", "b", now=T0)
        self.assertEqual(s.sweep_expired("v_a", now=T0 + 59), 0)
        self.assertEqual(s.sweep_expired("v_a", now=T0 + 60), 1)
        self.assertEqual(s.store.get_card("v_a", c.id).state, "expired")


class IsolationAndAmbiguityTests(unittest.TestCase):
    def test_cross_owner_card_refused(self):
        s = make()
        s.request("v_a", "post.message", "#g", now=T0)
        c = s.issue_card("v_b", "post.message", "#g", now=T0)
        with self.assertRaises(ApprovalError):
            s.approve("v_a", "post.message", c.id, now=T0 + 10)

    def test_cross_owner_invisible(self):
        s = make()
        s.request("v_a", "post.message", "#g", now=T0)
        self.assertEqual(s.list_pending("v_b"), [])
        self.assertEqual(s.handle_message("v_b", "pending approvals",
                                          now=T0),
                         "No pending approvals.")
        with self.assertRaises(NoMatch):
            s.deny("v_b", "post.message", now=T0)

    def test_ambiguous_match_refused_zero_effects(self):
        s = make()
        s.request("v_a", "post.message", "#general", now=T0)
        s.request("v_a", "post.message", "#general-chat", now=T0)
        with self.assertRaises(AmbiguousMatch):
            s.approve("v_a", "post.message", "c_x", now=T0)
        self.assertEqual(len(s.list_pending("v_a")), 2)

    def test_empty_match_refused(self):
        s = make()
        s.request("v_a", "post.message", "#g", now=T0)
        for bad in ("", "   "):
            with self.assertRaises(NoMatch):
                s.deny("v_a", bad, now=T0)


class CapAndGrammarTests(unittest.TestCase):
    def test_pending_cap_refuses(self):
        s = make()
        for i in range(MAX_PENDING_PER_OWNER):
            s.request("v_a", "a%d" % i, "t", now=T0)
        with self.assertRaises(PendingLimitReached):
            s.request("v_a", "overflow", "t", now=T0)
        # deny makes room
        s.deny("v_a", "a0", now=T0)
        s.request("v_a", "replacement", "t", now=T0)

    def test_grammar_fallthrough(self):
        s = make()
        for msg in ("approve", "approve something", "deny",
                    "approval audit trail", "what approvals"):
            self.assertIsNone(s.handle_message("v_a", msg, now=T0))

    def test_chat_roundtrip(self):
        s = make()
        r, _ = s.request("v_a", "post.message", "#general", now=T0)
        out = s.handle_message("v_a", "pending approvals", now=T0)
        self.assertIn("post.message", out)
        c = s.issue_card("v_a", "post.message", "#general", now=T0)
        out = s.handle_message(
            "v_a", "approve post.message with card %s" % c.id, now=T0 + 5)
        self.assertIn("Approved", out)
        out = s.handle_message("v_a", "approval audit", now=T0 + 6)
        self.assertIn("approved", out)
        exp = json.loads(s.handle_message("v_a", "export my approvals",
                                          now=T0 + 7))
        self.assertEqual(exp["version"], "crayon-approvals-v1")
        self.assertEqual(exp["requests"][0]["state"], "approved")


class AuditTests(unittest.TestCase):
    def test_every_decision_recorded(self):
        s = make()
        s.request("v_a", "post.message", "#g", now=T0)
        c = s.issue_card("v_a", "post.message", "#g", now=T0)
        s.approve("v_a", "post.message", c.id, now=T0 + 5)
        s.request("v_a", "delete.message", "#g", now=T0 + 6)
        s.deny("v_a", "delete.message", now=T0 + 7, reason="no")
        events = [e["event"] for e in s.audit("v_a")]
        self.assertEqual(events, ["requested", "card_issued", "approved",
                                  "requested", "denied"])


if __name__ == "__main__":
    unittest.main()


class Review5479329697RegressionTests(unittest.TestCase):
    """Truncation aliasing: overflow must REJECT, never truncate."""

    def setUp(self):
        self.s = make()

    def test_overlength_target_rejected_on_request_zero_effects(self):
        long_t = "t" * 201
        with self.assertRaises(ApprovalError):
            self.s.request("v_a", "post.message", long_t, now=T0)
        self.assertEqual(self.s.list_pending("v_a"), [])

    def test_overlength_action_rejected_on_request(self):
        with self.assertRaises(ApprovalError):
            self.s.request("v_a", "a" * 61, "t", now=T0)
        self.assertEqual(self.s.list_pending("v_a"), [])

    def test_overlength_rejected_on_issue_card(self):
        with self.assertRaises(ApprovalError):
            self.s.issue_card("v_a", "post.message", "t" * 201, now=T0)

    def test_no_aliasing_between_distinct_long_targets(self):
        # two targets sharing a 200-char prefix must stay distinct errors,
        # never one aliased binding
        t1 = "x" * 199 + "aa"
        t2 = "x" * 199 + "ab"
        for t in (t1, t2):
            with self.assertRaises(ApprovalError):
                self.s.request("v_a", "post.message", t, now=T0)
        self.assertEqual(self.s.list_pending("v_a"), [])

    def test_boundary_lengths_accepted(self):
        r, _ = self.s.request("v_a", "a" * 60, "t" * 200, now=T0)
        self.assertEqual(r.state, "pending")
