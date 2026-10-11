# PR-4: Submit-time capture-envelope MODEL - contract (v6 + controlling clarification, implemented form)

Status: MODEL ONLY, UNWIRED. Every construct in
ports/memory/envelope.py is synthetic and non-authoritative; every
production entry point is a fail-closed stub. Basis: PR-4 contract v6
+ peer DRAFT-READY verdict and controlling clarification (2026-10-11),
plus the peer first-pass review blockers 1-8, final-verdict
additions (scope order, current session/issuer, keyring uniqueness,
explicit key boundary), and r2 residuals (in-transaction owner/fence
validation, SHARE concrete destination, registry semantic coherence,
used-nonce commitment coherence), all folded into this document. Plan v1 (PR-4
entry), seam prep v4.1 sec 7/9, contract v6, owner decisions record
v1, PR-1/2/3 merged (main 1af1f78bcd97da0e7851311fb4551ad1f7df1c17).

Controlling paths (all NEW, no existing edits):
- ports/memory/envelope.py
- ports/memory/tests/test_pr4_envelope.py
- docs/memory/pr4-envelope.md  (this file)

## 1. Model status and honesty frame

- NOTHING here is a security boundary. Frozen dataclasses, closed
  enums, and exact-type validators are PYTHON MISUSE GUARDS ONLY;
  object tricks and subclasses remain outside the guarantee.
- ALL dependencies (keys, keyrings, boundary evidence, caller
  contexts, clocks, registry adapters, outcome/evidence refs) are
  SYNTHETIC PUBLIC FIXTURES. None is authoritative.
- Fernet with PUBLIC SYNTHETIC fixture keys models BYTE
  AUTHENTICATION / ENCRYPTION ONLY. It proves NOTHING about owner
  authentication, trusted identity or poll provenance, capability
  issuance, revocation, all-copy deletion, replay-safe effects, or
  production secrecy. Encryption authenticates BYTES, never the
  SENDER. A successful synthetic verify is MODEL-ONLY evidence and
  can NEVER bypass PR-3's production refusal.
- No real content is hashed, stored, or exported anywhere in this
  slice. The content fingerprint is a SYNTHETIC GUESSABLE FIXTURE -
  explicitly NOT privacy-preserving and NOT non-content. The future
  production commitment (separately keyed owner digest or protected
  envelope with independent purpose/key/lifecycle) stays UNREACHABLE
  and OPEN.
- Constants MAX_AGE=900, RESOLUTION_WINDOW=86400,
  PLAINTEXT_MAX_BYTES=16384, OUTER_TOKEN_MAX_BYTES=32768,
  required_scopes<=7, and all field limits are MODEL DEFAULTS, not
  security guarantees.
- production_seal / production_verify / production_dispatch /
  production_load / production_mint_poll_provenance REFUSE
  UNCONDITIONALLY (MODEL_ONLY) BEFORE touching any dependency - even
  a valid PR-3 ModelOnlyCapability.

## 2. Three separations

TRANSPORT vs INTENT; VERIFY vs CLAIM vs AUTHORIZE.

- verify_model decrypts + validates + compares ONLY: no registry
  handle, no mutation, no claim, no ACQUIRED, no execution
  permission.
- claim_model is the ONLY claim entry point and returns a synthetic
  receipt ONLY; no method executes a real effect.
- AUTHORIZE is PR-3's separate production surface and stays refused.

## 3. Payload schema (frozen, closed, strict)

ModelOnlyCapturedPayload = ModelOnlyRequestBinding (the IntentBinding
projection) + transport fields (nonce, format_version, key_id).

