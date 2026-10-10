"""Goals - durable per-user goal/task state with explicit checkpoints.

Companion-trait build (Meta Muse "goal-driven personal agent" + OpenAI
Dots "always-on responsibility"): durable goal pages with steps,
blockers, next actions, and EXPLICIT progress/blocker/completion states -
not one-shot chat replies. Stands alone; complements ports/memory
(facts), ports/reflect (conversation mode), ports/journal (record).

Design rules (same discipline as the other ports):
- Every operation takes `owner` explicitly; no ambient identity, no
  cross-owner path exists in this module.
- State is EXPLICIT and INSPECTABLE: every goal has a status and every
  step has a state; next_actions() renders exactly what is next and what
  is blocked. No inferred or hidden progress.
- Matching is deterministic: goal/step references are exact numbers or
  case-insensitive substrings; an AMBIGUOUS match is refused with ZERO
  effects (the reply asks for a more specific match).
- Deletion is explicit and separate from abandonment: `abandon` keeps the
  record with a terminal state; `delete` removes it. Caps refuse, never
  evict (same convention as the other ports).
- This module never calls a model and never schedules anything: reminder
  delivery is the scheduler port's job; this module owns STATE.
- Storage is injected (GoalStore). MemGoalStore is NOT durable; the SQL
  adapter is the integration point named in the README.
"""

import re
import time
import secrets

GOAL_STATUSES = ("active", "paused", "done", "abandoned")
STEP_STATES = ("pending", "doing", "blocked", "done")
MAX_GOALS_PER_OWNER = 100
MAX_STEPS_PER_GOAL = 50
MAX_TITLE = 300
MAX_STEP_TEXT = 300
MAX_BLOCKER = 300
MAX_EXCERPT = 200


class GoalError(Exception):
    pass


class AmbiguousMatch(GoalError):
    def __init__(self, kind, match, candidates):
        super().__init__("ambiguous %s match %r (%d candidates)"
                         % (kind, match, len(candidates)))
        self.candidates = candidates


class NoMatch(GoalError):
    pass


class GoalLimitReached(GoalError):
    pass


def _norm(text):
    return re.sub(r"\s+", " ", (text or "").strip()).lower()


class Step:
    __slots__ = ("n", "text", "state", "blocker", "created", "updated")

    def __init__(self, n, text, state="pending", blocker=None,
                 created=None, updated=None):
        self.n = n
        self.text = (text or "")[:MAX_STEP_TEXT]
        self.state = state if state in STEP_STATES else "pending"
        self.blocker = (blocker or None) and blocker[:MAX_BLOCKER]
        self.created = int(created if created is not None else time.time())
        self.updated = int(updated if updated is not None else self.created)

    def to_dict(self):
        return {"n": self.n, "text": self.text, "state": self.state,
                "blocker": self.blocker, "created": self.created,
                "updated": self.updated}


class Goal:
    __slots__ = ("id", "owner", "title", "status", "steps", "source_sid",
                 "source_ts", "source_excerpt", "created", "updated")

    def __init__(self, owner, title, source_sid=None, source_ts=None,
                 source_excerpt="", gid=None, status="active", steps=None,
                 created=None, updated=None):
        self.id = gid or "g_" + secrets.token_hex(8)
        self.owner = owner
        self.title = (title or "")[:MAX_TITLE]
        self.status = status if status in GOAL_STATUSES else "active"
        self.steps = list(steps or [])
        self.source_sid = source_sid
        self.source_ts = int(source_ts) if source_ts is not None else None
        self.source_excerpt = (source_excerpt or "")[:MAX_EXCERPT]
        self.created = int(created if created is not None else time.time())
        self.updated = int(updated if updated is not None else self.created)

    def to_dict(self):
        return {"id": self.id, "title": self.title, "status": self.status,
                "steps": [s.to_dict() for s in self.steps],
                "source": {"chat": self.source_sid,
                           "message_ts": self.source_ts,
                           "excerpt": self.source_excerpt},
                "created": self.created, "updated": self.updated}


