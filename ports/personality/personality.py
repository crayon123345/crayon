"""ports/personality - user-selected speaking style, no capability claims.

Companion-trait build (Muse "user-selected speaking style" + Dots
"personalized appearance WITHOUT confusing personality with
capability"): the owner picks a tone preset or writes a custom style
card; the module stores it and renders a style profile the integration
passes to the model layer. Stands alone.

Design invariants:

1. STYLE IS NOT CAPABILITY: style text that claims abilities
   ("you can make calls", "I can book flights", "always-on",
   "I have access to...") is REJECTED - a persona must never promise
   what the system does not do. The claim list is explicit and tested.
2. OWNER-EXPLICIT ONLY: style changes only on explicit owner commands.
   Nothing infers or adapts style from behavior.
3. RENDERING IS PURE: profile() returns the same dict for the same
   stored state - no clocks, no randomness at render time.
4. OWNER-STRICT: profiles are keyed to the owning account only; one
   profile per owner; setting replaces (the old one is exported first
   on request - no silent history claims).
5. BOUNDS: preset list is closed; custom names/traits are length-capped
   and normalized; trait count is capped. Overflow refuses with zero
   effects.
6. NOT MOUNTED: the module never touches the model; the integration
   applies the profile. Default (no profile) means the app's existing
   voice, unchanged.
"""

import json
import re

MAX_STYLE_NAME = 40
MAX_TRAITS = 12
MAX_TRAIT_LEN = 60
MAX_TRAIT_CHARS = 400

PRESETS = {
    "concise": ["brief", "direct", "no filler"],
    "warm": ["friendly", "encouraging", "plain words"],
    "playful": ["light humor", "casual", "energetic"],
    "formal": ["polished", "precise", "structured"],
}

# CLOSED tone vocabulary: custom style traits must come from this list.
# Tone dimensions only - there is no free-text trait channel, so no
# lexical filter has to catch capability claims in traits at all.
TONE_VOCABULARY = (
    "brief", "direct", "warm", "friendly", "encouraging", "casual",
    "playful", "polished", "precise", "structured", "patient", "calm",
    "upbeat", "gentle", "energetic", "plain words", "short sentences",
    "no filler", "light humor",
)

# phrases that turn a style NAME into a capability claim - rejected.
# NOTE: lexical, a known limit (see README); traits need no filter
# because they are closed-vocabulary.
CLAIM_MARKERS = (
    "you can ", "i can ", "i will ", "i'll ", "able to",
    "always-on", "always on", "24/7", "access to your",
    "i have access", "book ", "buy ", "purchase", "call ",
    "text your", "message your", "send ", "pay ",
    "ignore ", "approval",
)


class PersonalityError(Exception):
    pass


def _norm(text):
    return re.sub(r"\s+", " ", (text or "").strip())


class Profile:
    __slots__ = ("owner", "kind", "name", "traits", "created")

    def __init__(self, owner, kind, name, traits, created):
        self.owner = owner
        self.kind = kind            # "preset" or "custom"
        self.name = name
        self.traits = traits
        self.created = created

    def to_dict(self):
        return {"kind": self.kind, "name": self.name,
                "traits": list(self.traits), "created": self.created}


class MemPersonalityStore:
    """In-memory reference store. SQL adapter is mount work."""

    def __init__(self):
        self._profiles = {}

    def get(self, owner):
        return self._profiles.get(owner)

    def put(self, p):
        self._profiles[p.owner] = p

    def delete(self, owner):
        return self._profiles.pop(owner, None) is not None


def _reject_claims(text):
    low = " " + (text or "").lower() + " "
    for marker in CLAIM_MARKERS:
        if marker in low:
            raise PersonalityError(
                "style text cannot claim capabilities (%r) - "
                "personality is tone, not ability" % marker.strip())


