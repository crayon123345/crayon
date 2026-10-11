# PR-2 proposed schema - REVIEW DOCUMENT ONLY, NOT A MIGRATION

No DDL from this document is applied anywhere by PR-2. Migration is not
authorized. Table shapes are the app owners' schema decision; this
document proposes them for cross-review. Semantics are the v4.2
proposal set, ACCEPTED FOR THIS TWO-PATH LOCAL DRAFT by the peer
2026-10-11 (i2i: not code/schema approval; formal approval still
requires exact-head review of the actual files), with their four
draft-time clarifications folded in. Base: merged main
2a7ff6c268b1427f98f90813f44c2dca0eb71f3d (PR-1 merge commit;
re-verified live at drafting time). Additive column/table proposals
only; no changes to existing column definitions; no DDL applied.

Scope reminder: design docs + synthetic/local disposable tests ONLY.
Not code approval, not a migration, not encryption activation, not
real key creation/storage. SQLite design-model tests prove NOTHING
about Postgres ALTER/locking/encryption/auth/erasure. Any PG
fast-ALTER observation is version-dependent documentation, not lock
or migration proof.

## 1. facts.row_version - CAS foundation

```sql
ALTER TABLE facts ADD COLUMN row_version BIGINT NOT NULL DEFAULT 0;
-- proposed constraint: CHECK (row_version >= 0)
```

- 0 is a VALID value and means LEGACY / never touched by the seam.
  SEAM-CREATED rows start at row_version 1 (documented choice, see
  sec 2 note on superseded_by_version).
- STRICT increase on every COMMITTED material mutation of the row;
  an identical replay leaves it UNCHANGED; a CAS mismatch is an
  explicit typed conflict, never silent; overflow fails closed.
- Read/validation layer requires exact non-bool integers.

## 2. fact_history - immutable snapshots, ciphertext payload only

```sql
CREATE TABLE fact_history(
  id                  BIGSERIAL PRIMARY KEY,
  user_id             BIGINT NOT NULL,
  owner_lifetime      BIGINT NOT NULL,
  fact_id             BIGINT NOT NULL,
  source_row_version  BIGINT NOT NULL CHECK (source_row_version >= 0),
  closed_reason       TEXT NOT NULL
                      CHECK (closed_reason IN ('supersede','consolidate')),
  superseded_by_version BIGINT NOT NULL CHECK (superseded_by_version >= 1),
  operation_id        TEXT NOT NULL,
  replacement_fact_id BIGINT,
  archived_at         TIMESTAMPTZ NOT NULL DEFAULT now(),
  content_payload     BYTEA NOT NULL,
  UNIQUE (user_id, owner_lifetime, fact_id, source_row_version)
);
```

- One IMMUTABLE snapshot row per closed fact state. Snapshot
  uniqueness = (owner, lifetime, fact_id, source_row_version);
  same-operation grouping = operation_id (writer-minted UUID; there is
  NO correction-event counter anywhere).
- superseded_by_version [peer clarification 1]: for N-to-1
  consolidation this is the REPLACEMENT fact's POST-COMMIT row_version,
  paired with replacement_fact_id + owner_lifetime - it is NOT a
  global sequence. DOCUMENTED CHOICE on the fresh-replacement
  conflict: seam-created replacement rows start at row_version 1 for
  their first committed material state (0 is reserved for legacy
  pre-seam rows), so CHECK (superseded_by_version >= 1) always holds
  and the identity pair (replacement_fact_id, owner_lifetime) carries
  the actual identity. The N-to-1 junction is explicit: all N history
  rows share one operation_id and the surviving replacement_fact_id.
- content_payload: ONE serialized authenticated-encryption envelope
  holding the ENTIRE content snapshot - fact key, value, category,
  source AND the provenance snapshot (excerpt, capture timestamp,
  source ref, truncated flag). Envelope layout (documented, lands with
  PR-9): format_version, key_id, nonce, ciphertext, tag; AEAD context
  binds owner + owner_lifetime + fact_id + source_row_version +
  history id. NO plaintext duplicate of any content; NO content-bearing
  tombstones; NO plaintext content indexes; NO why-logs.
- Erasure-class operations PHYSICALLY REMOVE applicable rows;
  deletion-content history is NEVER inserted.
- NO SUPERSEDE-EVENT epoch column exists (PR-1's superseded_by_epoch
  concept is renamed to superseded_by_version + operation_id +
  replacement lineage). owner_lifetime STAYS: it is the lifetime
  identity (a PR-1 allocation) that began the account lifetime, not a
  per-supersede counter. Its derivation detail stays OPEN for
  implementation review - synthetic lifetimes in tests are not
  integration proof.