class MemGoalStore:
    """In-memory store. NOT durable - tests and staging only."""

    def __init__(self):
        self._goals = {}

    def put(self, goal):
        self._goals[goal.id] = goal

    def get(self, owner, gid):
        g = self._goals.get(gid)
        return g if g and g.owner == owner else None

    def list(self, owner, statuses=None):
        out = [g for g in self._goals.values() if g.owner == owner]
        if statuses:
            out = [g for g in out if g.status in statuses]
        return sorted(out, key=lambda g: (g.created, g.id))

    def count(self, owner):
        return sum(1 for g in self._goals.values() if g.owner == owner)

    def delete(self, owner, gid):
        g = self.get(owner, gid)
        if g is None:
            return False
        del self._goals[gid]
        return True


class Goals:
    """Durable per-owner goals with explicit checkpoint state."""

    def __init__(self, store):
        self.store = store

    # ---------- matching (deterministic, ambiguity refused) ----------

    def _match_goal(self, owner, match):
        match_n = _norm(match)
        if not match_n:
            raise GoalError("empty goal match")
        cands = [g for g in self.store.list(owner)
                 if match_n in _norm(g.title)]
        if not cands:
            raise NoMatch("no goal matches %r" % match)
        if len(cands) > 1:
            raise AmbiguousMatch("goal", match, cands)
        return cands[0]

    # ---------- core API ----------

    def add_goal(self, owner, title, source_sid=None, source_ts=None,
                 source_excerpt=""):
        title = (title or "").strip()
        if not title:
            raise GoalError("empty goal title")
        title = title[:MAX_TITLE]
        for g in self.store.list(owner):
            if _norm(g.title) == _norm(title) and g.status in ("active",
                                                               "paused"):
                raise GoalError("an open goal with that title already "
                                "exists (id %s)" % g.id)
        live = len(self.store.list(owner, statuses=("active", "paused")))
        if live >= MAX_GOALS_PER_OWNER:
            raise GoalLimitReached(
                "goal list is full (%d active/paused goals); abandon, "
                "complete or delete some first" % MAX_GOALS_PER_OWNER)
        g = Goal(owner, title, source_sid=source_sid, source_ts=source_ts,
                 source_excerpt=source_excerpt)
        self.store.put(g)
        return g

    def add_step(self, owner, goal_match, text):
        g = self._match_goal(owner, goal_match)
        if g.status not in ("active", "paused"):
            raise GoalError("goal %r is %s; steps can only be added to an "
                            "open goal" % (g.title, g.status))
        if len(g.steps) >= MAX_STEPS_PER_GOAL:
            raise GoalError("goal has %d steps already" % MAX_STEPS_PER_GOAL)
        text = (text or "").strip()
        if not text:
            raise GoalError("empty step")
        s = Step(len(g.steps) + 1, text)
        g.steps.append(s)
        g.updated = int(time.time())
        self.store.put(g)
        return g, s

    def _match_step(self, g, ref):
        ref = (ref or "").strip()
        if not ref:
            raise NoMatch("step reference required - refusing to guess")
        m = re.match(r"^#?(\d+)$", ref)
        if m:
            n = int(m.group(1))
            for s in g.steps:
                if s.n == n:
                    return s
            raise NoMatch("goal %r has no step %d" % (g.title, n))
        ref_n = _norm(ref)
        cands = [s for s in g.steps if ref_n in _norm(s.text)]
        if not cands:
            raise NoMatch("no step matches %r" % ref)
        if len(cands) > 1:
            raise AmbiguousMatch("step", ref, cands)
        return cands[0]

    def set_step_state(self, owner, goal_match, step_ref, state,
                       blocker=None):
        if state not in STEP_STATES:
            raise GoalError("unknown step state %r" % state)
        g = self._match_goal(owner, goal_match)
        s = self._match_step(g, step_ref)
        if state == "blocked" and not (blocker or "").strip():
            raise GoalError("blocking needs a reason")
        s.state = state
        s.blocker = (blocker or "").strip()[:MAX_BLOCKER] \
            if state == "blocked" else None
        s.updated = int(time.time())
        g.updated = s.updated
        self.store.put(g)
        return g, s

    def set_goal_status(self, owner, goal_match, status, now=None):
        if status not in GOAL_STATUSES:
            raise GoalError("unknown goal status %r" % status)
        g = self._match_goal(owner, goal_match)
        if status in ("active", "paused") and g.status not in ("active",
                                                                "paused"):
            # re-entering a live state must pass the live cap, or the
            # abandon->create->reactivate path bypasses it
            live = len(self.store.list(owner, statuses=("active",
                                                        "paused")))
            if live >= MAX_GOALS_PER_OWNER:
                raise GoalLimitReached(
                    "goal list is full (%d active/paused goals); abandon, "
                    "complete or delete some first - %r stays %s"
                    % (MAX_GOALS_PER_OWNER, g.title, g.status))
        g.status = status
        g.updated = int(time.time())
        self.store.put(g)
        return g

    def delete_goal(self, owner, goal_match):
        g = self._match_goal(owner, goal_match)
        self.store.delete(owner, g.id)
        return g

    # ---------- inspection ----------

    def next_actions(self, owner):
        """Inspectable state: for every ACTIVE goal, the next actionable
        step (lowest-numbered not-done, not-blocked step), blocked steps
        with their reasons, and progress counts. Deterministic order."""
        out = []
        for g in self.store.list(owner, statuses=("active",)):
            done = sum(1 for s in g.steps if s.state == "done")
            blocked = [s for s in g.steps if s.state == "blocked"]
            nxt = next((s for s in g.steps
                        if s.state in ("pending", "doing")), None)
            out.append({
                "goal_id": g.id, "title": g.title,
                "progress": "%d/%d" % (done, len(g.steps)),
                "next": nxt.text if nxt else None,
                "next_n": nxt.n if nxt else None,
                "blocked": [{"n": s.n, "text": s.text, "reason": s.blocker}
                            for s in blocked],
            })
        return out

    def render_goal(self, g):
        lines = ["%s [%s] - %d/%d steps done" % (
            g.title, g.status,
            sum(1 for s in g.steps if s.state == "done"), len(g.steps))]
        for s in g.steps:
            mark = {"done": "x", "doing": ">", "blocked": "!",
                    "pending": " "}[s.state]
            line = "  [%s] %d. %s" % (mark, s.n, s.text)
            if s.state == "blocked":
                line += " (blocked: %s)" % s.blocker
            lines.append(line)
        return "\n".join(lines)

    def export(self, owner):
        return {"goals": [g.to_dict() for g in self.store.list(owner)],
                "exported_at": int(time.time()),
                "format": "crayon-goals-v1"}

    # ---------- chat command surface ----------

    def handle_message(self, owner, text, source_sid=None, source_ts=None):
        """Parse a user chat message. Returns a reply string when the
        message was a goals command (caller should NOT pass it to the
        model), else None. Grammar is explicit and goal-prefixed;
        everything else falls through. Ambiguous matches are refused with
        ZERO effects."""
        t = (text or "").strip()
        low = re.sub(r"\s+", " ", t.lower())
        excerpt = t[:MAX_EXCERPT]

        m = re.match(r"^add goal:?\s+(.+)$", t, re.I | re.S)
        if m:
            try:
                g = self.add_goal(owner, m.group(1), source_sid=source_sid,
                                  source_ts=source_ts, source_excerpt=excerpt)
            except (GoalError, GoalLimitReached) as e:
                return str(e)
            return ("Goal added (id %s): %s. Add steps with 'goal %s add "
                    "step ...'." % (g.id, g.title, g.title[:30]))

        m = re.match(r"^goal\s+(.+?)\s+add step:?\s+(.+)$", t, re.I | re.S)
        if m:
            try:
                g, s = self.add_step(owner, m.group(1), m.group(2))
            except NoMatch as e:
                return str(e)
            except AmbiguousMatch as e:
                return ("Several goals match - be more specific: "
                        + "; ".join(c.title for c in e.candidates[:5]))
            except GoalError as e:
                return str(e)
            return "Step %d added to '%s': %s" % (s.n, g.title, s.text)

        m = re.match(r"^goal\s+(.+?)\s+step\s+(.+?)\s+"
                     r"(done|doing|blocked|pending)(?:\s*:\s*(.+))?$",
                     t, re.I | re.S)
        if m:
            gm, ref, state, reason = m.groups()
            try:
                g, s = self.set_step_state(owner, gm, ref, state.lower(),
                                           blocker=reason)
            except NoMatch as e:
                return str(e)
            except AmbiguousMatch as e:
                return ("Ambiguous - be more specific: "
                        + "; ".join(getattr(c, "title", getattr(c, "text", "?"))
                                    for c in e.candidates[:5]))
            except GoalError as e:
                return str(e)
            if state.lower() == "blocked":
                return "Step %d of '%s' blocked: %s" % (s.n, g.title,
                                                        s.blocker)
            return "Step %d of '%s' marked %s." % (s.n, g.title,
                                                   s.state)

        m = re.match(r"^goal\s+(.+?)\s+(pause|resume|done|complete|"
                     r"abandon|delete)$", low)
        if m:
            gm, action = m.group(1), m.group(2)
            try:
                if action == "delete":
                    g = self.delete_goal(owner, gm)
                    return "Deleted goal '%s' and its %d step(s)." % (
                        g.title, len(g.steps))
                status = {"pause": "paused", "resume": "active",
                          "done": "done", "complete": "done",
                          "abandon": "abandoned"}[action]
                g = self.set_goal_status(owner, gm, status)
            except NoMatch as e:
                return str(e)
            except AmbiguousMatch as e:
                return ("Several goals match - be more specific: "
                        + "; ".join(c.title for c in e.candidates[:5]))
            return "Goal '%s' is now %s." % (g.title, g.status)

        if re.match(r"^(my goals|list goals|goal status|goals status)$", low):
            goals = self.store.list(owner)
            if not goals:
                return "No goals yet. Add one with 'add goal ...'."
            lines = ["%d goal(s):" % len(goals)]
            for g in goals:
                done = sum(1 for s in g.steps if s.state == "done")
                lines.append("- %s [%s] %d/%d steps" % (
                    g.title, g.status, done, len(g.steps)))
            return "\n".join(lines)

        m = re.match(r"^(?:show )?goal\s+(.+?)\s+(?:detail|status|steps)$",
                     low)
        if m:
            try:
                g = self._match_goal(owner, m.group(1))
            except NoMatch as e:
                return str(e)
            except AmbiguousMatch as e:
                return ("Several goals match - be more specific: "
                        + "; ".join(c.title for c in e.candidates[:5]))
            return self.render_goal(g)

        if re.match(r"^what('s| is) next$|^next actions?$", low):
            na = self.next_actions(owner)
            if not na:
                return "No active goals with next actions."
            lines = []
            for item in na:
                if item["next"]:
                    lines.append("- %s: next is step %d - %s"
                                 % (item["title"], item["next_n"],
                                    item["next"]))
                for b in item["blocked"]:
                    lines.append("  (blocked: %d. %s - %s)"
                                 % (b["n"], b["text"], b["reason"]))
            return "\n".join(lines) if lines else \
                "Active goals, but every step is done or blocked."

        if re.match(r"^export (my )?goals$", low):
            import json
            return json.dumps(self.export(owner), indent=2)

        return None