class Personality:
    def __init__(self, store, id_factory=None):
        self.store = store

    # ---------- core API ----------

    def set_preset(self, owner, preset, now=None):
        preset = (preset or "").strip().lower()
        if preset not in PRESETS:
            raise PersonalityError(
                "unknown style %r (presets: %s)"
                % (preset, ", ".join(sorted(PRESETS))))
        p = Profile(owner, "preset", preset, list(PRESETS[preset]),
                    int(now or 0))
        self.store.put(p)
        return p

    def set_custom(self, owner, name, traits, now=None):
        """traits: iterable of tone words from TONE_VOCABULARY (closed)."""
        name = _norm(name)
        if not name:
            raise PersonalityError("style name required")
        # REJECT overflow - truncating would silently rename the style
        if len(name) > MAX_STYLE_NAME:
            raise PersonalityError("style name too long (%d chars max)"
                                   % MAX_STYLE_NAME)
        _reject_claims(name)
        clean = []
        for t in (traits or []):
            t = _norm(t).lower()
            if not t:
                continue
            if len(t) > MAX_TRAIT_LEN:
                raise PersonalityError("trait too long (%d chars max)"
                                       % MAX_TRAIT_LEN)
            if t not in TONE_VOCABULARY:
                raise PersonalityError(
                    "unknown tone %r - pick from: %s"
                    % (t, ", ".join(TONE_VOCABULARY)))
            if t not in clean:
                clean.append(t)
        if not clean:
            raise PersonalityError("at least one trait required (from: "
                                   "%s)" % ", ".join(TONE_VOCABULARY))
        if len(clean) > MAX_TRAITS:
            raise PersonalityError("too many traits (%d max)"
                                   % MAX_TRAITS)
        p = Profile(owner, "custom", name, clean, int(now or 0))
        self.store.put(p)
        return p

    def clear(self, owner):
        return self.store.delete(owner)

    def profile(self, owner):
        """Pure render: the style profile for the model layer, or None.

        None means: use the app's existing default voice. The profile
        NEVER contains capability language - presets are fixed tone
        words and custom text passed the claim filter at write time.
        """
        p = self.store.get(owner)
        if p is None:
            return None
        return {"version": "crayon-personality-v1", "kind": p.kind,
                "name": p.name, "traits": list(p.traits),
                "note": "tone only - describes HOW to speak, never "
                        "WHAT the assistant can do"}

    # ---------- chat surface ----------

    def handle_message(self, owner, text, now=None):
        """Explicit grammar; anything else returns None (falls through).

        my style | set style <preset> |
        set custom style <name>: <trait, trait, ...> |
        clear my style | export my style
        """
        low = re.sub(r"\s+", " ", (text or "").strip()).lower()
        if low == "my style":
            p = self.store.get(owner)
            if p is None:
                return "No style set - I'm using my default voice. " \
                    "Presets: %s." % ", ".join(sorted(PRESETS))
            return "Style: %s (%s) - %s" % (
                p.name, p.kind, ", ".join(p.traits))
        m = re.match(r"^set style\s+(\S+)$", low)
        if m:
            try:
                p = self.set_preset(owner, m.group(1), now=now)
            except PersonalityError as e:
                return str(e)
            return "Style set to %s (%s)." % (p.name,
                                              ", ".join(p.traits))
        m = re.match(r"^set custom style\s+([^:]+):\s*(.+)$", text or "",
                     re.IGNORECASE | re.DOTALL)
        if m:
            traits = [t for t in re.split(r"[,;]", m.group(2))]
            try:
                p = self.set_custom(owner, m.group(1), traits, now=now)
            except PersonalityError as e:
                return str(e)
            return "Custom style %r saved (%d traits)." % (p.name,
                                                           len(p.traits))
        if low == "clear my style":
            if self.clear(owner):
                return "Style cleared - back to my default voice."
            return "No style was set."
        if low == "export my style":
            p = self.store.get(owner)
            return json.dumps({"version": "crayon-personality-v1",
                               "profile": p.to_dict() if p else None},
                              indent=2, sort_keys=True)
        return None


VERSION = "crayon-personality-v1"
