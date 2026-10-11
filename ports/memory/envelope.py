"""PR-4 submit-time capture-envelope MODEL - SYNTHETIC, MODEL_ONLY, UNWIRED.

Every shape below is a SYNTHETIC MODEL_ONLY fixture - NOT runtime authority.
Controlling contract: docs/memory/pr4-envelope.md (v6 + DRAFT-READY
clarification, peer-routed 2026-10-11). Three separations hold throughout:
TRANSPORT vs INTENT, and VERIFY vs CLAIM vs AUTHORIZE.

- verify_model NEVER claims: it takes no registry handle, mutates nothing,
  and returns MODEL_ONLY evidence only.
- claim_model NEVER executes: it returns a synthetic receipt only.
- AUTHORIZE is PR-3's separate production surface and stays refused.
- All five production_* entry points are fail-closed stubs and refuse BEFORE
  touching any dependency, even a valid PR-3 ModelOnlyCapability.

Misuse-guard discipline (same as PR-1/2/3): "sealed", "immutable",
"non-caller-constructible" are constructor-level Python guarantees; object
tricks and subclasses remain OUTSIDE the model guarantee. Fernet here
models BYTE AUTHENTICATION / ENCRYPTION ONLY with PUBLIC SYNTHETIC fixture
keys: it proves NOTHING about owner authentication, trusted identity or
poll provenance, capability issuance, revocation, all-copy deletion,
replay-safe effects, or production secrecy. No real content is ever
hashed, stored, or exported by this module.
"""

import hashlib
import json
import secrets
import sqlite3
from dataclasses import dataclass
from enum import Enum
from typing import Callable, Optional, Tuple

from cryptography.fernet import Fernet, InvalidToken

import capability as cap  # PR-3 enums reused verbatim

# ---------------------------------------------------------------------------
# Constants - MODEL DEFAULTS, not security guarantees
# ---------------------------------------------------------------------------

FORMAT_VERSION = 1
MAX_AGE_SECONDS = 900
RESOLUTION_WINDOW_SECONDS = 86400
PLAINTEXT_MAX_BYTES = 16384
OUTER_TOKEN_MAX_BYTES = 32768
MAX_SCOPES = 7
AUDIENCE_ID_MAX_BYTES = 128
INFORMATION_REF_MAX_BYTES = 128
KEY_ID_MAX_CHARS = 64
REF_MAX_BYTES = 128
NONCE_HEX_LEN = 32          # 16 bytes, lowercase hex
OPERATION_ID_HEX_LEN = 32   # 16 bytes, lowercase hex
SESSION_HASH_HEX_LEN = 64
FINGERPRINT_HEX_LEN = 64
OWNER_TOKEN_HEX_LEN = 32    # 16 bytes, lowercase hex
BIGINT_MAX = cap.BIGINT_MAX


class EnvelopeCode(Enum):
    MALFORMED = "MALFORMED"
    TAMPERED = "TAMPERED"
    STALE = "STALE"
    EXPIRED = "EXPIRED"
    REPLAY = "REPLAY"
    IN_PROGRESS = "IN_PROGRESS"
    UNKNOWN = "UNKNOWN"
    KEY_UNAVAILABLE = "KEY_UNAVAILABLE"
    MODEL_ONLY = "MODEL_ONLY"


_MESSAGES = {
    EnvelopeCode.MALFORMED: "malformed envelope model input",
    EnvelopeCode.TAMPERED: "envelope authentication failed",
    EnvelopeCode.STALE: "capture no longer matches current state",
    EnvelopeCode.EXPIRED: "capture expired",
    EnvelopeCode.REPLAY: "operation replay refused",
    EnvelopeCode.IN_PROGRESS: "operation already in progress",
    EnvelopeCode.UNKNOWN: "operation outcome unknown",
    EnvelopeCode.KEY_UNAVAILABLE: "envelope key unavailable",
    EnvelopeCode.MODEL_ONLY: "production path is a fail-closed stub (MODEL_ONLY)",
}


class EnvelopeError(Exception):
    """Closed-taxonomy refusal. Fixed safe messages: no token, key, content,
    fingerprint, commitment, or owner-secret echo anywhere."""

    def __init__(self, code: EnvelopeCode):
        if not isinstance(code, EnvelopeCode) or type(code) is not EnvelopeCode:
            raise EnvelopeError(EnvelopeCode.MALFORMED)
        self.code = code
        super().__init__(_MESSAGES[code])


class ClaimState(Enum):
    ACQUIRED = "ACQUIRED"
    IN_PROGRESS = "IN_PROGRESS"
    COMMITTED = "COMMITTED"
    UNKNOWN = "UNKNOWN"
    EXPIRED = "EXPIRED"


class RegistryState(Enum):
    CLAIMED = "CLAIMED"
    COMMITTED = "COMMITTED"
    UNKNOWN = "UNKNOWN"
    EXPIRED = "EXPIRED"


class EffectState(Enum):
    CONFIRMED_COMMITTED = "CONFIRMED_COMMITTED"
    INCONCLUSIVE = "INCONCLUSIVE"


class AudienceKind(Enum):
    PRIVATE = "PRIVATE"
    GROUP = "GROUP"
    EXTERNAL = "EXTERNAL"


class Algorithm(Enum):
    FERNET = "FERNET"


# ---------------------------------------------------------------------------
# Validators (exact types; bool is not int; no coercion)
# ---------------------------------------------------------------------------

def _req_int(value, minimum):
    if type(value) is not int or value < minimum or value > BIGINT_MAX:
        raise EnvelopeError(EnvelopeCode.MALFORMED)
    return value


def _req_bool(value):
    if type(value) is not bool:
        raise EnvelopeError(EnvelopeCode.MALFORMED)
    return value


def _req_enum(value, enum_cls):
    if type(value) is not enum_cls:
        raise EnvelopeError(EnvelopeCode.MALFORMED)
    return value


def _req_hex(value, length):
    if type(value) is not str or len(value) != length:
        raise EnvelopeError(EnvelopeCode.MALFORMED)
    if value != value.lower():
        raise EnvelopeError(EnvelopeCode.MALFORMED)
    try:
        int(value, 16)
    except ValueError:
        raise EnvelopeError(EnvelopeCode.MALFORMED)
    return value


def _req_safe_str(value, max_bytes):
    if type(value) is not str or not value or value != value.strip():
        raise EnvelopeError(EnvelopeCode.MALFORMED)
    raw = value.encode("utf-8")
    if len(raw) > max_bytes:
        raise EnvelopeError(EnvelopeCode.MALFORMED)
    for ch in value:
        if ord(ch) < 0x20 or ord(ch) == 0x7F:
            raise EnvelopeError(EnvelopeCode.MALFORMED)
    return value


def _checked_add(a, b):
    _req_int(a, 0)
    _req_int(b, 0)
    if a > BIGINT_MAX - b:
        raise EnvelopeError(EnvelopeCode.MALFORMED)
    return a + b


# ---------------------------------------------------------------------------
# Canonical JSON (UTF-8, sorted keys, compact separators; no floats/NaN/
# duplicates/unknown fields; exact bytes-to-hex mapping)
# ---------------------------------------------------------------------------

def _canon(obj) -> bytes:
    return json.dumps(obj, sort_keys=True, separators=(",", ":"),
                      ensure_ascii=False).encode("utf-8")


