"""PR-3: capability + policy evaluation - MODEL ONLY, SYNTHETIC, NON-AUTHORITATIVE.

Contract: docs/memory/pr3-capability.md (v4 = v3 + peer controlling addendum
1/4-4/4, DRAFT-READY verdict 2026-10-11). Basis: plan v1 PR-3, seam prep v4.1,
contract v6, owner decisions record v1.

THIS MODULE IS A MODEL. Nothing here is a security boundary. Frozen
dataclasses, closed enums, and exact-type validators are PYTHON MISUSE GUARDS
ONLY. ALL dependencies (verifier, snapshot store, clock) are SYNTHETIC
CALLER-PROVIDED FIXTURES - NONE is authoritative. A fabricated proof mints a
capability because the verifier fixture says so; that proves NOTHING about
production authentication. The production entry points
(production_mint/production_export/production_load/production_authorize)
REFUSE UNCONDITIONALLY with ProductionStubRefused - irrespective of any
dependency, proof, or capability supplied, INCLUDING a fully valid-looking
synthetic capability - BEFORE any verifier/store/clock access. There is NO
permissive export alias and NO duck-typed authenticated=True path. Future
issuer/store/clock trust binding, transaction ownership, envelope
persistence, key and consent gates remain OPEN and UNREACHABLE from any
permissive runtime path.

Refusal semantics: mint_model/recheck_model RAISE documented typed errors on
refusal. Decision(allowed=True, code=MODEL_ALLOWED) is returned on success
and carries explicitly NO authority; Decision(allowed=False) is reserved for
the later store-integrated layer and is never produced by this model.

CAPACITY: the precedence chain retains a final capacity stage and CapRefused
carries a validated exact resulting_count, but NO capacity count/limit source
shape exists in the agreed contracts - capacity evaluation stays at the PR-1
owner_counters layer and the model fixture shape is an OPEN item needing
exact cross-side agreement. No model path in this file triggers capacity.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from enum import Enum
from typing import FrozenSet, Optional, Protocol, Tuple

BIGINT_MAX = 9223372036854775807  # signed BIGINT ceiling; uids are exact ints


# ---------------------------------------------------------------------------
# Closed enums (new members require a contract amendment)
# ---------------------------------------------------------------------------

class Scope(Enum):
    MEMORY_READ = "memory.read"
    MEMORY_WRITE = "memory.write"
    MEMORY_ERASE = "memory.erase"
    MEMORY_EXTRACT = "memory.extract"
    DATA_SHARE = "data.share"
    MEMORY_GROUP_WRITE = "memory.group_write"
    MEMORY_SENSITIVE = "memory.sensitive"


class OperationClass(Enum):
    READ = "READ"
    WRITE = "WRITE"
    ERASE = "ERASE"
    EXTRACT = "EXTRACT"
    SHARE = "SHARE"
    GROUP_WRITE = "GROUP_WRITE"
    SENSITIVE = "SENSITIVE"


class Channel(Enum):
    # Chosen initial membership for the synthetic model (exact-head review
    # covers this constant set); new members require amendment.
    WHATSAPP = "whatsapp"
    TELEGRAM = "telegram"


class Surface(Enum):
    PRIVATE = "private"
    GROUP = "group"


class Origin(Enum):
    DIRECT = "DIRECT"
    ANONYMOUS = "ANONYMOUS"
    SENDER_CHAT = "SENDER_CHAT"
    FORWARDED_AMBIGUOUS = "FORWARDED_AMBIGUOUS"


class ExtractionIntent(Enum):
    NOT_APPLICABLE = "NOT_APPLICABLE"
    EXPLICIT = "EXPLICIT"
    AUTOMATIC = "AUTOMATIC"


class GroupAction(Enum):
    WRITE = "WRITE"
    EXTRACT = "EXTRACT"


class ExtractionPolicy(Enum):
    EXPLICIT_ONLY = "EXPLICIT_ONLY"
    GUARDED_AUTO = "GUARDED_AUTO"


class SharingPolicy(Enum):
    EXPLICIT_PER_OP = "EXPLICIT_PER_OP"


class GroupEligibility(Enum):
    NEVER = "NEVER"
    DEFAULT_OFF_OPT_IN = "DEFAULT_OFF_OPT_IN"


class ModelStatus(Enum):
    MODEL_ONLY = "MODEL_ONLY"


class ModelCode(Enum):
    MISSING_IDENTITY = "MISSING_IDENTITY"
    MALFORMED_INPUT = "MALFORMED_INPUT"
    TIER_DISABLED = "TIER_DISABLED"
    EXPIRED = "EXPIRED"
    REVOKED = "REVOKED"
    STALE_POLICY = "STALE_POLICY"
    EPOCH = "EPOCH"
    REQUEST_DRIFT = "REQUEST_DRIFT"
    MISSING_SCOPE = "MISSING_SCOPE"
    MISSING_CONSENT = "MISSING_CONSENT"
    POLICY_REFUSED = "POLICY_REFUSED"
    CAPACITY = "CAPACITY"
    MODEL_ONLY = "MODEL_ONLY"
    MODEL_ALLOWED = "MODEL_ALLOWED"


# ---------------------------------------------------------------------------
# Typed errors - fixed safe messages, no fixture/proof/session/info-ref echo
# ---------------------------------------------------------------------------

class CapabilityModelError(Exception):
    """Base for all typed model refusals. Messages are FIXED and carry no
    request/proof/session/info-ref content; the only metadata ever attached
    is CapRefused.resulting_count (a whitelisted count)."""

    code: ModelCode = ModelCode.MALFORMED_INPUT
    _MSG = "capability model refusal"

    def __init__(self, message: Optional[str] = None) -> None:
        super().__init__(message if message is not None else self._MSG)


class AuthError(CapabilityModelError):
    code = ModelCode.MISSING_IDENTITY
    _MSG = "missing or invalid identity"


class ModelInputError(CapabilityModelError):
    code = ModelCode.MALFORMED_INPUT
    _MSG = "malformed model input"


class SensitiveRefused(CapabilityModelError):
    code = ModelCode.TIER_DISABLED
    _MSG = "sensitive tier is disabled"


class ExpiredRefused(CapabilityModelError):
    code = ModelCode.EXPIRED
    _MSG = "expired"


class ConflictError(CapabilityModelError):
    """Carries exactly one of REVOKED / STALE_POLICY / EPOCH / REQUEST_DRIFT.
    Later-store conflict types CAS/ERASED/RESURRECTED remain defined in the
    contract as TYPES ONLY; this model makes no detection claim for them."""

    _ALLOWED = frozenset({
        ModelCode.REVOKED, ModelCode.STALE_POLICY,
        ModelCode.EPOCH, ModelCode.REQUEST_DRIFT,
    })

    def __init__(self, code: ModelCode) -> None:
        if code not in self._ALLOWED:
            raise ModelInputError("invalid conflict code")
        self.code = code
        super().__init__("conflict: %s" % code.value)


class ScopeRefused(CapabilityModelError):
    code = ModelCode.MISSING_SCOPE
    _MSG = "missing required scope"


class ConsentRefused(CapabilityModelError):
    code = ModelCode.MISSING_CONSENT
    _MSG = "missing per-operation consent"


class PolicyRefused(CapabilityModelError):
    code = ModelCode.POLICY_REFUSED
    _MSG = "policy refusal"


class CapRefused(CapabilityModelError):
    code = ModelCode.CAPACITY
    _MSG = "capacity refusal"

    def __init__(self, resulting_count: int) -> None:
        if type(resulting_count) is not int or resulting_count < 0:
            raise ModelInputError("resulting_count must be an exact int >= 0")
        self.resulting_count = resulting_count
        super().__init__(self._MSG)


class ProductionStubRefused(CapabilityModelError):
    code = ModelCode.MODEL_ONLY
    _MSG = "production path is a fail-closed stub (MODEL_ONLY)"


# ---------------------------------------------------------------------------
# Exact-type validation helpers (misuse guards, NOT security boundaries)
# ---------------------------------------------------------------------------

def _req_int(value, minimum: int, maximum: int = BIGINT_MAX) -> None:
    # type(v) is int rejects bool (type(True) is bool) and floats/strings.
    if type(value) is not int or value < minimum or value > maximum:
        raise ModelInputError()


def _req_nonblank(value) -> None:
    if type(value) is not str or not value.strip():
        raise ModelInputError()


def _req_opt_nonblank(value) -> None:
    if value is not None:
        _req_nonblank(value)


def _req_enum(value, enum_cls) -> None:
    if not isinstance(value, enum_cls):
        raise ModelInputError()


def _req_bool(value) -> None:
    if type(value) is not bool:
        raise ModelInputError()


# ---------------------------------------------------------------------------
# Immutable shapes (frozen dataclasses; construction-time validation makes
# malformed shapes UNREPRESENTABLE - the shape classes themselves are the
# real validators exercised by tests, never self-raised exceptions)
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class Identity:
    owner_uid: int
    channel: Channel
    surface: Surface
    issuer_id: str
    session_binding: Optional[str]   # synthetic label only, never verified
    audience_id: Optional[str]
    lifetime_id: int
    origin: Origin

    def __post_init__(self) -> None:
        try:
            _req_int(self.owner_uid, 0)
            _req_enum(self.channel, Channel)
            _req_enum(self.surface, Surface)
            _req_nonblank(self.issuer_id)
            _req_opt_nonblank(self.session_binding)
            _req_opt_nonblank(self.audience_id)
            _req_int(self.lifetime_id, 1)
            _req_enum(self.origin, Origin)
        except ModelInputError:
            # an invalid identity must never yield a successful scope decision
            raise AuthError()


@dataclass(frozen=True)
class ModelProof:
    """Exact frozen synthetic fixture. Fabricated fixtures are ONLY synthetic
    data - they are NEVER authentication and no production path accepts them."""
    identity: Identity
    proof_label: str

    def __post_init__(self) -> None:
        if not isinstance(self.identity, Identity):
            raise ModelInputError()
        _req_nonblank(self.proof_label)


@dataclass(frozen=True)
class Request:
    operation_id: str
    operation_class: OperationClass
    required_scopes: FrozenSet[Scope]
    captured_epoch: Optional[int]
    requested_audience_id: Optional[str]
    permitted_information_ref: Optional[str]
    extraction_intent: ExtractionIntent
    group_action: Optional[GroupAction]
    sensitive: bool

    def __post_init__(self) -> None:
        _req_nonblank(self.operation_id)
        _req_enum(self.operation_class, OperationClass)
        object.__setattr__(self, "required_scopes", frozenset(self.required_scopes))
        for scope in self.required_scopes:
            _req_enum(scope, Scope)
        if not self.required_scopes:
            raise ModelInputError()
        if self.captured_epoch is not None:
            _req_int(self.captured_epoch, 1)
        _req_opt_nonblank(self.requested_audience_id)
        _req_opt_nonblank(self.permitted_information_ref)
        _req_enum(self.extraction_intent, ExtractionIntent)
        if self.group_action is not None:
            _req_enum(self.group_action, GroupAction)
        _req_bool(self.sensitive)
        cls = self.operation_class
        if cls is OperationClass.GROUP_WRITE:
            if self.group_action is None:
                raise ModelInputError()
        elif self.group_action is not None:
            raise ModelInputError()
        extraction_like = cls is OperationClass.EXTRACT or (
            cls is OperationClass.GROUP_WRITE
            and self.group_action is GroupAction.EXTRACT
        )
        if extraction_like:
            if self.extraction_intent not in (
                ExtractionIntent.EXPLICIT, ExtractionIntent.AUTOMATIC,
            ):
                raise ModelInputError()
        elif self.extraction_intent is not ExtractionIntent.NOT_APPLICABLE:
            raise ModelInputError()
        if cls is OperationClass.SHARE:
            # information ref and audience are required for SHARE and are
            # never inferred; binding to actual immutable content stays OPEN
            if self.permitted_information_ref is None:
                raise ModelInputError()
            if self.requested_audience_id is None:
                raise ModelInputError()


@dataclass(frozen=True)
class Grant:
    scope: Scope
    granted_at: int
    expires_at: Optional[int]
    per_op_required: bool  # PR-1 bool alone is NOT consent evidence

    def __post_init__(self) -> None:
        _req_enum(self.scope, Scope)
        _req_int(self.granted_at, 0)
        if self.expires_at is not None:
            _req_int(self.expires_at, 0)
            if self.expires_at <= self.granted_at:
                raise ModelInputError()
        _req_bool(self.per_op_required)

    def is_active(self, now: int) -> bool:
        return self.granted_at <= now and (
            self.expires_at is None or now < self.expires_at
        )

    def is_expired(self, now: int) -> bool:
        return self.expires_at is not None and now >= self.expires_at


@dataclass(frozen=True)
class Policy:
    extraction: ExtractionPolicy
    sharing: SharingPolicy
    group_write: GroupEligibility

    def __post_init__(self) -> None:
        _req_enum(self.extraction, ExtractionPolicy)
        _req_enum(self.sharing, SharingPolicy)
        _req_enum(self.group_write, GroupEligibility)


@dataclass(frozen=True)
class DecisionEnvelope:
    owner_uid: int
    policy: Policy
    issued_at: int
    expires_at: Optional[int]
    revision: int

    def __post_init__(self) -> None:
        _req_int(self.owner_uid, 0)
        if not isinstance(self.policy, Policy):
            raise ModelInputError()
        _req_int(self.issued_at, 0)
        if self.expires_at is not None:
            _req_int(self.expires_at, 0)
            if self.expires_at <= self.issued_at:
                raise ModelInputError()
        _req_int(self.revision, 0)


@dataclass(frozen=True)
class OperationConsent:
    owner_uid: int
    operation_id: str
    operation_class: OperationClass
    scopes: FrozenSet[Scope]
    audience_id: Optional[str]
    permitted_information_ref: str  # synthetic fixture, NOT consent proof
    issued_at: int
    expires_at: int
    consent_generation: int

    def __post_init__(self) -> None:
        _req_int(self.owner_uid, 0)
        _req_nonblank(self.operation_id)
        _req_enum(self.operation_class, OperationClass)
        object.__setattr__(self, "scopes", frozenset(self.scopes))
        if not self.scopes:
            raise ModelInputError()
        for scope in self.scopes:
            _req_enum(scope, Scope)
        _req_opt_nonblank(self.audience_id)
        _req_nonblank(self.permitted_information_ref)
        _req_int(self.issued_at, 0)
        _req_int(self.expires_at, 0)
        if self.expires_at <= self.issued_at:
            raise ModelInputError()
        _req_int(self.consent_generation, 0)


@dataclass(frozen=True)
class Snapshot:
    """ONE coherent atomic owner snapshot: grants + generations + policy +
    decision envelope + CURRENT consent generation/evidence read together."""
    owner_uid: int
    lifetime_id: int
    current_epoch: int
    grants_generation: int
    grants: Tuple[Grant, ...]
    policy_revision: int
    decision_revision: int
    config: Optional[Policy]
    decision_envelope: Optional[DecisionEnvelope]
    consent_generation: int
    consents: Tuple[OperationConsent, ...]

    def __post_init__(self) -> None:
        _req_int(self.owner_uid, 0)
        _req_int(self.lifetime_id, 1)
        _req_int(self.current_epoch, 1)
        _req_int(self.grants_generation, 0)
        object.__setattr__(self, "grants", tuple(self.grants))
        seen = set()
        for grant in self.grants:
            if not isinstance(grant, Grant):
                raise ModelInputError()
            if grant.scope in seen:
                raise ModelInputError()  # duplicate grant scopes rejected
            seen.add(grant.scope)
        _req_int(self.policy_revision, 0)
        _req_int(self.decision_revision, 0)
        if self.config is not None and not isinstance(self.config, Policy):
            raise ModelInputError()
        if (self.decision_envelope is not None
                and not isinstance(self.decision_envelope, DecisionEnvelope)):
            raise ModelInputError()
        _req_int(self.consent_generation, 0)
        object.__setattr__(self, "consents", tuple(self.consents))
        for consent in self.consents:
            if not isinstance(consent, OperationConsent):
                raise ModelInputError()


@dataclass(frozen=True)
class Capability:
    """Synthetic capability: MODEL_ONLY status, no authority, no serialization
    of authority, no production load path."""
    status: ModelStatus
    identity: Identity
    request: Request
    granted_scopes: FrozenSet[Scope]
    grants_generation: int
    policy_revision: int
    decision_revision: int
    consent_generation: int
    issued_at: int
    expires_at: int

    def __post_init__(self) -> None:
        if self.status is not ModelStatus.MODEL_ONLY:
            raise ModelInputError()
        if not isinstance(self.identity, Identity):
            raise ModelInputError()
        if not isinstance(self.request, Request):
            raise ModelInputError()
        object.__setattr__(self, "granted_scopes", frozenset(self.granted_scopes))
        for scope in self.granted_scopes:
            _req_enum(scope, Scope)
        _req_int(self.grants_generation, 0)
        _req_int(self.policy_revision, 0)
        _req_int(self.decision_revision, 0)
        _req_int(self.consent_generation, 0)
        _req_int(self.issued_at, 0)
        _req_int(self.expires_at, 0)
        if self.expires_at <= self.issued_at:
            raise ModelInputError()


@dataclass(frozen=True)
class Decision:
    """MODEL_ALLOWED is success with explicitly NO authority."""
    status: ModelStatus
    allowed: bool
    code: ModelCode

    def __post_init__(self) -> None:
        if self.status is not ModelStatus.MODEL_ONLY:
            raise ModelInputError()
        _req_bool(self.allowed)
        _req_enum(self.code, ModelCode)


# ---------------------------------------------------------------------------
# Dependency protocols (ALL synthetic caller-provided fixtures)
# ---------------------------------------------------------------------------

class ModelOnlyVerifier(Protocol):
    def verify(self, model_proof: ModelProof) -> Identity: ...


class ModelOnlySnapshotStore(Protocol):
    def read(self, owner_uid: int) -> Snapshot: ...  # ONE atomic read


class ModelOnlyClock(Protocol):
    def now(self) -> int: ...


# ---------------------------------------------------------------------------
# Scope derivation (derive/validate from operation+surface; caller omission
# is rejected - request.required_scopes must EQUAL the derived set)
# ---------------------------------------------------------------------------

_BASE_SCOPES = {
    OperationClass.READ: frozenset({Scope.MEMORY_READ}),
    OperationClass.WRITE: frozenset({Scope.MEMORY_WRITE}),
    OperationClass.ERASE: frozenset({Scope.MEMORY_ERASE}),
    OperationClass.EXTRACT: frozenset({Scope.MEMORY_EXTRACT}),
    OperationClass.SHARE: frozenset({Scope.DATA_SHARE}),
    OperationClass.SENSITIVE: frozenset({Scope.MEMORY_SENSITIVE}),
}

# classes that mutate stored state and therefore demand a captured epoch
_MUTATION_CLASSES = frozenset({
    OperationClass.WRITE, OperationClass.ERASE,
    OperationClass.EXTRACT, OperationClass.GROUP_WRITE,
})

# origins too ambiguous to support sharing or group writes
_AMBIGUOUS_ORIGINS = frozenset({
    Origin.ANONYMOUS, Origin.SENDER_CHAT, Origin.FORWARDED_AMBIGUOUS,
})


def derive_required_scopes(request: Request, identity: Identity) -> FrozenSet[Scope]:
    cls = request.operation_class
    if cls is OperationClass.GROUP_WRITE:
        scopes = {Scope.MEMORY_GROUP_WRITE}
        if request.group_action is GroupAction.WRITE:
            scopes.add(Scope.MEMORY_WRITE)
        else:
            scopes.add(Scope.MEMORY_EXTRACT)
    else:
        scopes = set(_BASE_SCOPES[cls])
    if identity.surface is Surface.GROUP and cls in (
        OperationClass.WRITE, OperationClass.EXTRACT,
    ):
        # group-SOURCE private write/extract additionally needs group_write;
        # data.share NEVER implies memory.group_write
        scopes.add(Scope.MEMORY_GROUP_WRITE)
    return frozenset(scopes)


# ---------------------------------------------------------------------------
# Evaluator
# ---------------------------------------------------------------------------

class ModelOnlyEvaluator:
    def __init__(self, verifier: ModelOnlyVerifier,
                 snapshot_store: ModelOnlySnapshotStore,
                 clock: ModelOnlyClock, model_ttl_seconds: int) -> None:
        if not callable(getattr(verifier, "verify", None)):
            raise ModelInputError()
        if not callable(getattr(snapshot_store, "read", None)):
            raise ModelInputError()
        if not callable(getattr(clock, "now", None)):
            raise ModelInputError()
        if type(model_ttl_seconds) is not int or model_ttl_seconds < 1:
            raise ModelInputError()
        self._verifier = verifier
        self._store = snapshot_store
        self._clock = clock
        self._ttl = model_ttl_seconds

    # -- public API ---------------------------------------------------------

    def mint_model(self, proof: ModelProof, request: Request) -> Capability:
        if not isinstance(proof, ModelProof):
            raise AuthError()
        identity = self._verifier.verify(proof)
        if not isinstance(identity, Identity):
            raise AuthError()
        if not isinstance(request, Request):
            raise ModelInputError()
        snapshot = self._store.read(identity.owner_uid)  # ONE atomic read
        now = self._now()
        derived, expiry = self._evaluate(snapshot, identity, request, now)
        return Capability(
            status=ModelStatus.MODEL_ONLY,
            identity=identity,
            request=request,
            granted_scopes=derived,
            grants_generation=snapshot.grants_generation,
            policy_revision=snapshot.policy_revision,
            decision_revision=snapshot.decision_revision,
            consent_generation=snapshot.consent_generation,
            issued_at=now,
            expires_at=expiry,
        )

    def recheck_model(self, cap: Capability, request: Request) -> Decision:
        if not isinstance(cap, Capability):
            raise ModelInputError()
        if not isinstance(request, Request):
            raise ModelInputError()
        # Final request must EXACTLY match the capability's immutable
        # operation/class/scopes/epoch/audience/ref/intent/action/sensitivity;
        # any drift refuses. Same reviewed operation: idempotent recheck only.
        if request != cap.request:
            raise ConflictError(ModelCode.REQUEST_DRIFT)
        now = self._now()
        if now >= cap.expires_at:
            raise ExpiredRefused()
        snapshot = self._store.read(cap.identity.owner_uid)  # ONE atomic read
        self._check_snapshot_binding(snapshot, cap.identity)
        if snapshot.grants_generation != cap.grants_generation:
            raise ConflictError(ModelCode.REVOKED)
        if snapshot.policy_revision != cap.policy_revision:
            raise ConflictError(ModelCode.STALE_POLICY)
        if snapshot.decision_revision != cap.decision_revision:
            raise ConflictError(ModelCode.STALE_POLICY)
        if snapshot.consent_generation != cap.consent_generation:
            raise ConflictError(ModelCode.REVOKED)
        self._evaluate(snapshot, cap.identity, request, now)
        return Decision(
            status=ModelStatus.MODEL_ONLY, allowed=True,
            code=ModelCode.MODEL_ALLOWED,
        )

    # -- internals ----------------------------------------------------------

    def _now(self) -> int:
        now = self._clock.now()
        if type(now) is not int or now < 0:
            raise ModelInputError()
        return now

    def _check_snapshot_binding(self, snapshot: Snapshot,
                                identity: Identity) -> None:
        if not isinstance(snapshot, Snapshot):
            raise ModelInputError()
        if snapshot.owner_uid != identity.owner_uid:
            raise ModelInputError()  # cross-owner snapshot is malformed here
        if snapshot.lifetime_id != identity.lifetime_id:
            # lifetime binding mismatch: refuse without any resurrection claim
            raise ConflictError(ModelCode.REVOKED)

    def _evaluate(self, snapshot: Snapshot, identity: Identity,
                  request: Request, now: int) -> Tuple[FrozenSet[Scope], int]:
        """Deterministic refusal precedence (contract sec 7 + addendum 4/4):
        identity (caller) -> malformed input/snapshot -> sensitive -> expiry
        -> revoked/stale/epoch/drift -> missing scope/consent/policy ->
        capacity (stage retained; NO model trigger - OPEN fixture shape)."""
        self._check_snapshot_binding(snapshot, identity)

        # sensitive: ANY sensitive operation/scope/flag is always refused
        if (request.sensitive
                or request.operation_class is OperationClass.SENSITIVE
                or Scope.MEMORY_SENSITIVE in request.required_scopes):
            raise SensitiveRefused()

        derived = derive_required_scopes(request, identity)
        if request.required_scopes != derived:
            raise ModelInputError()  # caller omission/widening rejected

        envelope = snapshot.decision_envelope
        if envelope is not None:
            if envelope.owner_uid != identity.owner_uid:
                raise ModelInputError()
            if now < envelope.issued_at:
                raise ModelInputError()  # not-yet-valid window
            # expiry precedes staleness
            if envelope.expires_at is not None and now >= envelope.expires_at:
                raise ExpiredRefused()
            if envelope.revision != snapshot.decision_revision:
                raise ConflictError(ModelCode.STALE_POLICY)

        # expiry of required grants precedes any missing-scope determination
        for scope in derived:
            matching = [g for g in snapshot.grants if g.scope is scope]
            if matching and all(g.is_expired(now) for g in matching):
                raise ExpiredRefused()

        # epoch: mutations require a captured epoch matching current; READ and
        # SHARE may explicitly omit it; a provided epoch must always match
        if request.captured_epoch is None:
            if request.operation_class in _MUTATION_CLASSES:
                raise ConflictError(ModelCode.EPOCH)
        elif request.captured_epoch != snapshot.current_epoch:
            raise ConflictError(ModelCode.EPOCH)

        # missing envelope/config creates no consent: safest refusal
        if envelope is None or snapshot.config is None:
            raise PolicyRefused()

        # policy axis A: extraction (checked independently of grants)
        if request.extraction_intent is ExtractionIntent.AUTOMATIC:
            if (snapshot.config.extraction is not ExtractionPolicy.GUARDED_AUTO
                    or envelope.policy.extraction
                    is not ExtractionPolicy.GUARDED_AUTO):
                raise PolicyRefused()
        # EXPLICIT intent is allowed under both extraction policies;
        # sharing is ALWAYS per-op regardless of the extraction axis.

        # policy axis C: group. Group eligibility NEVER refuses the
        # GROUP_WRITE class; group-SOURCE private write/extract is governed
        # by consent+scopes below and is never refused by eligibility.
        if request.operation_class is OperationClass.GROUP_WRITE:
            if (snapshot.config.group_write is GroupEligibility.NEVER
                    or envelope.policy.group_write is GroupEligibility.NEVER):
                raise PolicyRefused()
        # origin ambiguity refuses sharing and group writes
        if request.operation_class in (
            OperationClass.SHARE, OperationClass.GROUP_WRITE,
        ) and identity.origin in _AMBIGUOUS_ORIGINS:
            raise PolicyRefused()
        if (identity.surface is Surface.GROUP
                and request.operation_class in (
                    OperationClass.WRITE, OperationClass.EXTRACT,
                ) and identity.origin in _AMBIGUOUS_ORIGINS):
            raise PolicyRefused()

        # scope stage: active grant required for every derived scope
        active_grants = []
        for scope in derived:
            grant = next(
                (g for g in snapshot.grants
                 if g.scope is scope and g.is_active(now)),
                None,
            )
            if grant is None:
                raise ScopeRefused()
            active_grants.append(grant)

        # consent stage: data.share and memory.group_write ALWAYS need a
        # bound per-operation consent; a standing grant alone never suffices;
        # per_op_required grants additionally need consent for their scope.
        # Group-source consent requires the information ref (never inferred).
        consent_required = set()
        if Scope.DATA_SHARE in derived:
            consent_required.add(Scope.DATA_SHARE)
        if Scope.MEMORY_GROUP_WRITE in derived:
            consent_required.add(Scope.MEMORY_GROUP_WRITE)
            if request.permitted_information_ref is None:
                raise ModelInputError()
        for grant in active_grants:
            if grant.per_op_required:
                consent_required.add(grant.scope)

        matched_consents = []
        for scope in sorted(consent_required, key=lambda s: s.value):
            matched_consents.append(
                self._require_consent(snapshot, identity, request, scope, now)
            )

        # capability expiry: <= issued+TTL AND earliest relevant expiry
        expiry = now + self._ttl
        for grant in active_grants:
            if grant.expires_at is not None:
                expiry = min(expiry, grant.expires_at)
        if envelope.expires_at is not None:
            expiry = min(expiry, envelope.expires_at)
        for consent in matched_consents:
            expiry = min(expiry, consent.expires_at)
        if expiry <= now:
            raise ExpiredRefused()
        return derived, expiry

    def _require_consent(self, snapshot: Snapshot, identity: Identity,
                         request: Request, scope: Scope,
                         now: int) -> OperationConsent:
        candidates = [
            c for c in snapshot.consents
            if c.owner_uid == identity.owner_uid
            and c.operation_id == request.operation_id
            and c.operation_class is request.operation_class
            and scope in c.scopes
            and c.audience_id == request.requested_audience_id
            and c.permitted_information_ref
            == request.permitted_information_ref
        ]
        if not candidates:
            raise ConsentRefused()  # missing consent creates no consent
        saw_expired = False
        saw_revoked = False
        for consent in candidates:
            if now < consent.issued_at:
                continue  # not yet valid: treated as missing
            if now >= consent.expires_at:
                saw_expired = True
                continue
            if consent.consent_generation != snapshot.consent_generation:
                saw_revoked = True  # consent evidence is not current
                continue
            return consent
        if saw_expired:
            raise ExpiredRefused()  # expiry precedes revocation
        if saw_revoked:
            raise ConflictError(ModelCode.REVOKED)
        raise ConsentRefused()


# ---------------------------------------------------------------------------
# SQLite DESIGN-MODEL snapshot store (local disposable ONLY)
# ---------------------------------------------------------------------------

_SCHEMA = """
CREATE TABLE IF NOT EXISTS model_snapshot (
    owner_uid INTEGER PRIMARY KEY,
    lifetime_id INTEGER NOT NULL,
    current_epoch INTEGER NOT NULL,
    grants_generation INTEGER NOT NULL,
    policy_revision INTEGER NOT NULL,
    decision_revision INTEGER NOT NULL,
    consent_generation INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS model_grants (
    owner_uid INTEGER NOT NULL,
    scope TEXT NOT NULL,
    granted_at INTEGER NOT NULL,
    expires_at INTEGER,
    per_op_required INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS model_consents (
    owner_uid INTEGER NOT NULL,
    operation_id TEXT NOT NULL,
    operation_class TEXT NOT NULL,
    scope TEXT NOT NULL,
    audience_id TEXT,
    permitted_information_ref TEXT NOT NULL,
    issued_at INTEGER NOT NULL,
    expires_at INTEGER NOT NULL,
    consent_generation INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS model_policy (
    owner_uid INTEGER PRIMARY KEY,
    extraction TEXT NOT NULL,
    sharing TEXT NOT NULL,
    group_write TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS model_envelope (
    owner_uid INTEGER PRIMARY KEY,
    extraction TEXT NOT NULL,
    sharing TEXT NOT NULL,
    group_write TEXT NOT NULL,
    issued_at INTEGER NOT NULL,
    expires_at INTEGER,
    revision INTEGER NOT NULL
);
"""


class SqliteModelSnapshotStore:
    """DESIGN MODEL on local disposable sqlite. Proves statement atomicity
    of the single-transaction read ONLY - NOTHING about Postgres locking,
    encryption, auth, or erasure. Malformed rows are rejected with
    ModelInputError at the read boundary; sqlite operational failures
    PROPAGATE unwrapped. All contents are synthetic public fixtures."""

    def __init__(self, path: str) -> None:
        self._path = path
        con = sqlite3.connect(self._path)
        try:
            con.executescript(_SCHEMA)
            con.commit()
        finally:
            con.close()

    def read(self, owner_uid: int) -> Snapshot:
        _req_int(owner_uid, 0)
        con = sqlite3.connect(self._path)
        con.isolation_level = None  # explicit transaction control
        try:
            cur = con.cursor()
            cur.execute("BEGIN IMMEDIATE")  # ONE explicit local transaction
            try:
                row = cur.execute(
                    "SELECT lifetime_id, current_epoch, grants_generation,"
                    " policy_revision, decision_revision, consent_generation"
                    " FROM model_snapshot WHERE owner_uid = ?",
                    (owner_uid,),
                ).fetchone()
                grants = cur.execute(
                    "SELECT scope, granted_at, expires_at, per_op_required"
                    " FROM model_grants WHERE owner_uid = ?",
                    (owner_uid,),
                ).fetchall()
                consents = cur.execute(
                    "SELECT operation_id, operation_class, scope,"
                    " audience_id, permitted_information_ref, issued_at,"
                    " expires_at, consent_generation"
                    " FROM model_consents WHERE owner_uid = ?",
                    (owner_uid,),
                ).fetchall()
                policy = cur.execute(
                    "SELECT extraction, sharing, group_write"
                    " FROM model_policy WHERE owner_uid = ?",
                    (owner_uid,),
                ).fetchone()
                envelope = cur.execute(
                    "SELECT extraction, sharing, group_write, issued_at,"
                    " expires_at, revision"
                    " FROM model_envelope WHERE owner_uid = ?",
                    (owner_uid,),
                ).fetchone()
                cur.execute("COMMIT")
            except Exception:
                cur.execute("ROLLBACK")
                raise
        finally:
            con.close()
        if row is None:
            raise ModelInputError()  # absent snapshot creates no grants
        try:
            grant_objs = []
            for scope_text, granted_at, expires_at, per_op in grants:
                if per_op not in (0, 1):
                    raise ModelInputError()
                grant_objs.append(Grant(
                    scope=Scope(scope_text),
                    granted_at=granted_at,
                    expires_at=expires_at,
                    per_op_required=bool(per_op),
                ))
            consent_by_op = {}
            for (op_id, op_class_text, scope_text, audience_id, ref,
                 issued_at, expires_at, generation) in consents:
                key = (op_id, op_class_text, audience_id, ref,
                       issued_at, expires_at, generation)
                consent_by_op.setdefault(key, set()).add(Scope(scope_text))
            consent_objs = [
                OperationConsent(
                    owner_uid=owner_uid,
                    operation_id=key[0],
                    operation_class=OperationClass(key[1]),
                    scopes=frozenset(scope_set),
                    audience_id=key[2],
                    permitted_information_ref=key[3],
                    issued_at=key[4],
                    expires_at=key[5],
                    consent_generation=key[6],
                )
                for key, scope_set in consent_by_op.items()
            ]
            config = None
            if policy is not None:
                config = Policy(
                    extraction=ExtractionPolicy(policy[0]),
                    sharing=SharingPolicy(policy[1]),
                    group_write=GroupEligibility(policy[2]),
                )
            envelope_obj = None
            if envelope is not None:
                envelope_obj = DecisionEnvelope(
                    owner_uid=owner_uid,
                    policy=Policy(
                        extraction=ExtractionPolicy(envelope[0]),
                        sharing=SharingPolicy(envelope[1]),
                        group_write=GroupEligibility(envelope[2]),
                    ),
                    issued_at=envelope[3],
                    expires_at=envelope[4],
                    revision=envelope[5],
                )
            return Snapshot(
                owner_uid=owner_uid,
                lifetime_id=row[0],
                current_epoch=row[1],
                grants_generation=row[2],
                grants=tuple(grant_objs),
                policy_revision=row[3],
                decision_revision=row[4],
                config=config,
                decision_envelope=envelope_obj,
                consent_generation=row[5],
                consents=tuple(consent_objs),
            )
        except (ValueError, KeyError):
            # unknown enum text or malformed row shape at the read boundary
            raise ModelInputError()


# ---------------------------------------------------------------------------
# Production entry points: FAIL-CLOSED STUBS, unconditional, BEFORE any
# verifier/store/clock access. No permissive alias exists.
# ---------------------------------------------------------------------------

def production_mint(*args, **kwargs) -> None:
    raise ProductionStubRefused()


def production_export(*args, **kwargs) -> None:
    raise ProductionStubRefused()


def production_load(*args, **kwargs) -> None:
    raise ProductionStubRefused()


def production_authorize(*args, **kwargs) -> None:
    # refuses even a fully valid-looking synthetic capability
    raise ProductionStubRefused()