- Retention (2a.3): no automatic capacity deletion; capacity pressure
  = honest new-write refusal + paging; owner-directed erasure
  unconditional within scope.
- KNOWN OPEN SEAM: facts.key uniqueness / encrypted-key lookup once
  content (including key) sits inside envelopes is a FUTURE open seam.

## 3. Epoch vs version truth table

| event                           | users.memory_epoch  | row_version/lineage        |
|---------------------------------|---------------------|----------------------------|
| account creation                | advance (PR-1 only) | initial                    |
| ordinary correction / supersede | NO advance          | row_version +1, lineage row|
| N-to-1 FACT consolidation       | NO advance          | CAS/versioning ONLY        |
| erasure-class delete/purge/wipe | advance (PR-1 only) | rows physically removed    |
| ACCOUNT consolidation           | advance (PR-1 only) | per merge design           |

Ordinary supersede NEVER allocates an epoch, NEVER advances
users.memory_epoch, NEVER replaces captured request epochs, and has NO
hidden stale-intent refresh. Content/history/version changes roll back
together in the writer's single transaction (writer = later PR).

## 4. memory_markers - keyed exclusion markers

```sql
CREATE TABLE memory_markers(
  id                   BIGSERIAL PRIMARY KEY,
  user_id              BIGINT NOT NULL,
  marker_type          TEXT NOT NULL
                       CHECK (marker_type IN ('erased_content','excluded_content')),
  key_id               TEXT NOT NULL CHECK (length(btrim(key_id)) > 0),
  normalization_version TEXT NOT NULL
                       CHECK (length(btrim(normalization_version)) > 0),
  content_key_digest   TEXT NOT NULL
                       CHECK (content_key_digest ~ '^[0-9a-f]{64}$'),
  created_epoch        BIGINT NOT NULL CHECK (created_epoch >= 1),
  created_at           TIMESTAMPTZ NOT NULL DEFAULT now(),
  row_version          BIGINT NOT NULL DEFAULT 0 CHECK (row_version >= 0),
  UNIQUE (user_id, marker_type, key_id, normalization_version,
          content_key_digest)
);
```

- content_key_digest = full 32-byte HMAC-SHA256 as 64 LOWERCASE HEX,
  with ACTUAL hex-format validation (the regex CHECK above; the local
  sqlite mirror cannot express the regex and uses length + python-layer
  hex validation instead - dialect difference, stated honestly).
- key_id + normalization_version are nonblank VALIDATED metadata
  (CHECKs above); an unknown normalization profile FAILS CLOSED.
- KEY: INDEPENDENT random 32-byte OWNER-SCOPED marker key - not
  uid-derived, not global, NOT reused as (or derived from) any
  encryption key, NO unkeyed fallback. Missing key or unknown profile
  fails closed. HMAC limits DB-only guessing ONLY.
- created_epoch is METADATA ONLY - never an HMAC input, never a
  matching filter. Old exclusions SURVIVE epoch movement.
- NO normalized content, plaintext prefix, raw excerpt, or unkeyed
  hash in ANY marker, error, log, or export row. Markers are derived
  PRIVATE metadata, never exported/logged/surfaced.
- LOCK FOOTPRINT: memory_markers MUST JOIN the applicable per-operation
  lock footprint union for future writers; inventory/ordering stays
  OPEN, membership is settled.
- FULL-WIPE LIFECYCLE (resolved PER-OWNER, two independent selections
  2026-10-11 - not a joint decision): BOTH owners currently select
  keep_exclusions. Pratham's side: express per-choice delegation
  ("Your call") to his Instinct, peer i2i 4:30 PM. Our side:
  Instinct-selected under his explicit privacy-first delegation
  (2:31 PM) plus his endorsed criterion (4:31 PM), shown to him and
  correctable on his word - NOT owner-dictated. Design only;
  implementation/lifecycle effects stay separate gates.
    keep_exclusions (both scopes, current): the owner marker key AND
      exclusion entries SURVIVE a full memory wipe; the content-
      encryption key is destroyed SEPARATELY.
    clean_reset (available, selected by neither): markers AND the
      marker key are removed, leaving NO orphans.
  DISCLOSURE: retained exclusions are KEYED PRIVATE FINGERPRINTS -
  HMAC protects against DB-only guessing WITHOUT the key, but key
  compromise or oracle access CAN expose guessable content. Every
  erasure/wipe report MUST disclose retained exclusions. Neither
  option activates the sensitive tier (5A). This document makes no
  industry-practice claims; the framing stands on these disclosed
  tradeoffs only.

