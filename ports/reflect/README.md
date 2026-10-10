# /ports/reflect - reflective conversation mode (thought partner)

Per-owner conversation mode where Crayon acts as a thought partner:
**Listen / Reflect / Plan**, with ONE useful follow-up question per reply
instead of a barrage. P0 capability #2 in the Dot/Muse trait mapping,
layered on the shipped personal-continuity store (/ports/memory): open
`followup` facts are what make the single follow-up useful instead of
generic.

## STATUS: STAGING MODULE - NOT MOUNTED, NOT LIVE

This port is complete as a tested module (20/20 tests) but is NOT wired
into the running app. Named limits, exactly:

1. NOT MOUNTED: nothing calls ReflectiveMode from integrated/ yet.
   Integration surface (for the owning side):
   - call handle_message(owner, text, sid, ts) BEFORE model dispatch,
     AFTER the continuity handle_message (a non-None reply short-circuits
     the model); same `owner` (visitor vid from memory.identity());
   - when building the system prompt, append prompt_addendum(owner) -
     empty string when the mode is off, so the check is free;
   - pass the model's drafted reply through
     ReflectiveMode.enforce_one_question(reply) before sending.
2. PROMPT IS A REQUEST, THE FILTER IS THE GUARANTEE: the addendum asks the
   model for Listen/Reflect/Plan and exactly one question, but a model can
   ignore a prompt. The only enforced guarantee is the deterministic
   post-filter enforce_one_question(), which reduces a drafted reply to at
   most one question. Both halves ship here; the README does not claim the
   prompt half is enforceable.
3. STORAGE IS INJECTED, NOT DURABLE: mode state lives behind a ModeStore
   interface (get_mode/set_mode/get_last_fid/set_last_fid); MemModeStore
   is in-memory only. Follow-up grounding uses the SAME duck-typed
   FactStore interface as /ports/memory (list(owner, include_sensitive=False));
   passing the production continuity adapter in gives grounding for free.
   The SQL adapter for mode state is integration work, deliberately not
   shipped half-wired here.
4. MODE STATE IS PER-OWNER AND EXPLICIT: on/off only via the commands
   below or a programmatic set_mode call. There is no ambient mode and no
   cross-owner path. Ambiguous phrasing ("reflect on my day") is NOT a
   command and falls through to the model.
5. SENSITIVE FOLLOW-UPS NEVER ENTER THE PROMPT: open_followups() excludes
   sensitive facts always, independent of the owner's sensitive-storage
   opt-in, matching the continuity guarantee.
6. NO-NAGGING FOLLOW-UP POLICY: at most one open follow-up is surfaced per
   prompt; the one surfaced last time is skipped while another candidate
   exists and is NOT re-surfaced when it is the only one. Turning the mode
   off resets the surfacing history.
7. CRISIS HANDLING IS PROMPT-LEVEL ONLY: the contract tells the model to
   drop the format and encourage professional help if the user mentions
   self-harm. This module does NO crisis detection; a real safety layer is
   a separate, owned decision.
8. QUESTION GUARD IS NAIVE: question_count() counts every "?"; a "?" in a
   URL query string or emoticon also counts. Over-counting only ever trims
   extra questions, never invents content (tested).

## Commands (chat surface)

- `reflect on` / `reflective mode on` / `thought partner on` - enable
- `reflect off` / `reflective mode off` / `thought partner off` - disable
  (resets follow-up surfacing history; stored memory is untouched)
- `reflect status` (and the same aliases) - state + open follow-up count

## Acceptance criteria covered by tests (test_reflective.py, 20 tests)

- Contract content: addendum contains LISTEN / REFLECT / PLAN and the
  exactly-one-question instruction; empty when the mode is off.
- One useful follow-up: most recent open follow-up is surfaced; dedupe
  across consecutive prompts; a single candidate is not re-surfaced;
  sensitive and superseded follow-ups never appear; non-followup
  categories are never used.
- No barrage: a 3-question draft is reduced to exactly 1 question;
  non-question sentences survive; the filter never invents content;
  0- or 1-question replies pass through byte-identical.
- No cross-owner leakage: mode state and follow-up grounding are
  owner-scoped; a second owner's commands and facts have zero effect.
- Grammar discipline: commands create ZERO memory facts; ambiguous
  phrasing falls through; aliases are case-insensitive.

## Version

crayon-reflect-v1
