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

Expected: 116 tests, OK (~60-66s; file-backed exclusive-fixture DBs). No pytest dependency; stdlib unittest.

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

Covered by the suite (116 tests):

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
- V34 fault injection literally AFTER EACH declared pipeline step (v80
  3928): every "post" hook runs the REAL step body FIRST (its writes land
  in-txn), THEN raises, so the proven boundary is post-step, not a
  pre-step entry hook. Boundary map: after_1a footprint (post-body),
  after_2 current checks + Phase A (the step-2 block is complete at the
  entry of the adjacent 3a seam - the two steps run back-to-back in
  _execute_current_checks, so the 3a-seam entry hook IS the literal
  after-2 point), after_3a association/equality (post-body), after_4
  Phase-B readback, after_5 tombstone, after_6 CAS, after_7 classify,
  after_9 apply+record (all post-body), after_8 monotone allocation =
  the post-allocation CRASH (wipe path), and the post-apply boundary is
  the commit-guard abort/lost-commit
  pair): the attempt rolls back ENTIRELY (no partial effects, no claim
  leak), an unchanged retry is permitted, NO blind retry. RESTART leg per
  boundary: a real file close/reopen after the injected fault - NO
  partial attempt survives (no facts, no armed witness, no registry row)
  and a fresh mint on the reopened store executes; old-session handles
  refuse after reopen, durable rows survive, absent op rows type as
  UnknownOperation, never rollback proof.
- V34b receiver-side: exact repeat, delivery drift refusal, distinct
  same-second tokens, blocked repeat, blocked->resolved repeat with the
  closing resolution_event_id stamp; MIXED BATCH (old no-event + new
  late: 3 + 0), MIXED NO-EVENT/REPLAYED-LATE/NEW-LATE (0 + 0 + 3 = 3,
  stored_new_retained 1/3), DUPLICATE TOKENS IN ONE BATCH (late 3 not 6,
  blocked 1 not 2).