## 5. Normalization profile nfc-lf-v1 (FROZEN) + digest-input definition

- Require str input; strict UTF-8; CRLF and lone CR -> LF; NFC over
  the WHOLE text. PRESERVE case, accents, punctuation, tabs, spaces,
  zero-width characters. NO NFKC, NO casefold, NO whitespace
  collapsing, NO truncation before digest. Reject empty / non-string /
  invalid-surrogate / OVERSIZE input WITHOUT echoing content.
- MARKER_INPUT_MAX_BYTES = 4096 [peer clarification 2]: the frozen
  maximum UTF-8 BYTE count of the normalized marker input. This is
  NOT the 500-codepoint provenance budget - the two limits are
  independent.
- uid framing [peer clarification 2]: uid is a validated EXACT
  integer rendered as CANONICAL ASCII DECIMAL - no leading zeros, no
  coercion, no alternate spellings. ALL frame lengths are BYTE lengths.
- frame = b'crayon.memory-marker.v1\0'
          + u32be(uid byte length) + uid ASCII decimal bytes
          + u32be(profile byte length) + profile bytes
          + u64be(normalized UTF8 byte length) + normalizedUTF8
  digest = HMAC-SHA256(owner_marker_key, frame), hex lowercase.
- EXACT normalized-copy matching only; wider equivalence needs a
  separate proposal. This profile is SEPARATE from fact-key
  normalization.
- SYNTHETIC VECTORS (peer-supplied via i2i; independently
  compute-verified by our side with Python stdlib 2026-10-11 - exact
  matches; synthetic public values, NOT a production key):
    K = bytes 00..1f; uid = ASCII '76'; profile = 'nfc-lf-v1'
    Vector 1: input pair 'Cafe' + U+0301 COMBINING ACUTE + '\r\n' +
      'Tea' (decomposed, CRLF) and 'Caf' + U+00E9 PRECOMPOSED + '\n' +
      'Tea' (composed, LF) BOTH normalize to hex 436166c3a90a546561;
      expected HMAC
      7a82df294b89d711e393293622f2878d3241a21e32599222c69d4cc3683f6d57.
    Vector 2 (case sensitivity): 'caf' + U+00E9 + '\n' + 'Tea' ->
      bb25bf1063b468cfa81d4aa20804e42b7460bbc02e66b1357ec9f10118b40d9a.

## 6. facts.provenance_payload - opaque envelope column

```sql
ALTER TABLE facts ADD COLUMN provenance_payload BYTEA;
-- NO default; legacy NULL = UNKNOWN, NEVER backfill
```

- provenance exists ONLY as this ONE opaque envelope column. There is
  NO plaintext excerpt column and NO plaintext timestamp column
  ANYWHERE. Excerpt, CAPTURE timestamp (capture time, NOT updated_at),
  source ref, and truncated flag ALL live INSIDE the protected
  envelope payload, paired for new captures. fact_history.
  content_payload carries the same snapshot in its envelope.
- Bound: EXACTLY <=500 Unicode CODEPOINTS on the excerpt, enforced at
  the WRITER boundary on the authenticated PLAINTEXT BEFORE
  encryption - never a SQL CHECK on ciphertext (no content CHECKs in
  this DDL).
- The excerpt budget is NOT erasure evidence. Envelope format lands
  with PR-9; sensitive tier stays OFF (5A).

## Non-goals for PR-2

No writers routed, no supersede/erasure logic, no DDL application, no
real key creation/storage/provisioning, no encryption activation, no
sensitive-tier work, no /integrated edits, no backfill, no provider
calls, no existing-path changes, no crypto-erasure claims, no
industry-practice claims.

## Open items

- facts.key uniqueness / encrypted-key lookup: future open seam.
- Lock footprint inventory/ordering for memory_markers (membership
  settled): OPEN.
- owner_lifetime derivation detail: OPEN, non-executable here;
  implementation review.
- Encryption envelope implementation (PR-9): OPEN, non-executable
  here; implementation review.

## Local test note

ports/memory/tests/test_pr2_schema.py runs against a LOCAL disposable
sqlite design-model mirror (dialect adaptations: trim() for btrim(),
length-only digest CHECK plus python-layer hex validation, BLOB for
BYTEA) with paramstyle translation so the shipped SQL strings are what
execute, plus stdlib HMAC vector tests INLINE in the test file. No
hosted or external database is written anywhere; a hosted test-schema
run needs a separate approved authority.
