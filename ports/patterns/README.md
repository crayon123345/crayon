# /ports/patterns - pattern cards: reviewable, never auto-acting

Companion-trait build (Dots "proactive research is READ-ONLY" + Muse
"opt-in proactive suggestions"): the integration feeds OBSERVATIONS,
the engine spots simple recurring patterns with EXPLICIT deterministic
rules, and surfaces them as cards the owner can accept, dismiss or
snooze. Cards are suggestions with evidence - they never create goals,
reminders or messages by themselves. Stands alone.

## STATUS: STAGING MODULE - NOT MOUNTED, NOT LIVE

Complete as a tested module (19/19 tests) but NOT wired into the
running app. Named limits, exactly:

1. READ-ONLY BY CONSTRUCTION: no code path from a card to any action.
   accept() marks and returns the card; what the integration does with
   an accepted card is its own gated flow (ports/approvals territory).
2. EVIDENCE IS VISIBLE: every card carries rule id, matching observation
   count, first/last seen. No card without evidence; no evidence below
   the rule's minimum count.
3. RULES ARE DATA + DETERMINISTIC: window / minimum count / cooldown.
   No model calls, no fuzzy inference. Same input -> same cards
   (determinism-tested).
4. OPT-IN: observe() is a no-op until `patterns on`; `wipe my patterns`
   deletes observations AND cards AND the opt-in.
5. OWNER-STRICT: observations, cards, decisions keyed to the owning
   account only; a second owner's wipe leaves the first byte-identical.
6. CAPS REFUSE, NEVER EVICT: MAX_OBSERVATIONS = 500,
   MAX_CARDS_PER_OWNER = 30, MAX_CARDS_PER_RULE = 5 open+snoozed;
   overflow raises with zero effects.
7. NO NAGGING: one open card per (rule, signature); an ACCEPTED
   signature never re-raises in v1; a dismissed signature respects its
   rule cooldown; a snoozed card re-surfaces only after its snooze
   expires AND fresh evidence exists.
8. CLOCKS ARE INJECTED: event ts and processing now are both explicit.
9. WHAT COUNTS AS AN OBSERVATION IS MOUNT WORK: this module records
   what the integration feeds it (explicit owner-visible events);
   passive scraping of private content is a MOUNT-TIME policy decision,
   not something this port enables by itself.
10. STORAGE IS INJECTED, NOT DURABLE: MemPatternStore is in-memory; the
    SQL adapter (encryption-at-rest, vid keying) is integration work.

## Chat surface (explicit grammar)

- `patterns on` / `patterns off` / `wipe my patterns`
- `my patterns`
- `pattern <match> accept | dismiss | snooze [days] | show`
- `export my patterns`

Anything else falls through. Ambiguous or empty references refuse with
zero effects.

## Acceptance criteria covered by tests (test_patterns.py, 19 tests)

- Off-by-default zero writes; wipe deletes everything.
- Min-count gating, window exclusion, evidence fields on cards.
- Determinism: identical input -> identical cards.
- No duplicate open cards; snooze/cooldown/accepted suppression.
- Decisions have zero side effects; decided cards are not re-decidable;
  ambiguity and empty references refuse.
- Cross-owner isolation incl. wipe; observation and card caps refuse.
- Grammar fallthrough; chat round-trip incl. export.

## Version

crayon-patterns-v1
