"""ports/approvals - exact approval cards enforced OUTSIDE prompts.

Companion-trait build (priority #4): actions that need owner approval get
a structured, exact approval card - and the ENGINE, not a prompt, decides
whether a presented approval satisfies a pending request. The model can
propose; only a recorded, exact, unexpired, single-use owner decision
permits. This module owns decisions and cards only - executing the
approved action is the integration's job. Stands alone.

Design invariants:

1. CARDS ARE EXACT: a card binds owner + action + target + a canonical
   digest of the params. Presenting a card whose fields do not match the
   pending request EXACTLY refuses - no fuzzy matching, no "close
   enough".
2. SINGLE-USE: consuming a card burns it. Replay refuses, even for an
   identical request - a new request needs a new card.
3. EXPIRY: every card carries an absolute expiry (injected clock). An
   expired card refuses and is recorded as such.
4. DECISIONS ARE RECORDED: every request/decision/consume/refusal lands
   in an inspectable audit log per owner (capped) with reasons - no
   silent behavior.
5. POLICY IS DATA: risk classes map to requirements (auto / card /
   refuse). Unknown actions DEFAULT TO REQUIRE CARD - never to allow.
6. CAPS REFUSE, NEVER EVICT: MAX_PENDING per owner; the (cap+1)th
   pending request raises with zero effects.
7. OWNER-STRICT: requests, cards, decisions and audit are keyed to the
   owning account only. Cross-owner presentation refuses.
8. CLOCKS ARE INJECTED: every time-dependent method takes `now`; the
   module reads the wall clock only when the integration omits it.
9. NO EXECUTION: approve() marks the request approved and returns the
   request record; running the action is the integration's job.
"""

import hashlib
import json
import re
import time

REQUIRE_AUTO = "auto"      # policy: proceed without a card
REQUIRE_CARD = "card"      # policy: needs an exact owner card
REQUIRE_REFUSE = "refuse"  # policy: never permitted via this surface

REQUEST_STATES = ("pending", "approved", "denied", "expired", "consumed")
CARD_STATES = ("issued", "consumed", "expired", "revoked")

MAX_PENDING_PER_OWNER = 25
MAX_AUDIT_PER_OWNER = 200
MAX_ACTION = 60
MAX_TARGET = 200
MAX_PARAMS = 4000
CARD_TTL_SECONDS = 900  # 15 minutes default


class ApprovalError(Exception):
    pass


class PendingLimitReached(ApprovalError):
    pass


class NoMatch(ApprovalError):
    pass


class AmbiguousMatch(ApprovalError):
    def __init__(self, kind, ref, cands):
        super().__init__("ambiguous %s %r: %d candidates - be more "
                         "specific" % (kind, ref, len(cands)))
        self.cands = cands


def _norm(text):
    return re.sub(r"\s+", " ", (text or "").strip()).lower()


def params_digest(params):
    """Canonical digest of a params mapping. Exact-match key for cards."""
    if params is None:
        params = {}
    if not isinstance(params, dict):
        raise ApprovalError("params must be a mapping")
    blob = json.dumps(params, sort_keys=True, separators=(",", ":"))
    if len(blob) > MAX_PARAMS:
        raise ApprovalError("params too large (%d bytes max)" % MAX_PARAMS)
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()


class Request:
    __slots__ = ("id", "owner", "action", "target", "digest", "state",
                 "created", "decided", "note")

    def __init__(self, rid, owner, action, target, digest, created):
        self.id = rid
        self.owner = owner
        self.action = action
        self.target = target
        self.digest = digest
        self.state = "pending"
        self.created = created
        self.decided = None
        self.note = ""

    def to_dict(self):
        return {"id": self.id, "owner": self.owner, "action": self.action,
                "target": self.target, "digest": self.digest,
                "state": self.state, "created": self.created,
                "decided": self.decided, "note": self.note}


class Card:
    __slots__ = ("id", "owner", "action", "target", "digest", "state",
                 "issued", "expires", "consumed")

    def __init__(self, cid, owner, action, target, digest, issued, ttl):
        self.id = cid
        self.owner = owner
        self.action = action
        self.target = target
        self.digest = digest
        self.state = "issued"
        self.issued = issued
        self.expires = issued + ttl
        self.consumed = None

    def to_dict(self):
        return {"id": self.id, "owner": self.owner, "action": self.action,
                "target": self.target, "digest": self.digest,
                "state": self.state, "issued": self.issued,
                "expires": self.expires, "consumed": self.consumed}