- V34b issuer-harness vectors: LIVE ERASE - SCRUB INTERFACE (exact):
  the actual shape is scrub(token, refs) -> ScrubOutcome, the harness
  TOKEN WRAPPER over the SEPARATELY DECLARED v80 hook scrub_hook(refs:
  OwnedContainers) -> ScrubOutcome (the exact v80 2060-2137 hook shape,
  ALSO invoked standalone at teardown): the hook owns the refs checks +
  discard + live-absence check + typed outcome ONLY (NO recording, NO
  token); the wrapper binds 'cleared' to the erase step's completion
  record: refs IS the
  OwnedContainers closed enumeration {raw_ref_handle, comparison_buffer};
  the token keys the scrub_log completion row, written ONLY AFTER
  'cleared' is emitted (kind='cleared' AND raw_found=false STRICT AND ALL
  containers checked); 'refused' records NOTHING and changes NO state.
  raw_found is the CLOSED bool: True when raw was found OR absence is
  UNPROVEN (a None/0/empty/non-bool/failed/missing probe = conservative
  True); False ONLY on a COMPLETE strict literal-False probe set (the
  strict literal-False proven-empty is required BEFORE 'cleared'). The
  FULL WORKFLOW is proven on the CHOSEN KNOWN-COMMIT side (compose ->
  send -> deliver -> ack=delivered): a REFUSED scrub preserves the
  delivered commit (no rollback, no uncertainty, NO redelivery) and
  records NOTHING; a CLEARED scrub records completion AFTER the hook's
  proof with NO state change to the commit; the SAME hook runs
  standalone at teardown and records nothing on its own. W2 composes
  the hook with a FACTSTORE erase/readback outcome + REVISION proof on
  a known locked commit: a known put commits at store_revision R with
  an applied readback outcome; REFUSED preserves BOTH the delivered
  certainty AND revision R with no re-execute; the FactStore WIPE
  erase lands an applied outcome with the live row gone on readback at
  a FRESH revision R' > R; BOTH REFUSED and CLEARED are exercised
  AFTER the known erase/readback: REFUSED post-erase BLOCKS the erase
  step's completion record (no scrub_log row, R' and the delivered
  certainty retained, NO re-execute); CLEARED post-erase records
  completion ONLY after the live coverage passes with the erased
  revision/outcome/certainty untouched; teardown runs the same hook
  standalone. The COMPLETION RECORD BINDS THE ERASE STEP EXACTLY
  (eleventh fold): the caller passes ONLY an OPAQUE step CLAIM
  {operation_id, coverage_id} - a per-call outcome/revision/verify
  callback is REJECTED (a caller-selected callback is not proof), and
  the claim is COPIED + normalized BEFORE BEGIN (the caller's dict is
  never re-read). The claim resolves through the FROZEN typed
  OwnedEraseReadback adapter - the ONLY accepted registration type
  (exact type check, store bound AT CONSTRUCTION, checked against the
  harness's injected receiver store; a lambda/function/subclass/
  duck-typed callable/second registration all REFUSE) - into the
  SOURCE-OWNED FactStore.erase_proof route. erase_proof verifies the
  EXACT committed erase identity under ONE coherent receiver read
  snapshot: exactly one operation row with strict positive ids, WIPE
  method + 'applied', a TYPED DECODE through the ACTUAL
  _decode_stored_outcome rule (twelfth fold: outcome_json +
  outcome_kind + scrubbed decoded by the SAME rule _write path
  decodes - a durable missing/contradictory result, a union-
  impossible 'refused', an 'unknown'/scrubbed kind-flag mismatch, a
  dropped/mismatched wipe report (owner/lifetime/operation), and any
  unknown class beyond the DECLARED provider_side one all REFUSE;
  json.loads agreement alone is NOT proof), PLUS the EXACT REPORT
  JOIN (thirteenth fold): report.commit_revision ==
  outcome.applied_at_revision, report.new_epoch == outcome.new_epoch
  == the owner's DURABLE current epoch, and the NESTED first page
  binding the SAME report_id/owner/lifetime/operation/commit with
  every inventory entry carrying the SAME owner - a positive-but-
  contradictory report REFUSES; the negatives isolate ONE field per
  probe with a consistent-row control, so each refusal is
  attributable to the intended field), the decoded outcome
  agreeing on method + applied_at_revision, the owner's CURRENT lifetime, the
  FRESH-CURRENT policy (applied_at_revision == the CURRENT store
  revision - the claimed erase must be the LATEST committed op for
  the owner subject; historical revision-owned coverage is UNPROVABLE
  here and REFUSES; newer absence is never substituted for old
  coverage; unrelated owners never stale), and the CURRENT
  owner+lifetime-scoped coverage probe (facts == 0). The scrub_log
  row carries the OWNED tuple (applied AND verified revisions), never
  caller claims; the scrub-side CLOSED dict validation refuses a
  forged method/result/id/coverage/join even in a shaped dict. Step
  key erase:{owner_uid}:{lifetime_id}:{operation_id}:{coverage_id}:
  {applied_revision} is UNIQUE: a second completion of the same step
  OR the same token is a typed conflict refusal (NO re-execute).
  Verification runs AFTER BEGIN (issuer-log coupling; the readback
  reads the RECEIVER store - cross-DB atomicity is NOT claimed). A
  scrub WITHOUT the claim records ONLY a 'generic' volatile-hook
  completion - NEVER an erase-step gate; the standalone teardown hook
  completes NOTHING. No physical memory-wipe claim: the hook clears
  VOLATILE fixture handles ONLY. A standalone scrub_log row is NOT
  integration proof.
  PENDING RESTART (unacked rows redelivered with the SAME token), LOST
  RECEIVER ACK (row stays pending; same-token redelivery replays
  receiver-side, no double-count), LOST DELIVERED-STATE UPDATE
  (confirm-lost = typed uncertain; the durable reread binds token +
  recovery + projection - delivered = done, pending = re-attempt; an
  UNDECODABLE/shape-incoherent row or a MISSING binding input (no
  validated projection) = typed uncertain, NEVER a raw exception, NEVER
  an unvalidated success), SAFE PAYLOAD/STABLE TOKEN (ref_json is
  projection output only), forged committed_result / invented EvidenceRef
  = REFUSED, never delivered.
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
- Mutation caller-context binding (sec 10): the ORIGINAL envelope must
  BE the request's actual binding; ORIGINAL issuer == CURRENT issuer;
  the caller's CURRENT snapshot fields (uid/lifetime/capture_epoch/
  grant_generation/policy_revision/decision_revision/
  consent_generation) must EQUAL the store's OWNED snapshot re-read
  inside the transaction, AFTER Phase A (PR-3 tier precedence
  preserved) - a fabricated/stale field refuses typed STALE.