IntentBinding projection fields (EXACT; a changed projection under
the same operation refuses): uid (signed BIGINT >=0, PR-3 aligned),
lifetime (>=1), channel + surface (PR-3 enums verbatim; WHATSAPP out
of scope), issuer (WEB SessionRef = 64-hex session hash, or TELEGRAM
TelegramIssuer = bot_id positive int / chat_id signed BIGINT /
update_id nonnegative int - never an invented session hash),
operation_id (32 lower hex / 16 bytes), operation_class +
required_scopes (PR-3 enums verbatim, <=7 scopes; the constructor
normalizes to a UNIQUE SORTED canonical tuple - presentation order is
never intent, set semantics only; required_scopes must EQUAL the PR-3
derived set for the operation class, group action, and surface, with
omission and widening both refused; PR-3 group rules apply verbatim:
GROUP_WRITE requires a GROUP surface, and group-source
WRITE/EXTRACT/GROUP_WRITE bind a concrete GROUP-kind audience with a
non-None id, never inferred), audience
(ModelOnlyAudience: kind {PRIVATE, GROUP, EXTERNAL}, id nonblank safe
synthetic str <=128 UTF-8 bytes; id None ONLY where the operation
permits - MODEL default: GROUP requires an id; NOT an enum of actual
audiences), information_ref (opaque synthetic, <=128 bytes); SHARE requires a
CONCRETE destination (audience id non-None, never inferred),
content_fingerprint (64 lower hex), capture_epoch (>=1),
grant_generation / policy_revision / decision_revision /
consent_generation (>=0, exact non-bool), extraction_intent /
group_action / sensitive / origin (PR-3 fields and coherence rules
verbatim), capture_time (>=0), expiry (= capture_time + MAX_AGE,
immutable at submit, checked BIGINT addition, never refreshed or
restarted).

Transport fields: nonce (independently generated 32 lower hex / 16
bytes; injectable synthetic generator), format_version (==1), key_id
(<=64 safe printable ASCII, never arbitrary Unicode). Transport
fields are EXCLUDED from idempotency ONLY - they remain authenticated
and verified (outer routing must match the authenticated inner
payload; mismatch refuses TAMPERED).

All hex fields are EXACT lowercase [0-9a-f] of the declared length:
signs, 0x prefixes, whitespace, and separators refuse (MALFORMED).
All safe strings reject lone surrogates and other unencodable input
TYPED (MALFORMED) - no raw UnicodeEncodeError escapes.

Canonical serialization: canonical JSON, UTF-8, sorted keys, compact
separators; no floats, no NaN, no duplicate keys, no unknown fields;
exact bytes-to-hex mapping. Strict decoding refuses unknown/missing/
extra fields, enum drift, coercion (bool is not int; '1' is not 1),
and duplicate keys - all MALFORMED. CANONICAL-FORM CHECK: the
authenticated plaintext must BE byte-identical to the canonical
re-serialization of the strictly parsed payload - pretty-printed or
otherwise non-canonical authenticated JSON refuses (MALFORMED).

## 4. Caller context

ModelOnlyCallerContext = the ORIGINAL full binding (for equality)
PLUS a separate coherent CURRENT snapshot for staleness:
current_uid, current_lifetime, current_capture_epoch,
current_grant_generation, current_policy_revision,
current_decision_revision, current_consent_generation, AND a typed
current_issuer (SessionRef | TelegramIssuer) with an explicit
current_issuer_valid flag. Current times NEVER replace the original
capture/expiry. verify_model compares EVERY projection field for
equality, then every current-snapshot field for staleness; any
mismatch is STALE (uid recreation, wipe/recreate,
grant/policy/decision/consent revision bumps). A REVOKED
(current_issuer_valid=False) or REPLACED (issuer mismatch) session/
issuer is STALE even with uid/lifetime/revisions unchanged - the
original is compared, never refreshed. Envelope verification is
INDEPENDENT of PR-3 authorization; the future locked-store final
check (PR-5) is still required and is not claimed here.

## 5. seal/verify order of checks

seal_model(payload, key, clock): key window (not_before/decrypt_until)
+ original payload expiry (already-expired or future-capture payloads
refuse) -> canonicalize -> plaintext bound -> encrypt -> outer bound.