class MemApprovalStore:
    """In-memory reference store. SQL adapter is mount work."""

    def __init__(self):
        self._requests = {}
        self._cards = {}
        self._audit = {}

    def put_request(self, r):
        self._requests[r.id] = r

    def get_request(self, owner, rid):
        r = self._requests.get(rid)
        return r if r and r.owner == owner else None

    def list_requests(self, owner, states=None):
        out = [r for r in self._requests.values() if r.owner == owner]
        if states:
            out = [r for r in out if r.state in states]
        return sorted(out, key=lambda r: (r.created, r.id))

    def put_card(self, c):
        self._cards[c.id] = c

    def get_card(self, owner, cid):
        c = self._cards.get(cid)
        return c if c and c.owner == owner else None

    def audit(self, owner, ts, event, detail=""):
        log = self._audit.setdefault(owner, [])
        log.append({"ts": ts, "event": event, "detail": detail})
        if len(log) > MAX_AUDIT_PER_OWNER:
            del log[:len(log) - MAX_AUDIT_PER_OWNER]

    def get_audit(self, owner):
        return list(self._audit.get(owner, []))


class Approvals:
    """Exact approval cards enforced outside prompts.

    policy: {action: REQUIRE_*}. Unknown actions REQUIRE CARD.
    id_factory: injected for deterministic ids (tests); default derives
    from time+counter.
    """

    def __init__(self, store, policy=None, card_ttl=CARD_TTL_SECONDS,
                 id_factory=None):
        self.store = store
        self.policy = dict(policy or {})
        self.card_ttl = card_ttl
        self._ids = id_factory
        self._seq = 0

    # ---------- internals ----------

    def _now(self, now):
        return int(now if now is not None else time.time())

    def _next_id(self, prefix, now):
        self._seq += 1
        if self._ids:
            return self._ids(prefix, self._seq, now)
        return "%s_%012x%06x" % (prefix, now, self._seq)

    def _validate_fields(self, action, target):
        action = (action or "").strip()
        target = (target or "").strip()
        if not action:
            raise ApprovalError("action required")
        if not target:
            raise ApprovalError("target required")
        # REJECT overflow - truncating would alias two distinct long
        # actions/targets onto one exact-match key
        if len(action) > MAX_ACTION:
            raise ApprovalError("action too long (%d chars max)"
                                % MAX_ACTION)
        if len(target) > MAX_TARGET:
            raise ApprovalError("target too long (%d chars max)"
                                % MAX_TARGET)
        return action, target

    # ---------- core API ----------

    def requirement(self, action):
        return self.policy.get(action, REQUIRE_CARD)

    def request(self, owner, action, target, params=None, now=None):
        """Register an approval request. Returns (Request, requirement).

        requirement == REQUIRE_AUTO: no request is stored; caller may
        proceed directly. REQUIRE_REFUSE: stored as denied with a note.
        REQUIRE_CARD: stored pending; needs a matching card via approve().
        """
        now = self._now(now)
        action, target = self._validate_fields(action, target)
        req = self.requirement(action)
        if req == REQUIRE_AUTO:
            self.store.audit(owner, now, "auto_allowed",
                             "%s -> %s" % (action, target))
            return None, REQUIRE_AUTO
        digest = params_digest(params)
        if req == REQUIRE_REFUSE:
            r = Request(self._next_id("r", now), owner, action, target,
                        digest, now)
            r.state = "denied"
            r.decided = now
            r.note = "policy refuses this action"
            self.store.put_request(r)
            self.store.audit(owner, now, "policy_refused",
                             "%s -> %s" % (action, target))
            return r, REQUIRE_REFUSE
        pending = self.store.list_requests(owner, states=("pending",))
        if len(pending) >= MAX_PENDING_PER_OWNER:
            raise PendingLimitReached(
                "too many pending approvals (%d); approve or deny some "
                "first" % MAX_PENDING_PER_OWNER)
        r = Request(self._next_id("r", now), owner, action, target,
                    digest, now)
        self.store.put_request(r)
        self.store.audit(owner, now, "requested", "%s -> %s (%s)"
                         % (action, target, r.id))
        return r, REQUIRE_CARD

    def issue_card(self, owner, action, target, params=None, now=None,
                   ttl=None):
        """The OWNER's act of approval, recorded as an exact card.

        Only the owning account can issue for itself - the integration
        calls this only on an authenticated owner command. Cards are
        exact, single-use and expiring.
        """
        now = self._now(now)
        action, target = self._validate_fields(action, target)
        digest = params_digest(params)
        c = Card(self._next_id("c", now), owner, action, target, digest,
                 now, self.card_ttl if ttl is None else int(ttl))
        self.store.put_card(c)
        self.store.audit(owner, now, "card_issued", "%s -> %s (%s)"
                         % (action, target, c.id))
        return c

    def _match_request(self, owner, match, states=("pending",)):
        match = _norm(match)
        if not match:
            raise NoMatch("request reference required - refusing to guess")
        cands = [r for r in self.store.list_requests(owner, states=states)
                 if match in _norm(r.id) or match in _norm(r.action)
                 or match in _norm(r.target)]
        if not cands:
            raise NoMatch("no request matches %r" % match)
        if len(cands) > 1:
            raise AmbiguousMatch("request", match, cands)
        return cands[0]

    def approve(self, owner, match, card_id, now=None):
        """Consume CARD_ID against the pending request MATCH.

        The engine - not a prompt - checks owner, action, target, params
        digest, state, and expiry. Exact match or refusal, recorded
        either way.
        """
        now = self._now(now)
        r = self._match_request(owner, match)
        c = self.store.get_card(owner, card_id)
        if c is None:
            self.store.audit(owner, now, "approve_refused",
                             "%s: unknown card %r" % (r.id, card_id))
            raise ApprovalError("unknown card %r" % card_id)
        if c.state != "issued":
            self.store.audit(owner, now, "approve_refused",
                             "%s: card %s already %s" % (r.id, c.id,
                                                         c.state))
            raise ApprovalError("card %s already %s - approvals are "
                                "single-use" % (c.id, c.state))
        if now >= c.expires:
            c.state = "expired"
            self.store.put_card(c)
            self.store.audit(owner, now, "approve_refused",
                             "%s: card %s expired" % (r.id, c.id))
            raise ApprovalError("card %s expired - issue a fresh "
                                "approval" % c.id)
        if (c.action, c.target, c.digest) != (r.action, r.target,
                                              r.digest):
            self.store.audit(owner, now, "approve_refused",
                             "%s: card %s does not match exactly"
                             % (r.id, c.id))
            raise ApprovalError("card does not match this request "
                                "exactly - approvals are exact")
        c.state = "consumed"
        c.consumed = now
        self.store.put_card(c)
        r.state = "approved"
        r.decided = now
        r.note = "card %s" % c.id
        self.store.put_request(r)
        self.store.audit(owner, now, "approved", "%s via %s"
                         % (r.id, c.id))
        return r

    def deny(self, owner, match, now=None, reason=""):
        now = self._now(now)
        r = self._match_request(owner, match)
        r.state = "denied"
        r.decided = now
        r.note = (reason or "")[:MAX_TARGET]
        self.store.put_request(r)
        self.store.audit(owner, now, "denied", r.id)
        return r

    def sweep_expired(self, owner, now=None):
        """Mark pending requests whose matched window passed as expired.

        Requests do not expire on their own timer in v1 (a pending
        request simply waits); cards do. This sweeps issued cards past
        expiry so listings stay honest.
        """
        now = self._now(now)
        n = 0
        for c in list(self.store._cards.values()):
            if c.owner == owner and c.state == "issued" and now >= c.expires:
                c.state = "expired"
                self.store.put_card(c)
                n += 1
        if n:
            self.store.audit(owner, now, "cards_expired", str(n))
        return n

    def list_pending(self, owner):
        return self.store.list_requests(owner, states=("pending",))

    def audit(self, owner):
        return self.store.get_audit(owner)

    # ---------- chat surface ----------

    def handle_message(self, owner, text, now=None):
        """Explicit grammar; anything else returns None (falls through).

        pending approvals | approve <match> with card <id> |
        deny <match> [reason] | approval <match> show |
        approval audit | export my approvals
        """
        now = self._now(now)
        low = _norm(text)
        if low in ("pending approvals", "my approvals"):
            pending = self.list_pending(owner)
            if not pending:
                return "No pending approvals."
            lines = ["Pending approvals:"]
            for r in pending:
                lines.append("- %s: %s -> %s (id %s)"
                             % (r.id, r.action, r.target, r.id))
            return "\n".join(lines)
        m = re.match(r"^approve\s+(.+?)\s+with card\s+(\S+)$", low)
        if m:
            try:
                r = self.approve(owner, m.group(1), m.group(2), now=now)
            except (ApprovalError, NoMatch, AmbiguousMatch) as e:
                return str(e)
            return "Approved: %s -> %s (request %s, card consumed)." \
                % (r.action, r.target, r.id)
        m = re.match(r"^deny\s+(.+?)(?:\s+because\s+(.+))?$", low)
        if m:
            try:
                r = self.deny(owner, m.group(1), now=now,
                              reason=m.group(2) or "")
            except (ApprovalError, NoMatch, AmbiguousMatch) as e:
                return str(e)
            return "Denied: %s -> %s (request %s)." \
                % (r.action, r.target, r.id)
        m = re.match(r"^approval\s+(.+?)\s+show$", low)
        if m:
            try:
                r = self._match_request(owner, m.group(1),
                                        states=REQUEST_STATES)
            except (ApprovalError, NoMatch, AmbiguousMatch) as e:
                return str(e)
            return json.dumps(r.to_dict(), indent=2, sort_keys=True)
        if low == "approval audit":
            log = self.audit(owner)
            if not log:
                return "No approval events yet."
            return "\n".join("- %d %s %s" % (e["ts"], e["event"],
                                             e["detail"]) for e in log)
        if low == "export my approvals":
            return json.dumps({
                "version": "crayon-approvals-v1",
                "requests": [r.to_dict()
                             for r in self.store.list_requests(owner)],
                "audit": self.audit(owner),
            }, indent=2, sort_keys=True)
        return None


VERSION = "crayon-approvals-v1"
