# PR-3: Capability + policy MODEL - contract (v4, final contract form)

Status: MODEL ONLY. Every construct in ports/memory/capability.py is
synthetic and non-authoritative; every production entry point is a
fail-closed stub. Basis: PR-3 proposal v3 + peer controlling addendum
1/4-4/4 (DRAFT-READY verdict, 2026-10-11), plan v1, seam prep v4.1,
contract v6, owner decisions record v1, PR-1/PR-2 merged
(main 329ba7364fdcc239e25e2e7ce5e7777cac4b4284).

Controlling paths (all NEW, no existing edits):
- ports/memory/capability.py
- docs/memory/pr3-capability.md  (this file)
- ports/memory/tests/test_pr3_capability.py

## 1. Model status

- NOTHING here is a security boundary. Frozen dataclasses, closed enums,
  and exact-type validators are PYTHON MISUSE GUARDS ONLY.
- ALL model dependencies (verifier, snapshot store, clock, decision
  envelope, proofs) are SYNTHETIC CALLER-PROVIDED FIXTURES. NONE is
  authoritative. A fabricated proof mints a capability because the
  verifier fixture says so; that demonstrates non-authority, never
  authentication.
- production_mint / production_export / production_load /
  production_authorize REFUSE UNCONDITIONALLY with
  ProductionStubRefused - irrespective of any dependency, proof, or
  capability supplied, INCLUDING a fully valid-looking synthetic
  capability - BEFORE any verifier/store/clock access. No permissive
  export alias, no duck-typed authenticated=True path. Future
  issuer/store/clock trust binding, transaction ownership, envelope
  persistence, key and consent gates remain OPEN and UNREACHABLE from
  any permissive runtime path.
- mint_model / recheck_model RAISE documented typed errors on refusal.
  Decision(allowed=True, code=MODEL_ALLOWED) is success with explicitly
  NO authority. Decision(allowed=False) is reserved for the later
  store-integrated layer; this model never produces it.
- Error messages are FIXED strings. No proof, session, info-ref,
  operation, or audience content is echoed; the only metadata attached
  to any error is CapRefused.resulting_count (whitelisted count).

## 2. Closed enums

- Scope: memory.read, memory.write, memory.erase, memory.extract,
  data.share, memory.group_write, memory.sensitive (exactly seven).
- OperationClass: READ, WRITE, ERASE, EXTRACT, SHARE, GROUP_WRITE,
  SENSITIVE. Base scope mapping is 1:1 by name
  (READ->memory.read, ..., SENSITIVE->memory.sensitive).
- Channel: {WEB, TELEGRAM} - peer-agreed membership for this model
  slice (2026-10-11). WHATSAPP is DEFERRED: it joins only via a later
  contract amendment after a surface audit. Surface: {PRIVATE, GROUP}.
  New members of any enum require amendment.
- Origin: {DIRECT, ANONYMOUS, SENDER_CHAT, FORWARDED_AMBIGUOUS}.
- ExtractionIntent: {NOT_APPLICABLE, EXPLICIT, AUTOMATIC}.
- GroupAction: {WRITE, EXTRACT}.
- ExtractionPolicy: {EXPLICIT_ONLY, GUARDED_AUTO};
  SharingPolicy: {EXPLICIT_PER_OP};
  GroupEligibility: {NEVER, DEFAULT_OFF_OPT_IN}.
- ModelStatus: {MODEL_ONLY}.
- ModelCode (closed): MISSING_IDENTITY, MALFORMED_INPUT, TIER_DISABLED,
  EXPIRED, REVOKED, STALE_POLICY, EPOCH, REQUEST_DRIFT, MISSING_SCOPE,
  MISSING_CONSENT, POLICY_REFUSED, CAPACITY, MODEL_ONLY, MODEL_ALLOWED.

New members of any enum require a contract amendment.

## 3. Immutable shapes (all frozen, exact-typed, synthetic)

- Identity(owner_uid:int, channel, surface, issuer_id:str,
  session_binding:str|None, audience_id:str|None, lifetime_id:int,
  origin). Session binding is a synthetic label only, never verified.
  Malformed identity fields raise AuthError at construction: the NORMAL
  CONSTRUCTOR rejects them, so an invalid identity can never yield a
  successful scope decision through the normal path; object tricks and
  subclasses remain OUTSIDE the model guarantee (no Python
  invincibility claim). A group-surface identity must carry its
  concrete source group ID in audience_id.
- ModelProof(identity, proof_label:str) - exact frozen synthetic
  fixture; fabricated fixtures are ONLY synthetic data.