verify_model(envelope, keyring, current_context, clock): OUTER byte
bound -> explicit retained-key lookup (missing/not-yet-valid/retired
-> KEY_UNAVAILABLE; no trial-all, no fallback, no refresh) ->
authenticate/decrypt (InvalidToken -> TAMPERED; genuine crypto
operational failures propagate DISTINCTLY, unwrapped) -> INNER byte
bound -> strict decode/binding -> outer-vs-inner format/key match ->
time (future capture MALFORMED; now >= expiry EXPIRED, equality
refuses; signed BIGINT) -> binding equality + staleness.

claim_model rechecks time on the ORIGINAL capture/expiry after
verification (injected clock only; caller time is never
authoritative).

## 6. Registry (owned two-table atomic contract)

Tables (disposable local choice; NOT production DDL; PR-2's
memory_markers is NOT implicitly a runtime nonce registry):
- model_operations: PK (uid, lifetime, operation_id); private
  commitment; capture_time; expiry; state {CLAIMED, COMMITTED,
  UNKNOWN, EXPIRED}; owner_token; fence; outcome_ref;
  operation_marker_ref; resolution_deadline.
- model_used_nonces: PK (uid, lifetime, nonce); operation_id;
  commitment.
- model_markers: PK marker_ref; uid, lifetime, operation_id,
  commitment, owner_token, fence, outcome_ref (the FULL binding
  evidence tuple).

Every row read from the registry is validated at the boundary for
shape, exact types, closed state, AND semantic coherence: fence is
EXACTLY 1 (issued once, never reassigned), expiry ==
capture_time + MAX_AGE, resolution_deadline ==
expiry + RESOLUTION_WINDOW (checked BIGINT), COMMITTED IFF both
outcome_ref and operation_marker_ref are non-NULL (every other state
carries neither). Well-typed but incoherent rows refuse TYPED
(MALFORMED) exactly like corrupt ones. Raw sqlite operational
failures propagate DISTINCTLY, unwrapped.

Semantics:
- claim_model: ONE atomic insert/read + nonce-validation transaction
  across BOTH tables. The clock is read INSIDE the acquired
  transaction, immediately before the claim decision, and EVERY clock
  read is validated (exact non-bool int within signed BIGINT); the
  same rule holds in record_outcome_model before the
  state/effect-marker decision. Fresh claim -> ACQUIRED with fence=1
  and a fresh independently-random owner_token (16-byte hex). Fences
  are NEVER reassigned or reused; there is NO lease rollover.
- Same-operation reseal atomically REGISTERS the new nonce under the
  same operation - NEVER another claim. A nonce crossing to another
  operation REFUSES (REPLAY). A registered nonce whose stored
  operation OR commitment does not match the presented binding
  REFUSES (REPLAY) - a changed binding under a registered nonce is
  never accepted. CHANGED projection under the same operation REFUSES
  (REPLAY). Stored capture_time/expiry must match the immutable
  verified binding on every same-operation claim. Idempotency is keyed on the canonical
  PLAINTEXT commitment - ciphertext hashes are NEVER idempotency
  evidence (Fernet reseals change bytes).
- An existing CLAIMED row yields IN_PROGRESS - never a continuation,
  never a reclaim. A COMMITTED row yields the recorded outcome (no
  re-execution). UNKNOWN and EXPIRED rows yield their states.
- record_outcome_model: atomically checks owner_token AND fence AND
  current state AND expiry; the simulated effect's durable marker AND
  the outcome land in the SAME disposable transaction - NEVER
  execute-after-expiry-then-mark. A CLAIMED row observed after expiry
  becomes a blocked EXPIRED tombstone (the tombstone write COMMITS
  before the refusal). Duplicate/late records are compared against
  the FULL immutable outcome (outcome_ref AND operation_marker_ref):
  the SAME known outcome is idempotent; ANY change refuses (REPLAY);
  outcomes are never overwritten. Stale owner/fence refuses (REPLAY).
