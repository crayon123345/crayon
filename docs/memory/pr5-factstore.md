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
  unregistered op = UnknownOperation.
- V54 precedence: structural/sensitive tiers refuse BEFORE expiry
  (never ExpiredRefused), expiry BEFORE revocation, revocation BEFORE
  scope, scope BEFORE capacity, CAS not masked at capacity, ERASED
  precedes CAS/capacity; malformed cursor is structural everywhere.
- V55 full claim/binding mapping: no nonce/fence/target ids; issuer
  channel agreement (WEB = SessionRef-shaped 64-hex web_session;
  TELEGRAM = TelegramIssuer bot/chat/update ids, canonical JSON
  round-trip); current_issuer_valid False = STALE_POLICY; the full
  generations mapping (capture_epoch/grant_generation/policy_revision/
  decision_revision/consent_generation) equals the owned fixture
  snapshot.
- B9 wrapper typing: a None wrapper never dereferences - typed refusal
  on get/list/execute paths.
- The exclusive cross-instance isolation fixture: a second opener of the
  same path refuses; a file-backed connection WITHOUT the exclusive
  marker refuses; close releases the lock; :memory: remains allowed.

NOT yet covered (planned, no claims made):

- Concurrent double-delivery (one winner, loser replays) - the model's
  single-writer SQLite makes a true concurrent race inexpressible;
  the same-token replay path covers the loser's outcome.
- Post-scrub repeat (safe-field comparison) and the UNIQUE-drift /
  unrelated-same-owner-target receiver vectors.

## Known model boundaries

- SQLite serializes writers at BEGIN IMMEDIATE; the declared footprint
  order is documented but per-table locks are a no-op in the model.
- The clock is the JSON-driven test clock; no wall time.
- The resolver fences/attests through the model's abort-witness tables;
  an op with no witnesses stays UNRESOLVED (never absence-as-proof).