- Request(operation_id:str, operation_class, required_scopes:
  frozenset[Scope], captured_epoch:int|None, requested_audience_id:
  str|None, permitted_information_ref:str|None, extraction_intent,
  group_action:GroupAction|None, sensitive:bool).
  - permitted_information_ref is a nonblank opaque SYNTHETIC
    immutable-reviewed-set reference: no plaintext, no private data, no
    digest, no security claim. Required for SHARE and for group-source
    consent; NEVER inferred. Production binding to actual immutable
    content remains OPEN/unreachable.
  - Final equality includes ALL fields: changed ref/intent/action/
    sensitivity/audience/operation id fails REQUEST_DRIFT.
  - Cross-field rules (construction-time): group_action set exactly for
    GROUP_WRITE; extraction-like classes (EXTRACT, GROUP_WRITE/EXTRACT)
    require EXPLICIT or AUTOMATIC intent, all others NOT_APPLICABLE;
    SHARE requires ref + audience.
- Grant(scope, granted_at:int, expires_at:int|None,
  per_op_required:bool). Duplicate scopes rejected at Snapshot level.
  The per_op_required bool is NOT consent evidence by itself.
- Policy(extraction, sharing, group_write) - three DISTINCT axes, no
  aliases. Config and envelope both carry typed policies; a missing
  envelope (typed None) causes the safest refusal (POLICY_REFUSED);
  malformed input is ModelInputError.
- DecisionEnvelope(owner_uid:int, policy, issued_at:int,
  expires_at:int|None, revision:int). No hardcoded owner or project
  identity.
- OperationConsent(owner_uid:int, operation_id:str, operation_class,
  scopes:frozenset[Scope], audience_id:str|None,
  permitted_information_ref:str, issued_at:int, expires_at:int,
  consent_generation:int). Covers each consent-required scope; exact
  operation/class/audience/ref; no enlargement.
- Snapshot(owner_uid:int, lifetime_id:int, current_epoch:int,
  grants_generation:int, grants:tuple, policy_revision:int,
  decision_revision:int, config:Policy|None,
  decision_envelope:DecisionEnvelope|None, consent_generation:int,
  consents:tuple). ONE coherent atomic owner snapshot including CURRENT
  consent generation/evidence - never an embedded stale generation.
  Identity/lifetime binding checked; mutation captured epoch checked
  against current epoch; READ/SHARE may explicitly omit epoch. NO
  resurrection-detection claim.
- Capability(status=MODEL_ONLY, identity, request,
  granted_scopes:frozenset[Scope], grants_generation:int,
  policy_revision:int, decision_revision:int, consent_generation:int,
  issued_at:int, expires_at:int). Frozen deep copies; no mutable
  mapping references; no serialization of authority.
- Decision(status:ModelStatus, allowed:bool, code:ModelCode).

Exact values: uid is an exact signed BIGINT (type(v) is int - bool
rejected); generations/revisions >= 0; epoch/lifetime >= 1; times >= 0;
exact bool fields; closed enums; nonblank safe opaque identifiers.
Windows must be valid: issued_at < expires_at; active means
issued_at <= now < expires_at (expires_at None only where explicitly
supported: grant and envelope expiries). Missing/malformed/not-yet-
valid windows reject.

## 4. Methods

- ModelOnlyVerifier.verify(model_proof) -> Identity.
- ModelOnlySnapshotStore.read(owner_uid) -> Snapshot: ONE atomic read.
  The SQLite design model (SqliteModelSnapshotStore) reads in ONE
  explicit local transaction (BEGIN IMMEDIATE ... COMMIT); the fake
  fixture returns one immutable snapshot object. Malformed rows are
  rejected with ModelInputError at the read boundary; sqlite
  operational failures PROPAGATE unwrapped. The sqlite model proves
  statement atomicity ONLY - nothing about Postgres locking,
  encryption, auth, or erasure.
- ModelOnlyClock.now() -> int (exact non-negative int).
- ModelOnlyEvaluator(verifier, snapshot_store, clock,
  model_ttl_seconds): mint_model(proof, request) -> Capability;
  recheck_model(cap, request) -> Decision. Consent/policy/grants come
  from ONE snapshot; no caller-authoritative arguments. Capability
  expiry <= issued_at + model_ttl_seconds AND the earliest relevant
  grant/envelope/consent expiry; an expiry <= now refuses (EXPIRED).
  Clock values and the TTL are exact signed BIGINTs (type(v) is int,
  bool rejected); a future-issued capability rechecks as MALFORMED; the
  expiry addition is overflow-checked BEFORE capability construction
  and refuses (MALFORMED).
- production_mint/export/load/authorize(*args, **kwargs) -> NoReturn:
  ProductionStubRefused BEFORE any verifier/store/clock access.

## 5. Scope derivation (no caller omission, no implication chains)