- mark_uncertain_model: LOCAL receipt ONLY - a timeout CANNOT assert
  a durable UNKNOWN write; this method writes NOTHING.
- resolve_unknown_model: resolves ONLY from ModelOnlyEffectEvidence
  whose OWN owner_token/fence match the claim's - validated BEFORE
  EVERY state branch, including the COMMITTED idempotent return, and
  with method owner/fence compared against the actual row INSIDE the
  atomic section (no pre-lock-only checks; no reassignment API;
  corruption fails closed) - AND whose scope + binding commitment
  match,
  AND a durable synthetic marker readback from the adapter that
  carries the FULL binding: scope (uid, lifetime, operation_id),
  canonical commitment, owner_token, fence, marker ref, and the FULL
  outcome ref. Global marker/outcome existence is NEVER sufficient; a
  caller CONFIRMED with an unrelated marker NEVER commits.
  INCONCLUSIVE or missing/mismatched marker stays UNKNOWN/BLOCKED -
  no blind retry, no fake not-written certainty, no retry grant. LATE
  confirmed-marker evidence CAN resolve UNKNOWN after expiry
  (accounting for an already-occurring effect) but NEVER grants fresh
  execution. EXPIRED tombstones never reopen. UNKNOWN never
  auto-expires to NEW.
- readback_model: typed ModelOnlyRegistryRecord ONLY
  (status/scope/state/expiry/resolution_deadline/outcome) - never
  commitment, raw token, owner secret, or fingerprint.
- resolution_deadline = expiry + RESOLUTION_WINDOW (checked BIGINT);
  unresolved past the deadline stays BLOCKED. No tombstone deletion
  in this slice: finite used-ID cleanup waits for ALL
  issuance/validity windows AND the future capture uniqueness policy.
- The registry ALONE is NOT an exactly-once proof; effect + marker
  require one shared transaction or an explicit recoverable protocol.

## 7. Typed records (exact shapes)

- ModelOnlyOutcome(outcome_ref, operation_marker_ref): nonblank
  synthetic opaque refs <=128 UTF-8 bytes; no arbitrary maps/data.
- ModelOnlyRegistryRecord(status=MODEL_ONLY, scope, state, expiry,
  resolution_deadline, outcome); outcome populated IFF COMMITTED.
- ModelOnlyClaimResult(status=MODEL_ONLY, claim_state {ACQUIRED,
  IN_PROGRESS, COMMITTED, UNKNOWN, EXPIRED}, scope, fence,
  owner_token, outcome): owner_token/fence populated ONLY on
  ACQUIRED; outcome ONLY on COMMITTED; IN_PROGRESS is non-executable.
- ModelOnlyUncertainReceipt(status=MODEL_ONLY, scope, fence,
  receipt_state=UNKNOWN): local observation, no durability claim.
- ModelOnlyEffectEvidence(status=MODEL_ONLY, scope, binding,
  owner_token (32 lower hex), fence (>=1), operation_marker_ref,
  effect_state {CONFIRMED_COMMITTED, INCONCLUSIVE}, outcome):
  CONFIRMED_COMMITTED requires a matching outcome; fixture-only,
  never runtime authority.
- ModelOnlyPollProvenance(status=MODEL_ONLY, payload, minted_at):
  payload is an EXACT TELEGRAM-channel payload; invariant
  capture_time <= minted_at < expiry. Field carriage ONLY - no
  trusted polling, authentication, or issuance claim. Minting
  requires SyntheticBoundaryEvidence (constructor-gated misuse guard;
  NO "test paths cannot mint" guarantee is claimed).

## 8. Keys and errors