def _reject_constant(_value):
    raise EnvelopeError(EnvelopeCode.MALFORMED)


def _reject_float(_value):
    raise EnvelopeError(EnvelopeCode.MALFORMED)


def _no_dupes(pairs):
    out = {}
    for key, val in pairs:
        if key in out:
            raise EnvelopeError(EnvelopeCode.MALFORMED)
        out[key] = val
    return out


def _strict_loads(data: bytes):
    if type(data) is not bytes:
        raise EnvelopeError(EnvelopeCode.MALFORMED)
    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError:
        raise EnvelopeError(EnvelopeCode.MALFORMED)
    try:
        return json.loads(text, object_pairs_hook=_no_dupes,
                          parse_float=_reject_float,
                          parse_constant=_reject_constant)
    except json.JSONDecodeError:
        raise EnvelopeError(EnvelopeCode.MALFORMED)


# ---------------------------------------------------------------------------
# Identity bindings
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class SessionRef:
    """WEB issuer binding. SYNTHETIC MODEL_ONLY - NOT a real session."""
    session_token_hash: str

    def __post_init__(self):
        _req_hex(self.session_token_hash, SESSION_HASH_HEX_LEN)


@dataclass(frozen=True)
class TelegramIssuer:
    """TELEGRAM issuer/bot/chat/update binding - never an invented session
    hash. SYNTHETIC MODEL_ONLY - NOT trusted polling provenance."""
    bot_id: int
    chat_id: int
    update_id: int

    def __post_init__(self):
        _req_int(self.bot_id, 1)
        _req_int(self.chat_id, -BIGINT_MAX)
        _req_int(self.update_id, 0)


def _issuer_to_dict(issuer):
    if type(issuer) is SessionRef:
        return {"session_token_hash": issuer.session_token_hash,
                "type": "web_session"}
    if type(issuer) is TelegramIssuer:
        return {"bot_id": issuer.bot_id, "chat_id": issuer.chat_id,
                "type": "telegram_issuer", "update_id": issuer.update_id}
    raise EnvelopeError(EnvelopeCode.MALFORMED)


def _issuer_from_dict(obj):
    if type(obj) is not dict:
        raise EnvelopeError(EnvelopeCode.MALFORMED)
    kind = obj.get("type")
    if kind == "web_session":
        if set(obj) != {"type", "session_token_hash"}:
            raise EnvelopeError(EnvelopeCode.MALFORMED)
        return SessionRef(session_token_hash=obj["session_token_hash"])
    if kind == "telegram_issuer":
        if set(obj) != {"type", "bot_id", "chat_id", "update_id"}:
            raise EnvelopeError(EnvelopeCode.MALFORMED)
        return TelegramIssuer(bot_id=obj["bot_id"], chat_id=obj["chat_id"],
                              update_id=obj["update_id"])
    raise EnvelopeError(EnvelopeCode.MALFORMED)


# ---------------------------------------------------------------------------
# Audience
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class ModelOnlyAudience:
    """kind + optional synthetic id. id is None ONLY where the operation
    permits; MODEL default: GROUP always requires a non-None id. NOT an
    enum of actual audiences."""
    kind: AudienceKind
    id: Optional[str]

    def __post_init__(self):
        _req_enum(self.kind, AudienceKind)
        if self.id is not None:
            _req_safe_str(self.id, AUDIENCE_ID_MAX_BYTES)
        if self.kind is AudienceKind.GROUP and self.id is None:
            raise EnvelopeError(EnvelopeCode.MALFORMED)


# ---------------------------------------------------------------------------
# IntentBinding projection (EXACT; transport fields excluded)
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class ModelOnlyRequestBinding:
    """The EXACT canonical IntentBinding projection: every identity,
    lifetime, revision, and request field INCLUDING the PR-3
    extraction_intent / group_action / sensitive / origin fields and the
    ORIGINAL capture_time/expiry. Transport fields (nonce, key_id,
    format_version, token bytes) are EXCLUDED from idempotency - and only
    from idempotency; they are still authenticated and verified.

    SYNTHETIC MODEL_ONLY - NOT runtime authority. A changed projection
    under the same operation refuses (REPLAY); a transport-only reseal
    matches."""
    uid: int
    lifetime: int
    channel: cap.Channel
    surface: cap.Surface
    issuer: object  # SessionRef | TelegramIssuer (exact types)
    operation_id: str
    operation_class: cap.OperationClass
    required_scopes: Tuple[cap.Scope, ...]
    audience: ModelOnlyAudience
    information_ref: str
    content_fingerprint: str
    capture_epoch: int
    grant_generation: int
    policy_revision: int
    decision_revision: int
    consent_generation: int
    extraction_intent: cap.ExtractionIntent
    group_action: Optional[cap.GroupAction]
    sensitive: bool
    origin: cap.Origin
    capture_time: int
    expiry: int

    def __post_init__(self):
        _req_int(self.uid, 0)
        _req_int(self.lifetime, 1)
        _req_enum(self.channel, cap.Channel)
        _req_enum(self.surface, cap.Surface)
        if type(self.issuer) not in (SessionRef, TelegramIssuer):
            raise EnvelopeError(EnvelopeCode.MALFORMED)
        if self.channel is cap.Channel.WEB and type(self.issuer) is not SessionRef:
            raise EnvelopeError(EnvelopeCode.MALFORMED)
        if (self.channel is cap.Channel.TELEGRAM
                and type(self.issuer) is not TelegramIssuer):
            raise EnvelopeError(EnvelopeCode.MALFORMED)
        _req_hex(self.operation_id, OPERATION_ID_HEX_LEN)
        _req_enum(self.operation_class, cap.OperationClass)
        if type(self.required_scopes) not in (tuple, frozenset):
            raise EnvelopeError(EnvelopeCode.MALFORMED)
        scopes = tuple(self.required_scopes)
        if not scopes or len(scopes) > MAX_SCOPES:
            raise EnvelopeError(EnvelopeCode.MALFORMED)
        for scope in scopes:
            _req_enum(scope, cap.Scope)
        if len(set(scopes)) != len(scopes):
            raise EnvelopeError(EnvelopeCode.MALFORMED)
        object.__setattr__(self, "required_scopes", scopes)
        if type(self.audience) is not ModelOnlyAudience:
            raise EnvelopeError(EnvelopeCode.MALFORMED)
        _req_safe_str(self.information_ref, INFORMATION_REF_MAX_BYTES)
        _req_hex(self.content_fingerprint, FINGERPRINT_HEX_LEN)
        _req_int(self.capture_epoch, 1)
        _req_int(self.grant_generation, 0)
        _req_int(self.policy_revision, 0)
        _req_int(self.decision_revision, 0)
        _req_int(self.consent_generation, 0)
        _req_enum(self.extraction_intent, cap.ExtractionIntent)
        if self.group_action is not None:
            _req_enum(self.group_action, cap.GroupAction)
        _req_bool(self.sensitive)
        _req_enum(self.origin, cap.Origin)
        # PR-3 group/extraction coherence rules, reused verbatim
        if self.operation_class is cap.OperationClass.GROUP_WRITE:
            if self.group_action is None:
                raise EnvelopeError(EnvelopeCode.MALFORMED)
        elif self.group_action is not None:
            raise EnvelopeError(EnvelopeCode.MALFORMED)
        extraction_like = self.operation_class is cap.OperationClass.EXTRACT or (
            self.operation_class is cap.OperationClass.GROUP_WRITE
            and self.group_action is cap.GroupAction.EXTRACT
        )
        if extraction_like:
            if self.extraction_intent not in (
                    cap.ExtractionIntent.EXPLICIT,
                    cap.ExtractionIntent.AUTOMATIC):
                raise EnvelopeError(EnvelopeCode.MALFORMED)
        elif self.extraction_intent is not cap.ExtractionIntent.NOT_APPLICABLE:
            raise EnvelopeError(EnvelopeCode.MALFORMED)
        _req_int(self.capture_time, 0)
        _req_int(self.expiry, 0)
        # Immutable submitted expiry: expiry == capture_time + MAX_AGE,
        # checked BIGINT addition. Never refreshed, never restarted.
        if self.expiry != _checked_add(self.capture_time, MAX_AGE_SECONDS):
            raise EnvelopeError(EnvelopeCode.MALFORMED)

    def projection_dict(self) -> dict:
        return {
            "audience": {"id": self.audience.id,
                         "kind": self.audience.kind.value},
            "capture_epoch": self.capture_epoch,
            "capture_time": self.capture_time,
            "channel": self.channel.value,
            "consent_generation": self.consent_generation,
            "content_fingerprint": self.content_fingerprint,
            "decision_revision": self.decision_revision,
            "extraction_intent": self.extraction_intent.value,
            "expiry": self.expiry,
            "grant_generation": self.grant_generation,
            "group_action": (self.group_action.value
                             if self.group_action is not None else None),
            "information_ref": self.information_ref,
            "issuer": _issuer_to_dict(self.issuer),
            "lifetime": self.lifetime,
            "operation_class": self.operation_class.value,
            "operation_id": self.operation_id,
            "origin": self.origin.value,
            "policy_revision": self.policy_revision,
            "required_scopes": sorted(s.value for s in self.required_scopes),
            "sensitive": self.sensitive,
            "surface": self.surface.value,
            "uid": self.uid,
        }

    def commitment(self) -> str:
        """Canonical PLAINTEXT commitment over the FULL projection. Synthetic
        guessable fixture - NOT privacy-preserving; adapter-private, never
        exported. Ciphertext is NEVER idempotency evidence."""
        return hashlib.sha256(_canon(self.projection_dict())).hexdigest()


