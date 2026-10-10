"""ports/patterns - pattern cards: reviewable, never auto-acting.

Companion-trait build (Dots "proactive research is READ-ONLY" + Muse
"opt-in proactive suggestions"): the integration feeds OBSERVATIONS
(owner-supplied event records), the engine spots simple recurring
patterns with EXPLICIT deterministic rules, and surfaces them as cards
the owner can accept, dismiss or snooze. Cards are suggestions with
evidence - they never create goals, reminders or messages by
themselves. Stands alone.

Design invariants:

1. READ-ONLY BY CONSTRUCTION: there is no code path from a card to any
   action. accept() marks the card accepted and returns the card - what
   the integration does with an accepted card is its own gated flow
   (and goes through ports/approvals when the target action needs it).
2. EVIDENCE IS VISIBLE: every card carries the rule id, the matching
   observation count, and first/last seen timestamps. No card without
   evidence; no evidence below the rule's minimum count.
3. RULES ARE DATA AND DETERMINISTIC: window, minimum count, cooldown.
   No model calls, no fuzzy inference. Same input -> same cards.
4. OPT-IN: observation recording is gated per owner (patterns on/off);
   off means observe() is a no-op returning None, and wiping deletes
   observations AND cards.
5. OWNER-STRICT: observations, cards and decisions are keyed to the
   owning account only.
6. CAPS REFUSE, NEVER EVICT: MAX_OBSERVATIONS and MAX_CARDS_PER_OWNER;
   overflow raises with zero effects - history is never silently
   dropped to make room.
7. NO DUPLICATE CARDS: a rule that already has an open card for the
   same signature does not re-raise; a fresh card may be raised after
   dismissal cooldown.
8. CLOCKS ARE INJECTED: every time-dependent method takes `now`.
9. NOT MOUNTED, NO DELIVERY: surfacing cards to the user is the
   integration's job; this module owns state and decisions only.
"""

import json
import re
import time

CARD_STATES = ("open", "accepted", "dismissed", "snoozed")
MAX_OBSERVATIONS = 500
MAX_CARDS_PER_OWNER = 30
MAX_KIND = 40
MAX_DETAIL = 200
MAX_CARDS_PER_RULE = 5  # open+snoozed, per rule, per owner


class PatternError(Exception):
    pass


class ObservationLimitReached(PatternError):
    pass


class CardLimitReached(PatternError):
    pass


class NoMatch(PatternError):
    pass


class AmbiguousMatch(PatternError):
    def __init__(self, kind, ref, cands):
        super().__init__("ambiguous %s %r: %d candidates - be more "
                         "specific" % (kind, ref, len(cands)))
        self.cands = cands


def _norm(text):
    return re.sub(r"\s+", " ", (text or "").strip()).lower()


class Rule:
    """A deterministic detection rule.

    kind: observation kind to match (exact, normalized).
    window: seconds; min_count: matches needed inside the window.
    cooldown: seconds after dismissal before the rule may re-raise.
    title: card title template; may contain {kind} and {count}.
    """

    __slots__ = ("id", "kind", "window", "min_count", "cooldown",
                 "title")

    def __init__(self, rid, kind, window, min_count, cooldown, title):
        if not kind or not kind.strip():
            raise PatternError("rule kind required")
        if window <= 0 or min_count < 2 or cooldown < 0:
            raise PatternError("bad rule parameters")
        self.id = rid
        self.kind = _norm(kind)[:MAX_KIND]
        self.window = int(window)
        self.min_count = int(min_count)
        self.cooldown = int(cooldown)
        self.title = title[:MAX_DETAIL]


DEFAULT_RULES = [
    Rule("weekly-recur", "any", 7 * 86400, 3, 3 * 86400,
         "You often log '{kind}' - {count} times this week. Track it?"),
]


class Observation:
    __slots__ = ("id", "owner", "kind", "detail", "ts")

    def __init__(self, oid, owner, kind, detail, ts):
        self.id = oid
        self.owner = owner
        self.kind = kind
        self.detail = detail
        self.ts = ts

    def to_dict(self):
        return {"id": self.id, "kind": self.kind, "detail": self.detail,
                "ts": self.ts}


class Card:
    __slots__ = ("id", "owner", "rule_id", "title", "signature",
                 "evidence_count", "first_seen", "last_seen", "state",
                 "created", "decided", "snoozed_until")

    def __init__(self, cid, owner, rule, signature, evidence, created):
        self.id = cid
        self.owner = owner
        self.rule_id = rule.id
        self.title = rule.title
        self.signature = signature
        self.evidence_count = len(evidence)
        self.first_seen = min(o.ts for o in evidence)
        self.last_seen = max(o.ts for o in evidence)
        self.state = "open"
        self.created = created
        self.decided = None
        self.snoozed_until = None

    def to_dict(self):
        return {"id": self.id, "rule_id": self.rule_id,
                "title": self.title, "signature": self.signature,
                "evidence_count": self.evidence_count,
                "first_seen": self.first_seen,
                "last_seen": self.last_seen, "state": self.state,
                "created": self.created, "decided": self.decided,
                "snoozed_until": self.snoozed_until}


