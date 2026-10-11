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

Expected: 61 tests, OK (~8s). No pytest dependency; stdlib unittest.

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

Covered by the suite (61 tests):

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
- V34 (partial) restart: old-session handles refuse after reopen.
- V34b (receiver-side subset): exact repeat, delivery drift refusal,
  distinct same-second tokens, blocked repeat, blocked->resolved repeat
  with the closing resolution_event_id stamp.
- V35 durable-blocked and local-uncertain commit-uncertainty paths.
- V38 receipt store_revision floor on no-op commits.
- V39 conflicting-receipt evidence never auto-resolves; rollback proceeds
  only through the witness protocol; evidence rows immutable.
- V40 evidence overflow: count frozen at 64, overflow marker set and
  retained, 65th delivery received-refused.
- V41 revision bump table (applied/unchanged/conflict/wipe).
- V42 store_revision overflow refusal.
- V43 uid 0 valid.
- V44 changed read targets refuse (MISSING_IDENTITY).
- V45/V46 live vs history views never merged; history ordered.
- V47 cursor suite: malformed structure, fabricated id, unknown cursor,
  paging to exhaustion.
- V48 count matches list under identical filters.
- V49 production stubs refuse unconditionally.
- V50 sensitive-scope requirement refuses at Phase A (no execution).
- V51 legacy_sensitive fixtures erase as ordinary content.
- V52 set_optin missing/same/changed incl. FIRST-False (virtual default
  OFF, no row, revision +0).
- V53 historical APIs need a fresh READ claim + typed originals;
  unregistered op = UnknownOperation.
- V54 (partial) ERASED precedes CAS/capacity; malformed cursor covered in
  V47.
- V55 (partial) binding projection carries no nonce/fence/target ids;
  identity/channel/capture fields present and stable.

NOT yet covered (planned, no claims made):

- V34 step-by-step fault injection after each of steps 1-8 individually.
- V34b issuer-harness vectors (LIVE ERASE hook, pending restart, lost
  receiver ack, lost delivered-update reread, safe payload/stable token)
  and the mixed-batch accounting vectors; the receiver-side
  duplicate-token-in-one-batch accounting.
- V36/V37 known-commit read failures (ReadbackUnavailableError shape).
- V44's one-transaction binding property (structural; not directly
  observable in the model test harness).
- V47 report-page cursor retention rules beyond the suite's checks;
  live-revision invalidation of facts cursors.
- V54 full precedence matrix beyond the covered rows.
- V55 full claim/cap/request/current-issuer/generations mapping rows.

## Known model boundaries

- SQLite serializes writers at BEGIN IMMEDIATE; the declared footprint
  order is documented but per-table locks are a no-op in the model.
- The clock is the JSON-driven test clock; no wall time.
- The resolver fences/attests through the model's abort-witness tables;
  an op with no witnesses stays UNRESOLVED (never absence-as-proof).
