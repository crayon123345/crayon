"""Living journal - opted-in dated entries with original-message links.

P1 capability #3 (Dot/Muse trait mapping): opted-in chats/voice notes
become editable dated summaries with original-message links, with JSON and
Markdown portability. Layered NEXT TO ports/memory and ports/reflect; no
dependency on either.

Design rules (same discipline as the other ports):
- Every operation takes `owner` explicitly; no ambient identity, no
  cross-owner path exists in this module.
- RECORDING IS OPT-IN: nothing is stored unless the owner turned the
  journal on. Turning it off pauses recording; entries are kept until an
  explicit wipe. There is no passive capture path.
- This module never calls a model. Summarization is a pluggable callable;
  the default is a deterministic extractive rendering of the day's
  recorded excerpts. A model-written summary is integration work, named
  in the README, never claimed here.
- Every entry keeps its source links: (source chat id, source message
  timestamp, capped excerpt). Editing the summary text never touches the
  source links.
- Dates are EXPLICIT: the caller/integration passes the entry date
  (YYYY-MM-DD); the module owns no timezone. The integration (which knows
  the user's tz) supplies the date.
- Storage is injected (JournalStore). MemJournalStore is NOT durable; the
  SQL adapter is the integration point named in the README.
"""

import re
import time
import secrets

MAX_ENTRIES_PER_OWNER = 400   # parked retention: refuse, never evict (same as #1)
MAX_ENTRY_TEXT = 4000
MAX_EXCERPT = 200
MAX_SOURCES_PER_ENTRY = 50
DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")

EXPORT_FORMAT = "crayon-journal-v1"


class JournalError(Exception):
    pass


class JournalNotOptedIn(JournalError):
    pass


class JournalLimitReached(JournalError):
    pass


class JournalEntry:
    __slots__ = ("id", "owner", "date", "text", "sources", "edited",
                 "created", "updated")

    def __init__(self, owner, date, text="", sources=None, eid=None,
                 edited=False, created=None, updated=None):
        self.id = eid or "j_" + secrets.token_hex(8)
        self.owner = owner
        if not DATE_RE.match(date or ""):
            raise JournalError("date must be YYYY-MM-DD")
        self.date = date
        self.text = (text or "")[:MAX_ENTRY_TEXT]
        self.sources = list(sources or [])[:MAX_SOURCES_PER_ENTRY]
        self.edited = bool(edited)
        self.created = int(created if created is not None else time.time())
        self.updated = int(updated if updated is not None else self.created)

    def to_dict(self):
        return {"id": self.id, "date": self.date, "text": self.text,
                "edited": self.edited,
                "sources": [{"chat": s.get("chat"), "message_ts": s.get("ts"),
                             "excerpt": s.get("excerpt", "")}
                            for s in self.sources],
                "created": self.created, "updated": self.updated}


class MemJournalStore:
    """In-memory store. NOT durable - tests and staging only."""

    def __init__(self):
        self._entries = {}   # eid -> JournalEntry
        self._by_day = {}    # (owner, date) -> eid
        self._optin = {}     # owner -> bool

    def put(self, entry):
        self._entries[entry.id] = entry
        self._by_day[(entry.owner, entry.date)] = entry.id

    def get(self, owner, eid):
        e = self._entries.get(eid)
        return e if e and e.owner == owner else None

    def get_by_day(self, owner, date):
        eid = self._by_day.get((owner, date))
        return self.get(owner, eid) if eid else None

    def list(self, owner):
        return sorted((e for e in self._entries.values() if e.owner == owner),
                      key=lambda e: (e.date, e.id))

    def count(self, owner):
        return sum(1 for e in self._entries.values() if e.owner == owner)

    def delete(self, owner, eid):
        e = self.get(owner, eid)
        if e is None:
            return False
        del self._entries[eid]
        self._by_day.pop((e.owner, e.date), None)
        return True

    def wipe(self, owner):
        n = self.count(owner)
        self._entries = {k: v for k, v in self._entries.items()
                         if v.owner != owner}
        self._by_day = {k: v for k, v in self._by_day.items()
                        if k[0] != owner}
        return n

    def get_optin(self, owner):
        return bool(self._optin.get(owner, False))

    def set_optin(self, owner, value):
        self._optin[owner] = bool(value)


def default_summarizer(day_items):
    """Deterministic extractive summary: the day's recorded excerpts as
    bullet lines, in order. NO model, NO invention - this is a rendering of
    what was recorded, and the README says so. `day_items` is a list of
    (ts, excerpt) tuples."""
    return "\n".join("- %s" % ex for _, ex in day_items if ex)