Necessary scopes are DERIVED from operation + surface and the request's
required_scopes must EQUAL the derived set; a narrower or wider caller
set is MALFORMED_INPUT:

- base: READ->memory.read, WRITE->memory.write, ERASE->memory.erase,
  EXTRACT->memory.extract, SHARE->data.share, SENSITIVE->memory.
  sensitive.
- GROUP_WRITE names its underlying action via GroupAction and requires
  BOTH memory.group_write AND the underlying scope (memory.write or
  memory.extract).
- Group-SOURCE private write/extract (surface=GROUP, class WRITE or
  EXTRACT) ADDS memory.group_write.
- data.share NEVER implies memory.group_write; no wildcard or prefix
  matching; no write=>share implication.

## 6. Three axes - separated IN THE EVALUATOR, not just in docs

A. EXTRACTION (per owner, independent): explicit_only still allows
   explicit authorized memory instructions; guarded_auto covers ONLY
   permitted automatic extraction. AUTOMATIC intent requires BOTH
   config AND envelope policy = GUARDED_AUTO, plus an independently
   active memory.extract grant. Ours: explicit_only (decision 1C).
   Theirs: guarded_auto (stands; NOT revoked).
B. SHARING (explicit-grant, per-operation END-USER consent every time;
   Pratham's direct "A" selection governs the SHARING axis only; no
   cross-owner or project-owner substitution). A standing grant ALONE
   never satisfies sharing consent. permitted_information_ref is a
   synthetic fixture, NOT actual consent proof. Sharing consent is NOT
   imposed on private read/write, and private guarded_auto is not
   accidentally disabled.
C. GROUP (never below default_off_opt_in; eligibility is not consent):
   - GROUP-SOURCE private write/extract requires memory.group_write
     PLUS the underlying scope PLUS a bound per-op consent. Eligibility
     NEVER is the owner's hard NO and applies SURFACE-WIDE: it refuses
     the GROUP_WRITE class (either underlying action) AND any
     group-source WRITE/EXTRACT, whenever config OR envelope policy
     says NEVER, however favorable the grants/consent (peer first-pass
     item 1, 2026-10-11). Under DEFAULT_OFF_OPT_IN the bound consent is
     still required.
   - GROUP SOURCE BINDING: group-source writes/extracts and the
     GROUP_WRITE class bind request AND consent (via exact consent
     matching) to the identity's concrete source group ID; a None or
     mismatched audience refuses MALFORMED. Group DISCLOSURE may name
     a distinct destination audience, explicitly bound by the SHARE
     request + consent, never inferred (peer first-pass item 2).
   - The GROUP_WRITE class additionally requires eligibility: NEVER (in
     config OR envelope policy) refuses POLICY_REFUSED. Ours: NEVER
     (decision 2A).
   - Anonymous / sender_chat / forwarded-ambiguous origins refuse
     SHARE, GROUP_WRITE, and group-source writes (POLICY_REFUSED).
   - Disclosure permission != memory-write permission (G9); a
     participant's utterance != owner consent.
Owner FIXTURES illustrate policies in tests; there are NO OUR/THEIR
lookup constants in the model.

## 7. Deterministic refusal precedence (ONE shared staging pipeline)

mint_model and recheck_model run the SAME staged pipeline
(_evaluate); identity verification (tier 0, MISSING_IDENTITY) happens
only in mint, before the store read. Production stubs refuse first,
unconditionally. Stages:

1. malformed input/snapshot (MALFORMED_INPUT: cross-owner snapshot,
   envelope owner/window shape, derived-scope mismatch, group binding,
   future-issued capability; never a successful decision)
2. sensitive anywhere (TIER_DISABLED)
3. expiry (EXPIRED: capability, envelope, required grants)
4. revoked / stale / epoch / request drift - deterministic inner order:
   lifetime binding (REVOKED, no resurrection claim), envelope revision
   (STALE_POLICY), grants generation (REVOKED), policy revision
   (STALE_POLICY), decision revision (STALE_POLICY), consent
   generation (REVOKED), full request equality (REQUEST_DRIFT), epoch
   (EPOCH)
5. missing scope / consent or policy refusal (MISSING_SCOPE,
   MISSING_CONSENT, POLICY_REFUSED). Within a single consent lookup the
   deterministic inner order is: expired match (EXPIRED) before stale
   generation (REVOKED) before missing (MISSING_CONSENT).
6. capacity (stage retained, UNTRIGGERED - see below)

Collision behavior is covered by dedicated collision tests (sensitive
vs revoked, malformed vs sensitive, sensitive vs expiry, expiry vs
stale/missing-scope, stale vs missing-scope, epoch vs missing-scope,
recheck expiry vs drift, recheck stale vs drift, policy vs
missing-scope). Invalid final requests always refuse with the
documented deterministic code.