def _binding_from_dict(obj) -> ModelOnlyRequestBinding:
    if type(obj) is not dict:
        raise EnvelopeError(EnvelopeCode.MALFORMED)
    expected = {
        "audience", "capture_epoch", "capture_time", "channel",
        "consent_generation", "content_fingerprint", "decision_revision",
        "extraction_intent", "expiry", "grant_generation", "group_action",
        "information_ref", "issuer", "lifetime", "operation_class",
        "operation_id", "origin", "policy_revision", "required_scopes",
        "sensitive", "surface", "uid",
    }
    if set(obj) != expected:
        raise EnvelopeError(EnvelopeCode.MALFORMED)
    aud = obj["audience"]
    if type(aud) is not dict or set(aud) != {"id", "kind"}:
        raise EnvelopeError(EnvelopeCode.MALFORMED)
    try:
        kind = AudienceKind(aud["kind"])
    except ValueError:
        raise EnvelopeError(EnvelopeCode.MALFORMED)
    if type(obj["required_scopes"]) is not list:
        raise EnvelopeError(EnvelopeCode.MALFORMED)
    try:
        scopes = tuple(cap.Scope(s) for s in obj["required_scopes"])
        operation_class = cap.OperationClass(obj["operation_class"])
        channel = cap.Channel(obj["channel"])
        surface = cap.Surface(obj["surface"])
        origin = cap.Origin(obj["origin"])
        extraction_intent = cap.ExtractionIntent(obj["extraction_intent"])
    except ValueError:
        raise EnvelopeError(EnvelopeCode.MALFORMED)
    group_action = obj["group_action"]
    if group_action is not None:
        try:
            group_action = cap.GroupAction(group_action)
        except ValueError:
            raise EnvelopeError(EnvelopeCode.MALFORMED)
    return ModelOnlyRequestBinding(
        uid=obj["uid"], lifetime=obj["lifetime"], channel=channel,
        surface=surface, issuer=_issuer_from_dict(obj["issuer"]),
        operation_id=obj["operation_id"], operation_class=operation_class,
        required_scopes=scopes,
        audience=ModelOnlyAudience(kind=kind, id=aud["id"]),
        information_ref=obj["information_ref"],
        content_fingerprint=obj["content_fingerprint"],
        capture_epoch=obj["capture_epoch"],
        grant_generation=obj["grant_generation"],
        policy_revision=obj["policy_revision"],
        decision_revision=obj["decision_revision"],
        consent_generation=obj["consent_generation"],
        extraction_intent=extraction_intent, group_action=group_action,
        sensitive=obj["sensitive"], origin=origin,
        capture_time=obj["capture_time"], expiry=obj["expiry"],
    )


# ---------------------------------------------------------------------------
# Payload, envelope, verified capture, caller context
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class ModelOnlyCapturedPayload:
    """Full sealed payload: the IntentBinding projection PLUS the transport
    fields. SYNTHETIC MODEL_ONLY - NOT runtime authority."""
    binding: ModelOnlyRequestBinding
    nonce: str
    format_version: int
    key_id: str

    def __post_init__(self):
        if type(self.binding) is not ModelOnlyRequestBinding:
            raise EnvelopeError(EnvelopeCode.MALFORMED)
        _req_hex(self.nonce, NONCE_HEX_LEN)
        _req_int(self.format_version, 1)
        if self.format_version != FORMAT_VERSION:
            raise EnvelopeError(EnvelopeCode.MALFORMED)
        _req_safe_str(self.key_id, KEY_ID_MAX_CHARS)

    def to_dict(self) -> dict:
        out = self.binding.projection_dict()
        out["format_version"] = self.format_version
        out["key_id"] = self.key_id
        out["nonce"] = self.nonce
        return out

    @staticmethod
    def from_dict(obj) -> "ModelOnlyCapturedPayload":
        if type(obj) is not dict:
            raise EnvelopeError(EnvelopeCode.MALFORMED)
        binding_keys = {
            "audience", "capture_epoch", "capture_time", "channel",
            "consent_generation", "content_fingerprint", "decision_revision",
            "extraction_intent", "expiry", "grant_generation", "group_action",
            "information_ref", "issuer", "lifetime", "operation_class",
            "operation_id", "origin", "policy_revision", "required_scopes",
            "sensitive", "surface", "uid",
        }
        if set(obj) != binding_keys | {"format_version", "key_id", "nonce"}:
            raise EnvelopeError(EnvelopeCode.MALFORMED)
        return ModelOnlyCapturedPayload(
            binding=_binding_from_dict(
                {k: obj[k] for k in binding_keys}),
            nonce=obj["nonce"], format_version=obj["format_version"],
            key_id=obj["key_id"])