class MemPatternStore:
    """In-memory reference store. SQL adapter is mount work."""

    def __init__(self):
        self._obs = {}
        self._cards = {}
        self._enabled = set()

    def is_enabled(self, owner):
        return owner in self._enabled

    def set_enabled(self, owner, on):
        if on:
            self._enabled.add(owner)
        else:
            self._enabled.discard(owner)

    def put_obs(self, o):
        self._obs[o.id] = o

    def list_obs(self, owner, kind=None, since=None):
        out = [o for o in self._obs.values() if o.owner == owner]
        if kind is not None:
            out = [o for o in out if o.kind == kind]
        if since is not None:
            out = [o for o in out if o.ts >= since]
        return sorted(out, key=lambda o: (o.ts, o.id))

    def count_obs(self, owner):
        return sum(1 for o in self._obs.values() if o.owner == owner)

    def put_card(self, c):
        self._cards[c.id] = c

    def get_card(self, owner, cid):
        c = self._cards.get(cid)
        return c if c and c.owner == owner else None

    def list_cards(self, owner, states=None):
        out = [c for c in self._cards.values() if c.owner == owner]
        if states:
            out = [c for c in out if c.state in states]
        return sorted(out, key=lambda c: (c.created, c.id))

    def count_cards(self, owner):
        return sum(1 for c in self._cards.values() if c.owner == owner)

    def wipe(self, owner):
        self._obs = {k: v for k, v in self._obs.items()
                     if v.owner != owner}
        self._cards = {k: v for k, v in self._cards.items()
                       if v.owner != owner}
        self._enabled.discard(owner)