CAPACITY STAGE IS RETAINED BUT NOT TRIGGERED BY THIS MODEL: the agreed
shapes carry no capacity count/limit source; inventing one is a shape
change requiring exact cross-side agreement. Capacity enforcement is
NOT implemented anywhere in the merged tree (PR-1 owner_counters
allocates monotone epochs only); the fixture shape is OPEN and
unattributed. EPOCH/CAS/ERASED/RESURRECTED semantics beyond the epoch check are
later-store conflict TYPES ONLY - no detection claim without
store/lifetime binding. Real DB failures propagate, distinct from every
refusal.

## 8. Per-operation evidence + replay

Per-op consent binds exact owner, operation id/class, scope(s),
audience, information set ref, issued/expiry, and consent generation.
Replay is decided explicitly: the SAME reviewed operation may be
rechecked/retried IDEMPOTENTLY - never a different operation and never
an enlarged payload or audience (REQUEST_DRIFT). One-use consumption /
idempotent commit store remains OPEN; because production stubs refuse,
NO production reuse claim exists.

## 9. Coverage map (tests -> risks)

- identity: verifier error / non-identity; malformed identity fields
  unrepresentable; fabricated proof mints (synthetic) but production
  refuses.
- request: malformed fields; scope omission/widening; group_action and
  extraction-intent cross-field rules; SHARE ref/audience required.
- snapshot: cross-owner; lifetime mismatch (no resurrection claim);
  duplicate grant scopes.
- sensitive: flag / class / scope, even with full grants.
- expiry/windows: expired envelope/grant/consent; not-yet-valid;
  malformed windows; earliest-expiry TTL; missing envelope/config
  safest refusal; envelope owner/revision mismatch.
- epoch: mutation requires matching epoch; READ omits.
- sharing: missing/wrong-id/wrong-class/wrong-scope/wrong-audience/
  wrong-ref/expired/revoked consent; standing grant insufficient;
  origin ambiguity; extraction axis does not contaminate sharing.
- extraction: explicit under explicit_only; automatic refused under
  explicit_only; automatic allowed under guarded_auto; config/envelope
  disagreement refuses.
- group: GROUP_WRITE ok/refused-under-NEVER/underlying-scope omission/
  missing consent; group-source allowed with consent/consent omission/
  eligibility-NEVER tolerated/scope omission/ambiguous origin; private
  write never needs sharing consent.
- per-op-required grants need consent.
- recheck: authority-free MODEL_ALLOWED decision; request drift per
  field (incl. sensitive drift); generation drifts (grants->REVOKED,
  policy->STALE_POLICY, decision->STALE_POLICY, consent->REVOKED,
  epoch->EPOCH); expired capability; grant expiry after mint;
  idempotent same-operation recheck.
- production stubs refuse before any dependency (exploding fixtures)
  and refuse a valid-looking synthetic capability; no permissive load.
- group NEVER matrix: 12 refusal variants (config/envelope/both x
  GROUP_WRITE-WRITE/GROUP_WRITE-EXTRACT/group-source WRITE/EXTRACT)
  plus allowed variants under DEFAULT_OFF_OPT_IN.
- group audience binding: group-surface identity requires source ID;
  None/mismatched request audience refuses; bound audience allowed;
  distinct disclosure destination explicitly bound.
- clock/TTL: future-issued capability; invalid TTL/clock values;
  expiry-addition overflow.
- typed collection guards: None/str/non-iterable collections refused
  typed, no raw TypeError; public errors ignore arbitrary prose across
  every error class.
- precedence collisions: 10 collision probes through the shared
  pipeline.
- CapRefused resulting_count validation (bool/negative/str rejected).
- immutability (frozen instances, frozenset scopes); error no-echo.
- sqlite design store: roundtrip + evaluator integration; absent
  snapshot; malformed scope/policy/bool rows rejected at read;
  operational failure propagates as sqlite3.Error.

## 10. Out of scope / OPEN items

- Production trust binding (verifier/store/clock/envelope persistence/
  key/consent gates): OPEN, unreachable.
- Capacity count/limit fixture shape for the evaluator: OPEN (exact
  agreement required); no capacity enforcement exists anywhere yet.
- One-use consumption / idempotent commit store: OPEN.
- Real immutable-content binding for permitted_information_ref: OPEN.
- CAS/ERASED/RESURRECTED integration detections: later-store types only.
- No source-grant mutations, ambient real stores, entry mint/writer/
  provider calls, token/session secrets, or serialized authority.
- No Neon/external DB writes or DDL; local disposable sqlite only.
