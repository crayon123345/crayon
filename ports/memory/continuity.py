"""Personal continuity memory - structured per-user facts with provenance.

A durable fact store layered NEXT TO the existing rolling transcript memory
(integrated/public/memory.py, which forgets context at every "new chat"
boundary). Facts here survive fresh chats by construction.

Design rules:
- Every operation takes `owner` explicitly; no ambient identity, no
  cross-owner path exists in this module.
- Every fact carries provenance: source chat id, source message timestamp,
  and a capped excerpt of the source message.
- Sensitive facts are stored ONLY when the owner has opted in, are never
  injected into model context, and are hard-deleted when the owner opts out.
- Storage is injected (FactStore). MemFactStore is the default and is NOT
  durable; the SQL adapter for the production database is the integration
  point named in the README.
"""

import re
import time
import secrets

CATEGORIES = ("preference", "person", "goal", "experience", "followup", "other")
MAX_FACTS_PER_OWNER = 500
MAX_FACT_TEXT = 500
MAX_EXCERPT = 200


class FactError(Exception):
    pass


class SensitiveNotAllowed(FactError):
    pass


class FactLimitReached(FactError):
    pass


def _norm(text):
    return re.sub(r"\s+", " ", text.strip()).lower()


class Fact:
    __slots__ = ("id", "owner", "category", "text", "sensitive", "source_sid",
                 "source_ts", "source_excerpt", "created", "updated", "superseded_by")

    def __init__(self, owner, category, text, sensitive, source_sid, source_ts,
                 source_excerpt, fid=None, created=None, updated=None, superseded_by=None):
        self.id = fid or "f_" + secrets.token_hex(8)
        self.owner = owner
        self.category = category if category in CATEGORIES else "other"
        self.text = text
        self.sensitive = bool(sensitive)
        self.source_sid = source_sid
        self.source_ts = int(source_ts)
        self.source_excerpt = (source_excerpt or "")[:MAX_EXCERPT]
        self.created = int(created if created is not None else time.time())
        self.updated = int(updated if updated is not None else self.created)
        self.superseded_by = superseded_by  # fid of the correcting fact, if any

    def to_dict(self):
        return {"id": self.id, "category": self.category, "text": self.text,
                "sensitive": self.sensitive,
                "source": {"chat": self.source_sid, "message_ts": self.source_ts,
                           "excerpt": self.source_excerpt},
                "created": self.created, "updated": self.updated,
                "superseded_by": self.superseded_by}


class MemFactStore:
    """In-memory store. NOT durable - tests and staging only."""

    def __init__(self):
        self._facts = {}      # fid -> Fact
        self._optin = {}      # owner -> bool

    # facts
    def put(self, fact):
        self._facts[fact.id] = fact

    def get(self, owner, fid):
        f = self._facts.get(fid)
        return f if f and f.owner == owner else None

    def list(self, owner, include_superseded=False, include_sensitive=True):
        out = [f for f in self._facts.values() if f.owner == owner]
        if not include_superseded:
            out = [f for f in out if f.superseded_by is None]
        if not include_sensitive:
            out = [f for f in out if not f.sensitive]
        return sorted(out, key=lambda f: f.created)

    def count(self, owner):
        return sum(1 for f in self._facts.values()
                   if f.owner == owner and f.superseded_by is None)

    def delete(self, owner, fid):
        f = self.get(owner, fid)
        if f is None:
            return False
        del self._facts[fid]
        return True

    def wipe(self, owner):
        n = sum(1 for f in self._facts.values() if f.owner == owner)
        self._facts = {k: v for k, v in self._facts.items() if v.owner != owner}
        return n

    # sensitive opt-in flag
    def get_optin(self, owner):
        return bool(self._optin.get(owner, False))

    def set_optin(self, owner, value):
        self._optin[owner] = bool(value)