@dataclass(frozen=True)
class ModelOnlyEnvelope:
    """Sealed envelope: outer routing (format/key) + token bytes. SYNTHETIC
    MODEL_ONLY fixture."""
    format_version: int
    key_id: str
    token: bytes

    def __post_init__(self):
        _req_int(self.format_version, 1)
        _req_safe_str(self.key_id, KEY_ID_MAX_CHARS)
        if type(self.token) is not bytes:
            raise EnvelopeError(EnvelopeCode.MALFORMED)


@dataclass(frozen=True)
class ModelOnlyVerifiedCapture:
    """Output of verify_model. MODEL_ONLY evidence - NEVER authority, NEVER
    a claim, NEVER execution permission."""
    status: cap.ModelStatus
    payload: ModelOnlyCapturedPayload
    binding: ModelOnlyRequestBinding

    def __post_init__(self):
        _req_enum(self.status, cap.ModelStatus)
        if self.status is not cap.ModelStatus.MODEL_ONLY:
            raise EnvelopeError(EnvelopeCode.MALFORMED)
        if type(self.payload) is not ModelOnlyCapturedPayload:
            raise EnvelopeError(EnvelopeCode.MALFORMED)
        if type(self.binding) is not ModelOnlyRequestBinding:
            raise EnvelopeError(EnvelopeCode.MALFORMED)
        if self.payload.binding != self.binding:
            raise EnvelopeError(EnvelopeCode.MALFORMED)


@dataclass(frozen=True)
class ModelOnlyCallerContext:
    """ONE coherent synthetic snapshot: the ORIGINAL full binding for
    equality PLUS a separate CURRENT identity/epoch/revision snapshot for
    staleness. Current times NEVER replace the original capture/expiry."""
    original_binding: ModelOnlyRequestBinding
    current_uid: int
    current_lifetime: int
    current_capture_epoch: int
    current_grant_generation: int
    current_policy_revision: int
    current_decision_revision: int
    current_consent_generation: int

    def __post_init__(self):
        if type(self.original_binding) is not ModelOnlyRequestBinding:
            raise EnvelopeError(EnvelopeCode.MALFORMED)
        _req_int(self.current_uid, 0)
        _req_int(self.current_lifetime, 1)
        _req_int(self.current_capture_epoch, 1)
        _req_int(self.current_grant_generation, 0)
        _req_int(self.current_policy_revision, 0)
        _req_int(self.current_decision_revision, 0)
        _req_int(self.current_consent_generation, 0)


# ---------------------------------------------------------------------------
# Keys and clock (typed SYNTHETIC fixtures)
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class SyntheticKey:
    """Typed synthetic key. PUBLIC FIXTURE MATERIAL - provides NO secrecy
    and NO real authentication. Never the marker key, never the content
    key."""
    algorithm: Algorithm
    format_version: int
    key_id: str
    not_before: int
    decrypt_until: int
    material: bytes

    def __post_init__(self):
        _req_enum(self.algorithm, Algorithm)
        _req_int(self.format_version, 1)
        if self.format_version != FORMAT_VERSION:
            raise EnvelopeError(EnvelopeCode.MALFORMED)
        _req_safe_str(self.key_id, KEY_ID_MAX_CHARS)
        _req_int(self.not_before, 0)
        _req_int(self.decrypt_until, 0)
        if self.decrypt_until <= self.not_before:
            raise EnvelopeError(EnvelopeCode.MALFORMED)
        if type(self.material) is not bytes or len(self.material) != 44:
            raise EnvelopeError(EnvelopeCode.MALFORMED)

    def usable_at(self, now: int) -> bool:
        return self.not_before <= now <= self.decrypt_until


@dataclass(frozen=True)
class SyntheticKeyRing:
    """Explicit retained-key lookup ONLY: no trial-all, no fallback, no
    refresh, no re-encryption TTL extension."""
    keys: Tuple[SyntheticKey, ...]

    def __post_init__(self):
        if type(self.keys) is not tuple:
            raise EnvelopeError(EnvelopeCode.MALFORMED)
        for key in self.keys:
            if type(key) is not SyntheticKey:
                raise EnvelopeError(EnvelopeCode.MALFORMED)

    def lookup(self, key_id: str, now: int) -> SyntheticKey:
        _req_safe_str(key_id, KEY_ID_MAX_CHARS)
        _req_int(now, 0)
        for key in self.keys:
            if key.key_id == key_id:
                if not key.usable_at(now):
                    raise EnvelopeError(EnvelopeCode.KEY_UNAVAILABLE)
                return key
        raise EnvelopeError(EnvelopeCode.KEY_UNAVAILABLE)


class ModelOnlyClock:
    """Synthetic injected clock. No distributed-time guarantee."""

    def now(self) -> int:  # pragma: no cover - interface
        raise NotImplementedError


class SyntheticClock(ModelOnlyClock):
    def __init__(self, now: int):
        _req_int(now, 0)
        self._now = now

    def now(self) -> int:
        return self._now

    def advance(self, seconds: int) -> None:
        _req_int(seconds, 0)
        self._now = _checked_add(self._now, seconds)


def _req_clock(clock) -> ModelOnlyClock:
    if not isinstance(clock, ModelOnlyClock):
        raise EnvelopeError(EnvelopeCode.MALFORMED)
    _req_int(clock.now(), 0)
    return clock


# ---------------------------------------------------------------------------
# seal_model / verify_model (VERIFY NEVER CLAIMS)
# ---------------------------------------------------------------------------

def seal_model(payload: ModelOnlyCapturedPayload, key: SyntheticKey,
               clock: ModelOnlyClock) -> ModelOnlyEnvelope:
    """Seal the payload. Checks the key window AND the original payload
    expiry at seal time. SYNTHETIC MODEL_ONLY."""
    if type(payload) is not ModelOnlyCapturedPayload:
        raise EnvelopeError(EnvelopeCode.MALFORMED)
    if type(key) is not SyntheticKey:
        raise EnvelopeError(EnvelopeCode.MALFORMED)
    clock = _req_clock(clock)
    now = clock.now()
    if key.algorithm is not Algorithm.FERNET or not key.usable_at(now):
        raise EnvelopeError(EnvelopeCode.KEY_UNAVAILABLE)
    if payload.key_id != key.key_id:
        raise EnvelopeError(EnvelopeCode.MALFORMED)
    if now >= payload.binding.expiry:
        raise EnvelopeError(EnvelopeCode.EXPIRED)
    if payload.binding.capture_time > now:
        raise EnvelopeError(EnvelopeCode.MALFORMED)
    plaintext = _canon(payload.to_dict())
    if len(plaintext) > PLAINTEXT_MAX_BYTES:
        raise EnvelopeError(EnvelopeCode.MALFORMED)
    token = Fernet(key.material).encrypt(plaintext)
    if len(token) > OUTER_TOKEN_MAX_BYTES:
        raise EnvelopeError(EnvelopeCode.MALFORMED)
    return ModelOnlyEnvelope(format_version=FORMAT_VERSION,
                             key_id=key.key_id, token=token)


