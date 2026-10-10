"""Reflective conversation mode - thought-partner behavior layer.

P0 capability #2 (Dot/Muse trait mapping): a per-owner conversation mode
where Crayon acts as a thought partner - Listen / Reflect / Plan - and asks
ONE useful follow-up question per reply instead of a barrage. Layered on top
of the shipped personal-continuity store (ports/memory): open `followup`
facts are what make the single follow-up USEFUL instead of generic.

Design rules (same discipline as ports/memory):
- Every operation takes `owner` explicitly; no ambient identity, no
  cross-owner path exists in this module.
- The mode is EXPLICIT: it turns on/off only via the documented command
  grammar or a programmatic set_mode call. Ambiguous phrasing falls through
  to the model unchanged.
- This module never calls a model. It produces (a) a system-prompt behavior
  contract and (b) a deterministic post-filter. The prompt contract is a
  REQUEST to the model; the ONLY enforced guarantee is
  enforce_one_question(), which deterministically reduces a drafted reply
  to at most one question. The README names this split.
- Sensitive facts are NEVER injected into the prompt addendum, matching the
  continuity guarantee, even when the owner opted in to sensitive storage.
- Storage is injected (ModeStore for mode state; a duck-typed FactStore for
  follow-up grounding). MemModeStore is NOT durable; the SQL adapter is the
  integration point named in the README.
"""

import re

CONTRACT_VERSION = "crayon-reflect-v2"
MAX_FOLLOWUP_TEXT = 200

# Duck-typed FactStore interface (satisfied by ports/memory MemFactStore and
# its future SQL adapter): list(owner, include_sensitive=False) -> iterable
# of fact objects with .id, .category, .text, .created, .source_ts,
# .superseded_by, .sensitive. Mode grounding uses ONLY category == "followup".


class ReflectError(Exception):
    pass


class MemModeStore:
    """In-memory mode state. NOT durable - tests and staging only."""

    def __init__(self):
        self._mode = {}      # owner -> bool
        self._surfaced = {}  # owner -> list of fids already surfaced (ordered)

    def get_mode(self, owner):
        return bool(self._mode.get(owner, False))

    def set_mode(self, owner, value):
        self._mode[owner] = bool(value)

    def get_surfaced(self, owner):
        return list(self._surfaced.get(owner, ()))

    def add_surfaced(self, owner, fid):
        hist = self._surfaced.setdefault(owner, [])
        if fid not in hist:
            hist.append(fid)

    def clear_surfaced(self, owner):
        self._surfaced.pop(owner, None)