- Evidence target/source existence (v80 795-812): an OP_RECORDS
  receipt target must NAME the anchor's receipt AND that receipt row
  must EXIST in the durable inventory - guessed/fabricated strings
  refuse; per-kind positive/absent/mismatch checks are unit-tested.
- Scrub typed outcomes (v80 2060-2137): destroy/clear/probe FAILURES
  are contained (typed 'refused', never raw exceptions); ONLY an
  explicit live_raw() == False probe proves absence - a missing or
  failed probe is UNKNOWN and never clears; the outcome schema is
  EXACTLY {kind, ref_ids, live_check}.
- Issuer-ack durable reread (v80 1628-1645): binds token + recovery +
  projection (source included); a POSITIVE same-identity pending =
  re-attempt; missing/unreadable/unknown-state/incoherent = typed
  uncertain - never a blind pending claim.
- The (recovery_id, delivery_token) UNIQUE fence is exercised by the
  ACTUAL loser path in tests (forced real INSERT conflict -> rollback
  -> winner compare -> replay or typed drift refusal); a stored
  evidence row with a MISSING insertion event is a typed integrity
  refusal (never a defaulted count); V34 fault injection covers each
  execute step INDIVIDUALLY with a LITERAL post-step fault (1a
  footprint, 2 current checks via the 3a-seam entry, 3a association,
  4 Phase-B readback, 5 tombstone, 6 CAS, 7 classify, 8 allocation,
  9 apply+record) AND the replay + forced-fence loser RE-VERIFY the
  ONE-TO-ONE PER-CLASS ledger coupling (v80 1254-1308: count, class,
  subject, event, destination, triple, anchor, source) - a
  missing/wrong ledger/class/subject/triple/anchor/source/count is a
  typed EvidenceIntegrityError on BOTH paths, never an incoherent
  default success. The SAME FULL validation runs on EVERY canonical
  path (late branch, forced-fence loser branch, AND the dedup/confirm
  read): the resolved anchor identity is verified ALWAYS, including an
  EMPTY ledger set (a zeroed-count event with no ledger rows refuses);
  the SELECTED delivery is bound EXACTLY - the answered evidence row
  must itself be a ledger member of the event (a row bound to a
  coherent event for a DIFFERENT delivery refuses); and the event<->
  evidence set is BIDIRECTIONALLY equal (an extra evidence row
  claiming the event with no ledger entry refuses). W1 adds the INSERTION-IDENTITY/COUNT proof (v80
  960-965): each joined evidence row's inserting uid/lifetime/op
  EQUALS the event's inserting identity (mutations on EITHER the
  evidence row OR the event row refuse typed on BOTH paths), and
  new_retained is RECOMPUTED from the durable rows the event created
  (2*inserted_count + 1: evidence + event + ledger) - the event's own
  new_retained claim is verified, and replay/loser results report the
  CANONICAL count, never the claim. The WRITE leg (v80 1050-1074) is
  ENFORCED at insert: the capability's own request op == the
  registered original binding's op == the supplied READ op, typed
  refusal otherwise - the read_operation_id argument is never trusted
  (negative: minted-for-b3 called with b4 refuses, nothing inserted).
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
