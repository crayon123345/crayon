# PR-5: FactStore MODEL (ports/memory/factstore.py)

MODEL ONLY, unwired. This PR adds three append-only paths and touches
nothing else:

- `ports/memory/factstore.py` - the FactStore model
- `ports/memory/tests/test_pr5_factstore.py` - the named-vector suite
- `docs/memory/pr5-factstore.md` - this document

## What it is

A synthetic, disposable, single-connection SQLite MODEL of the PR-5
FactStore contract (locked v80), built over the UNMODIFIED merged PR-1/3/4
modules (`owner_counters.py`, `capability.py`, `envelope.py`). Every
fixture is public synthetic ("minted by fixture F") and is never
authority. There is no wiring, no real keys or content, no migrations,
no non-disposable DDL, no network, and no external preflight.

The PRODUCTION path is a stub: `ProductionFactStore` and
`SensitivePurgeStub` refuse `ProductionStubRefused` / MODEL_ONLY
unconditionally, before any dependency injection.

## How to run

```
cd ports/memory
python3 -m unittest tests.test_pr5_factstore
```

Expected: 96 tests, OK (~52s; file-backed exclusive-fixture DBs). No pytest dependency; stdlib unittest.

## Shape

- ONE disposable SQLite connection (receiver scope). The issuer harness's
  `fixture_store_conn` is a SEPARATE connection; no cross-DB atomicity.
- BEGIN IMMEDIATE per operation; the 9-step execute pipeline
  (footprint -> Phase A via the REAL PR-3 `ModelOnlyEvaluator.recheck_model`
  -> association/equality re-checks -> Phase B replay/readback -> tombstone
  -> CAS -> classify -> allocation -> effects + records), commit-uncertainty
  handling (durable blocked marker / local uncertain), historical reads
  (readback/resolve), and the evidence receiving path (insert_evidence).
- The extended binding hash = SHA-256 over the canonical PR-4 projection
  plus the canonical PR-5 method + payload; transport fields (nonce, key)
  never enter it.
- Erasure model: content rows physically removed; identity moves to
  `erasure_identities` (ids/commitments only); shared op rows scrub
  WHOLE-SAFE at first delete; tombstone markers (HMAC key_refs, no
  content) refuse future puts of erased keys with ERASED while
  marker.generation <= current epoch.
- Epochs come only from the PR-1 monotone allocator and are never reused.
- COMMITTED receipts always observe store_revision >= 1, including no-op
  commits (applied_at_revision None on no-ops; the two are never
  conflated).
- Post-scrub replays through the historical API return the ScrubbedOutcome
  variant - never content. Mutation replays require the ORIGINAL
  capability still valid; after epoch/policy movement the fresh-read
  historical API is the only lookup (EPOCH/REVOKED/STALE_POLICY refuse
  first, per the PR-3 tier order; there is no universal DRIFT-over-EPOCH).

## Test-vector coverage (honest)

Covered by the suite (96 tests):

- V01-V10 put/supersede classification incl. normalization collision,
  oversize and string rejections, N-to-1, duplicate old_fids, non-target
  key collision, no epoch on supersede.
- V11-V18b delete/wipe incl. fresh epoch + DeleteProof, absent delete
  (recorded, no epoch), marked-target delete, history-only full closure,
  empty wipe, retained whitelist, post-scrub ScrubbedOutcome via readback,
  safe-id-only reports, report paging over >4096 inventory.
- V19-V25 CAS refusal purity, same-binding retry, changed-versions drift,
  cap boundaries, N-to-1 at cap, CapRefused(resulting_count), last-slot
  serialization.
- V26-V33 terminal epoch refusals (no recapture), epoch never reused,
  prior-lifetime and cross-owner replay refusals, valid-capability replay,
  Phase-A-before-drift precedence (incl. EXPIRED first), REVOKED before
  replay, reseal/registry re-claim refusal.