SyntheticKey {algorithm: FERNET, format: 1, key_id, not_before,
decrypt_until, public fixture material}. Key lookup is EXPLICIT
retained-key only, and the keyring requires a UNIQUE key_id mapping -
duplicate ids refuse at construction. The decrypt_until boundary is
EXPLICIT and INCLUSIVE: the key is usable at now == decrypt_until and
UNAVAILABLE from decrypt_until + 1; there is no implicit grace
period. Format version and key_id are bound in the OUTER routing AND
the authenticated inner payload; mismatch refuses. The envelope key
is DISTINCT from the PR-2 marker key and the PR-9 content key - three
purposes, three keys; no reuse is implied. Decrypt keys are retained
only until max envelope expiry + RESOLUTION_WINDOW. No runtime env
loading (production_load refuses); the future real key route is OPEN,
vault-only, the implementing side's decision; NO credential action in
this PR. Fernet's actual metadata/size/age behavior is checked
against library docs during implementation review - no "opaque
everything" claim.

FERNET NOTES (review-verified): the Fernet token's timestamp is
PLAINTEXT-VISIBLE in the token; Fernet.decrypt is called WITHOUT a
ttl, so the token's own age is ignored - freshness derives ONLY from
the checked AUTHENTICATED capture_time/expiry inside the payload,
never from token metadata. InvalidToken covers malformed, signature,
and ttl failures; there is no trial-all fallback. Dependency
environment: the repo declares cryptography>=42,<47
(crayon-pratham/requirements.txt); the peer's reviewer reran this
slice's tests on disposable 46.0.7 (in-range) and our side ran them
on 50.0.1 - the model uses only the stable Fernet API; conformance is
asserted in the DECLARED environment, not on 50.0.1 alone.

Fixed refusal codes (closed taxonomy, safe, no echo): MALFORMED,
TAMPERED, STALE, EXPIRED, REPLAY, IN_PROGRESS, UNKNOWN,
KEY_UNAVAILABLE, MODEL_ONLY. Real operational failures propagate
DISTINCTLY; no token/key/content/fingerprint echo in any error, log,
export, or readback.

## 9. Fixture/version truth table

| format_version | key state        | decode class        | required outcome |
|----------------|------------------|---------------------|------------------|
| 1              | valid            | valid               | verified capture (MODEL_ONLY) |
| 1              | valid            | bit-flip/tag break  | TAMPERED |
| 1              | wrong material   | auth failure        | TAMPERED |
| 1              | not-yet-valid    | any                 | KEY_UNAVAILABLE |
| 1              | retired          | any                 | KEY_UNAVAILABLE |
| 1              | missing/unknown  | any                 | KEY_UNAVAILABLE |
| 1              | valid            | outer bound exceeded| MALFORMED (pre-decrypt) |
| 1              | valid            | inner bound exceeded| MALFORMED (post-decrypt, pre-parse) |
| 1              | valid            | unknown/missing/extra field, enum drift, coercion, dup keys, non-canonical | MALFORMED |
| !=1            | any              | any                 | MALFORMED (constructor/decode) |
| outer/inner format/key mismatch | valid | any | TAMPERED |
| 1              | valid            | future capture_time | MALFORMED |
| 1              | valid            | now >= expiry       | EXPIRED |
| 1              | valid            | binding/context mismatch | STALE |

## 10. Future ingress map (doc-only checklist, NOT a mount)

Any adoption of this model is a separate, explicitly agreed PR
requiring exact files + hunks, ingress, ALL writers, staged rollback,
real issuance keys, and a runtime authority amendment. Reference-only
anchors from the audited packets: web submit after the exact-Origin
gate and session resolution; web dispatch; the Telegram
bot-token-authenticated HTTPS polling entry after identity gates. No
claim is made about these paths at runtime; nothing there is edited.

## 11. Unchanged boundaries

Terminal write-path checks (EPOCH/REVOKED/RESURRECTED) remain PR-5;
write-path routing remains PR-6; fact-content encryption at rest
remains DECIDED YES under PR-9 (distinct mechanism, distinct key);
group writing stays governed by the recorded per-owner policy
unchanged; the sensitive tier stays OFF. No live capture, no live
poll, no unknown-commit closure, no actual-side-effect closure is
claimed.
