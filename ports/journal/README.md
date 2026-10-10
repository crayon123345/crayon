# /ports/journal - living journal (opted-in dated entries)

P1 capability #3 in the Dot/Muse trait mapping: opted-in chats and voice
notes become **editable dated summaries with original-message links**,
with JSON and Markdown portability. Layered next to /ports/memory and
/ports/reflect; no dependency on either.

## STATUS: STAGING MODULE - NOT MOUNTED, NOT LIVE

This port is complete as a tested module (22/22 tests) but is NOT wired
into the running app. Named limits, exactly:

1. NOT MOUNTED: nothing calls Journal from integrated/ yet. Integration
   surface (for the owning side): when the journal is on for this owner
   (visitor vid, same as the other ports), call
   record(owner, date, text, source_sid, source_ts) per message/voice
   note (a None return means journaling is off - store nothing), call
   handle_message(owner, text, date=...) BEFORE model dispatch (after
   continuity and reflect), and use the export endpoints for the
   download surface.
2. SUMMARIES ARE NOT MODEL-WRITTEN HERE: summarization is a pluggable
   callable; the shipped default is a deterministic EXTRACTIVE rendering
   of the day's recorded excerpts (bullets of what was actually said).
   A model-written narrative summary is integration work: pass a
   summarizer callable at construction. This README does not claim any
   model behavior.
3. RECORDING IS OPT-IN AND PAUSABLE: nothing is stored while the journal
   is off (record() returns None with zero writes - tested). `journal
   off` pauses recording and KEEPS entries; `delete my journal` wipes
   entries AND turns the journal off. There is no passive capture path.
4. EDITING NEVER LOSES PROVENANCE: edit_entry replaces only the summary
   text; source links (chat id, message timestamp, capped excerpt) are
   untouched. An owner-edited entry is never overwritten by later
   recording - links keep accruing, text stays the owner's.
5. DATES ARE EXPLICIT: the integration passes YYYY-MM-DD and owns the
   user's timezone; this module deliberately has no clock-derived dates.
6. STORAGE IS INJECTED, NOT DURABLE: MemJournalStore is in-memory; the
   SQL adapter (same encryption-at-rest and vid keying convention as
   cw_mem) is integration work, deliberately not shipped half-wired.
7. CAPS REFUSE, NEVER EVICT: MAX_ENTRIES_PER_OWNER = 400 (distinct days);
   recording past the cap raises instead of silently dropping history.
   Retention policy is a parked product decision (same convention as #1).
8. PDF EXPORT IS NOT HERE: the spec's "reuse PDF export" refers to the
   app's existing docs exporter in integrated/ - wiring the journal
   Markdown/JSON into it is mount-time work for the owning side. This
   port ships JSON + Markdown only.

## Commands (chat surface)

- `journal on` / `start my journal` - opt in and start recording
- `journal off` / `pause my journal` - pause recording (entries kept)
- `delete my journal` - wipe all entries and turn the journal off
- `journal status` / `show my journal` - state + recent entries
- `show my journal for YYYY-MM-DD` - one entry with its text
- `edit journal for YYYY-MM-DD: <text>` - replace the summary text
  (source links unchanged)
- `export my journal as json` / `export my journal as markdown`

## Acceptance criteria covered by tests (test_journal.py, 22 tests)

- Opt-in gate: record without opt-in stores ZERO rows; off pauses
  recording with zero new links; wipe deletes everything and turns off.
- Dated entries with original-message links (chat id + message ts +
  excerpt); one entry per owner per day; voice-note sources work.
- Default summary is extractive only: every line traces to a recorded
  excerpt (no invention possible).
- Editing: keeps source links, sets edited, survives later recording;
  editing a missing entry is refused with zero effects.
- Exports: JSON carries format crayon-journal-v1 + full provenance and is
  serializable; Markdown carries dates, edit marks, and source links;
  empty journal exports cleanly.
- No cross-owner leakage: a second owner's records, reads, exports,
  opt-ins and wipes have zero effect on the first owner.
- Grammar discipline: ambiguous phrasing falls through; commands create
  zero entries; invalid dates refused.
- Cap: refuses at 400 entries with ZERO eviction.

## Version

crayon-journal-v1