- V34 fault injection after each step class: Phase-A refusal (claim
  unwinds; unchanged retry permitted), apply fault (the ENTIRE attempt
  rolls back - no armed witness/registry left), commit-guard abort
  (typed local_uncertain, NO blind retry), lost commit confirmation
  (durable_blocked; the witness stays ARMED for the resolver). RESTART:
  a real file close/reopen - old-session handles refuse, durable rows
  survive, absent op rows type as UnknownOperation, never rollback proof.
- V34b receiver-side: exact repeat, delivery drift refusal, distinct
  same-second tokens, blocked repeat, blocked->resolved repeat with the
  closing resolution_event_id stamp; MIXED BATCH (old no-event + new
  late: 3 + 0), MIXED NO-EVENT/REPLAYED-LATE/NEW-LATE (0 + 0 + 3 = 3,
  stored_new_retained 1/3), DUPLICATE TOKENS IN ONE BATCH (late 3 not 6,
  blocked 1 not 2).
- V34b issuer-harness vectors: LIVE ERASE (the scrub hook destroys
  the volatile raw BEFORE the completion row; hook failure = step
  refusal, nothing recorded), PENDING RESTART (unacked rows redelivered
  with the SAME token), LOST RECEIVER ACK (row stays pending; same-token
  redelivery replays receiver-side, no double-count), LOST
  DELIVERED-STATE UPDATE (confirm-lost = typed uncertain; durable reread
  decides; unreadable reread = typed uncertain), SAFE PAYLOAD/STABLE
  TOKEN (ref_json is projection output only), forged committed_result /
  invented EvidenceRef = REFUSED, never delivered.
- V35 durable-blocked and local-uncertain commit-uncertainty paths.
- V38 receipt store_revision floor on no-op commits.
- V39 conflicting-receipt evidence never auto-resolves; rollback proceeds
  only through the witness protocol; evidence rows immutable.
- V40 evidence overflow: count frozen at 64, overflow marker set and
  retained, 65th delivery received-refused.
- V41 revision bump table (applied/unchanged/conflict/wipe).
- V42 store_revision overflow refusal.
- V43 uid 0 valid.
- V36/V37 known-commit read failures (protocol AND operational):
  ReadbackUnavailableError carries the KNOWN outcome + revision; the
  commit certainty is unchanged (the same op replays); a failed current
  read yields NO PostReadResult (current_revision None).
- V44 one-transaction binding: Phase A runs INSIDE the one read
  transaction (observed via the evaluator seam); changed read targets
  refuse (MISSING_IDENTITY); a setup minted for fid1 never reads fid2.
- V45/V46 live vs history views never merged; history ordered.
- V47 cursor suite: malformed structure (STRUCTURAL - refuses before
  revocation), fabricated id, unknown cursor, paging to exhaustion;
  live-revision movement invalidates 'facts' cursors; 'report_page'
  cursors RETAINED against their bound revision until erased (wipe
  clears the registry).
- V48 count matches list under identical filters.
- V49 production stubs refuse unconditionally.
- V50 sensitive-scope requirement refuses at Phase A (no execution).
- V51 legacy_sensitive fixtures erase as ordinary content.
- V52 set_optin missing/same/changed incl. FIRST-False (virtual default
  OFF, no row, revision +0).