class Journal:
    """Opted-in dated journal entries with source links."""

    def __init__(self, store, summarizer=None):
        self.store = store
        self.summarizer = summarizer or default_summarizer

    # ---------- recording (opt-in gated) ----------

    def record(self, owner, date, text, source_sid=None, source_ts=None):
        """Record one message/voice-note into the owner's entry for `date`.
        No-op returning None when the journal is off (nothing is stored -
        the caller treats None as 'not journaling'). Creates the day's
        entry on first record; appends a source link otherwise. The entry
        text is (re)rendered by the summarizer from the day's excerpts."""
        if not self.store.get_optin(owner):
            return None
        ts = int(source_ts if source_ts is not None else time.time())
        excerpt = (text or "")[:MAX_EXCERPT]
        entry = self.store.get_by_day(owner, date)
        if entry is None:
            if self.store.count(owner) >= MAX_ENTRIES_PER_OWNER:
                raise JournalLimitReached(
                    "journal is full (%d entries); delete some first"
                    % MAX_ENTRIES_PER_OWNER)
            entry = JournalEntry(owner, date)
        if len(entry.sources) < MAX_SOURCES_PER_ENTRY:
            entry.sources.append({"chat": source_sid, "ts": ts,
                                  "excerpt": excerpt})
        if not entry.edited:
            # un-edited entries re-render from sources; an owner-edited
            # entry is NEVER overwritten by later recording (links still
            # accrue, so provenance stays complete)
            entry.text = self.summarizer(
                [(s["ts"], s["excerpt"]) for s in entry.sources])[
                :MAX_ENTRY_TEXT]
        entry.updated = int(time.time())
        self.store.put(entry)
        return entry

    # ---------- editing ----------

    def edit_entry(self, owner, date, new_text):
        """Replace the summary text of the owner's entry for `date`.
        Source links are untouched; `edited` is set so later recording
        never overwrites the owner's words (links keep accruing)."""
        entry = self.store.get_by_day(owner, date)
        if entry is None:
            raise JournalError("no journal entry for %s" % date)
        new_text = (new_text or "").strip()[:MAX_ENTRY_TEXT]
        if not new_text:
            raise JournalError("empty edit")
        entry.text = new_text
        entry.edited = True
        entry.updated = int(time.time())
        self.store.put(entry)
        return entry

    # ---------- reading / export ----------

    def get_entry(self, owner, date):
        return self.store.get_by_day(owner, date)

    def list_entries(self, owner):
        return self.store.list(owner)

    def delete_entry(self, owner, date):
        entry = self.store.get_by_day(owner, date)
        return self.store.delete(owner, entry.id) if entry else False

    def wipe(self, owner):
        return self.store.wipe(owner)

    def export_json(self, owner):
        return {"entries": [e.to_dict() for e in self.store.list(owner)],
                "journal_on": self.store.get_optin(owner),
                "exported_at": int(time.time()),
                "format": EXPORT_FORMAT}

    def export_markdown(self, owner):
        lines = ["# Crayon journal export", ""]
        for e in self.store.list(owner):
            lines.append("## %s%s" % (e.date,
                                      " (edited)" if e.edited else ""))
            lines.append("")
            lines.append(e.text or "(empty)")
            if e.sources:
                lines.append("")
                lines.append("Sources:")
                for s in e.sources:
                    lines.append("- chat %s, message %s"
                                 % (s.get("chat") or "?",
                                    s.get("ts") or "?"))
            lines.append("")
        return "\n".join(lines)

    # ---------- chat command surface ----------

    def handle_message(self, owner, text, source_sid=None, source_ts=None,
                       date=None):
        """Parse a user chat message. Returns a reply string when the
        message was a journal command (caller should NOT pass it to the
        model), else None. Grammar is explicit; ambiguous phrasing falls
        through. `date` (YYYY-MM-DD) is supplied by the integration, which
        owns the user's timezone."""
        t = (text or "").strip()
        low = re.sub(r"\s+", " ", t.lower())

        if re.match(r"^(journal|journaling) on$|^start (my )?journal$", low):
            self.store.set_optin(owner, True)
            return ("Journal is ON. While it is on, our chats are kept as "
                    "dated entries with links back to the original "
                    "messages. Say 'journal off' to pause, 'delete my "
                    "journal' to wipe.")

        if re.match(r"^(journal|journaling) off$|^pause (my )?journal$", low):
            self.store.set_optin(owner, False)
            return ("Journal is OFF (paused). Existing entries are kept; "
                    "say 'journal on' to resume or 'delete my journal' to "
                    "wipe them.")

        if re.match(r"^delete my journal$|^wipe (my )?journal$", low):
            n = self.wipe(owner)
            self.store.set_optin(owner, False)
            return "Journal deleted: %d entry(ies) wiped, journal off." % n

        m = re.match(r"^(?:show|read) (?:my )?journal(?: entry)?(?: for)? "
                     r"(\d{4}-\d{2}-\d{2})$", low)
        if m:
            entry = self.store.get_by_day(owner, m.group(1))
            if entry is None:
                return "No journal entry for %s." % m.group(1)
            head = "Journal %s%s:" % (entry.date,
                                      " (edited)" if entry.edited else "")
            return "%s\n%s" % (head, entry.text or "(empty)")

        if re.match(r"^(show|read|list) (my )?journal$|^journal status$", low):
            entries = self.store.list(owner)
            state = "ON" if self.store.get_optin(owner) else "OFF"
            if not entries:
                return "Journal is %s. No entries yet." % state
            lines = ["Journal is %s, %d entry(ies):"
                     % (state, len(entries))]
            for e in entries[-7:]:
                lines.append("- %s%s (%d source(s))"
                             % (e.date, " (edited)" if e.edited else "",
                                len(e.sources)))
            return "\n".join(lines)

        m = re.match(r"^edit journal (?:entry )?(?:for )?"
                     r"(\d{4}-\d{2}-\d{2})\s*(?:to|:)\s*(.+)$", t, re.I | re.S)
        if m:
            try:
                e = self.edit_entry(owner, m.group(1), m.group(2))
            except JournalError as ex:
                return str(ex)
            return ("Updated the %s entry (your edit is kept; original "
                    "message links are unchanged)." % e.date)

        if re.match(r"^export (my )?journal( as)? json$", low):
            import json
            return json.dumps(self.export_json(owner), indent=2)

        if re.match(r"^export (my )?journal( as)? (markdown|md)$", low):
            return self.export_markdown(owner)

        return None