def verify_model(envelope: ModelOnlyEnvelope, keyring: SyntheticKeyRing,
                 current_context: ModelOnlyCallerContext,
                 clock: ModelOnlyClock) -> ModelOnlyVerifiedCapture:
    """Decrypt + validate + staleness comparison ONLY. NO registry handle,
    NO mutation, NO claim, NO execution permission. Order of checks:
    outer bound -> authenticate -> inner bound -> strict decode/binding ->
    time/context."""
    if type(envelope) is not ModelOnlyEnvelope:
        raise EnvelopeError(EnvelopeCode.MALFORMED)
    if type(keyring) is not SyntheticKeyRing:
        raise EnvelopeError(EnvelopeCode.MALFORMED)
    if type(current_context) is not ModelOnlyCallerContext:
        raise EnvelopeError(EnvelopeCode.MALFORMED)
    clock = _req_clock(clock)
    now = clock.now()
    # outer bound BEFORE decrypt
    if len(envelope.token) > OUTER_TOKEN_MAX_BYTES:
        raise EnvelopeError(EnvelopeCode.MALFORMED)
    # explicit retained-key lookup only
    key = keyring.lookup(envelope.key_id, now)
    if key.algorithm is not Algorithm.FERNET:
        raise EnvelopeError(EnvelopeCode.KEY_UNAVAILABLE)
    # authenticate/decrypt (InvalidToken -> TAMPERED; genuine crypto
    # operational failures propagate DISTINCTLY, unwrapped)
    try:
        plaintext = Fernet(key.material).decrypt(envelope.token)
    except InvalidToken:
        raise EnvelopeError(EnvelopeCode.TAMPERED)
    # inner bound AFTER authenticate/decrypt, BEFORE parsing
    if len(plaintext) > PLAINTEXT_MAX_BYTES:
        raise EnvelopeError(EnvelopeCode.MALFORMED)
    payload = ModelOnlyCapturedPayload.from_dict(_strict_loads(plaintext))
    # format/key bound in outer routing AND authenticated inner payload
    if (envelope.format_version != payload.format_version
            or envelope.key_id != payload.key_id):
        raise EnvelopeError(EnvelopeCode.TAMPERED)
    # time: no future capture; now >= expiry refuses (equality refuses)
    if payload.binding.capture_time > now:
        raise EnvelopeError(EnvelopeCode.MALFORMED)
    if now >= payload.binding.expiry:
        raise EnvelopeError(EnvelopeCode.EXPIRED)
    # binding equality: EVERY projection field vs the original full binding
    if payload.binding != current_context.original_binding:
        raise EnvelopeError(EnvelopeCode.STALE)
    # staleness vs the CURRENT coherent snapshot
    binding = payload.binding
    if (binding.uid != current_context.current_uid
            or binding.lifetime != current_context.current_lifetime
            or binding.capture_epoch != current_context.current_capture_epoch
            or binding.grant_generation
            != current_context.current_grant_generation
            or binding.policy_revision
            != current_context.current_policy_revision
            or binding.decision_revision
            != current_context.current_decision_revision
            or binding.consent_generation
            != current_context.current_consent_generation):
        raise EnvelopeError(EnvelopeCode.STALE)
    return ModelOnlyVerifiedCapture(status=cap.ModelStatus.MODEL_ONLY,
                                    payload=payload, binding=payload.binding)


# ---------------------------------------------------------------------------
# Registry records (exact typed shapes; readback is record-only)
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class ModelOnlyOperationScope:
    uid: int
    lifetime: int
    operation_id: str

    def __post_init__(self):
        _req_int(self.uid, 0)
        _req_int(self.lifetime, 1)
        _req_hex(self.operation_id, OPERATION_ID_HEX_LEN)


@dataclass(frozen=True)
class ModelOnlyOutcome:
    """Nonblank synthetic opaque refs only; no arbitrary maps or data."""
    outcome_ref: str
    operation_marker_ref: str

    def __post_init__(self):
        _req_safe_str(self.outcome_ref, REF_MAX_BYTES)
        _req_safe_str(self.operation_marker_ref, REF_MAX_BYTES)


@dataclass(frozen=True)
class ModelOnlyRegistryRecord:
    """Readback record: typed status/scope/state/expiry/deadline/outcome
    ONLY - never commitment, raw token, owner secret, or fingerprint."""
    status: cap.ModelStatus
    scope: ModelOnlyOperationScope
    state: RegistryState
    expiry: int
    resolution_deadline: int
    outcome: Optional[ModelOnlyOutcome]

    def __post_init__(self):
        _req_enum(self.status, cap.ModelStatus)
        if self.status is not cap.ModelStatus.MODEL_ONLY:
            raise EnvelopeError(EnvelopeCode.MALFORMED)
        if type(self.scope) is not ModelOnlyOperationScope:
            raise EnvelopeError(EnvelopeCode.MALFORMED)
        _req_enum(self.state, RegistryState)
        _req_int(self.expiry, 0)
        _req_int(self.resolution_deadline, 0)
        if self.outcome is not None and type(self.outcome) is not ModelOnlyOutcome:
            raise EnvelopeError(EnvelopeCode.MALFORMED)
        if self.state is RegistryState.COMMITTED and self.outcome is None:
            raise EnvelopeError(EnvelopeCode.MALFORMED)
        if self.state is not RegistryState.COMMITTED and self.outcome is not None:
            raise EnvelopeError(EnvelopeCode.MALFORMED)


@dataclass(frozen=True)
class ModelOnlyClaimResult:
    """owner_token/fence populated ONLY on ACQUIRED; outcome ONLY on
    COMMITTED; IN_PROGRESS is non-executable."""
    status: cap.ModelStatus
    claim_state: ClaimState
    scope: ModelOnlyOperationScope
    fence: Optional[int]
    owner_token: Optional[str]
    outcome: Optional[ModelOnlyOutcome]

    def __post_init__(self):
        _req_enum(self.status, cap.ModelStatus)
        if self.status is not cap.ModelStatus.MODEL_ONLY:
            raise EnvelopeError(EnvelopeCode.MALFORMED)
        _req_enum(self.claim_state, ClaimState)
        if type(self.scope) is not ModelOnlyOperationScope:
            raise EnvelopeError(EnvelopeCode.MALFORMED)
        if self.claim_state is ClaimState.ACQUIRED:
            _req_int(self.fence, 1)
            _req_hex(self.owner_token, OWNER_TOKEN_HEX_LEN)
            if self.outcome is not None:
                raise EnvelopeError(EnvelopeCode.MALFORMED)
        else:
            if self.fence is not None or self.owner_token is not None:
                raise EnvelopeError(EnvelopeCode.MALFORMED)
        if self.claim_state is ClaimState.COMMITTED:
            if type(self.outcome) is not ModelOnlyOutcome:
                raise EnvelopeError(EnvelopeCode.MALFORMED)
        elif self.outcome is not None:
            raise EnvelopeError(EnvelopeCode.MALFORMED)


@dataclass(frozen=True)
class ModelOnlyUncertainReceipt:
    """LOCAL observation of uncertainty - NOT a durability proof and NOT a
    durable registry write."""
    status: cap.ModelStatus
    scope: ModelOnlyOperationScope
    fence: int
    receipt_state: ClaimState

    def __post_init__(self):
        _req_enum(self.status, cap.ModelStatus)
        if self.status is not cap.ModelStatus.MODEL_ONLY:
            raise EnvelopeError(EnvelopeCode.MALFORMED)
        if type(self.scope) is not ModelOnlyOperationScope:
            raise EnvelopeError(EnvelopeCode.MALFORMED)
        _req_int(self.fence, 1)
        if self.receipt_state is not ClaimState.UNKNOWN:
            raise EnvelopeError(EnvelopeCode.MALFORMED)


