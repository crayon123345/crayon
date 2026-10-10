# /ports/approvals - exact approval cards enforced outside prompts

Companion-trait build (priority #4): actions that need owner approval get
a structured, exact approval card - and the ENGINE, not a prompt, decides
whether a presented approval satisfies a pending request. The model can
propose; only a recorded, exact, unexpired, single-use owner decision
permits. Stands alone.

## STATUS: STAGING MODULE - NOT MOUNTED, NOT LIVE

This port is complete as a tested module (25/25 tests) but is NOT wired
into the running app. Named limits, exactly:

1. NO EXECUTION: this module owns requests, cards and decisions only.
   approve() marks a request approved and returns the record - RUNNING
   the action is the integration's job, gated on the returned state.
2. CARDS ARE EXACT: owner + action + target + canonical params digest.
   A card that differs in any field refuses - no fuzzy matching.
3. SINGLE-USE: consuming a card burns it; replay refuses even for an
   identical request (a new request needs a fresh card).
4. EXPIRY: cards expire (default 15 min, injected clock); expired cards
   refuse and are marked. Boundary tested at ttl-1 / ttl.
5. POLICY IS DATA: actions map to auto / card / refuse. UNKNOWN ACTIONS
   DEFAULT TO REQUIRE CARD - never to allow.
6. DECISIONS ARE RECORDED: requests, issues, approvals, denials and
   refusals all land in a per-owner audit log (capped 200) - no silent
   behavior.
7. OWNER-STRICT: requests, cards, decisions and audit are keyed to the
   owning account only. Cross-owner presentation and even cross-owner
   expiry sweeps have zero effect.
8. CAPS REFUSE, NEVER EVICT: MAX_PENDING_PER_OWNER = 25; denying or
   approving makes room.
9. CLOCKS + IDS ARE INJECTED: every time-dependent method takes `now`;
   id_factory is injectable. The test suite is fully deterministic.
10. AUTHENTICATED IDENTITY IS MOUNT WORK: issue_card() is the owner's
    act of approval - the integration must call it only on an
    authenticated owner command. This module enforces exactness and
    bookkeeping; it does not authenticate callers (same convention as
    every port: vid keying is supplied by the mount).
11. OVERFLOW REJECTS, NEVER TRUNCATES: action (60), target (200) and
    params (4000 bytes) over their bounds raise with zero effects -
    truncating would alias two distinct long values onto one
    exact-match key (review 5479329697 regression-tested, incl. the
    boundary at exactly-max).

## Integration standard (recorded with the owning side)

A card is only ever minted from the ORIGINAL authenticated owner
decision on the exact rendered recipient/contents. Card possession,
model text, or peer text is NOT permission. The mount must bind:
owner, session, origin, request, params hash, expiry, and atomic
one-use consumption; revalidate the payload against the card at
execution time; on any unknown or mismatch, refuse WITHOUT retry.
11. STORAGE IS INJECTED, NOT DURABLE: MemApprovalStore is in-memory; the
    SQL adapter (encryption-at-rest, vid keying) is integration work.

## Chat surface (explicit grammar)

- `pending approvals` / `my approvals`
- `approve <match> with card <id>`
- `deny <match> [because <reason>]`
- `approval <match> show` / `approval audit` / `export my approvals`

Anything else falls through. Ambiguous or empty references refuse with
zero effects.

## Acceptance criteria covered by tests (test_approvals.py, 25 tests)

- Unknown actions require a card (never auto); auto stores nothing;
  refused-policy actions deny immediately and cannot be revived.
- Exactness: wrong params / target / action / extra key all refuse;
  request stays pending.
- Single-use: replay on an identical request refuses; expiry refuses and
  marks; fresh card after expiry approves; sweep marks expired cards.
- Isolation: cross-owner card/request/deny/sweep all zero-effect.
- Ambiguity and empty references refuse with zero effects.
- Pending cap refuses; deny makes room.
- Grammar: near-miss phrasings fall through; chat round-trip works.
- Audit: every decision recorded in order.

## Version

crayon-approvals-v1