- V53 historical APIs need a fresh READ claim + typed originals;
  unregistered op = UnknownOperation. LOAD_STORED historical reads
  recompute the FULL request+binding extended hash (the stored typed
  request + the caller's typed binding) and compare EQUAL to the
  retained commitment; a stored row whose request/binding pair no
  longer hashes to its commitment is drift.
- V54 precedence: structural/sensitive tiers refuse BEFORE expiry
  (never ExpiredRefused), expiry BEFORE revocation, revocation BEFORE
  scope, scope BEFORE capacity, CAS not masked at capacity, ERASED
  precedes CAS/capacity; malformed cursor is structural everywhere.
- V55 FULL claim/binding mapping (v80 4045-4052), every element
  covered: the binding carries NO nonce/fence/target ids; transport
  reseal (nonce/key change) still matches (V33); a content or identity
  change drifts (V31); issuer channel agreement (WEB =
  SessionRef-shaped 64-hex web_session; TELEGRAM = TelegramIssuer
  bot/chat/update ids, canonical JSON round-trip); current_issuer_valid
  False = STALE_POLICY; original-issuer == current-issuer equality on
  every read; the CLAIM mapping (claim_state ACQUIRED + owner_token +
  fence >= 1 at the execution gate); the CAPABILITY mapping (the read
  commitment binds the wrapper handle AND the actual supplied caller
  canon; the mutation wrapper binds the registered request+binding
  hash); the REQUEST mapping (the extended commitment covers method +
  targets + versions + payload); the full GENERATIONS mapping
  (capture_epoch/grant_generation/policy_revision/decision_revision/
  consent_generation) equal to the owned fixture snapshot.
- B9 wrapper typing: a None wrapper never dereferences - typed refusal
  on get/list/execute paths.
- The exclusive cross-instance isolation fixture: a second opener of the
  same path refuses; a file-backed connection WITHOUT the exclusive
  marker refuses; close releases the lock; :memory: remains allowed.

- V34 fault injection after EACH execute step (v80 3928):
  launch-gate/Phase-B readback (steps 1-4), the TXN-2 current-checks
  recheck (steps 2-3a), tombstone (5), CAS (6), resulting-count
  classification (7), monotone allocation (8), apply+record (9) - each
  fault rolls the attempt back ENTIRELY (no partial effects, no
  armed-witness/registry leak) and an UNCHANGED retry stays permitted
  (pre-commit refusal, no blind retry); the commit-guard abort and
  COMMIT-loss stay typed uncertain/blocked with the witness ARMED for
  the resolver; RESTART refuses old-session claims; an absent row is
  never treated as rollback proof.
- V34b insertion-event named vectors (v80 3931-3953), each with a
  dedicated test: concurrent double-delivery (two independently
  composed same-token deliveries: one winner, the loser replays with
  no writes); exact repeat (replay, no writes); delivery drift (typed
  refusal); lost ack (lookup-before-mint replay); LOST DELIVERED-UPDATE
  (durable reread under the same token/recovery/projection: delivered
  = done, pending = re-attempt, unreadable = typed uncertain, no blind
  pending claim, no blind redelivery); restart (the durable row wins,
  same-token redelivery); post-scrub repeat (safe-field projection
  comparison ONLY, replay); LATER RECEIPT REDELIVERY (same token =
  replay); DISTINCT SAME-SECOND deliveries (distinct tokens, both
  inserted); UNCHANGED-SCRUB replay (post-scrub replay via the safe
  projection); CHANGED target/source under the same delivery identity
  (scope/drift refusal); UNIQUE drift (the (recovery_id,
  delivery_token) UNIQUE fence: conflict -> rollback -> winner compare
  -> replay or typed drift refusal); UNRELATED SAME-OWNER TARGET (the
  store-built target/source binding refuses); STORED MISMATCHED
  PROJECTION (stored != arriving = drift refusal); DELAYED
  BLOCKED-ORIGINAL AFTER CLOSURE + BLOCKED BEFORE/AFTER (no event, no
  writes both times); BLOCKED REPEAT / BLOCKED->RESOLVED REPEAT
  (no-event, no mint, no retroactive counts); LATE-ORIGINAL AFTER
  REPLAY (same triple/projection/counts = replay); MIXED BATCH and
  MIXED NO-EVENT/REPLAYED-LATE/NEW-LATE (batch contribution 0+0+3);
  DUPLICATE TOKENS IN ONE BATCH (counted ONCE); FORGED FIELDS (a
  durable-join mismatch = integrity refusal); the issuer-harness
  vectors LIVE ERASE, PENDING RESTART, LOST RECEIVER ACK, LOST
  DELIVERED-STATE UPDATE, SAFE PAYLOAD/STABLE TOKEN.

## Known model boundaries

- SQLite serializes writers at BEGIN IMMEDIATE; the declared footprint
  order is documented but per-table locks are a no-op in the model.
- The clock is the JSON-driven test clock; no wall time.
- The resolver fences/attests through the model's abort-witness tables;
  an op with no witnesses stays UNRESOLVED (never absence-as-proof).