@dataclass(frozen=True)
class ModelOnlyEffectEvidence:
    """Typed synthetic refs only - NEVER runtime authority. A
    CONFIRMED_COMMITTED claim must match scope + binding + owner + fence
    AND a durable synthetic marker readback from the adapter; a caller
    flag alone NEVER commits. All fixture-only."""
    status: cap.ModelStatus
    scope: ModelOnlyOperationScope
    binding: ModelOnlyRequestBinding
    owner_token: str
    fence: int
    operation_marker_ref: str
    effect_state: EffectState
    outcome: Optional[ModelOnlyOutcome]

    def __post_init__(self):
        _req_enum(self.status, cap.ModelStatus)
        if self.status is not cap.ModelStatus.MODEL_ONLY:
            raise EnvelopeError(EnvelopeCode.MALFORMED)
        if type(self.scope) is not ModelOnlyOperationScope:
            raise EnvelopeError(EnvelopeCode.MALFORMED)
        if type(self.binding) is not ModelOnlyRequestBinding:
            raise EnvelopeError(EnvelopeCode.MALFORMED)
        _req_hex(self.owner_token, OWNER_TOKEN_HEX_LEN)
        _req_int(self.fence, 1)
        _req_safe_str(self.operation_marker_ref, REF_MAX_BYTES)
        _req_enum(self.effect_state, EffectState)
        if self.effect_state is EffectState.CONFIRMED_COMMITTED:
            if type(self.outcome) is not ModelOnlyOutcome:
                raise EnvelopeError(EnvelopeCode.MALFORMED)
            if self.outcome.operation_marker_ref != self.operation_marker_ref:
                raise EnvelopeError(EnvelopeCode.MALFORMED)
        elif self.outcome is not None:
            raise EnvelopeError(EnvelopeCode.MALFORMED)


# ---------------------------------------------------------------------------
# Synthetic registry adapter (disposable sqlite; NOT production DDL)
# ---------------------------------------------------------------------------

class SyntheticRegistryAdapter:
    """Disposable sqlite adapter. Owns BOTH tables plus the durable marker
    table. A synthetic local choice - PR-2's memory_markers is NOT
    implicitly a runtime nonce registry."""

    def __init__(self, db: sqlite3.Connection):
        if type(db) is not sqlite3.Connection:
            raise EnvelopeError(EnvelopeCode.MALFORMED)
        self._db = db
        self._db.execute(
            "CREATE TABLE IF NOT EXISTS model_operations ("
            "uid BIGINT NOT NULL, lifetime BIGINT NOT NULL,"
            " operation_id TEXT NOT NULL, commitment TEXT NOT NULL,"
            " capture_time BIGINT NOT NULL, expiry BIGINT NOT NULL,"
            " state TEXT NOT NULL, owner_token TEXT NOT NULL,"
            " fence BIGINT NOT NULL, outcome_ref TEXT,"
            " operation_marker_ref TEXT, resolution_deadline BIGINT NOT NULL,"
            " PRIMARY KEY (uid, lifetime, operation_id))")
        self._db.execute(
            "CREATE TABLE IF NOT EXISTS model_used_nonces ("
            "uid BIGINT NOT NULL, lifetime BIGINT NOT NULL,"
            " nonce TEXT NOT NULL, operation_id TEXT NOT NULL,"
            " commitment TEXT NOT NULL,"
            " PRIMARY KEY (uid, lifetime, nonce))")
        self._db.execute(
            "CREATE TABLE IF NOT EXISTS model_markers ("
            "marker_ref TEXT PRIMARY KEY, outcome_ref TEXT NOT NULL)")
        self._db.commit()

    def transaction(self):
        return _Transaction(self._db)

    def get_operation(self, scope: ModelOnlyOperationScope):
        row = self._db.execute(
            "SELECT commitment, capture_time, expiry, state, owner_token,"
            " fence, outcome_ref, operation_marker_ref, resolution_deadline"
            " FROM model_operations WHERE uid=? AND lifetime=?"
            " AND operation_id=?",
            (scope.uid, scope.lifetime, scope.operation_id)).fetchone()
        return row

    def get_nonce(self, uid: int, lifetime: int, nonce: str):
        return self._db.execute(
            "SELECT operation_id, commitment FROM model_used_nonces"
            " WHERE uid=? AND lifetime=? AND nonce=?",
            (uid, lifetime, nonce)).fetchone()

    def insert_operation(self, scope, commitment, capture_time, expiry,
                         state, owner_token, fence, resolution_deadline):
        self._db.execute(
            "INSERT INTO model_operations (uid, lifetime, operation_id,"
            " commitment, capture_time, expiry, state, owner_token, fence,"
            " outcome_ref, operation_marker_ref, resolution_deadline)"
            " VALUES (?,?,?,?,?,?,?,?,?,NULL,NULL,?)",
            (scope.uid, scope.lifetime, scope.operation_id, commitment,
             capture_time, expiry, state.value, owner_token, fence,
             resolution_deadline))

    def insert_nonce(self, uid, lifetime, nonce, operation_id, commitment):
        self._db.execute(
            "INSERT INTO model_used_nonces (uid, lifetime, nonce,"
            " operation_id, commitment) VALUES (?,?,?,?,?)",
            (uid, lifetime, nonce, operation_id, commitment))

    def set_committed(self, scope, outcome: ModelOnlyOutcome):
        self._db.execute(
            "UPDATE model_operations SET state=?, outcome_ref=?,"
            " operation_marker_ref=? WHERE uid=? AND lifetime=?"
            " AND operation_id=?",
            (RegistryState.COMMITTED.value, outcome.outcome_ref,
             outcome.operation_marker_ref, scope.uid, scope.lifetime,
             scope.operation_id))

    def set_state(self, scope, state: RegistryState):
        self._db.execute(
            "UPDATE model_operations SET state=? WHERE uid=? AND lifetime=?"
            " AND operation_id=?",
            (state.value, scope.uid, scope.lifetime, scope.operation_id))

    def put_marker(self, marker_ref: str, outcome_ref: str):
        self._db.execute(
            "INSERT INTO model_markers (marker_ref, outcome_ref)"
            " VALUES (?,?)", (marker_ref, outcome_ref))

    def marker_exists(self, marker_ref: str) -> bool:
        return self._db.execute(
            "SELECT 1 FROM model_markers WHERE marker_ref=?",
            (marker_ref,)).fetchone() is not None


class _Transaction:
    def __init__(self, db):
        self._db = db

    def __enter__(self):
        self._db.execute("BEGIN IMMEDIATE")
        return self

    def __exit__(self, exc_type, exc, tb):
        if exc_type is None:
            self._db.commit()
        else:
            self._db.rollback()
        return False


def _row_to_record(scope, row) -> ModelOnlyRegistryRecord:
    state = RegistryState(row[3])
    outcome = None
    if state is RegistryState.COMMITTED:
        outcome = ModelOnlyOutcome(outcome_ref=row[6],
                                   operation_marker_ref=row[7])
    return ModelOnlyRegistryRecord(
        status=cap.ModelStatus.MODEL_ONLY, scope=scope, state=state,
        expiry=row[2], resolution_deadline=row[8], outcome=outcome)