class Continuity:
    """Structured per-user memory with Remember / Correct / Forget commands."""

    def __init__(self, store):
        self.store = store

    # ---------- core API (also usable programmatically by the app) ----------

    def remember(self, owner, text, category="other", sensitive=False,
                 source_sid=None, source_ts=None, source_excerpt=""):
        text = (text or "").strip()
        if not text:
            raise FactError("empty fact")
        text = text[:MAX_FACT_TEXT]
        if sensitive and not self.store.get_optin(owner):
            raise SensitiveNotAllowed(
                "sensitive memory is off for this user; opt in first")
        # idempotent on normalized text among current facts
        for f in self.store.list(owner):
            if _norm(f.text) == _norm(text) and f.sensitive == sensitive:
                return f, False
        if self.store.count(owner) >= MAX_FACTS_PER_OWNER:
            raise FactLimitReached(
                "memory is full (%d facts); forget something first"
                % MAX_FACTS_PER_OWNER)
        f = Fact(owner, category, text, sensitive, source_sid,
                 source_ts if source_ts is not None else time.time(),
                 source_excerpt)
        self.store.put(f)
        return f, True

    def correct(self, owner, match, new_text, source_sid=None, source_ts=None,
                source_excerpt=""):
        """Supersede current facts containing `match` (case-insensitive) with a
        corrected fact. Returns (new_fact, superseded_count). Zero matches
        stores the correction as a fresh fact and reports 0."""
        match_n = _norm(match or "")
        if not match_n:
            raise FactError("empty match")
        new_text = (new_text or "").strip()[:MAX_FACT_TEXT]
        if not new_text:
            raise FactError("empty correction")
        targets = [f for f in self.store.list(owner)
                   if match_n in _norm(f.text)]
        category = targets[0].category if targets else "other"
        sensitive = any(f.sensitive for f in targets)
        if sensitive and not self.store.get_optin(owner):
            raise SensitiveNotAllowed(
                "correction touches a sensitive fact but sensitive memory is off")
        f, _ = self.remember(owner, new_text, category=category,
                             sensitive=sensitive, source_sid=source_sid,
                             source_ts=source_ts, source_excerpt=source_excerpt)
        for t in targets:
            if t.id != f.id:
                t.superseded_by = f.id
                t.updated = int(time.time())
                self.store.put(t)
        return f, len(targets)

    def forget(self, owner, match):
        """Hard-delete current facts containing `match`. Returns count."""
        match_n = _norm(match or "")
        if not match_n:
            raise FactError("empty match")
        targets = [f for f in self.store.list(owner, include_superseded=True)
                   if match_n in _norm(f.text)]
        n = 0
        for t in targets:
            if self.store.delete(owner, t.id):
                n += 1
        return n

    def forget_all(self, owner):
        return self.store.wipe(owner)

    def set_sensitive_optin(self, owner, enabled):
        """Opting out hard-deletes every sensitive fact first."""
        if not enabled:
            purged = 0
            for f in list(self.store.list(owner, include_superseded=True)):
                if f.sensitive:
                    self.store.delete(owner, f.id)
                    purged += 1
            self.store.set_optin(owner, False)
            return purged
        self.store.set_optin(owner, True)
        return 0

    def export(self, owner):
        """Full portable export, including superseded history."""
        return {"owner_facts": [f.to_dict() for f in
                                self.store.list(owner, include_superseded=True)],
                "sensitive_opt_in": self.store.get_optin(owner),
                "exported_at": int(time.time()),
                "format": "crayon-memory-v1"}

    def context_block(self, owner):
        """Durable facts formatted for model context. Sensitive facts are
        NEVER included. Superseded facts are excluded; corrected text is what
        survives. This is what makes corrections persist across fresh chats:
        it is called independently of any rolling-memory boundary."""
        facts = self.store.list(owner, include_sensitive=False)
        if not facts:
            return ""
        lines = ["Known durable facts about this user (each with source):"]
        for f in facts:
            lines.append("- [%s] %s (from chat %s, %s)"
                         % (f.category, f.text, f.source_sid or "?",
                            time.strftime("%Y-%m-%d", time.gmtime(f.source_ts))))
        return "\n".join(lines)

    # ---------- chat command surface ----------

    def handle_message(self, owner, text, source_sid=None, source_ts=None):
        """Parse a user chat message. Returns a reply string when the message
        was a memory command (caller should NOT pass it to the model), else
        None. Every reply states what actually happened (counts, ids)."""
        t = (text or "").strip()
        low = t.lower()
        excerpt = t[:MAX_EXCERPT]
        ts = source_ts if source_ts is not None else time.time()

        m = re.match(r"^remember(?:\s+that)?\s+(.+)$", t, re.I | re.S)
        if m:
            body = m.group(1).strip()
            sensitive = False
            sm = re.match(r"^sensitive\s*:\s*(.+)$", body, re.I | re.S)
            if sm:
                sensitive = True
                body = sm.group(1).strip()
            category = "other"
            cm = re.match(r"^#(preference|person|goal|experience|followup)\s+(.+)$",
                          body, re.I | re.S)
            if cm:
                category = cm.group(1).lower()
                body = cm.group(2).strip()
            try:
                f, created = self.remember(owner, body, category=category,
                                           sensitive=sensitive,
                                           source_sid=source_sid, source_ts=ts,
                                           source_excerpt=excerpt)
            except SensitiveNotAllowed:
                return ("Sensitive memory is off for you. Say 'allow sensitive "
                        "memory' to opt in first; nothing was stored.")
            except FactLimitReached as e:
                return str(e)
            if not created:
                return "I already have that, unchanged: %s" % f.id
            return "Remembered (%s%s, id %s, saved from this chat)." % (
                f.category, ", sensitive" if f.sensitive else "", f.id)

        m = re.match(r"^correct\s*:?\s+(.+?)\s+to\s+(.+)$", t, re.I | re.S)
        if m:
            try:
                f, n = self.correct(owner, m.group(1), m.group(2),
                                    source_sid=source_sid, source_ts=ts,
                                    source_excerpt=excerpt)
            except SensitiveNotAllowed:
                return ("That correction touches a sensitive fact while "
                        "sensitive memory is off; nothing was changed.")
            if n:
                return ("Corrected: %d previous fact(s) superseded by %s. "
                        "The corrected version is what I will use from now on, "
                        "including in new chats." % (n, f.id))
            return ("No existing fact matched; stored as a new fact (id %s)."
                    % f.id)

        if re.match(r"^forget\s+everything$|^forget\s+all$|^delete\s+my\s+memory$",
                    low):
            n = self.forget_all(owner)
            return "Forgot everything: %d fact(s) deleted." % n

        m = re.match(r"^forget\s+(?:that\s+)?(.+)$", t, re.I | re.S)
        if m:
            n = self.forget(owner, m.group(1))
            return ("Forgot %d fact(s) matching that." % n) if n else \
                   "No stored fact matched that."

        if re.match(r"^what do you remember( about me)?\??$|^list (my )?memory$",
                    low):
            facts = self.store.list(owner)
            if not facts:
                return "I have no durable facts stored for you."
            lines = ["I remember %d thing(s):" % len(facts)]
            for f in facts:
                lines.append("- [%s] %s (id %s, from %s)" % (
                    f.category, f.text, f.id,
                    time.strftime("%Y-%m-%d", time.gmtime(f.source_ts))))
            return "\n".join(lines)

        if re.match(r"^export (my )?memory$", low):
            import json
            return json.dumps(self.export(owner), indent=2)

        if re.match(r"^allow sensitive memory$|^sensitive memory on$", low):
            self.store.set_optin(owner, True)
            return ("Sensitive memory is now ON. Sensitive facts are stored "
                    "only because you opted in; they never enter chat context. "
                    "Say 'sensitive memory off' to delete them all and opt out.")

        if re.match(r"^(sensitive memory off|disallow sensitive memory)$", low):
            purged = self.set_sensitive_optin(owner, False)
            return ("Sensitive memory is OFF. Deleted %d sensitive fact(s)."
                    % purged)

        return None