class Patterns:
    def __init__(self, store, rules=None, id_factory=None):
        self.store = store
        self.rules = list(rules if rules is not None else DEFAULT_RULES)
        self._ids = id_factory
        self._seq = 0

    def _now(self, now):
        return int(now if now is not None else time.time())

    def _next_id(self, prefix, now):
        self._seq += 1
        if self._ids:
            return self._ids(prefix, self._seq, now)
        return "%s_%012x%06x" % (prefix, now, self._seq)

    # ---------- opt-in ----------

    def enable(self, owner):
        self.store.set_enabled(owner, True)

    def disable(self, owner):
        self.store.set_enabled(owner, False)

    def wipe(self, owner):
        self.store.wipe(owner)

    # ---------- recording ----------

    def observe(self, owner, kind, detail="", ts=None, now=None):
        """Record an observation. No-op returning None when not opted in.

        ts is the EVENT time (integration-supplied, explicit); now is the
        processing clock. Both injected in tests.
        """
        if not self.store.is_enabled(owner):
            return None
        now = self._now(now)
        kind = _norm(kind)[:MAX_KIND]
        if not kind:
            raise PatternError("observation kind required")
        detail = (detail or "").strip()[:MAX_DETAIL]
        if self.store.count_obs(owner) >= MAX_OBSERVATIONS:
            raise ObservationLimitReached(
                "observation log is full (%d); wipe or wait for "
                "retention policy" % MAX_OBSERVATIONS)
        o = Observation(self._next_id("o", now), owner, kind, detail,
                        int(ts if ts is not None else now))
        self.store.put_obs(o)
        return o

    # ---------- detection ----------

    def scan(self, owner, now=None):
        """Run all rules over the observation window. Returns new cards.

        Deterministic: same observations + same now -> same cards.
        Never duplicates an open/snoozed card for the same
        (rule, signature); respects dismissal cooldowns and per-rule and
        per-owner card caps (overflow refuses with zero effects).
        """
        now = self._now(now)
        if not self.store.is_enabled(owner):
            return []
        new_cards = []
        for rule in self.rules:
            kinds = ({o.kind for o in self.store.list_obs(owner)}
                     if rule.kind == "any" else {rule.kind})
            for kind in sorted(kinds):
                matches = self.store.list_obs(owner, kind=kind,
                                              since=now - rule.window)
                if len(matches) < rule.min_count:
                    continue
                signature = "%s:%s" % (rule.id, kind)
                existing = [c for c in self.store.list_cards(owner)
                            if c.signature == signature]
                if any(c.state in ("open", "accepted") or
                       (c.state == "snoozed" and
                        (c.snoozed_until or 0) > now)
                       for c in existing):
                    # open: already surfaced; accepted: owner engaged -
                    # never re-nag the same signature in v1
                    continue
                dismissed = [c for c in existing
                             if c.state == "dismissed"]
                if dismissed and now - max(c.decided for c in dismissed) \
                        < rule.cooldown:
                    continue
                live = [c for c in existing
                        if c.state in ("open", "snoozed")]
                if len(live) >= MAX_CARDS_PER_RULE:
                    continue
                if self.store.count_cards(owner) >= MAX_CARDS_PER_OWNER:
                    raise CardLimitReached(
                        "pattern card list is full (%d); accept or "
                        "dismiss some first" % MAX_CARDS_PER_OWNER)
                c = Card(self._next_id("c", now), owner, rule, signature,
                         matches, now)
                c.title = rule.title.replace("{kind}", kind).replace(
                    "{count}", str(len(matches)))
                self.store.put_card(c)
                new_cards.append(c)
        return new_cards

    # ---------- decisions (never actions) ----------

    def _match_card(self, owner, match, states=("open", "snoozed")):
        match = _norm(match)
        if not match:
            raise NoMatch("card reference required - refusing to guess")
        cands = [c for c in self.store.list_cards(owner, states=states)
                 if match in _norm(c.id) or match in _norm(c.title)
                 or match in _norm(c.signature)]
        if not cands:
            raise NoMatch("no card matches %r" % match)
        if len(cands) > 1:
            raise AmbiguousMatch("card", match, cands)
        return cands[0]

    def accept(self, owner, match, now=None):
        """Mark a card accepted. RETURNS THE CARD - does NOT act on it.

        What an accepted card means (create a goal? a reminder?) is the
        integration's own gated flow, outside this module.
        """
        now = self._now(now)
        c = self._match_card(owner, match)
        c.state = "accepted"
        c.decided = now
        self.store.put_card(c)
        return c

    def dismiss(self, owner, match, now=None):
        now = self._now(now)
        c = self._match_card(owner, match)
        c.state = "dismissed"
        c.decided = now
        self.store.put_card(c)
        return c

    def snooze(self, owner, match, days, now=None):
        now = self._now(now)
        if not days or days <= 0 or days > 90:
            raise PatternError("snooze must be 1..90 days")
        c = self._match_card(owner, match)
        c.state = "snoozed"
        c.decided = now
        c.snoozed_until = now + int(days) * 86400
        self.store.put_card(c)
        return c

    # ---------- chat surface ----------

    def handle_message(self, owner, text, now=None):
        """Explicit grammar; anything else returns None (falls through).

        patterns on | patterns off | wipe my patterns |
        my patterns | pattern <match> accept|dismiss|snooze <days> |
        pattern <match> show | export my patterns
        """
        now = self._now(now)
        low = _norm(text)
        if low == "patterns on":
            self.enable(owner)
            return "Pattern cards on. Observations you send me can " \
                "become suggestions - reviewing them is always yours."
        if low == "patterns off":
            self.disable(owner)
            return "Pattern cards off. Existing cards kept; nothing " \
                "new is recorded."
        if low == "wipe my patterns":
            self.wipe(owner)
            return "Pattern observations and cards deleted."
        if low == "my patterns":
            cards = self.store.list_cards(owner, states=("open",
                                                         "snoozed"))
            if not cards:
                return "No open pattern cards."
            lines = ["Open pattern cards:"]
            for c in cards:
                lines.append("- %s [%s, %d observations]"
                             % (c.title, c.id, c.evidence_count))
            return "\n".join(lines)
        m = re.match(r"^pattern\s+(.+?)\s+(accept|dismiss|"
                     r"snooze(?:\s+(\d+))?|show)$", low)
        if m:
            ref, action, days = m.group(1), m.group(2), m.group(3)
            try:
                if action == "accept":
                    c = self.accept(owner, ref, now=now)
                    return "Accepted: %s. Nothing happens by itself - " \
                        "tell me what to do with it." % c.title
                if action == "dismiss":
                    c = self.dismiss(owner, ref, now=now)
                    return "Dismissed: %s." % c.title
                if action == "show":
                    c = self._match_card(owner, ref,
                                         states=CARD_STATES)
                    return json.dumps(c.to_dict(), indent=2,
                                      sort_keys=True)
                c = self.snooze(owner, ref, int(days or 7), now=now)
                return "Snoozed: %s (%d days)." % (c.title,
                                                   int(days or 7))
            except (PatternError, NoMatch, AmbiguousMatch) as e:
                return str(e)
        if low == "export my patterns":
            return json.dumps({
                "version": "crayon-patterns-v1",
                "observations": [o.to_dict()
                                 for o in self.store.list_obs(owner)],
                "cards": [c.to_dict()
                          for c in self.store.list_cards(owner)],
            }, indent=2, sort_keys=True)
        return None


VERSION = "crayon-patterns-v1"
