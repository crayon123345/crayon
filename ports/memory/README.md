# /ports/memory - personal continuity (structured durable memory)

Per-user structured memory for Crayon: preferences, people, goals,
experiences, and unfinished follow-ups, each stored WITH provenance (source
chat id, source message timestamp, capped source excerpt) and retrievable
across sessions. Layered NEXT TO the existing rolling transcript memory
(integrated/public/memory.py), which forgets model context at every "new
chat" boundary - facts here survive fresh chats by construction.

## STATUS: STAGING MODULE - NOT MOUNTED, NOT LIVE

This port is complete as a tested module (18/18 tests) but is NOT wired
into the running app. No route, no UI, no production storage, no automatic
extraction. Named limits, exactly:

1. NOT MOUNTED: nothing calls Continuity from integrated/ yet. Integration
   surface (for the owning side): call handle_message(owner, text, sid, ts)
   before model dispatch - a non-None reply short-circuits the model - and
   prepend context_block(owner) to the system prompt at chat start. The
   existing `owner` is the visitor id (vid) from memory.identity().
2. STORAGE IS INJECTED: MemFactStore is in-memory only and NOT durable.
   The production adapter must implement the FactStore interface over the
   app database with the same encryption-at-rest convention as cw_mem
   (Fernet from SECRET_KEY) and the same vid keying. DDL + adapter are the
   integration work, deliberately not shipped half-wired here.
3. NO AUTOMATIC EXTRACTION: facts are created ONLY by explicit user
   command (remember/correct) or explicit programmatic call. Model-suggested
   extraction (Dot/Muse-style passive memory) is a deliberate parked
   decision: it needs owner-visible review before anything is stored.
4. SENSITIVE MEMORY: stored only after explicit opt-in, NEVER injected
   into model context, hard-deleted on opt-out. There is no per-fact
   unlock flow; that is parked as a UX decision.
5. COMMANDS ARE EXPLICIT GRAMMAR (documented below), not free-form NLU.
   Ambiguous phrasing falls through to the model unchanged.
6. NO RETENTION SWEEP YET: facts have no expiry; the cap
   (MAX_FACTS_PER_OWNER = 500) REFUSES new facts rather than silently
   evicting memories. Retention policy is a parked product decision.
7. EXPORT FORMAT is crayon-memory-v1 JSON (includes superseded history).
   Markdown/PDF export reuses the docs exporter later (P1 journal work).

## Commands (chat surface)

- `remember [that] [#category] [sensitive: ] <text>` - store a fact
  (categories: preference, person, goal, experience, followup; default other)
- `correct <old text match> to <new text>` - supersede matching facts; the
  corrected version is what survives into fresh chats
- `forget <text match>` - hard-delete matching facts (reply states the count)
- `forget everything` / `delete my memory` - wipe all facts for this owner
- `what do you remember about me?` - list current facts with ids + dates
- `export my memory` - full JSON export including provenance + history
- `allow sensitive memory` / `sensitive memory off` - opt in / purge+opt out

## Acceptance criteria covered by tests (test_continuity.py, 18 tests)

- Corrected facts survive fresh chats (context_block after supersede shows
  only the correction; old text is gone from context).
- No cross-user leakage: reads, lists, exports, corrects, forgets, deletes,
  and wipes are owner-scoped by construction; tests attack every path from
  a second owner and count store rows (zero effects).
- Sensitive: refused without opt-in with ZERO writes; stored after opt-in;
  never in context; purged on opt-out; corrections preserve sensitivity.
- Forget reports exact counts; forget-everything wipes only that owner.
- Provenance: source chat + message timestamp + excerpt on every fact and
  in every export.
- Idempotent remember (normalized-text dedupe, no duplicate rows).
- Cap refuses without silent eviction.

## Generalization note (owner direction: more companion traits may follow)

Categories are open-ended (the #category grammar accepts the five named
ones now; the Fact model stores any string under `other` fallback), the
store interface is storage-agnostic, and export format is versioned - the
same store carries P1 journal entries and P2 pattern-card links later
without schema upheaval.
