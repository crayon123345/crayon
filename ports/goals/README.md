# /ports/goals - durable goal/task state with explicit checkpoints

Companion-trait build (Meta Muse "goal-driven personal agent" + OpenAI
Dots "always-on responsibility"): durable goal pages with steps,
blockers, next actions, and EXPLICIT progress/blocker/completion states -
not one-shot chat replies. Stands alone; complements /ports/memory,
/ports/reflect, /ports/journal.

## STATUS: STAGING MODULE - NOT MOUNTED, NOT LIVE

This port is complete as a tested module (22/22 tests) but is NOT wired
into the running app. Named limits, exactly:

1. NOT MOUNTED: nothing calls Goals from integrated/ yet. Integration
   surface (for the owning side): call handle_message(owner, text,
   source_sid, source_ts) BEFORE model dispatch (after continuity,
   reflect, journal - a non-None reply short-circuits the model);
   next_actions(owner) feeds any "what's next" surface; the owner is the
   visitor vid, same as the other ports.
2. STATE ONLY - NO EXECUTION, NO SCHEDULING: this module owns goal and
   step STATE. It does not run jobs, send reminders, or call models.
   Background execution (Muse) and scheduled reminders (Dots) are the
   scheduler port's job; "background execution" is NOT claimed here.
3. STATE IS EXPLICIT, NEVER INFERRED: goal status in
   (active, paused, done, abandoned); step state in
   (pending, doing, blocked, done); blocking REQUIRES a reason. There is
   no automatic progress inference from chat content - steps change state
   only via command or programmatic call.
4. MATCHING IS DETERMINISTIC, AMBIGUITY REFUSED: goal and step
   references are step numbers or case-insensitive substrings; a match
   with multiple candidates is refused with ZERO effects and a reply
   asking for a more specific reference.
5. DELETE vs ABANDON: abandon keeps the record with a terminal state;
   delete removes goal + steps. Caps refuse, never evict
   (MAX_GOALS_PER_OWNER = 100, MAX_STEPS_PER_GOAL = 50).
6. ORDERING: store.list sorts by (created, id); created via the command
   surface has 1s resolution, so same-second goals order by id (stable,
   not insertion order). Tests control created explicitly. The SQL
   adapter should use a serial/created tie-break with real ordering.
7. STORAGE IS INJECTED, NOT DURABLE: MemGoalStore is in-memory; the SQL
   adapter (same encryption-at-rest and vid keying convention as cw_mem)
   is integration work, deliberately not shipped half-wired.

## Commands (chat surface)

- `add goal <title>` - create (duplicates of an open title refused)
- `my goals` / `goal status` - list with progress counts
- `goal <match> add step <text>` - append a checkpoint
- `goal <match> step <n|text> done|doing|pending|blocked[: reason]` -
  set step state (blocked requires a reason)
- `goal <match> detail` - full page with every step state
- `goal <match> pause|resume|done|abandon|delete`
- `what's next` / `next actions` - next actionable step per active goal
  plus every blocker with its reason
- `export my goals` - JSON (crayon-goals-v1) with provenance

## Acceptance criteria covered by tests (test_goals.py, 22 tests)

- Durable goals with provenance (source chat/message/excerpt);
  duplicate open titles refused with zero effects.
- Explicit states: full status transitions; blocker requires a reason;
  unblocking clears the reason; progress counts are exact; closed goals
  refuse new steps.
- Inspectable state: next_actions shows the next actionable step, every
  blocker with its reason, exact progress, deterministic order; paused
  and done goals are excluded.
- Deterministic matching: ambiguous goal or step matches are refused
  with ZERO effects; missing matches are clean errors.
- No cross-owner leakage: a second owner's commands, views,
  next-actions and exports have zero effect on the first owner.
- Grammar discipline: near-miss phrasings fall through; read-only
  commands create zero effects.
- Caps: 101st goal and 51st step refused, nothing evicted.

## Version

crayon-goals-v1