class ReflectiveMode:
    """Per-owner reflective conversation mode.

    Recency semantics (EXPLICIT): "most recent" means greatest source_ts -
    when the user actually said it - falling back to storage time (created),
    and ties beyond that break by storage-created then id for pure
    determinism. Ids never carry semantic order; they only stabilize exact
    timestamp ties. Sort key: (source_ts, created, id), highest wins.

    No-nagging is GLOBAL within a mode session: a surfaced follow-up is
    never re-surfaced while other candidates exist AND is not re-surfaced
    even when it is the only one. Surfacing history resets only when the
    mode is toggled off (or cleared programmatically); a corrected fact
    gets a new id and therefore becomes surfacable again.
    """

    def __init__(self, mode_store, fact_store=None):
        self.mode = mode_store
        self.facts = fact_store  # may be None: mode works, just ungrounded

    # ---------- mode state ----------

    def set_mode(self, owner, enabled):
        self.mode.set_mode(owner, enabled)
        if not enabled:
            self.mode.clear_surfaced(owner)  # reset surfacing history

    def is_on(self, owner):
        return self.mode.get_mode(owner)

    # ---------- follow-up grounding ----------

    def open_followups(self, owner):
        """Current, non-sensitive, open follow-up facts, most recent first
        by the documented recency semantics. Sensitive follow-ups are
        excluded ALWAYS (never enter the prompt), independent of the
        owner's sensitive-storage opt-in."""
        if self.facts is None:
            return []
        cands = [f for f in self.facts.list(owner, include_sensitive=False)
                 if getattr(f, "category", None) == "followup"
                 and getattr(f, "superseded_by", None) is None]
        cands.sort(key=lambda f: (f.source_ts, f.created, f.id))
        cands.reverse()
        return cands

    def pick_followup(self, owner):
        """Pure: choose at most ONE follow-up to surface. The most recent
        candidate (per the documented recency semantics) that has NOT been
        surfaced this mode session wins. Returns None when every candidate
        was already surfaced - a surfaced follow-up is not re-raised until
        the mode is toggled off/on or the fact is corrected (new id)."""
        surfaced = set(self.mode.get_surfaced(owner))
        for f in self.open_followups(owner):
            if f.id not in surfaced:
                return f
        return None

    # ---------- prompt surface ----------

    def prompt_addendum(self, owner):
        """System-prompt behavior contract for reflective mode. Empty when
        the mode is off. EFFECT: if a follow-up is surfaced here, its id is
        recorded in the owner's surfaced history (this is the one call the
        integration makes per model dispatch, so it is the natural effect
        point). The contract is a REQUEST to the model; the deterministic
        guarantee lives in enforce_one_question()."""
        if not self.mode.get_mode(owner):
            return ""
        lines = [
            "Reflective mode is ON for this conversation. Behavior contract:",
            "- LISTEN: read the whole message before replying; name the",
            "  specific thing the user said, not a generic acknowledgement.",
            "- REFLECT: mirror the user's point in your own words and check",
            "  you understood ('it sounds like... did I get that right?')",
            "  before advising. Do not diagnose; you are a thought partner,",
            "  not a therapist. If the user mentions harming themselves,",
            "  drop this format, respond with care, and encourage",
            "  professional help.",
            "- PLAN: when the user wants a next step, end with one concrete,",
            "  small plan item they can act on.",
            "- Ask EXACTLY ONE follow-up question per reply - the single",
            "  most useful one. Never a barrage of questions.",
        ]
        fu = self.pick_followup(owner)
        if fu is not None:
            lines.append("- There is one open follow-up worth raising if it")
            lines.append("  fits naturally: %s" % fu.text[:MAX_FOLLOWUP_TEXT])
            self.mode.add_surfaced(owner, fu.id)
        return "\n".join(lines)

    # ---------- deterministic reply guard ----------

    @staticmethod
    def question_count(text):
        """Guard metric: every '?' counts as a question. Documented limit:
        a '?' inside a URL query string or an emoticon also counts;
        over-counting only ever TRIMS, never invents content."""
        return (text or "").count("?")

    @staticmethod
    def _sentences(text):
        """Split into sentences on terminal punctuation runs, keeping the
        punctuation with its sentence, WITH or WITHOUT following
        whitespace: 'Why?How?' -> ['Why?', 'How?']; 'Really??' -> ['Really??']
        (one sentence, repeated marks)."""
        parts = re.findall(r"[^.!?]+[.!?]+|[^.!?]+$", text)
        return [p.strip() for p in parts if p.strip()]

    @staticmethod
    def enforce_one_question(text):
        """Deterministic post-filter on a drafted reply: reduce it to at
        most ONE question mark. Sentences are recognized with or without
        separating whitespace; sentences up to and including the first
        question sentence are kept in order; later question sentences are
        dropped; later non-question sentences are kept. Inside the kept
        first question sentence, a run of repeated '?' collapses to one
        ('Really??' -> 'Really?'). A reply whose only '?' sits inside the
        first question sentence with no repeats is returned
        byte-identical."""
        if ReflectiveMode.question_count(text) <= 1:
            return text
        out = []
        seen_question = False
        for s in ReflectiveMode._sentences(text):
            if "?" in s:
                if not seen_question:
                    out.append(re.sub(r"\?{2,}", "?", s))
                    seen_question = True
                # else: drop the extra question sentence
            else:
                out.append(s)
        return " ".join(out)

    # ---------- chat command surface ----------

    def handle_message(self, owner, text, source_sid=None, source_ts=None):
        """Parse a user chat message. Returns a reply string when the message
        was a reflective-mode command (caller should NOT pass it to the
        model), else None. Grammar is explicit; 'reflect on my day' is NOT a
        command and falls through to the model."""
        t = (text or "").strip()
        low = re.sub(r"\s+", " ", t.lower())

        if re.match(r"^(reflect|reflective mode|thought partner) on$", low):
            self.set_mode(owner, True)
            return ("Reflective mode is ON. I will listen, reflect back what "
                    "I hear, help you plan, and ask one useful follow-up "
                    "question at a time. Say 'reflect off' to switch back.")

        if re.match(r"^(reflect|reflective mode|thought partner) off$", low):
            self.set_mode(owner, False)
            return ("Reflective mode is OFF. Back to normal conversation; "
                    "your stored memory is unchanged.")

        if re.match(r"^(reflect|reflective mode|thought partner) status$", low):
            state = "ON" if self.mode.get_mode(owner) else "OFF"
            n = len(self.open_followups(owner))
            return ("Reflective mode is %s. Open follow-ups I could raise: "
                    "%d." % (state, n))

        return None