# ---------------------------------------------------------------------------
# ModelOnlyRegistryModel (CLAIM NEVER EXECUTES)
# ---------------------------------------------------------------------------

class ModelOnlyRegistryModel:
    """Owned two-table atomic registry. claim_model uses the ORIGINAL
    payload's scope/binding/nonce/capture/expiry and the INJECTED clock -
    never caller-authoritative time. A fresh ACQUIRED yields ONLY a
    synthetic receipt; no real effect is executed by any method."""

    def __init__(self, adapter: SyntheticRegistryAdapter,
                 clock: ModelOnlyClock,
                 owner_token_factory: Optional[Callable[[], str]] = None):
        if type(adapter) is not SyntheticRegistryAdapter:
            raise EnvelopeError(EnvelopeCode.MALFORMED)
        self._adapter = adapter
        self._clock = _req_clock(clock)
        if owner_token_factory is None:
            owner_token_factory = lambda: secrets.token_hex(16)
        self._owner_token_factory = owner_token_factory

    def _fresh_owner_token(self) -> str:
        token = self._owner_token_factory()
        _req_hex(token, OWNER_TOKEN_HEX_LEN)
        return token

    def claim_model(self, verified: ModelOnlyVerifiedCapture,
                    claim_owner: str) -> ModelOnlyClaimResult:
        if type(verified) is not ModelOnlyVerifiedCapture:
            raise EnvelopeError(EnvelopeCode.MALFORMED)
        _req_safe_str(claim_owner, REF_MAX_BYTES)
        payload = verified.payload
        binding = payload.binding
        scope = ModelOnlyOperationScope(
            uid=binding.uid, lifetime=binding.lifetime,
            operation_id=binding.operation_id)
        now = self._clock.now()
        # MANDATORY time recheck after verification, on ORIGINAL times
        if binding.capture_time > now:
            raise EnvelopeError(EnvelopeCode.MALFORMED)
        if now >= binding.expiry:
            raise EnvelopeError(EnvelopeCode.EXPIRED)
        commitment = binding.commitment()
        resolution_deadline = _checked_add(binding.expiry,
                                           RESOLUTION_WINDOW_SECONDS)
        with self._adapter.transaction():
            nonce_row = self._adapter.get_nonce(binding.uid,
                                                binding.lifetime,
                                                payload.nonce)
            if nonce_row is not None and nonce_row[0] != binding.operation_id:
                # nonce crossing to another operation REFUSES
                raise EnvelopeError(EnvelopeCode.REPLAY)
            row = self._adapter.get_operation(scope)
            if row is None:
                owner_token = self._fresh_owner_token()
                self._adapter.insert_operation(
                    scope, commitment, binding.capture_time, binding.expiry,
                    RegistryState.CLAIMED, owner_token, 1,
                    resolution_deadline)
                self._adapter.insert_nonce(binding.uid, binding.lifetime,
                                           payload.nonce,
                                           binding.operation_id, commitment)
                return ModelOnlyClaimResult(
                    status=cap.ModelStatus.MODEL_ONLY,
                    claim_state=ClaimState.ACQUIRED, scope=scope, fence=1,
                    owner_token=owner_token, outcome=None)
            if row[0] != commitment:
                # CHANGED projection under the same operation REFUSES
                raise EnvelopeError(EnvelopeCode.REPLAY)
            # same operation: a reseal atomically REGISTERS the new nonce
            # under the same operation - NEVER another claim
            if nonce_row is None:
                self._adapter.insert_nonce(binding.uid, binding.lifetime,
                                           payload.nonce,
                                           binding.operation_id, commitment)
            state = RegistryState(row[3])
            if state is RegistryState.CLAIMED:
                return ModelOnlyClaimResult(
                    status=cap.ModelStatus.MODEL_ONLY,
                    claim_state=ClaimState.IN_PROGRESS, scope=scope,
                    fence=None, owner_token=None, outcome=None)
            if state is RegistryState.COMMITTED:
                outcome = ModelOnlyOutcome(outcome_ref=row[6],
                                           operation_marker_ref=row[7])
                return ModelOnlyClaimResult(
                    status=cap.ModelStatus.MODEL_ONLY,
                    claim_state=ClaimState.COMMITTED, scope=scope,
                    fence=None, owner_token=None, outcome=outcome)
            if state is RegistryState.UNKNOWN:
                return ModelOnlyClaimResult(
                    status=cap.ModelStatus.MODEL_ONLY,
                    claim_state=ClaimState.UNKNOWN, scope=scope,
                    fence=None, owner_token=None, outcome=None)
            return ModelOnlyClaimResult(
                status=cap.ModelStatus.MODEL_ONLY,
                claim_state=ClaimState.EXPIRED, scope=scope,
                fence=None, owner_token=None, outcome=None)

    def record_outcome_model(self, scope: ModelOnlyOperationScope,
                             owner_token: str, fence: int,
                             outcome: ModelOnlyOutcome
                             ) -> ModelOnlyRegistryRecord:
        """Atomic: check owner/fence/current state/expiry, then the
        simulated effect's durable marker AND the outcome land in the SAME
        disposable transaction. NEVER execute-after-expiry-then-mark.
        Duplicate/late records with the SAME known outcome are idempotent;
        a CHANGED outcome refuses; outcomes are never overwritten."""
        self._check_scope_owner_fence(scope, owner_token, fence)
        if type(outcome) is not ModelOnlyOutcome:
            raise EnvelopeError(EnvelopeCode.MALFORMED)
        now = self._clock.now()
        expired_tombstoned = False
        with self._adapter.transaction():
            row = self._require_row(scope)
            state = RegistryState(row[3])
            if state is RegistryState.CLAIMED and now >= row[2]:
                # expired pending -> blocked EXPIRED tombstone, never NEW.
                # The tombstone write COMMITS; the refusal is raised after.
                self._adapter.set_state(scope, RegistryState.EXPIRED)
                expired_tombstoned = True
            elif state is RegistryState.CLAIMED:
                self._adapter.put_marker(outcome.operation_marker_ref,
                                         outcome.outcome_ref)
                self._adapter.set_committed(scope, outcome)
            elif state is RegistryState.COMMITTED:
                if row[6] != outcome.outcome_ref:
                    raise EnvelopeError(EnvelopeCode.REPLAY)
                # same known outcome: idempotent, never overwrite
            elif state is RegistryState.UNKNOWN:
                # UNKNOWN resolves ONLY via resolve_unknown_model + evidence
                raise EnvelopeError(EnvelopeCode.UNKNOWN)
            else:
                raise EnvelopeError(EnvelopeCode.EXPIRED)
            row = self._adapter.get_operation(scope)
            record = _row_to_record(scope, row)
        if expired_tombstoned:
            raise EnvelopeError(EnvelopeCode.EXPIRED)
        return record

    def mark_uncertain_model(self, scope: ModelOnlyOperationScope,
                             owner_token: str, fence: int
                             ) -> ModelOnlyUncertainReceipt:
        """LOCAL uncertainty receipt. A timeout CANNOT assert a durable
        UNKNOWN write; this method writes NOTHING."""
        self._check_scope_owner_fence(scope, owner_token, fence)
        with self._adapter.transaction():
            row = self._require_row(scope)
            state = RegistryState(row[3])
            if state not in (RegistryState.CLAIMED, RegistryState.UNKNOWN):
                raise EnvelopeError(EnvelopeCode.MALFORMED)
        return ModelOnlyUncertainReceipt(
            status=cap.ModelStatus.MODEL_ONLY, scope=scope, fence=fence,
            receipt_state=ClaimState.UNKNOWN)

    def resolve_unknown_model(self, scope: ModelOnlyOperationScope,
                              owner_token: str, fence: int,
                              evidence: ModelOnlyEffectEvidence
                              ) -> ModelOnlyRegistryRecord:
        """Resolves ONLY from authoritative evidence matching scope +
        binding + owner + fence AND a durable synthetic marker readback.
        Absent/inconclusive stays UNKNOWN/BLOCKED - no blind retry, no fake
        not-written certainty, no retry grant. LATE confirmed-marker
        evidence CAN resolve UNKNOWN after expiry (accounting for an
        already-occurring effect); it NEVER grants fresh execution."""
        self._check_scope_owner_fence(scope, owner_token, fence)
        if type(evidence) is not ModelOnlyEffectEvidence:
            raise EnvelopeError(EnvelopeCode.MALFORMED)
        if evidence.scope != scope:
            raise EnvelopeError(EnvelopeCode.REPLAY)
        with self._adapter.transaction():
            row = self._require_row(scope)
            state = RegistryState(row[3])
            if state is RegistryState.EXPIRED:
                # tombstones never reopen
                raise EnvelopeError(EnvelopeCode.EXPIRED)
            if evidence.binding.commitment() != row[0]:
                raise EnvelopeError(EnvelopeCode.REPLAY)
            if state is RegistryState.COMMITTED:
                if (evidence.effect_state is EffectState.CONFIRMED_COMMITTED
                        and row[6] == evidence.outcome.outcome_ref):
                    return _row_to_record(scope, row)
                raise EnvelopeError(EnvelopeCode.REPLAY)
            if evidence.effect_state is EffectState.CONFIRMED_COMMITTED:
                if not self._adapter.marker_exists(
                        evidence.operation_marker_ref):
                    # missing marker stays BLOCKED
                    self._adapter.set_state(scope, RegistryState.UNKNOWN)
                    row = self._adapter.get_operation(scope)
                    return _row_to_record(scope, row)
                self._adapter.set_committed(scope, evidence.outcome)
            else:
                # INCONCLUSIVE stays BLOCKED
                self._adapter.set_state(scope, RegistryState.UNKNOWN)
            row = self._adapter.get_operation(scope)
            return _row_to_record(scope, row)

    def readback_model(self, scope: ModelOnlyOperationScope
                       ) -> Optional[ModelOnlyRegistryRecord]:
        """Typed record ONLY: status/scope/state/expiry/deadline/outcome.
        Never commitment, raw token, owner secret, or fingerprint."""
        if type(scope) is not ModelOnlyOperationScope:
            raise EnvelopeError(EnvelopeCode.MALFORMED)
        with self._adapter.transaction():
            row = self._adapter.get_operation(scope)
            if row is None:
                return None
            return _row_to_record(scope, row)

    def _check_scope_owner_fence(self, scope, owner_token, fence):
        if type(scope) is not ModelOnlyOperationScope:
            raise EnvelopeError(EnvelopeCode.MALFORMED)
        _req_hex(owner_token, OWNER_TOKEN_HEX_LEN)
        _req_int(fence, 1)
        row = self._adapter.get_operation(scope)
        if row is not None and (row[4] != owner_token or row[5] != fence):
            # stale owner or fence CANNOT complete
            raise EnvelopeError(EnvelopeCode.REPLAY)

    def _require_row(self, scope):
        row = self._adapter.get_operation(scope)
        if row is None:
            raise EnvelopeError(EnvelopeCode.MALFORMED)
        return row


# ---------------------------------------------------------------------------
# Poll provenance (constructor-gated SYNTHETIC boundary; NOT trusted
# polling provenance)
# ---------------------------------------------------------------------------

_EVIDENCE_SEAL = object()


class SyntheticBoundaryEvidence:
    """Constructor-gated synthetic boundary evidence. The seal is a
    MISUSE-GUARD ONLY: it claims NO 'test paths cannot mint' guarantee and
    NO trusted polling/authentication/issuance property."""

    def __init__(self, payload: ModelOnlyCapturedPayload, _seal=None):
        if _seal is not _EVIDENCE_SEAL:
            raise EnvelopeError(EnvelopeCode.MODEL_ONLY)
        if type(payload) is not ModelOnlyCapturedPayload:
            raise EnvelopeError(EnvelopeCode.MALFORMED)
        self._payload = payload

    @classmethod
    def for_testing(cls, payload: ModelOnlyCapturedPayload
                    ) -> "SyntheticBoundaryEvidence":
        return cls(payload, _seal=_EVIDENCE_SEAL)

    @property
    def payload(self) -> ModelOnlyCapturedPayload:
        return self._payload


@dataclass(frozen=True)
class ModelOnlyPollProvenance:
    """Field carriage ONLY: no trusted polling, authentication, or
    issuance claim. The payload is an EXACT TELEGRAM-channel payload."""
    status: cap.ModelStatus
    payload: ModelOnlyCapturedPayload
    minted_at: int

    def __post_init__(self):
        _req_enum(self.status, cap.ModelStatus)
        if self.status is not cap.ModelStatus.MODEL_ONLY:
            raise EnvelopeError(EnvelopeCode.MALFORMED)
        if type(self.payload) is not ModelOnlyCapturedPayload:
            raise EnvelopeError(EnvelopeCode.MALFORMED)
        if self.payload.binding.channel is not cap.Channel.TELEGRAM:
            raise EnvelopeError(EnvelopeCode.MALFORMED)
        _req_int(self.minted_at, 0)
        if self.minted_at < self.payload.binding.capture_time:
            raise EnvelopeError(EnvelopeCode.MALFORMED)
        if self.minted_at >= self.payload.binding.expiry:
            raise EnvelopeError(EnvelopeCode.MALFORMED)


def mint_poll_provenance_model(
        evidence: SyntheticBoundaryEvidence,
        clock: ModelOnlyClock) -> ModelOnlyPollProvenance:
    """Mint from SYNTHETIC boundary evidence only. capture_time <=
    minted_at < expiry enforced."""
    if type(evidence) is not SyntheticBoundaryEvidence:
        raise EnvelopeError(EnvelopeCode.MODEL_ONLY)
    clock = _req_clock(clock)
    return ModelOnlyPollProvenance(
        status=cap.ModelStatus.MODEL_ONLY, payload=evidence.payload,
        minted_at=clock.now())


# ---------------------------------------------------------------------------
# Production surfaces: fail-closed stubs. They REFUSE UNCONDITIONALLY
# BEFORE touching any dependency, even a valid PR-3 ModelOnlyCapability.
# ---------------------------------------------------------------------------

def _production_refused():
    raise EnvelopeError(EnvelopeCode.MODEL_ONLY)


def production_seal(*args, **kwargs):
    _production_refused()


def production_verify(*args, **kwargs):
    _production_refused()


def production_dispatch(*args, **kwargs):
    _production_refused()


def production_load(*args, **kwargs):
    _production_refused()


def production_mint_poll_provenance(*args, **kwargs):
    _production_refused()
